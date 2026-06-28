"""The read path: ``assist(query, role)`` — role-routed, ACL-gated, cited.

Pipeline:
  1. over-fetch hybrid search from the shared store (memento's RRF),
  2. ACL-filter by the asker's groups (fail closed),
  3. soft-rerank: boost rows whose tags/tier match the role profile,
  4. synthesize an answer with citations — the only LLM call. Synthesis is
     PLUGGABLE: with no model wired up it falls back to an extractive answer
     (ranked snippets + citations) so the whole path runs offline/stdlib-only.

Roles are config (``roles.json``), not code — see §4 of docs/team-brain.md.
"""
from __future__ import annotations

import json
import os

from . import store as _store

_ROLES = None


def _roles() -> dict:
    global _ROLES
    if _ROLES is None:
        path = os.environ.get("TEAMBRAIN_ROLES") or os.path.join(
            os.path.dirname(__file__), "..", "roles.json"
        )
        with open(path, "r", encoding="utf-8") as fh:
            _ROLES = {k: v for k, v in json.load(fh).items() if not k.startswith("_")}
    return _ROLES


def _rerank(rows, profile):
    """Soft boost (never a hard filter) by role tag/tier affinity, preserving the
    store's hybrid order as the tiebreaker."""
    bias_tags = set(profile.get("tags", []))
    bias_tiers = set(profile.get("tiers", []))

    def score(idx_row):
        idx, m = idx_row
        s = 0.0
        s += len(set(_store._tags_of(m)) & bias_tags) * 1.0
        s += 0.5 if m.get("tier") in bias_tiers else 0.0
        s -= idx * 0.01  # keep original hybrid rank as a gentle tiebreaker
        return s

    return [m for _, m in sorted(enumerate(rows), key=score, reverse=True)]


def _synthesize(query, role, profile, rows):
    """Turn ranked memories into an answer. Override by setting
    ``TEAMBRAIN_SYNTH`` to ``module:function`` accepting (query, role, profile,
    rows) -> str. Default is extractive (no LLM)."""
    hook = os.environ.get("TEAMBRAIN_SYNTH")
    if hook:
        mod, _, fn = hook.partition(":")
        import importlib
        return getattr(importlib.import_module(mod), fn)(query, role, profile, rows)
    if not rows:
        return ("No knowledge found for this question. Nothing in the team brain "
                "covers it yet — say so rather than guessing.")
    lines = [f"(extractive answer — wire TEAMBRAIN_SYNTH for synthesis)",
             f"Top knowledge for a {role} on: {query}", ""]
    for i, m in enumerate(rows, 1):
        snippet = str(m.get("content", "")).strip().replace("\n", " ")[:240]
        lines.append(f"[{i}] {m.get('title')}: {snippet}")
    return "\n".join(lines)


def _cite(rows):
    return [{"n": i, "id": m.get("id"), "title": m.get("title"),
             "source": m.get("source"), "url": _store.source_url_of(m)}
            for i, m in enumerate(rows, 1)]


def assist(query, role, namespace, asker_groups=None, limit=8, overfetch=4):
    """Help a ``role`` solve ``query`` from team ``namespace``.

    asker_groups: groups the caller belongs to, for ACL. None => only public
    memories are returned (fail closed)."""
    profile = _roles().get(role)
    if profile is None:
        raise ValueError(f"unknown role '{role}'; known: {', '.join(_roles())}")

    rows = _store.store().search(query, limit=limit * overfetch,
                                 namespace=namespace, mode="hybrid")
    visible, hidden = _store.acl_filter(rows, asker_groups)
    ranked = _rerank(visible, profile)[:limit]
    return {
        "role": role,
        "query": query,
        "answer": _synthesize(query, role, profile, ranked),
        "citations": _cite(ranked),
        "hidden_by_acl": hidden,
    }


def draft_ticket(query, namespace, asker_groups=None, limit=8):
    """Help a product owner turn a question into the ticket they need.

    Retrieves the relevant team knowledge — including business rules mined from
    code (the GitLab connector) — through the PO profile (ACL-gated), and shapes
    it into a ticket draft (title, background, acceptance criteria) with
    citations back to the source. The PO reviews and files it; this does not
    write to Jira."""
    ask = (f"Draft a ticket for: {query}. Output a clear Title, a Background "
           f"section stating the business rules and current behavior involved, "
           f"and Acceptance Criteria as a checklist. Cite the source for each "
           f"rule. If the knowledge base doesn't cover part of it, flag that as "
           f"an open question rather than inventing requirements.")
    res = assist(ask, "po", namespace, asker_groups=asker_groups, limit=limit)
    return {"query": query, "ticket": res["answer"],
            "citations": res["citations"], "hidden_by_acl": res["hidden_by_acl"]}
