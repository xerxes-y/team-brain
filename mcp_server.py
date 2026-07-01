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
import os
import sys

from teambrain import store as _store
from teambrain.log import log
from teambrain.assist import assist as _assist
from teambrain.assist import draft_ticket as _draft_ticket
from teambrain.assist import explain_ticket as _explain_ticket
from teambrain.assist import test_plan as _test_plan
from teambrain.capture import capture as _capture

PROTOCOL_VERSION = "2024-11-05"

TOOLS = [
    {
        "name": "team_assist",
        "description": "Help a role (tester|developer|po) solve a question from the team brain. ACL-gated; returns an answer with citations.",
        "schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "role": {"type": "string", "enum": ["tester", "developer", "po", "ops", "security"]},
                "namespace": {"type": "string", "description": "team scope; omit to use the server's TEAMBRAIN_NAMESPACE default"},
                "groups": {"type": "array", "items": {"type": "string"},
                           "description": "asker's groups, for ACL (omit => public only)"},
                "limit": {"type": "integer"},
            },
            "required": ["query", "role"],
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
                "namespace": {"type": "string", "description": "team scope; omit to use the server's TEAMBRAIN_NAMESPACE default"},
                "tags": {"type": "array", "items": {"type": "string"}},
                "tier": {"type": "string"},
                "groups": {"type": "array", "items": {"type": "string"}},
                "source": {"type": "string"},
            },
            "required": ["title", "content"],
        },
    },
    {
        "name": "team_sources",
        "description": "Inspect the raw memories backing an answer (ACL-gated, no synthesis). Use to verify a ticket was ingested, check ACL coverage, or audit which sources team-brain draws from for a given query.",
        "schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "namespace": {"type": "string", "description": "team scope; omit to use the server's TEAMBRAIN_NAMESPACE default"},
                "groups": {"type": "array", "items": {"type": "string"}},
                "limit": {"type": "integer"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "team_sync",
        "description": "Run a deliberate connector ingest into a namespace. source=confluence|jira|pr|gitlab|devin|intellij|openspec; key is the space key / project key / 'owner/repo' / 'group/project' / local project path (omit for devin — it reads local Devin IDE transcripts). gitlab mines business rules from code for the PO; devin ingests Devin IDE user<->LLM activity; intellij ingests a local IntelliJ project's git commits + TODO/FIXME notes (key=project path; optional web_base for commit back-links); openspec ingests a repo's openspec/ tree — proposals, specs, designs — as ticket-tagged memories (key=repo path; optional web_base for file back-links). Needs the connector's env credentials. Resolves source ACL into acl:* tags (fail-closed).",
        "schema": {
            "type": "object",
            "properties": {
                "source": {"type": "string", "enum": ["confluence", "jira", "pr", "gitlab", "devin", "intellij", "openspec"]},
                "key": {"type": "string", "description": "space key | project key | owner/repo | group/project | local project path (not used by devin)"},
                "namespace": {"type": "string", "description": "team scope; omit to use the server's TEAMBRAIN_NAMESPACE default"},
                "since": {"type": "string", "description": "incremental checkpoint (CQL/JQL date, ISO updated_at, ISO transcript time, or git date); not used by gitlab"},
                "labels": {"type": "array", "items": {"type": "string"},
                           "description": "confluence only: restrict to these labels"},
                "max_files": {"type": "integer", "description": "gitlab only: cap files mined"},
                "web_base": {"type": "string", "description": "intellij only: repo web URL to turn commit SHAs into src: links"},
                "groups": {"type": "array", "items": {"type": "string"},
                           "description": "devin/intellij only: scope ingested memories to these groups (acl:*)"},
            },
            "required": ["source"],
        },
    },
    {
        "name": "team_draft_ticket",
        "description": "Help a product owner turn a need into a ticket draft (title, background, acceptance criteria) from the team brain — including business rules mined from code — with citations. ACL-gated. Does NOT write to Jira; the PO reviews and files it.",
        "schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "what the PO wants the ticket to address"},
                "namespace": {"type": "string", "description": "team scope; omit to use the server's TEAMBRAIN_NAMESPACE default"},
                "groups": {"type": "array", "items": {"type": "string"},
                           "description": "asker's groups, for ACL (omit => public only)"},
                "limit": {"type": "integer"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "team_explain_ticket",
        "description": "Developer-facing reverse bridge: given a Jira ticket (key and/or text), explain the BUSINESS logic in plain terms and point to the code/PRs/commits that implement it — for a developer who doesn't speak the business language. Boosts memories linked to the ticket (Jira issue + IntelliJ commits/TODOs that referenced the key). ACL-gated, cited.",
        "schema": {
            "type": "object",
            "properties": {
                "ticket": {"type": "string", "description": "Jira key (e.g. ABC-123) and/or the ticket text"},
                "namespace": {"type": "string", "description": "team scope; omit to use the server's TEAMBRAIN_NAMESPACE default"},
                "groups": {"type": "array", "items": {"type": "string"},
                           "description": "asker's groups, for ACL (omit => public only)"},
                "limit": {"type": "integer"},
            },
            "required": ["ticket"],
        },
    },
    {
        "name": "team_test_plan",
        "description": "Tester-facing both-sides bridge: a tester's questions span the developer AND the product owner. Retrieves across BOTH (business rules/acceptance/decisions + code/PRs/commits) and reconciles EXPECTED vs ACTUAL behavior into concrete test cases (incl. edge cases) — so the tester gets one cited answer instead of chasing two people. ACL-gated.",
        "schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "feature/ticket/behavior to test (a Jira key boosts linked work)"},
                "namespace": {"type": "string", "description": "team scope; omit to use the server's TEAMBRAIN_NAMESPACE default"},
                "groups": {"type": "array", "items": {"type": "string"},
                           "description": "asker's groups, for ACL (omit => public only)"},
                "limit": {"type": "integer"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "team_capture",
        "description": "End-of-work capture: push the important parts of THIS chat (decisions, business rules, gotchas a PO or developer figured out while working a ticket) into the team brain. Pass ticket=<Jira key> so it's tagged ticket:<KEY> and surfaces in team_explain_ticket / team_test_plan. role=tester|developer|po biases later retrieval; groups=[...] scopes it (acl:*). Optionally distills the text into discrete facts (TEAMBRAIN_DISTILL), else stores it chunked.",
        "schema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "the important chat content to remember"},
                "namespace": {"type": "string", "description": "team scope; omit to use the server's TEAMBRAIN_NAMESPACE default"},
                "ticket": {"type": "string", "description": "Jira key and/or text this knowledge belongs to"},
                "role": {"type": "string", "enum": ["tester", "developer", "po", "ops", "security"],
                         "description": "who captured it (retrieval bias)"},
                "groups": {"type": "array", "items": {"type": "string"},
                           "description": "ACL scope (omit => public in the namespace)"},
                "title": {"type": "string", "description": "optional title for the stored note(s)"},
            },
            "required": ["text"],
        },
    },
]
_BY_NAME = {t["name"]: t for t in TOOLS}


def _ns(a: dict):
    """Resolve the team namespace: explicit arg wins, else this server's
    configured default (``TEAMBRAIN_NAMESPACE``), else None → memento's default.
    Lets a team point their server at one namespace and stop passing it per call."""
    return a.get("namespace") or os.environ.get("TEAMBRAIN_NAMESPACE")


def _team_assist(a: dict) -> str:
    try:
        res = _assist(a.get("query"), a.get("role"), _ns(a),
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
                              namespace=_ns(a))
    scope = f" (acl: {', '.join(a['groups'])})" if a.get("groups") else " (public)"
    return f"[team-brain] remembered ({mid}){scope}: {a.get('title')}"


def _team_sources(a: dict) -> str:
    rows = _store.store().search(a.get("query"), limit=(a.get("limit") or 8) * 4,
                                 namespace=_ns(a), mode="hybrid")
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
    ns = _ns(a)
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
    elif source == "intellij":
        from teambrain.connectors.intellij import sync
        res = sync(key, ns, since=since, web_base=a.get("web_base"),
                   acl_groups=a.get("groups"))
    elif source == "openspec":
        from teambrain.connectors.openspec import sync
        res = sync(key, ns, web_base=a.get("web_base"),
                   acl_groups=a.get("groups"))
    else:
        return f"[team-brain] unknown source: {source}"
    what = f"{source} '{key}'" if key else source
    return f"[team-brain] synced {what} -> {ns}: {res}"


def _team_draft_ticket(a: dict) -> str:
    res = _draft_ticket(a.get("query"), _ns(a),
                        asker_groups=a.get("groups"), limit=a.get("limit") or 8)
    cites = "\n".join(f"  [{c['n']}] {c['title']}"
                      + (f" — {c['url']}" if c.get("url") else "")
                      for c in res["citations"])
    tail = f"\n\nSources:\n{cites}" if cites else ""
    note = f"\n({res['hidden_by_acl']} hidden by ACL)" if res["hidden_by_acl"] else ""
    return f"{res['ticket']}{tail}{note}"


def _team_explain_ticket(a: dict) -> str:
    res = _explain_ticket(a.get("ticket"), _ns(a),
                          asker_groups=a.get("groups"), limit=a.get("limit") or 8)
    cites = "\n".join(f"  [{c['n']}] {c['title']}"
                      + (f" — {c['url']}" if c.get("url") else "")
                      for c in res["citations"])
    tail = f"\n\nSources:\n{cites}" if cites else ""
    note = f"\n({res['hidden_by_acl']} hidden by ACL)" if res["hidden_by_acl"] else ""
    return f"{res['answer']}{tail}{note}"


def _team_test_plan(a: dict) -> str:
    res = _test_plan(a.get("query"), _ns(a),
                     asker_groups=a.get("groups"), limit=a.get("limit") or 10)
    cites = "\n".join(f"  [{c['n']}] {c['title']}"
                      + (f" — {c['url']}" if c.get("url") else "")
                      for c in res["citations"])
    tail = f"\n\nSources:\n{cites}" if cites else ""
    note = f"\n({res['hidden_by_acl']} hidden by ACL)" if res["hidden_by_acl"] else ""
    return f"{res['answer']}{tail}{note}"


def _team_capture(a: dict) -> str:
    res = _capture(a.get("text"), _ns(a), ticket=a.get("ticket"),
                   role=a.get("role"), groups=a.get("groups"), title=a.get("title"))
    tix = f" linked to {', '.join(res['tickets'])}" if res["tickets"] else ""
    scope = f" (acl: {', '.join(a['groups'])})" if a.get("groups") else " (public)"
    return f"[team-brain] captured {res['stored']} memory(ies){tix}{scope}."


# ── Prompts: slash commands in clients that render MCP prompts (e.g. /team-capture).
# A prompt just returns a message that nudges the agent to call team_capture with
# THIS chat's content — same job as the "start saving team-brain" phrase, nicer UX.
PROMPTS = [
    {
        "name": "team-capture",
        "description": "Save the important parts of this chat into the team brain (vectors).",
        "arguments": [
            {"name": "ticket", "description": "Jira key this knowledge belongs to", "required": False},
            {"name": "role", "description": "tester|developer|po (retrieval bias)", "required": False},
        ],
    },
]
_BY_PROMPT = {p["name"]: p for p in PROMPTS}


def _prompt_get(name, args):
    ticket = (args or {}).get("ticket") or ""
    role = (args or {}).get("role") or ""
    hint = (f" Use ticket={ticket}." if ticket else " Infer the Jira key from the chat if any.")
    hint += f" Use role={role}." if role else ""
    return ("Call the team_capture tool now with the important content of THIS "
            "conversation — the decisions, business rules, and gotchas we figured "
            "out — so it's stored in the team brain and surfaces for the ticket."
            + hint)


_ACTIONS = {"team_assist": _team_assist, "team_remember": _team_remember,
            "team_sources": _team_sources, "team_sync": _team_sync,
            "team_draft_ticket": _team_draft_ticket,
            "team_explain_ticket": _team_explain_ticket,
            "team_test_plan": _team_test_plan, "team_capture": _team_capture}


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
            "capabilities": {"tools": {}, "prompts": {}},
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
        args = params.get("arguments") or {}
        log.debug("call %s ns=%s role=%s", name, args.get("namespace"), args.get("role"))
        try:
            text = _ACTIONS[name](args)
        except Exception as exc:  # fail soft: never crash the client
            log.exception("tool %s failed", name)  # ERROR + traceback to stderr
            text = f"[team-brain] error: {exc}"
        return _result(id_, {"content": [{"type": "text", "text": text}]})
    if method == "prompts/list":
        return _result(id_, {"prompts": PROMPTS})
    if method == "prompts/get":
        params = req.get("params") or {}
        name = params.get("name")
        if name not in _BY_PROMPT:
            return _error(id_, -32602, f"unknown prompt: {name}")
        return _result(id_, {
            "description": _BY_PROMPT[name]["description"],
            "messages": [{"role": "user", "content": {
                "type": "text", "text": _prompt_get(name, params.get("arguments"))}}],
        })
    if method == "ping":
        return _result(id_, {})
    return _error(id_, -32601, f"method not found: {method}")


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "init":
        from teambrain.init import main as _init
        return _init(sys.argv[2:])
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
