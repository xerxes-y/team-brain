# Ingesting company GitLab on another machine

Run this on a machine that can reach your company (self-hosted) GitLab. It mines
project **code into business rules** the PO/developer roles can query, with a
`src:` back-link to the exact file and access control carried into `acl:*` tags.

## 1. Prerequisites

- Python 3.10+
- Network access to your GitLab instance
- A GitLab **personal access token** with **`read_api`** scope (covers reading
  the repository tree + raw files). `read_repository` also works for file reads,
  but `read_api` is needed for the tree/visibility calls.

## 2. Install team-brain + the storage engine

```bash
git clone https://github.com/xerxes-y/team-brain.git
cd team-brain
python3 -m pip install -e .

# storage engine (memento); SQLite path is stdlib-only
git clone https://github.com/xerxes-y/memento.git ../memento
export MEMENTO_ENGINE_REPO="$PWD/../memento"     # or place it as ../SkillOPT
```

## 3. Point at your company GitLab

```bash
export GITLAB_BASE_URL=https://gitlab.mycompany.com/api/v4   # IMPORTANT: include /api/v4
export GITLAB_TOKEN=glpat-xxxxxxxxxxxxxxxx                   # read_api scope
```

Self-hosted notes:
- The base URL **must end in `/api/v4`**.
- Behind a custom CA / proxy: make sure the machine trusts the cert
  (`SSL_CERT_FILE=/path/ca.pem`) and that `HTTPS_PROXY` is set if required — the
  client uses stdlib `urllib`, which honors both.

## 4. Choose a storage backend

**Quick (no setup):** local SQLite, lexical search — fine for a first run.
Nothing to set; `MEMENTO_DB_URL` stays unset.

**Recommended:** shared Postgres + pgvector + semantic embeddings (search by
meaning — how an LLM/agent queries). See the README "Agent-grade stack":

```bash
export MEMENTO_DB_URL=postgresql://memento:memento@<db-host>:5432/memento
python3 -m pip install "psycopg[binary]" sentence-transformers
export TEAMBRAIN_EMBED_PROFILE=demo     # local, offline, no API key (CPU/MPS)
```

## 5. Ingest

```bash
# one project
team-brain-gitlab group/subgroup/project --namespace team-eng

# several at once, cap files while testing
team-brain-gitlab group/api group/web group/jobs --namespace team-eng --max-files 200

# equivalently, without the console script:
python3 -m teambrain.ingest_gitlab group/project --namespace team-eng
```

Options: `--ref <branch|tag|sha>`, `--max-files N`, `--exts py,go,sql`,
`--public` (see ACL below).

## 6. Access control — important for company repos

By default a **private/internal** project is **fail-closed**: every mined memory
is tagged `acl:repo:<slug>`, so it's hidden unless the asker presents that group.
After a sync the tool prints the exact group, e.g.:

```
[group/api] private: 84/120 files -> 173 chunks
        ACL-gated -> query with groups=['repo:group-api'] (or re-run with --public)
```

Two ways to make it queryable:
- **Pass the group at query time** (keeps it gated): `assist(..., asker_groups=["repo:group-api"])`
  / the MCP `team_assist` `groups` argument.
- **`--public`** — make the project's knowledge visible to **everyone in the
  namespace**. Use this only when the whole namespace audience is authorized to
  see that repo (e.g. an internal repo your whole team may read):
  ```bash
  team-brain-gitlab group/api --namespace team-eng --public
  ```

> Namespaces are the hard team boundary; ACL `acl:*` tags are the soft, per-item
> gate. Pick a namespace per team, then decide gated-vs-`--public` per project.

## 7. Verify / query

```bash
python3 scripts/smoke_test.py        # backend + embedder sanity (offline-safe)

python3 - <<'PY'
from teambrain.assist import assist
r = assist("what validation rules does the API enforce", "developer", "team-eng",
           asker_groups=["repo:group-api"])   # drop groups if you used --public
print(r["answer"])
for c in r["citations"]:
    print(" -", c["title"], c["url"])
PY
```

For written (non-extractive) answers, also set:
```bash
export TEAMBRAIN_SYNTH=teambrain.synth_claude:synth ANTHROPIC_API_KEY=sk-...
```
Without a key the code→business extractor uses an offline heuristic and answers
are extractive (ranked snippets + citations) — retrieval still works.

## 8. Re-running / incremental

Re-running a sync re-mines and upserts (same content dedupes by hash). After
changing the embedding model/dimension, run `python3 -m teambrain.reindex` to
rebuild the pgvector column.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `set GITLAB_BASE_URL and GITLAB_TOKEN` | export both; base URL ends in `/api/v4` |
| `401`/`403` from GitLab | token missing `read_api`, or no access to that project |
| `visibility: private` but everything hidden when you query | pass `groups=['repo:<slug>']`, or re-sync with `--public` |
| SSL errors on self-hosted | `export SSL_CERT_FILE=/path/to/company-ca.pem` |
| 0 files indexed | only code extensions outside vendored/test dirs are mined; try `--exts` |
