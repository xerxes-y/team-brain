"""The read path: ``assist(query, role)`` — role-routed, ACL-gated, cited.

Pipeline:
  1. over-fetch hybrid search from the shared store (memento's RRF),
  2. ACL-filter by the asker's groups (fail closed),
  3. soft-rerank: boost rows whose tags/tier match the role profile,
  4. synthesize an answer with citations — the only LLM call. Synthesis is
     PLUGGABLE: with no model wired up it falls back to an extractive answer
     (ranked snippets + citations) so the whole path runs offline/stdlib-only.

Roles are config (``roles.json``), not code — see §4 of docs/team-brain.md.

On top of generic ``assist`` sit three role-specific bridges that close the
PO↔dev↔tester gap, each reusing the same core (``_retrieve`` + synthesis):

  * ``draft_ticket`` (PO)      — need → business rules from code → ticket draft.
  * ``explain_ticket`` (dev)   — Jira ticket → business logic in plain terms +
                                 the code/PRs/commits that implement it.
  * ``test_plan`` (tester)     — retrieves BOTH sides (PO business rules + dev
                                 code) and reconciles expected vs actual into
                                 test cases, since a tester's questions span both.

The dev/tester bridges boost memories tagged ``ticket:<KEY>`` — Jira issues and
the IntelliJ commits/TODOs that referenced the key — so a ticket pulls together
its spec and its implementation.
"""
from __future__ import annotations

import json
import os

from . import store as _store
from .connectors._text import ticket_keys

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


def _rerank(rows, profile, boost_tags=None):
    """Soft boost (never a hard filter) by role tag/tier affinity, preserving the
    store's hybrid order as the tiebreaker. ``boost_tags`` (e.g. ``ticket:ABC-1``)
    get a strong boost — they link an answer to a specific ticket/entity."""
    bias_tags = set(profile.get("tags", []))
    bias_tiers = set(profile.get("tiers", []))
    boost = set(boost_tags or ())

    def score(idx_row):
        idx, m = idx_row
        tags = set(_store._tags_of(m))
        s = 0.0
        s += len(tags & bias_tags) * 1.0
        s += len(tags & boost) * 3.0  # ticket/entity match is a strong signal
        s += 0.5 if m.get("tier") in bias_tiers else 0.0
        s -= idx * 0.01  # keep original hybrid rank as a gentle tiebreaker
        return s

    return [m for _, m in sorted(enumerate(rows), key=score, reverse=True)]


def _merge_profiles(*profiles):
    """Union the retrieval bias of several role profiles (tags + tiers) so one
    query can see *both sides* of the gap — e.g. the PO's business rules and the
    developer's code/PRs at once. ``task_prompt`` is supplied by the caller."""
    tags, tiers = [], []
    for p in profiles:
        for t in p.get("tags", []):
            if t not in tags:
                tags.append(t)
        for t in p.get("tiers", []):
            if t not in tiers:
                tiers.append(t)
    return {"tags": tags, "tiers": tiers}


def _retrieve(query, profile, namespace, asker_groups, limit, overfetch=4,
              boost_tags=None):
    """Shared read core: hybrid search → ACL filter (fail closed) → role rerank.
    Returns ``(ranked_visible_rows, n_hidden)``."""
    rows = _store.store().search(query, limit=limit * overfetch,
                                 namespace=namespace, mode="hybrid")
    visible, hidden = _store.acl_filter(rows, asker_groups)
    return _rerank(visible, profile, boost_tags)[:limit], hidden


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

    ranked, hidden = _retrieve(query, profile, namespace, asker_groups, limit,
                               overfetch=overfetch)
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


_EXPLAIN_PROMPT = (
    "You help a developer who was assigned a ticket but does NOT speak the "
    "business language. From the sources, (1) explain in plain terms what the "
    "product is supposed to do here and WHY, then (2) point to the specific code, "
    "PR, or commit that implements or enforces each rule, citing it. Translate "
    "business → code. If part of the ticket isn't covered by the knowledge base, "
    "flag it as an open question rather than inventing behavior.")


def explain_ticket(ticket, namespace, asker_groups=None, limit=8):
    """Developer-facing reverse bridge: given a Jira ticket (key and/or text),
    explain the business logic in plain terms and point to the code/PRs/commits
    that implement it — closing the half of the PO↔dev gap the developer feels.

    Memories tagged ``ticket:<KEY>`` (Jira issues, and the IntelliJ commits/TODOs
    that referenced the key) are boosted, so the assigned work surfaces first."""
    keys = ticket_keys(ticket)
    boost = [f"ticket:{k}" for k in keys]
    profile = dict(_roles()["developer"])
    profile["task_prompt"] = _EXPLAIN_PROMPT
    query = (f"Explain the business logic and implementing code for "
             f"{' '.join(keys)}: {ticket}".strip())
    ranked, hidden = _retrieve(query, profile, namespace, asker_groups, limit,
                               boost_tags=boost)
    return {"ticket": ticket, "tickets": keys,
            "answer": _synthesize(query, "developer", profile, ranked),
            "citations": _cite(ranked), "hidden_by_acl": hidden}


_TESTPLAN_PROMPT = (
    "You help a QA tester who sits between the product owner and the developer. "
    "From the sources, reconcile the EXPECTED behavior (business rules, "
    "acceptance criteria, decisions — the PO side) with the ACTUAL implementation "
    "(code, PRs, commits — the dev side). Lead with: expected behavior, how it is "
    "implemented, and where the two might diverge. Every divergence or unstated "
    "rule is a test case — output concrete test cases including edge cases. Cite "
    "every claim. Where behavior is undefined in the knowledge base, say so "
    "(that is itself a question for the PO or developer) rather than guessing.")


def test_plan(query, namespace, asker_groups=None, limit=10):
    """Tester-facing both-sides bridge: a tester's questions span the developer
    AND the product owner. This retrieves across BOTH (merged PO + developer +
    tester bias), then reconciles expected behavior vs actual implementation into
    test cases — so the tester gets one answer instead of chasing two people."""
    keys = ticket_keys(query)
    boost = [f"ticket:{k}" for k in keys]
    profile = _merge_profiles(_roles()["po"], _roles()["developer"],
                              _roles()["tester"])
    profile["task_prompt"] = _TESTPLAN_PROMPT
    ranked, hidden = _retrieve(query, profile, namespace, asker_groups, limit,
                               boost_tags=boost)
    return {"query": query, "tickets": keys,
            "answer": _synthesize(query, "tester", profile, ranked),
            "citations": _cite(ranked), "hidden_by_acl": hidden}
