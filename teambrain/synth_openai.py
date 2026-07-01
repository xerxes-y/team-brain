"""Provider-agnostic synthesis — for teams without Anthropic.

Talks to any **OpenAI-compatible** chat endpoint over stdlib ``urllib`` (no SDK,
no Anthropic). That covers:

  * a fully **local** model — Ollama / LM Studio / vLLM / llama.cpp — so you need
    **no API key and nothing leaves the machine**,
  * a company LLM gateway, Azure OpenAI, or OpenAI itself.

Wire it in::

    export TEAMBRAIN_SYNTH=teambrain.synth_openai:synth
    # local, no key (Ollama):
    export OPENAI_BASE_URL=http://localhost:11434/v1
    export TEAMBRAIN_SYNTH_MODEL=llama3.1
    # or cloud:
    export OPENAI_API_KEY=sk-...           # + TEAMBRAIN_SYNTH_MODEL=gpt-4o-mini

It also exposes ``summarize_code`` for the GitLab/code ingest, so the
code→business extraction can use the same local model::

    export TEAMBRAIN_CODE_SUMMARY=teambrain.synth_openai:summarize_code

Both degrade gracefully (extractive answer / heuristic summary) if the endpoint
is unreachable, so the path never hard-fails.
"""
from __future__ import annotations

import json
import os
import ssl
import urllib.request

from . import store as _store
from .synth_claude import _extractive, _sources_block
from . import code_summary

DEFAULT_MODEL = "gpt-4o-mini"


def _chat(system: str, user: str, max_tokens: int = 1500) -> str:
    """One OpenAI-compatible chat completion. Raises on transport/HTTP error."""
    base = (os.environ.get("OPENAI_BASE_URL") or "https://api.openai.com/v1").rstrip("/")
    model = os.environ.get("TEAMBRAIN_SYNTH_MODEL") or DEFAULT_MODEL
    body = {
        "model": model,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "temperature": 0,
        "max_tokens": max_tokens,
    }
    headers = {"Content-Type": "application/json"}
    key = os.environ.get("OPENAI_API_KEY")
    if key:
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(base + "/chat/completions",
                                 data=json.dumps(body).encode(), headers=headers)
    # TEAMBRAIN_TLS_INSECURE=1: accept self-signed certs (TEST gateways only).
    ctx = (ssl._create_unverified_context()
           if os.environ.get("TEAMBRAIN_TLS_INSECURE") == "1" else None)
    with urllib.request.urlopen(req, timeout=60, context=ctx) as r:
        payload = json.loads(r.read().decode())
    return (payload["choices"][0]["message"]["content"] or "").strip()


def synth(query, role, profile, rows) -> str:
    """``TEAMBRAIN_SYNTH`` hook — a cited answer from an OpenAI-compatible model."""
    if not rows:
        return ("No knowledge in the team brain covers this yet. Say so rather "
                "than guessing.")
    system = (
        (profile.get("task_prompt", "") + "\n\n").lstrip()
        + "Answer ONLY from the numbered SOURCES below. Cite every claim with its "
          "[n] marker. If the sources don't cover the question, say so plainly — "
          "do not invent facts. Be concise and lead with the answer."
    )
    user = f"Question (asked by a {role}): {query}\n\nSOURCES:\n{_sources_block(rows)}"
    try:
        text = _chat(system, user)
    except Exception as exc:
        return _extractive(query, role, rows) + f"\n\n(synthesis unavailable: {exc})"
    return text or _extractive(query, role, rows)


def summarize_code(code: str, path: str) -> str:
    """``TEAMBRAIN_CODE_SUMMARY`` hook — business rules from code via the same
    OpenAI-compatible model; falls back to the offline heuristic on failure."""
    code = (code or "")[:code_summary.MAX_CODE]
    if not code.strip():
        return ""
    try:
        text = _chat(code_summary._SYSTEM, f"File: {path}\n\n{code}", max_tokens=1000)
    except Exception:
        return code_summary._heuristic(code, path)
    if not text or text.strip() == "NO_BUSINESS_LOGIC":
        return ""
    return text
