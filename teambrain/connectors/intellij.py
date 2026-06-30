"""IntelliJ project VCS activity + notes -> team-brain ingestion (local).

The developer's other IDE is IntelliJ. Unlike Devin there is no agent transcript
to tap — the valuable record IntelliJ accumulates is **local VCS activity**: the
commits a developer makes (the *why*, in their own words) and the **TODO/FIXME
notes** IntelliJ's TODO tool window surfaces from source comments. Both are
captured straight from the project's git working copy — no IDE-internal parsing,
no plugin, no network.

What it stores:
  * **commits** -> ``episodic`` ``intellij``/``commit`` memories — "what the dev
    did and why", carrying the branch as context;
  * **TODO/FIXME notes** -> ``semantic`` ``intellij``/``todo`` memories — standing
    developer intent, with a ``src:`` back-link to ``path#Lnn``.

The PO<->dev bridge: every commit message and branch name is scanned for Jira
issue keys (``ABC-123``) which become ``ticket:<KEY>`` tags — so a tester or PO
asking about a ticket finds the dev work (and the notes) that touched it.

ACL: a local checkout is private by nature; pass ``acl_groups`` to scope a
project's memories fail-closed (a memory with no ``acl:*`` tag is public within
its namespace, like every other connector).

Git access is isolated behind :class:`GitRepo` (stdlib ``subprocess``, injectable)
so ``tests/test_intellij.py`` exercises the full data flow with a fake repo and
no real ``git``.
"""
from __future__ import annotations

import os
import re
import subprocess

from .. import store as _store
from ._text import chunk_markdown, chunk_fixed, slug, ticket_keys

COMMIT_TIER = "episodic"
TODO_TIER = "semantic"

# Source extensions IntelliJ's TODO view scans (kept small; skip vendored/binary).
TODO_EXTS = (".java", ".kt", ".kts", ".scala", ".groovy", ".py", ".js", ".ts",
             ".tsx", ".jsx", ".go", ".rs", ".rb", ".php", ".cs", ".cpp", ".c",
             ".h", ".sql", ".xml", ".yml", ".yaml", ".gradle", ".properties")
_TODO_RE = re.compile(r"(?:#|//|/\*|\*|--|<!--)\s*(TODO|FIXME|XXX|HACK)\b[:\s]?(.*)",
                      re.IGNORECASE)


def factualize(text: str) -> str:
    """Optional depersonalise/clean hook before storage (distill idea, docs §6).
    Default is identity."""
    return text


def _acl_tags(acl_groups) -> list:
    return [f"{_store.ACL_PREFIX}{slug(g)}" for g in (acl_groups or []) if str(g).strip()]


# ── ingest seams ──────────────────────────────────────────────────────────────

def ingest_commit(commit: dict, namespace: str, repo: str = "", web_base=None,
                  acl_groups=None) -> int:
    """Store one git commit as memories. ``commit`` is the normalized shape
    produced by :meth:`GitRepo.log`. Returns the chunk count."""
    sha = (commit.get("sha") or "").strip()
    subject = (commit.get("subject") or "").strip()
    body = (commit.get("body") or "").strip()
    author = (commit.get("author") or "").strip()
    branch = (commit.get("branch") or "").strip()
    if not (subject or body):
        return 0

    tags = ["intellij", "commit"]
    if repo:
        tags.append(f"repo:{slug(repo)}")
    if branch:
        tags.append(f"branch:{slug(branch)}")
    tags += [f"ticket:{k}" for k in ticket_keys(branch, subject, body)]
    tags += _acl_tags(acl_groups)
    if web_base and sha:
        tags.append(f"{_store.SRC_PREFIX}{web_base.rstrip('/')}/commit/{sha}")

    head = f"[{sha[:8]}] {subject}" if sha else subject
    facts = []
    if author:
        facts.append(f"Committed by {author}.")
    if branch:
        facts.append(f"On branch {branch}.")
    content = factualize(("\n".join(facts) + "\n\n" + body).strip()
                         if body else "\n".join(facts) or subject)

    st = _store.store()
    n = 0
    for _, chunk in chunk_markdown(content):
        st.save(head, chunk, tier=COMMIT_TIER, tags=tags,
                source="intellij", namespace=namespace)
        n += 1
    return n


def ingest_todo(todo: dict, namespace: str, repo: str = "", acl_groups=None) -> int:
    """Store one TODO/FIXME note as a memory with a ``path#Lnn`` back-link."""
    text = (todo.get("text") or "").strip()
    path = (todo.get("file") or "").strip()
    line = todo.get("line")
    marker = (todo.get("marker") or "TODO").upper()
    if not text:
        return 0

    tags = ["intellij", "todo", f"marker:{slug(marker)}"]
    if repo:
        tags.append(f"repo:{slug(repo)}")
    tags += [f"ticket:{k}" for k in ticket_keys(text)]
    tags += _acl_tags(acl_groups)
    if path:
        anchor = f"{path}#L{line}" if line else path
        tags.append(f"{_store.SRC_PREFIX}{anchor}")

    head = f"{marker} in {path}" if path else marker
    st = _store.store()
    st.save(head, factualize(text), tier=TODO_TIER, tags=tags,
            source="intellij", namespace=namespace)
    return 1


# ── git access (injectable) ────────────────────────────────────────────────────

_REC, _UNIT = "\x1e", "\x1f"


class GitRepo:
    """Thin git accessor over stdlib ``subprocess``, scoped to one working copy.
    Injectable so the connector's data flow is testable with no real repo."""

    def __init__(self, path: str):
        self.path = os.path.abspath(os.path.expanduser(path))

    def _run(self, *args) -> str:
        out = subprocess.run(["git", "-C", self.path, *args],
                             capture_output=True, text=True, check=True)
        return out.stdout

    def current_branch(self) -> str:
        try:
            return self._run("rev-parse", "--abbrev-ref", "HEAD").strip()
        except Exception:
            return ""

    def log(self, since=None, max_count=500, branch=None):
        """Yield commits newest-first as normalized dicts. ``since`` is any git
        date expression (ISO date, ``2 weeks ago``, …); ``branch`` defaults to
        the current one and is attached to each commit as context."""
        branch = branch or self.current_branch()
        fmt = _UNIT.join(["%H", "%an", "%aI", "%s", "%b"]) + _REC
        args = ["log", "--no-color", f"--pretty=format:{fmt}"]
        if max_count:
            args.append(f"--max-count={int(max_count)}")
        if since:
            args.append(f"--since={since}")
        for raw in self._run(*args).split(_REC):
            raw = raw.strip("\n")
            if not raw.strip():
                continue
            sha, author, date, subject, body = (raw.split(_UNIT) + ["", "", "", "", ""])[:5]
            yield {"sha": sha, "author": author, "date": date,
                   "subject": subject, "body": body, "branch": branch}

    def tracked_files(self):
        for line in self._run("ls-files").splitlines():
            if line.strip():
                yield line.strip()

    def file_lines(self, rel_path: str):
        full = os.path.join(self.path, rel_path)
        try:
            with open(full, "r", encoding="utf-8", errors="replace") as fh:
                return fh.read().splitlines()
        except OSError:
            return []


def scan_todos(repo: "GitRepo", exts=TODO_EXTS, max_files=2000):
    """Yield ``{file, line, marker, text}`` for TODO/FIXME-style comments in the
    repo's tracked source files — the IntelliJ TODO tool window, from the CLI."""
    seen = 0
    for rel in repo.tracked_files():
        if not rel.lower().endswith(tuple(exts)):
            continue
        seen += 1
        if max_files and seen > max_files:
            break
        for i, text in enumerate(repo.file_lines(rel), 1):
            m = _TODO_RE.search(text)
            if m:
                note = m.group(2).strip().rstrip("*/ ").strip()
                if note:
                    yield {"file": rel, "line": i,
                           "marker": m.group(1).upper(), "text": note}


# ── sync ────────────────────────────────────────────────────────────────────

def sync(path: str, namespace: str, since=None, repo=None, web_base=None,
         acl_groups=None, max_commits=500, include_todos=True,
         git: "GitRepo | None" = None) -> dict:
    """Ingest a local IntelliJ project's git commits (+ TODO/FIXME notes) into
    ``namespace``. ``path`` is the project root; ``repo`` is a label for tags
    (defaults to the folder name); ``web_base`` (e.g. a GitLab/GitHub repo URL)
    turns SHAs into ``src:`` commit links. Returns a summary including
    ``checkpoint`` (newest commit date seen) for incremental re-sync."""
    git = git or GitRepo(path)
    repo = repo or os.path.basename(git.path)

    commits = chunks = 0
    checkpoint = since
    for c in git.log(since=since, max_count=max_commits):
        n = ingest_commit(c, namespace, repo=repo, web_base=web_base,
                          acl_groups=acl_groups)
        if n:
            commits += 1
            chunks += n
        d = c.get("date") or ""
        if d and (checkpoint is None or d > checkpoint):
            checkpoint = d

    todos = 0
    if include_todos:
        for t in scan_todos(git):
            todos += ingest_todo(t, namespace, repo=repo, acl_groups=acl_groups)

    return {"project": repo, "namespace": namespace, "branch": git.current_branch(),
            "commits": commits, "chunks": chunks, "todos": todos,
            "checkpoint": checkpoint}
