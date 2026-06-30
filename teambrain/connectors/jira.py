"""Jira -> team-brain ingestion.

Roadmap step 2 (docs §8): Jira pairs with the **tester** (known bugs, expected
behaviour, acceptance criteria) and the **product owner** (what was decided, what
is blocked). One issue becomes a few memories:

  * a **semantic** fact memory — ``[KEY] summary`` + status/resolution + body
    (the current decision/state),
  * an optional **acceptance-criteria** memory (tagged ``acceptance``/``test``)
    when the configured custom field is present — the tester's gold,
  * an **episodic** discussion memory from the comment thread — the *why* and
    what-happened.

ACL: Jira **issue security levels** restrict visibility. A classified issue maps
to an ``acl:jira-sec:<level>`` tag so it is gated fail-closed at read time, the
same way Confluence restrictions are. Project-level permissions are coarse (like
Confluence space restrictions) — choose which projects to sync; never the whole
instance.

Cloud descriptions/comments arrive as ADF (Atlassian Document Format, JSON);
``adf_to_text`` flattens them. Server/DC sends strings, handled too.

Network is isolated behind :class:`JiraClient` (stdlib ``urllib``, injectable),
so the data flow is testable with no credentials — see ``tests/test_jira.py``.
"""
from __future__ import annotations

import json
import os
import re
import ssl
import urllib.parse
import urllib.request

from .. import store as _store
from ._text import chunk_markdown, slug

DEFAULT_TIER = "semantic"

# fields requested from Jira; the acceptance custom field (if any) is appended.
BASE_FIELDS = ("summary,description,status,resolution,issuetype,labels,"
               "priority,fixVersions,comment,security,updated,created")


# ── ADF / field flattening ────────────────────────────────────────────────────

def adf_to_text(node) -> str:
    """Flatten an Atlassian Document Format node (or a plain string) to text."""
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "".join(adf_to_text(n) for n in node)
    t = node.get("type")
    if t == "text":
        return node.get("text", "")
    if t == "hardBreak":
        return "\n"
    children = adf_to_text(node.get("content"))
    if t in ("paragraph", "heading"):
        return children + "\n\n"
    if t == "listItem":
        return "- " + children
    return children


def text_of(field) -> str:
    """Normalize a Jira text field (ADF dict, string, or None) to clean text."""
    if field is None:
        return ""
    if isinstance(field, str):
        return field.strip()
    if isinstance(field, dict):
        return re.sub(r"\n{3,}", "\n\n", adf_to_text(field)).strip()
    return str(field)


# ── ACL ───────────────────────────────────────────────────────────────────────

def security_acl_tags(issue: dict) -> list:
    """Map an issue's security level to an ``acl:`` tag. Empty => unclassified
    (public within the namespace)."""
    sec = ((issue.get("fields") or {}).get("security") or {})
    name = sec.get("name") or sec.get("id")
    return [f"{_store.ACL_PREFIX}jira-sec:{slug(name)}"] if name else []


# ── ingest ────────────────────────────────────────────────────────────────────

def ingest_issue(issue: dict, namespace: str, base_url: str = "",
                 project_key: str = "", acceptance_field: str | None = None) -> int:
    """Store one raw Jira issue object as memories. Returns the chunk count."""
    st = _store.store()
    f = issue.get("fields") or {}
    key = issue.get("key") or ""
    itype = ((f.get("issuetype") or {}).get("name") or "")
    status = ((f.get("status") or {}).get("name") or "")
    resolution = ((f.get("resolution") or {}).get("name") or "")
    labels = f.get("labels") or []

    tags = ["jira"]
    if project_key:
        tags.append(f"project:{project_key}")
    if itype:
        tags.append(f"issuetype:{slug(itype)}")
    if status:
        tags.append(f"status:{slug(status)}")
    if itype.lower() == "bug":
        tags.append("bug")
    tags += [f"label:{slug(l)}" for l in labels]
    tags += security_acl_tags(issue)
    url = f"{base_url.rstrip('/')}/browse/{key}" if base_url and key else None
    if url:
        tags.append(f"{_store.SRC_PREFIX}{url}")

    head = (f"[{key}] {f.get('summary') or ''}").strip()
    n = 0

    # 1) decision / current-state fact memory
    facts = []
    if status:
        facts.append(f"Status: {status}")
    if resolution:
        facts.append(f"Resolution: {resolution}")
    desc = text_of(f.get("description"))
    body = "\n".join(facts)
    body = (body + "\n\n" + desc).strip() if desc else body
    if body:
        for _, chunk in chunk_markdown(body):
            st.save(head, chunk, tier=DEFAULT_TIER, tags=tags,
                    source="jira", namespace=namespace)
            n += 1
    elif head:
        st.save(head, status or key, tier=DEFAULT_TIER, tags=tags,
                source="jira", namespace=namespace)
        n += 1

    # 2) acceptance criteria (tester's gold), if the configured field is present
    af = acceptance_field or os.environ.get("JIRA_ACCEPTANCE_FIELD")
    if af and f.get(af):
        ac = text_of(f.get(af))
        for _, chunk in chunk_markdown(ac):
            st.save(f"{head} — acceptance", chunk, tier=DEFAULT_TIER,
                    tags=tags + ["acceptance", "test"], source="jira",
                    namespace=namespace)
            n += 1

    # 3) comment thread (the why / what-happened) as episodic memory
    comments = ((f.get("comment") or {}).get("comments")) or []
    parts = []
    for cm in comments:
        author = ((cm.get("author") or {}).get("displayName")) or ""
        text = text_of(cm.get("body"))
        if text:
            parts.append((author + ": " if author else "") + text)
    blob = "\n\n".join(parts)
    if blob:
        for _, chunk in chunk_markdown(blob):
            st.save(f"{head} — discussion", chunk, tier="episodic",
                    tags=tags + ["comment"], source="jira", namespace=namespace)
            n += 1
    return n


def build_jql(project: str, since: str | None = None, extra: str | None = None) -> str:
    """JQL for one project, newest-friendly ordering for incremental sync.
    ``since`` is a JQL datetime, e.g. ``"2026/06/01 00:00"``."""
    clauses = [f'project = "{project}"']
    if extra:
        clauses.append(f"({extra})")
    if since:
        clauses.append(f'updated >= "{since}"')
    return " AND ".join(clauses) + " ORDER BY updated ASC"


# ── REST client ───────────────────────────────────────────────────────────────

class JiraClient:
    """Thin Jira REST client over stdlib ``urllib``.

    Auth: Cloud uses Basic (``JIRA_EMAIL`` + ``JIRA_TOKEN``); Server/DC uses a
    Bearer PAT (``JIRA_TOKEN`` only). ``base_url`` e.g.
    ``https://acme.atlassian.net``.
    """

    def __init__(self, base_url=None, token=None, email=None, timeout=30,
                 search_path="/rest/api/3/search"):
        self.base_url = (base_url or os.environ.get("JIRA_BASE_URL") or "").rstrip("/")
        token = token if token is not None else os.environ.get("JIRA_TOKEN")
        email = email if email is not None else os.environ.get("JIRA_EMAIL")
        if not self.base_url or not token:
            raise RuntimeError("set JIRA_BASE_URL and JIRA_TOKEN")
        if email:
            import base64
            cred = base64.b64encode(f"{email}:{token}".encode()).decode()
            self._auth = f"Basic {cred}"
        else:
            self._auth = f"Bearer {token}"
        self.timeout = timeout
        self.search_path = search_path
        # Corporate TLS: prefer pointing at the CA bundle (JIRA_CERT_BUNDLE) so
        # verification still happens; JIRA_VERIFY_SSL=false is the last-resort
        # escape hatch for a proxy that breaks the chain. Default: normal verify.
        bundle = os.environ.get("JIRA_CERT_BUNDLE")
        if bundle:
            self._ssl = ssl.create_default_context(cafile=bundle)
        elif os.environ.get("JIRA_VERIFY_SSL", "true").lower() == "false":
            self._ssl = ssl._create_unverified_context()
        else:
            self._ssl = None  # urllib's default verified context

    def get_json(self, url_or_path: str, params: dict | None = None) -> dict:
        url = url_or_path if url_or_path.startswith("http") else self.base_url + url_or_path
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={
            "Authorization": self._auth, "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=self.timeout, context=self._ssl) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def iter_issues(self, jql: str, fields: str, page_size: int = 50):
        """Yield issues for a JQL query, supporting both ``nextPageToken`` (newer
        Cloud enhanced search) and ``startAt``/``total`` (Server/DC) paging."""
        start, token, mode = 0, None, None
        while True:
            params = {"jql": jql, "maxResults": page_size, "fields": fields}
            if mode == "token":
                params["nextPageToken"] = token
            else:
                params["startAt"] = start
            data = self.get_json(self.search_path, params)
            issues = data.get("issues", []) or []
            for it in issues:
                yield it
            if "nextPageToken" in data or mode == "token":
                mode = "token"
                token = data.get("nextPageToken")
                if not token or data.get("isLast"):
                    break
            else:
                total = data.get("total")
                start += len(issues)
                if not issues or (total is not None and start >= total):
                    break


def sync_project(project_key: str, namespace: str, since: str | None = None,
                 jql: str | None = None, client: "JiraClient | None" = None,
                 page_size: int = 50, acceptance_field: str | None = None) -> dict:
    """Incremental sync of ONE Jira project into ``namespace``. Returns a summary
    incl. ``checkpoint`` (max ``updated`` seen) for incremental re-sync."""
    client = client or JiraClient()
    q = jql or build_jql(project_key, since=since)
    af = acceptance_field or os.environ.get("JIRA_ACCEPTANCE_FIELD")
    fields = BASE_FIELDS + (f",{af}" if af else "")
    issues = chunks = 0
    checkpoint = since
    for issue in client.iter_issues(q, fields, page_size=page_size):
        chunks += ingest_issue(issue, namespace, base_url=client.base_url,
                               project_key=project_key, acceptance_field=af)
        issues += 1
        upd = ((issue.get("fields") or {}).get("updated")) or ""
        if upd and (checkpoint is None or upd > checkpoint):
            checkpoint = upd
    return {"project": project_key, "namespace": namespace,
            "issues": issues, "chunks": chunks, "checkpoint": checkpoint}
