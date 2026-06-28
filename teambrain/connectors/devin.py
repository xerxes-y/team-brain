"""Devin IDE activity -> team-brain ingestion.

The team uses Devin (Cognition) as its IDE/agent. The valuable record there is
**the activity between the user and the LLM** — what was attempted, the agent's
actions, and how it turned out. Devin persists this locally as **ATIF-v1.7
transcripts** (``~/.local/share/devin/cli/transcripts/*.json`` and per-OS
variants); each is ``{schema_version:"ATIF...", session_id, steps:[{source, message,
timestamp}]}`` where ``source`` is ``user`` / ``agent`` / ``system``.

This connector reads those transcripts and stores each session as an **episodic**
memory (the "what happened" tier) — so a developer/tester/PO can later ask "did
we work on X with Devin, and what came of it?" and get a cited answer.

Devin ACP (Agent Client Protocol) is a *live* transport, not a stored log — but
its turns have the same user/agent shape. So the ingest seam is
:func:`ingest_session`, which takes a normalized session dict; the local-file
reader (:func:`sync_transcripts`) is one producer, and a future live ACP tap can
call :func:`ingest_session` directly with the same shape.

Network/filesystem is isolated behind the dir resolver + an injectable
``read_json``, so ``tests/test_devin.py`` exercises parsing and ingest with a
temp transcript dir and no Devin install.
"""
from __future__ import annotations

import json
import os
import sys

from .. import store as _store
from ._text import chunk_fixed, slug

DEFAULT_TIER = "episodic"


def factualize(text: str) -> str:
    """Optional depersonalise/clean hook before storage (the distill idea, docs
    §6). Default is identity."""
    return text


# ── locating Devin transcripts (per-OS, like memento's harvester) ─────────────

def transcript_dirs() -> list:
    """Candidate directories holding Devin CLI ATIF transcripts. Override with
    ``DEVIN_TRANSCRIPTS_DIR`` (``os.pathsep``-separated absolute paths)."""
    override = os.environ.get("DEVIN_TRANSCRIPTS_DIR")
    if override:
        return [os.path.expanduser(p) for p in override.split(os.pathsep) if p]
    home = os.path.expanduser("~")
    cands = []
    if os.name == "nt":
        appdata = os.environ.get("LOCALAPPDATA") or os.path.join(home, "AppData", "Local")
        cands.append(os.path.join(appdata, "devin", "cli", "transcripts"))
    elif sys.platform == "darwin":
        cands.append(os.path.join(home, "Library", "Application Support",
                                  "devin", "cli", "transcripts"))
    cands.append(os.path.join(home, ".local", "share", "devin", "cli", "transcripts"))
    return [d for d in cands if os.path.isdir(d)]


# ── parsing ───────────────────────────────────────────────────────────────────

# Devin CLI versions differ in what they write — strict ATIF transcripts, a
# slug-named per-session JSON export, or (current) the sessions.db SQLite store.
# These keys cover the shapes seen across versions; the parser is deliberately
# tolerant so a new export shape degrades to "best effort", not "skipped".
_USER_ROLES = {"user", "human", "you", "prompt"}
_ROLE_KEYS = ("source", "role", "sender", "author", "type")
_TEXT_KEYS = ("message", "content", "text", "body")
_TURN_LIST_KEYS = ("steps", "messages", "transcript", "events", "nodes", "turns",
                   "history", "chat")
_TS_KEYS = ("timestamp", "ts", "created_at", "time", "createdAt")


def _role(value) -> str:
    return "User" if str(value or "").strip().lower() in _USER_ROLES else "Devin"


def _msg_text(item) -> str:
    """Extract text from a turn whatever the shape: a plain string, a
    ``{text|content|...}`` dict, or a list of ACP content blocks."""
    if isinstance(item, str):
        return item.strip()
    if isinstance(item, list):  # ACP-style content blocks
        return " ".join(t for t in (_msg_text(b) for b in item) if t).strip()
    if isinstance(item, dict):
        for k in _TEXT_KEYS:
            if item.get(k) is not None:
                return _msg_text(item[k])
    return ""


def _turn_list(data: dict):
    for k in _TURN_LIST_KEYS:
        v = data.get(k)
        if isinstance(v, list) and v:
            return v
    return None


def parse_atif(data: dict) -> dict | None:
    """Strict ATIF-v1.7 transcript -> ``{session_id, turns, started, project}``,
    or ``None`` if it isn't ATIF / has no user turn. (Kept as the validated path;
    :func:`parse_session` is the tolerant entry point.)"""
    if not str(data.get("schema_version", "")).startswith("ATIF"):
        return None
    return _from_steps(data, data.get("steps") or [], require_user=True)


def parse_session(data: dict, fallback_id: str = "") -> dict | None:
    """Tolerant parse for any Devin CLI JSON shape (ATIF, slug-named export, or a
    generic ``{messages|transcript|events|...}`` list). Falls back to
    ``fallback_id`` (e.g. the filename) when the payload carries no session id.
    Returns ``None`` only when no turns can be recovered."""
    if not isinstance(data, dict):
        return None
    atif = parse_atif(data)
    if atif:
        if not atif["session_id"]:
            atif["session_id"] = fallback_id
        return atif
    items = _turn_list(data)
    if items is None:
        return None
    sess = _from_steps(data, items, require_user=False)
    if sess and not sess["session_id"]:
        sess["session_id"] = fallback_id
    return sess


def _from_steps(data: dict, items, require_user: bool) -> dict | None:
    turns, started = [], None
    for step in items:
        if not isinstance(step, dict):
            text = _msg_text(step)
            if text:
                turns.append({"role": "Devin", "text": text})
            continue
        role_val = next((step.get(k) for k in _ROLE_KEYS if step.get(k)), "")
        if str(role_val).strip().lower() == "system":
            continue
        text = _msg_text(step)
        if not text:
            continue
        turns.append({"role": _role(role_val), "text": text})
        if started is None:
            started = next((step.get(k) for k in _TS_KEYS if step.get(k)), None)
    if not turns or (require_user and not any(t["role"] == "User" for t in turns)):
        return None
    project = (data.get("project") or data.get("workspace")
               or data.get("cwd") or data.get("working_directory")
               or data.get("repo") or "")
    sid = (data.get("session_id") or data.get("id")
           or data.get("session") or "")
    return {"session_id": sid, "turns": turns, "started": started,
            "project": project}


def conversation_text(turns) -> str:
    return "\n\n".join(f"{t['role']}: {t['text']}" for t in turns)


# ── ingest (the seam: local files OR a live ACP tap both land here) ────────────

def ingest_session(session: dict, namespace: str, acl_groups=None,
                   default_project: str = "") -> int:
    """Store one normalized Devin session as episodic memory. Returns chunk count.

    ``session`` shape: ``{session_id, turns:[{role,text}], started?, project?}``.
    ``acl_groups`` scopes the session to those groups (``acl:*`` tags, fail
    closed); omit for public-within-namespace."""
    turns = session.get("turns") or []
    if not turns:
        return 0
    sid = session.get("session_id") or "unknown"
    project = session.get("project") or default_project
    first_user = next((t["text"] for t in turns if t["role"] == "User"), "")
    title = f"Devin session: {first_user.strip()[:100] or sid}"

    tags = ["devin", "session"]
    if project:
        tags.append(f"project:{slug(os.path.basename(str(project).rstrip('/')))}")
    tags += [f"{_store.ACL_PREFIX}{g}" for g in (acl_groups or []) if g]
    tags.append(f"{_store.SRC_PREFIX}devin:{sid}")  # stable session back-reference

    st = _store.store()
    n = 0
    for chunk in chunk_fixed(factualize(conversation_text(turns)), max_chars=1500):
        st.save(title, chunk, tier=DEFAULT_TIER, tags=tags,
                source="devin", namespace=namespace)
        n += 1
    return n


def sync_transcripts(namespace: str, dirs=None, since: str | None = None,
                     acl_groups=None, read_json=None) -> dict:
    """Ingest Devin per-session JSON files (ATIF or slug-named export, e.g.
    ``artistic-gecko.json``) into ``namespace``. The filename stem is used as the
    session id when the payload carries none.

    ``since`` is an ISO timestamp checkpoint — sessions at or before it are
    skipped. Returns a summary including ``checkpoint`` (max ``started`` seen)."""
    dirs = dirs if dirs is not None else transcript_dirs()
    read_json = read_json or _read_json
    sessions = chunks = skipped = 0
    checkpoint = since
    for path in _iter_files(dirs):
        data = read_json(path)
        if data is None:
            continue
        session = parse_session(data, fallback_id=_stem(path))
        if session is None:
            continue
        started = session.get("started")
        if since and started and started <= since:
            skipped += 1
            continue
        c = ingest_session(session, namespace, acl_groups=acl_groups)
        if c:
            sessions += 1
            chunks += c
        if started and (checkpoint is None or started > checkpoint):
            checkpoint = started
    return {"namespace": namespace, "sessions": sessions, "chunks": chunks,
            "skipped_by_checkpoint": skipped, "checkpoint": checkpoint}


# ── source: current Devin CLI sessions.db (SQLite) ────────────────────────────

def default_db_path() -> str:
    """The Devin CLI's SQLite session store. Override with ``DEVIN_SESSIONS_DB``."""
    override = os.environ.get("DEVIN_SESSIONS_DB")
    if override:
        return os.path.expanduser(override)
    return os.path.join(os.path.expanduser("~"), ".local", "share", "devin",
                        "cli", "sessions.db")


def sync_db(namespace: str, db_path: str | None = None, since=None,
            acl_groups=None) -> dict:
    """Ingest from the Devin CLI's ``sessions.db`` (SQLite): one episodic memory
    per session, turns recovered from ``message_nodes.chat_message`` (a tolerant
    JSON parse — Devin serialises each node as JSON; unknown shapes degrade to
    skipped, never crash). ``since`` is an epoch (``last_activity_at``) cutoff.

    NOTE: validated against the schema on this machine; the exact ``chat_message``
    JSON shape is parsed leniently. If a Devin version stores it differently,
    point me at one row and I'll tighten the extractor."""
    import sqlite3
    path = db_path or default_db_path()
    summary = {"namespace": namespace, "sessions": 0, "chunks": 0,
               "skipped_by_checkpoint": 0, "checkpoint": since, "db": path}
    if not os.path.isfile(path):
        return summary
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
    except Exception:
        return summary
    try:
        rows = conn.execute(
            "SELECT id, title, working_directory, last_activity_at "
            "FROM sessions WHERE COALESCE(hidden,0)=0 "
            "ORDER BY last_activity_at").fetchall()
        for s in rows:
            last = s["last_activity_at"]
            if since is not None and last is not None and last <= since:
                summary["skipped_by_checkpoint"] += 1
                continue
            nodes = conn.execute(
                "SELECT chat_message FROM message_nodes WHERE session_id=? "
                "ORDER BY node_id", (s["id"],)).fetchall()
            turns = []
            for nd in nodes:
                try:
                    msg = json.loads(nd["chat_message"])
                except Exception:
                    continue
                role = next((msg.get(k) for k in _ROLE_KEYS if isinstance(msg, dict)
                             and msg.get(k)), "") if isinstance(msg, dict) else ""
                if str(role).strip().lower() == "system":
                    continue
                text = _msg_text(msg)
                if text:
                    turns.append({"role": _role(role), "text": text})
            session = {"session_id": s["id"], "turns": turns,
                       "project": s["working_directory"] or "", "started": None}
            c = ingest_session(session, namespace, acl_groups=acl_groups)
            if c:
                summary["sessions"] += 1
                summary["chunks"] += c
            if last is not None and (summary["checkpoint"] is None
                                     or last > summary["checkpoint"]):
                summary["checkpoint"] = last
    finally:
        conn.close()
    return summary


def sync(namespace: str, since: str | None = None, acl_groups=None) -> dict:
    """Umbrella: ingest from BOTH Devin CLI stores — per-session JSON files and
    the sessions.db SQLite store — so whichever the local CLI uses is captured."""
    t = sync_transcripts(namespace, since=since, acl_groups=acl_groups)
    d = sync_db(namespace, acl_groups=acl_groups)
    return {"namespace": namespace,
            "sessions": t["sessions"] + d["sessions"],
            "chunks": t["chunks"] + d["chunks"],
            "from_transcripts": t, "from_db": d}


def _iter_files(dirs):
    for d in dirs:
        if not os.path.isdir(d):
            continue
        for name in sorted(os.listdir(d)):
            if name.endswith(".json"):
                yield os.path.join(d, name)


def _stem(path: str) -> str:
    return os.path.splitext(os.path.basename(path))[0]


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None
