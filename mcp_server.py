#!/usr/bin/env python3
"""team-brain — MCP server (stdio, JSON-RPC 2.0).

Mirrors memento's mcp_server.py plumbing. Exposes the role-routed knowledge
assistant. Reuses memento's MemoryStorePG via teambrain.store (driven by
MEMENTO_DB_URL, same as memento team mode).

Tools:
  team_assist    help a role (tester|developer|po) solve a question, with citations
  team_remember  deliberately store a piece of team knowledge (optionally ACL-scoped)
  team_sources   show the raw memories that would back an answer (no synthesis)
  team_sync      run a deliberate connector ingest (confluence|jira|pr) into a namespace

Configure an MCP client to launch:  python3 mcp_server.py
Env: MEMENTO_DB_URL (shared Postgres), TEAMBRAIN_ROLES, TEAMBRAIN_SYNTH.
Connector creds (only for team_sync): CONFLUENCE_*, JIRA_*, GITHUB_TOKEN.
"""
from __future__ import annotations

import json
import sys

from teambrain import store as _store
from teambrain.assist import assist as _assist
from teambrain.assist import draft_ticket as _draft_ticket

PROTOCOL_VERSION = "2024-11-05"

TOOLS = [
    {
        "name": "team_assist",
        "description": "Help a role (tester|developer|po) solve a question from the team brain. ACL-gated; returns an answer with citations.",
        "schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "role": {"type": "string", "enum": ["tester", "developer", "po"]},
                "namespace": {"type": "string", "description": "team scope"},
                "groups": {"type": "array", "items": {"type": "string"},
                           "description": "asker's groups, for ACL (omit => public only)"},
                "limit": {"type": "integer"},
            },
            "required": ["query", "role", "namespace"],
        },
    },
    {
        "name": "team_remember",
        "description": "Store a piece of team knowledge. Scope it with groups=[...] to make it visible only to those groups (acl:* tags).",
        "schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "content": {"type": "string"},
                "namespace": {"type": "string"},
                "tags": {"type": "array", "items": {"type": "string"}},
                "tier": {"type": "string"},
                "groups": {"type": "array", "items": {"type": "string"}},
                "source": {"type": "string"},
            },
            "required": ["title", "content", "namespace"],
        },
    },
    {
        "name": "team_sources",
        "description": "Show the raw memories backing an answer (ACL-gated, no synthesis) — for transparency/debugging.",
        "schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "namespace": {"type": "string"},
                "groups": {"type": "array", "items": {"type": "string"}},
                "limit": {"type": "integer"},
            },
            "required": ["query", "namespace"],
        },
    },
    {
        "name": "team_sync",
        "description": "Run a deliberate connector ingest into a namespace. source=confluence|jira|pr|gitlab|devin; key is the space key / project key / 'owner/repo' / 'group/project' (omit for devin — it reads local Devin IDE transcripts). gitlab mines business rules from code for the PO; devin ingests Devin IDE user<->LLM activity. Needs the connector's env credentials. Resolves source ACL into acl:* tags (fail-closed).",
        "schema": {
            "type": "object",
            "properties": {
                "source": {"type": "string", "enum": ["confluence", "jira", "pr", "gitlab", "devin"]},
                "key": {"type": "string", "description": "space key | project key | owner/repo | group/project (not used by devin)"},
                "namespace": {"type": "string"},
                "since": {"type": "string", "description": "incremental checkpoint (CQL/JQL date, ISO updated_at, or ISO transcript time); not used by gitlab"},
                "labels": {"type": "array", "items": {"type": "string"},
                           "description": "confluence only: restrict to these labels"},
                "max_files": {"type": "integer", "description": "gitlab only: cap files mined"},
                "groups": {"type": "array", "items": {"type": "string"},
                           "description": "devin only: scope ingested sessions to these groups (acl:*)"},
            },
            "required": ["source", "namespace"],
        },
    },
    {
        "name": "team_draft_ticket",
        "description": "Help a product owner turn a need into a ticket draft (title, background, acceptance criteria) from the team brain — including business rules mined from code — with citations. ACL-gated. Does NOT write to Jira; the PO reviews and files it.",
        "schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "what the PO wants the ticket to address"},
                "namespace": {"type": "string"},
                "groups": {"type": "array", "items": {"type": "string"},
                           "description": "asker's groups, for ACL (omit => public only)"},
                "limit": {"type": "integer"},
            },
            "required": ["query", "namespace"],
        },
    },
]
_BY_NAME = {t["name"]: t for t in TOOLS}


def _team_assist(a: dict) -> str:
    try:
        res = _assist(a.get("query"), a.get("role"), a.get("namespace"),
                      asker_groups=a.get("groups"), limit=a.get("limit") or 8)
    except ValueError as exc:
        return f"[team-brain] {exc}"
    cites = "\n".join(f"  [{c['n']}] {c['title']} ({c['id']})" for c in res["citations"])
    tail = f"\n\nSources:\n{cites}" if cites else ""
    note = f"\n({res['hidden_by_acl']} memory(ies) hidden by ACL)" if res["hidden_by_acl"] else ""
    return f"{res['answer']}{tail}{note}"


def _team_remember(a: dict) -> str:
    tags = list(a.get("tags") or [])
    tags += [f"{_store.ACL_PREFIX}{g}" for g in (a.get("groups") or [])]
    mid = _store.store().save(a.get("title"), a.get("content"),
                              tier=a.get("tier") or "semantic", tags=tags,
                              source=a.get("source") or "manual",
                              namespace=a.get("namespace"))
    scope = f" (acl: {', '.join(a['groups'])})" if a.get("groups") else " (public)"
    return f"[team-brain] remembered ({mid}){scope}: {a.get('title')}"


def _team_sources(a: dict) -> str:
    rows = _store.store().search(a.get("query"), limit=(a.get("limit") or 8) * 4,
                                 namespace=a.get("namespace"), mode="hybrid")
    visible, hidden = _store.acl_filter(rows, a.get("groups"))
    visible = visible[:a.get("limit") or 8]
    if not visible:
        return "[team-brain] no visible sources."
    lines = [f"- ({m.get('tier')}) {m.get('title')}: "
             f"{str(m.get('content','')).strip()[:160]}" for m in visible]
    note = f"\n({hidden} hidden by ACL)" if hidden else ""
    return "[team-brain] sources:\n" + "\n".join(lines) + note


def _team_sync(a: dict) -> str:
    source = a.get("source")
    key = a.get("key")
    ns = a.get("namespace")
    since = a.get("since")
    if source == "confluence":
        from teambrain.connectors.confluence import sync_space
        res = sync_space(key, ns, since=since, labels=a.get("labels"))
    elif source == "jira":
        from teambrain.connectors.jira import sync_project
        res = sync_project(key, ns, since=since)
    elif source == "pr":
        from teambrain.connectors.pr import sync_repo
        res = sync_repo(key, ns, since=since)
    elif source == "gitlab":
        from teambrain.connectors.gitlab import sync_project
        res = sync_project(key, ns, max_files=a.get("max_files"))
    elif source == "devin":
        from teambrain.connectors.devin import sync
        res = sync(ns, since=since, acl_groups=a.get("groups"))
    else:
        return f"[team-brain] unknown source: {source}"
    what = f"{source} '{key}'" if key else source
    return f"[team-brain] synced {what} -> {ns}: {res}"


def _team_draft_ticket(a: dict) -> str:
    res = _draft_ticket(a.get("query"), a.get("namespace"),
                        asker_groups=a.get("groups"), limit=a.get("limit") or 8)
    cites = "\n".join(f"  [{c['n']}] {c['title']}"
                      + (f" — {c['url']}" if c.get("url") else "")
                      for c in res["citations"])
    tail = f"\n\nSources:\n{cites}" if cites else ""
    note = f"\n({res['hidden_by_acl']} hidden by ACL)" if res["hidden_by_acl"] else ""
    return f"{res['ticket']}{tail}{note}"


_ACTIONS = {"team_assist": _team_assist, "team_remember": _team_remember,
            "team_sources": _team_sources, "team_sync": _team_sync,
            "team_draft_ticket": _team_draft_ticket}


# ── JSON-RPC / MCP plumbing (mirrors memento) ─────────────────────────────────

def _result(id_, result):
    return {"jsonrpc": "2.0", "id": id_, "result": result}


def _error(id_, code, message):
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}


def handle(req: dict):
    method = req.get("method")
    id_ = req.get("id")
    if method == "initialize":
        return _result(id_, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "team-brain", "version": "0.1.0"},
        })
    if method in ("notifications/initialized", "initialized"):
        return None
    if method == "tools/list":
        return _result(id_, {"tools": [
            {"name": t["name"], "description": t["description"],
             "inputSchema": t["schema"]} for t in TOOLS]})
    if method == "tools/call":
        params = req.get("params") or {}
        name = params.get("name")
        if name not in _ACTIONS:
            return _error(id_, -32602, f"unknown tool: {name}")
        try:
            text = _ACTIONS[name](params.get("arguments") or {})
        except Exception as exc:  # fail soft: never crash the client
            text = f"[team-brain] error: {exc}"
        return _result(id_, {"content": [{"type": "text", "text": text}]})
    if method == "ping":
        return _result(id_, {})
    return _error(id_, -32601, f"method not found: {method}")


def main() -> int:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        resp = handle(req)
        if resp is not None:
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
