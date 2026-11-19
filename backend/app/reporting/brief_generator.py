"""
brief_generator.py - Maintainer Evidence Brief (open-weight LLM, summarizer only).

The LLM never decides anything. The verdict is already computed by graph.py and
campaign.py; the model only rewrites those verified JSON facts into 2-3 neutral
sentences. It is forbidden from classifying intent or judging whether text was
AI-written, and its output is validated against the payload before use.

Env vars (OpenAI-compatible endpoint, e.g. Groq or OpenRouter):
  LLM_API_KEY   - API key
  LLM_BASE_URL  - e.g. https://api.groq.com/openai/v1
  LLM_MODEL     - optional, default "llama-3.1-8b-instant" (or "openai/gpt-oss-20b")

Expected verdict_payload keys (all optional except pr_id):
  pr_id, author, graph_status, trust_weight, denounced_by (list of
  {"repo", "reason", "weight"?}), matching_prs (list[str]), cluster_size,
  distinct_repos, window_hours, final_verdict
"""

import json
import os
import re
import urllib.error
import urllib.request

SYSTEM_PROMPT = """You are an evidence summarizer for open-source maintainers.
You receive a JSON object of verified facts computed by deterministic code.

Write a neutral brief of 2 to 3 sentences, plain text, no markdown, no lists.

Rules:
1. Use ONLY facts present in the JSON: repository names, PR IDs, trust weights,
   denouncement reasons, counts, and the final verdict. Copy them exactly.
2. Never invent, estimate, or alter any number, name, repository, or PR ID.
3. Never classify intent, motive, or quality. Never say or imply whether any
   text was written by AI, a bot, or a human.
4. Never speculate, moralize, recommend punishment, or use emotive language.
5. If a field is missing or empty, do not mention it.
6. State the final verdict exactly as given."""

_PR_REF = re.compile(r"[\w.\-]+/?[\w.\-]*#\d+")


def _fmt_weight(value):
    return f"{value:.2f}" if isinstance(value, (int, float)) else str(value)


def build_mock_brief(payload):
    """Deterministic offline brief built only from payload fields."""
    pr_id = payload.get("pr_id", "unknown PR")
    verdict = payload.get("final_verdict", payload.get("graph_status", "UNKNOWN"))
    parts = [f"PR {pr_id} received the verdict {verdict}."]

    facts = []
    if "graph_status" in payload:
        facts.append(f"graph status {payload['graph_status']}")
    if "trust_weight" in payload:
        facts.append(f"trust weight {_fmt_weight(payload['trust_weight'])}")
    if facts:
        parts.append("Trust graph result: " + ", ".join(facts) + ".")

    denounced = payload.get("denounced_by") or []
    if denounced:
        items = []
        for d in denounced:
            if isinstance(d, dict):
                text = d.get("repo", "unknown repo")
                if d.get("reason"):
                    text += f' (reason: "{d["reason"]}")'
                if "weight" in d:
                    text += f" [weight {_fmt_weight(d['weight'])}]"
                items.append(text)
            else:
                items.append(str(d))
        parts.append("Denounced by: " + "; ".join(items) + ".")

    matches = payload.get("matching_prs") or []
    if matches:
        window = payload.get("window_hours")
        span = f" within {window} hours" if window else ""
        repos = payload.get("distinct_repos")
        repo_txt = f" across {repos} repositories" if repos else ""
        parts.append(
            f"Structurally near-identical PRs{repo_txt}{span}: {', '.join(matches)}."
        )
    elif "cluster_size" in payload and not denounced:
        parts.append("No matching cross-repository PR cluster was found.")

    return " ".join(parts)


def _allowed_tokens(payload):
    """Every PR reference and repo name the model is allowed to mention."""
    allowed = {str(payload.get("pr_id", ""))}
    allowed.update(str(m) for m in payload.get("matching_prs") or [])
    return allowed


def validate_brief(text, payload):
    """Reject unsupported PR references and unsafe claims."""

    if not isinstance(text, str) or not text.strip():
        return False

    if not isinstance(payload, dict):
        return False

    allowed = _allowed_tokens(payload)

    # Reject unknown PR references
    for ref in _PR_REF.findall(text):
        if ref not in allowed:
            return False

    # Reject unsupported or prohibited claims
    banned = (
        "written by ai",
        "ai-generated",
        "ai generated",
        "chatgpt",
        "malicious",
        "intent",
        "likely a bot",
        "probably",
        "seems to",
        "must be banned",
        "should be rejected",
        "definitely fraudulent",
    )

    lowered = text.lower()

    if any(phrase in lowered for phrase in banned):
        return False

    # Reject repository names not present in the payload
    allowed_repos = {
        str(item.get("repo"))
        for item in payload.get("denounced_by", [])
        if isinstance(item, dict) and item.get("repo")
    }

    for repo in re.findall(r'\b[\w.-]+/[\w.-]+\b', text):
        if repo not in allowed_repos:
            return False

    return True


def _call_llm(payload, api_key, base_url, model, timeout=20):
    body = {
        "model": model,
        "temperature": 0,
        "max_tokens": 220,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",
             "content": "Verified facts (JSON):\n" + json.dumps(payload, sort_keys=True)},
        ],
    }
    req = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {api_key}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data["choices"][0]["message"]["content"].strip()


def generate_evidence_brief(verdict_payload, use_mock=True):
    """
    Return a 2-3 sentence evidence brief for a maintainer.

    use_mock=True  -> fully offline, deterministic.
    use_mock=False -> open-weight LLM via OpenAI-compatible endpoint; falls back
                      to the mock brief if keys are missing, the call fails, or
                      the output cites anything not in the payload.
    """
    if use_mock:
        return build_mock_brief(verdict_payload)

    api_key = os.environ.get("LLM_API_KEY")
    base_url = os.environ.get("LLM_BASE_URL")
    model = os.environ.get("LLM_MODEL", "llama-3.1-8b-instant")
    if not api_key or not base_url:
        return build_mock_brief(verdict_payload)

    try:
        text = _call_llm(verdict_payload, api_key, base_url, model)
    except (urllib.error.URLError, TimeoutError, KeyError, ValueError, OSError):
        return build_mock_brief(verdict_payload)

    return text if validate_brief(text, verdict_payload) else build_mock_brief(verdict_payload)
