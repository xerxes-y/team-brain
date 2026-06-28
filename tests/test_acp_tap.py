"""ACP tap recorder: maps ACP JSON-RPC frames to Devin sessions and ingests them.

Synthetic frames per the ACP spec (newline-delimited JSON-RPC 2.0). When we have
a real Devin --record sample these get pinned to its exact shapes."""
from __future__ import annotations

import json

from teambrain import store as _store
from teambrain.connectors import acp_tap
from teambrain.connectors.acp_tap import AcpRecorder, _update_to_turn, HOST_TO_AGENT, AGENT_TO_HOST


def _text(s):
    return [{"type": "text", "text": s}]


# ── update mapping ────────────────────────────────────────────────────────────

def test_find_devin_binary_env_override(tmp_path, monkeypatch):
    fake = tmp_path / "devin"
    fake.write_text("#!/bin/sh\n")
    monkeypatch.setenv("DEVIN_BIN", str(fake))
    assert acp_tap.find_devin_binary() == str(fake)


def test_find_devin_binary_missing_override_falls_through(monkeypatch):
    monkeypatch.setenv("DEVIN_BIN", "/no/such/devin")
    # returns either a real detected path or None — never the bogus override
    assert acp_tap.find_devin_binary() != "/no/such/devin"


def test_update_to_turn_variants():
    assert _update_to_turn({"sessionUpdate": "agent_message_chunk", "content": _text("hi")}) == ("Devin", "hi")
    assert _update_to_turn({"sessionUpdate": "user_message_chunk", "content": _text("yo")}) == ("User", "yo")
    role, text = _update_to_turn({"sessionUpdate": "tool_call", "title": "edit refund.py", "status": "completed"})
    assert role == "Devin" and "edit refund.py" in text and "completed" in text
    assert _update_to_turn({"sessionUpdate": "agent_thought_chunk", "content": _text("thinking")}) == ("Devin", "thinking")
    assert _update_to_turn({"sessionUpdate": "unknown"}) is None


# ── recorder end to end ───────────────────────────────────────────────────────

def test_recorder_session_new_prompt_update_flush(temp_store):
    r = AcpRecorder("team-eng")
    # host opens a session; agent assigns an id
    r.observe(HOST_TO_AGENT, {"id": 1, "method": "session/new",
                              "params": {"cwd": "/work/shop"}})
    r.observe(AGENT_TO_HOST, {"id": 1, "result": {"sessionId": "sess-1"}})
    # user prompt
    r.observe(HOST_TO_AGENT, {"method": "session/prompt",
                              "params": {"sessionId": "sess-1",
                                         "prompt": _text("add rate limiting to the API")}})
    # streamed agent reply (two chunks -> coalesced) + a tool call
    r.observe(AGENT_TO_HOST, {"method": "session/update",
                              "params": {"sessionId": "sess-1",
                                         "update": {"sessionUpdate": "agent_message_chunk",
                                                    "content": _text("Adding a")}}})
    r.observe(AGENT_TO_HOST, {"method": "session/update",
                              "params": {"sessionId": "sess-1",
                                         "update": {"sessionUpdate": "agent_message_chunk",
                                                    "content": _text("token-bucket limiter")}}})
    r.observe(AGENT_TO_HOST, {"method": "session/update",
                              "params": {"sessionId": "sess-1",
                                         "update": {"sessionUpdate": "tool_call",
                                                    "title": "edit middleware.py",
                                                    "status": "completed"}}})
    r.flush()

    row = temp_store.search("rate limiting token bucket", namespace="team-eng", mode="bm25")[0]
    tags = _store._tags_of(row)
    assert "devin" in tags and "session" in tags
    assert "project:shop" in tags                     # cwd from session/new
    assert _store.source_url_of(row) == "devin:sess-1"
    body = row["content"]
    assert "User: add rate limiting" in body
    assert "Devin: Adding a token-bucket limiter" in body   # two chunks coalesced
    assert "[tool] edit middleware.py (completed)" in body


def test_recorder_coalesces_then_splits_on_role_change(temp_store):
    r = AcpRecorder("team-eng")
    r.observe(HOST_TO_AGENT, {"method": "session/prompt",
                              "params": {"sessionId": "s", "prompt": _text("first ask")}})
    r.observe(AGENT_TO_HOST, {"method": "session/update",
                              "params": {"sessionId": "s",
                                         "update": {"sessionUpdate": "agent_message_chunk",
                                                    "content": _text("answer one")}}})
    r.observe(HOST_TO_AGENT, {"method": "session/prompt",
                              "params": {"sessionId": "s", "prompt": _text("second ask")}})
    r.flush()
    row = temp_store.search("first ask second ask", namespace="team-eng", mode="bm25")[0]
    # three distinct turns in order
    assert row["content"].count("User:") == 2
    assert "Devin: answer one" in row["content"]


def test_recorder_acl_scoping(temp_store):
    r = AcpRecorder("team-eng", acl_groups=["platform"])
    r.observe(HOST_TO_AGENT, {"method": "session/prompt",
                              "params": {"sessionId": "s", "prompt": _text("infra secret work")}})
    r.flush()
    row = temp_store.search("infra secret", namespace="team-eng", mode="bm25")[0]
    assert "acl:platform" in _store._tags_of(row)
    assert not _store.visible_to(row, None)
    assert _store.visible_to(row, ["platform"])


def test_recorder_ignores_non_session_frames(temp_store):
    r = AcpRecorder("team-eng")
    r.observe(HOST_TO_AGENT, {"id": 0, "method": "initialize", "params": {}})
    r.observe(AGENT_TO_HOST, {"id": 0, "result": {"protocolVersion": 1}})
    r.observe(HOST_TO_AGENT, {"method": "session/cancel", "params": {"sessionId": "s"}})
    assert r.flush() == 0      # nothing ingestable
    assert not temp_store.search("anything", namespace="team-eng", mode="bm25")


def test_observe_never_raises_on_garbage(temp_store):
    r = AcpRecorder("team-eng")
    for junk in (None, "string", 42, [], {"method": "session/update", "params": "bad"}):
        r.observe(HOST_TO_AGENT, junk)   # must not raise
    assert r.flush() == 0


# ── unix-socket proxy (fallback transport) ────────────────────────────────────

def test_socket_proxy_bridges_and_records(temp_store):
    import socket as _sock
    import threading as _th
    import os as _os

    # AF_UNIX paths are length-capped (~104 on macOS) — keep them short under /tmp
    base = f"/tmp/tbacp{_os.getpid()}"
    upstream = base + "a.sock"
    listen = base + "h.sock"
    for p in (upstream, listen):
        if _os.path.exists(p):
            _os.unlink(p)

    # fake upstream agent: echo each line back, then close on EOF
    up_srv = _sock.socket(_sock.AF_UNIX, _sock.SOCK_STREAM)
    up_srv.bind(upstream); up_srv.listen(1)

    def fake_agent():
        conn, _ = up_srv.accept()
        f = conn.makefile("rb")
        for line in iter(f.readline, b""):
            conn.sendall(line)        # echo back to the proxy (agent->host)
        conn.close()
    ta = _th.Thread(target=fake_agent, daemon=True); ta.start()

    # proxy handles exactly one connection then returns
    tp = _th.Thread(target=acp_tap.run_socket_proxy,
                    args=(listen, upstream, "team-eng"),
                    kwargs={"max_conns": 1}, daemon=True)
    tp.start()

    # wait for the listen socket to appear, then connect as the host
    import time
    for _ in range(100):
        if __import__("os").path.exists(listen):
            break
        time.sleep(0.01)
    host = _sock.socket(_sock.AF_UNIX, _sock.SOCK_STREAM)
    host.connect(listen)
    frame = json.dumps({"method": "session/prompt",
                        "params": {"sessionId": "sk1",
                                   "prompt": _text("socket bridged prompt")}}) + "\n"
    host.sendall(frame.encode())
    echoed = host.makefile("rb").readline()      # proxy forwarded agent echo back
    host.shutdown(_sock.SHUT_WR)
    tp.join(timeout=5); ta.join(timeout=5)
    up_srv.close()
    for p in (upstream, listen):
        if _os.path.exists(p):
            _os.unlink(p)

    assert b"socket bridged prompt" in echoed     # verbatim forwarding both ways
    row = temp_store.search("socket bridged prompt", namespace="team-eng", mode="bm25")[0]
    assert _store.source_url_of(row) == "devin:sk1"
