"""
parser.py - Parser for `.github/VOUCHED.td` files.

File format
-----------
    # comment                         -> ignored
    alice                             -> alice is VOUCHED
    github:mitchellh                  -> github:mitchellh is VOUCHED
    -github:spambot99 Submitted AI slop PRs
                                      -> github:spambot99 is DENOUNCED, with a reason

Design notes
------------
* Pure text parsing. Deterministic. No network, no LLMs.
* Usernames are normalised (lower-cased, whitespace-stripped, and bare names are
  prefixed with a default platform, so `Alice` and `github:alice` are the same
  identity). Pass `default_platform=None` to disable platform prefixing.
* If the same user appears more than once in a file, the LAST entry wins. A
  user therefore never appears as both vouched and denounced in one file.
* Malformed lines never raise; they are skipped and reported in `warnings`
  so that one bad line in a stranger's file can't crash the engine.

NOTE: this module is named `parser`; the stdlib module of the same name was
removed in Python 3.10, so run on Python >= 3.10.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set

DEFAULT_PLATFORM = "github"

# Optional "platform:" prefix followed by a name. Applied after lower-casing.
_USERNAME_RE = re.compile(r"^(?:[a-z0-9_.-]+:)?[a-z0-9_.-]+$")


@dataclass
class ParsedVouchFile:
    """Structured result of parsing one VOUCHED.td file."""

    vouched: Set[str] = field(default_factory=set)
    # username -> reason (reason may be an empty string)
    denounced: Dict[str, str] = field(default_factory=dict)
    unvouched: Set[str] = field(default_factory=set)
    # Human-readable notes about malformed lines that were skipped.
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        """JSON-friendly view with stable ordering."""
        return {
            "vouched": sorted(self.vouched),
            "denounced": dict(sorted(self.denounced.items())),
            "unvouched": sorted(self.unvouched),
            "warnings": list(self.warnings),
        }


def normalize_username(raw: str, default_platform: Optional[str] = DEFAULT_PLATFORM) -> str:
    """
    Canonicalise a username: strip, lower-case, and add `default_platform:`
    to bare names. Raises ValueError if the result isn't a valid identity.
    """
    name = raw.strip().lower()
    if not _USERNAME_RE.match(name):
        raise ValueError(f"invalid username: {raw!r}")
    if default_platform and ":" not in name:
        name = f"{default_platform.lower()}:{name}"
    return name


def parse_vouch_text(text: str, default_platform: Optional[str] = DEFAULT_PLATFORM) -> ParsedVouchFile:
    """Parse the contents of a VOUCHED.td file."""
    result = ParsedVouchFile()

    for lineno, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()

        # Blank lines and comments.
        if not line or line.startswith("#"):
            continue

        # ---- Denounced entry: "-username reason..." -------------------------
        if line.startswith("-"):
            body = line[1:].strip()  # tolerate "- username reason" too
            if not body:
                result.warnings.append(f"line {lineno}: bare '-' with no username")
                continue
            parts = body.split(None, 1)  # first word = username, rest = reason
            reason = parts[1].strip() if len(parts) > 1 else ""
            try:
                user = normalize_username(parts[0], default_platform)
            except ValueError as exc:
                result.warnings.append(f"line {lineno}: {exc}")
                continue
            result.vouched.discard(user)
            result.unvouched.discard(user)
            result.denounced[user] = reason
            continue

        # ---- Unvouched entry: "?username" -------------------------------
        if line.startswith("?"):
            body = line[1:].strip()
            tokens = body.split()

            if not tokens:
                result.warnings.append(
                    f"line {lineno}: bare '?' with no username"
                )
                continue

            try:
                user = normalize_username(tokens[0], default_platform)
            except ValueError as exc:
                result.warnings.append(f"line {lineno}: {exc}")
                continue

            result.vouched.discard(user)
            result.denounced.pop(user, None)
            result.unvouched.add(user)
            continue

        # ---- Vouched entry: a single username --------------------------------
        tokens = line.split()
        if len(tokens) > 1:
            result.warnings.append(
                f"line {lineno}: extra text after username ignored: {' '.join(tokens[1:])!r}"
            )
        try:
            user = normalize_username(tokens[0], default_platform)
        except ValueError as exc:
            result.warnings.append(f"line {lineno}: {exc}")
            continue
        result.denounced.pop(user, None)
        result.unvouched.discard(user)
        result.vouched.add(user)

    return result


def parse_vouch_file(path: str | Path, default_platform: Optional[str] = DEFAULT_PLATFORM) -> ParsedVouchFile:
    """Read and parse a VOUCHED.td file from disk (UTF-8, BOM tolerated)."""
    text = Path(path).read_text(encoding="utf-8-sig")
    return parse_vouch_text(text, default_platform)


if __name__ == "__main__":
    import json
    import sys

    if len(sys.argv) != 2:
        sys.exit("usage: python parser.py path/to/VOUCHED.td")
    print(json.dumps(parse_vouch_file(sys.argv[1]).to_dict(), indent=2))
