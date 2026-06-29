#!/usr/bin/env python3
"""Ingest company GitLab project(s) into team-brain — business rules from code.

Designed to run on a machine that can reach your (self-hosted) company GitLab.

    export GITLAB_BASE_URL=https://gitlab.mycompany.com/api/v4   # note: .../api/v4
    export GITLAB_TOKEN=glpat-xxxxxxxx                           # read_api scope
    # storage (recommended: shared Postgres + semantic embeddings)
    export MEMENTO_DB_URL=postgresql://memento:memento@db:5432/memento
    export TEAMBRAIN_EMBED_PROFILE=demo

    team-brain-gitlab group/subgroup/project --namespace team-eng
    # or:  python3 -m teambrain.ingest_gitlab group/project --namespace team-eng

Multiple projects and a whole prefix are supported by listing them. Private /
internal projects are ACL-gated by default (query with the printed group); pass
--public to make a project's knowledge visible to everyone in the namespace
(only when the whole audience is authorized).
"""
from __future__ import annotations

import argparse
import os
import sys

from .connectors import gitlab


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="team-brain-gitlab",
        description="Mine GitLab project code into team-brain (business rules for the PO).")
    p.add_argument("projects", nargs="+", metavar="group/project",
                   help="one or more GitLab project paths (or numeric IDs)")
    p.add_argument("--namespace", required=True, help="team-brain namespace to store into")
    p.add_argument("--ref", default="HEAD", help="branch/tag/commit (default: HEAD)")
    p.add_argument("--max-files", type=int, default=None,
                   help="cap files mined per project (staleness guard)")
    p.add_argument("--exts", default=None,
                   help="comma-separated extensions to include (default: the code-ext allowlist)")
    p.add_argument("--public", action="store_true",
                   help="make mined knowledge visible to everyone in the namespace "
                        "(skip the acl:repo gate — only if the audience is authorized)")
    args = p.parse_args(argv)

    if not os.environ.get("GITLAB_BASE_URL"):
        print("note: GITLAB_BASE_URL unset -> defaulting to https://gitlab.com/api/v4 "
              "(set it to your company GitLab's .../api/v4)", file=sys.stderr)
    if not os.environ.get("GITLAB_TOKEN"):
        print("warning: GITLAB_TOKEN unset -> only public projects are readable",
              file=sys.stderr)

    exts = {e.strip().lstrip(".") for e in args.exts.split(",")} if args.exts else None
    try:
        client = gitlab.GitLabClient()   # reads GITLAB_BASE_URL / GITLAB_TOKEN
    except RuntimeError as exc:
        p.error(str(exc))

    total = 0
    for proj in args.projects:
        try:
            res = gitlab.sync_project(proj, args.namespace, ref=args.ref, exts=exts,
                                      max_files=args.max_files, client=client,
                                      public=args.public)
        except Exception as exc:
            print(f"[{proj}] ERROR: {exc}", file=sys.stderr)
            continue
        total += res["chunks"]
        print(f"[{proj}] {res['visibility']}: {res['files_indexed']}/{res['files_seen']} "
              f"files -> {res['chunks']} chunks"
              + (f" (capped, {res['files_skipped_over_cap']} skipped)"
                 if res['files_skipped_over_cap'] else ""))
        if res["acl_group"]:
            print(f"        ACL-gated -> query with groups=['{res['acl_group']}'] "
                  f"(or re-run with --public)")
    print(f"done: {total} chunks into namespace {args.namespace!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
