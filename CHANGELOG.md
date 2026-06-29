# Changelog

All notable changes to team-brain are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project adheres
to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Company-GitLab ingest CLI** — `team-brain-gitlab` (`teambrain/ingest_gitlab.py`):
  mine one or more (self-hosted) GitLab projects' code into a namespace, with a
  `--public` opt-in to make a project's knowledge namespace-visible instead of
  the default fail-closed `acl:repo:<slug>` gate. `gitlab.sync_project(public=…)`
  and a returned `acl_group`. Setup guide: `docs/setup-gitlab.md`. Plus
  `scripts/smoke_test.py`, a backend-agnostic first-local-test.
- **Semantic search (pgvector ANN)** — optional dense embedders (`teambrain/embed.py`)
  that flip memento's pgvector path on. Env-driven via `TEAMBRAIN_EMBED`
  (`openai` | `local` | `none`), `TEAMBRAIN_EMBED_MODEL`, `TEAMBRAIN_EMBED_DIM`.
  - `local` — **no API key**, fully offline via `sentence-transformers`
    (default `BAAI/bge-small-en-v1.5`, 384-d). The no-key default.
  - `openai` — stdlib-only REST over `urllib`; works against any
    OpenAI-compatible server via `OPENAI_BASE_URL` (Ollama, LM Studio, vLLM…).
  - `store()` builds `MemoryStorePG` with the embedder when on Postgres, and
    falls back cleanly to lexical SQLite otherwise.
  - `pyproject` extras: `postgres` (`psycopg`), `embed-local`
    (`sentence-transformers`). README documents the docker `pgvector` stack.
  - Note: embeddings require an *embedding* model — a chat LLM (Devin, Claude)
    can't be the embedder; chat models belong on the `TEAMBRAIN_SYNTH` seam.
- **Embedder resource profiles (adaptor)** — `TEAMBRAIN_EMBED_PROFILE`
  (`demo` | `cpu` | `gpu-small` | `gpu-large` | `server`) picks backend+model+dim
  for a given resource tier; individual `TEAMBRAIN_EMBED*` vars still override.
  Local backend now auto-detects device (`cuda`/`mps`/`cpu`, override with
  `TEAMBRAIN_EMBED_DEVICE`) and supports Matryoshka dim truncation for Qwen3.
  New `python3 -m teambrain.reindex` (console script `team-brain-reindex`)
  rebuilds the pgvector column and re-embeds all rows after a model/dim switch.
  Lets a demo run on a laptop (CPU/MPS, no key) and scale up to a GPU model with
  one env-var change.

### Pending

- Pin the Devin ACP tap parser to real `session/prompt` / `session/update`
  frames once a captured session sample is available.

## [0.1.0] - 2026-06-28

First public release — a role-aware org knowledge assistant built as a thin layer
over memento's `MemoryStorePG` (reuses storage/search/graph; does not rebuild it).

### Added

- **Read path** — `assist(query, role)` (ACL-gated, role-reranked, cited) and
  `draft_ticket(query)` for the product owner. Pluggable Claude synthesis via
  `TEAMBRAIN_SYNTH` (`synth_claude.py`) with an offline extractive fallback.
- **Access control** — `acl:*` tags, **fail-closed**: unknown askers are denied
  restricted memories; every memory keeps a `src:` citation back-link.
- **Connectors** (stdlib-only, injectable clients, offline-tested):
  - Confluence — CQL paging + page-restriction ACL + heading chunking + incremental.
  - Jira — JQL paging (token + startAt) + ADF flattening + issue-security ACL + comments.
  - PR (GitHub) — merged-PR "why" + private-repo ACL.
  - GitLab — mine business rules from code for the PO (`code_summary`: Claude or
    offline heuristic).
  - Devin — CLI `sessions.db` + per-session JSON exports.
- **Live Devin ACP tap** (`acp_tap.py`) — transparent stdio/socket proxy that
  records user↔LLM activity; cross-platform wrappers (`bin/devin-acp-tapped[.cmd]`)
  with binary auto-detection (macOS/Linux/Windows). Validated against the real agent.
- **MCP server** — `team_assist`, `team_remember`, `team_sources`, `team_sync`,
  `team_draft_ticket`.
- **Roles** — tester / developer / po profiles in `roles.json` (config, not code).
- **Docs** — design (`docs/team-brain.md`) and per-OS Devin ACP wiring
  (`docs/devin-acp-tap.md`).
- **Project** — MIT license, brand assets (SVG + PNG), CI (GitHub Actions,
  Python 3.10 & 3.12), 57-test offline suite, CONTRIBUTING + issue/PR templates.

[Unreleased]: https://github.com/xerxes-y/team-brain/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/xerxes-y/team-brain/releases/tag/v0.1.0
