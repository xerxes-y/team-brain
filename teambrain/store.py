"""Storage access for team-brain.

team-brain does **not** ship its own storage — it reuses memento's
``MemoryStorePG`` (Postgres: tsvector BM25 + pgvector, RRF fusion, namespaces,
entity graph, audit). This module:

  1. bootstraps memento onto ``sys.path`` (sibling repo or installed package),
  2. opens the shared store via ``memento_memory.open_store`` (driven by
     ``MEMENTO_DB_URL`` exactly like memento's team mode),
  3. provides the **ACL** helpers that are team-brain's own addition.

ACL model (default, documented in docs/team-brain.md §9)
--------------------------------------------------------
Access control is encoded as **tags** so no change to memento's schema is
needed:

  * a memory tagged ``acl:<group>`` is visible only to askers in ``<group>``;
  * a memory with **no** ``acl:*`` tag is **public** within its namespace;
  * a restricted memory is shown only if the asker's groups intersect its
    ``acl:*`` tags. When the asker's identity/groups are unknown we **deny**
    restricted memories (fail closed) — a leaked restricted Confluence page is
    hard to reverse, so the default is conservative.

Over-fetch then filter: we ask the store for more rows than ``limit`` and drop
the ones the asker may not see, because the ACL gate lives here, not in SQL.
"""
from __future__ import annotations

import os
import sys

_STORE = None


def _bootstrap_memento() -> None:
    """Make ``memento_memory`` importable: prefer an installed package, else the
    sibling ``../memento`` checkout (override with ``MEMENTO_ENGINE_REPO``)."""
    try:
        import memento_memory  # noqa: F401  (already importable)
        return
    except ImportError:
        pass
    repo = os.environ.get("MEMENTO_ENGINE_REPO") or os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "..", "memento")
    )
    if os.path.isdir(repo) and repo not in sys.path:
        sys.path.insert(0, repo)


def _is_pg(dsn: str) -> bool:
    return dsn.startswith(("postgres://", "postgresql://"))


def store():
    """Lazily open (and cache) the shared memory store.

    When ``MEMENTO_DB_URL`` is a Postgres DSN **and** a dense embedder is
    configured (``TEAMBRAIN_EMBED``), we construct ``MemoryStorePG`` directly so
    we can hand it the embedder — this is what flips memento's **pgvector** ANN
    path on (real semantic ``<=>`` search instead of lexical cosine). In every
    other case we defer to memento's own ``open_store`` (SQLite locally / in
    tests, plain-lexical Postgres when no embedder is set)."""
    global _STORE
    if _STORE is None:
        _bootstrap_memento()
        import memento_memory
        from .embed import make_dense_embedder

        dsn = os.environ.get("MEMENTO_DB_URL", "")
        dense = make_dense_embedder() if _is_pg(dsn) else None
        if dense is not None:
            import memento_memory_pg
            _STORE = memento_memory_pg.MemoryStorePG(dsn, dense_embedder=dense)
        else:
            _STORE = memento_memory.open_store()
    return _STORE


# ── ACL helpers ───────────────────────────────────────────────────────────────

ACL_PREFIX = "acl:"
SRC_PREFIX = "src:"  # back-link to the origin page, carried as a tag until the
                     # store schema gains a real source_url column (docs §6).


def _tags_of(mem: dict) -> list:
    """memento stores ``tags`` as a comma/space-delimited string; normalise to a
    list so we can scan for ``acl:*`` markers."""
    raw = mem.get("tags") or ""
    if isinstance(raw, (list, tuple)):
        return [str(t).strip() for t in raw if str(t).strip()]
    return [t.strip() for t in str(raw).replace(",", " ").split() if t.strip()]


def required_groups(mem: dict) -> set:
    """The set of groups allowed to see this memory; empty set == public."""
    return {t[len(ACL_PREFIX):] for t in _tags_of(mem) if t.startswith(ACL_PREFIX)}


def source_url_of(mem: dict):
    """The origin back-link for a memory, recovered from its ``src:`` tag (the
    connectors store one per chunk). ``None`` if the memory has no back-link."""
    for t in _tags_of(mem):
        if t.startswith(SRC_PREFIX):
            return t[len(SRC_PREFIX):]
    return mem.get("source_url")


def visible_to(mem: dict, asker_groups) -> bool:
    """True if an asker in ``asker_groups`` may see ``mem``.

    Public memories (no ``acl:*`` tag) are always visible. Restricted memories
    require a non-empty intersection; unknown askers (``None``/empty) are denied
    any restricted memory — fail closed."""
    needed = required_groups(mem)
    if not needed:
        return True
    return bool(needed & set(asker_groups or ()))


def acl_filter(rows, asker_groups):
    """Drop memories the asker may not see. Returns (visible_rows, n_hidden)."""
    visible = [m for m in rows if visible_to(m, asker_groups)]
    return visible, len(rows) - len(visible)
