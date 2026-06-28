"""Turn source code into PRODUCT-OWNER-readable business knowledge.

The product owner doesn't want "where is X defined" (that's the developer's
code-structure need — adopt codebase-memory-mcp for it, docs §7). They want the
*business rules and behaviors* the software encodes, in plain language, so they
can search them and write the ticket they need.

``summarize(code, path) -> str`` does that extraction. It is the connectors'
``factualize``-style hook (docs §6), pluggable three ways, best-effort first:

  1. ``TEAMBRAIN_CODE_SUMMARY=module:function`` — your own extractor,
  2. else Claude (if the SDK + ``ANTHROPIC_API_KEY`` are present) — extracts
     business rules as prose, no implementation detail,
  3. else a stdlib heuristic digest (comments, docstrings, declared names,
     routes, user-facing strings) — offline, no LLM. Worse than the model, but
     it still surfaces the domain vocabulary a PO would search for.
"""
from __future__ import annotations

import importlib
import os
import re

DEFAULT_MODEL = "claude-opus-4-8"
MAX_CODE = 12000  # cap what we feed the model / scan per file

_SYSTEM = (
    "You extract BUSINESS rules and product behavior from source code for a "
    "product owner who will write tickets from your output. State, in plain "
    "language, what the software does and the business rules it enforces — "
    "limits, validations, statuses, permissions, pricing, workflows, edge cases. "
    "Do NOT describe implementation detail, frameworks, or code structure. Write "
    "short declarative statements a non-engineer can act on. If the file has no "
    "business logic (pure plumbing/config/tests), reply exactly: NO_BUSINESS_LOGIC."
)


def summarize(code: str, path: str) -> str:
    """Business-rule summary of one file. Returns ``""`` when there is nothing a
    PO would care about, so the connector can skip storing noise."""
    hook = os.environ.get("TEAMBRAIN_CODE_SUMMARY")
    if hook:
        mod, _, fn = hook.partition(":")
        return getattr(importlib.import_module(mod), fn)(code, path) or ""

    code = (code or "")[:MAX_CODE]
    if not code.strip():
        return ""

    try:
        import anthropic
    except ImportError:
        return _heuristic(code, path)
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        return _heuristic(code, path)

    model = os.environ.get("TEAMBRAIN_SYNTH_MODEL") or DEFAULT_MODEL
    try:
        client = anthropic.Anthropic()
        resp = client.messages.create(
            model=model, max_tokens=1000, thinking={"type": "adaptive"},
            system=_SYSTEM,
            messages=[{"role": "user", "content": f"File: {path}\n\n{code}"}],
        )
        text = "".join(b.text for b in resp.content
                       if getattr(b, "type", None) == "text").strip()
    except Exception:
        return _heuristic(code, path)
    if not text or text.strip() == "NO_BUSINESS_LOGIC":
        return ""
    return text


# ── offline heuristic ─────────────────────────────────────────────────────────

_DOCSTRING = re.compile(r'(?s)("""|\'\'\')(.*?)\1')
_LINE_COMMENT = re.compile(r'(?m)(?://|#)\s?(.+)$')
_BLOCK_COMMENT = re.compile(r'(?s)/\*(.*?)\*/')
_NAMES = re.compile(
    r'\b(?:def|class|func|function|interface|enum|public|private|protected)\s+'
    r'([A-Za-z_]\w{2,})')
_ROUTE = re.compile(r'''(?i)(?:@\w+\.|\.)(get|post|put|patch|delete|route)\(\s*["']([^"']+)''')
_STRING = re.compile(r'''["']([^"'\n]{6,80})["']''')
_WORDY = re.compile(r'[A-Za-z].*[ A-Za-z]')  # strings that read like text, not tokens


def _dedup(items, limit):
    seen, out = set(), []
    for it in items:
        it = it.strip()
        k = it.lower()
        if it and k not in seen:
            seen.add(k)
            out.append(it)
        if len(out) >= limit:
            break
    return out


def _heuristic(code: str, path: str) -> str:
    """A best-effort, LLM-free digest: the human-readable signal already in the
    file (comments, docstrings, names, routes, message strings)."""
    docs = [m.group(2) for m in _DOCSTRING.finditer(code)]
    comments = [m.group(1) for m in _LINE_COMMENT.finditer(code)]
    comments += [m.group(1) for m in _BLOCK_COMMENT.finditer(code)]
    names = [m.group(1) for m in _NAMES.finditer(code)]
    routes = [f"{m.group(1).upper()} {m.group(2)}" for m in _ROUTE.finditer(code)]
    strings = [s for s in (m.group(1) for m in _STRING.finditer(code))
               if _WORDY.fullmatch(s) and " " in s]

    parts = [f"Source file {path}."]
    prose = _dedup(docs + comments, 12)
    if prose:
        parts.append("Documented behavior: " + " | ".join(prose))
    if names:
        parts.append("Defines: " + ", ".join(_dedup(names, 20)))
    if routes:
        parts.append("Endpoints: " + ", ".join(_dedup(routes, 15)))
    if strings:
        parts.append("User-facing strings: " + " | ".join(_dedup(strings, 12)))
    # If we found nothing but the filename, it's noise — let the connector skip it.
    return "\n".join(parts) if len(parts) > 1 else ""
