# Contributing to team-brain

Thanks for helping build the team brain. This is a small, deliberately-scoped
project — a thin, role-aware layer over memento's `MemoryStorePG`. Please read
[`docs/team-brain.md`](docs/team-brain.md) before proposing changes; it explains
what team-brain is (and isn't).

## Ground rules (the load-bearing ones)

1. **Don't rebuild the storage engine.** team-brain reuses memento's
   `MemoryStorePG`. New features should be the thin differentiated layer
   (role routing, connectors, ACL, assist), not storage/search/graph code.
2. **ACL is fail-closed — keep it that way.** Any new connector must carry the
   source's access control into `acl:*` tags, and an unknown asker must be
   *denied* restricted memories. ACL changes require tests proving the
   fail-closed path. See `docs/team-brain.md` §6.
3. **Connectors stay stdlib-only and offline-testable.** Network/IO lives behind
   an injectable client (see `confluence.ConfluenceClient`, `pr.GitHubClient`,
   `devin.sync_db`/`acp_tap`). Tests must run with no credentials and no network.
4. **Every memory gets a citation back-link** (`src:` tag) so answers are
   traceable to their source.

## Dev setup

```bash
git clone https://github.com/xerxes-y/team-brain.git
cd team-brain
python3 -m pip install -e .
# storage engine (sibling repo); SQLite path is stdlib-only:
git clone https://github.com/xerxes-y/memento.git ../memento
export MEMENTO_ENGINE_REPO="$PWD/../memento"   # or place memento as ../SkillOPT
pip install pytest
```

## Running the tests

```bash
MEMENTO_DB_URL="" python3 -m pytest -q      # offline, local SQLite store
```

The suite is fully offline (SQLite + mocked HTTP/SQLite). If memento can't be
imported, store-backed tests **skip** rather than fail. New behavior needs tests;
match the style in `tests/` (an injectable fake client + a `temp_store` assertion).

## Adding a connector

Mirror the existing ones:
- an injectable client (so tests need no network),
- map each record → memory with `tier`, role-relevant `tags`, a `src:` back-link,
  and **source ACL → `acl:*` tags (fail-closed)**,
- an incremental `sync_*` returning a summary with a `checkpoint`,
- offline tests covering paging, ACL fail-closed, and chunking,
- wire it into `mcp_server.py`'s `team_sync` and update `roles.json` tags + docs.

## Pull requests

- Branch from `main`; keep PRs focused. `main` is protected — CI (`ci-success`)
  must pass.
- Run the suite locally first. Match surrounding code style (comment density,
  naming, idioms).
- Note any new env vars / external surfaces in the README and `docs/`.
- Don't commit secrets — all credentials are env vars (`MEMENTO_DB_URL`,
  `CONFLUENCE_*`, `JIRA_*`, `GITHUB_TOKEN`, `GITLAB_TOKEN`, `ANTHROPIC_API_KEY`).

## Working with Claude / LLM code

If a change calls Claude (e.g. synthesis, code→business extraction), use the
official Anthropic SDK and default to a current model; keep an offline fallback
so the path runs without an API key (see `synth_claude.py`, `code_summary.py`).

By contributing you agree your contributions are licensed under the project's
[MIT License](LICENSE).
