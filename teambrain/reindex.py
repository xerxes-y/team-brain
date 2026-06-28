"""Re-embed every memory with the **currently configured** embedder.

Embeddings from different models live in different vector spaces *and*
dimensions, so after you change ``TEAMBRAIN_EMBED_PROFILE`` / model / dim you
must rebuild the pgvector column and re-encode existing rows — otherwise old
vectors are meaningless (or the dimension no longer matches).

    # e.g. demo (384-d bge-small) -> gpu-small (1024-d Qwen3)
    export TEAMBRAIN_EMBED_PROFILE=gpu-small
    python3 -m teambrain.reindex

This is a no-op (with a clear message) unless you are on Postgres with a dense
embedder configured. It rewrites the ``embedding`` column to the new dimension
and re-embeds in batches, reporting progress.
"""
from __future__ import annotations

import sys

from . import store as _store


def reembed_all(batch: int = 256) -> int:
    """Rebuild the pgvector column at the embedder's dim and re-encode all rows.
    Returns the number of memories re-embedded."""
    st = _store.store()
    dense = getattr(st, "dense", None)
    if dense is None or not getattr(st, "pgvector", False):
        print("Nothing to do: no dense embedder / not on pgvector. "
              "Set MEMENTO_DB_URL (postgres) + TEAMBRAIN_EMBED(_PROFILE).")
        return 0

    dim = dense.dim
    print(f"Re-embedding with {type(dense).__name__} "
          f"(model={getattr(dense, 'model', '?')}, dim={dim})")

    with st._conn() as c:
        # rebuild the column at the new dimension (drops stale vectors)
        c.execute("ALTER TABLE memories DROP COLUMN IF EXISTS embedding")
        c.execute(f"ALTER TABLE memories ADD COLUMN embedding vector({dim})")
        rows = c.execute("SELECT id, title, content FROM memories").fetchall()

    total = len(rows)
    print(f"{total} memories to encode…")
    done = 0
    with st._conn() as c:
        for r in rows:
            lit = st._dense_literal((r["title"] or "") + " " + (r["content"] or ""))
            c.execute("UPDATE memories SET embedding=%s::vector WHERE id=%s",
                      (lit, r["id"]))
            done += 1
            if done % batch == 0:
                print(f"  {done}/{total}")
    print(f"Done: re-embedded {done} memories at dim {dim}.")
    return done


def main() -> int:
    return 0 if reembed_all() >= 0 else 1


if __name__ == "__main__":
    sys.exit(main())
