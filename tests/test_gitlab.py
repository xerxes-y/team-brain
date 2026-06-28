"""GitLab connector + code business-extraction: selection, ACL, tree paging,
the heuristic summarizer, and a full mocked sync into PO-searchable memories."""
from __future__ import annotations

from teambrain import store as _store, code_summary
from teambrain.connectors import gitlab


# ── file selection ────────────────────────────────────────────────────────────

def test_selection_by_ext_and_skipdir():
    exts = gitlab.CODE_EXTS
    assert gitlab._selected("app/billing.py", exts)
    assert gitlab._selected("src/Refund.java", exts)
    assert not gitlab._selected("node_modules/x/index.js", exts)
    assert not gitlab._selected("tests/test_billing.py", exts)
    assert not gitlab._selected("README.md", exts)         # md not a code ext
    assert not gitlab._selected("Makefile", exts)           # no ext


# ── heuristic summarizer (offline, no LLM) ────────────────────────────────────

def test_heuristic_extracts_business_signal():
    code = '''
def approve_refund(amount, role):
    """Refunds over 500 require a manager."""
    # business rule: cap auto-approval at 500
    if amount > 500 and role != "manager":
        raise PermissionError("Refund over limit requires manager approval")
'''
    out = code_summary._heuristic(code, "billing/refund.py")
    assert "billing/refund.py" in out
    assert "manager" in out.lower()
    assert "approve_refund" in out


def test_heuristic_empty_for_noise():
    assert code_summary.summarize("x = 1\ny = 2\n", "config/x.py") == ""


# ── ACL by project visibility ─────────────────────────────────────────────────

class FakeGitLab:
    web_base = "https://gitlab.com"

    def __init__(self, files, visibility="public"):
        self._files = files            # {path: source}
        self._visibility = visibility

    def visibility(self, project):
        return self._visibility

    def iter_tree(self, project, ref="HEAD", per_page=100):
        for path in self._files:
            yield {"path": path, "type": "blob"}

    def file_raw(self, project, file_path, ref="HEAD"):
        return self._files[file_path]


# deterministic summarizer for the sync test — no LLM, no heuristic ambiguity
def _fake_summary(code, path):
    return "" if "noise" in path else f"Business rules in {path}: {code.strip()}"


def test_sync_public_project(temp_store):
    files = {
        "billing/refund.py": "refunds over 500 need a manager",
        "config/noise.py": "x=1",                       # summarizer returns "" -> skipped
        "node_modules/lib.js": "ignored",              # filtered by selection
    }
    client = FakeGitLab(files, visibility="public")
    summary = gitlab.sync_project("group/shop", "team-eng",
                                  client=client, summarize=_fake_summary)

    assert summary["files_seen"] == 2          # node_modules filtered out
    assert summary["files_indexed"] == 1       # noise.py produced no business signal
    assert summary["visibility"] == "public"

    row = temp_store.search("refunds manager", namespace="team-eng", mode="bm25")[0]
    tags = _store._tags_of(row)
    assert "business" in tags and "gitlab" in tags
    assert "repo:group/shop" in tags
    assert _store.source_url_of(row) == \
        "https://gitlab.com/group/shop/-/blob/HEAD/billing/refund.py"
    assert _store.visible_to(row, None)        # public project => public


def test_sync_private_project_is_acl_gated(temp_store):
    client = FakeGitLab({"core/pricing.py": "enterprise tier is 20pct off"},
                        visibility="private")
    gitlab.sync_project("group/secret", "team-eng",
                        client=client, summarize=_fake_summary)
    row = temp_store.search("enterprise tier", namespace="team-eng", mode="bm25")[0]
    assert "acl:repo:group/secret" in _store._tags_of(row)
    assert not _store.visible_to(row, None)                 # fail closed
    assert _store.visible_to(row, ["repo:group/secret"])


def test_max_files_cap(temp_store):
    files = {f"src/m{i}.py": f"rule {i}" for i in range(5)}
    client = FakeGitLab(files, visibility="public")
    summary = gitlab.sync_project("g/p", "team-eng", client=client,
                                  max_files=2, summarize=_fake_summary)
    assert summary["files_seen"] == 2
    assert summary["files_skipped_over_cap"] == 3


# ── client tree paging ────────────────────────────────────────────────────────

def test_iter_tree_paging(monkeypatch):
    c = gitlab.GitLabClient(token="t")
    pages = {1: [{"path": f"f{i}.py", "type": "blob"} for i in range(100)],
             2: [{"path": "last.py", "type": "blob"}, {"path": "d", "type": "tree"}]}

    def fake_get(path, params=None):
        return pages.get(params["page"], [])

    monkeypatch.setattr(c, "get_json", fake_get)
    paths = [e["path"] for e in c.iter_tree("g/p")]
    assert len(paths) == 101                  # blobs only; the 'tree' entry dropped
    assert "last.py" in paths


def test_web_base_derivation():
    c = gitlab.GitLabClient(token="t", base_url="https://gl.acme.com/api/v4")
    assert c.web_base == "https://gl.acme.com"
