"""
test_engine.py - Mock network demo + assertions.

Run as a script for a printed report:   python test_engine.py
Run under pytest for pass/fail:         pytest -q test_engine.py

Network (all trust edges are configured by the *source* project):

    Project A (querying) --0.8--> Project B --0.7--> Project C
    Project A            --0.4--> Project D --0.3--> Project H (hostile)
    Project H            --1.0--> fake-000 ... a ring of 50 sybil repos
    (every sybil also "trusts" Project A and each other at 1.0 - irrelevant,
     because nothing flows INTO the sybils from A's seed set)

Resulting trust from A:  B=0.80  D=0.40  C=0.8*0.7*0.5=0.28  H=0.4*0.3*0.5=0.06
Sybils are 3 hops away (A->D->H->fake) so they get exactly 0.
"""

from __future__ import annotations

from backend.app.graph.graph_engine import Status, TrustGraph
from backend.app.ingestion.parser import parse_vouch_text

N_SYBILS = 50

VOUCHED_FILES = {
    "ProjectA": """
        # Project A - our own list
        github:erin
        -github:mallory Repeated low-effort AI PRs
    """,
    "ProjectB": """
        alice
        github:mallory
        github:erin
        -github:spambot99 Submitted AI slop PRs
    """,
    "ProjectC": """
        bob
        -github:carol Mass-generated PRs with hallucinated APIs
    """,
    "ProjectD": """
        -github:carol Hallucinated patches
        -github:dana Disagreement over a review
    """,
    "ProjectH": """
        # Hostile project: smears a good contributor, launders a spammer
        -github:alice Fake accusation
        github:spammer_pal
    """,
}


def build_network() -> TrustGraph:
    g = TrustGraph()
    for name, text in VOUCHED_FILES.items():
        g.add_project(name, parse_vouch_text(text))

    g.add_trust_edge("ProjectA", "ProjectB", 0.8)
    g.add_trust_edge("ProjectB", "ProjectC", 0.7)
    g.add_trust_edge("ProjectA", "ProjectD", 0.4)
    g.add_trust_edge("ProjectD", "ProjectH", 0.3)

    # Sybil ring: vouch for each other + a shill, denounce a good contributor.
    fakes = [f"fake-{i:03d}" for i in range(N_SYBILS)]
    for i, fake in enumerate(fakes):
        lines = [f.replace("fake-", "sybil_") for f in fakes if f != fake]
        lines += ["sybil_shill", "-github:alice Totally a spammer"]
        g.add_project(fake, parse_vouch_text("\n".join(lines)))
        g.add_trust_edge(fake, "ProjectA", 1.0)                      # inbound to A: useless
        g.add_trust_edge(fake, fakes[(i + 1) % N_SYBILS], 1.0)       # ring
    g.add_trust_edge("ProjectH", fakes[0], 1.0)                      # 3 hops from A: capped
    return g


# username -> (expected status, short description)
EXPECTED = {
    "alice": (Status.WARNING, "vouched by B; smeared by hostile H + 50 sybils -> only a warning"),
    "bob": (Status.TRUSTED, "vouched by C at 2 hops (0.28)"),
    "spambot99": (Status.BLOCKED, "denounced by highly trusted B"),
    "carol": (Status.BLOCKED, "denounced by D and C -> corroborated"),
    "dana": (Status.WARNING, "denounced by D alone -> isolated"),
    "mallory": (Status.BLOCKED, "A denounces locally even though B vouches"),
    "erin": (Status.TRUSTED, "A vouches locally; B vouch is irrelevant"),
    "spammer_pal": (Status.LOW_TRUST, "only the hostile H vouches (0.06)"),
    "sybil_shill": (Status.UNKNOWN, f"vouched by {N_SYBILS} sybils -> trust 0"),
    "total_stranger": (Status.UNKNOWN, "nobody has heard of them"),
}


def test_project_trust_values_and_sybil_isolation() -> None:
    g = build_network()
    t = g.project_trust({"ProjectA"})
    assert t["ProjectA"] == 1.0
    assert abs(t["ProjectB"] - 0.8) < 1e-9
    assert abs(t["ProjectC"] - 0.28) < 1e-9          # 0.8 * 0.7 * 0.5
    assert abs(t["ProjectH"] - 0.06) < 1e-9          # 0.4 * 0.3 * 0.5
    assert not any(k.startswith("fake-") for k in t)  # sybils: strictly zero


def test_contributor_verdicts() -> None:
    g = build_network()
    for user, (expected, _) in EXPECTED.items():
        v = g.evaluate("ProjectA", user)
        assert v.status == expected, f"{user}: got {v.status}, expected {expected}"
    assert g.evaluate("ProjectA", "sybil_shill").vouch_score == 0.0


def main() -> None:
    g = build_network()

    print("=== Project trust, from ProjectA's perspective ===")
    trust = g.project_trust({"ProjectA"})
    for proj, score in sorted(trust.items(), key=lambda kv: -kv[1]):
        print(f"  {proj:<10} {score:.2f}")
    print(f"  (all {N_SYBILS} fake-* sybil repos: 0.00 - unreachable)\n")

    print("=== Contributor verdicts ===")
    print(f"{'user':<24}{'status':<11}{'vouch':>6}{'denounce':>10}  expected / note")
    print("-" * 100)
    ok = True
    for user, (expected, note) in EXPECTED.items():
        v = g.evaluate("ProjectA", user)
        match = "OK  " if v.status == expected else "FAIL"
        ok &= v.status == expected
        print(f"{v.username:<24}{v.status.value:<11}{v.vouch_score:>6.2f}{v.denounce_score:>10.2f}  "
              f"[{match}] {note}")
        print(f"{'':<24}-> {v.explanation}")
    print("\nAll expectations met." if ok else "\nSOME EXPECTATIONS FAILED.")


if __name__ == "__main__":
    main()
