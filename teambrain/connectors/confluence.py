"""Confluence -> team-brain ingestion.

Confluence is the highest-value source and the one that burns org-knowledge
products (docs §6). The three hard parts are handled here:

  * PERMISSIONS / ACL  — each page's read restrictions are resolved to
    ``acl:<group>`` / ``acl:user:<id>`` tags. A page with **no** read restriction
    is public; any restriction (group *or* user) produces at least one ``acl:``
    tag, so a user-only restriction never falls through to public. Restricted
    chunks are gated at read time by ``teambrain.store.visible_to`` — **fail
    closed.** (Space-level / inherited restrictions are NOT visible on the
    page-restriction endpoint; choose which spaces to sync accordingly — see the
    caveat on ``read_acl_tags``.)
  * STALENESS          — select by space + label + recency (``since``); never
    dump a whole instance. Start with ONE high-signal space.
  * LENGTH + FRESHNESS — chunk by heading, one memory per chunk with a back-link
    (``src:`` tag → citation URL); incremental re-sync via CQL
    ``lastmodified >= <checkpoint>``.

Network is isolated behind :class:`ConfluenceClient` (stdlib ``urllib``, no new
deps). ``sync_space`` accepts a ``client=`` so the data flow is testable with a
fake — no credentials or network needed to exercise paging/ACL/chunking.

Optional ``factualize`` hook (idea from 5queezer/distill): depersonalise/clean
raw page text via a local LLM before storing. Off by default (identity).
"""
from __future__ import annotations

import html as _html
import json
import os
import re
import urllib.parse
import urllib.request

from .. import store as _store

# tier for doc knowledge; restriction tags get an acl: prefix
DOC_TIER = "semantic"


# ── text cleaning + chunking ──────────────────────────────────────────────────

def factualize(text: str) -> str:
    """Clean/neutralise page text before storage. Override via a hook later;
    default is identity (off)."""
    return text


_HEADING = re.compile(r"(?is)<h[1-3][^>]*>(.*?)</h[1-3]>")
_LOOKS_HTML = re.compile(r"(?is)<(h[1-6]|p|div|ul|ol|table|ac:|span)\b")


def _strip_tags(fragment: str) -> str:
    """Confluence storage format is XHTML; reduce a fragment to plain text."""
    fragment = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", fragment)
    fragment = re.sub(r"(?s)<[^>]+>", " ", fragment)
    fragment = _html.unescape(fragment)
    return re.sub(r"\s+", " ", fragment).strip()


def chunk_storage_format(html_body: str, max_chars: int = 1500):
    """Split Confluence storage-format (XHTML) into heading-bounded plain-text
    chunks. Yields ``(heading, body)``; long sections are sub-split at
    ``max_chars`` so one long page never becomes one giant memory."""
    sections, pos, heading = [], 0, ""
    for m in _HEADING.finditer(html_body):
        pre = html_body[pos:m.start()]
        if pre.strip():
            sections.append((heading, pre))
        heading = _strip_tags(m.group(1))
        pos = m.end()
    tail = html_body[pos:]
    if tail.strip():
        sections.append((heading, tail))
    for head, body_html in sections:
        body = _strip_tags(body_html)
        for i in range(0, len(body), max_chars):
            chunk = body[i:i + max_chars].strip()
            if chunk:
                yield (head, chunk)


def chunk_by_heading(text: str, max_chars: int = 1500):
    """Markdown fallback splitter (``#``/``##``/``###`` headings). Yields
    ``(heading, body)``."""
    parts = re.split(r"(?m)^(#{1,3}\s+.*$)", text)
    if len(parts) == 1:
        for i in range(0, len(text), max_chars):
            chunk = text[i:i + max_chars].strip()
            if chunk:
                yield ("", chunk)
        return
    heading = ""
    for seg in parts:
        if re.match(r"^#{1,3}\s+", seg or ""):
            heading = seg.strip("# ").strip()
        elif seg and seg.strip():
            body = seg.strip()
            for i in range(0, len(body), max_chars):
                chunk = body[i:i + max_chars].strip()
                if chunk:
                    yield (heading, chunk)


def chunk_page(body: str, max_chars: int = 1500):
    """Pick the right chunker by sniffing the body: storage-format XHTML from the
    Confluence API, else markdown."""
    if _LOOKS_HTML.search(body or ""):
        yield from chunk_storage_format(body, max_chars)
    else:
        yield from chunk_by_heading(body, max_chars)


# ── ACL mapping ───────────────────────────────────────────────────────────────

def restriction_tags(page: dict) -> list:
    """Map a normalized page's read restrictions to ``acl:`` tags. Empty =>
    public.

    Accepts both ``read_groups`` and ``read_users`` so a user-only restriction
    is never silently treated as public (fail closed). The asker presents
    matching identities (``<group>`` and ``user:<id>``) at read time."""
    tags = [f"{_store.ACL_PREFIX}{g}" for g in page.get("read_groups", []) if g]
    tags += [f"{_store.ACL_PREFIX}user:{u}" for u in page.get("read_users", []) if u]
    return tags


# ── REST client (network isolated here) ───────────────────────────────────────

class ConfluenceClient:
    """Thin Confluence REST v1 client over stdlib ``urllib``.

    Auth: a Cloud API token uses Basic auth (set ``CONFLUENCE_EMAIL`` +
    ``CONFLUENCE_TOKEN``); a Server/DC personal access token uses Bearer (set
    ``CONFLUENCE_TOKEN`` only). ``base_url`` must include the context path on
    Cloud, e.g. ``https://acme.atlassian.net/wiki``.
    """

    def __init__(self, base_url=None, token=None, email=None, timeout=30):
        self.base_url = (base_url or os.environ.get("CONFLUENCE_BASE_URL") or "").rstrip("/")
        token = token if token is not None else os.environ.get("CONFLUENCE_TOKEN")
        email = email if email is not None else os.environ.get("CONFLUENCE_EMAIL")
        if not self.base_url or not token:
            raise RuntimeError("set CONFLUENCE_BASE_URL and CONFLUENCE_TOKEN")
        if email:
            import base64
            cred = base64.b64encode(f"{email}:{token}".encode()).decode()
            self._auth = f"Basic {cred}"
        else:
            self._auth = f"Bearer {token}"
        self.timeout = timeout

    def get_json(self, url_or_path: str, params: dict | None = None) -> dict:
        url = url_or_path if url_or_path.startswith("http") else self.base_url + url_or_path
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={
            "Authorization": self._auth, "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def iter_search(self, cql: str, expand: str, limit: int = 25):
        """Yield content results for a CQL query, following ``_links.next`` so it
        works with both cursor (Cloud) and start (Server) paging."""
        data = self.get_json("/rest/api/content/search",
                             {"cql": cql, "limit": limit, "expand": expand})
        while True:
            for row in data.get("results", []):
                yield row
            links = data.get("_links") or {}
            nxt = links.get("next")
            if not nxt:
                break
            base = links.get("base", "")
            data = self.get_json((base + nxt) if nxt.startswith("/") else nxt)

    def read_acl_tags(self, page_id: str) -> list:
        """Resolve a page's READ restrictions to ``acl:`` tags.

        CAVEAT: ``/restriction/byOperation/read`` returns only restrictions set
        *directly on the page* — it does NOT include space-level or ancestor
        restrictions. A page can therefore look public here while being walled
        off at the space level. Mitigation (docs §6): only sync spaces you have
        deliberately chosen, and never the whole instance."""
        data = self.get_json(
            f"/rest/api/content/{page_id}/restriction/byOperation/read")
        restr = data.get("restrictions") or {}
        groups = [g.get("name") for g in (restr.get("group") or {}).get("results", [])]
        users = [u.get("accountId") or u.get("username") or u.get("userKey")
                 for u in (restr.get("user") or {}).get("results", [])]
        page = {"read_groups": [g for g in groups if g],
                "read_users": [u for u in users if u]}
        return restriction_tags(page)


# ── ingest ────────────────────────────────────────────────────────────────────

def ingest_page(page: dict, namespace: str, space_label: str = "") -> int:
    """Store one normalized Confluence page as chunked memories. Returns the
    chunk count.

    ``page`` shape (produced by ``sync_space`` from the API, or hand-built in
    tests)::

        {id, title, body, url,
         read_groups:[...], read_users:[...], labels:[...], acl_tags:[...]}

    ``acl_tags`` (pre-resolved ``acl:`` strings) take precedence over
    ``read_groups``/``read_users`` so the network-resolved restrictions from
    ``ConfluenceClient.read_acl_tags`` flow straight through.
    """
    st = _store.store()
    acl = page.get("acl_tags")
    if acl is None:
        acl = restriction_tags(page)
    base_tags = ["confluence"]
    if space_label:
        base_tags.append(f"space:{space_label}")
    base_tags += [f"label:{l}" for l in page.get("labels", [])]
    base_tags += list(acl)
    url = page.get("url")
    if url:
        base_tags.append(f"{_store.SRC_PREFIX}{url}")
    n = 0
    for heading, body in chunk_page(page.get("body", "")):
        title = page.get("title") or "(untitled)"
        if heading and heading.lower() not in title.lower():
            title = f"{title} — {heading}"
        st.save(title, factualize(body), tier=DOC_TIER, tags=base_tags,
                source="confluence", namespace=namespace)
        n += 1
    return n


def _normalize(raw: dict, client: "ConfluenceClient") -> dict:
    """Map a raw API content object + its resolved restrictions to the
    ``ingest_page`` shape."""
    pid = raw.get("id")
    body = (((raw.get("body") or {}).get("storage") or {}).get("value")) or ""
    labels = [l.get("name") for l in
              (((raw.get("metadata") or {}).get("labels") or {}).get("results", []))
              if l.get("name")]
    webui = ((raw.get("_links") or {}).get("webui")) or ""
    base = client.base_url if (client and webui.startswith("/")) else ""
    url = (base + webui) if webui else None
    when = ((raw.get("version") or {}).get("when")) or ""
    return {
        "id": pid,
        "title": raw.get("title"),
        "body": body,
        "url": url,
        "labels": labels,
        "acl_tags": client.read_acl_tags(pid) if pid else [],
        "last_modified": when,
    }


def build_cql(space_key: str, labels=None, since: str | None = None) -> str:
    """Assemble the CQL for one space: pages only, optional label and recency
    filters. ``since`` is a CQL datetime string, e.g. ``"2026/06/01 00:00"``."""
    clauses = [f'space = "{space_key}"', "type = page"]
    for lab in (labels or []):
        clauses.append(f'label = "{lab}"')
    if since:
        clauses.append(f'lastmodified >= "{since}"')
    return " AND ".join(clauses) + " ORDER BY lastmodified ASC"


def sync_space(space_key: str, namespace: str, since: str | None = None,
               labels=None, client: "ConfluenceClient | None" = None,
               page_size: int = 25) -> dict:
    """Incremental sync of ONE space into ``namespace``.

    Pages through CQL, resolves each page's read restrictions to ``acl:`` tags,
    chunks the body, and stores each chunk with a ``src:`` back-link. Returns a
    summary including ``checkpoint`` (the max ``lastmodified`` seen) — pass it
    back as ``since`` next run for incremental re-sync.
    """
    client = client or ConfluenceClient()
    cql = build_cql(space_key, labels=labels, since=since)
    expand = "body.storage,version,metadata.labels"
    pages = chunks = 0
    checkpoint = since
    for raw in client.iter_search(cql, expand, limit=page_size):
        page = _normalize(raw, client)
        chunks += ingest_page(page, namespace, space_label=space_key)
        pages += 1
        if page["last_modified"] and (checkpoint is None
                                      or page["last_modified"] > checkpoint):
            checkpoint = page["last_modified"]
    return {"space": space_key, "namespace": namespace,
            "pages": pages, "chunks": chunks, "checkpoint": checkpoint}
