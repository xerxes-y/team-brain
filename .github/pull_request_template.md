<!-- Keep PRs focused. `main` is protected — CI (ci-success) must pass. -->

## What & why

<!-- What this changes and the problem it solves. Link issues: Closes #NN -->

## Area

- [ ] read path  - [ ] connector  - [ ] ACL  - [ ] MCP  - [ ] docs/CI

## Checklist

- [ ] Tests added/updated; `MEMENTO_DB_URL="" pytest -q` passes locally
- [ ] No new storage/search/graph code (reuses memento's `MemoryStorePG`)
- [ ] If it touches access control: source ACL → `acl:*` tags, **fail-closed**, with a test
- [ ] New connectors: injectable client, offline tests, `src:` back-link, wired into `team_sync` + `roles.json` + docs
- [ ] No secrets committed; new env vars/surfaces documented
- [ ] LLM calls (if any) use the Anthropic SDK with an offline fallback
