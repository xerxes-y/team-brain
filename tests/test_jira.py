"""Jira connector: ADF flattening, security ACL, JQL, paging, and a mocked sync."""
from __future__ import annotations

from teambrain import store as _store
from teambrain.connectors import jira


# ── ADF / text ────────────────────────────────────────────────────────────────

def test_adf_to_text_flattens():
    adf = {"type": "doc", "content": [
        {"type": "paragraph", "content": [{"type": "text", "text": "Hello "},
                                          {"type": "text", "text": "world"}]},
        {"type": "bulletList", "content": [
            {"type": "listItem", "content": [
                {"type": "paragraph", "content": [{"type": "text", "text": "item"}]}]}]},
    ]}
    out = jira.text_of(adf)
    assert "Hello world" in out
    assert "item" in out


def test_text_of_handles_plain_string():
    assert jira.text_of("just text") == "just text"


# ── security ACL ──────────────────────────────────────────────────────────────

def test_security_acl_tag():
    issue = {"fields": {"security": {"name": "Internal Only"}}}
    assert jira.security_acl_tags(issue) == ["acl:jira-sec:Internal-Only"]


def test_no_security_is_public():
    assert jira.security_acl_tags({"fields": {}}) == []


# ── JQL ───────────────────────────────────────────────────────────────────────

def test_build_jql():
    q = jira.build_jql("ENG", since="2026/06/01 00:00")
    assert 'project = "ENG"' in q
    assert 'updated >= "2026/06/01 00:00"' in q
    assert q.endswith("ORDER BY updated ASC")


# ── paging (both modes) ───────────────────────────────────────────────────────

def test_iter_issues_startat_paging(monkeypatch):
    c = jira.JiraClient(base_url="https://acme.atlassian.net", token="t", email="e@x.com")
    pages = [
        {"issues": [{"key": "E-1"}, {"key": "E-2"}], "startAt": 0, "maxResults": 2, "total": 3},
        {"issues": [{"key": "E-3"}], "startAt": 2, "maxResults": 2, "total": 3},
    ]
    calls = []

    def fake_get(path, params=None):
        calls.append(params["startAt"])
        return pages[len(calls) - 1]

    monkeypatch.setattr(c, "get_json", fake_get)
    keys = [i["key"] for i in c.iter_issues("project=ENG", "summary", page_size=2)]
    assert keys == ["E-1", "E-2", "E-3"]
    assert calls == [0, 2]


def test_iter_issues_token_paging(monkeypatch):
    c = jira.JiraClient(base_url="https://acme.atlassian.net", token="t")
    pages = [
        {"issues": [{"key": "E-1"}], "nextPageToken": "tok", "isLast": False},
        {"issues": [{"key": "E-2"}], "isLast": True},
    ]

    def fake_get(path, params=None):
        return pages[0] if "nextPageToken" not in params else pages[1]

    monkeypatch.setattr(c, "get_json", fake_get)
    keys = [i["key"] for i in c.iter_issues("project=ENG", "summary")]
    assert keys == ["E-1", "E-2"]


# ── full mocked sync ──────────────────────────────────────────────────────────

class FakeJira:
    base_url = "https://acme.atlassian.net"

    def __init__(self, issues):
        self._issues = issues

    def iter_issues(self, jql, fields, page_size=50):
        yield from self._issues


def _issue(key, summary, itype="Bug", status="Open", security=None,
           comments=(), updated="2026-06-20T00:00:00.000+0000"):
    f = {"summary": summary, "issuetype": {"name": itype},
         "status": {"name": status}, "description": f"Repro for {summary}.",
         "labels": ["backend"], "updated": updated,
         "comment": {"comments": [{"author": {"displayName": a}, "body": b}
                                  for a, b in comments]}}
    if security:
        f["security"] = {"name": security}
    return {"key": key, "fields": f}


def test_sync_project_end_to_end(temp_store):
    issues = [
        _issue("ENG-1", "Login fails", comments=[("Dev", "Fixed in build 12")]),
        _issue("ENG-2", "Secret roadmap", itype="Story", status="In Progress",
               security="Confidential", updated="2026-06-22T00:00:00.000+0000"),
    ]
    summary = jira.sync_project("ENG", "team-eng", client=FakeJira(issues))

    assert summary["issues"] == 2
    assert summary["checkpoint"] == "2026-06-22T00:00:00.000+0000"

    bug = temp_store.search("login fails", namespace="team-eng", mode="bm25")[0]
    tags = _store._tags_of(bug)
    assert "jira" in tags and "bug" in tags
    assert "project:ENG" in tags
    assert _store.source_url_of(bug) == "https://acme.atlassian.net/browse/ENG-1"
    assert _store.visible_to(bug, None)  # unclassified → public

    # the discussion thread became its own episodic memory
    disc = temp_store.search("Fixed in build 12", namespace="team-eng", mode="bm25")[0]
    assert disc.get("tier") == "episodic"

    # the security-classified issue is ACL-gated
    sec = temp_store.search("secret roadmap", namespace="team-eng", mode="bm25")[0]
    assert "acl:jira-sec:Confidential" in _store._tags_of(sec)
    assert not _store.visible_to(sec, None)
    assert _store.visible_to(sec, ["jira-sec:Confidential"])
