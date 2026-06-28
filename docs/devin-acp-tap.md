# Devin ACP tap — wiring (macOS / Linux / Windows)

The team's Devin build keeps **no conversation locally** — the IDE drives the
agent live over **ACP** (Agent Client Protocol: JSON-RPC 2.0, newline-delimited,
over the agent's stdio). The tap sits *in that stream*: your IDE launches the tap
instead of the `devin` agent; the tap spawns the real agent, **forwards every
byte both ways verbatim** (Devin is unaffected), and records the user↔LLM turns
into team-brain.

```
IDE / ACP host  ⇄  [ devin-acp-tapped ]  ⇄  real `devin acp`
                         └─ session/prompt → User, session/update → Devin
                            → ingest_session()  → team-brain (episodic memories)
```

Verified against the real agent on this machine (chisel `2026.8.18`, "Affogato
Agent"): standard ACP, ndjson framing, methods `session/new` / `session/prompt` /
`session/update`, auth `windsurf-api-key`.

---

## 1. Wrapper to point your IDE at

| OS | Wrapper (set as the ACP agent command) |
|----|----------------------------------------|
| macOS / Linux | `…/team-brain/bin/devin-acp-tapped` |
| Windows | `…\team-brain\bin\devin-acp-tapped.cmd` |

Both auto-detect the real `devin` binary, forward the IDE's args (e.g. `acp`)
through, and need no edits. They locate the project via their own path, so keep
them inside the repo's `bin/`.

### Make the POSIX wrapper executable (macOS / Linux)

```sh
chmod +x /path/to/team-brain/bin/devin-acp-tapped
```

### Make the tap importable from anywhere (recommended)

The wrappers set `PYTHONPATH` from their own location, so they work as long as
they stay inside the repo's `bin/`. To run the tap from **any** directory (and to
drop the `PYTHONPATH` dependency entirely), install the package once:

```sh
cd /path/to/team-brain
python3 -m pip install -e .            # editable: code stays live, edits apply immediately
# Windows:  py -m pip install -e .
```

After this, `python -m teambrain.connectors.acp_tap …` resolves globally and you
may copy the wrapper anywhere (e.g. `~/bin/devin-acp-tapped`). Use the same
interpreter for the install and for `TEAMBRAIN_PYTHON`. For the shared-Postgres
store also set `MEMENTO_DB_URL`; otherwise it writes the local SQLite store of
whichever account runs the IDE.

---

## 2. Environment knobs

Set these in the environment the IDE launches the agent in (or edit the wrapper):

| Variable | Default | Meaning |
|----------|---------|---------|
| `TEAMBRAIN_NS` | `team-eng` | team-brain namespace to store sessions into |
| `TEAMBRAIN_ACP_RECORD` | `~/devin-acp.jsonl` (`%USERPROFILE%\devin-acp.jsonl`) | raw JSON-RPC frame log (the schema sample) |
| `DEVIN_BIN` | auto-detected | path to the real `devin` agent (set if detection fails) |
| `TEAMBRAIN_PYTHON` | `python3` (`python` on Win) | Python interpreter to run the tap |
| `MEMENTO_DB_URL` | — | shared Postgres DSN; unset ⇒ local SQLite store |

ACL scoping: to restrict captured sessions to groups, run the module directly
with `--groups <g1> <g2>` instead of the wrapper (those become `acl:*` tags,
fail-closed).

---

## 3. Where the real `devin` agent lives (auto-detected)

The wrapper finds these automatically; listed for reference / `DEVIN_BIN`.

### macOS
- Devin.app: `/Applications/Devin.app/Contents/Resources/app/extensions/windsurf/devin/bin/devin`
- JetBrains ACP agent: `~/Library/Caches/JetBrains/acp-agents/devin/<version>/bin/devin`

### Linux
- JetBrains ACP agent: `~/.cache/JetBrains/acp-agents/devin/<version>/bin/devin`
- CLI: `~/.local/share/devin/cli/devin`, `/usr/share/devin/bin/devin`, `/opt/devin/bin/devin`, or `devin` on `PATH`

### Windows
- JetBrains ACP agent: `%LOCALAPPDATA%\JetBrains\acp-agents\devin\<version>\bin\devin.exe` (also under `%APPDATA%`)
- Devin app: `%LOCALAPPDATA%\Programs\Devin\resources\app\extensions\windsurf\devin\bin\devin.exe`

If yours is elsewhere: `DEVIN_BIN=/full/path/to/devin` (or `set DEVIN_BIN=...` on Windows).

---

## 4. Wiring it into the host

### JetBrains IDEs (IntelliJ IDEA 2025.3 / PyCharm / …)

JetBrains manages ACP agents from a registry it downloads to
`~/Library/Caches/JetBrains/acp-agents/` (Linux `~/.cache/JetBrains/…`,
Windows `%LOCALAPPDATA%\JetBrains\…`). Each agent is `{cmd, args}` — Devin's is
`./bin/devin` + `["acp"]`, run from the extracted `…/acp-agents/devin/<version>/`.

**Preferred — add a custom ACP agent (survives Devin updates).** In
`Settings/Preferences → Tools → AI Assistant` (look for the **Agents / ACP /
external (custom) agent** section in 2025.3), add a new agent whose **command**
is the wrapper and whose **argument** is `acp`:

| OS | Command | Args |
|----|---------|------|
| macOS / Linux | `/abs/path/to/team-brain/bin/devin-acp-tapped` | `acp` |
| Windows | `C:\path\to\team-brain\bin\devin-acp-tapped.cmd` | `acp` |

Point your work at that custom agent instead of the stock "Devin". Nothing in the
JetBrains cache is touched, so IDE/agent updates don't disturb it.

**Fallback — shim the cached binary (verified mechanism; re-apply after updates).**
If your 2025.3 build doesn't expose a custom-agent command, shim the binary the
registry launches. macOS/Linux:

```sh
cd ~/Library/Caches/JetBrains/acp-agents/devin/*/bin        # Linux: ~/.cache/...
mv devin devin.real
cat > devin <<'SH'
#!/bin/sh
exec DEVIN_BIN="$(dirname "$0")/devin.real" \
  /abs/path/to/team-brain/bin/devin-acp-tapped "$@"
SH
chmod +x devin
# revert:  mv -f devin.real devin
```

Windows (PowerShell): rename `devin.exe` → `devin.real.exe` in
`%LOCALAPPDATA%\JetBrains\acp-agents\devin\<ver>\bin`, then create `devin.cmd`
there containing
`@set "DEVIN_BIN=%~dp0devin.real.exe"` and a line calling
`C:\path\to\team-brain\bin\devin-acp-tapped.cmd %*`. A JetBrains agent update
re-downloads the binary and overwrites the shim — re-apply after updates (the
custom-agent route above avoids this).

### Devin desktop app
Same idea via the app's **Chisel config / ACP** settings page (there is a
"Chisel config" surface). Point the agent command at the wrapper.

---

## 5. Verify

After wiring, run one short Devin session, then:

### macOS / Linux
```sh
wc -l ~/devin-acp.jsonl                      # raw frames captured
python3 -c "from teambrain.connectors import acp_tap; print(acp_tap.find_devin_binary())"
```

### Windows (PowerShell)
```powershell
(Get-Content $env:USERPROFILE\devin-acp.jsonl).Count
python -c "from teambrain.connectors import acp_tap; print(acp_tap.find_devin_binary())"
```

Then query what was captured (any OS):
```sh
# sources backing a query, ACL-gated, no synthesis
python3 - <<'PY'
from teambrain.assist import assist
print(assist("what did we work on in devin", "developer", "team-eng")["answer"])
PY
```

Stored shape: one **episodic** memory per session, tags `devin` / `session` /
`project:<name>`, back-reference `src:devin:<sessionId>`; surfaced for the
**tester** and **developer** roles.

---

## 6. Smoke-test the wrapper without a full session

Sends a real ACP `initialize` (no login needed) through the wrapper and confirms
forwarding + recording:

```sh
printf '{"jsonrpc":"2.0","id":0,"method":"initialize","params":{"protocolVersion":1,"clientCapabilities":{}}}\n' \
  | bin/devin-acp-tapped acp ; echo
cat ~/devin-acp.jsonl     # should show host->agent initialize + agent->host result
```

Windows: pipe the same line into `bin\devin-acp-tapped.cmd acp`.

---

## 7. Troubleshooting

| Symptom | Fix |
|---|---|
| `could not auto-detect the devin binary` | set `DEVIN_BIN` to the full path (§3) |
| `~/devin-acp.jsonl` stays empty | the IDE isn't launching the wrapper — recheck the agent-command setting; confirm `bin/devin-acp-tapped` is executable |
| Devin works but nothing is stored | frames are captured (`--record` non-empty) but turns aren't — send us ~30–50 lines of the record file so we can pin the parser |
| `ModuleNotFoundError: teambrain` | ensure the wrapper runs from inside the repo (it sets `PYTHONPATH` from its own location); or `pip install -e .` |
| Windows: `python` not found | install Python 3 or set `TEAMBRAIN_PYTHON` to the full `python.exe` path |

---

## 8. Open item

The turn mapping is built to the ACP spec applied tolerantly. The **handshake**
is confirmed against the real agent; the turn-bearing `session/prompt` /
`session/update` shapes will be **pinned to Devin's exact variants** once a real
session's `~/devin-acp.jsonl` sample is reviewed. Send a sample and we lock it.
