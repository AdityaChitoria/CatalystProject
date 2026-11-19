import pytest

from backend.app.campaign.campaign_detector import (
    CampaignDetector,
    build_fingerprint,
    fingerprint_similarity,
    jaccard,
)


def test_empty_jaccard_similarity():
    assert jaccard(set(), set()) == 0.0


def test_two_empty_fingerprints_have_zero_similarity():
    fp_a = build_fingerprint("", "", [])
    fp_b = build_fingerprint("", "", [])

    assert fingerprint_similarity(fp_a, fp_b) == 0.0


def test_empty_text_and_files_do_not_match():
    fp_a = build_fingerprint("", "", [])
    fp_b = build_fingerprint("Fix login bug", "Updated authentication", ["auth.py"])

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

@pytest.fixture(autouse=True)
def campaign_salt(monkeypatch):
    monkeypatch.setenv("CAMPAIGN_AUTHOR_SALT", "test-only-secret")   

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