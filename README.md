<p align="center">
  <img src="assets/wordmark.svg" alt="team-brain" width="600">
</p>

# team-brain

[![tests](https://github.com/xerxes-y/team-brain/actions/workflows/ci.yml/badge.svg)](https://github.com/xerxes-y/team-brain/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-5b5bf0.svg)](LICENSE)

A **role-aware org knowledge assistant** that helps testers, developers, and
product owners *solve problems* from one shared, deliberately-curated knowledge
base — bridging the product-owner ↔ developer gap.

> Sibling of [memento](../memento). team-brain **reuses memento's
> `MemoryStorePG`** (Postgres: BM25 tsvector + pgvector, RRF, namespaces, entity
> graph, audit) as its storage engine. It does **not** ship its own store.
> Full design: [`docs/team-brain.md`](docs/team-brain.md).

## Why

The tax isn't "we don't have enough docs" — it's **translation loss at the
PO ↔ developer ↔ tester seam, repeated per ticket, with no durable record**. A
PO and a dev spend 20 minutes on *why* a rule exists; that reasoning lives in
two heads and an unsearchable scrollback. Three sprints later a different dev
breaks it, or re-asks. The tester chases both people to reconcile expected vs
actual behavior. When someone rotates off the team, the *why* leaves with
them — the ticket and the diff survive, the reasoning doesn't.

Confluence/Jira hold the *artifacts*; they don't hold the reasoning that
connects them. That's the gap team-brain targets — it turns a recurring
interruption into a cited, role-voiced query.

**Honest caveat:** this only pays off if capture actually happens. It's not a
search layer over docs you already have — it's a bet that automatic capture
(connectors + `team_capture`) beats manual documentation, specifically at the
PO/dev/tester handoff. Wire the connectors and use `team_capture` for real, or
you've built a search engine over an empty store.

## Architecture

### System diagram (team-brain ↔ PostgreSQL ↔ LLM)

```
┌─────────────────────────────────────────────────────────────────────────────┐
│ IDE / MCP Client (Devin, IntelliJ, Claude)                                  │
│  • team_assist(query, role)       ← the main entry point                     │
│  • team_draft_ticket / team_explain_ticket / team_test_plan                  │
│  • team_capture (push chat → memories)                                       │
└─────────────────────────────────────────────────────────────────────────────┘
                                       │
                      ┌────────────────┴────────────────┐
                      │                                 │
                      ▼                                 ▼
         ┌──────────────────────────┐    ┌──────────────────────┐
         │     team-brain           │    │  Connectors (async)  │
         │  (mcp_server.py)         │    │                      │
         │                          │    │  • Jira ingester     │
         │  1. RETRIEVE:            │    │  • Confluence sync   │
         │     hybrid search        │    │  • PR miner          │
         │     (RRF fusion)         │    │  • GitLab business   │
         │                          │    │    rules extractor   │
         │  2. FILTER:              │    │  • Devin/IntelliJ    │
         │     ACL (fail-closed)    │    │    IDE activity tap  │
         │                          │    │                      │
         │  3. RERANK:              │    │  + team_capture      │
         │     role tag/tier bias   │    │    (deliberate push) │
         │                          │    └─────────┬────────────┘
         │  4. SYNTHESIZE:          │              │
         │     (optional)           │              │
         │                          │              │
         └──────────────┬───────────┘              │
                        │                         │
          ┌─────────────┴─────────────┐           │
          │                           │           │
          ▼                           ▼           ▼
     ┌─────────────────────────────────────────────────┐
     │      PostgreSQL (MemoryStorePG)                 │
     │                                                 │
     │  • memories table:                              │
     │    - id, title, content (text)                  │
     │    - embedding (pgvector 384/768/1024-d)        │
     │    - tags (role, acl:group, src:url)            │
     │    - tier (semantic/procedural/episodic)        │
     │    - namespace (team-1, team-2, ...)            │
     │                                                 │
     │  • Search:                                      │
     │    BM25 tsvector (lexical)                       │
     │    + pgvector <=> (semantic ANN)                │
     │    fused by RRF (reciprocal-rank fusion)        │
     │                                                 │
     └─────────────────────────────────────────────────┘
                        │
          ┌─────────────┤
          │             │
          │             ▼
          │        ┌─────────────────┐
          │        │   LLM (optional)│
          │        │ TEAMBRAIN_SYNTH │
          │        │                 │
          │        │ • Claude        │
          │        │ • OpenAI        │
          │        │ • Local Ollama  │
          │        │ • Company LLM   │
          │        │                 │
          │        │ Reads ranked    │
          │        │ memories + cites│
          │        └─────────────────┘
          │             │
          │ (no synth)  │ (with synth)
          ▼             ▼
      ┌────────────────────────────┐
      │  Answer + citations        │
      │                            │
      │  Extractive (ranked snipps)│
      │  OR                        │
      │  Synthesized (LLM written) │
      └────────────────────────────┘
```

### Data flow detail

**Write path** (ingestion):
```
Jira ──────────┐
Confluence ────├─→ connector (chunk + ACL + embed) ──→ PostgreSQL
GitHub/GitLab ─┤   (connectors run passively or on-demand)
IDE ───────────┤
Chat ──────────→ team_capture (deliberate push)
```

**Read path** (retrieval):
```
Query (string)
  ↓
1. Embed query (same model as memories)
  ↓
2. PostgreSQL hybrid search:
   • BM25 tsvector (keyword match)
   • pgvector <=> (semantic similarity, ANN)
   • RRF (blend rankings)
  ↓
3. team-brain ACL filter (fail-closed):
   • Drop memories with acl:group tags unless asker is in group
   • Public memories (no acl:* tag) always pass
  ↓
4. Soft rerank (role-aware):
   • Boost memories tagged with role's tags
   • Boost memories with role's preferred tiers
   • Keep hybrid search order as tiebreaker
  ↓
5. Synthesize (optional):
   • If LLM wired: read top-K memories, synthesize answer
   • If no LLM: return ranked snippets + citations
  ↓
Answer + citations (id, title, source_url)
```

### Core architectural choices

| Decision | Why | Tradeoff |
|---|---|---|
| **One namespace, roles as retrieval profiles** | Dev answer can pull PO's business rules; tester sees both sides. Bridges gap instead of rebuilding it. | Roles don't enforce separation; ACL does (fail-closed). |
| **Connectors + capture, not manual docs** | Continuous ingestion beats write-once. Captures what teams already produce (Jira, PRs, chat). | Requires discipline; no capture → empty store. |
| **Search first, synthesize second** | Retrieval bounds the LLM's context; wrong retrievals produce confident wrong answers. Extractive fallback works. | Smaller LLM calls (cheaper), but needs good search. |
| **Postgres + pgvector, not SaaS vector DB** | Self-hosted, no API keys, data stays in control. Shared with memento engine. | Ops burden (Postgres admin), not serverless. |
| **ACL as tags, not schema** | No schema changes to memento; connectors just add `acl:group` tags. Fail-closed default. | Tag-based is flexible but loose; need discipline. |

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
  connectors/intellij.py   ingest a local IntelliJ project: git commits + TODO/FIXME notes
  connectors/openspec.py   ingest a repo's OpenSpec tree: proposals/specs/designs, ticket-tagged
  connectors/acp_tap.py    LIVE Devin (ACP) stdio tap -> records turns as they happen
  connectors/_text.py      shared chunk/slug helpers
mcp_server.py              MCP: team_assist / team_remember / team_sources / team_sync
                                / team_draft_ticket (PO) / team_explain_ticket (dev) / team_test_plan (tester)
                                / team_capture (push the important bits of a chat to the brain, tagged to a ticket)
                           MCP prompt `team-capture` -> `/team-capture` slash command in clients that render prompts
teambrain/capture.py       deliberate end-of-work capture: chat -> memories (optional distill)
teambrain/teams.py         Microsoft Teams outgoing-webhook bridge: read-only Q&A surface (HMAC, fail-closed ACL)
teambrain/demo.py          `team-brain demo` — cold-start sweep: ingest every repo under a directory
bin/devin-acp-tapped[.cmd] IDE-launchable ACP tap wrapper (macOS/Linux + Windows)
roles.json                 role profiles (config, not code): tester / developer / po
docs/team-brain.md         the design + open decisions
docs/devin-mcp.md          add team-brain to Devin as an MCP server (clone -> config -> /team-capture)
docs/team-demo-setup.md    2-3 people + shared Postgres + an internal Llama endpoint, step by step
docs/teams-setup.md        wire the Microsoft Teams Q&A agent (outgoing webhook -> bridge)
docs/devin-acp-tap.md      Devin ACP tap wiring (macOS / Linux / Windows)
docs/setup-gitlab.md       ingest company (self-hosted) GitLab on another machine
scripts/smoke_test.py      backend-agnostic first-local-test
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
python3 mcp_server.py                                   # stdio MCP server
```

The storage backend is chosen at runtime from the environment:

| `MEMENTO_DB_URL` | `TEAMBRAIN_EMBED` | backend | search |
|---|---|---|---|
| *unset* | — | local **SQLite** | lexical (BM25 + TF cosine, RRF) — used by tests/CI |
| `postgresql://…` | *unset* | **Postgres** | lexical (BM25 tsvector + TF cosine, RRF) |
| `postgresql://…` | `openai` / `local` | **Postgres + pgvector** | **semantic** (real embeddings, ANN `<=>`) fused with BM25 |

### Agent-grade stack (recommended): shared Postgres + semantic vectors

Best for using team-brain *as an agent backend* — shared across the team and
retrieving by **meaning**, which is how an LLM asks questions.

```bash
# 1) start one Postgres+pgvector for the team (ships with memento)
docker compose -f ../memento/team/docker-compose.yml up -d
python3 -m pip install "psycopg[binary]"           # Postgres backend needs psycopg v3

# 2) point team-brain at it (isolated from other teams by namespace)
export MEMENTO_DB_URL=postgresql://memento:memento@localhost:5432/memento

# 3) turn on real semantic embeddings — pick ONE:

# (a) LOCAL, no API key — runs offline on CPU (recommended when you have no key)
python3 -m pip install sentence-transformers
export TEAMBRAIN_EMBED=local              # default model: BAAI/bge-small-en-v1.5 (384-d)

# (b) OpenAI (or any OpenAI-compatible server via OPENAI_BASE_URL, e.g. Ollama)
export TEAMBRAIN_EMBED=openai             # stdlib-only REST call, no extra deps
export OPENAI_API_KEY=sk-...              # default model: text-embedding-3-small (1536-d)

python3 mcp_server.py
```

> **Embeddings need an *embedding* model, not a chat agent.** A generative LLM
> (Devin's IDE agent, Claude, etc.) returns text, not the numeric vectors
> pgvector stores — so it can't be the embedder. With no API key, use
> `TEAMBRAIN_EMBED=local` (offline) or point `OPENAI_BASE_URL` at a local
> OpenAI-compatible server (Ollama `nomic-embed-text`, LM Studio, vLLM, …).
> Chat LLMs belong on the **synthesis** seam (`TEAMBRAIN_SYNTH`), not here.

### Swap the embedder by resource (demo → production)

The embedder is an **adaptor**: pick a resource-tier *profile* and team-brain
chooses backend + model + dimension for you. Start on `demo` (CPU/Apple-GPU, no
key) for a laptop demo; as you get a GPU, change one env var — no code change.

| `TEAMBRAIN_EMBED_PROFILE` | backend / model | dim | needs |
|---|---|---|---|
| `demo` (start here) | local `bge-small-en-v1.5` | 384 | CPU/MPS, ~0.3 GB, no key |
| `cpu` | local `bge-base-en-v1.5` | 768 | CPU, a bit more RAM |
| `gpu-small` | local `Qwen3-Embedding-4B` | 1024 | ~4–9 GB VRAM |
| `gpu-large` | local `Qwen3-Embedding-8B` | 1024 | ~16 GB VRAM |
| `server` | `openai` backend → `OPENAI_BASE_URL` | — | a shared GPU embed server (Ollama/vLLM/TEI) |

```bash
# laptop demo — no key, uses CPU or Apple-silicon GPU (MPS) automatically
export MEMENTO_DB_URL=postgresql://memento:memento@localhost:5432/memento
export TEAMBRAIN_EMBED_PROFILE=demo
python3 mcp_server.py

# later, on a GPU box — flip the profile, then re-embed existing rows
export TEAMBRAIN_EMBED_PROFILE=gpu-small
python3 -m teambrain.reindex          # rebuilds the pgvector column + re-encodes
```

Fine-grained overrides (any of these beat the profile):

| env var | meaning | default |
|---|---|---|
| `TEAMBRAIN_EMBED` | `openai` · `local` · `none` | from profile / `none` |
| `TEAMBRAIN_EMBED_MODEL` | model name | from profile |
| `TEAMBRAIN_EMBED_DIM` | output dim (Matryoshka-truncated if supported) | from profile / native |
| `TEAMBRAIN_EMBED_DEVICE` | `cpu` · `cuda` · `mps` (local) | auto-detected |
| `OPENAI_API_KEY` / `OPENAI_BASE_URL` | cloud key / point at any compatible server | — |

`local` uses `sentence-transformers` (`pip install sentence-transformers`; for
GPU profiles install the matching `torch`). Switching model/dimension changes
the vector space, so always run `python3 -m teambrain.reindex` afterwards — the
dimension is baked into the pgvector column on first init.

Synthesis is **pluggable**: without `TEAMBRAIN_SYNTH` set, `team_assist` returns
an extractive answer (ranked snippets + citations) so the path runs with no LLM.
Point `TEAMBRAIN_SYNTH=module:function` at a real model call to get synthesis.

### No Anthropic? You have three options

team-brain never *requires* Anthropic. Embeddings already use local
`sentence-transformers` (offline) or any OpenAI-compatible server, and both LLM
seams degrade gracefully:

| Want | Set | Needs |
|---|---|---|
| **No LLM at all** | (nothing) | extractive answers + heuristic code-mining — works offline, no keys |
| **Local model** (no key, nothing leaves the box) | `TEAMBRAIN_SYNTH=teambrain.synth_openai:synth` + `OPENAI_BASE_URL=http://localhost:11434/v1` + `TEAMBRAIN_SYNTH_MODEL=llama3.1` | an Ollama/LM Studio/vLLM server |
| **OpenAI / Azure / company gateway** | same `synth_openai` + `OPENAI_API_KEY` (+ `OPENAI_BASE_URL`) | that endpoint |
| **OIDC-token gateway** (enterprise AI hub, short-lived bearer) | `TEAMBRAIN_SYNTH=teambrain.synth_oidc:synth` + `TEAMBRAIN_OIDC_TOKEN_URL`/`_BODY` (+ `OPENAI_BASE_URL`) — auto-refreshes the token | the issuer + gateway |
| **Anthropic** | `TEAMBRAIN_SYNTH=teambrain.synth_claude:synth` + `ANTHROPIC_API_KEY` | a Claude key |

The same OpenAI-compatible backend can drive code→business extraction for the
GitLab ingest: `TEAMBRAIN_CODE_SUMMARY=teambrain.synth_openai:summarize_code`
(falls back to the offline heuristic if the endpoint is down).

> **Note on Devin:** the Devin/Cognition agent is a *chat* model on the synthesis
> seam, not an embedder — it can't produce the vectors pgvector needs. Use a
> local embedder (or OpenAI-compatible embed server) for search; a local LLM for
> synthesis.

### First local test

A backend-agnostic smoke check (save → recall, ACL fail-closed, and — when an
embedder is configured — a zero-overlap *semantic* match). It writes to a
throwaway namespace and cleans up after itself:

```bash
# offline (local SQLite, lexical) — no setup needed
python3 scripts/smoke_test.py

# semantic stack (after the docker + env steps above)
MEMENTO_DB_URL=postgresql://memento:memento@localhost:5432/memento \
  TEAMBRAIN_EMBED_PROFILE=demo python3 scripts/smoke_test.py
```

And the unit suite (offline, SQLite-backed):

```bash
MEMENTO_DB_URL="" python3 -m pytest -q
```

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

# IntelliJ — capture a local project's git commits + TODO/FIXME notes (no creds)
#   key = project path. Jira keys in branch/commit/TODO -> ticket:<KEY> tags (PO<->dev bridge).
#   web_base turns SHAs into src: commit links; acl_groups scopes a private project (fail-closed).
python3 -c "from teambrain.connectors.intellij import sync; \
  print(sync('/path/to/intellij/project', namespace='team-eng'))"

# DEMO / cold start — pre-fill a namespace from EVERY git repo under a
#   directory: commits + TODOs, OpenSpec trees, and business rules mined from
#   local source (LLM if wired, offline heuristic otherwise; --max-files caps
#   cost per repo). Optional --jira PROJ / --confluence SPACE / --github o/r
#   run those connectors too (env creds). Per-repo failures don't stop the sweep.
team-brain demo ~/IdeaProjects --namespace demo --jira PROSET

# OpenSpec — ingest a repo's openspec/ tree (no creds): proposal.md (why),
#   specs (scenarios), design.md (how); tasks.md skipped. Jira keys in the
#   change id/text -> ticket:<KEY> tags, so specs join the ticket's commits
#   and captured chats. web_base turns src: back-links into full URLs.
python3 -c "from teambrain.connectors.openspec import sync; \
  print(sync('/path/to/repo', namespace='team-eng'))"

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

# Capture a chat into the brain — from inside the IDE agent (Devin/IntelliJ), not the shell.
#   Trigger phrase: tell the agent "start saving team-brain" (add an instruction to its config:
#     -> call the team_capture MCP tool with this chat's decisions/rules, ticket=<KEY>, role=...).
#   Or /team-capture if the client renders MCP prompts as slash commands (Devin support: verify).
#   Read it back the only meaningful way (embed+search+ACL, not raw SQL): team_assist.

# Microsoft Teams — read-only Q&A bridge (stdlib, no Bot Framework SDK).
#   Create an Outgoing Webhook in Teams, point it (via a reverse proxy) at this
#   server, and set the token it generates. Askers are ACL-unknown (public
#   memories only, fail-closed); TEAMBRAIN_TEAMS_GROUPS grants a channel more.
#   Per-question role override: "@team-brain as tester: what should I test?"
#   Full wiring guide (webhook creation, HTTPS, security): docs/teams-setup.md
export TEAMBRAIN_TEAMS_SECRET=...   # TEAMBRAIN_TEAMS_ROLE / _GROUPS / _PORT optional
python3 -m teambrain.teams

# Real cited answers + sharp business extraction via Claude (else: extractive / heuristic)
export TEAMBRAIN_SYNTH=teambrain.synth_claude:synth ANTHROPIC_API_KEY=...

pip install pytest && python3 -m pytest tests -q   # offline, SQLite-backed
```

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). The load-bearing rules: reuse memento's
storage (don't rebuild it), keep ACL **fail-closed**, and keep connectors
stdlib-only and offline-testable. `main` is protected — CI must pass.
Security policy: [SECURITY.md](SECURITY.md) · changes: [CHANGELOG.md](CHANGELOG.md).

## License

[MIT](LICENSE) © 2026 Khashayar Yadmand. team-brain reuses memento's
`MemoryStorePG` as its storage engine and is designed to run `codebase-memory-mcp`
(MIT) alongside it for the developer role (docs §7).
