# Microsoft Teams: wire team-brain in as a Q&A agent

`teambrain/teams.py` is a **read-only** bridge: people in a Teams channel ask
`@team-brain <question>` and get the same cited, ACL-gated answer the IDE
tools return. It's an **Outgoing Webhook** — the lightest Teams integration:
no Azure subscription, no Bot Framework SDK, no app-store approval. One
webhook per team.

```
Teams channel ──@mention──> Teams cloud ──HTTPS POST (HMAC-signed)──> your URL
                                                                        │
              answer in the thread <── JSON message <── team-brain ─────┘
                                            (assist(): search → ACL → Llama cites)
```

## What you need

* the bridge running somewhere: `python3 -m teambrain.teams` (default port 8085),
  with the same env as your MCP servers (`MEMENTO_DB_URL`, `TEAMBRAIN_NAMESPACE`,
  embed + synth vars — see [team-demo-setup.md](team-demo-setup.md));
* a **publicly reachable HTTPS URL** that forwards to it. Teams is a cloud
  service — it must be able to POST to you. Options:
  * demo: a quick tunnel — `ngrok http 8085` or `cloudflared tunnel --url http://localhost:8085`;
  * production: your public ingress / reverse proxy (TLS terminates there; the
    bridge itself speaks plain HTTP). Allow-list Microsoft ranges or put a WAF
    in front if policy requires.

Honest scope note: the *question and answer text* transit Teams' cloud —
exactly like any message typed into Teams. The brain, the Postgres, and the
Llama endpoint stay inside your network; nothing else leaves.

## 1. Create the Outgoing Webhook in Teams

In the **team** (not a chat) where the bot should answer:

1. Team name → **⋯ → Manage team → Apps** → **Create an outgoing webhook**
   (bottom-right link; wording varies slightly by client version).
2. **Name:** `team-brain` — this is what people will @mention.
3. **Callback URL:** your public HTTPS URL (the tunnel/proxy from above).
4. **Description** + optional avatar (`assets/logo.png` works).
5. Click **Create** — Teams shows a **security token once**. Copy it now.

## 2. Configure and start the bridge

```bash
export TEAMBRAIN_TEAMS_SECRET='<the token from step 1>'   # required — unset = reject all
export TEAMBRAIN_NAMESPACE=team-demo                      # which brain answers
export TEAMBRAIN_TEAMS_ROLE=developer                     # default answer voice
# optional, deliberate channel-level grant (see Security below):
# export TEAMBRAIN_TEAMS_GROUPS=team-shop
# plus the usual store/embed/synth env (same block as your MCP servers)
python3 -m teambrain.teams                                # TEAMBRAIN_TEAMS_PORT to change 8085
```

## 3. Use it

In any channel of that team:

```
@team-brain why do we match on the counterparty account?
@team-brain as tester: what should I test for PROSET-9913?
@team-brain as po: what's still blocking the settlement epic?
```

`as <role>:` overrides the default role per question. Answers come back in the
thread with **Sources** and, when ACL hid something, a hidden-count note.
The bridge is read-only: it can't capture, sync, or file tickets — capture
stays in the IDE where the work happens.

## Security model (what to tell your security team)

* **HMAC, fail closed.** Every request must carry Teams'
  `Authorization: HMAC <sig>` header, verified over the raw body against the
  shared token. Wrong or missing signature → 401. **No token configured →
  everything is rejected**, so a half-configured bridge leaks nothing.
* **Askers are ACL-unknown, fail closed.** An outgoing webhook can't tell us
  the asker's AD groups, so every Teams asker sees **public memories only**.
  `TEAMBRAIN_TEAMS_GROUPS` grants more — a deliberate, per-bridge setting.
  Run **one bridge per team** with that team's namespace and grant; never one
  global bridge with a wide grant.
* **Read-only surface.** No write tool is exposed; an injected instruction in
  a message has nothing to trigger.
* Quiet logs: the bridge doesn't log question text.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| bot never replies | Teams can't reach the callback URL — tunnel died / firewall; check the bridge's terminal for POSTs |
| `⛔ signature verification failed` | wrong `TEAMBRAIN_TEAMS_SECRET` (recreate the webhook and copy the fresh token) or a proxy that rewrites the body — proxies must pass it byte-identical |
| replies but "No knowledge found" | wrong `TEAMBRAIN_NAMESPACE` on the bridge, or the store env isn't set in the bridge's shell |
| snippets instead of prose | `TEAMBRAIN_SYNTH`/`OPENAI_BASE_URL` not set in the bridge's env |
| restricted memory not shown | working as intended (fail-closed); grant via `TEAMBRAIN_TEAMS_GROUPS` only if the whole channel should see it |
| works in one team, not another | outgoing webhooks are **per team** — create one in each team, each with its own token/bridge |

## When you outgrow the webhook

Outgoing webhooks are per-team, mention-only, and identity-blind. If the org
wants a real Teams **app** — installable everywhere, DM-able, with the asker's
Azure AD identity (→ real per-user ACL groups instead of per-channel grants) —
that's an Azure Bot registration (Bot Framework). Same `assist()` underneath;
only the HTTP front changes. Do it when the pilot earns it, not before.
