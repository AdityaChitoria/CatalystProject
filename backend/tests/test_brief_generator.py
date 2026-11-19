import urllib.error

from backend.app.reporting import brief_generator as bg


PAYLOAD = {
    "pr_id": "alpha-lib#101",
    "graph_status": "unknown",
    "trust_weight": 0.0,
    "matching_prs": [],
    "final_verdict": "NEWCOMER_TRIAGE",
}


def configure_llm(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setenv("LLM_BASE_URL", "https://example.com")


def test_llm_success(monkeypatch):
    configure_llm(monkeypatch)

    expected = (
        "The graph status is unknown. "
        "The final verdict is NEWCOMER_TRIAGE."
    )

    monkeypatch.setattr(
        bg,
        "_call_llm",
        lambda *args, **kwargs: expected
    )

    result = bg.generate_evidence_brief(PAYLOAD, use_mock=False)

    assert result == expected


def test_llm_rejects_invented_pr_reference(monkeypatch):
    configure_llm(monkeypatch)

    fake_response = "Evidence also appears in attacker/repo#999."

    monkeypatch.setattr(
        bg,
        "_call_llm",
        lambda *args, **kwargs: fake_response
    )

    result = bg.generate_evidence_brief(PAYLOAD, use_mock=False)

    assert result == bg.build_mock_brief(PAYLOAD)


def test_llm_timeout_uses_mock_fallback(monkeypatch):
    configure_llm(monkeypatch)

    def timeout(*args, **kwargs):
        raise urllib.error.URLError("Connection timed out")

    monkeypatch.setattr(bg, "_call_llm", timeout)

    result = bg.generate_evidence_brief(PAYLOAD, use_mock=False)

    assert result == bg.build_mock_brief(PAYLOAD)


def test_missing_api_configuration_uses_mock(monkeypatch):
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("LLM_BASE_URL", raising=False)

    result = bg.generate_evidence_brief(PAYLOAD, use_mock=False)

    assert result == bg.build_mock_brief(PAYLOAD)


def test_invalid_llm_output_uses_mock(monkeypatch):
    configure_llm(monkeypatch)

    monkeypatch.setattr(
        bg,
        "_call_llm",
        lambda *args, **kwargs: None
    )

    result = bg.generate_evidence_brief(PAYLOAD, use_mock=False)

    assert result == bg.build_mock_brief(PAYLOAD)