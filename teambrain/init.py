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


def build_config(db, embed, synth, key, namespace="", synth_url="",
                 synth_model="", oidc_url="", oidc_body="", tls_insecure=False):
    """The (env, mcp-block) for the given choices — pure, so it's testable.

    ``synth``: "" / "claude" / "openai" (any OpenAI-compatible endpoint) /
    "oidc" (OpenAI-compatible gateway behind a short-lived OIDC bearer).
    Legacy bools still work (True == "claude")."""
    synth = {True: "claude", False: ""}.get(synth, synth) or ""
    env = {}
    if db:
        env["MEMENTO_DB_URL"] = db
    if namespace:
        env["TEAMBRAIN_NAMESPACE"] = namespace
    if embed and embed != "none":
        env["TEAMBRAIN_EMBED"] = embed
    if synth == "claude":
        env["TEAMBRAIN_SYNTH"] = "teambrain.synth_claude:synth"
        if key:
            env["ANTHROPIC_API_KEY"] = key
    elif synth in ("openai", "oidc"):
        mod = "synth_oidc" if synth == "oidc" else "synth_openai"
        env["TEAMBRAIN_SYNTH"] = f"teambrain.{mod}:synth"
        env["TEAMBRAIN_CODE_SUMMARY"] = f"teambrain.{mod}:summarize_code"
        if synth_url:
            env["OPENAI_BASE_URL"] = synth_url
        if synth_model:
            env["TEAMBRAIN_SYNTH_MODEL"] = synth_model
        if key:
            env["OPENAI_API_KEY"] = key
        if synth == "oidc":
            env["TEAMBRAIN_OIDC_TOKEN_URL"] = oidc_url
            if oidc_body:
                env["TEAMBRAIN_OIDC_BODY"] = oidc_body
        if tls_insecure:
            env["TEAMBRAIN_TLS_INSECURE"] = "1"
    server = {"command": "team-brain"}
    if env:
        server["env"] = env
    return env, {"mcpServers": {"team-brain": server}}


def main(argv=None):
    print("team-brain init — configure the MCP server for your IDE\n")
    db = _ask("Shared Postgres DSN (blank = local SQLite, solo trial)")
    namespace = _ask("Team namespace (your team/project scope, e.g. proset, payments)")
    embed = _ask("Embedder: local / openai / none", "local") if db else "none"

    synth = _ask("Synthesis: none / claude / openai / oidc-gateway", "none").lower()
    synth = {"oidc-gateway": "oidc"}.get(synth, synth)
    if synth not in ("claude", "openai", "oidc"):
        synth = ""
    key = synth_url = synth_model = oidc_url = oidc_body = ""
    tls_insecure = False
    if synth == "claude":
        key = _ask("ANTHROPIC_API_KEY")
    elif synth in ("openai", "oidc"):
        synth_url = _ask("Gateway base URL (ends in /v1)",
                         "http://localhost:11434/v1")
        synth_model = _ask("Model name", "llama3.1")
        if synth == "oidc":
            oidc_url = _ask("OIDC token URL (the issuer's /token endpoint)")
            oidc_body = _ask(
                "OIDC token request JSON",
                '{"tenant_id":"team-brain","user_id":"team-brain","roles":["admin"]}')
        else:
            key = _ask("API key (blank if the endpoint needs none)")
        tls_insecure = _yes("Accept self-signed TLS certs (TEST envs only)?")

    _env, block = build_config(db, embed, synth, key, namespace,
                               synth_url=synth_url, synth_model=synth_model,
                               oidc_url=oidc_url, oidc_body=oidc_body,
                               tls_insecure=tls_insecure)
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
