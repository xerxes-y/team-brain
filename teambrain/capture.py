"""Deliberate end-of-work capture: turn the important bits of a chat into memories.

The connectors (devin/intellij) capture activity in *bulk* and passively. This is
the *deliberate, curated* counterpart: when a PO finishes analysing a ticket, or a
developer finishes implementing one, they run one command to push the decisions /
business rules / gotchas from the conversation into the team brain — tagged to the
ticket, so the ``explain_ticket`` / ``test_plan`` bridges pick them up immediately.

``capture(text, namespace, ticket=…)``:
  1. extract Jira keys (from the explicit ``ticket`` arg + the text) → ``ticket:<KEY>``
     tags — the link from this knowledge to the ticket it belongs to,
  2. optionally **distill** the raw chat into discrete facts (the "factualise before
     store" idea, docs §6) via the pluggable ``TEAMBRAIN_DISTILL`` hook — with no
     hook set it falls back to chunking the text, so the path runs offline,
  3. store each as a memory, ACL-scoped by ``groups`` (fail closed, like everything).

This is the write seam for the IDE chat: Devin/IntelliJ's agent calls the
``team_capture`` MCP tool; nothing here is IDE-specific.
"""
from __future__ import annotations

import importlib
import os

from . import store as _store
from .connectors._text import chunk_markdown, slug, ticket_keys

DEFAULT_TIER = "semantic"


def _distill(text, context):
    """Split raw chat into discrete ``{title, content, tier?, tags?}`` facts via the
    ``TEAMBRAIN_DISTILL=module:function`` hook. Returns ``None`` if no hook is set
    (caller then falls back to chunking). The hook gets ``(text, context)`` where
    context carries role/ticket/namespace so it can label facts."""
    hook = os.environ.get("TEAMBRAIN_DISTILL")
    if not hook:
        return None
    mod, _, fn = hook.partition(":")
    items = getattr(importlib.import_module(mod), fn)(text, context)
    return list(items or [])


def _fallback_items(text, title, ticket_key):
    """No distiller → keep the text verbatim, chunked so one long paste never
    becomes one giant memory. Heading-bounded, like the connectors."""
    base = title or (f"Note on {ticket_key}" if ticket_key else "Captured note")
    items = []
    for i, (heading, body) in enumerate(chunk_markdown(text), 1):
        name = f"{base} — {heading}" if heading else base
        items.append({"title": name, "content": body})
    return items or [{"title": base, "content": text}]


def capture(text, namespace, ticket=None, role=None, groups=None, title=None,
            source="chat", tier=DEFAULT_TIER):
    """Store the important parts of a chat into the team brain.

    ticket: a Jira key and/or text — keys found here AND in ``text`` become
    ``ticket:<KEY>`` tags. role: who captured it (tester|developer|po) → a
    ``role:`` tag for retrieval bias. groups: ACL scope (omit ⇒ public in the
    namespace). Returns a summary: how many memories were stored and their ids."""
    text = (text or "").strip()
    keys = ticket_keys(ticket or "", text)
    if not text:
        return {"stored": 0, "tickets": keys, "ids": []}

    base_tags = ["capture"]
    if role:
        base_tags.append(f"role:{slug(role)}")
    base_tags += [f"ticket:{k}" for k in keys]
    base_tags += [f"{_store.ACL_PREFIX}{slug(g)}" for g in (groups or []) if str(g).strip()]

    context = {"namespace": namespace, "role": role, "ticket": ticket, "tickets": keys}
    items = _distill(text, context)
    if items is None:
        items = _fallback_items(text, title, keys[0] if keys else None)

    st = _store.store()
    ids = []
    for it in items:
        body = str(it.get("content") or "").strip()
        if not body:
            continue
        tags = list(base_tags) + [str(t) for t in (it.get("tags") or [])]
        ids.append(st.save(it.get("title") or title or "Captured note", body,
                           tier=it.get("tier") or tier, tags=tags,
                           source=source, namespace=namespace))
    return {"stored": len(ids), "tickets": keys, "ids": ids}
