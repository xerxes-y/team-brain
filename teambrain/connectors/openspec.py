"""OpenSpec change archive -> team-brain ingestion (local).

`OpenSpec <https://github.com/Fission-AI/openspec>`_ is a spec-driven
development workflow: each change lives under ``openspec/changes/<change-id>/``
as markdown (``proposal.md`` — the *why*, ``specs/**/spec.md`` — requirements &
scenarios, ``design.md`` — the *how*), and the current source of truth lives
under ``openspec/specs/<capability>/spec.md``. Those artifacts record the
team's decisions — but as folders you browse, not memories you can ask.

This connector walks a repo's ``openspec/`` tree and stores:

  * ``proposal.md``  -> ``semantic``  ``openspec``/``proposal`` memories (the why
    — the PO role boosts these);
  * ``spec.md`` files -> ``semantic``  ``openspec``/``spec`` memories (scenarios
    — ready-made input for the tester's ``team_test_plan``);
  * ``design.md``    -> ``procedural`` ``openspec``/``design`` memories (the how
    — the developer role boosts these);
  * ``project.md``   -> ``semantic``  ``openspec``/``project`` (conventions).

``tasks.md`` is deliberately skipped — an ephemeral checklist is stale the day
the change merges.

The PO<->dev bridge: the change id and document text are scanned for Jira issue
keys (``ABC-123``) which become ``ticket:<KEY>`` tags — so the spec joins the
ticket's commits (IntelliJ connector) and captured chats instead of being
another silo. Archived changes (``changes/archive/…``) are ingested too, tagged
``archived``.

ACL: a local checkout is private by nature; pass ``acl_groups`` to scope the
memories fail-closed. Each chunk carries a ``src:`` back-link to the markdown
file (relative path, or a full URL when ``web_base`` is given).

Pure stdlib + filesystem — no credentials, offline-testable.
"""
from __future__ import annotations

import os

from .. import store as _store
from ._text import chunk_markdown, slug, ticket_keys

TIERS = {"proposal": "semantic", "spec": "semantic",
         "design": "procedural", "project": "semantic"}


def _acl_tags(acl_groups) -> list:
    return [f"{_store.ACL_PREFIX}{slug(g)}" for g in (acl_groups or []) if str(g).strip()]


def ingest_doc(rel_path: str, text: str, kind: str, namespace: str, repo: str = "",
               change: str = "", archived: bool = False, web_base=None,
               acl_groups=None) -> int:
    """Store one OpenSpec markdown document as memories. ``rel_path`` is the
    path inside the repo (used for the ``src:`` back-link); ``kind`` is one of
    ``proposal|spec|design|project``. Returns the chunk count."""
    text = (text or "").strip()
    if not text or kind not in TIERS:
        return 0

    tags = ["openspec", kind]
    if repo:
        tags.append(f"repo:{slug(repo)}")
    if change:
        tags.append(f"change:{slug(change)}")
    if archived:
        tags.append("archived")
    tags += [f"ticket:{k}" for k in ticket_keys(change, text)]
    tags += _acl_tags(acl_groups)
    src = f"{web_base.rstrip('/')}/{rel_path}" if web_base else rel_path
    tags.append(f"{_store.SRC_PREFIX}{src}")

    label = change or os.path.basename(os.path.dirname(rel_path)) or repo
    st = _store.store()
    n = 0
    for heading, chunk in chunk_markdown(text):
        head = f"openspec {kind}: {label}" + (f" — {heading}" if heading else "")
        st.save(head, chunk, tier=TIERS[kind], tags=tags,
                source="openspec", namespace=namespace)
        n += 1
    return n


def _classify(parts) -> tuple:
    """(kind, change, archived) for a path split into parts under ``openspec/``.
    kind=None means skip (tasks.md, README, non-markdown, …)."""
    name = parts[-1].lower()
    if not name.endswith(".md"):
        return None, "", False
    change, archived = "", False
    if parts[0] == "changes" and len(parts) >= 2:
        archived = parts[1] == "archive"
        idx = 2 if archived else 1
        if len(parts) <= idx + 1:          # a file directly under changes/ or archive/
            return None, "", False
        change = parts[idx]
    if name == "tasks.md":
        return None, "", False
    if name == "proposal.md":
        return "proposal", change, archived
    if name == "design.md":
        return "design", change, archived
    if name == "project.md" and len(parts) == 1:
        return "project", "", False
    if "specs" in parts[:-1]:
        return "spec", change, archived
    return None, "", False


def sync(path: str, namespace: str, repo=None, web_base=None, acl_groups=None,
         include_archive: bool = True, root: str = "openspec") -> dict:
    """Ingest a repo's OpenSpec tree into ``namespace``. ``path`` is the repo
    root (the directory containing ``openspec/``); ``repo`` is a label for tags
    (defaults to the folder name); ``web_base`` (e.g. the repo's web URL) turns
    ``src:`` back-links into full URLs. Returns a summary."""
    top = os.path.abspath(os.path.expanduser(path))
    base = os.path.join(top, root)
    if not os.path.isdir(base):
        raise FileNotFoundError(f"no {root}/ directory under {path}")
    repo = repo or os.path.basename(top)

    docs = chunks = 0
    changes = set()
    for dirpath, _, files in os.walk(base):
        for fname in sorted(files):
            fp = os.path.join(dirpath, fname)
            rel = os.path.relpath(fp, base)
            parts = rel.split(os.sep)
            kind, change, archived = _classify(parts)
            if kind is None or (archived and not include_archive):
                continue
            try:
                with open(fp, "r", encoding="utf-8", errors="replace") as fh:
                    text = fh.read()
            except OSError:
                continue
            n = ingest_doc(os.path.join(root, rel).replace(os.sep, "/"), text,
                           kind, namespace, repo=repo, change=change,
                           archived=archived, web_base=web_base,
                           acl_groups=acl_groups)
            if n:
                docs += 1
                chunks += n
                if change:
                    changes.add(change)

    return {"project": repo, "namespace": namespace,
            "changes": len(changes), "docs": docs, "chunks": chunks}
