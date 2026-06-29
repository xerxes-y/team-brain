#!/usr/bin/env python3
"""First-local-test smoke check for team-brain.

Backend-agnostic: it exercises whatever the environment selects (local SQLite, or
Postgres ± pgvector embeddings) and reports what it used.

  python3 scripts/smoke_test.py

What it checks (always):
  * the store opens and reports its backend + embedder
  * save -> recall round-trips
  * ACL is fail-closed (a restricted memory is hidden from an unknown asker)

And, only when a dense embedder is configured (Postgres + TEAMBRAIN_EMBED*):
  * a zero-lexical-overlap query resolves by *meaning* (real pgvector ANN)

It writes to a unique throwaway namespace and deletes every row it created, so it
is safe to run against a shared store. Exit code 0 on success, 1 on failure.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from teambrain import store as _store          # noqa: E402
from teambrain.assist import assist            # noqa: E402


def main() -> int:
    st = _store.store()
    backend = type(st).__name__
    dense = getattr(st, "dense", None)
    embedder = type(dense).__name__ if dense else None
    dim = getattr(dense, "dim", None)
    print(f"backend: {backend} | embedder: {embedder}"
          + (f" (dim={dim})" if dim else "")
          + f" | MEMENTO_DB_URL={'set' if os.environ.get('MEMENTO_DB_URL') else 'unset (SQLite)'}")

    ns = "smoke-" + str(abs(hash((os.getpid(), backend))) % 100000)
    ids, ok = [], True

    def check(label, cond):
        nonlocal ok
        print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
        ok = ok and cond

    try:
        ids.append(st.save("Upload size cap",
                           "The platform rejects any file larger than 25 megabytes.",
                           tier="semantic", tags=["business"], source="manual", namespace=ns))
        ids.append(st.save("Refund window",
                           "Customers may return an item within 30 days for a full refund.",
                           tier="semantic", tags=["business"], source="manual", namespace=ns))
        ids.append(st.save("Comp bands (HR only)",
                           "Confidential salary ranges per level.",
                           tier="semantic", tags=["business", "acl:hr"],
                           source="manual", namespace=ns))

        # 1) basic recall
        rows = st.search("refund policy for returns", limit=3, namespace=ns, mode="hybrid")
        check("save -> recall round-trips", any(r["title"] == "Refund window" for r in rows))

        # 2) ACL fail-closed via the read path
        anon = assist("salary ranges", "po", ns)                       # no groups
        hr = assist("salary ranges", "po", ns, asker_groups=["hr"])    # hr group
        check("ACL hides restricted memory from unknown asker", anon["hidden_by_acl"] >= 1)
        check("ACL reveals restricted memory to its group", hr["hidden_by_acl"] == 0)

        # 3) semantic (only meaningful with an embedder)
        if dense:
            sem = st.search("how big an attachment can I send", limit=3,
                            namespace=ns, mode="vector")
            check("semantic match with zero lexical overlap",
                  bool(sem) and sem[0]["title"] == "Upload size cap")
        else:
            print("  [skip] semantic test (no dense embedder; set Postgres + TEAMBRAIN_EMBED*)")
    finally:
        for mid in ids:
            try:
                st.forget(mem_id=mid)
            except Exception:
                pass
        print(f"  cleaned up namespace {ns!r}")

    print("RESULT:", "OK" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
