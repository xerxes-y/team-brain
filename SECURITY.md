# Security Policy

## Reporting a vulnerability

**Do not open a public issue for security problems** — especially anything that
could expose restricted knowledge.

Report privately via GitHub Security Advisories:
<https://github.com/xerxes-y/team-brain/security/advisories/new>

Please include repro steps, affected version/commit, and impact. We aim to
acknowledge within a few days. Coordinated disclosure is appreciated — give us a
reasonable window to ship a fix before publicizing.

## What we care about most

team-brain's central safety property is **access control on org knowledge**.
Treat these as high severity:

- **ACL bypass / fail-open** — a restricted memory (`acl:*` tag) returned to an
  asker who shouldn't see it: an unknown/anonymous asker getting restricted
  content, a wrong-group match, or a connector that fails to carry a source's
  restriction into `acl:*` tags. The gate must **fail closed** (see
  `teambrain/store.py` `visible_to` and `docs/team-brain.md` §6).
- **Restricted content leaking into an answer or citation** via synthesis or the
  `team_sources` / `team_assist` / `team_draft_ticket` surfaces.
- **Connector ingestion pulling content the asker's identity shouldn't reach**
  (e.g. inherited space/project restrictions not captured).
- **Secret exposure** — credentials are environment variables only
  (`MEMENTO_DB_URL`, `CONFLUENCE_*`, `JIRA_*`, `GITHUB_TOKEN`, `GITLAB_TOKEN`,
  `ANTHROPIC_API_KEY`). Report anything that writes a secret into a stored
  memory, log, or the repo.

## Scope notes

- The **Devin ACP tap** forwards bytes between your IDE and the agent and records
  activity locally into your store; it adds no network listener of its own
  (stdio mode) and the socket mode binds a local unix socket you control.
- team-brain depends on memento's `MemoryStorePG`; storage-layer issues should be
  reported against [memento](https://github.com/xerxes-y/memento).

## Supported versions

Pre-1.0: only the latest release / `main` receives security fixes.

| Version | Supported |
|---------|-----------|
| 0.1.x   | ✅ |
| < 0.1   | ❌ |
