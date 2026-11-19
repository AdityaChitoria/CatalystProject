"""
test_api.py - Automated tests for the Catalyst FastAPI service.

Every PR submission uses use_mock=True, so these tests run 100% offline: no LLM
key, no network. The app is loaded from the real mock_network/ directory.

Run from the project root:   python -m pytest backend/tests/test_api.py -v
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.app.main import app

MOCK_NETWORK = Path(__file__).resolve().parents[2] / "mock_network"
N_PROJECTS = sum(1 for p in MOCK_NETWORK.iterdir() if p.is_dir() and not p.name.startswith("."))

T0 = 1_700_000_000
TYPO_FILES = ["README.md"]
WAVE_BOTS = {"bot_1", "bot_2", "bot_3", "bot_4"}


@pytest.fixture(scope="module")
def client():
    return TestClient(app)


@pytest.fixture(autouse=True)
def fresh_detector(client):
    """The campaign detector is process-wide state: start every test from a clean slate."""
    assert client.post("/api/reset").status_code == 200
    yield


def submit(client, repo, pr_id, author, title, body="", files=None, ts=None, **extra):
    payload = {
        "repo": repo,
        "pr_id": pr_id,
        "author": author,
        "title": title,
        "body": body,
        "files_changed": files if files is not None else [],
        "use_mock": True,
        **extra,
    }
    if ts is not None:
        payload["timestamp_epoch"] = ts
    return client.post("/api/pr/submit", json=payload)


# --------------------------------------------------------------------------- #
# CORS
# --------------------------------------------------------------------------- #
def test_cors_allows_any_origin(client):
    resp = client.get("/api/health", headers={"Origin": "http://localhost:5173"})
    assert resp.headers["access-control-allow-origin"] == "*"

    preflight = client.options(
        "/api/pr/submit",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )
    assert preflight.status_code == 200
    assert preflight.headers["access-control-allow-origin"] == "*"


# --------------------------------------------------------------------------- #
# GET /api/health
# --------------------------------------------------------------------------- #
def test_health(client):
    resp = client.get("/api/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["projects_loaded"] == N_PROJECTS
    assert isinstance(body["live_llm_configured"], bool)


# --------------------------------------------------------------------------- #
# GET /api/network
# --------------------------------------------------------------------------- #
def test_network_structure(client):
    resp = client.get("/api/network")
    assert resp.status_code == 200
    body = resp.json()

    nodes = {n["id"]: n for n in body["nodes"]}
    assert len(nodes) == N_PROJECTS
    for node in nodes.values():
        assert set(node) == {"id", "vouched_count", "denounced_count"}
        assert node["vouched_count"] >= 0 and node["denounced_count"] >= 0

    # fastapi/VOUCHED.td denounces exactly spambot99 and eve.
    assert nodes["fastapi"]["denounced_count"] == 2
    assert nodes["fastapi"]["vouched_count"] > 0

    assert body["edges"], "expected at least one trust edge"
    for edge in body["edges"]:
        assert set(edge) == {"source", "target", "weight"}
        assert edge["source"] in nodes and edge["target"] in nodes
        assert 0.0 < edge["weight"] <= 1.0

    # ghostty/TRUSTED_PROJECTS.td: "fastapi 0.85"
    assert any(
        e["source"] == "ghostty" and e["target"] == "fastapi" and e["weight"] == pytest.approx(0.85)
        for e in body["edges"]
    )


# --------------------------------------------------------------------------- #
# GET /api/evaluate
# --------------------------------------------------------------------------- #
def test_evaluate_blocked_user(client):
    resp = client.get("/api/evaluate", params={"project": "my_project", "username": "spambot99"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "blocked"
    assert body["username"] == "github:spambot99"
    assert body["querying_project"] == "my_project"
    assert body["denounce_score"] > 0
    assert any(project == "ghostty" for project, *_ in body["denounced_by"])
    # Reachable project-trust map: the querying project itself is 1.0, ghostty is 0.90.
    assert body["project_trust"]["my_project"] == pytest.approx(1.0)
    assert body["project_trust"]["ghostty"] == pytest.approx(0.9)


def test_evaluate_trusted_and_unknown(client):
    trusted = client.get("/api/evaluate", params={"project": "my_project", "username": "bob"}).json()
    assert trusted["status"] == "trusted"
    assert trusted["vouch_score"] > 0

    unknown = client.get("/api/evaluate", params={"project": "my_project", "username": "nobody_xyz"}).json()
    assert unknown["status"] == "unknown"
    assert unknown["vouched_by"] == [] and unknown["denounced_by"] == []


def test_evaluate_accepts_platform_prefixed_username(client):
    resp = client.get("/api/evaluate", params={"project": "ghostty", "username": "github:MitchellH"})
    assert resp.status_code == 200
    assert resp.json()["username"] == "github:mitchellh"


def test_evaluate_unknown_project_is_404(client):
    resp = client.get("/api/evaluate", params={"project": "no_such_project", "username": "alice"})
    assert resp.status_code == 404
    assert "no_such_project" in resp.json()["detail"]


def test_evaluate_invalid_inputs_are_422(client):
    assert client.get("/api/evaluate", params={"username": "alice"}).status_code == 422
    assert client.get("/api/evaluate", params={"project": "my_project"}).status_code == 422
    assert client.get("/api/evaluate", params={"project": "my_project", "username": ""}).status_code == 422
    bad = client.get("/api/evaluate", params={"project": "my_project", "username": "not a valid name!"})
    assert bad.status_code == 422


# --------------------------------------------------------------------------- #
# POST /api/pr/submit
# --------------------------------------------------------------------------- #
def test_submit_response_shape(client):
    resp = submit(client, "my_project", 1, "bob", "Fix cache bug", "Guards eviction.", ["src/cache.py"], ts=T0)
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"pr_id", "author", "repo", "final_verdict", "graph_verdict", "campaign", "brief"}
    assert body["pr_id"] == 1 and body["author"] == "bob" and body["repo"] == "my_project"
    assert set(body["brief"]) == {"text", "source", "tag", "status_line"}
    assert body["graph_verdict"]["querying_project"] == "my_project"


@pytest.mark.parametrize(
    "author, expected_verdict, expected_status",
    [
        ("bob", "ALLOW (Vouched)", "trusted"),
        ("spambot99", "BLOCKED (Denounced)", "blocked"),
        ("alice", "REVIEW (Partial Denouncement)", "warning"),   # vouched, but low-trust hostile denouncement
        ("nobody_xyz", "NEWCOMER_TRIAGE", "unknown"),
    ],
)
def test_submit_graph_driven_verdicts(client, author, expected_verdict, expected_status):
    body = submit(client, "my_project", 10, author, "Improve docs", "Updated docs.", ["docs/index.md"], ts=T0).json()
    assert body["final_verdict"] == expected_verdict
    assert body["graph_verdict"]["status"] == expected_status


def test_submit_mock_brief_is_offline_and_deterministic(client):
    body = submit(client, "my_project", 11, "spambot99", "Improve docs", "Updated docs.", ["docs/index.md"], ts=T0).json()
    brief = body["brief"]
    assert brief["source"] == "mock"
    assert brief["tag"] == "[OFFLINE MOCK]"
    assert brief["status_line"] == "[OFFLINE MOCK]"
    assert "my_project#11" in brief["text"]
    assert "BLOCKED (Denounced)" in brief["text"]


def test_submit_defaults_use_mock_and_timestamp(client):
    resp = client.post(
        "/api/pr/submit",
        json={"repo": "my_project", "pr_id": 12, "author": "nobody_xyz", "title": "Tweak", "body": "x", "files_changed": ["a.py"]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["brief"]["source"] == "mock"  # use_mock defaults to True
    assert body["final_verdict"] == "NEWCOMER_TRIAGE"


def test_submit_only_unknown_authors_enter_campaign_detector(client):
    # A vouched author's PR is never fingerprinted, so the campaign result stays empty.
    body = submit(client, "my_project", 13, "bob", "Fix typo in README", "Fixed a typo.", TYPO_FILES, ts=T0).json()
    assert body["campaign"]["is_campaign"] is False
    assert body["campaign"]["cluster_size"] == 0
    assert body["campaign"]["matching_prs"] == []


def test_submit_detects_campaign_incrementally(client):
    waves = [
        ("ghostty", 1, "bot_1", "Fix typo in README", "Fixed a small typo in the README file.", 0),
        ("fastapi", 2, "bot_2", "Fixed typo in readme", "Small typo fix in the readme file.", 8),
        ("aurora_cli", 3, "bot_3", "Fix typo in README", "Fix a small typo in the README file.", 16),
    ]
    results = [submit(client, r, n, a, t, b, TYPO_FILES, ts=T0 + m * 60).json() for r, n, a, t, b, m in waves]

    # Two repos so far is below the 3-repo threshold; the third submission completes the wave.
    assert results[0]["final_verdict"] == "NEWCOMER_TRIAGE"
    assert results[1]["final_verdict"] == "NEWCOMER_TRIAGE"
    assert results[2]["final_verdict"] == "BLOCKED (Active Campaign)"
    assert results[2]["campaign"]["is_campaign"] is True
    assert results[2]["campaign"]["distinct_repos"] == 3
    assert sorted(results[2]["campaign"]["matching_prs"]) == ["fastapi#2", "ghostty#1"]
    assert "ghostty#1" in results[2]["brief"]["text"]


def test_submit_unknown_repo_is_404(client):
    resp = submit(client, "no_such_repo", 1, "bob", "t", ts=T0)
    assert resp.status_code == 404


def test_submit_invalid_inputs(client):
    # Invalid username -> 422
    assert submit(client, "my_project", 1, "not a valid name!", "t", ts=T0).status_code == 422
    # Missing required fields -> 422
    assert client.post("/api/pr/submit", json={"repo": "my_project"}).status_code == 422
    # Empty author -> 422
    assert submit(client, "my_project", 1, "", "t", ts=T0).status_code == 422
    # Wrong type for files_changed -> 422
    bad_files = client.post(
        "/api/pr/submit",
        json={"repo": "my_project", "pr_id": 1, "author": "bob", "title": "t", "files_changed": "README.md"},
    )
    assert bad_files.status_code == 422
    # Non-numeric timestamp -> 422
    bad_ts = client.post(
        "/api/pr/submit",
        json={"repo": "my_project", "pr_id": 1, "author": "bob", "title": "t", "timestamp_epoch": "yesterday"},
    )
    assert bad_ts.status_code == 422


# --------------------------------------------------------------------------- #
# POST /api/simulate/spam-wave
# --------------------------------------------------------------------------- #
def _assert_wave(results):
    assert len(results) == 5
    by_author = {r["author"]: r for r in results}
    assert set(by_author) == WAVE_BOTS | {"newcomer_dev"}

    # All 4 bots are flagged as an active campaign...
    for bot in WAVE_BOTS:
        r = by_author[bot]
        assert r["final_verdict"] == "BLOCKED (Active Campaign)", bot
        assert r["graph_verdict"]["status"] == "unknown"
        assert r["campaign"]["is_campaign"] is True
        assert r["campaign"]["distinct_repos"] >= 3
        assert r["brief"]["source"] == "mock"

    # ...they hit 4 different projects...
    assert len({by_author[b]["repo"] for b in WAVE_BOTS}) == 4

    # ...and the genuine newcomer is routed to triage, not swept up in the campaign.
    newcomer = by_author["newcomer_dev"]
    assert newcomer["final_verdict"] == "NEWCOMER_TRIAGE"
    assert newcomer["campaign"]["is_campaign"] is False
    assert newcomer["campaign"]["matching_prs"] == []


def test_simulate_spam_wave_without_body(client):
    resp = client.post("/api/simulate/spam-wave")
    assert resp.status_code == 200
    _assert_wave(resp.json())


def test_simulate_spam_wave_explicit_mock(client):
    resp = client.post("/api/simulate/spam-wave", json={"use_mock": True})
    assert resp.status_code == 200
    _assert_wave(resp.json())


def test_simulate_spam_wave_is_repeatable(client):
    first = client.post("/api/simulate/spam-wave", json={"use_mock": True}).json()
    second = client.post("/api/simulate/spam-wave", json={"use_mock": True}).json()
    _assert_wave(first)
    _assert_wave(second)  # detector is reset each run, so the result does not drift
    assert [r["final_verdict"] for r in first] == [r["final_verdict"] for r in second]


def test_simulate_spam_wave_rejects_bad_body(client):
    assert client.post("/api/simulate/spam-wave", json={"use_mock": "not-a-bool"}).status_code == 422


# --------------------------------------------------------------------------- #
# POST /api/reset
# --------------------------------------------------------------------------- #
def test_reset_returns_status(client):
    resp = client.post("/api/reset")
    assert resp.status_code == 200
    assert resp.json() == {"status": "reset"}


def test_reset_clears_campaign_state(client):
    submit(client, "ghostty", 1, "bot_1", "Fix typo in README", "Fixed a small typo in the README file.", TYPO_FILES, ts=T0)
    submit(client, "fastapi", 2, "bot_2", "Fixed typo in readme", "Small typo fix in the readme file.", TYPO_FILES, ts=T0 + 480)

    assert client.post("/api/reset").json() == {"status": "reset"}

    # Without the two earlier PRs, a third similar PR is no longer a 3-repo campaign.
    third = submit(
        client, "aurora_cli", 3, "bot_3", "Fix typo in README", "Fix a small typo in the README file.",
        TYPO_FILES, ts=T0 + 960,
    ).json()
    assert third["final_verdict"] == "NEWCOMER_TRIAGE"
    assert third["campaign"]["is_campaign"] is False
    assert third["campaign"]["matching_prs"] == []
