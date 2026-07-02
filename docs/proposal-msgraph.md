# Proposal: Microsoft Graph connector — Teams channels & meeting transcripts

**Status:** proposed · connector buildable now against fakes · blocked on Graph permissions (IT ticket below)
**Owner:** team-brain
**Effort:** ~ the Jira connector (a few days, mostly Graph-shape handling)

## Summary

Ingest two Microsoft 365 sources into the brain, per team and opt-in:

1. **Teams channel messages** — decision threads from designated channels;
2. **Meeting transcripts** — the Copilot/Teams VTT transcript, distilled to
   the decisions that today evaporate when the call ends.

Write-path only: one new module `teambrain/connectors/msgraph.py`. The read
path (`assist`, roles, ACL filter, all surfaces) does not change — the same
property the OpenSpec connector had.

## Why

The logical case's Premise 1 says the expensive knowledge is reasoning that
lands in no artifact. Meetings are the biggest single leak: the "why" is
spoken, the ticket records only the outcome. Today the zero-code answer is
deliberate capture (paste the Copilot recap → `/team-capture`) — it works and
ships now, but it depends on someone remembering. This connector makes the
meeting/channel seam *passive*, like commits and PRs already are.

## Design

```
Teams channel msgs ─┐
Meeting VTT ────────┤→ msgraph.py: GraphClient (injectable, stdlib urllib)
                    │     │  OAuth2 client-credentials, token cached w/ TTL
                    │     ▼  (same pattern as synth_oidc._token)
                    │  ingest_thread() / ingest_transcript()   ← pure, unit-testable
                    │     │  thread = root msg + replies, ONE document
                    │     │  VTT → plain text, chunk_fixed
                    │     │  ticket_keys() → ticket:<KEY> tags (the PO↔dev bridge)
                    │     │  src: = message webUrl / meeting link (clickable citations)
                    │     ▼
                    └→ MemoryStorePG  (episodic raw + semantic distilled)
```

Key decisions, with the reasoning:

* **Thread granularity, not message granularity.** Knowledge lives in the
  exchange; "yes, agreed 👍" is not a memory. Root message + replies are
  joined into one document before chunking.
* **Distill via the existing seam.** Chat and transcripts are the noisiest
  sources we ingest. Raw content is stored as `episodic`; when a distiller is
  wired (`TEAMBRAIN_SYNTH`-style hook, internal LLM), a compact
  decisions/facts memory is stored as `semantic` — that is what the role
  bridges surface. No LLM ⇒ raw-only, never a hard dependency
  (`code_summary` precedent).
* **ACL is a required parameter, not a guess.** Graph group IDs ≠ team-brain
  group names; that mapping is policy. `sync_channel`/`sync_transcripts`
  refuse to run without an explicit `acl_groups` — fail-closed at the API
  boundary. Transcripts (attendee-scoped) get one grant per meeting *series*.
* **Stateless incremental sync.** `@odata.nextLink` paging,
  `lastModifiedDateTime` filter, returned `checkpoint` — the caller owns the
  cursor, like every other connector.
* **Opt-in channel allowlist.** Point it at the channels where decisions
  happen (architecture, incident reviews). Never "all of Teams".
* **Endpoints configurable** per project rule: `MSGRAPH_BASE_URL`,
  `MSGRAPH_TENANT_ID`, `MSGRAPH_CLIENT_ID`, `MSGRAPH_CLIENT_SECRET`.

Wiring: `team_sync(source="msgraph", key="<team-id>/<channel-id>")`; a
`--teams-channel` flag on `team-brain demo` later. Both are three-line diffs
into existing dispatch.

## Test plan

Offline, like all six connector suites: a `FakeGraph` injected as `client`
returning canned Graph-shaped JSON pages + a sample VTT. Asserts: thread
joining, ticket-key bridge, ACL-required refusal, `webUrl` citations, VTT
stripping, `nextLink` paging, checkpoint monotonicity. No credentials in CI.

## Security & privacy

* Read-only Graph scopes; the connector never posts to Teams (the existing
  Teams *bridge* answers questions; this connector only reads).
* Fail-closed ACL as above; memories land in the team's namespace only.
* Transcripts are personal data: **works-council / privacy sign-off before
  enabling transcript sync**. Recommended pilot order: (1) one opt-in
  channel's messages, (2) transcripts for one recurring meeting with
  attendee consent, (3) widen by policy.
* All content stays in the team's own Postgres; distillation uses the
  internal LLM gateway — nothing leaves the network.

## The blocker, and the IT ticket to file now

Reading channel messages via Graph **application permissions is a Microsoft
"protected API"** — tenant-admin consent plus a Microsoft access request.
This has lead time; the connector can be built and fully tested against
fakes while it is pending. Copy-paste request:

> **Azure app registration request — team-brain msgraph connector (read-only)**
> * App: `team-brain-connector`, client-credentials flow, secret in <vault>.
> * Application permissions (admin consent):
>   * `ChannelMessage.Read.All` — read messages of allowlisted channels
>     (protected API: requires the Microsoft access-request form).
>   * `OnlineMeetingTranscript.Read.All` — read transcripts of consented
>     meeting series (phase 2, after privacy sign-off).
>   * `Team.ReadBasic.All` — resolve team/channel names for tags.
> * Scope: read-only; no write scopes requested. Target tenant: <tenant>.
> * Data destination: on-prem Postgres (team-brain store), team-scoped ACL.

## Alternatives considered

* **Zero-code capture (status quo, ships today):** paste Copilot recap →
  `/team-capture`. Right granularity, but depends on a human remembering.
  Keep it regardless — it is the fallback and the consent-friendly mode.
* **Teams outgoing webhook as input:** only sees messages that @mention the
  bot; wrong shape for passive history ingest.
* **Export files (channel export / .vtt from OneDrive) fed to a folder
  connector:** no Graph approval needed; viable interim if IT stalls — a
  strictly simpler variant of this design (same ingest seams, file reader
  instead of GraphClient).

## Rollout

1. File the IT ticket (above) — longest pole, start immediately.
2. Build connector + fake-backed tests (few days; mergeable while waiting).
3. Pilot: one opt-in channel, one namespace, `team_sync` manual runs.
4. Privacy sign-off → enable transcripts for one meeting series.
5. Cron the sync; add `--teams-channel` to the demo sweep.
