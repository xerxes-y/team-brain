"""GitLab codebase -> team-brain ingestion, for the PRODUCT OWNER.

The point is *not* code structure (that's the developer's need — adopt
codebase-memory-mcp, docs §7). The point is the **business knowledge buried in
the code**: rules, limits, statuses, permissions, workflows. We pull selected
files from a GitLab project, run each through ``code_summary.summarize`` (Claude,
or an offline heuristic) to extract product-owner-readable business rules, and
store those as ``business`` memories the PO role can search to write tickets —
each with a ``src:`` back-link to the exact GitLab file.

The three Confluence failure modes (docs §6) apply to code too:
  * PERMISSIONS — a private/internal project is gated: every memory gets an
    ``acl:repo:<project>`` tag (fail closed). Public projects are public.
  * STALENESS  — select by extension + path, skip vendored/build dirs, cap file
    count. Never index a whole monorepo blindly.
  * LENGTH     — one memory per file's business summary (sub-split if long), not
    one giant blob; raw code is summarized, not stored verbatim.

Network is isolated behind :class:`GitLabClient` (stdlib ``urllib``, injectable),
so ``tests/test_gitlab.py`` exercises tree paging, selection, ACL, and the
business-extraction flow with no token and no LLM.
"""
from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request

from .. import store as _store
from .. import code_summary
from ._text import chunk_fixed, slug

DEFAULT_TIER = "semantic"
GITLAB_API = "https://gitlab.com/api/v4"

# code-bearing extensions worth mining for business rules; tune per project.
CODE_EXTS = {
    "py", "js", "ts", "tsx", "jsx", "java", "kt", "go", "rb", "rs", "cs",
    "php", "scala", "sql", "graphql",
}
# directories that are noise for business knowledge.
SKIP_DIRS = ("node_modules/", "vendor/", "dist/", "build/", "target/",
             ".git/", "__pycache__/", "test/", "tests/", "spec/", "__tests__/")


def _ext(path: str) -> str:
    return path.rsplit(".", 1)[-1].lower() if "." in path else ""


def _selected(path: str, exts) -> bool:
    if any(skip in (path + "/") for skip in SKIP_DIRS):
        return False
    return _ext(path) in exts


# ── REST client ───────────────────────────────────────────────────────────────

class GitLabClient:
    """Thin GitLab REST v4 client over stdlib ``urllib``.

    Auth: a personal/project access token via ``PRIVATE-TOKEN`` (set
    ``GITLAB_TOKEN``), or omit for public projects. ``base_url`` defaults to
    ``https://gitlab.com/api/v4``; set ``GITLAB_BASE_URL`` for self-hosted.
    """

    def __init__(self, token=None, base_url=None, timeout=30):
        self.base_url = (base_url or os.environ.get("GITLAB_BASE_URL") or GITLAB_API).rstrip("/")
        self.token = token if token is not None else os.environ.get("GITLAB_TOKEN")
        self.timeout = timeout
        # web base for blob links: strip a trailing /api/v4
        self.web_base = self.base_url[:-len("/api/v4")] if self.base_url.endswith("/api/v4") \
            else self.base_url

    def _headers(self):
        return {"PRIVATE-TOKEN": self.token} if self.token else {}

    def get_json(self, path: str, params: dict | None = None):
        url = path if path.startswith("http") else self.base_url + path
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={**self._headers(),
                                                   "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def get_text(self, path: str, params: dict | None = None) -> str:
        url = path if path.startswith("http") else self.base_url + path
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers=self._headers())
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")

    @staticmethod
    def _pid(project: str) -> str:
        return urllib.parse.quote(project, safe="")  # group/project -> URL-encoded id

    def visibility(self, project: str) -> str:
        """'public' | 'internal' | 'private'. Defaults to 'private' (fail closed)
        if the field is missing or unreadable."""
        try:
            return (self.get_json(f"/projects/{self._pid(project)}") or {}).get(
                "visibility", "private")
        except Exception:
            return "private"

    def iter_tree(self, project: str, ref: str = "HEAD", per_page: int = 100):
        """Yield blob entries ({path, type, ...}) for the whole repo tree, paging
        until a short page."""
        page = 1
        pid = self._pid(project)
        while True:
            rows = self.get_json(f"/projects/{pid}/repository/tree", {
                "ref": ref, "recursive": "true", "per_page": per_page, "page": page})
            if not rows:
                break
            for r in rows:
                if r.get("type") == "blob":
                    yield r
            if len(rows) < per_page:
                break
            page += 1

    def file_raw(self, project: str, file_path: str, ref: str = "HEAD") -> str:
        fp = urllib.parse.quote(file_path, safe="")
        return self.get_text(
            f"/projects/{self._pid(project)}/repository/files/{fp}/raw", {"ref": ref})


# ── ingest ────────────────────────────────────────────────────────────────────

def ingest_file(project: str, file_path: str, body: str, namespace: str,
                acl_tags=None, web_base: str = "", ref: str = "HEAD",
                summarize=code_summary.summarize) -> int:
    """Extract business rules from one file's source and store them. Returns the
    chunk count (0 if the file has no business signal)."""
    summary = summarize(body, file_path)
    if not summary.strip():
        return 0
    st = _store.store()
    tags = ["gitlab", "code", "business", "spec",
            f"repo:{slug(project)}", f"lang:{_ext(file_path)}"]
    tags += list(acl_tags or [])
    if web_base:
        url = f"{web_base}/{project}/-/blob/{ref}/{file_path}"
        tags.append(f"{_store.SRC_PREFIX}{url}")
    title = f"{project}:{file_path}"
    n = 0
    for chunk in chunk_fixed(summary, max_chars=1500):
        st.save(title, chunk, tier=DEFAULT_TIER, tags=tags,
                source="gitlab", namespace=namespace)
        n += 1
    return n


def sync_project(project: str, namespace: str, ref: str = "HEAD",
                 exts=None, max_files: int | None = None,
                 client: "GitLabClient | None" = None,
                 summarize=code_summary.summarize, public: bool = False) -> dict:
    """Mine business knowledge from ONE GitLab project into ``namespace``.

    ``project`` is ``group/project`` (or a numeric id). Only ``exts`` files
    outside vendored/test dirs are read.

    ACL: by default a private/internal project is gated with ``acl:repo:<slug>``
    (fail-closed) — readers must present that group. Set ``public=True`` to make
    the mined knowledge **visible to everyone in the namespace** — use this only
    when you've decided the whole namespace audience is authorized to see the
    project (e.g. an internal repo synced into your team's namespace).

    Returns a summary including ``files_seen``/``files_indexed``/``chunks``.
    """
    client = client or GitLabClient()
    exts = set(exts) if exts else CODE_EXTS
    vis = client.visibility(project)
    gated = (vis != "public") and not public
    acl_tags = [f"{_store.ACL_PREFIX}repo:{slug(project)}"] if gated else []

    seen = indexed = chunks = skipped = 0
    for entry in client.iter_tree(project, ref=ref):
        path = entry.get("path", "")
        if not _selected(path, exts):
            continue
        if max_files is not None and seen >= max_files:
            skipped += 1
            continue
        seen += 1
        try:
            body = client.file_raw(project, path, ref=ref)
        except Exception:
            continue
        c = ingest_file(project, path, body, namespace, acl_tags=acl_tags,
                        web_base=client.web_base, ref=ref, summarize=summarize)
        if c:
            indexed += 1
            chunks += c
    return {"project": project, "namespace": namespace, "visibility": vis,
            "files_seen": seen, "files_indexed": indexed, "chunks": chunks,
            "files_skipped_over_cap": skipped,
            "acl_group": (acl_tags[0][len(_store.ACL_PREFIX):] if acl_tags else None)}
