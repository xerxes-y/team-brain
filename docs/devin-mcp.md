# Adding team-brain to Devin (and other MCP clients)

team-brain is an **MCP server**, not an app-store plugin. You don't install it
from a catalog — you register a command that Devin spawns over stdio. That means
the code must live on the machine Devin runs on, and the server must reach the
**same shared Postgres** the team uses, or your captures land in a private store
nobody else can read.

## 1. Get the code on the machine

```bash
git clone https://github.com/xerxes-y/team-brain.git
cd team-brain && pip install -e .     # registers the `team-brain` command (pyproject [project.scripts])
```

`pip install -e .` gives you a `team-brain` command that runs `mcp_server:main`.
Skip it and call `python3 /abs/path/to/team-brain/mcp_server.py` instead — same result.

### memento must be importable (most common setup error)

team-brain does **not** bundle its storage engine — it imports `memento_memory`
at runtime. If only team-brain is on the machine, you'll hit:

```
ModuleNotFoundError: No module named 'memento_memory'
```

Fix it one of three ways (team-brain looks in this order — see `teambrain/store.py`):

1. **pip-install memento** on the machine, or
2. **clone memento as a sibling** so `../memento` resolves next to team-brain:
   ```
   parent/
     team-brain/
     memento/      <- memento checkout
   ```
3. **point at it explicitly** — set `MEMENTO_ENGINE_REPO=/abs/path/to/memento`
   (in the MCP `env` block below). Use this when memento lives elsewhere.

No version pin: team-brain uses whatever memento is on the path. It only needs
`memento_memory.open_store()` and `memento_memory_pg.MemoryStorePG(dsn, dense_embedder=)`,
both present in current memento.

## 2. Register it in Devin's MCP settings

Add one server to Devin's MCP config (the same JSON shape every MCP client uses).
Put the shared-store env vars in the `env` block so this server talks to the team
Postgres, not a local SQLite:

```json
{
  "mcpServers": {
    "team-brain": {
      "command": "team-brain",
      "env": {
        "MEMENTO_ENGINE_REPO": "/abs/path/to/memento",
        "MEMENTO_DB_URL": "postgresql://memento:memento@db.internal:5432/memento",
        "TEAMBRAIN_EMBED": "local",
        "TEAMBRAIN_SYNTH": "teambrain.synth_claude:synth",
        "ANTHROPIC_API_KEY": "sk-..."
      }
    }
  }
}
```

- `MEMENTO_ENGINE_REPO` — path to the memento checkout. Drop it if memento is
  pip-installed or sits at `../memento`. See "memento must be importable" above.
- `MEMENTO_DB_URL` — **the important one.** Point every teammate's server at the
  same Postgres+pgvector, or the brain isn't shared. Omit it and you get a local
  SQLite file (fine for a solo test, useless for a team).
- `TEAMBRAIN_EMBED` — `local` (offline, no key) or `openai` (set `OPENAI_API_KEY`,
  or `OPENAI_BASE_URL` for a local embed server). This is the **embedding** model
  that fills pgvector — not a chat LLM. See the README embedder table.
- `TEAMBRAIN_SYNTH` + `ANTHROPIC_API_KEY` — optional. Turns retrieved text into a
  written answer with citations. Without it, `team_assist` returns the raw matched
  memories (extractive) — retrieval still works, only the prose is skipped.

If you used `python3 mcp_server.py` instead of the installed command:

```json
"team-brain": { "command": "python3", "args": ["/abs/path/to/team-brain/mcp_server.py"], "env": { ... } }
```

## 3. Trigger capture from inside a chat

Two ways, both just nudge the agent to call the `team_capture` tool:

- **Slash command** — type `/team-capture` (with optional `ticket=` / `role=`).
  Only appears if Devin renders MCP prompts as slash commands — **verify in your
  Devin build**; if it doesn't show, use the phrase below.
- **Trigger phrase** — say "start saving team-brain". For this to work, add an
  instruction to Devin's agent rules:

  ```
  When the user says "start saving team-brain" (or runs /team-capture), call the
  team_capture MCP tool with this chat's decisions/rules/gotchas, ticket=<Jira key>
  (infer from the chat), and role=tester|developer|po. Then confirm what was saved.
  ```

A successful capture replies: `[team-brain] captured N memory(ies) linked to <KEY>`.

## 4. Read the brain back

Don't point Devin at the Postgres vector column directly — a raw embedding is a
float array no LLM can read, and raw SQL bypasses the ACL. Devin understands the
brain by calling **`team_assist`** (or `team_explain_ticket` / `team_test_plan`),
which embeds the question, runs the pgvector similarity search, applies ACL, and
hands the matched **text** to the LLM.

## Quick offline sanity check (no Devin, no Postgres)

```bash
# server speaks MCP over stdin — confirms /team-capture is advertised
printf '%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"initialize"}' \
  '{"jsonrpc":"2.0","id":2,"method":"prompts/list"}' \
  | python3 mcp_server.py
```
