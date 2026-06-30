"""IntelliJ connector: commit + TODO ingest, ticket-key bridge, ACL, mocked sync."""
from __future__ import annotations

from teambrain import store as _store
from teambrain.connectors import intellij


def _commit(sha, subject, body="", branch="main", author="dev"):
    return {"sha": sha, "subject": subject, "body": body, "branch": branch,
            "author": author, "date": "2026-06-20T00:00:00+00:00"}


def test_ticket_keys_from_branch_and_message():
    assert intellij.ticket_keys("feature/ABC-123-login", "fix ABC-123 and DEF-9") \
        == ["ABC-123", "DEF-9"]
    assert intellij.ticket_keys("no ticket here") == []


def test_ingest_commit_links_ticket_and_branch(temp_store):
    n = intellij.ingest_commit(
        _commit("abcdef1234", "Enforce 3-retry login cap", branch="feature/ABC-123-login"),
        "team-eng", repo="shop")
    assert n >= 1
    row = temp_store.search("retry login cap", namespace="team-eng", mode="bm25")[0]
    tags = _store._tags_of(row)
    assert "intellij" in tags and "commit" in tags
    assert "repo:shop" in tags
    assert "ticket:ABC-123" in tags          # the PO<->dev bridge
    assert "branch:feature/ABC-123-login" in tags
    assert _store.visible_to(row, None)      # public within namespace


def test_ingest_commit_web_base_backlink(temp_store):
    intellij.ingest_commit(_commit("deadbeef99", "Tweak pricing"), "team-eng",
                           repo="shop", web_base="https://gl.acme.com/group/shop/")
    row = temp_store.search("Tweak pricing", namespace="team-eng", mode="bm25")[0]
    assert _store.source_url_of(row) == "https://gl.acme.com/group/shop/commit/deadbeef99"


def test_ingest_todo_backlink_and_acl(temp_store):
    n = intellij.ingest_todo(
        {"file": "src/Main.java", "line": 42, "marker": "FIXME",
         "text": "handle ABC-7 refund edge case"},
        "team-eng", repo="shop", acl_groups=["team-shop"])
    assert n == 1
    row = temp_store.search("refund edge case", namespace="team-eng", mode="bm25")[0]
    tags = _store._tags_of(row)
    assert "todo" in tags and "marker:FIXME" in tags
    assert "ticket:ABC-7" in tags
    assert _store.source_url_of(row) == "src/Main.java#L42"
    assert not _store.visible_to(row, None)            # restricted, fail-closed
    assert _store.visible_to(row, ["team-shop"])


class FakeRepo:
    """Stands in for GitRepo — no real git, no filesystem."""
    path = "/home/dev/shop"

    def current_branch(self):
        return "feature/ABC-123-login"

    def log(self, since=None, max_count=500, branch=None):
        yield _commit("aaa1111", "Add login cap", branch="feature/ABC-123-login")
        yield _commit("bbb2222", "", branch="feature/ABC-123-login")  # empty -> skipped
        yield _commit("ccc3333", "Refactor pricing", branch="feature/ABC-123-login")

    def tracked_files(self):
        return ["src/Main.java", "README.md", "src/util.py"]

    def file_lines(self, rel):
        return {
            "src/Main.java": ["class Main {", "  // TODO ABC-9 cache the result", "}"],
            "src/util.py": ["# FIXME drop the legacy path", "x = 1"],
            "README.md": ["# docs"],
        }.get(rel, [])


def test_scan_todos_only_source_files():
    todos = list(intellij.scan_todos(FakeRepo()))
    files = {t["file"] for t in todos}
    assert files == {"src/Main.java", "src/util.py"}     # README skipped (not source)
    java = next(t for t in todos if t["file"] == "src/Main.java")
    assert java["marker"] == "TODO" and java["line"] == 2
    assert "cache the result" in java["text"]


def test_sync_end_to_end(temp_store):
    summary = intellij.sync("/home/dev/shop", "team-eng", repo="shop", git=FakeRepo())
    assert summary["commits"] == 2                       # empty-message commit skipped
    assert summary["todos"] == 2
    assert summary["branch"] == "feature/ABC-123-login"
    assert summary["checkpoint"] == "2026-06-20T00:00:00+00:00"

    hit = temp_store.search("cache the result", namespace="team-eng", mode="bm25")[0]
    assert "ticket:ABC-9" in _store._tags_of(hit)
