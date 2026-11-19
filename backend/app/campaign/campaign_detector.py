"""
campaign_detector.py - Privacy-preserving cross-project spam campaign detector.

Stores per PR:
- Repository name
- PR ID
- Keyed author hash
- Timestamp
- Keyed structural fingerprint

Never stores raw PR title/body text, IPs, emails, or raw author handles.
"""

import hashlib
import math
import os
import re
import string
from dataclasses import dataclass, field


_PUNCT_TABLE = str.maketrans({c: " " for c in string.punctuation})

MAX_PERMUTATIONS = 256


def normalize_text(text):
    """Lowercase, replace punctuation with spaces, collapse whitespace."""
    if text is None:
        return ""

    if not isinstance(text, str):
        raise ValueError("text must be a string")

    text = text.lower().translate(_PUNCT_TABLE)
    return re.sub(r"\s+", " ", text).strip()


def normalize_path(path):
    """Normalize changed-file paths."""
    if path is None:
        return ""

    if not isinstance(path, str):
        raise ValueError("path must be a string")

    return path.strip().lower().lstrip("./")


def char_shingles(text, k=3):
    """Generate character k-grams."""

    if not isinstance(text, str):
        raise ValueError("text must be a string")

    if (
        not isinstance(k, int)
        or isinstance(k, bool)
        or k <= 0
    ):
        raise ValueError("k must be a positive integer")

    if len(text) <= k:
        return {text} if text else set()

    return {
        text[i:i + k]
        for i in range(len(text) - k + 1)
    }


def _validate_num_perm(num_perm):
    if (
        not isinstance(num_perm, int)
        or isinstance(num_perm, bool)
        or not 1 <= num_perm <= MAX_PERMUTATIONS
    ):
        raise ValueError(
            f"num_perm must be an integer between 1 and {MAX_PERMUTATIONS}"
        )


def _hash64(value, seed, hash_key=None):
    """Generate a stable seeded 64-bit hash."""

    key = hash_key or b""

    digest = hashlib.blake2b(
        value.encode("utf-8"),
        digest_size=8,
        key=key,
        person=seed.to_bytes(8, "big")
    ).digest()

    return int.from_bytes(digest, "big")


def minhash_signature(shingles, num_perm=64, hash_key=None):
    """Generate a MinHash signature."""

    _validate_num_perm(num_perm)

    if not shingles:
        return tuple([0] * num_perm)

    return tuple(
        min(
            _hash64(s, seed, hash_key)
            for s in shingles
        )
        for seed in range(num_perm)
    )


def minhash_similarity(sig_a, sig_b):
    """Estimate Jaccard similarity using MinHash signatures."""

    if not sig_a or len(sig_a) != len(sig_b):
        return 0.0

    return sum(
        1 for a, b in zip(sig_a, sig_b) if a == b
    ) / len(sig_a)


def jaccard(set_a, set_b):
    """Calculate Jaccard similarity."""

    if not set_a and not set_b:
        return 0.0

    union = len(set_a | set_b)

    return len(set_a & set_b) / union if union else 0.0


@dataclass(frozen=True)
class Fingerprint:
    """Structural fingerprint without raw PR text."""

    text_sig: tuple
    file_hashes: frozenset


def build_fingerprint(
    title,
    body,
    files_changed,
    num_perm=64,
    shingle_size=3,
    hash_key=None
):
    """Build a structural fingerprint from PR information."""

    _validate_num_perm(num_perm)

    if not isinstance(title, str):
        raise ValueError("title must be a string")

    if not isinstance(body, str):
        raise ValueError("body must be a string")

    if (
        not isinstance(shingle_size, int)
        or isinstance(shingle_size, bool)
        or shingle_size <= 0
    ):
        raise ValueError("shingle_size must be a positive integer")

    if not isinstance(files_changed, (list, tuple, set)):
        raise ValueError("files_changed must be a list, tuple, or set")

    if not all(
        isinstance(path, str) and path.strip()
        for path in files_changed
    ):
        raise ValueError("All changed file paths must be non-empty strings")

    text = normalize_text(f"{title} {body}")

    text_sig = minhash_signature(
        char_shingles(text, shingle_size),
        num_perm,
        hash_key
    )

    file_hashes = frozenset(
        hashlib.blake2b(
            normalize_path(path).encode("utf-8"),
            digest_size=16,
            key=hash_key or b""
        ).hexdigest()
        for path in files_changed
        if normalize_path(path)
    )

    return Fingerprint(
        text_sig=text_sig,
        file_hashes=file_hashes
    )


def fingerprint_similarity(
    fp_a,
    fp_b,
    text_weight=0.6,
    file_weight=0.4
):
    """Calculate combined text and changed-file similarity."""

    for weight in (text_weight, file_weight):
        if (
            not isinstance(weight, (int, float))
            or isinstance(weight, bool)
            or not math.isfinite(weight)
            or weight < 0
        ):
            raise ValueError("Similarity weights must be finite and non-negative")

    total_weight = text_weight + file_weight

    if total_weight <= 0:
        raise ValueError("At least one similarity weight must be positive")

    text_a_empty = not any(fp_a.text_sig)
    text_b_empty = not any(fp_b.text_sig)

    if text_a_empty or text_b_empty:
        text_sim = 0.0
    else:
        text_sim = minhash_similarity(
            fp_a.text_sig,
            fp_b.text_sig
        )

    file_sim = jaccard(
        fp_a.file_hashes,
        fp_b.file_hashes
    )

    return (
        text_weight * text_sim +
        file_weight * file_sim
    ) / total_weight


@dataclass
class _PRRecord:
    key: str
    repo: str
    pr_id: object
    author_hash: str
    timestamp: float
    fingerprint: Fingerprint = field(repr=False)


class CampaignDetector:
    """Cross-project sliding-window campaign detector."""

    def __init__(
        self,
        num_perm=64,
        shingle_size=3,
        author_salt=None
    ):
        _validate_num_perm(num_perm)

        if (
            not isinstance(shingle_size, int)
            or isinstance(shingle_size, bool)
            or shingle_size <= 0
        ):
            raise ValueError("shingle_size must be a positive integer")

        if author_salt is None:
            author_salt = os.getenv("CAMPAIGN_AUTHOR_SALT")

        if not isinstance(author_salt, str) or not author_salt.strip():
            raise ValueError("author_salt must be configured and non-empty")

        self.num_perm = num_perm
        self.shingle_size = shingle_size

        self._salt = author_salt.encode("utf-8")

        # Derive a fixed-size key for keyed fingerprint hashing.
        self._hash_key = hashlib.sha256(self._salt).digest()

        self._records = {}

    @staticmethod
    def make_key(repo_name, pr_id):
        """Create a stable repository-specific PR key."""
        return f"{repo_name}#{pr_id}"

    def _hash_author(self, author):
        """Hash author identity without storing the original handle."""

        return hashlib.blake2b(
            author.encode("utf-8"),
            digest_size=16,
            key=self._hash_key
        ).hexdigest()

    def ingest_pr(
        self,
        repo_name,
        pr_id,
        author,
        title,
        body,
        files_changed,
        timestamp_epoch
    ):
        """
        Validate and store PR metadata and its structural fingerprint.

        Re-ingesting the same repository/PR key updates its record.
        """

        if (
            not isinstance(repo_name, str)
            or not repo_name.strip()
            or "#" in repo_name
            or any(ord(c) < 32 for c in repo_name)
        ):
            raise ValueError("repo_name must be a valid non-empty repository name")

        if (
            isinstance(pr_id, bool)
            or not isinstance(pr_id, (int, str))
            or not str(pr_id).strip()
        ):
            raise ValueError("pr_id must be a non-empty string or integer")

        if not isinstance(author, str) or not author.strip():
            raise ValueError("author must be a non-empty string")

        if not isinstance(title, str) or not title.strip():
            raise ValueError("title must be a non-empty string")

        if not isinstance(body, str):
            raise ValueError("body must be a string")

        if not isinstance(files_changed, (list, tuple, set)):
            raise ValueError("files_changed must be a list, tuple, or set")

        if not all(
            isinstance(path, str) and path.strip()
            for path in files_changed
        ):
            raise ValueError("All changed file paths must be non-empty strings")

        if (
            isinstance(timestamp_epoch, bool)
            or not isinstance(timestamp_epoch, (int, float))
        ):
            raise ValueError("timestamp_epoch must be a finite number")

        try:
            timestamp = float(timestamp_epoch)
        except (ValueError, OverflowError):
            raise ValueError("timestamp_epoch must be a finite number") from None

        if not math.isfinite(timestamp):
            raise ValueError("timestamp_epoch must be a finite number")

        repo_name = repo_name.strip()
        author = author.strip()

        key = self.make_key(repo_name, pr_id)

        fingerprint = build_fingerprint(
            title,
            body,
            files_changed,
            self.num_perm,
            self.shingle_size,
            self._hash_key
        )

        self._records[key] = _PRRecord(
            key=key,
            repo=repo_name,
            pr_id=pr_id,
            author_hash=self._hash_author(author),
            timestamp=timestamp,
            fingerprint=fingerprint
        )

        return key

    def check_pr_campaign(
        self,
        pr_id,
        window_hours=4,
        similarity_threshold=0.75,
        min_repos=3
    ):
        """
        Detect similar PRs across distinct repositories within a time window.

        The selected cluster must contain the target PR.
        """

        if (
            isinstance(window_hours, bool)
            or not isinstance(window_hours, (int, float))
        ):
            raise ValueError("window_hours must be a finite positive number")

        try:
            window_hours = float(window_hours)
        except (ValueError, OverflowError):
            raise ValueError("window_hours must be a finite positive number") from None

        if not math.isfinite(window_hours) or window_hours <= 0:
            raise ValueError("window_hours must be a finite positive number")

        if (
            isinstance(similarity_threshold, bool)
            or not isinstance(similarity_threshold, (int, float))
        ):
            raise ValueError(
                "similarity_threshold must be a finite number between 0 and 1"
            )

        try:
            similarity_threshold = float(similarity_threshold)
        except (ValueError, OverflowError):
            raise ValueError(
                "similarity_threshold must be a finite number between 0 and 1"
            ) from None

        if (
            not math.isfinite(similarity_threshold)
            or not 0 <= similarity_threshold <= 1
        ):
            raise ValueError(
                "similarity_threshold must be a finite number between 0 and 1"
            )

        if (
            not isinstance(min_repos, int)
            or isinstance(min_repos, bool)
            or min_repos < 2
        ):
            raise ValueError(
                "min_repos must be an integer greater than or equal to 2"
            )

        target = self._resolve(pr_id)

        result = {
            "is_campaign": False,
            "cluster_size": 1 if target else 0,
            "matching_prs": [],
            "distinct_repos": 1 if target else 0,
            "window_hours": window_hours,
            "similarity_threshold": similarity_threshold
        }

        if target is None:
            return result

        window = window_hours * 3600.0

        similar = [
            record
            for record in self._records.values()
            if record.key == target.key
            or fingerprint_similarity(
                target.fingerprint,
                record.fingerprint
            ) >= similarity_threshold
        ]

        # Candidate windows must contain the target PR.
        starts = sorted({
            record.timestamp
            for record in similar
            if target.timestamp - window
            <= record.timestamp
            <= target.timestamp
        })

        best = [target]
        best_start = target.timestamp

        def cluster_score(members, start):
            distinct_repos = len({record.repo for record in members})

            # Prefer more repositories, then more PRs, then earlier windows.
            return (
                distinct_repos,
                len(members),
                -start
            )

        best_score = cluster_score(best, best_start)

        for start in starts:
            members = [
                record
                for record in similar
                if start <= record.timestamp <= start + window
            ]

            if target not in members:
                continue

            score = cluster_score(members, start)

            if score > best_score:
                best = members
                best_start = start
                best_score = score

        # Sort output to make results deterministic.
        best = sorted(
            best,
            key=lambda record: (record.timestamp, record.key)
        )

        distinct = len({record.repo for record in best})

        result["distinct_repos"] = distinct
        result["cluster_size"] = len(best)

        result["matching_prs"] = sorted(
            record.key
            for record in best
            if record.key != target.key
        )

        result["is_campaign"] = distinct >= min_repos

        return result

    def _resolve(self, pr_id):
        """Resolve a full PR key or an unambiguous bare PR ID."""

        if isinstance(pr_id, bool) or not isinstance(pr_id, (str, int)):
            return None

        if not str(pr_id).strip():
            return None

        if isinstance(pr_id, str) and pr_id in self._records:
            return self._records[pr_id]

        matches = [
            record
            for record in self._records.values()
            if str(record.pr_id) == str(pr_id)
        ]

        return matches[0] if len(matches) == 1 else None