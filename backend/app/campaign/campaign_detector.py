"""
campaign.py - Privacy-preserving cross-project spam campaign detector (Q6).

What is stored per PR:  repo name, PR id, a salted hash of the author handle,
a timestamp, and a structural fingerprint (MinHash signature + hashed file set).
What is NEVER stored:   raw title/body text, IPs, emails, or any account metadata.

Detection is 100% deterministic (shingling + Jaccard/MinHash + a sliding time
window). No model is asked about intent or about whether text was AI-written.
"""

import hashlib
import math
import re
import string
from dataclasses import dataclass, field

_PUNCT_TABLE = str.maketrans({c: " " for c in string.punctuation})


def _is_int(value):
    """True for real ints only (bool is an int subclass, so exclude it)."""
    return isinstance(value, int) and not isinstance(value, bool)


def normalize_text(text):
    """Lowercase, replace punctuation with spaces, collapse whitespace."""
    text = (text or "").lower().translate(_PUNCT_TABLE)
    return re.sub(r"\s+", " ", text).strip()


def normalize_path(path):
    """Normalize a changed-file path so 'README.md' and './readme.md' match."""
    return (path or "").strip().lower().lstrip("./")


def char_shingles(text, k=3):
    """Set of character k-grams. Robust to small rewordings of short text."""
    if len(text) <= k:
        return {text} if text else set()
    return {text[i:i + k] for i in range(len(text) - k + 1)}


def _hash64(value, seed):
    """Seeded 64-bit hash built on hashlib (dependency-free, stable across runs)."""
    digest = hashlib.blake2b(
        value.encode("utf-8"), digest_size=8, key=seed.to_bytes(8, "big")
    ).digest()
    return int.from_bytes(digest, "big")


def minhash_signature(shingles, num_perm=64):
    """MinHash signature: for each seed keep the minimum hash over all shingles."""
    if not shingles:
        return tuple([0] * num_perm)
    return tuple(min(_hash64(s, seed) for s in shingles) for seed in range(num_perm))


def minhash_similarity(sig_a, sig_b):
    """
    Estimated Jaccard similarity = fraction of matching signature slots.

    A signature with no content (the all-zero sentinel from minhash_signature)
    never matches anything, including another empty signature.
    """
    if not sig_a or len(sig_a) != len(sig_b):
        return 0.0
    if not any(sig_a) or not any(sig_b):
        return 0.0
    return sum(1 for a, b in zip(sig_a, sig_b) if a == b) / len(sig_a)


def jaccard(set_a, set_b):
    """Jaccard similarity. Two empty sets have no evidence of overlap -> 0.0."""
    union = len(set_a | set_b)
    return len(set_a & set_b) / union if union else 0.0


@dataclass(frozen=True)
class Fingerprint:
    """Shareable structural fingerprint. Contains no recoverable PR text."""
    text_sig: tuple
    file_hashes: frozenset


def build_fingerprint(title, body, files_changed, num_perm=64, shingle_size=3):
    """Normalize title + body + files and build the structural fingerprint."""
    text = normalize_text(f"{title or ''} {body or ''}")
    text_sig = minhash_signature(char_shingles(text, shingle_size), num_perm)
    file_hashes = frozenset(
        hashlib.blake2b(normalize_path(f).encode("utf-8"), digest_size=8).hexdigest()
        for f in (files_changed or [])
        if normalize_path(f)
    )
    return Fingerprint(text_sig=text_sig, file_hashes=file_hashes)


def fingerprint_similarity(fp_a, fp_b, text_weight=0.6, file_weight=0.4):
    """
    Blend of text similarity (MinHash) and changed-file overlap (Jaccard).
    Weighted so that 'same files + similar wording' scores high even when the
    wording was lightly rephrased, while same files with unrelated text does not.
    """
    text_sim = minhash_similarity(fp_a.text_sig, fp_b.text_sig)
    file_sim = jaccard(fp_a.file_hashes, fp_b.file_hashes)
    return text_weight * text_sim + file_weight * file_sim


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

    def __init__(self, num_perm=64, shingle_size=3, author_salt="catalyst-local-salt"):
        if not _is_int(num_perm) or num_perm < 1:
            raise ValueError("num_perm must be a positive integer")
        if not _is_int(shingle_size) or shingle_size < 1:
            raise ValueError("shingle_size must be a positive integer")
        if not isinstance(author_salt, str) or not author_salt.strip():
            raise ValueError("author_salt must be a non-empty string")
        self.num_perm = num_perm
        self.shingle_size = shingle_size
        self._salt = author_salt
        self._records = {}

    @staticmethod
    def make_key(repo_name, pr_id):
        return f"{repo_name}#{pr_id}"

    def _hash_author(self, author):
        """Salted hash: lets us count distinct authors without keeping handles."""
        return hashlib.sha256(f"{self._salt}:{author}".encode("utf-8")).hexdigest()[:16]

    def ingest_pr(self, repo_name, pr_id, author, title, body, files_changed, timestamp_epoch):
        """
        Store only metadata + fingerprint. Raw title/body are discarded here.

        Raises ValueError for an empty repo name or author, a files_changed that
        is not a list/tuple/set of strings (None means no files), or a
        timestamp that is not a finite number.
        """
        if not isinstance(repo_name, str) or not repo_name.strip():
            raise ValueError("repo_name must be a non-empty string")
        if not isinstance(author, str) or not author.strip():
            raise ValueError("author must be a non-empty string")
        if files_changed is None:
            files_changed = []
        if not isinstance(files_changed, (list, tuple, set, frozenset)) or not all(
            isinstance(f, str) for f in files_changed
        ):
            raise ValueError("files_changed must be a list of file path strings")
        if (
            isinstance(timestamp_epoch, bool)
            or not isinstance(timestamp_epoch, (int, float))
            or not math.isfinite(timestamp_epoch)
        ):
            raise ValueError("timestamp_epoch must be a finite number")

        key = self.make_key(repo_name, pr_id)
        self._records[key] = _PRRecord(
            key=key,
            repo=repo_name,
            pr_id=pr_id,
            author_hash=self._hash_author(author),
            timestamp=float(timestamp_epoch),
            fingerprint=build_fingerprint(
                title, body, files_changed, self.num_perm, self.shingle_size
            ),
        )
        return key

    def check_pr_campaign(self, pr_id, window_hours=4, similarity_threshold=0.75, min_repos=3):
        """
        Is this PR part of a near-identical wave hitting >= min_repos distinct
        repos inside one sliding window of window_hours?

        pr_id may be the full key ("repoA#12") or a bare id if it is unambiguous.
        Returns: {is_campaign, cluster_size, matching_prs, distinct_repos,
                  window_hours, similarity_threshold}
        """
        target = self._resolve(pr_id)
        result = {
            "is_campaign": False,
            "cluster_size": 1 if target else 0,
            "matching_prs": [],
            "distinct_repos": 1 if target else 0,
            "window_hours": window_hours,
            "similarity_threshold": similarity_threshold,
        }
        if target is None:
            return result

        window = window_hours * 3600.0
        similar = [
            r for r in self._records.values()
            if r.key == target.key
            or fingerprint_similarity(target.fingerprint, r.fingerprint) >= similarity_threshold
        ]

        # Sliding window: try every window [start, start+window] that contains the
        # target and begins at one of the similar PRs' timestamps; keep the best.
        starts = [r.timestamp for r in similar
                  if target.timestamp - window <= r.timestamp <= target.timestamp]
        best = [target]
        for start in starts:
            members = [r for r in similar if start <= r.timestamp <= start + window]
            if target not in members:
                continue
            if len({r.repo for r in members}) > len({r.repo for r in best}):
                best = members

        distinct = len({r.repo for r in best})
        result["distinct_repos"] = distinct
        result["cluster_size"] = len(best)
        result["matching_prs"] = sorted(r.key for r in best if r.key != target.key)
        result["is_campaign"] = distinct >= min_repos
        return result

    def _resolve(self, pr_id):
        if pr_id in self._records:
            return self._records[pr_id]
        matches = [r for r in self._records.values() if str(r.pr_id) == str(pr_id)]
        return matches[0] if len(matches) == 1 else None
