"""Confluence connector: chunking, ACL mapping, CQL, and a full mocked sync.

The sync test drives ``sync_space`` with a FakeClient (no network) to prove the
data flow end to end: CQL paging -> restriction resolution -> chunk -> store
with src: back-link, and that restricted pages land with acl: tags so the read
path will gate them.
"""
from __future__ import annotations

from teambrain import store as _store
from teambrain.connectors import confluence as cf


# ── chunking ──────────────────────────────────────────────────────────────────

def test_chunk_storage_format_splits_on_headings():
    html = ("<h1>Auth</h1><p>We use JWT.</p>"
            "<h2>Why</h2><p>Stateless &amp; simple.</p>")
    chunks = list(cf.chunk_storage_format(html))
    headings = [h for h, _ in chunks]
    assert "Auth" in headings and "Why" in headings
    bodies = " ".join(b for _, b in chunks)
    assert "JWT" in bodies
    assert "&amp;" not in bodies and "&" in bodies  # entities unescaped
    assert "<" not in bodies                          # tags stripped


def test_chunk_page_sniffs_html_vs_markdown():
    assert list(cf.chunk_page("<p>hi there</p>"))      # html path
    md = list(cf.chunk_page("# Title\n\nbody text"))
    assert md and md[0][0] == "Title"


def test_long_section_is_subsplit():
    html = "<h1>Big</h1><p>" + ("x" * 4000) + "</p>"
    chunks = list(cf.chunk_storage_format(html, max_chars=1500))
    assert len(chunks) >= 3
    assert all(len(b) <= 1500 for _, b in chunks)


# ── ACL mapping (fail closed) ─────────────────────────────────────────────────

def test_restriction_tags_groups_and_users():
    tags = cf.restriction_tags({"read_groups": ["hr"], "read_users": ["acc-1"]})
    assert "acl:hr" in tags
    assert "acl:user:acc-1" in tags


def test_user_only_restriction_is_not_public():
    # A page restricted to a single user must NOT come out public.
    tags = cf.restriction_tags({"read_users": ["acc-1"]})
    assert tags == ["acl:user:acc-1"]


def test_no_restriction_is_public():
    assert cf.restriction_tags({}) == []


# ── CQL ───────────────────────────────────────────────────────────────────────

def test_build_cql_space_label_recency():
    cql = cf.build_cql("ENG", labels=["arch"], since="2026/06/01 00:00")
    assert 'space = "ENG"' in cql and "type = page" in cql
    assert 'label = "arch"' in cql
    assert 'lastmodified >= "2026/06/01 00:00"' in cql
    assert cql.endswith("ORDER BY lastmodified ASC")


# ── full mocked sync ──────────────────────────────────────────────────────────

class FakeClient:
    """Stand-in for ConfluenceClient: serves canned content + restrictions and
    paginates via _links.next so iter_search's paging is exercised."""

    base_url = "https://acme.atlassian.net/wiki"

    def __init__(self, pages, restrictions):
        self._pages = pages              # list of raw API content objects
        self._restr = restrictions       # page_id -> list[acl tag]

    # reuse the real paging + normalize logic; only get_json is faked
    def get_json(self, url_or_path, params=None):
        if "/restriction/" in url_or_path:
            return {}  # unused; read_acl_tags is overridden below
        raise AssertionError("unexpected get_json call")

    def iter_search(self, cql, expand, limit=25):
        # two pages of one item each, to exercise the next-link loop
        first, rest = self._pages[:1], self._pages[1:]
        for r in first:
            yield r
        for r in rest:
            yield r

    def read_acl_tags(self, page_id):
        return list(self._restr.get(page_id, []))


def _raw(pid, title, body, labels=()):
    return {
        "id": pid, "title": title,
        "body": {"storage": {"value": body}},
        "metadata": {"labels": {"results": [{"name": l} for l in labels]}},
        "version": {"when": f"2026-06-2{pid}T00:00:00.000Z"},
        "_links": {"webui": f"/spaces/ENG/pages/{pid}/{title.replace(' ', '+')}"},
    }


def test_client_iter_search_follows_next_link(monkeypatch):
    c = cf.ConfluenceClient(base_url="https://acme.atlassian.net/wiki",
                            token="t", email="e@x.com")
    calls = []

    def fake_get(url_or_path, params=None):
        calls.append(url_or_path)
        if "cursor=PAGE2" in url_or_path:
            return {"results": [{"id": "2"}], "_links": {}}
        return {"results": [{"id": "1"}],
                "_links": {"base": "https://acme.atlassian.net/wiki",
                           "next": "/rest/api/content/search?cursor=PAGE2"}}

    monkeypatch.setattr(c, "get_json", fake_get)
    ids = [r["id"] for r in c.iter_search("space=ENG", "body.storage")]
    assert ids == ["1", "2"]                 # paged across the next link
    assert any("cursor=PAGE2" in u for u in calls)


def test_client_read_acl_tags_parsing(monkeypatch):
    c = cf.ConfluenceClient(base_url="https://acme.atlassian.net/wiki", token="t")

    def fake_get(url_or_path, params=None):
        return {"restrictions": {
            "group": {"results": [{"name": "hr"}]},
            "user": {"results": [{"accountId": "acc-1"}]}}}

    monkeypatch.setattr(c, "get_json", fake_get)
    tags = c.read_acl_tags("99")
    assert set(tags) == {"acl:hr", "acl:user:acc-1"}


def test_sync_space_end_to_end(temp_store):
    pages = [
        _raw("1", "Auth Design", "<h1>Auth</h1><p>We chose JWT in PR 42.</p>",
             labels=["arch"]),
        _raw("2", "Comp Bands", "<h1>Salary</h1><p>Confidential ranges.</p>"),
    ]
    client = FakeClient(pages, restrictions={"2": ["acl:hr"]})

    summary = cf.sync_space("ENG", namespace="team-eng", client=client)

    assert summary["pages"] == 2
    assert summary["chunks"] >= 2
    assert summary["checkpoint"] == "2026-06-22T00:00:00.000Z"  # max lastmodified

    rows = temp_store.search("JWT", namespace="team-eng", mode="bm25")
    assert rows, "public page should be retrievable"
    pub = rows[0]
    assert "confluence" in _store._tags_of(pub)
    assert "space:ENG" in _store._tags_of(pub)
    assert "label:arch" in _store._tags_of(pub)
    # back-link recovered for citations
    assert _store.source_url_of(pub).endswith("/spaces/ENG/pages/1/Auth+Design")
    # public page is visible to an anonymous asker
    assert _store.visible_to(pub, None)

    restricted = temp_store.search("Salary", namespace="team-eng", mode="bm25")
    assert restricted
    sal = restricted[0]
    assert "acl:hr" in _store._tags_of(sal)
    assert not _store.visible_to(sal, None)        # fail closed for anon
    assert not _store.visible_to(sal, ["eng"])     # wrong group denied
    assert _store.visible_to(sal, ["hr"])          # hr allowed
