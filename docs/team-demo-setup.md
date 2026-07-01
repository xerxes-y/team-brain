# Team setup: shared Postgres + a Llama REST endpoint + 2–3 people

The smallest real multi-user deployment — one shared brain, one internal LLM,
a few teammates' IDEs — e.g. for a live demo. Nothing here leaves your network.

```
Dev A IDE ─┐                      ┌─> PostgreSQL + pgvector   (one box)
Dev B IDE ─┼─ MCP: team-brain ────┤
PO IDE ────┘   (runs per person)  └─> Llama REST endpoint     (you already have)
```

Each person runs their **own** `team-brain` MCP server process; they share the
**same** Postgres and the **same** namespace. The Llama endpoint serves
synthesis (and optionally code→business mining) for everyone.

## 0. Is your Llama endpoint supported?

Yes, if it's **OpenAI-compatible** — vLLM, Ollama, TGI (openai mode),
llama.cpp server, and most company gateways are. Verify with:

```bash
curl -s $LLAMA_URL/chat/completions -H 'Content-Type: application/json' -d '{
  "model": "<your-model-name>", 
  "messages": [{"role":"user","content":"say ok"}] }'
# expect: {"choices":[{"message":{"content":"ok..."}}], ...}
```

Where `$LLAMA_URL` is the base ending in `/v1` (e.g. `http://llm.internal:8000/v1`).
If the shape differs (a custom REST contract), you don't need to wait for us:
synthesis is a `module:function` hook. Write a ~20-line adapter —

```python
# mysynth.py — adapt a custom REST contract to the synth seam
import json, urllib.request
def synth(query, role, profile, rows):
    sources = "\n".join(f"[{i}] {m['title']}: {m['content']}"
                        for i, m in enumerate(rows, 1))
    body = {"prompt": f"Answer with [n] citations.\nQ: {query}\n{sources}"}  # your contract
    req = urllib.request.Request("http://llm.internal/generate",
                                 data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)["text"]                                          # your contract
```

— put it on `PYTHONPATH` and set `TEAMBRAIN_SYNTH=mysynth:synth`.

### OIDC-token gateways (enterprise AI hubs) — supported out of the box

Company gateways (e.g. an Envoy front for Llama) are often OpenAI-compatible
but authenticate with a **short-lived bearer token** from an OIDC issuer — a
static `OPENAI_API_KEY` would expire mid-session. Use the OIDC front instead
of `synth_openai`; it fetches the token, caches it, and refreshes before the
TTL runs out (default margin 50 min for a 1-hour token):

```bash
export TEAMBRAIN_SYNTH=teambrain.synth_oidc:synth
export TEAMBRAIN_CODE_SUMMARY=teambrain.synth_oidc:summarize_code
export OPENAI_BASE_URL=https://<gateway-host>/v1
export TEAMBRAIN_SYNTH_MODEL=meta-llama/llama-3.1-8b-instruct
export TEAMBRAIN_OIDC_TOKEN_URL=https://<issuer-host>/token
export TEAMBRAIN_OIDC_BODY='{"tenant_id":"team-brain","user_id":"team-brain","roles":["admin"]}'
export TEAMBRAIN_TLS_INSECURE=1     # TEST envs with self-signed certs ONLY
```

The issuer must return `{"access_token": "..."}`; `TEAMBRAIN_OIDC_TTL`
(seconds) tunes the refresh margin. If the issuer is down, answers fall back
to extractive with a visible warning — the demo can't hard-fail on auth.

To verify such a gateway by hand, fetch a token first, then the curl above
with `-H "Authorization: Bearer $TOKEN"` (add `-k` for self-signed certs).

## 1. One person: start the shared Postgres

On the machine that will host the brain (a laptop is fine for a demo):

```bash
docker compose -f ../memento/team/docker-compose.yml up -d   # pgvector included
```

Or reuse an existing Postgres — you just need the pgvector extension available.
Note the machine's hostname/IP; everyone connects to it.

## 2. Every person: install + the env block

```bash
pip install "git+https://github.com/xerxes-y/team-brain.git" "psycopg[binary]" sentence-transformers
```

Everyone uses the **same values** for the starred lines — especially the embed
profile: vectors share one column, so one dimension for all.

```bash
# shared store — point at the host from step 1
export MEMENTO_DB_URL=postgresql://memento:memento@<pg-host>:5432/memento   # *
export TEAMBRAIN_NAMESPACE=team-demo                                        # *

# embeddings — local, no key; SAME profile for everyone
export TEAMBRAIN_EMBED_PROFILE=demo                                         # *

# synthesis + code mining — your Llama endpoint
export TEAMBRAIN_SYNTH=teambrain.synth_openai:synth
export TEAMBRAIN_CODE_SUMMARY=teambrain.synth_openai:summarize_code
export OPENAI_BASE_URL=http://<llama-host>/v1                               # *
export TEAMBRAIN_SYNTH_MODEL=<your-model-name>                              # *
# OPENAI_API_KEY only if your gateway requires a bearer token
```

Then register the MCP server in each IDE — `team-brain init` walks through it,
or by hand in the MCP client config:

```json
{ "mcpServers": { "team-brain": {
    "command": "team-brain",
    "env": { "MEMENTO_DB_URL": "postgresql://memento:memento@<pg-host>:5432/memento",
             "TEAMBRAIN_NAMESPACE": "team-demo",
             "TEAMBRAIN_EMBED_PROFILE": "demo",
             "TEAMBRAIN_SYNTH": "teambrain.synth_openai:synth",
             "OPENAI_BASE_URL": "http://<llama-host>/v1",
             "TEAMBRAIN_SYNTH_MODEL": "<your-model-name>" } } } }
```

## 3. Pre-fill the brain (one person, the night before)

```bash
team-brain demo ~/work/repos --namespace team-demo --jira <YOUR-PROJECT> --max-files 200
```

Commits, TODOs, OpenSpec trees, and Llama-mined business rules from every repo
land in the shared namespace; `--jira` adds the real tickets.

## 4. Smoke-test the whole chain

```bash
MEMENTO_DB_URL=postgresql://memento:memento@<pg-host>:5432/memento \
  TEAMBRAIN_EMBED_PROFILE=demo python3 scripts/smoke_test.py
```

Then, from any wired IDE: `team_assist("what business rules govern <your
domain>?", role="po")` — a cited answer means Postgres, embeddings, and Llama
are all connected. Ask each teammate to run one query and one
`/team-capture` so the multi-person path is exercised before the audience is
in the room.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| answer says `(synthesis unavailable: …)` | Llama endpoint unreachable/wrong URL — the demo still runs (extractive fallback), fix `OPENAI_BASE_URL` |
| `expected N dimensions` on save/search | someone used a different embed profile — align `TEAMBRAIN_EMBED_PROFILE`, then `python3 -m teambrain.reindex` |
| answers are snippets, not prose | `TEAMBRAIN_SYNTH` not set in the *MCP server's* env (the IDE spawns it — env must be in the MCP config, not just your shell) |
| "No knowledge found" on a known topic | wrong namespace — every person and the sweep must use the same `TEAMBRAIN_NAMESPACE` |
| teammate sees fewer results | ACL working as intended — restricted memories need matching `groups` |
