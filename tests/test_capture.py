"""End-of-work chat capture: ticket linking, ACL, chunking, and the distill hook."""
from __future__ import annotations

from teambrain import capture
from teambrain import store as _store


def test_capture_links_ticket_and_role(temp_store):
    res = capture.capture(
        "We decided refunds are capped at 30 days for SHOP-42. Edge case: gift cards never expire.",
        "team-eng", ticket="SHOP-42", role="developer")
    assert res["stored"] >= 1 and res["tickets"] == ["SHOP-42"]
    row = temp_store.search("refunds capped 30 days", namespace="team-eng", mode="bm25")[0]
    tags = _store._tags_of(row)
    assert "capture" in tags and "role:developer" in tags
    assert "ticket:SHOP-42" in tags          # surfaces in explain_ticket / test_plan
    assert _store.visible_to(row, None)       # public within namespace


def test_capture_acl_fail_closed(temp_store):
    capture.capture("Enterprise discount logic is 40% (SHOP-99).", "team-eng",
                    ticket="SHOP-99", groups=["finance"])
    row = temp_store.search("Enterprise discount", namespace="team-eng", mode="bm25")[0]
    assert "acl:finance" in _store._tags_of(row)
    assert not _store.visible_to(row, None)
    assert _store.visible_to(row, ["finance"])


def test_capture_empty_text_is_noop(temp_store):
    assert capture.capture("   ", "team-eng", ticket="SHOP-1")["stored"] == 0


def test_capture_distill_hook(temp_store, monkeypatch):
    def fake_distill(text, context):
        assert context["ticket"] == "SHOP-7"
        return [{"title": "Fact A", "content": "Refunds need a manager.",
                 "tags": ["business"]},
                {"title": "Fact B", "content": "Audit log is mandatory."}]

    monkeypatch.setattr(capture, "_distill", lambda t, c: fake_distill(t, c))
    res = capture.capture("long messy chat...", "team-eng", ticket="SHOP-7")
    assert res["stored"] == 2
    a = temp_store.search("manager", namespace="team-eng", mode="bm25")[0]
    tags = _store._tags_of(a)
    assert "business" in tags and "ticket:SHOP-7" in tags  # per-item + base tags merge
