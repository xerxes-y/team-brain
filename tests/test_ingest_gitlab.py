"""CLI for company-GitLab ingest: arg handling, public override, multi-project."""
from __future__ import annotations

from teambrain import ingest_gitlab
from teambrain.connectors import gitlab


def test_public_override_drops_acl(temp_store, monkeypatch):
    monkeypatch.setenv("GITLAB_BASE_URL", "https://gl.acme.com/api/v4")
    monkeypatch.setenv("GITLAB_TOKEN", "t")

    class FakeGL:
        web_base = "https://gl.acme.com"
        def visibility(self, project): return "private"
        def iter_tree(self, project, ref="HEAD", per_page=100):
            yield {"path": "app/pricing.py", "type": "blob"}
        def file_raw(self, project, fp, ref="HEAD"):
            return "# enterprise tier gets 20 percent discount"

    monkeypatch.setattr(gitlab, "GitLabClient", lambda *a, **k: FakeGL())

    rc = ingest_gitlab.main(["group/shop", "--namespace", "team-eng", "--public"])
    assert rc == 0
    from teambrain import store as _store
    row = temp_store.search("enterprise discount", namespace="team-eng", mode="bm25")[0]
    assert not any(t.startswith("acl:") for t in _store._tags_of(row))   # --public => ungated
    assert _store.visible_to(row, None)


def test_private_default_is_gated(temp_store, monkeypatch):
    monkeypatch.setenv("GITLAB_BASE_URL", "https://gl.acme.com/api/v4")
    monkeypatch.setenv("GITLAB_TOKEN", "t")

    class FakeGL:
        web_base = "https://gl.acme.com"
        def visibility(self, project): return "private"
        def iter_tree(self, project, ref="HEAD", per_page=100):
            yield {"path": "core/auth.py", "type": "blob"}
        def file_raw(self, project, fp, ref="HEAD"):
            return "# sessions expire after fifteen minutes idle"

    monkeypatch.setattr(gitlab, "GitLabClient", lambda *a, **k: FakeGL())
    ingest_gitlab.main(["group/secret", "--namespace", "team-eng"])
    from teambrain import store as _store
    row = temp_store.search("sessions idle", namespace="team-eng", mode="bm25")[0]
    assert "acl:repo:group/secret" in _store._tags_of(row)
    assert not _store.visible_to(row, None)
    assert _store.visible_to(row, ["repo:group/secret"])


def test_tokenless_client_allowed(monkeypatch):
    # GitLab read is allowed without a token (public projects) — unlike Confluence/
    # Jira, the client does NOT require credentials. Defaults to gitlab.com.
    monkeypatch.delenv("GITLAB_BASE_URL", raising=False)
    monkeypatch.delenv("GITLAB_TOKEN", raising=False)
    c = gitlab.GitLabClient()
    assert c.token is None
    assert c.base_url.endswith("/api/v4")
