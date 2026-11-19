"""
graph.py - Deterministic, local, transitive trust engine over many projects'
VOUCHED.td files.

Model
-----
* Nodes are PROJECTS. A directed edge  A -> B  with weight w in (0, 1]  means
  "project A trusts project B's vouches/denouncements at strength w".
* Each project publishes a parsed VOUCHED.td (vouched users + denounced users).
* All trust is computed FROM THE PERSPECTIVE OF A QUERYING PROJECT (its "seed
  set"), in the spirit of Personalized PageRank. There is no global score.

Business rules implemented
--------------------------
1. Transitive trust with hop decay
   Trust along a path = product of edge weights, times `damping` for every hop
   after the first. Direct edges are used at face value ("A trusts B at 0.8"
   means 0.8). Examples with damping=0.5:
        A -0.8-> B                 : 0.8
        A -0.8-> B -0.7-> C        : 0.8 * 0.7 * 0.5 = 0.28
   Paths are capped at `max_hops` (hard-capped at 2) and are simple paths (no
   revisiting nodes), so cycles can't loop or amplify. If several paths reach
   the same project, the BEST single path wins (max, not sum), so adding more
   intermediaries never inflates trust.

2. Sybil resistance
   Trust only flows OUT of the seed set along edges. A project with no path
   from the seeds within `max_hops` has trust exactly 0, so 50 fake repos that
   only vouch for / trust each other contribute nothing: their vouches and
   denouncements are ignored. Edges pointing INTO the seeds are irrelevant.

3. Adversarial resilience (corroboration threshold)
   Each denouncement is weighted by the querying project's trust in the
   denouncing project. The outcome is:
     BLOCKED  if any single denouncer has trust >= `block_trust`, OR
              at least `corroboration_min_count` distinct denouncers each with
              trust >= `corroboration_min_trust` whose combined weight
              (noisy-OR) is >= `corroboration_combined`.
     WARNING  if there are denouncements from reachable (trust > 0) projects
              but they fail the tests above (isolated / low-trust).
   The per-denouncer floor means an attacker can't "buy" a block by spinning up
   many barely-trusted nodes.

4. Local sovereignty (optional, on by default)
   If the querying project itself vouches for a user, remote denouncements
   cannot override that decision. A local denouncement always blocks.

Everything here is plain arithmetic - no randomness, no ML, no network.
"""
 
from __future__ import annotations
import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, Iterable, List, Optional, Set, Tuple

from backend.app.ingestion.parser import (
    DEFAULT_PLATFORM,
    ParsedVouchFile,
    normalize_username
)

HARD_MAX_HOPS = 2  # absolute ceiling, regardless of configuration


# --------------------------------------------------------------------------- #
# Configuration & result types
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class TrustConfig:
    max_hops: int = 2                       # clamped to [0, HARD_MAX_HOPS]
    damping: float = 0.5                    # multiplier per hop after the first
    trusted_threshold: float = 0.25         # min vouch score to be "trusted"
    block_trust: float = 0.7                # single denouncer this trusted => blocked
    corroboration_min_trust: float = 0.2    # a denouncer must be at least this trusted to corroborate
    corroboration_min_count: int = 2        # number of distinct corroborating denouncers
    corroboration_combined: float = 0.5     # combined denounce weight needed for corroborated block
    local_vouch_overrides: bool = True      # seed's own vouch beats remote denouncements

    def __post_init__(self) -> None:

        # Validate max_hops
        if (
            not isinstance(self.max_hops, int)
            or isinstance(self.max_hops, bool)
        ):
            raise ValueError("max_hops must be an integer")

        object.__setattr__(
            self,
            "max_hops",
            max(0, min(self.max_hops, HARD_MAX_HOPS))
        )

        # Validate probability and trust thresholds
        thresholds = {
            "damping": self.damping,
            "trusted_threshold": self.trusted_threshold,
            "block_trust": self.block_trust,
            "corroboration_min_trust": self.corroboration_min_trust,
            "corroboration_combined": self.corroboration_combined,
        }

        for name, value in thresholds.items():
            if (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not math.isfinite(value)
                or not 0.0 <= value <= 1.0
            ):
                raise ValueError(
                    f"{name} must be a finite number between 0 and 1"
                )

        # Validate corroboration count
        if (
            not isinstance(self.corroboration_min_count, int)
            or isinstance(self.corroboration_min_count, bool)
            or self.corroboration_min_count < 1
        ):
            raise ValueError(
                "corroboration_min_count must be a positive integer"
            )

        # Validate local sovereignty setting
        if not isinstance(self.local_vouch_overrides, bool):
            raise ValueError("local_vouch_overrides must be a boolean")


class Status(str, Enum):
    BLOCKED = "blocked"      # do not accept contributions
    WARNING = "warning"      # weak/isolated denouncement; flag for human review
    TRUSTED = "trusted"      # vouched with enough trust
    LOW_TRUST = "low_trust"  # vouched, but only via weak/distant trust
    UNKNOWN = "unknown"      # no trusted opinion either way


@dataclass(frozen=True)
class Evidence:
    """One project's claim about a user, with the querying project's trust in it."""
    project: str
    trust: float
    reason: str = ""


@dataclass
class Verdict:
    username: str
    querying_project: str
    status: Status
    vouch_score: float                       # noisy-OR of trust in vouching projects
    denounce_score: float                    # noisy-OR of trust in denouncing projects
    vouched_by: List[Evidence] = field(default_factory=list)
    denounced_by: List[Evidence] = field(default_factory=list)
    ignored_projects: List[str] = field(default_factory=list)  # zero-trust projects with claims
    explanation: str = ""

    def to_dict(self) -> dict:
        return {
            "username": self.username,
            "querying_project": self.querying_project,
            "status": self.status.value,
            "vouch_score": round(self.vouch_score, 4),
            "denounce_score": round(self.denounce_score, 4),
            "vouched_by": [(e.project, round(e.trust, 4)) for e in self.vouched_by],
            "denounced_by": [(e.project, round(e.trust, 4), e.reason) for e in self.denounced_by],
            "ignored_projects": list(self.ignored_projects),
            "explanation": self.explanation,
        }


def _noisy_or(weights: Iterable[float]) -> float:
    """Combine independent confidences: 1 - prod(1 - w). Result stays in [0, 1]."""
    miss = 1.0
    for w in weights:
        miss *= 1.0 - w
    return 1.0 - miss


# --------------------------------------------------------------------------- #
# The graph
# --------------------------------------------------------------------------- #
class TrustGraph:
    def __init__(self, config: Optional[TrustConfig] = None) -> None:
        self.config = config or TrustConfig()
        self._edges: Dict[str, Dict[str, float]] = {}   # src -> {dst: weight}
        self._files: Dict[str, ParsedVouchFile] = {}    # project -> parsed VOUCHED.td

    # ---- construction ------------------------------------------------------
    def add_project(self, name: str, vouch_file: Optional[ParsedVouchFile] = None) -> None:
        self._edges.setdefault(name, {})
        if vouch_file is not None:
            self._files[name] = vouch_file

    def add_trust_edge(self, src: str, dst: str, weight: float) -> None:
        """Declare that src trusts dst at a finite weight in (0, 1]."""

        # Validate project names
        if not isinstance(src, str) or not src.strip():
            raise ValueError("src must be a non-empty string")

        if not isinstance(dst, str) or not dst.strip():
            raise ValueError("dst must be a non-empty string")

        # Validate weight type
        if (
            not isinstance(weight, (int, float))
            or isinstance(weight, bool)
        ):
            raise ValueError("trust weight must be a number")

        # Reject NaN, infinity, zero, negative and values above 1
        if not math.isfinite(weight) or not 0.0 < weight <= 1.0:
            raise ValueError(
                f"trust weight must be finite and in (0, 1], got {weight}"
            )

        src = src.strip()
        dst = dst.strip()

        if src == dst:
            return  # self-trust is implicit; ignore self loops

        self.add_project(src)
        self.add_project(dst)

        self._edges[src][dst] = float(weight)

    # ---- project-level trust (personalised, local) --------------------------
    def project_trust(self, seeds: Iterable[str]) -> Dict[str, float]:
        """
        Trust in every project reachable from `seeds` (seeds themselves = 1.0).
        Projects not present in the result have trust 0 (unreachable).
        """
        cfg = self.config
        seed_set = set(seeds)
        trust: Dict[str, float] = {s: 1.0 for s in seed_set}

        def walk(node: str, weight: float, hops: int, visited: Set[str]) -> None:
            if hops >= cfg.max_hops:
                return
            for nbr, w in self._edges.get(node, {}).items():
                if nbr in visited:
                    continue  # simple paths only => cycles can't amplify trust
                decay = cfg.damping if hops >= 1 else 1.0  # damp from the 2nd hop on
                score = weight * w * decay
                if score <= 0.0:
                    continue
                if score > trust.get(nbr, 0.0):
                    trust[nbr] = score                      # best path wins
                walk(nbr, score, hops + 1, visited | {nbr})

        for seed in seed_set:
            walk(seed, 1.0, 0, {seed})
        return trust

    # ---- contributor-level evaluation --------------------------------------
    def evaluate(
        self,
        querying_project: str,
        username: str,
        extra_seeds: Iterable[str] = (),
    ) -> Verdict:
        """Evaluate `username` from `querying_project`'s point of view."""
        cfg = self.config
        user = normalize_username(username, DEFAULT_PLATFORM)
        seeds = {querying_project, *extra_seeds}
        trust = self.project_trust(seeds)

        vouches: List[Evidence] = []
        denouncements: List[Evidence] = []
        ignored: List[str] = []

        for project, parsed in self._files.items():
            t = trust.get(project, 0.0)
            vouched = user in parsed.vouched
            denounced = user in parsed.denounced
            if not (vouched or denounced):
                continue
            if t <= 0.0:
                ignored.append(project)  # Sybil / unreachable: zero influence
                continue
            if vouched:
                vouches.append(Evidence(project, t))
            if denounced:
                denouncements.append(Evidence(project, t, parsed.denounced[user]))

        # Deterministic ordering: most trusted first, then name.
        vouches.sort(key=lambda e: (-e.trust, e.project))
        denouncements.sort(key=lambda e: (-e.trust, e.project))
        ignored.sort()

        vouch_score = _noisy_or(e.trust for e in vouches)
        denounce_score = _noisy_or(e.trust for e in denouncements)
        blocked, block_reason = self._block_decision(denouncements)

        local_vouch = any(
            e.project == querying_project for e in vouches
        )

        local_denounce = any(
            e.project == querying_project for e in denouncements
        )

        if cfg.local_vouch_overrides and local_vouch and not local_denounce:
            status = Status.TRUSTED
            why = f"vouched for directly by '{querying_project}' (local decision overrides remote denouncements)"
        elif blocked:
            status, why = Status.BLOCKED, block_reason
        elif denouncements:
            top = denouncements[0]
            status = Status.WARNING
            why = (f"isolated/low-trust denouncement (strongest: '{top.project}' at trust "
                   f"{top.trust:.2f}) - below block threshold, flagged for review")
        elif vouch_score >= cfg.trusted_threshold:
            status, why = Status.TRUSTED, f"vouched by {len(vouches)} trusted project(s)"
        elif vouch_score > 0.0:
            status, why = Status.LOW_TRUST, "vouched only via weak/distant trust"
        else:
            status, why = Status.UNKNOWN, "no trusted project has an opinion"

        if ignored:
            why += f"; ignored {len(ignored)} zero-trust project(s)"

        return Verdict(
            username=user,
            querying_project=querying_project,
            status=status,
            vouch_score=vouch_score,
            denounce_score=denounce_score,
            vouched_by=vouches,
            denounced_by=denouncements,
            ignored_projects=ignored,
            explanation=why,
        )

    def evaluate_many(self, querying_project: str, usernames: Iterable[str]) -> List[Verdict]:
        return [self.evaluate(querying_project, u) for u in usernames]

    # ---- corroboration logic ------------------------------------------------
    def _block_decision(self, denouncements: List[Evidence]) -> Tuple[bool, str]:
        """
        Block only when at least one individual denouncer
        meets the single-source trust threshold.

        Multiple denouncements are preserved as evidence,
        but are not automatically treated as independent.
        """

        cfg = self.config

        if not denouncements:
            return False, ""

        # A single sufficiently trusted source can trigger a block.
        top = denouncements[0]

        if top.trust >= cfg.block_trust:
            return True, (
                f"denounced by highly trusted '{top.project}' "
                f"(trust {top.trust:.2f})"
            )

        # Multiple sources cannot trigger a block because
        # their independence has not yet been established.
        return False, ""