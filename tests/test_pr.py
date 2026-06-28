"""PR (GitHub) connector: merged-only ingest, repo ACL, paging, mocked sync."""
from __future__ import annotations

from teambrain import store as _store
from teambrain.connectors import pr


def _pr(num, title, merged=True, private=False, body="why we did it",
        updated="2026-06-20T00:00:00Z", repo="acme/app"):
    return {
        "number": num, "title": title, "body": body,
        "merged_at": "2026-06-20T00:00:00Z" if merged else None,
        "updated_at": updated,
        "user": {"login": "dev"},
        "labels": [{"name": "feature"}],
        "html_url": f"https://github.com/{repo}/pull/{num}",
        "base": {"ref": "main", "repo": {"full_name": repo, "private": private}},
    }


def test_repo_acl_tags_private_vs_public():
    assert pr.repo_acl_tags(_pr(1, "x", private=True)) == ["acl:repo:acme/app"]
    assert pr.repo_acl_tags(_pr(1, "x", private=False)) == []


def test_unmerged_pr_skipped(temp_store):
    assert pr.ingest_pr(_pr(7, "draft", merged=False), "team-eng") == 0


def test_ingest_merged_pr(temp_store):
    n = pr.ingest_pr(_pr(42, "Add JWT auth"), "team-eng", repo="acme/app")
    assert n >= 1
    row = temp_store.search("JWT auth", namespace="team-eng", mode="bm25")[0]
    tags = _store._tags_of(row)
    assert "pr" in tags and "decision" in tags
    assert "repo:acme/app" in tags
    assert _store.source_url_of(row) == "https://github.com/acme/app/pull/42"
    assert _store.visible_to(row, None)  # public repo → public


def test_iter_pulls_pages(monkeypatch):
    c = pr.GitHubClient(token="t")
    pages = {1: [{"id": i} for i in range(100)], 2: [{"id": 100}]}

    def fake_get(path, params=None):
        return pages.get(params["page"], [])

    monkeypatch.setattr(c, "get_json", fake_get)
    ids = [p["id"] for p in c.iter_pulls("acme/app")]
    assert len(ids) == 101  # full first page (100) + short second page (1)


class FakeGH:
    def iter_pulls(self, repo, state="closed", per_page=100):
        yield _pr(1, "Add JWT auth", repo=repo)
        yield _pr(2, "Draft", merged=False, repo=repo)
        yield _pr(3, "Private fix", private=True, repo=repo,
                  updated="2026-06-21T00:00:00Z")


def test_sync_repo_end_to_end(temp_store):
    summary = pr.sync_repo("acme/app", "team-eng", client=FakeGH())
    assert summary["prs"] == 2  # unmerged #2 skipped
    assert summary["checkpoint"] == "2026-06-21T00:00:00Z"

    priv = temp_store.search("Private fix", namespace="team-eng", mode="bm25")[0]
    assert "acl:repo:acme/app" in _store._tags_of(priv)
    assert not _store.visible_to(priv, None)
    assert _store.visible_to(priv, ["repo:acme/app"])


def test_sync_repo_since_filter(temp_store):
    summary = pr.sync_repo("acme/app", "team-eng", since="2026-06-21T00:00:00Z",
                           client=FakeGH())
    # only the private fix updated at the cutoff survives the since filter
    assert summary["prs"] == 1
