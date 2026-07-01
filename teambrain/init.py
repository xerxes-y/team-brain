"""``team-brain init`` — interactive setup.

Asks a few questions, then writes ``team-brain.mcp.json`` and prints the exact
MCP-server block to paste into your IDE client (Devin, Claude Desktop, …) plus
the capture-trigger instruction for the agent's rules.

No per-client adapters: it emits a file + paste block rather than editing a
specific IDE's config, so one command covers every MCP host. (Point an IDE that
has an MCP-config *file* at the generated JSON; paste into ones that use a UI.)
"""
from __future__ import annotations

import json
import os
import sys

INSTRUCTION = (
    'When the user says "start saving team-brain" (or runs /team-capture), call the\n'
    "team_capture MCP tool with this chat's decisions/rules/gotchas, ticket=<Jira key>\n"
    "(infer from the chat), and role=tester|developer|po. Then confirm what was saved."
)


def _ask(prompt, default=""):
    """One question; returns the answer or the default. EOF (non-interactive) → default."""
    label = f"{prompt}{f' [{default}]' if default else ''}: "
    try:
        return input(label).strip() or default
    except EOFError:
        return default


def _yes(prompt, default=False):
    d = "Y/n" if default else "y/N"
    ans = _ask(f"{prompt} ({d})", "y" if default else "n").lower()
    return ans.startswith("y")


def build_config(db, embed, synth, key, namespace=""):
    """The (env, mcp-block) for the given choices — pure, so it's testable."""
    env = {}
    if db:
        env["MEMENTO_DB_URL"] = db
    if namespace:
        env["TEAMBRAIN_NAMESPACE"] = namespace
    if embed and embed != "none":
        env["TEAMBRAIN_EMBED"] = embed
    if synth:
        env["TEAMBRAIN_SYNTH"] = "teambrain.synth_claude:synth"
        if key:
            env["ANTHROPIC_API_KEY"] = key
    server = {"command": "team-brain"}
    if env:
        server["env"] = env
    return env, {"mcpServers": {"team-brain": server}}


def main(argv=None):
    print("team-brain init — configure the MCP server for your IDE\n")
    db = _ask("Shared Postgres DSN (blank = local SQLite, solo trial)")
    namespace = _ask("Team namespace (your team/project scope, e.g. proset, payments)")
    embed = _ask("Embedder: local / openai / none", "local") if db else "none"
    synth = _yes("Enable Claude-backed answers (TEAMBRAIN_SYNTH)?")
    key = _ask("ANTHROPIC_API_KEY") if synth else ""

    _env, block = build_config(db, embed, synth, key, namespace)
    text = json.dumps(block, indent=2)

    out = os.path.abspath("team-brain.mcp.json")
    with open(out, "w") as f:
        f.write(text + "\n")

    print("\n── Add this to your IDE's MCP config ─────────────────────────────")
    print(text)
    print(f"\nSaved to {out}")
    if not db:
        print("\nNote: no DSN → local SQLite. Fine solo; for a shared team brain re-run "
              "with the team's Postgres DSN so everyone reads/writes the same store.")
    print("\n── Add this to your agent's rules (capture trigger) ──────────────")
    print(INSTRUCTION)
    print("\nThen restart the IDE's MCP connection. Capture with /team-capture or "
          '"start saving team-brain"; read with team_assist.')
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
