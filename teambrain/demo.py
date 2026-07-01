"""``team-brain demo`` — pre-fill a namespace from every repo you already have.

The cold-start killer. team-brain's value depends on a filled store, so fill
it from artifacts that already exist *before* anyone is asked to change how
they work. Point this at the directory that holds your checkouts; it finds
every git repo underneath and ingests, per repo:

  * **git commits + TODO/FIXME notes** (intellij connector — no creds),
  * **the OpenSpec tree**, when the repo has one (openspec connector),
  * **business rules mined from source** (the gitlab connector's miner run
    over local files — a wired LLM makes them sharp, the offline heuristic
    works without one).

Optionally — when the credentials are already in the env — one Jira project,
one Confluence space, and one GitHub repo's merged PRs. Everything lands in
one namespace, so the demo isn't slides: it's "ask it about YOUR ticket."

    team-brain demo ~/IdeaProjects --namespace demo --jira PROSET

Failures are per-repo and non-fatal: a broken checkout is reported and the
sweep continues. Mining is capped per repo (``--max-files``) so a wired LLM's
cost stays bounded; raise the cap for the overnight run.
"""
from __future__ import annotations

import argparse
import os

from . import code_summary
from .connectors import gitlab, intellij, openspec


def find_repos(root: str, max_depth: int = 3) -> list:
    """Git repos under ``root`` (or ``root`` itself). Hidden dirs are skipped;
    found repos are not descended into."""
    root = os.path.abspath(os.path.expanduser(root))
    if os.path.isdir(os.path.join(root, ".git")):
        return [root]
    out = []
    base = root.rstrip(os.sep).count(os.sep)
    for dirpath, dirnames, _ in os.walk(root):
        if os.path.isdir(os.path.join(dirpath, ".git")):
            out.append(dirpath)
            dirnames[:] = []
            continue
        if dirpath.count(os.sep) - base >= max_depth:
            dirnames[:] = []
            continue
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
    return sorted(out)


def mine_business(path: str, namespace: str, repo: str, max_files: int = 40,
                  summarize=None, git=None) -> dict:
    """Run the gitlab connector's business-rule miner over a *local* checkout:
    same file selection, same extraction, no API. Caps attempts at
    ``max_files`` to bound LLM cost."""
    git = git or intellij.GitRepo(path)
    summarize = summarize or code_summary.summarize
    tried = mined = chunks = 0
    for rel in git.tracked_files():
        if not gitlab._selected(rel, gitlab.CODE_EXTS):
            continue
        if tried >= max_files:
            break
        tried += 1
        body = "\n".join(git.file_lines(rel))
        if not body.strip():
            continue
        n = gitlab.ingest_file(repo, rel, body, namespace, summarize=summarize)
        if n:
            mined += 1
            chunks += n
    return {"tried": tried, "mined": mined, "chunks": chunks}


def ingest_repo(path: str, namespace: str, mine: bool = True,
                max_files: int = 40, summarize=None) -> dict:
    """One repo, all local sources. Errors are recorded, never raised."""
    name = os.path.basename(path.rstrip(os.sep))
    res = {"repo": name, "commits": 0, "todos": 0, "openspec_docs": 0,
           "mined": 0, "errors": []}
    try:
        s = intellij.sync(path, namespace, repo=name)
        res["commits"], res["todos"] = s["commits"], s["todos"]
    except Exception as exc:
        res["errors"].append(f"git: {exc}")

    if os.path.isdir(os.path.join(path, "openspec")):
        try:
            res["openspec_docs"] = openspec.sync(path, namespace, repo=name)["docs"]
        except Exception as exc:
            res["errors"].append(f"openspec: {exc}")

    if mine and not any(e.startswith("git:") for e in res["errors"]):
        try:
            res["mined"] = mine_business(path, namespace, name,
                                         max_files=max_files,
                                         summarize=summarize)["mined"]
        except Exception as exc:
            res["errors"].append(f"mine: {exc}")
    return res


def _optional_syncs(a, out=print):
    """Jira / Confluence / GitHub PRs, when asked for — creds come from the
    same env vars the connectors already document."""
    if a.jira:
        try:
            from .connectors.jira import sync_project
            out(f"  jira {a.jira}: {sync_project(a.jira, a.namespace)}")
        except Exception as exc:
            out(f"  jira {a.jira}: FAILED — {exc}")
    if a.confluence:
        try:
            from .connectors.confluence import sync_space
            out(f"  confluence {a.confluence}: {sync_space(a.confluence, a.namespace)}")
        except Exception as exc:
            out(f"  confluence {a.confluence}: FAILED — {exc}")
    if a.github:
        try:
            from .connectors.pr import sync_repo
            out(f"  github {a.github}: {sync_repo(a.github, a.namespace)}")
        except Exception as exc:
            out(f"  github {a.github}: FAILED — {exc}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="team-brain demo",
        description="Pre-fill a namespace from every git repo under a "
                    "directory (+ optional Jira/Confluence/GitHub).")
    ap.add_argument("root", nargs="?", default=".",
                    help="directory holding your checkouts (or one repo)")
    ap.add_argument("--namespace",
                    default=os.environ.get("TEAMBRAIN_NAMESPACE") or "demo")
    ap.add_argument("--max-files", type=int, default=40,
                    help="business-mining cap per repo (bounds LLM cost)")
    ap.add_argument("--no-mine", action="store_true",
                    help="skip business-rule mining (commits/TODOs/specs only)")
    ap.add_argument("--jira", metavar="PROJECT",
                    help="also ingest this Jira project (JIRA_* env)")
    ap.add_argument("--confluence", metavar="SPACE",
                    help="also ingest this Confluence space (CONFLUENCE_* env)")
    ap.add_argument("--github", metavar="OWNER/REPO",
                    help="also ingest this repo's merged PRs (GITHUB_TOKEN env)")
    a = ap.parse_args(argv)

    repos = find_repos(a.root)
    if not repos:
        print(f"no git repos found under {a.root}")
        return 1
    print(f"{len(repos)} repo(s) under {os.path.abspath(a.root)} "
          f"-> namespace '{a.namespace}'\n")

    totals = {"commits": 0, "todos": 0, "openspec_docs": 0, "mined": 0}
    failed = 0
    for path in repos:
        r = ingest_repo(path, a.namespace, mine=not a.no_mine,
                        max_files=a.max_files)
        for k in totals:
            totals[k] += r[k]
        line = (f"  {r['repo']}: {r['commits']} commits, {r['todos']} todos, "
                f"{r['openspec_docs']} spec docs, {r['mined']} files mined")
        if r["errors"]:
            failed += 1
            line += f"  [{'; '.join(r['errors'])}]"
        print(line)

    _optional_syncs(a)

    print(f"\ndone: {totals['commits']} commits, {totals['todos']} todos, "
          f"{totals['openspec_docs']} spec docs, {totals['mined']} mined files "
          f"across {len(repos)} repos"
          + (f" ({failed} with errors)" if failed else ""))
    print(f"\ntry it (MCP server: `team-brain`, namespace '{a.namespace}'):\n"
          f"  team_assist(\"what business rules govern <your domain>?\", role=\"po\")\n"
          f"  team_explain_ticket(\"<YOUR-TICKET-KEY>\")\n"
          f"  team_test_plan(\"<a feature you shipped last sprint>\")")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
