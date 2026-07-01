"""demo sweep: repo discovery, local business mining, per-repo error isolation."""
from __future__ import annotations

import pytest

from teambrain import demo, store as _store
from teambrain.connectors import intellij


def _mk_repo(base, name, git=True):
    d = base / name
    (d / (".git" if git else "nogit")).mkdir(parents=True)
    return d


def test_find_repos(tmp_path):
    _mk_repo(tmp_path, "alpha")
    _mk_repo(tmp_path, "group/beta")
    _mk_repo(tmp_path, "plain", git=False)
    (tmp_path / ".hidden").mkdir()
    _mk_repo(tmp_path, ".hidden/secret")
    nested = _mk_repo(tmp_path, "alpha/vendored")          # inside a repo: skipped
    assert nested
    found = [p.rsplit("/", 1)[-1] for p in demo.find_repos(str(tmp_path))]
    assert found == ["alpha", "beta"]
    # pointing at a repo itself returns just it
    assert demo.find_repos(str(tmp_path / "alpha")) == [str(tmp_path / "alpha")]


class FakeGit:
    """Local file access without real git (same seam as intellij tests)."""
    def __init__(self, files):
        self.files = files

    def tracked_files(self):
        return list(self.files)

    def file_lines(self, rel):
        return self.files[rel]


def test_mine_business_local(temp_store):
    git = FakeGit({
        "src/Settle.java": ["class Settle {", "// finalize type 2 transfers", "}"],
        "docs/readme.md": ["not code"],                    # wrong ext: skipped
        "tests/T.java": ["@Test"],                         # test dir: skipped
    })
    summarize = lambda code, path: "Type-2 transfers are finalized on settlement."
    res = demo.mine_business("/x", "team-eng", "shop", git=git, summarize=summarize)
    assert res == {"tried": 1, "mined": 1, "chunks": 1}
    row = temp_store.search("finalized on settlement", namespace="team-eng", mode="bm25")[0]
    assert "repo:shop" in _store._tags_of(row)


def test_mine_business_cap(temp_store):
    git = FakeGit({f"src/F{i}.py": ["x = 1"] for i in range(10)})
    res = demo.mine_business("/x", "team-eng", "shop", max_files=3, git=git,
                             summarize=lambda c, p: "A business rule.")
    assert res["tried"] == 3


def test_ingest_repo_isolates_errors(tmp_path, temp_store, monkeypatch):
    repo = _mk_repo(tmp_path, "shop")
    monkeypatch.setattr(intellij, "sync",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    res = demo.ingest_repo(str(repo), "team-eng")
    assert res["commits"] == 0 and any("git: boom" in e for e in res["errors"])
    assert res["mined"] == 0                               # mining skipped, not crashed


def test_main_end_to_end(tmp_path, temp_store, monkeypatch, capsys):
    _mk_repo(tmp_path, "alpha")
    _mk_repo(tmp_path, "beta")
    monkeypatch.setattr(
        intellij, "sync",
        lambda path, ns, repo=None, **k: {"commits": 2, "todos": 1})
    monkeypatch.setattr(demo, "mine_business",
                        lambda *a, **k: {"tried": 1, "mined": 1, "chunks": 1})
    rc = demo.main([str(tmp_path), "--namespace", "team-eng"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "2 repo(s)" in out and "4 commits" in out and "2 mined files" in out
    assert "team_assist" in out                            # try-it hints printed


def test_main_no_repos(tmp_path, capsys):
    assert demo.main([str(tmp_path)]) == 1
    assert "no git repos" in capsys.readouterr().out
