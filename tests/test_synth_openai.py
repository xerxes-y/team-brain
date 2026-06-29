"""Provider-agnostic (OpenAI-compatible) synthesis — no network in tests."""
from __future__ import annotations

from teambrain import synth_openai


_ROWS = [{"title": "Refund window", "content": "Returns within 30 days get a full refund.",
          "tags": ["src:https://x/blob/main/refund.py"]}]
_PROFILE = {"task_prompt": "You help a developer."}


def test_synth_uses_chat(monkeypatch):
    seen = {}
    def fake_chat(system, user, max_tokens=1500):
        seen["system"] = system; seen["user"] = user
        return "Returns are refundable within 30 days [1]."
    monkeypatch.setattr(synth_openai, "_chat", fake_chat)
    out = synth_openai.synth("refund policy", "developer", _PROFILE, _ROWS)
    assert "30 days" in out and "[1]" in out
    assert "SOURCES:" in seen["user"]          # sources passed to the model
    assert "You help a developer." in seen["system"]


def test_synth_falls_back_to_extractive(monkeypatch):
    def boom(*a, **k): raise RuntimeError("no endpoint")
    monkeypatch.setattr(synth_openai, "_chat", boom)
    out = synth_openai.synth("refund policy", "developer", _PROFILE, _ROWS)
    assert "Refund window" in out                 # extractive fallback
    assert "synthesis unavailable" in out


def test_synth_empty_rows():
    assert "No knowledge" in synth_openai.synth("q", "developer", _PROFILE, [])


def test_summarize_code_uses_chat(monkeypatch):
    monkeypatch.setattr(synth_openai, "_chat",
                        lambda s, u, max_tokens=1000: "Refunds over 500 need manager approval.")
    out = synth_openai.summarize_code("def f(): pass", "billing/refund.py")
    assert "manager approval" in out


def test_summarize_code_no_business(monkeypatch):
    monkeypatch.setattr(synth_openai, "_chat", lambda s, u, max_tokens=1000: "NO_BUSINESS_LOGIC")
    assert synth_openai.summarize_code("x=1", "config.py") == ""


def test_summarize_code_falls_back_to_heuristic(monkeypatch):
    def boom(*a, **k): raise RuntimeError("down")
    monkeypatch.setattr(synth_openai, "_chat", boom)
    code = 'def approve_refund():\n    """Refunds over 500 require a manager."""\n    pass'
    out = synth_openai.summarize_code(code, "billing/refund.py")
    assert "billing/refund.py" in out and "manager" in out.lower()
