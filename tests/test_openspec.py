"""OpenSpec connector: doc classification, ticket-key bridge, ACL, tmp-dir sync."""
from __future__ import annotations

import pytest

from teambrain import store as _store
from teambrain.connectors import openspec


def _mk(root, rel, text):
    fp = root / rel
    fp.parent.mkdir(parents=True, exist_ok=True)
    fp.write_text(text, encoding="utf-8")


@pytest.fixture
def repo(tmp_path):
    """A minimal OpenSpec tree: one active change, one archived, current specs."""
    os = tmp_path / "openspec"
    _mk(os, "project.md", "Conventions: python, offline tests.")
    _mk(os, "specs/settlement/spec.md",
        "## Matching\nWHEN a message arrives from Bank A THEN match by comment transfer-id.")
    _mk(os, "changes/PROSET-9913-finalize/proposal.md",
        "## Why\nAuto-finalize type 2/3 cash transfers on settlement messages.")
    _mk(os, "changes/PROSET-9913-finalize/design.md",
        "Reuse the type-8 pipeline; match on the counterparty account.")
    _mk(os, "changes/PROSET-9913-finalize/specs/settlement/spec.md",
        "WHEN Bank B or C THEN resolve via clearing-route lookup.")
    _mk(os, "changes/PROSET-9913-finalize/tasks.md", "- [ ] 1.1 add resolver")
    _mk(os, "changes/archive/2026-05-01-add-type8/proposal.md",
        "Original type-8 finalize flow for ABC-77.")
    _mk(os, "changes/README.md", "not a change document")
    return tmp_path


def test_classify():
    assert openspec._classify(["project.md"]) == ("project", "", False)
    assert openspec._classify(["specs", "pay", "spec.md"]) == ("spec", "", False)
    assert openspec._classify(["changes", "add-x", "proposal.md"]) == ("proposal", "add-x", False)
    assert openspec._classify(["changes", "add-x", "design.md"]) == ("design", "add-x", False)
    assert openspec._classify(["changes", "add-x", "specs", "pay", "spec.md"]) == ("spec", "add-x", False)
    assert openspec._classify(["changes", "add-x", "tasks.md"])[0] is None
    assert openspec._classify(["changes", "archive", "2026-old", "proposal.md"]) == ("proposal", "2026-old", True)
    assert openspec._classify(["changes", "README.md"])[0] is None
    assert openspec._classify(["changes", "add-x", "notes.txt"])[0] is None


def test_sync_end_to_end(temp_store, repo):
    summary = openspec.sync(str(repo), "team-eng", repo="shop")
    assert summary["docs"] == 6            # tasks.md + README.md skipped
    assert summary["changes"] == 2         # active + archived

    row = temp_store.search("counterparty account", namespace="team-eng", mode="bm25")[0]
    tags = _store._tags_of(row)
    assert "openspec" in tags and "design" in tags
    assert "repo:shop" in tags
    assert "change:PROSET-9913-finalize" in tags
    assert "ticket:PROSET-9913" in tags    # the bridge to commits + captured chats
    assert row.get("tier") == "procedural"
    assert _store.source_url_of(row) == "openspec/changes/PROSET-9913-finalize/design.md"
    assert _store.visible_to(row, None)    # public within namespace

    prop = temp_store.search("auto-finalize", namespace="team-eng", mode="bm25")[0]
    assert prop.get("tier") == "semantic"

    old = temp_store.search("type-8 finalize flow", namespace="team-eng", mode="bm25")[0]
    old_tags = _store._tags_of(old)
    assert "archived" in old_tags and "ticket:ABC-77" in old_tags

    assert not temp_store.search("add resolver", namespace="team-eng", mode="bm25")


def test_sync_acl_and_web_base(temp_store, repo):
    openspec.sync(str(repo), "team-eng", repo="shop",
                  web_base="https://gl.acme.com/group/shop/-/blob/main/",
                  acl_groups=["team-shop"])
    row = temp_store.search("clearing-route lookup", namespace="team-eng", mode="bm25")[0]
    assert _store.source_url_of(row) == (
        "https://gl.acme.com/group/shop/-/blob/main/"
        "openspec/changes/PROSET-9913-finalize/specs/settlement/spec.md")
    assert not _store.visible_to(row, None)          # restricted, fail-closed
    assert _store.visible_to(row, ["team-shop"])


def test_sync_skip_archive(temp_store, repo):
    summary = openspec.sync(str(repo), "team-eng", include_archive=False)
    assert summary["docs"] == 5
    assert not temp_store.search("type-8 finalize flow", namespace="team-eng", mode="bm25")


def test_sync_missing_dir(tmp_path):
    with pytest.raises(FileNotFoundError):
        openspec.sync(str(tmp_path / "nowhere"), "team-eng")
