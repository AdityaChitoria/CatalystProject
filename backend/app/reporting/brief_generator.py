"""
brief_generator.py - Maintainer Evidence Brief (open-weight LLM, summarizer only).

The LLM never decides anything. The verdict is already computed by graph.py and
campaign.py; the model only rewrites those verified JSON facts into 2-3 neutral
sentences. It is forbidden from classifying intent or judging whether text was
AI-written, and its output is validated against the payload before use.

Env vars (OpenAI-compatible endpoint, e.g. Groq or OpenRouter). Never hardcode
these; put them in a git-ignored `.env` (see `.env.example`) or your shell:
  LLM_API_KEY   - API key
  LLM_BASE_URL  - e.g. https://api.groq.com/openai/v1
  LLM_MODEL     - optional, default "llama-3.1-8b-instant"

Every brief carries a provenance tag so you always know what produced it:
  [LIVE LLM: <model>]   the model answered and the output passed validation
  [FALLBACK MOCK]       live mode was requested but something failed; the exact
                        reason is attached (missing env, HTTP error, validation...)
  [OFFLINE MOCK]        mock mode was requested explicitly (use_mock=True)

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
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

DEFAULT_MODEL = "openai/gpt-oss-20b"

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

_BANNED_PHRASES = ("written by ai", "ai-generated", "ai generated", "chatgpt",
                   "malicious", "intent", "likely a bot", "probably", "seems to")


# --------------------------------------------------------------------------- #
# Result type with provenance
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class BriefResult:
    text: str
    source: str            # "live" | "fallback" | "mock"
    model: str = ""        # model name used / attempted (live + fallback)
    reason: str = ""       # why we fell back (fallback) or a short note (mock)
    raw_output: str = ""   # rejected model output, kept for debugging (fallback only)

    @property
    def tag(self) -> str:
        if self.source == "live":
            return f"[LIVE LLM: {self.model}]"
        if self.source == "fallback":
            return "[FALLBACK MOCK]"
        return "[OFFLINE MOCK]"

    @property
    def status_line(self) -> str:
        """Tag plus, when relevant, the exact reason it fell back."""
        if self.source == "fallback" and self.reason:
            return f"{self.tag} reason: {self.reason}"
        return self.tag

    def __str__(self) -> str:
        return f"{self.tag} {self.text}"


# --------------------------------------------------------------------------- #
# Deterministic offline brief
# --------------------------------------------------------------------------- #
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


# --------------------------------------------------------------------------- #
# Validation of model output
# --------------------------------------------------------------------------- #
def _allowed_tokens(payload):
    """Every PR reference and repo name the model is allowed to mention."""
    allowed = {str(payload.get("pr_id", ""))}
    allowed.update(str(m) for m in payload.get("matching_prs") or [])
    return allowed


def validation_failure_reason(text, payload) -> Optional[str]:
    """
    Return None if the brief is acceptable, otherwise a precise reason string:
    empty output, a PR reference absent from the payload, or banned
    intent/AI-detection language.
    """
    if not text or not text.strip():
        return "validation failed: model returned empty output"
    allowed = _allowed_tokens(payload)
    for ref in _PR_REF.findall(text):
        if ref not in allowed:
            return f"validation failed: cited PR reference {ref!r} not present in payload"
    lowered = text.lower()
    for phrase in _BANNED_PHRASES:
        if phrase in lowered:
            return f"validation failed: contains banned phrase {phrase!r}"
    return None


def validate_brief(text, payload):
    """True if the brief is acceptable (kept for backward compatibility)."""
    return validation_failure_reason(text, payload) is None


# --------------------------------------------------------------------------- #
# Config + .env loading (stdlib only, so no python-dotenv dependency)
# --------------------------------------------------------------------------- #
def load_dotenv(path=".env", override=False) -> bool:
    """
    Minimal .env loader: KEY=VALUE lines, '#' comments, optional 'export ',
    optional surrounding quotes. Existing environment variables win unless
    override=True. Returns True if a file was read. Called explicitly by scripts,
    never on import, so importing this module has no side effects.
    """
    p = Path(path)
    if not p.is_file():
        return False
    for line in p.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key and (override or key not in os.environ):
            os.environ[key] = value
    return True


def live_configured() -> bool:
    """True when both LLM_API_KEY and LLM_BASE_URL are set (non-empty)."""
    return bool(os.environ.get("LLM_API_KEY") and os.environ.get("LLM_BASE_URL"))


def current_model() -> str:
    return os.environ.get("LLM_MODEL") or DEFAULT_MODEL


def _redact(text, secret):
    """Make sure an API key can never leak into logs or error messages."""
    return text.replace(secret, "***") if secret else text


# --------------------------------------------------------------------------- #
# Live LLM call
# --------------------------------------------------------------------------- #
def _call_llm(payload, api_key, base_url, model, timeout=20):
    body = {
        "model": model,
        "temperature": 0,
        "max_completion_tokens": 1024,
        "reasoning_effort": "low",
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",
             "content": "Verified facts (JSON):\n" + json.dumps(payload, sort_keys=True)},
        ],
    }
    req = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Bearer {api_key}",
            # Groq sits behind Cloudflare, which rejects urllib's default
            # "Python-urllib/x.y" User-Agent with a 403 (error code 1010).
            "User-Agent": "catalyst-trust-network/1.0",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data["choices"][0]["message"]["content"].strip()


def generate_brief(verdict_payload, use_mock=True) -> BriefResult:
    """
    Produce a 2-3 sentence evidence brief with full provenance.

    use_mock=True  -> fully offline, deterministic  ([OFFLINE MOCK]).
    use_mock=False -> open-weight LLM via an OpenAI-compatible endpoint
                      ([LIVE LLM: model]); on ANY problem (missing env, network,
                      HTTP error, bad response, failed validation) it falls back
                      to the deterministic brief ([FALLBACK MOCK]) and records
                      exactly why.
    """
    mock_text = build_mock_brief(verdict_payload)
    if use_mock:
        return BriefResult(mock_text, "mock", reason="use_mock=True")

    api_key = os.environ.get("LLM_API_KEY", "")
    base_url = os.environ.get("LLM_BASE_URL", "")
    model = current_model()

    missing = [n for n, v in (("LLM_API_KEY", api_key), ("LLM_BASE_URL", base_url)) if not v]
    if missing:
        return BriefResult(mock_text, "fallback", model,
                           f"missing environment variable(s): {', '.join(missing)}")

    raw = ""
    try:
        raw = _call_llm(verdict_payload, api_key, base_url, model)
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8", errors="replace").strip()[:200]
        except Exception:
            detail = ""
        reason = f"HTTP {exc.code} {exc.reason}" + (f": {detail}" if detail else "")
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, TimeoutError):
            reason = "request timed out"
        else:
            reason = f"network error: {exc.reason}"
    except TimeoutError:
        reason = "request timed out"
    except json.JSONDecodeError as exc:
        reason = f"response was not valid JSON ({exc})"
    except (KeyError, IndexError, TypeError) as exc:
        reason = f"unexpected response shape ({type(exc).__name__}: {exc})"
    except OSError as exc:
        reason = f"connection error: {exc}"
    else:
        problem = validation_failure_reason(raw, verdict_payload)
        if problem is None:
            return BriefResult(raw, "live", model)
        return BriefResult(mock_text, "fallback", model, problem, raw_output=raw)

    return BriefResult(mock_text, "fallback", model, _redact(reason, api_key))


def generate_evidence_brief(verdict_payload, use_mock=True, return_status=False):
    """
    Backward-compatible wrapper around generate_brief().

    Returns the brief text (str) by default, or the full BriefResult (text +
    provenance tag + fallback reason) when return_status=True.
    """
    result = generate_brief(verdict_payload, use_mock=use_mock)
    return result if return_status else result.text
