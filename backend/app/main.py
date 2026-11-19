"""
main.py - FastAPI service for the Catalyst decentralized contributor trust network.

Run from the project root:
    uvicorn backend.app.main:app --reload

Endpoints
---------
    GET  /api/health            service status
    GET  /api/network           graph structure (nodes + edges) for visualization
    GET  /api/evaluate          graph verdict for one user from one project's view
    POST /api/pr/submit         full deterministic PR pipeline (graph -> campaign -> verdict -> brief)
    POST /api/simulate/spam-wave  scripted 4-repo bot wave + 1 genuine newcomer
    POST /api/reset             clear the in-memory campaign detector

The verdict is ALWAYS computed deterministically (graph + campaign detector).
The LLM, when enabled with use_mock=false, only summarizes those verified facts.
"""

from __future__ import annotations

import os
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from backend.app.campaign.campaign_detector import CampaignDetector
from backend.app.graph.graph_engine import Status, TrustGraph
from backend.app.ingestion.network_loader import load_network_from_dir
from backend.app.ingestion.parser import normalize_username
from backend.app.reporting.brief_generator import generate_brief, live_configured, load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]
NETWORK_DIR = Path(os.environ.get("CATALYST_NETWORK_DIR", PROJECT_ROOT / "mock_network"))


# --------------------------------------------------------------------------- #
# Application state (in-memory, single process)
# --------------------------------------------------------------------------- #
# The graph is loaded once at import time so it is available both under uvicorn
# and under TestClient (which does not run lifespan events unless used as a
# context manager). The detector is swapped out wholesale on reset.
GRAPH: TrustGraph = load_network_from_dir(NETWORK_DIR)

_detector_lock = threading.Lock()  # sync endpoints run in a threadpool
_detector: CampaignDetector = CampaignDetector()


@asynccontextmanager
async def lifespan(_: FastAPI):
    # Read LLM_* settings from a git-ignored .env when the real server starts.
    # Deliberately NOT done at import time, so tests never pick up secrets.
    load_dotenv(PROJECT_ROOT / ".env")
    yield


app = FastAPI(
    title="Catalyst Trust Network API",
    description="Decentralized, evidence-based contributor trust: graph + campaign detection + evidence briefs.",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# --------------------------------------------------------------------------- #
# Pydantic models
# --------------------------------------------------------------------------- #
class HealthResponse(BaseModel):
    status: str
    projects_loaded: int
    live_llm_configured: bool


class NetworkNode(BaseModel):
    id: str
    vouched_count: int
    denounced_count: int


class NetworkEdge(BaseModel):
    source: str
    target: str
    weight: float


class NetworkResponse(BaseModel):
    nodes: List[NetworkNode]
    edges: List[NetworkEdge]


class VerdictModel(BaseModel):
    """Mirror of graph_engine.Verdict.to_dict()."""
    username: str
    querying_project: str
    status: str
    vouch_score: float
    denounce_score: float
    vouched_by: List[Tuple[str, float]]
    denounced_by: List[Tuple[str, float, str]]
    ignored_projects: List[str]
    explanation: str


class EvaluateResponse(VerdictModel):
    project_trust: Dict[str, float]  # trust in every project reachable from the querying project


class CampaignModel(BaseModel):
    """Mirror of CampaignDetector.check_pr_campaign()'s result."""
    is_campaign: bool
    cluster_size: int
    matching_prs: List[str]
    distinct_repos: int
    window_hours: float
    similarity_threshold: float


class BriefModel(BaseModel):
    text: str
    source: str       # "live" | "fallback" | "mock"
    tag: str          # e.g. "[OFFLINE MOCK]"
    status_line: str  # tag plus the fallback reason, when relevant


class PRSubmitRequest(BaseModel):
    repo: str = Field(..., min_length=1, description="Querying project that received the PR")
    pr_id: Union[int, str]
    author: str = Field(..., min_length=1)
    title: str
    body: str = ""
    files_changed: List[str] = Field(default_factory=list)
    timestamp_epoch: Optional[float] = Field(
        default=None, allow_inf_nan=False, description="Defaults to the current time"
    )
    use_mock: bool = True


class PRResult(BaseModel):
    pr_id: Union[int, str]
    author: str
    repo: str
    final_verdict: str
    graph_verdict: VerdictModel
    campaign: CampaignModel
    brief: BriefModel


class SpamWaveRequest(BaseModel):
    use_mock: bool = True


class ResetResponse(BaseModel):
    status: str


# --------------------------------------------------------------------------- #
# Pipeline helpers (mirror backend/tests/test_phase3.py)
# --------------------------------------------------------------------------- #
def known_projects() -> List[str]:
    return sorted(GRAPH._edges)


def require_project(name: str) -> str:
    if name not in GRAPH._edges:
        raise HTTPException(status_code=404, detail=f"Unknown project: {name!r}")
    return name


def require_valid_username(username: str) -> str:
    try:
        return normalize_username(username)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def decide(status: Status, campaign: dict) -> str:
    """Deterministic final verdict from graph status + campaign result. No model involved."""
    if status in (Status.TRUSTED, Status.LOW_TRUST):
        return "ALLOW (Vouched)"
    if status is Status.BLOCKED:
        return "BLOCKED (Denounced)"
    if status is Status.WARNING:
        return "REVIEW (Partial Denouncement)"
    if campaign["is_campaign"]:
        return "BLOCKED (Active Campaign)"
    return "NEWCOMER_TRIAGE"


@dataclass
class _Pending:
    """Everything decided deterministically for one PR, before the brief is written."""
    repo: str
    pr_id: Union[int, str]
    author: str
    verdict: Any    # graph_engine.Verdict
    campaign: dict
    final_verdict: str


def _make_pending(repo, pr_id, author, verdict, campaign) -> _Pending:
    return _Pending(repo, pr_id, author, verdict, campaign, decide(verdict.status, campaign))


def _to_result(p: _Pending, use_mock: bool) -> PRResult:
    """Build the brief payload exactly as test_phase3.py does, then the API response."""
    v = p.verdict
    payload = {
        "pr_id": CampaignDetector.make_key(p.repo, p.pr_id),
        "graph_status": v.status.value,
        "trust_weight": v.vouch_score,
        "denounced_by": [
            {"repo": e.project, "reason": e.reason, "weight": e.trust} for e in v.denounced_by
        ],
        "matching_prs": p.campaign["matching_prs"],
        "cluster_size": p.campaign["cluster_size"],
        "distinct_repos": p.campaign["distinct_repos"],
        "window_hours": p.campaign["window_hours"],
        "final_verdict": p.final_verdict,
    }
    brief = generate_brief(payload, use_mock=use_mock)  # may do network I/O when use_mock=False
    return PRResult(
        pr_id=p.pr_id,
        author=p.author,
        repo=p.repo,
        final_verdict=p.final_verdict,
        graph_verdict=VerdictModel(**v.to_dict()),
        campaign=CampaignModel(**p.campaign),
        brief=BriefModel(
            text=brief.text, source=brief.source, tag=brief.tag, status_line=brief.status_line
        ),
    )


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/api/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(
        status="ok",
        projects_loaded=len(GRAPH._edges),
        live_llm_configured=live_configured(),
    )


@app.get("/api/network", response_model=NetworkResponse)
def network() -> NetworkResponse:
    nodes = []
    for name in known_projects():
        parsed = GRAPH._files.get(name)
        nodes.append(NetworkNode(
            id=name,
            vouched_count=len(parsed.vouched) if parsed else 0,
            denounced_count=len(parsed.denounced) if parsed else 0,
        ))
    edges = [
        NetworkEdge(source=src, target=dst, weight=weight)
        for src in known_projects()
        for dst, weight in sorted(GRAPH._edges[src].items())
    ]
    return NetworkResponse(nodes=nodes, edges=edges)


@app.get("/api/evaluate", response_model=EvaluateResponse)
def evaluate(
    project: str = Query(..., min_length=1, description="Querying project (must exist in the network)"),
    username: str = Query(..., min_length=1, description="Contributor, e.g. 'alice' or 'github:alice'"),
) -> EvaluateResponse:
    require_project(project)
    require_valid_username(username)
    verdict = GRAPH.evaluate(project, username)
    project_trust = {
        name: round(score, 4) for name, score in sorted(GRAPH.project_trust({project}).items())
    }
    return EvaluateResponse(**verdict.to_dict(), project_trust=project_trust)


@app.post("/api/pr/submit", response_model=PRResult)
def submit_pr(req: PRSubmitRequest) -> PRResult:
    repo = require_project(req.repo.strip())
    author = req.author.strip()
    require_valid_username(author)
    timestamp = req.timestamp_epoch if req.timestamp_epoch is not None else time.time()

    global _detector
    with _detector_lock:
        # 1. Graph lookup from the receiving repo's perspective.
        verdict = GRAPH.evaluate(repo, author)
        # 2. Only authors the graph has no opinion on are fingerprinted and campaign-checked.
        if verdict.status is Status.UNKNOWN:
            try:
                _detector.ingest_pr(
                    repo, req.pr_id, author, req.title, req.body, req.files_changed, timestamp
                )
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
        campaign = _detector.check_pr_campaign(CampaignDetector.make_key(repo, req.pr_id))
        # 3. Deterministic verdict.
        pending = _make_pending(repo, req.pr_id, author, verdict, campaign)

    # 4. Evidence brief (outside the lock: live mode may block on the network).
    return _to_result(pending, req.use_mock)


# --- spam-wave simulation --------------------------------------------------- #
_WAVE_PREFERRED_PROJECTS = ["ghostty", "fastapi", "aurora_cli", "rustwave"]
_TYPO_FILES = ["README.md"]
_MIN = 60

# (offset index into the 4 wave projects, pr number, author, title, body, files, minutes after wave start)
_WAVE_SCRIPT = [
    (0, 101, "bot_1", "Fix typo in README", "Fixed a small typo in the README file.", _TYPO_FILES, 0),
    (1, 44, "bot_2", "Fixed typo in readme", "Small typo fix in the readme file.", _TYPO_FILES, 8),
    (2, 7, "bot_3", "Fix typo in README", "Fix a small typo in the README file.", _TYPO_FILES, 16),
    (3, 230, "bot_4", "Fix typos in the README", "Fixed small typos in the readme.", _TYPO_FILES, 24),
    (1, 45, "newcomer_dev", "Add retry with backoff to fetch_url",
     "fetch_url now retries on 5xx with exponential backoff. Adds unit tests.",
     ["src/http.py", "tests/test_http.py"], 20),
]


def _wave_projects() -> List[str]:
    """The four wave targets from mock_network/; falls back to the first four loaded projects."""
    available = known_projects()
    if all(p in GRAPH._edges for p in _WAVE_PREFERRED_PROJECTS):
        return list(_WAVE_PREFERRED_PROJECTS)
    if len(available) < 4:
        raise HTTPException(status_code=409, detail="Spam-wave simulation needs at least 4 loaded projects")
    return available[:4]


@app.post("/api/simulate/spam-wave", response_model=List[PRResult])
def simulate_spam_wave(req: Optional[SpamWaveRequest] = Body(default=None)) -> List[PRResult]:
    use_mock = req.use_mock if req is not None else True
    projects = _wave_projects()
    start = int(time.time()) - 60 * _MIN  # wave began an hour ago; all PRs are in the past

    global _detector
    with _detector_lock:
        _detector = CampaignDetector()  # fresh state for a clean demo

        # Pass 1: graph lookup; only authors nobody trusts has an opinion on enter the detector.
        staged = []
        for proj_idx, pr_id, author, title, body, files, minute in _WAVE_SCRIPT:
            repo = projects[proj_idx]
            verdict = GRAPH.evaluate(repo, author)
            if verdict.status is Status.UNKNOWN:
                _detector.ingest_pr(repo, pr_id, author, title, body, files, start + minute * _MIN)
            staged.append((repo, pr_id, author, verdict))

        # Pass 2: with the whole wave ingested, check each PR and decide.
        pending = [
            _make_pending(
                repo, pr_id, author, verdict,
                _detector.check_pr_campaign(CampaignDetector.make_key(repo, pr_id)),
            )
            for repo, pr_id, author, verdict in staged
        ]

    return [_to_result(p, use_mock) for p in pending]


@app.post("/api/reset", response_model=ResetResponse)
def reset() -> ResetResponse:
    global _detector
    with _detector_lock:
        _detector = CampaignDetector()
    return ResetResponse(status="reset")
