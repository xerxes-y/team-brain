"""Pull/Merge requests -> team-brain ingestion (GitHub).

Roadmap step 3 (docs §8): PRs carry **the why** a change shipped — the gold for
the **developer** role (pairs with the codebase-memory MCP, which carries the
*where/how in code*, docs §7). We ingest only **merged** PRs: the decision that
actually landed, not every abandoned attempt. One PR becomes a semantic memory:
``[#N] title`` + who merged it + the PR body (the rationale), with a ``src:``
back-link to the PR.

ACL: GitHub access is repo-level. A PR from a **private** repo is tagged
``acl:repo:<owner/repo>`` so it is gated fail-closed; public-repo PRs are public
within the namespace. Choose which repos to sync.

Network is isolated behind :class:`GitHubClient` (stdlib ``urllib``, injectable)
so the data flow is testable with no token — see ``tests/test_pr.py``.
"""
from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request

from .. import store as _store
from ._text import chunk_markdown, slug

DEFAULT_TIER = "semantic"
GITHUB_API = "https://api.github.com"


def repo_acl_tags(pr: dict) -> list:
    """Private-repo PRs get an ``acl:repo:<full_name>`` tag (fail closed);
    public repos are public within the namespace."""
    repo = ((pr.get("base") or {}).get("repo") or {})
    if repo.get("private"):
        full = repo.get("full_name")
        return [f"{_store.ACL_PREFIX}repo:{slug(full) if full else 'private'}"]
    return []


def ingest_pr(pr: dict, namespace: str, repo: str = "") -> int:
    """Store one merged GitHub PR as memories. Unmerged PRs are skipped (no
    decision shipped). Returns the chunk count."""
    if not pr.get("merged_at"):
        return 0
    st = _store.store()
    num = pr.get("number")
    title = pr.get("title") or ""
    body = pr.get("body") or ""
    user = ((pr.get("user") or {}).get("login")) or ""
    labels = [l.get("name") for l in (pr.get("labels") or []) if l.get("name")]
    repo = repo or ((pr.get("base") or {}).get("repo") or {}).get("full_name") or ""

    tags = ["pr", "decision"]
    if repo:
        tags.append(f"repo:{slug(repo)}")
    tags += [f"label:{slug(l)}" for l in labels]
    tags += repo_acl_tags(pr)
    url = pr.get("html_url")
    if url:
        tags.append(f"{_store.SRC_PREFIX}{url}")

    head = (f"[#{num}] {title}").strip()
    facts = []
    if user:
        facts.append(f"Merged PR by {user}.")
    content = ("\n".join(facts) + "\n\n" + body).strip() if body else "\n".join(facts)
    if not content.strip():
        content = title
    n = 0
    for _, chunk in chunk_markdown(content):
        st.save(head, chunk, tier=DEFAULT_TIER, tags=tags,
                source="pr", namespace=namespace)
        n += 1
    return n


# ── REST client ───────────────────────────────────────────────────────────────

class GitHubClient:
    """Thin GitHub REST client over stdlib ``urllib``. ``token`` (``GITHUB_TOKEN``)
    is optional for public repos but recommended (rate limits / private access)."""

    def __init__(self, token=None, base_url=GITHUB_API, timeout=30):
        self.base_url = (base_url or GITHUB_API).rstrip("/")
        self.token = token if token is not None else os.environ.get("GITHUB_TOKEN")
        self.timeout = timeout

    def get_json(self, url_or_path: str, params: dict | None = None):
        url = url_or_path if url_or_path.startswith("http") else self.base_url + url_or_path
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        headers = {"Accept": "application/vnd.github+json",
                   "X-GitHub-Api-Version": "2022-11-28"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def iter_pulls(self, repo: str, state: str = "closed", per_page: int = 100):
        """Yield PRs for ``owner/repo``, page-number paging until a short page."""
        page = 1
        while True:
            data = self.get_json(f"/repos/{repo}/pulls", {
                "state": state, "per_page": per_page, "page": page,
                "sort": "updated", "direction": "asc"})
            if not data:
                break
            for pr in data:
                yield pr
            if len(data) < per_page:
                break
            page += 1


def sync_repo(repo: str, namespace: str, since: str | None = None,
              client: "GitHubClient | None" = None, state: str = "closed") -> dict:
    """Incremental sync of merged PRs from ONE ``owner/repo`` into ``namespace``.
    ``since`` is an ISO-8601 ``updated_at`` cutoff. Returns a summary incl.
    ``checkpoint`` (max ``updated_at`` seen) for incremental re-sync."""
    client = client or GitHubClient()
    prs = chunks = 0
    checkpoint = since
    for pr in client.iter_pulls(repo, state=state):
        upd = pr.get("updated_at") or ""
        if since and upd and upd < since:
            continue
        c = ingest_pr(pr, namespace, repo=repo)
        if c:
            prs += 1
            chunks += c
        if upd and (checkpoint is None or upd > checkpoint):
            checkpoint = upd
    return {"repo": repo, "namespace": namespace,
            "prs": prs, "chunks": chunks, "checkpoint": checkpoint}
