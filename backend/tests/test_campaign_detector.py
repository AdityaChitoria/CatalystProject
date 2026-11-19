import pytest

from backend.app.campaign.campaign_detector import (
    CampaignDetector,
    build_fingerprint,
    fingerprint_similarity,
    jaccard,
)

T0 = 1_000_000  # arbitrary epoch seconds


# --------------------------------------------------------------------------- #
# Fingerprints & similarity (implemented behaviour)
# --------------------------------------------------------------------------- #
def test_empty_text_and_files_do_not_match():
    fp_a = build_fingerprint("", "", [])
    fp_b = build_fingerprint("Fix login bug", "Updated authentication", ["auth.py"])

    assert fingerprint_similarity(fp_a, fp_b) == 0.0


def test_identical_fingerprints_are_fully_similar():
    fp_a = build_fingerprint("Fix typo", "Small docs change", ["README.md"])
    fp_b = build_fingerprint("Fix typo", "Small docs change", ["./readme.md"])

    assert fingerprint_similarity(fp_a, fp_b) == pytest.approx(1.0)


def test_unrelated_fingerprints_are_not_similar():
    fp_a = build_fingerprint("Fix typo", "Small docs change", ["README.md"])
    fp_b = build_fingerprint("Add OAuth login", "New identity provider", ["auth.py"])

    assert fingerprint_similarity(fp_a, fp_b) < 0.5


def test_jaccard_basic():
    assert jaccard({"a", "b"}, {"b", "c"}) == pytest.approx(1 / 3)
    assert jaccard({"a"}, set()) == 0.0


# --------------------------------------------------------------------------- #
# Campaign detection (implemented behaviour)
# --------------------------------------------------------------------------- #
def _spam_wave(detector, repos, spacing_seconds=600):
    for i, repo in enumerate(repos):
        detector.ingest_pr(
            repo,
            10 + i,
            f"user{i}",
            "Fix typo in README",
            "Please merge this small docs change",
            ["README.md"],
            T0 + i * spacing_seconds,
        )


def test_near_identical_prs_across_three_repos_are_a_campaign():
    detector = CampaignDetector()
    _spam_wave(detector, ["repoA", "repoB", "repoC"])

    result = detector.check_pr_campaign("repoA#10")

    assert result["is_campaign"] is True
    assert result["distinct_repos"] == 3
    assert result["cluster_size"] == 3
    assert result["matching_prs"] == ["repoB#11", "repoC#12"]


def test_two_repos_is_below_the_default_threshold():
    detector = CampaignDetector()
    _spam_wave(detector, ["repoA", "repoB"])

    assert detector.check_pr_campaign("repoA#10")["is_campaign"] is False


def test_prs_outside_the_time_window_are_not_a_campaign():
    detector = CampaignDetector()
    _spam_wave(detector, ["repoA", "repoB", "repoC"], spacing_seconds=5 * 3600)

    assert detector.check_pr_campaign("repoA#10")["is_campaign"] is False


def test_dissimilar_prs_are_not_a_campaign():
    detector = CampaignDetector()
    detector.ingest_pr("repoA", 1, "x", "Fix typo", "docs", ["README.md"], T0)
    detector.ingest_pr("repoB", 2, "y", "Add OAuth login", "new provider", ["auth.py"], T0 + 60)
    detector.ingest_pr("repoC", 3, "z", "Bump deps", "update lockfile", ["poetry.lock"], T0 + 120)

    assert detector.check_pr_campaign("repoA#1")["is_campaign"] is False


def test_unknown_pr_returns_empty_result():
    result = CampaignDetector().check_pr_campaign("missing#1")

    assert result["is_campaign"] is False
    assert result["cluster_size"] == 0


def test_only_hashed_author_and_fingerprint_are_stored():
    detector = CampaignDetector()
    detector.ingest_pr("repoA", 1, "alice", "Secret title", "Secret body", ["a.py"], T0)

    record = detector._records["repoA#1"]
    stored = repr(vars(record)) + repr(record.fingerprint)

    assert "alice" not in stored
    assert "Secret" not in stored


# --------------------------------------------------------------------------- #
# Validation & empty-input handling
# --------------------------------------------------------------------------- #
def test_empty_jaccard_similarity():
    assert jaccard(set(), set()) == 0.0


def test_two_empty_fingerprints_have_zero_similarity():
    fp_a = build_fingerprint("", "", [])
    fp_b = build_fingerprint("", "", [])

    assert fingerprint_similarity(fp_a, fp_b) == 0.0


def test_invalid_num_perm():
    with pytest.raises(ValueError):
        CampaignDetector(num_perm=0)

    with pytest.raises(ValueError):
        CampaignDetector(num_perm=-1)


def test_invalid_shingle_size():
    with pytest.raises(ValueError):
        CampaignDetector(shingle_size=0)

    with pytest.raises(ValueError):
        CampaignDetector(shingle_size=-1)


def test_invalid_author_salt():
    with pytest.raises(ValueError):
        CampaignDetector(author_salt="")

    with pytest.raises(ValueError):
        CampaignDetector(author_salt="   ")


def test_invalid_repo_name():
    detector = CampaignDetector()

    with pytest.raises(ValueError):
        detector.ingest_pr("", 1, "alice", "Fix bug", "", [], 1000)


def test_invalid_author():
    detector = CampaignDetector()

    with pytest.raises(ValueError):
        detector.ingest_pr("repo", 1, "", "Fix bug", "", [], 1000)


def test_invalid_files_changed():
    detector = CampaignDetector()

    with pytest.raises(ValueError):
        detector.ingest_pr("repo", 1, "alice", "Fix bug", "", "auth.py", 1000)


def test_invalid_timestamp():
    detector = CampaignDetector()

    with pytest.raises(ValueError):
        detector.ingest_pr("repo", 1, "alice", "Fix bug", "", [], float("inf"))


def test_valid_constructor_boundaries_are_accepted():
    detector = CampaignDetector(num_perm=1, shingle_size=1, author_salt="x")

    assert detector.num_perm == 1
    assert detector.shingle_size == 1


@pytest.mark.parametrize("bad", [True, 1.5, "64", None])
def test_non_integer_num_perm_rejected(bad):
    with pytest.raises(ValueError):
        CampaignDetector(num_perm=bad)


@pytest.mark.parametrize("bad", [float("nan"), float("-inf"), "1000", None, True])
def test_invalid_timestamp_types_rejected(bad):
    detector = CampaignDetector()

    with pytest.raises(ValueError):
        detector.ingest_pr("repo", 1, "alice", "Fix bug", "", [], bad)


def test_files_changed_none_and_empty_are_allowed():
    detector = CampaignDetector()

    detector.ingest_pr("repo", 1, "alice", "Fix bug", "", None, 1000)
    detector.ingest_pr("repo", 2, "alice", "Fix bug", "", [], 1000)

    assert set(detector._records) == {"repo#1", "repo#2"}


def test_files_changed_with_non_string_entry_rejected():
    detector = CampaignDetector()

    with pytest.raises(ValueError):
        detector.ingest_pr("repo", 1, "alice", "Fix bug", "", ["a.py", 3], 1000)


def test_none_title_and_body_do_not_become_text():
    assert build_fingerprint(None, None, []) == build_fingerprint("", "", [])


def test_empty_prs_across_repos_are_not_a_campaign():
    detector = CampaignDetector()
    for i, repo in enumerate(["repoA", "repoB", "repoC"]):
        detector.ingest_pr(repo, i, f"user{i}", "", "", [], T0 + i * 60)

    assert detector.check_pr_campaign("repoA#0")["is_campaign"] is False
