"""Live Devin (ACP) activity tap -> team-brain.

This Devin build persists nothing locally — the IDE host drives the agent live
over **ACP** (Agent Client Protocol, JSON-RPC 2.0, newline-delimited over stdio).
So to capture the user↔LLM activity we sit *in the stream*: the host launches
this tap instead of the agent; the tap spawns the real agent and **forwards every
byte both ways verbatim** (Devin works exactly as before) while recording the
turns into team-brain via :func:`teambrain.connectors.devin.ingest_session`.

    ACP host (Devin IDE)  ⇄  [ acp_tap ]  ⇄  real agent (chisel acp)
                                  └─ observes session/prompt + session/update
                                     → coalesces turns → ingest_session()

Wire it up by pointing your ACP host's *agent command* at the tap:

    python -m teambrain.connectors.acp_tap --namespace team-eng \
        --record ~/devin-acp.jsonl -- <real-agent-command> [args...]

``--record`` dumps every raw JSON-RPC frame (with direction) to a JSONL file —
that's the schema sample: run one Devin session, send me ~50 lines, and I'll
tighten the parser to Devin's exact ACP shapes. The forwarding is byte-exact and
never blocked by recording (all observation is wrapped), so the tap can't break
Devin even if a frame is unexpected.

The ACP mapping (per spec, applied tolerantly):
  * ``session/new``  → remember the session's ``cwd`` (the project)
  * ``session/prompt`` (host→agent) → a **User** turn (params.prompt blocks)
  * ``session/update`` (agent→host notification) → **Devin** activity, by
    ``update.sessionUpdate``: ``agent_message_chunk`` / ``agent_thought_chunk``
    (text), ``tool_call`` / ``tool_call_update`` ([tool] title+status),
    ``plan``; ``user_message_chunk`` → **User**. Consecutive same-role chunks are
    coalesced into one turn.

:class:`AcpRecorder` is pure and unit-tested; the stdio pump is the thin I/O shell.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import shutil
import socket
import subprocess
import sys
import threading

from . import devin as _devin

HOST_TO_AGENT = "host->agent"
AGENT_TO_HOST = "agent->host"


def _update_to_turn(update):
    """Map one ACP ``session/update`` payload to ``(role, text)`` or ``None``."""
    if not isinstance(update, dict):
        return None
    kind = str(update.get("sessionUpdate") or update.get("type") or "").lower()
    if "user" in kind:
        return ("User", _devin._msg_text(update.get("content")))
    if "tool_call" in kind:
        title = update.get("title") or update.get("toolCallId") or "tool"
        status = update.get("status") or ""
        return ("Devin", f"[tool] {title}" + (f" ({status})" if status else ""))
    if "plan" in kind:
        return ("Devin", "[plan] " + _devin._msg_text(
            update.get("entries") or update.get("content")))
    # agent_message_chunk / agent_thought_chunk / anything else with content
    text = _devin._msg_text(update.get("content"))
    return ("Devin", text) if text else None


class AcpRecorder:
    """Accumulates ACP traffic into normalized Devin sessions and ingests them.

    Pure/observable: feed frames with :meth:`observe`, call :meth:`flush` to
    persist. Same-role chunks coalesce into one turn so streamed deltas don't
    become hundreds of one-word memories."""

    def __init__(self, namespace, acl_groups=None, ingest=_devin.ingest_session):
        self.namespace = namespace
        self.acl_groups = acl_groups
        self._ingest = ingest
        self._pending_new = {}          # jsonrpc request id -> cwd (session/new)
        self._sessions = {}             # sid -> {"turns": [], "project": str,
                                        #         "role": str|None, "buf": [str]}
        self.ingested = 0

    def _sess(self, sid):
        return self._sessions.setdefault(
            str(sid), {"turns": [], "project": "", "role": None, "buf": []})

    def _emit_buf(self, s):
        if s["role"] and s["buf"]:
            text = " ".join(t for t in s["buf"] if t).strip()
            if text:
                s["turns"].append({"role": s["role"], "text": text})
        s["role"], s["buf"] = None, []

    def _push(self, sid, role, text):
        if not text:
            return
        s = self._sess(sid)
        if s["role"] and s["role"] != role:
            self._emit_buf(s)
        s["role"] = role
        s["buf"].append(text)

    def observe(self, direction, msg):
        """Record one parsed JSON-RPC frame. Never raises (callers forward bytes
        regardless)."""
        try:
            self._observe(direction, msg)
        except Exception:
            pass

    def _observe(self, direction, msg):
        if not isinstance(msg, dict):
            return
        method = msg.get("method")
        params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
        if method == "session/new":
            mid = msg.get("id")
            if mid is not None:
                self._pending_new[mid] = params.get("cwd") or params.get("workingDirectory") or ""
            return
        # session/new response carries the assigned sessionId
        if "result" in msg and msg.get("id") in self._pending_new:
            cwd = self._pending_new.pop(msg.get("id"))
            res = msg.get("result") if isinstance(msg.get("result"), dict) else {}
            sid = res.get("sessionId") or res.get("session_id")
            if sid:
                self._sess(sid)["project"] = cwd
            return
        if method == "session/prompt":
            sid = params.get("sessionId") or params.get("session_id")
            if sid is not None:
                self._push(sid, "User", _devin._msg_text(params.get("prompt")))
            return
        if method == "session/update":
            sid = params.get("sessionId") or params.get("session_id")
            turn = _update_to_turn(params.get("update") or params)
            if sid is not None and turn:
                self._push(sid, turn[0], turn[1])

    def flush(self):
        """Persist every session that has turns. Idempotent-ish: re-flushing the
        same content upserts in the store (dedups by content hash)."""
        for sid, s in self._sessions.items():
            self._emit_buf(s)
            if not s["turns"]:
                continue
            n = self._ingest({"session_id": sid, "turns": s["turns"],
                              "project": s["project"]},
                             self.namespace, acl_groups=self.acl_groups)
            if n:
                self.ingested += 1
        return self.ingested


# ── stdio proxy (thin I/O shell around AcpRecorder) ───────────────────────────

def _pump(src, dst, direction, recorder, record_fp, lock):
    """Forward bytes from src to dst line-by-line, verbatim, observing a parsed
    copy. Forwarding must never depend on parsing succeeding."""
    for raw in iter(src.readline, b""):
        dst.write(raw)
        dst.flush()
        line = raw.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception:
            msg = None
        if record_fp is not None:
            with lock:
                record_fp.write(json.dumps({"dir": direction,
                                            "raw": line.decode("utf-8", "replace")}) + "\n")
                record_fp.flush()
        if msg is not None:
            recorder.observe(direction, msg)
    try:
        dst.close()
    except Exception:
        pass


def run_proxy(agent_cmd, namespace, acl_groups=None, record_path=None) -> int:
    """Spawn the real agent and proxy host<->agent stdio, recording activity."""
    recorder = AcpRecorder(namespace, acl_groups=acl_groups)
    record_fp = open(record_path, "a", encoding="utf-8") if record_path else None
    lock = threading.Lock()
    proc = subprocess.Popen(agent_cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    t_in = threading.Thread(target=_pump, args=(
        sys.stdin.buffer, proc.stdin, HOST_TO_AGENT, recorder, record_fp, lock), daemon=True)
    t_out = threading.Thread(target=_pump, args=(
        proc.stdout, sys.stdout.buffer, AGENT_TO_HOST, recorder, record_fp, lock), daemon=True)
    t_in.start(); t_out.start()
    code = proc.wait()
    t_out.join(timeout=2)
    recorder.flush()
    if record_fp is not None:
        record_fp.close()
    return code


# ── unix-socket proxy (fallback when the host connects to the agent over a sock) ─

def _pump_sock(src_file, dst_sock, direction, recorder, record_fp, lock):
    """Like :func:`_pump` but writes to a socket and signals EOF with
    ``shutdown(SHUT_WR)`` — closing a ``makefile`` half does NOT propagate EOF to
    the peer, which would hang the opposite pump."""
    for raw in iter(src_file.readline, b""):
        try:
            dst_sock.sendall(raw)
        except OSError:
            break
        line = raw.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception:
            msg = None
        if record_fp is not None:
            with lock:
                record_fp.write(json.dumps({"dir": direction,
                                            "raw": line.decode("utf-8", "replace")}) + "\n")
                record_fp.flush()
        if msg is not None:
            recorder.observe(direction, msg)
    try:
        dst_sock.shutdown(socket.SHUT_WR)
    except OSError:
        pass


def _handle_conn(client, upstream_path, namespace, acl_groups, record_fp, lock):
    """Bridge one accepted host connection to the real agent socket, recording."""
    recorder = AcpRecorder(namespace, acl_groups=acl_groups)
    up = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        up.connect(upstream_path)
    except OSError:
        client.close()
        return
    cf, uf = client.makefile("rb"), up.makefile("rb")
    t1 = threading.Thread(target=_pump_sock, args=(
        cf, up, HOST_TO_AGENT, recorder, record_fp, lock), daemon=True)
    t2 = threading.Thread(target=_pump_sock, args=(
        uf, client, AGENT_TO_HOST, recorder, record_fp, lock), daemon=True)
    t1.start(); t2.start(); t1.join(); t2.join()
    recorder.flush()
    for x in (client, up):
        try:
            x.close()
        except Exception:
            pass


def run_socket_proxy(listen_path, upstream_path, namespace, acl_groups=None,
                     record_path=None, max_conns=None) -> int:
    """Listen on ``listen_path`` (a unix socket the ACP host connects to) and
    forward each connection to the real agent at ``upstream_path``, recording.

    Point the host at ``listen_path`` and move/point the real agent's socket to
    ``upstream_path``. ``max_conns`` (testing) handles N connections then returns."""
    if os.path.exists(listen_path):
        os.unlink(listen_path)
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(listen_path)
    srv.listen(8)
    record_fp = open(record_path, "a", encoding="utf-8") if record_path else None
    lock = threading.Lock()
    handled = 0
    threads = []
    try:
        while True:
            client, _ = srv.accept()
            t = threading.Thread(target=_handle_conn, args=(
                client, upstream_path, namespace, acl_groups, record_fp, lock),
                daemon=True)
            t.start()
            threads.append(t)
            handled += 1
            if max_conns and handled >= max_conns:
                t.join()
                break
    finally:
        srv.close()
        try:
            os.unlink(listen_path)
        except OSError:
            pass
        if record_fp is not None:
            record_fp.close()
    return 0


def find_devin_binary():
    """Locate the ``devin`` agent binary across OSes (macOS/Linux/Windows).
    ``DEVIN_BIN`` overrides. Returns the path or ``None``."""
    env = os.environ.get("DEVIN_BIN")
    if env and os.path.exists(env):
        return env
    home = os.path.expanduser("~")
    cands = []
    if sys.platform == "darwin":
        cands.append("/Applications/Devin.app/Contents/Resources/app/"
                     "extensions/windsurf/devin/bin/devin")
        cands += sorted(glob.glob(home + "/Library/Caches/JetBrains/"
                                  "acp-agents/devin/*/bin/devin"), reverse=True)
    elif os.name == "nt":
        local = os.environ.get("LOCALAPPDATA", os.path.join(home, "AppData", "Local"))
        appd = os.environ.get("APPDATA", os.path.join(home, "AppData", "Roaming"))
        for root in (local, appd):
            cands += sorted(glob.glob(os.path.join(
                root, "JetBrains", "acp-agents", "devin", "*", "bin", "devin.exe")),
                reverse=True)
        cands.append(os.path.join(local, "Programs", "Devin", "resources", "app",
                                  "extensions", "windsurf", "devin", "bin", "devin.exe"))
    else:  # linux / other unix
        cands += sorted(glob.glob(home + "/.cache/JetBrains/"
                                  "acp-agents/devin/*/bin/devin"), reverse=True)
        cands += [home + "/.local/share/devin/cli/devin",
                  "/usr/share/devin/bin/devin", "/opt/devin/bin/devin"]
    which = shutil.which("devin")
    if which:
        cands.append(which)
    for c in cands:
        if c and os.path.exists(c):
            return c
    return None


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description="Tap a Devin/ACP agent and record activity into team-brain")
    p.add_argument("--namespace", required=True, help="team-brain namespace to store into")
    p.add_argument("--groups", nargs="*", default=None,
                   help="scope captured sessions to these ACL groups (fail-closed)")
    p.add_argument("--record", default=None,
                   help="also dump every raw JSON-RPC frame to this JSONL file (schema sample)")
    p.add_argument("--socket", dest="listen", default=None,
                   help="SOCKET MODE: unix-socket path the ACP host connects to")
    p.add_argument("--upstream", default=None,
                   help="SOCKET MODE: the real agent's unix-socket path to forward to")
    p.add_argument("--devin-auto", action="store_true",
                   help="STDIO MODE: auto-detect the devin binary and prepend it to "
                        "the args after -- (the IDE passes e.g. `acp`)")
    p.add_argument("agent", nargs=argparse.REMAINDER,
                   help="STDIO MODE: -- <real agent command> [args...]")
    args = p.parse_args(argv)
    if args.listen:
        if not args.upstream:
            p.error("--socket requires --upstream <real agent socket path>")
        return run_socket_proxy(args.listen, args.upstream, args.namespace,
                                acl_groups=args.groups, record_path=args.record)
    rest = args.agent[1:] if args.agent and args.agent[0] == "--" else args.agent
    if args.devin_auto:
        binp = find_devin_binary()
        if not binp:
            p.error("could not auto-detect the devin binary; set DEVIN_BIN")
        cmd = [binp] + (rest or ["acp"])
    else:
        cmd = rest
    if not cmd:
        p.error("provide the real agent command after -- (stdio mode), use "
                "--devin-auto, or use --socket/--upstream (socket mode)")
    return run_proxy(cmd, args.namespace, acl_groups=args.groups, record_path=args.record)


if __name__ == "__main__":
    raise SystemExit(main())
