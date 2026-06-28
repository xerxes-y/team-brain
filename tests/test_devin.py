"""Devin IDE connector: ATIF parsing, role mapping, ingest, ACL, and a temp-dir
sync of real transcript files."""
from __future__ import annotations

import json

from teambrain import store as _store
from teambrain.connectors import devin


def _atif(session_id, steps, **extra):
    return {"schema_version": "ATIF-v1.7", "session_id": session_id,
            "steps": steps, **extra}


def _step(source, message, ts=None):
    s = {"source": source, "message": message}
    if ts:
        s["timestamp"] = ts
    return s


# ── parsing ───────────────────────────────────────────────────────────────────

def test_parse_atif_maps_roles_and_drops_system():
    data = _atif("s1", [
        _step("system", "boot", ts="2026-06-20T10:00:00Z"),
        _step("user", "Add rate limiting to the API", ts="2026-06-20T10:00:01Z"),
        _step("agent", "Editing middleware.py to add a limiter"),
        _step("action", "ran: pytest"),     # non-user, non-system -> Devin activity
    ], project="/home/me/api")
    s = devin.parse_atif(data)
    assert s["session_id"] == "s1"
    assert [t["role"] for t in s["turns"]] == ["User", "Devin", "Devin"]
    assert s["started"] == "2026-06-20T10:00:01Z"  # first non-system step's ts
    assert s["project"] == "/home/me/api"


def test_parse_atif_handles_dict_message():
    data = _atif("s2", [_step("user", {"text": "hello there"})])
    assert devin.parse_atif(data)["turns"][0]["text"] == "hello there"


def test_parse_rejects_non_atif_and_userless():
    assert devin.parse_atif({"schema_version": "other", "steps": []}) is None
    assert devin.parse_atif(_atif("s", [_step("agent", "no user here")])) is None


def test_conversation_text_renders_turns():
    txt = devin.conversation_text([{"role": "User", "text": "hi"},
                                   {"role": "Devin", "text": "ok"}])
    assert txt == "User: hi\n\nDevin: ok"


# ── ingest ────────────────────────────────────────────────────────────────────

def test_ingest_session_episodic_with_backref(temp_store):
    session = {"session_id": "abc", "project": "/work/shop",
               "turns": [{"role": "User", "text": "Fix the refund bug"},
                         {"role": "Devin", "text": "Patched refund.py limit check"}]}
    n = devin.ingest_session(session, "team-eng")
    assert n == 1
    row = temp_store.search("refund bug", namespace="team-eng", mode="bm25")[0]
    assert row["tier"] == "episodic"
    tags = _store._tags_of(row)
    assert "devin" in tags and "session" in tags
    assert "project:shop" in tags
    assert _store.source_url_of(row) == "devin:abc"
    assert row["title"].startswith("Devin session: Fix the refund bug")
    assert _store.visible_to(row, None)  # no acl_groups => public


def test_ingest_session_acl_scoped(temp_store):
    session = {"session_id": "x", "turns": [{"role": "User", "text": "secret infra work"}]}
    devin.ingest_session(session, "team-eng", acl_groups=["platform"])
    row = temp_store.search("secret infra", namespace="team-eng", mode="bm25")[0]
    assert "acl:platform" in _store._tags_of(row)
    assert not _store.visible_to(row, None)
    assert _store.visible_to(row, ["platform"])


# ── full sync over a temp transcript dir ──────────────────────────────────────

def test_sync_transcripts_reads_dir(temp_store, tmp_path):
    (tmp_path / "a.json").write_text(json.dumps(_atif("s1", [
        _step("user", "Build the export feature", ts="2026-06-20T09:00:00Z"),
        _step("agent", "Created exporter module")])))
    (tmp_path / "b.json").write_text(json.dumps(_atif("s2", [
        _step("user", "Investigate the timeout", ts="2026-06-21T09:00:00Z"),
        _step("agent", "Found a slow query")])))
    (tmp_path / "notdevin.json").write_text(json.dumps({"schema_version": "x"}))
    (tmp_path / "ignore.txt").write_text("nope")

    summary = devin.sync_transcripts("team-eng", dirs=[str(tmp_path)])
    assert summary["sessions"] == 2
    assert summary["checkpoint"] == "2026-06-21T09:00:00Z"
    assert temp_store.search("export feature", namespace="team-eng", mode="bm25")


def test_sync_transcripts_checkpoint_skips_old(temp_store, tmp_path):
    (tmp_path / "old.json").write_text(json.dumps(_atif("s1", [
        _step("user", "old task", ts="2026-06-20T09:00:00Z")])))
    (tmp_path / "new.json").write_text(json.dumps(_atif("s2", [
        _step("user", "new task", ts="2026-06-22T09:00:00Z")])))

    summary = devin.sync_transcripts("team-eng", dirs=[str(tmp_path)],
                                     since="2026-06-21T00:00:00Z")
    assert summary["sessions"] == 1               # only the newer session
    assert summary["skipped_by_checkpoint"] == 1
    assert not temp_store.search("old task", namespace="team-eng", mode="bm25")


def test_transcript_dirs_env_override(monkeypatch):
    monkeypatch.setenv("DEVIN_TRANSCRIPTS_DIR", "/a/b" + __import__("os").pathsep + "/c/d")
    assert devin.transcript_dirs() == ["/a/b", "/c/d"]


# ── tolerant parsing of non-ATIF CLI exports ──────────────────────────────────

def test_parse_session_generic_messages_shape():
    data = {"id": "x1", "messages": [
        {"role": "user", "content": "build the importer"},
        {"role": "assistant", "content": "done, added importer.py"}]}
    s = devin.parse_session(data)
    assert s["session_id"] == "x1"
    assert [t["role"] for t in s["turns"]] == ["User", "Devin"]
    assert "importer" in s["turns"][0]["text"]


def test_parse_session_acp_content_blocks():
    data = {"transcript": [
        {"sender": "human", "content": [{"type": "text", "text": "ship "},
                                        {"type": "text", "text": "the fix"}]}]}
    s = devin.parse_session(data)
    assert s["turns"][0]["text"] == "ship the fix"
    assert s["turns"][0]["role"] == "User"


def test_parse_session_fallback_id_from_filename():
    # a slug-named export with no session id inside -> filename stem is the id
    s = devin.parse_session({"messages": [{"role": "user", "content": "hi"}]},
                            fallback_id="artistic-gecko")
    assert s["session_id"] == "artistic-gecko"


def test_sync_transcripts_uses_filename_as_id(temp_store, tmp_path):
    (tmp_path / "pobble-reference.json").write_text(json.dumps(
        {"messages": [{"role": "user", "content": "investigate flaky test"},
                      {"role": "assistant", "content": "it was a race in setup"}]}))
    devin.sync_transcripts("team-eng", dirs=[str(tmp_path)])
    row = temp_store.search("flaky test race", namespace="team-eng", mode="bm25")[0]
    assert _store.source_url_of(row) == "devin:pobble-reference"


# ── sessions.db (SQLite) source ───────────────────────────────────────────────

def _make_sessions_db(path, sessions):
    import sqlite3
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE sessions(id TEXT PRIMARY KEY, title TEXT, "
              "working_directory TEXT, last_activity_at INTEGER, hidden INTEGER DEFAULT 0)")
    c.execute("CREATE TABLE message_nodes(row_id INTEGER PRIMARY KEY AUTOINCREMENT, "
              "session_id TEXT, node_id INTEGER, chat_message TEXT)")
    for sid, last, hidden, nodes in sessions:
        c.execute("INSERT INTO sessions(id,title,working_directory,last_activity_at,hidden)"
                  " VALUES(?,?,?,?,?)", (sid, sid, "/work/proj", last, hidden))
        for i, (role, text) in enumerate(nodes):
            c.execute("INSERT INTO message_nodes(session_id,node_id,chat_message) VALUES(?,?,?)",
                      (sid, i, json.dumps({"role": role, "content": text})))
    c.commit(); c.close()


def test_sync_db_reads_sessions(temp_store, tmp_path):
    db = str(tmp_path / "sessions.db")
    _make_sessions_db(db, [
        ("lowly-broccoli", 1000, 0, [("user", "add pagination to the list API"),
                                     ("assistant", "added cursor pagination")]),
        ("hidden-one", 1001, 1, [("user", "secret")]),   # hidden -> skipped
    ])
    summary = devin.sync_db("team-eng", db_path=db)
    assert summary["sessions"] == 1
    assert summary["checkpoint"] == 1000
    row = temp_store.search("cursor pagination", namespace="team-eng", mode="bm25")[0]
    assert "devin" in _store._tags_of(row)
    assert _store.source_url_of(row) == "devin:lowly-broccoli"


def test_sync_db_checkpoint(temp_store, tmp_path):
    db = str(tmp_path / "sessions.db")
    _make_sessions_db(db, [
        ("old", 1000, 0, [("user", "old work")]),
        ("new", 3000, 0, [("user", "new work")]),
    ])
    summary = devin.sync_db("team-eng", db_path=db, since=2000)
    assert summary["sessions"] == 1
    assert summary["skipped_by_checkpoint"] == 1
    assert not temp_store.search("old work", namespace="team-eng", mode="bm25")


def test_sync_db_missing_file_is_safe(temp_store, tmp_path):
    summary = devin.sync_db("team-eng", db_path=str(tmp_path / "nope.db"))
    assert summary["sessions"] == 0
