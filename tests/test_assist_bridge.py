"""The cross-role bridges: explain_ticket (dev) and test_plan (tester).

Offline (extractive synthesis, SQLite store). We seed both a PO-side memory
(a business rule) and a dev-side memory (a commit) that share a Jira key, then
assert each flow retrieves across the gap and boosts the ticket-linked work.
"""
from __future__ import annotations

from teambrain import assist
from teambrain import store as _store


def _seed(st):
    # PO side — a business rule (what the product should do)
    st.save("Refund policy", "Refunds are allowed within 30 days of purchase.",
            tier="semantic", tags=["business", "gitlab", "ticket:SHOP-42",
                                   "src:https://gl/shop/blob/main/Refund.java"],
            source="gitlab", namespace="team-eng")
    # dev side — a commit that implemented it (how/where in code)
    st.save("[abc1234] Cap refund window", "Committed by dev.\nOn branch SHOP-42.",
            tier="episodic", tags=["intellij", "commit", "ticket:SHOP-42",
                                   "branch:SHOP-42"],
            source="intellij", namespace="team-eng")
    # noise that mentions refunds but is unrelated to the ticket
    st.save("Marketing note", "We should advertise easy refunds in the banner.",
            tier="semantic", tags=["confluence"], source="confluence",
            namespace="team-eng")


def test_explain_ticket_boosts_linked_work(temp_store):
    _seed(temp_store)
    res = assist.explain_ticket("SHOP-42 refund window", "team-eng")
    assert res["tickets"] == ["SHOP-42"]
    titles = [c["title"] for c in res["citations"]]
    # both the business rule and the implementing commit surface, ranked above noise
    assert any("Refund policy" in t for t in titles)
    assert any("Cap refund window" in t for t in titles)
    assert titles[0] != "Marketing note"  # ticket-linked work beats the noise


def test_test_plan_sees_both_sides(temp_store):
    _seed(temp_store)
    res = assist.test_plan("refunds within SHOP-42", "team-eng")
    assert res["tickets"] == ["SHOP-42"]
    sources = {c["title"] for c in res["citations"]}
    # the tester gets the PO's rule AND the dev's commit in one answer
    assert any("Refund policy" in t for t in sources)
    assert any("Cap refund window" in t for t in sources)


def test_explain_ticket_acl_fail_closed(temp_store):
    temp_store.save("Secret pricing", "Enterprise gets 40% off (SHOP-99).",
                    tier="semantic", tags=["business", "ticket:SHOP-99",
                                           "acl:finance"],
                    source="gitlab", namespace="team-eng")
    public = assist.explain_ticket("SHOP-99 pricing", "team-eng")  # unknown asker
    assert public["citations"] == [] and public["hidden_by_acl"] >= 1
    scoped = assist.explain_ticket("SHOP-99 pricing", "team-eng",
                                   asker_groups=["finance"])
    assert any("Secret pricing" in c["title"] for c in scoped["citations"])
