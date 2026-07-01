"""Extractive-miss gate: weak snippets become an honest 'not covered' answer."""
from __future__ import annotations

from teambrain import assist


def _seed(st):
    st.save("Counterparty rule",
            "Match settlement rows on the counterparty account, never the source.",
            tier="semantic", tags=["openspec"], source="openspec",
            namespace="team-eng")


# Shares exactly one term ("account") with the seeded memory: BM25 *returns*
# the row, but 1 of 8 content terms = 0.125 relevance — under the 0.15 gate.
WEAK_Q = "kubernetes ingress timeout tuning account gateway pods production"


def test_relevance_scoring():
    rows = [{"title": "Counterparty rule",
             "content": "Match settlement rows on the counterparty account."}]
    assert assist._relevance("how do we match settlement rows?", rows) > 0.5
    assert assist._relevance("kubernetes ingress timeout tuning", rows) == 0.0
    assert assist._relevance("???", rows) == 1.0          # no content terms: pass


def test_extractive_miss_returns_not_covered(temp_store, monkeypatch):
    monkeypatch.delenv("TEAMBRAIN_SYNTH", raising=False)
    _seed(temp_store)
    res = assist.assist(WEAK_Q, "developer", "team-eng")
    assert "covers it yet" in res["answer"]
    assert res["citations"] == []                          # no weak citations either


def test_extractive_hit_unaffected(temp_store, monkeypatch):
    monkeypatch.delenv("TEAMBRAIN_SYNTH", raising=False)
    _seed(temp_store)
    res = assist.assist("how do we match settlement rows?", "developer", "team-eng")
    assert "Counterparty rule" in res["answer"]
    assert res["citations"]


def test_gate_disabled_by_env(temp_store, monkeypatch):
    monkeypatch.delenv("TEAMBRAIN_SYNTH", raising=False)
    monkeypatch.setenv("TEAMBRAIN_MIN_RELEVANCE", "0")
    _seed(temp_store)
    res = assist.assist(WEAK_Q, "developer", "team-eng")
    assert res["citations"]                                # old behavior: snippets shown


def test_gate_skipped_when_synth_wired(temp_store, monkeypatch):
    import sys
    import types
    mod = types.ModuleType("fakesynth")
    mod.synth = lambda query, role, profile, rows: "synthesized"
    monkeypatch.setitem(sys.modules, "fakesynth", mod)
    monkeypatch.setenv("TEAMBRAIN_SYNTH", "fakesynth:synth")
    _seed(temp_store)
    res = assist.assist(WEAK_Q, "developer", "team-eng")
    assert res["answer"] == "synthesized"                  # LLM path sees the rows
    assert res["citations"]
