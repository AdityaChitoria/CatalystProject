"""
test_phase3.py - End-to-end Phase 3 simulation (graph.py + campaign.py + brief_generator.py).

Run as a script for a printed report:   python test_phase3.py            (offline unless LLM_API_KEY + LLM_BASE_URL are set)
Force live Groq/OpenAI-compatible briefs: python test_phase3.py --live
Force offline briefs:                     python test_phase3.py --mock
Run under pytest for pass/fail:           pytest -q test_phase3.py        (always offline)

Live mode reads LLM_API_KEY / LLM_BASE_URL / LLM_MODEL from the environment or a
git-ignored .env file next to this script (copy .env.example). Each brief is
printed with a provenance tag: [LIVE LLM: model], [FALLBACK MOCK] + reason, or
[OFFLINE MOCK].

Pipeline per incoming PR:
  1. graph.py      TrustGraph.evaluate(repo, author)  -> Status (trusted / blocked / unknown ...)
  2. campaign.py   only authors the graph has NO opinion on (Status.UNKNOWN) are fingerprinted
                   and checked for a cross-repo wave
  3. decide()      deterministic verdict, no model involved
  4. brief_generator.py  summarises the verified facts above (mock mode: fully offline)

Scenario (all four repos mutually trust each other so any repo can be the "querying project"):
  * bot_1..bot_4        unvouched, reworded README typo PR to 4 repos in 24 min  -> BLOCKED (Active Campaign)
  * newcomer_dev        unvouched, unique PR                                      -> NEWCOMER_TRIAGE
  * carol               vouched by alpha-lib, smeared by hostile evil-repo        -> ALLOW (Vouched)
  * mallory             denounced by trusted beta-cli                             -> BLOCKED (Denounced)
"""

from __future__ import annotations

import argparse
import os
from collections import Counter
from pathlib import Path

from backend.app.reporting.brief_generator import current_model, generate_brief, live_configured, load_dotenv
from backend.app.campaign.campaign_detector import CampaignDetector
from backend.app.graph.graph_engine import Status, TrustGraph
from backend.app.ingestion.parser import parse_vouch_text

T0 = 1_700_000_000
MIN = 60

VOUCHED_FILES = {
    "alpha-lib": "github:carol\n",
    "beta-cli": "-github:mallory Repeated low-effort PRs with invented APIs\n",
    "gamma-ui": "# no opinions yet\n",
    "delta-db": "# no opinions yet\n",
    "evil-repo": "-github:carol Fake accusation\n",
}

TRUST_EDGES = [
    ("alpha-lib", "beta-cli", 0.8), ("alpha-lib", "gamma-ui", 0.6),
    ("alpha-lib", "delta-db", 0.5), ("alpha-lib", "evil-repo", 0.3),
    ("beta-cli", "alpha-lib", 0.8), ("gamma-ui", "alpha-lib", 0.6),
    ("delta-db", "alpha-lib", 0.5),
]

TYPO_FILES = ["README.md"]
# (repo, pr_number, author, title, body, files, minutes_after_T0)
INCOMING = [
    ("alpha-lib", 101, "bot_1", "Fix typo in README", "Fixed a small typo in the README file.", TYPO_FILES, 0),
    ("beta-cli", 44, "bot_2", "Fixed typo in readme", "Small typo fix in the readme file.", TYPO_FILES, 8),
    ("gamma-ui", 7, "bot_3", "Fix typo in README", "Fix a small typo in the README file.", TYPO_FILES, 16),
    ("delta-db", 230, "bot_4", "Fix typos in the README", "Fixed small typos in the readme.", TYPO_FILES, 24),
    ("beta-cli", 45, "newcomer_dev", "Add retry with backoff to fetch_url",
     "fetch_url now retries on 5xx with exponential backoff. Adds unit tests.",
     ["src/http.py", "tests/test_http.py"], 20),
    ("alpha-lib", 102, "carol", "Fix race in cache eviction", "Guards eviction with a lock.", ["src/cache.py"], 30),
    ("beta-cli", 46, "mallory", "Improve docs", "Updated docs.", ["docs/index.md"], 35),
]


def build_graph() -> TrustGraph:
    g = TrustGraph()
    for name, text in VOUCHED_FILES.items():
        g.add_project(name, parse_vouch_text(text))
    for src, dst, w in TRUST_EDGES:
        g.add_trust_edge(src, dst, w)
    return g


def decide(status: Status, campaign: dict) -> str:
    """Deterministic verdict from graph status + campaign result."""
    if status in (Status.TRUSTED, Status.LOW_TRUST):
        return "ALLOW (Vouched)"
    if status is Status.BLOCKED:
        return "BLOCKED (Denounced)"
    if status is Status.WARNING:
        return "REVIEW (Partial Denouncement)"
    if campaign["is_campaign"]:
        return "BLOCKED (Active Campaign)"
    return "NEWCOMER_TRIAGE"


def run(verbose: bool = True, use_mock: bool = True) -> dict:
    graph = build_graph()
    detector = CampaignDetector()
    verdicts = {}

    # Pass 1: graph lookup; only authors nobody trusted has an opinion on enter the detector.
    for repo, pr, author, title, body, files, minute in INCOMING:
        v = graph.evaluate(repo, author)
        verdicts[(repo, pr)] = v
        if v.status is Status.UNKNOWN:
            detector.ingest_pr(repo, pr, author, title, body, files, T0 + minute * MIN)

    # Pass 2: campaign check + verdict + brief.
    results = {}
    sources = Counter()
    for repo, pr, author, *_ in INCOMING:
        key = f"{repo}#{pr}"
        v = verdicts[(repo, pr)]
        campaign = detector.check_pr_campaign(key)
        verdict = decide(v.status, campaign)
        payload = {
            "pr_id": key,
            "graph_status": v.status.value,
            "trust_weight": v.vouch_score,
            "denounced_by": [{"repo": e.project, "reason": e.reason, "weight": e.trust}
                             for e in v.denounced_by],
            "matching_prs": campaign["matching_prs"],
            "cluster_size": campaign["cluster_size"],
            "distinct_repos": campaign["distinct_repos"],
            "window_hours": campaign["window_hours"],
            "final_verdict": verdict,
        }
        results[key] = verdict
        brief = generate_brief(payload, use_mock=use_mock)
        sources[brief.source] += 1
        if verbose:
            print(f"[{author}] {key} -> {verdict}")
            print(f"    {brief.status_line}")
            print(f"    {brief.text}")
            if brief.raw_output:
                print(f"    rejected model output: {brief.raw_output[:200]!r}")
            print()
    if verbose:
        print("Brief sources: " + ", ".join(
            f"{sources[s]} {label}" for s, label in
            (("live", "live LLM"), ("fallback", "fallback mock"), ("mock", "offline mock"))))
    return results


def test_pipeline() -> None:
    results = run(verbose=False)
    for repo, pr, author, *_ in INCOMING:
        if author.startswith("bot_"):
            assert results[f"{repo}#{pr}"] == "BLOCKED (Active Campaign)", author
    assert results["beta-cli#45"] == "NEWCOMER_TRIAGE"
    assert results["alpha-lib#102"] == "ALLOW (Vouched)"
    assert results["beta-cli#46"] == "BLOCKED (Denounced)"


def main(argv=None) -> None:
    # Load .env only when run as a script, so importing this module or running
    # pytest never picks up secrets or touches the network.
    load_dotenv(Path(__file__).with_name(".env"))

    ap = argparse.ArgumentParser(description="End-to-end PR pipeline simulation.")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--live", action="store_true",
                      help="use the live LLM for briefs (falls back to mock, with the reason, on any failure)")
    mode.add_argument("--mock", action="store_true",
                      help="force offline deterministic briefs even if LLM env vars are set")
    args = ap.parse_args(argv)

    if args.mock:
        use_mock, why = True, "--mock"
    elif args.live:
        use_mock, why = False, "--live"
    elif live_configured():
        use_mock, why = False, "auto: LLM_API_KEY and LLM_BASE_URL are set"
    else:
        use_mock, why = True, "default: LLM_API_KEY / LLM_BASE_URL not set"

    if use_mock:
        print(f"Brief mode: OFFLINE MOCK ({why})\n")
    else:
        print(f"Brief mode: LIVE LLM model={current_model()} "
              f"base_url={os.environ.get('LLM_BASE_URL', '<unset>')} ({why})\n")

    run(verbose=True, use_mock=use_mock)
    test_pipeline()  # always offline: asserts verdicts only
    print("\nAll Phase 3 assertions passed.")


if __name__ == "__main__":
    main()
