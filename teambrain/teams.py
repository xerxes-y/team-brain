"""Microsoft Teams surface: an Outgoing-Webhook bridge onto the read path.

Connectors write *into* the brain; this is the inverse — a thin, **read-only**
surface that lets anyone in a Teams channel ask the brain and get the same
cited, ACL-gated answer the IDE tools return. With synthesis pointed at an
internal LLM (``TEAMBRAIN_SYNTH=teambrain.synth_openai:synth`` +
``OPENAI_BASE_URL``), nothing leaves the network.

Wiring (Teams side): add an **Outgoing Webhook** to the team (Teams > Apps >
Manage your apps > Create an outgoing webhook), point it at this server's
public HTTPS URL (put a reverse proxy in front; this process speaks plain
HTTP), and copy the security token Teams generates into
``TEAMBRAIN_TEAMS_SECRET``. Users then ask with ``@<webhook-name> <question>``.

Security model — fail closed twice:

  * every request must carry Teams' ``Authorization: HMAC <sig>`` header,
    verified against the shared secret over the raw body; a missing or wrong
    signature is rejected. An unconfigured secret rejects everything.
  * an outgoing webhook cannot tell us the asker's AD groups, so the asker is
    **unknown** to the ACL: only public memories answer. To grant a channel
    more, set ``TEAMBRAIN_TEAMS_GROUPS`` — a deliberate, channel-level grant
    (run one bridge per channel/team). Never wider than the channel deserves.

Role: answers default to ``TEAMBRAIN_TEAMS_ROLE`` (or ``developer``); an asker
can override per question with a prefix — ``@brain as tester: what should I
test for PROSET-9913?``.

Env: TEAMBRAIN_TEAMS_SECRET (required), TEAMBRAIN_NAMESPACE,
TEAMBRAIN_TEAMS_ROLE, TEAMBRAIN_TEAMS_GROUPS (comma-separated),
TEAMBRAIN_TEAMS_PORT (default 8085).

Run: ``python3 -m teambrain.teams``  (stdlib only — no Bot Framework SDK; if
you outgrow one-channel webhooks, an Azure Bot registration is the upgrade
path, same ``assist()`` underneath).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import os
import re

from .assist import assist

_AT_RE = re.compile(r"(?is)<at>.*?</at>")             # the bot mention, name included
_TAG_RE = re.compile(r"<[^>]+>")                      # any other HTML Teams sends
_ROLE_RE = re.compile(r"(?is)^\s*as\s+([a-z]+)\s*[:,]\s*(.+)$")


def verify_signature(body: bytes, auth_header, secret_b64) -> bool:
    """Teams outgoing-webhook HMAC: base64(HMAC-SHA256(base64decode(secret),
    raw_body)) must equal the ``Authorization: HMAC <sig>`` header. Any missing
    piece => False (fail closed)."""
    if not secret_b64 or not auth_header:
        return False
    try:
        key = base64.b64decode(secret_b64)
    except Exception:
        return False
    parts = str(auth_header).split(None, 1)
    if len(parts) != 2 or parts[0].upper() != "HMAC":
        return False
    digest = base64.b64encode(hmac.new(key, body, hashlib.sha256).digest()).decode()
    return hmac.compare_digest(digest, parts[1].strip())


def clean_text(raw) -> str:
    """Drop the bot's <at>mention</at> (name included — it isn't part of the
    question) and any other markup Teams sends."""
    text = _TAG_RE.sub(" ", _AT_RE.sub(" ", raw or ""))
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def parse_question(text: str, default_role: str):
    """``as tester: what breaks?`` -> ('tester', 'what breaks?'); no prefix ->
    (default_role, text)."""
    m = _ROLE_RE.match(text or "")
    if m:
        return m.group(1).lower(), m.group(2).strip()
    return default_role, (text or "").strip()


def format_answer(res: dict) -> str:
    """The MCP tool's answer shape, in Teams-friendly markdown."""
    cites = "\n".join(
        f"[{c['n']}] {c['title']}" + (f" — {c['url']}" if c.get("url") else "")
        for c in res.get("citations") or [])
    out = res.get("answer") or "(no answer)"
    if cites:
        out += f"\n\n**Sources**\n{cites}"
    if res.get("hidden_by_acl"):
        out += f"\n\n_({res['hidden_by_acl']} memory(ies) hidden by ACL)_"
    return out


def _msg(text: str) -> dict:
    return {"type": "message", "text": text}


def handle(body: bytes, auth_header, *, secret=None, namespace=None,
           default_role=None, groups=None):
    """One webhook request -> (http_status, teams_message_dict). Pure logic —
    the HTTP server below and the tests both call this."""
    secret = secret if secret is not None else os.environ.get("TEAMBRAIN_TEAMS_SECRET")
    if not verify_signature(body, auth_header, secret):
        return 401, _msg("⛔ team-brain: signature verification failed.")

    try:
        payload = json.loads(body.decode("utf-8"))
    except Exception:
        return 200, _msg("team-brain: could not parse the message.")

    default_role = default_role or os.environ.get("TEAMBRAIN_TEAMS_ROLE") or "developer"
    role, question = parse_question(clean_text(payload.get("text")), default_role)
    if not question:
        return 200, _msg("Ask me something — e.g. `@team-brain why do we match "
                         "on the counterparty account?` or `@team-brain as "
                         "tester: what should I test for PROSET-9913?`")

    namespace = namespace or os.environ.get("TEAMBRAIN_NAMESPACE")
    if groups is None:
        raw = os.environ.get("TEAMBRAIN_TEAMS_GROUPS", "")
        groups = [g.strip() for g in raw.split(",") if g.strip()] or None

    try:
        res = assist(question, role, namespace, asker_groups=groups)
    except ValueError as exc:                       # unknown role
        return 200, _msg(f"team-brain: {exc}")
    return 200, _msg(format_answer(res))


# ── stdlib HTTP server ────────────────────────────────────────────────────────

def main():
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            status, msg = handle(body, self.headers.get("Authorization"))
            data = json.dumps(msg).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, fmt, *args):          # quiet; no question text in logs
            pass

    port = int(os.environ.get("TEAMBRAIN_TEAMS_PORT") or 8085)
    if not os.environ.get("TEAMBRAIN_TEAMS_SECRET"):
        print("TEAMBRAIN_TEAMS_SECRET is not set — every request will be "
              "rejected (fail closed). Copy the token Teams shows when you "
              "create the outgoing webhook.")
    print(f"team-brain Teams bridge on :{port} "
          f"(namespace={os.environ.get('TEAMBRAIN_NAMESPACE') or '(default)'})")
    HTTPServer(("", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
