"""
network_loader.py - Build a TrustGraph from a directory of project folders.

Expected layout
---------------
    <root_dir>/
        <project_name>/
            VOUCHED.td              (or .github/VOUCHED.td - preferred if present)
            TRUSTED_PROJECTS.td     (optional)
        <another_project>/
            ...

Every non-hidden sub-folder of ``root_dir`` is a project named after the folder.

VOUCHED.td
    Parsed by ``parser.parse_vouch_file``. ``.github/VOUCHED.td`` takes
    precedence over ``VOUCHED.td`` when both exist. A project with neither file
    is still added to the graph, just without any vouch data.

TRUSTED_PROJECTS.td
    One ``<project_name> <weight>`` entry per line, weight in (0, 1]:

        # comment
        ghostty 0.90
        fastapi 0.70      # trailing comments are fine too

    Blank lines and ``#`` comments are ignored. Malformed lines (wrong number
    of fields, non-numeric / non-finite / out-of-range weight) are skipped with
    a logged warning - one bad line in someone else's file never crashes the
    load. If a project is listed twice, the last valid entry wins.

Command line
------------
    python backend/app/ingestion/network_loader.py <root_dir> <project> <username>

prints the verdict for ``username`` from ``project``'s point of view.

Standard library only.
"""

from __future__ import annotations

import argparse
import logging
import math
import sys
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

# When run as a script (python backend/app/ingestion/network_loader.py ...) only
# this file's folder is on sys.path, so make the project root importable too.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from backend.app.graph.graph_engine import TrustGraph, Verdict  # noqa: E402
from backend.app.ingestion.parser import parse_vouch_file  # noqa: E402

logger = logging.getLogger(__name__)

VOUCH_FILENAME = "VOUCHED.td"
GITHUB_DIRNAME = ".github"
TRUST_FILENAME = "TRUSTED_PROJECTS.td"

TrustEntry = Tuple[str, float]


# --------------------------------------------------------------------------- #
# TRUSTED_PROJECTS.td parsing
# --------------------------------------------------------------------------- #
def parse_trusted_projects_text(text: str) -> Tuple[List[TrustEntry], List[str]]:
    """
    Parse the text of a TRUSTED_PROJECTS.td file.

    Returns ``(entries, warnings)`` where ``entries`` is a list of
    ``(project_name, weight)`` in file order and ``warnings`` describes every
    skipped line. Never raises on bad content.
    """
    entries: List[TrustEntry] = []
    warnings: List[str] = []

    for lineno, raw_line in enumerate(text.splitlines(), start=1):
        # Drop comments (whole-line or trailing) and surrounding whitespace.
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue

        parts = line.split()
        if len(parts) != 2:
            warnings.append(
                f"line {lineno}: expected '<project_name> <weight>', "
                f"got {raw_line.strip()!r}"
            )
            continue

        name, weight_text = parts
        try:
            weight = float(weight_text)
        except ValueError:
            warnings.append(
                f"line {lineno}: trust weight {weight_text!r} is not a number"
            )
            continue

        if not math.isfinite(weight) or not 0.0 < weight <= 1.0:
            warnings.append(
                f"line {lineno}: trust weight {weight_text!r} must be in (0, 1]"
            )
            continue

        entries.append((name, weight))

    return entries, warnings


def parse_trusted_projects_file(path: str | Path) -> Tuple[List[TrustEntry], List[str]]:
    """Read and parse a TRUSTED_PROJECTS.td file (UTF-8, BOM tolerated)."""
    text = Path(path).read_text(encoding="utf-8-sig")
    return parse_trusted_projects_text(text)


# --------------------------------------------------------------------------- #
# Directory scanning
# --------------------------------------------------------------------------- #
def _project_dirs(root: Path) -> List[Path]:
    """Sorted, non-hidden sub-directories of ``root``; raises if root is unusable."""
    if not root.is_dir():
        raise FileNotFoundError(f"Network directory not found: {root}")
    return sorted(
        p for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")
    )


def _vouch_file_for(project_dir: Path) -> Optional[Path]:
    """``.github/VOUCHED.td`` if present, else ``VOUCHED.td``, else None."""
    for candidate in (
        project_dir / GITHUB_DIRNAME / VOUCH_FILENAME,
        project_dir / VOUCH_FILENAME,
    ):
        if candidate.is_file():
            return candidate
    return None


def load_network_from_dir(root_dir: str | Path) -> TrustGraph:
    """
    Load every project under ``root_dir`` into a new ``TrustGraph``.

    Raises ``FileNotFoundError`` if ``root_dir`` is not a directory. Problems
    inside individual files are logged as warnings and skipped.
    """
    root = Path(root_dir)
    project_dirs = _project_dirs(root)
    graph = TrustGraph()

    # Pass 1: register every project (with its vouch data, if any).
    for project_dir in project_dirs:
        vouch_path = _vouch_file_for(project_dir)
        parsed = None
        if vouch_path is not None:
            try:
                parsed = parse_vouch_file(vouch_path)
            except (OSError, UnicodeDecodeError) as exc:
                logger.warning("%s: could not read file, skipping (%s)", vouch_path, exc)
            else:
                for warning in parsed.warnings:
                    logger.warning("%s: %s", vouch_path, warning)
        graph.add_project(project_dir.name, parsed)

    # Pass 2: trust edges between projects.
    for project_dir in project_dirs:
        trust_path = project_dir / TRUST_FILENAME
        if not trust_path.is_file():
            continue

        try:
            entries, warnings = parse_trusted_projects_file(trust_path)
        except (OSError, UnicodeDecodeError) as exc:
            logger.warning("%s: could not read file, skipping (%s)", trust_path, exc)
            continue

        for warning in warnings:
            logger.warning("%s: %s - skipped", trust_path, warning)

        for target, weight in entries:
            try:
                graph.add_trust_edge(project_dir.name, target, weight)
            except ValueError as exc:  # defensive; entries are pre-validated
                logger.warning("%s: %s - skipped", trust_path, exc)

    return graph


# --------------------------------------------------------------------------- #
# Command line
# --------------------------------------------------------------------------- #
def format_verdict(verdict: Verdict) -> str:
    """Human-readable multi-line rendering of a Verdict."""

    def _evidence(items) -> str:
        if not items:
            return "-"
        return ", ".join(
            f"{e.project} (trust {e.trust:.2f}{', ' + e.reason if e.reason else ''})"
            for e in items
        )

    lines = [
        f"Verdict for {verdict.username} from the perspective of '{verdict.querying_project}'",
        f"  status:         {verdict.status.value.upper()}",
        f"  vouch score:    {verdict.vouch_score:.4f}",
        f"  denounce score: {verdict.denounce_score:.4f}",
        f"  vouched by:     {_evidence(verdict.vouched_by)}",
        f"  denounced by:   {_evidence(verdict.denounced_by)}",
        f"  ignored:        {', '.join(verdict.ignored_projects) or '-'}",
        f"  explanation:    {verdict.explanation}",
    ]
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Print a contributor's verdict from one project's perspective."
    )
    ap.add_argument("root_dir", help="network directory, e.g. mock_network")
    ap.add_argument("project", help="querying project (a sub-folder of root_dir)")
    ap.add_argument("username", help="contributor to evaluate, e.g. alice or github:alice")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.WARNING, format="warning: %(message)s")

    try:
        known = [p.name for p in _project_dirs(Path(args.root_dir))]
        if args.project not in known:
            ap.error(
                f"project {args.project!r} not found in {args.root_dir!r} "
                f"(known: {', '.join(known) or 'none'})"
            )
        graph = load_network_from_dir(args.root_dir)
        verdict = graph.evaluate(args.project, args.username)
    except (FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(format_verdict(verdict))
    return 0


if __name__ == "__main__":
    sys.exit(main())
