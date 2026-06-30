# Team Brain — Design

**Status:** draft / pre-implementation
**Sibling of:** `../memento` (memento). Reuses memento's `MemoryStorePG` as the storage engine.
**One line:** A role-aware assistant that helps testers, developers, and product owners solve problems from one shared, deliberately-curated org knowledge base — bridging the PO↔developer knowledge gap.

---

## 1. Why this is a separate product from memento

memento and team-brain **share the same storage engine** (`memento_memory_pg.MemoryStorePG`) but are different products with different users, write paths, and success metrics. Do **not** overload memento to do both — it blurs both.

| | memento (existing) | team-brain (this) |
|---|---|---|
| Purpose | Agent self-improvement: harvest sessions → evolve `SKILL.md` | Help humans+agents solve problems from org knowledge |
| Who writes | the agent, automatically | humans + connectors (Confluence/Jira/PR), deliberately |
| Who reads | the agent, before acting | testers / developers / POs + role agents |
| LLM call | none during recall | one synthesis/assist call per answer |
| Metric | skill score ↑ | answer correctness + "did it bridge the gap" |
| Shared | — | **imports `MemoryStorePG`** |

**Rule:** team-brain is a sibling repo that *depends on* memento's memory module. Reuse the engine, build only the thin differentiated layer.

---

## 2. Prior-art check (done 2026-06-28)

No one has built this exact product. Closest matches are **coding-context sharers**, not org knowledge bridges:

- `infinity-ai-dev/SyncContext` (1★) — share coding context between agents. No role routing, no Jira/Confluence ingest, no Q&A layer.
- `5queezer/distill` (1★) — team KB for Claude Code; local-LLM depersonalizes in-session corrections. No deliberate ingest, not multi-tenant. **Worth stealing:** the local-LLM "factualize before store" idea.
- `ogham-mcp/ogham-mcp` (110★) — generic shared memory store.

Big players are **infrastructure to build on, not the product**: `mem0` (60k★), `supermemory` (28k★), `cognee` (25k★), `zep` (4.7k★). All are "universal memory layer" SDKs — none ships role-routed assist, PO↔dev bridging, or deliberate Confluence/Jira ingestion.

**Conclusion:** the differentiated parts (role-routed assist, PO↔dev bridging, deliberate org ingestion) are real and unbuilt. The storage layer is a commodity — **do not market team-brain as "a memory MCP."** Market it as "the team brain that answers PO, tester, and developer questions from one source."

---

## 3. Core modeling decision

Keep roles in **one shared namespace** — separating k8s and business into different namespaces would rebuild the PO↔dev wall in the database. The point is to *cross* the gap.

```
namespace = TEAM / org boundary    (hard isolation; already gated in MemoryStorePG)
tag/facet = DOMAIN  (k8s, product, jira, confluence, decisions)   ← soft, queryable
tier      = KIND    (semantic=facts, procedural=how-to, episodic=what-happened)
```

The "k8s agent" vs "business agent" are **not separate stores** — they are **retrieval profiles**: same `search()`, different default tag/tier bias + different task prompt. A k8s-profile query can still pull a business memory when needed. That bridge is the product.

---

## 4. From Q&A to problem-solving

Not just retrieval — each role gets a task-oriented assistant. Same `search()` underneath; the role profile carries a **task prompt**, not only a tag filter.

| Role | Real question | Pulls from | "Solve" means |
|---|---|---|---|
| **Tester** | "Is X a known bug? expected behavior? what cases exist?" | Jira bugs, test plans, Confluence acceptance docs | surface known issues + expected behavior + draft test cases |
| **Developer** | "How does auth work here? why this design?" | PRs, ADRs, k8s/Confluence arch docs (+ code intelligence, §7) | explain the *why*, point to decision/code |
| **Product Owner** | "What did we decide on X? what's blocked?" | Jira, meeting notes, roadmap, Confluence specs | reconstruct decision history across the gap |

`answer(query, role)` → `assist(query, role)`. Roles are **config, not code**:

```jsonc
{
  "tester":    { "tags": ["jira","test","bug","confluence"], "tiers": ["episodic","semantic"], "task_prompt": "Help a tester ..." },
  "developer": { "tags": ["pr","adr","infra","k8s"],         "tiers": ["procedural","semantic"], "task_prompt": "Explain the why ..." },
  "po":        { "tags": ["jira","decision","roadmap"],      "tiers": ["semantic","episodic"],   "task_prompt": "Reconstruct decisions ..." }
}
```

---

## 5. Architecture

```
   WRITE / INGEST (deliberate + connectors)        MemoryStorePG (reused)            READ (role-routed, active)
 ┌───────────────────────────────────────┐    ┌──────────────────────────┐     ┌──────────────────────────┐
 │ Confluence  ──chunk+ACL+incremental──► │    │ namespace · tags · tiers │     │ Tester    assist(q,role) │
 │ Jira close  ──decision+criteria─────►  │    │ entities · hybrid search │     │ Developer assist(q,role) │
 │ PR/MR merge ──the "why"─────────────►  │───►│ + restriction/ACL (NEW)  │────►│ PO  assist + draft_ticket│
 │ GitLab code ──business rules (PO)───►  │    └──────────────────────────┘     └──────────────────────────┘
 │ Devin IDE   ──user↔LLM activity─────►  │
 │ human entry ──business rule─────────►  │         ▲ optional: local-LLM "factualize" on write
```

**Reused from memento (≈80%, no new storage code):** `save`, hybrid `search` (BM25 tsvector + vector, RRF), `namespace` gating, entity graph (`related`/`graph`), tiers, audit, `_ns`/`_actor` resolution.

**New code:** `assist.py` (role-routed), connectors (Confluence/Jira/PR), role config, and **one schema addition: ACL/restriction field** (see §6).

---

## 6. Confluence ingestion — the part that burns most org-knowledge products

Confluence is the highest-value source (it's where the org actually writes things down) and the most dangerous. Plan for three failure modes from day one:

1. **Permissions = the killer.** Confluence has space- and page-level restrictions. Dumping everything into one namespace can leak a restricted HR/strategy/security page through an answer — **hard to reverse** once indexed and surfaced.
   → Carry each page's restriction into the memory record (**new `restriction`/ACL field**), and **filter retrieval by the asker's access**. Don't index restricted pages into the shared namespace unless the gate is enforced.
2. **Most Confluence is stale.** Indexing all of it floods retrieval with noise.
   → Be selective: by space, by label, by `lastUpdated` recency. Start with 1–2 high-signal spaces, not the whole instance.
3. **Pages are long → chunk + re-sync.** One page ≠ one memory.
   → Chunk by heading; store each chunk with a back-link (`source_url`) to the page. Use incremental sync (Confluence CQL `lastModified > checkpoint`), not a one-time dump.

Page → schema mapping:
```
namespace = team/space    tags = [confluence, <space>, <labels>]
tier      = semantic       content = chunked body
NEW: source_url            NEW: restriction (who can see it)
```
Only schema change required: **store the ACL** so the read path can enforce it.

**Optional (stolen from distill):** run raw input through a local LLM to depersonalize/factualize before storing. Valuable for a year of Jira+Confluence history.

---

## 7. Code intelligence is a separate, already-solved capability — adopt, don't build

`DeusData/codebase-memory-mcp` (18.8k★, MIT, C/C++) is a **source-code** intelligence engine (tree-sitter AST, call graphs, routes, code-trained embeddings, **SQLite**). It is the **wrong tool for Confluence-in-Postgres** — it indexes code only, not prose, and doesn't use Postgres.

But it fits the **developer role's code-understanding need** perfectly. So:

| Need | Tool |
|---|---|
| Org prose knowledge (Confluence/Jira/PR-why/decisions) → Postgres | **build:** team-brain + `MemoryStorePG` + connectors |
| **Business rules expressed in code** (PO: "what does the product enforce here?") | **build:** `connectors/gitlab.py` + `code_summary` → `business` memories (§8.5) |
| Code structure ("where is X defined", call graphs) | **adopt:** run `codebase-memory-mcp` as a second MCP |

The distinction is **prose vs structure**, not code vs not-code. Extracting *business meaning* from code (for the PO to write tickets) is prose knowledge — team-brain's job, done by the GitLab connector. Extracting *call graphs / definitions* (for the developer) is structure — `codebase-memory-mcp`'s job. They are complementary MCPs; the developer-role assistant queries **both**. Do **not** rebuild code-structure intelligence.

---

## 8. Build sequencing

Front-load the one thing that can sink the product (leaking restricted pages), not the engine (already done).

1. ~~**Confluence connector — ONE space, with ACL stored from day one.**~~ **DONE.** `connectors/confluence.py`: `sync_space()` pages CQL (`build_cql` → space + label + `lastmodified` recency), `ConfluenceClient.read_acl_tags()` resolves page read-restrictions (groups *and* users) to `acl:` tags so a user-only restriction never falls through to public, `chunk_page()` splits storage-format XHTML by heading, and each chunk is stored with a `src:` citation back-link. Network is isolated behind `ConfluenceClient` (stdlib `urllib`, injectable) — `tests/test_confluence.py` proves paging + ACL fail-closed + chunking offline. **Known gap:** the page-restriction endpoint returns page-level restrictions only, *not* inherited space/ancestor restrictions — so sync only deliberately-chosen spaces (§6), never the whole instance.
2. ~~**Jira** (decisions/bugs — pairs with tester + PO).~~ **DONE.** `connectors/jira.py`: `sync_project()` pages JQL (both `nextPageToken` and `startAt` modes), flattens ADF (`adf_to_text`), and turns each issue into a semantic fact memory (`[KEY] summary` + status/resolution + description), an optional acceptance-criteria memory (`JIRA_ACCEPTANCE_FIELD`, tagged `acceptance`/`test`), and an episodic comment-thread memory. **Issue security levels** → `acl:jira-sec:<level>` (fail-closed); `src:` back-links to `/browse/KEY`. `tests/test_jira.py` covers ADF, ACL, both paging modes, and a full mocked sync.
3. ~~**PR** (the "why" — for developers).~~ **DONE.** `connectors/pr.py`: `sync_repo()` pages a GitHub repo's PRs and ingests **merged** ones only (the decision that shipped) as semantic memories — `[#N] title` + who-merged + body. Private-repo PRs → `acl:repo:<owner/repo>` (fail-closed); `src:` back-links to the PR. `tests/test_pr.py` covers merged-only, repo ACL, paging, and `since` filtering.
4. ~~**Read path:** ship `assist(query, role)` with the three role profiles.~~ **DONE.** `assist.py` (ACL-gated, role-reranked, cited); synthesis is pluggable via `TEAMBRAIN_SYNTH` — `teambrain/synth_claude.py` wires a Claude call (`claude-opus-4-8`, adaptive thinking) that answers only from ACL-filtered sources, degrading to the extractive answer if the SDK/key is absent.
5. **Code intelligence — split by role (DONE for the PO; adopt for the developer):**
   - **Product owner — DONE.** `connectors/gitlab.py` mines **business rules** from a GitLab project (not code structure): `sync_project()` pages the repo tree, selects code files (extension allowlist, skips vendored/test dirs, `max_files` cap), and runs each through `code_summary.summarize` — Claude extracts plain-language business rules (limits, statuses, permissions, pricing, workflows), or an offline heuristic digests comments/docstrings/names/routes/strings when no LLM is available. Results are stored as `business`/`gitlab` semantic memories the PO profile boosts, with `src:` back-links to the exact GitLab file. Private/internal projects → `acl:repo:<project>` (fail-closed). `assist.draft_ticket()` (MCP `team_draft_ticket`) then shapes the retrieved rules into a ticket draft with citations — the PO reviews and files it (team-brain does not write to Jira). `tests/test_gitlab.py` covers selection, ACL-by-visibility, tree paging, the heuristic, and a full mocked sync.
   - **Developer — adopt, don't build.** Run `codebase-memory-mcp` as a second MCP for "where is X defined / call graph" questions (§7). The two are complementary: prose-brain (incl. GitLab business rules) for the *why/what*, codebase-memory for the *where/how in code*.
6. ~~**Devin activity** (the team's agent IDE/CLI).~~ **DONE.** `connectors/devin.py` ingests the **user↔LLM activity** as `devin`/`session` **episodic** memories — what was attempted with the agent and how it turned out — boosted by the tester and developer profiles. User turns → `User`, every other non-system turn (agent reply, action, observation) → `Devin`, so the agent's actions are captured as activity. **Devin CLI versions store sessions differently, so `sync()` reads both stores:**
   - **`sync_db()`** — the current CLI **`sessions.db`** SQLite store (`~/.local/share/devin/cli/sessions.db`; `DEVIN_SESSIONS_DB` to override): one memory per session, turns from `message_nodes.chat_message` (tolerant JSON parse — Devin serialises each node as JSON; the schema also stores tool calls as `acp::ToolCall` JSON, confirming ACP under the hood). `last_activity_at` epoch checkpoint.
   - **`sync_transcripts()`** — per-session JSON files (`transcripts/*.json` or slug-named exports like `artistic-gecko.json`; `DEVIN_TRANSCRIPTS_DIR` to override). The filename stem becomes the session id when the payload has none. `parse_session()` is tolerant of strict ATIF-v1.7, slug-named exports, and generic/ACP `{messages|transcript|events|...}` shapes, so a new CLI export format degrades to best-effort rather than being skipped.

   Optional `acl_groups` scopes a session (fail-closed). **`ingest_session()` is the seam** — file reader, SQLite reader, and the live **Devin ACP** tap all feed the same normalized `{session_id, turns}` shape. `tests/test_devin.py` (16 cases) covers ATIF + generic/ACP parsing, filename-as-id, role mapping, ingest, ACL, both checkpoints, and a real temp `sessions.db`. **Open:** the exact `message_nodes.chat_message` JSON shape is parsed leniently (the local store was empty at build time) — confirm against a populated row and tighten if needed.

   **Live ACP tap (`connectors/acp_tap.py`) — for builds that persist nothing locally.** Investigating an installed Devin showed it runs in *ACP-host mode* (`chisel_agent::acp_server`, "ACP host is the sole source of credentials"): the conversation lives server-side and streams over **ACP** (Agent Client Protocol, JSON-RPC 2.0, newline-delimited over stdio) — `sessions.db`/transcripts stay empty. So the tap sits *in the stream*: the ACP host launches `python -m teambrain.connectors.acp_tap --namespace … -- <agent-cmd>`, which spawns the real agent, **forwards every byte both ways verbatim** (Devin is unaffected — observation is wrapped and never blocks forwarding), and records turns: `session/new`→project cwd, `session/prompt`→User, `session/update`→Devin (message/thought chunks coalesced, `tool_call`→`[tool] …`). `--record FILE` dumps raw frames as the schema sample. Two transports, since how the host reaches the agent varies: **stdio mode** (`-- <agent-cmd>`, the standard ACP spawn) and **socket mode** (`--socket <listen> --upstream <real-agent.sock>`, for hosts that connect to the agent over a unix socket like Devin's `…/Devin/1.11-main.sock`). Both reuse the same `AcpRecorder` (socket mode signals EOF with `shutdown(SHUT_WR)` so neither pump hangs). `AcpRecorder` is pure/unit-tested and the socket bridge has a real `AF_UNIX` end-to-end test (`tests/test_acp_tap.py`, 7 cases); the stdio pump is smoke-tested with a stub agent. **Open:** mapping is built to the ACP spec applied tolerantly — pin to Devin's exact `session/update` variants once a real `--record` sample exists.

Minimal genuinely-new code: `assist.py`, the connectors, role config, and the ACL field. Storage, search, namespacing, entity graph, audit — already shipped in `memento_memory_pg.py`.

---

## 9. Open decisions (pin before coding)

- [x] **ACL model:** RESOLVED for the tag-based approach. Confluence read-restrictions → `acl:<group>` / `acl:user:<id>` tags on each chunk (no schema change); the asker presents `groups` (incl. `user:<id>`) at read time and `store.visible_to` intersects them, denying restricted memories to unknown askers (fail closed). Open sub-question: where the asker's `groups` come from in production (MCP client identity / SSO claims) is still a deployment decision — `team_assist` takes them as an explicit `groups` arg today.
- [ ] **First space:** which 1–2 Confluence spaces are highest-signal?
- [ ] **Local-LLM factualize:** add later — `connectors.confluence.factualize` is an identity hook today; the connectors all route stored text through their `factualize`/storage path so it can be swapped in without touching the data flow.
- [ ] **Same Postgres as memento, or separate DB?** (namespaces isolate teams; decide if team-brain shares memento's instance.)
- [x] **MCP tool surface:** RESOLVED — `team_assist(query, role)`, `team_remember(...)`, `team_sources(query)` shipped in `mcp_server.py`, mirroring memento's pattern.
- [x] **Synthesis:** RESOLVED — pluggable via `TEAMBRAIN_SYNTH`; `teambrain/synth_claude.py` is the Claude implementation, extractive fallback otherwise.
