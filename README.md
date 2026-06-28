<p align="center">
  <img src="assets/wordmark.svg" alt="team-brain" width="600">
</p>

# team-brain

[![tests](https://github.com/xerxes-y/team-brain/actions/workflows/ci.yml/badge.svg)](https://github.com/xerxes-y/team-brain/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-5b5bf0.svg)](LICENSE)

A **role-aware org knowledge assistant** that helps testers, developers, and
product owners *solve problems* from one shared, deliberately-curated knowledge
base — bridging the product-owner ↔ developer gap.

> Sibling of [memento](../SkillOPT). team-brain **reuses memento's
> `MemoryStorePG`** (Postgres: BM25 tsvector + pgvector, RRF, namespaces, entity
> graph, audit) as its storage engine. It does **not** ship its own store.
> Full design: [`docs/team-brain.md`](docs/team-brain.md).

## What's here (v0.1 scaffold)

```
teambrain/
  store.py                 reuse memento's store + ACL helpers (acl:<group> tags)
  assist.py                read path: assist(query, role) + draft_ticket(query) for the PO
  synth_claude.py          optional Claude-backed synthesis (TEAMBRAIN_SYNTH hook)
  code_summary.py          code -> business rules (Claude, or offline heuristic)
  connectors/confluence.py CQL paging + page-restriction ACL + heading chunk + incremental
  connectors/jira.py       JQL paging + ADF flatten + issue-security ACL + comments
  connectors/pr.py         GitHub: merged-PR "why" + private-repo ACL
  connectors/gitlab.py     mine BUSINESS RULES from a GitLab codebase for the PO
  connectors/devin.py      ingest Devin CLI activity (sessions.db + JSON exports)
  connectors/acp_tap.py    LIVE Devin (ACP) stdio tap -> records turns as they happen
  connectors/_text.py      shared chunk/slug helpers
mcp_server.py              MCP: team_assist / team_remember / team_sources / team_sync / team_draft_ticket
bin/devin-acp-tapped[.cmd] IDE-launchable ACP tap wrapper (macOS/Linux + Windows)
roles.json                 role profiles (config, not code): tester / developer / po
docs/team-brain.md         the design + open decisions
docs/devin-acp-tap.md      Devin ACP tap wiring (macOS / Linux / Windows)
tests/                     offline pytest suite (SQLite, mocked HTTP)
```

## How it differs from memento

| | memento | team-brain |
|---|---|---|
| writes | the agent, automatically | humans + connectors, deliberately |
| reads | the agent, before acting | testers / developers / POs |
| output | a better `SKILL.md` | a cited answer that helps solve a problem |
| shares | — | **imports memento's `MemoryStorePG`** |

## Run

```bash
export MEMENTO_DB_URL=postgresql://user:pass@host/db   # shared with memento; isolated by namespace
python3 mcp_server.py                                   # stdio MCP server
```

Synthesis is **pluggable**: without `TEAMBRAIN_SYNTH` set, `team_assist` returns
an extractive answer (ranked snippets + citations) so the path runs with no LLM.
Point `TEAMBRAIN_SYNTH=module:function` at a real model call to get synthesis.

## Access control (default)

ACL is encoded as `acl:<group>` **tags** — no change to memento's schema. A
memory with no `acl:*` tag is public within its namespace; restricted memories
are shown only if the asker's `groups` intersect. Unknown asker => restricted
memories are **denied** (fail closed). See `docs/team-brain.md` §6.

## Status

Storage/search/namespacing/graph are reused from memento and work today;
**`team_assist`/`team_remember`/`team_sources` run**. Five deliberate-ingest
**connectors are implemented** (Confluence, Jira, PR, GitLab, Devin IDE), each
carrying source ACL into `acl:` tags (fail-closed) and a `src:` citation
back-link, with the source isolated behind an injectable client/reader so the
offline test suite exercises paging + ACL + chunking with no credentials.
Synthesis is **pluggable to Claude** (`teambrain.synth_claude:synth`).

The team works in **Devin**, so the Devin connector ingests the user↔LLM
**activity** as `devin`/`session` episodic memories — "what did we attempt with
the agent, and how did it turn out?" — searchable by the tester and developer
roles. Devin CLI versions store this differently, so `sync()` reads **both**: the
current `sessions.db` SQLite store (`sync_db`) and per-session JSON files like
`artistic-gecko.json` (`sync_transcripts`, filename → session id). The JSON
parser is tolerant of ATIF, slug-named exports, and generic/ACP message shapes.
`ingest_session()` is the seam: file reader, SQLite reader, and a future live
**Devin ACP** (Agent Client Protocol) tap all feed the same normalized session
shape.

The **GitLab connector serves the product owner**: it mines *business rules* from
code (not code structure — that's the developer's need; adopt
[`codebase-memory-mcp`](https://github.com/DeusData/codebase-memory-mcp) for that,
docs §7) via `code_summary` (Claude, or an offline heuristic), stores them as
`business` memories the PO role boosts, and `assist.draft_ticket()` /
`team_draft_ticket` turn them into a ticket draft with citations back to the exact
GitLab file — so the PO can go from "what does the product actually do here?" to a
filable ticket. The PO reviews and files it; team-brain does not write to Jira.

```bash
# Confluence — one space (Cloud: set CONFLUENCE_EMAIL for Basic auth; Server/DC: token only)
export CONFLUENCE_BASE_URL=https://acme.atlassian.net/wiki CONFLUENCE_TOKEN=...
python3 -c "from teambrain.connectors.confluence import sync_space; \
  print(sync_space('ENG', namespace='team-eng', labels=['arch']))"

# Jira — one project (acceptance criteria via JIRA_ACCEPTANCE_FIELD=customfield_XXXXX)
export JIRA_BASE_URL=https://acme.atlassian.net JIRA_EMAIL=you@acme.com JIRA_TOKEN=...
python3 -c "from teambrain.connectors.jira import sync_project; \
  print(sync_project('ENG', namespace='team-eng'))"

# PR — one repo's merged PRs (GITHUB_TOKEN optional for public repos)
export GITHUB_TOKEN=...
python3 -c "from teambrain.connectors.pr import sync_repo; \
  print(sync_repo('acme/app', namespace='team-eng'))"

# GitLab — mine business rules from code for the PO (GITLAB_TOKEN optional for public)
export GITLAB_TOKEN=...   # GITLAB_BASE_URL=https://gl.acme.com/api/v4 for self-hosted
python3 -c "from teambrain.connectors.gitlab import sync_project; \
  print(sync_project('group/shop', namespace='team-eng', max_files=200))"

# Devin CLI — capture activity from BOTH local stores (no creds)
#   sessions.db (SQLite) + per-session JSON exports like artistic-gecko.json
#   override paths with DEVIN_SESSIONS_DB / DEVIN_TRANSCRIPTS_DIR
python3 -c "from teambrain.connectors.devin import sync; \
  print(sync(namespace='team-eng'))"

# Devin LIVE — when the build persists nothing locally and only talks ACP.
#   Both modes forward verbatim (Devin is unaffected) and record turns;
#   --record dumps raw JSON-RPC frames (the schema sample) for tightening the parser.
#
# stdio mode (host spawns the agent as a subprocess — standard ACP):
python3 -m teambrain.connectors.acp_tap --namespace team-eng \
  --record ~/devin-acp.jsonl -- <real-agent-command>          # e.g. -- chisel acp
#
# socket mode (host connects to the agent over a unix socket):
#   point the host at --socket, and the real agent's socket at --upstream
python3 -m teambrain.connectors.acp_tap --namespace team-eng --record ~/devin-acp.jsonl \
  --socket ~/devin-tap.sock --upstream "$HOME/Library/Application Support/Devin/1.11-main.sock"

# Easiest: point your IDE's ACP agent command at the ready-made wrapper
#   macOS/Linux: bin/devin-acp-tapped     Windows: bin\devin-acp-tapped.cmd
#   (auto-detects the devin binary). Full wiring per OS: docs/devin-acp-tap.md
#   To run the tap/wrapper from anywhere:  python3 -m pip install -e .

# Real cited answers + sharp business extraction via Claude (else: extractive / heuristic)
export TEAMBRAIN_SYNTH=teambrain.synth_claude:synth ANTHROPIC_API_KEY=...

pip install pytest && python3 -m pytest tests -q   # offline, SQLite-backed
```

## License

[MIT](LICENSE) © 2026 Khashayar Yadmand. team-brain reuses memento's
`MemoryStorePG` as its storage engine and is designed to run `codebase-memory-mcp`
(MIT) alongside it for the developer role (docs §7).
