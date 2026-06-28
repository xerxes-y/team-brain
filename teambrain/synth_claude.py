"""Claude-backed synthesis for team-brain's read path.

Wire it in with::

    export TEAMBRAIN_SYNTH=teambrain.synth_claude:synth
    export ANTHROPIC_API_KEY=...                 # or an `ant auth login` profile
    # optional: export TEAMBRAIN_SYNTH_MODEL=claude-sonnet-4-6   # cheaper

``synth(query, role, profile, rows) -> str`` matches the hook signature
``teambrain.assist`` expects. It turns the ACL-filtered, role-ranked memories
into a cited answer that helps the role *solve* the problem (the role's
``task_prompt`` steers the synthesis). The model only ever sees memories that
already passed the ACL gate — synthesis is the last step, after retrieval and
filtering, so it cannot leak a restricted page.

Citations: the answer cites ``[n]`` markers that map to the numbered sources
below; ``assist`` already appends the source list, so the model is told to use
those exact numbers and never invent facts not in the sources.
"""
from __future__ import annotations

import os

from . import store as _store

DEFAULT_MODEL = "claude-opus-4-8"
MAX_SNIPPET = 1200


def _sources_block(rows) -> str:
    lines = []
    for i, m in enumerate(rows, 1):
        body = str(m.get("content", "")).strip().replace("\n", " ")[:MAX_SNIPPET]
        url = _store.source_url_of(m)
        head = f"[{i}] {m.get('title')}" + (f" <{url}>" if url else "")
        lines.append(f"{head}\n{body}")
    return "\n\n".join(lines)


def synth(query, role, profile, rows) -> str:
    """Synthesize a cited answer with Claude. Falls back to an extractive answer
    if the SDK/key is unavailable so the read path never hard-fails."""
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
        import anthropic
    except ImportError:
        return _extractive(query, role, rows)

    model = os.environ.get("TEAMBRAIN_SYNTH_MODEL") or DEFAULT_MODEL
    try:
        client = anthropic.Anthropic()
        resp = client.messages.create(
            model=model,
            max_tokens=1500,
            thinking={"type": "adaptive"},
            system=system,
            messages=[{"role": "user", "content": user}],
        )
    except Exception as exc:  # network/auth/quota — degrade, don't crash
        return _extractive(query, role, rows) + f"\n\n(synthesis unavailable: {exc})"

    text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
    return text.strip() or _extractive(query, role, rows)


def _extractive(query, role, rows) -> str:
    """The offline fallback — ranked snippets with citation markers."""
    out = [f"Top knowledge for a {role} on: {query}", ""]
    for i, m in enumerate(rows, 1):
        snippet = str(m.get("content", "")).strip().replace("\n", " ")[:240]
        out.append(f"[{i}] {m.get('title')}: {snippet}")
    return "\n".join(out)
