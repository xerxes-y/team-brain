# Changelog

All notable changes to team-brain are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project adheres
to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
