"""OIDC-token front for OpenAI-compatible enterprise LLM gateways.

Many company AI hubs (e.g. an Envoy gateway in front of Llama) speak the
OpenAI chat API but authenticate with a **short-lived bearer token** from an
OIDC issuer instead of a static key — a static ``OPENAI_API_KEY`` goes stale
within the hour. This module fetches the token (a JSON POST to the issuer),
caches it, refreshes before expiry, and delegates the actual call to
:mod:`teambrain.synth_openai`.

Wire it in::

    export TEAMBRAIN_SYNTH=teambrain.synth_oidc:synth
    export TEAMBRAIN_CODE_SUMMARY=teambrain.synth_oidc:summarize_code
    export OPENAI_BASE_URL=https://<gateway-host>/v1
    export TEAMBRAIN_SYNTH_MODEL=meta-llama/llama-3.1-8b-instruct
    export TEAMBRAIN_OIDC_TOKEN_URL=https://<issuer-host>/token
    export TEAMBRAIN_OIDC_BODY='{"tenant_id":"team-brain","user_id":"team-brain","roles":["admin"]}'
    # test environments with self-signed certs ONLY:
    export TEAMBRAIN_TLS_INSECURE=1

``TEAMBRAIN_OIDC_TTL`` (seconds, default 3000) controls the refresh margin —
50 minutes for the usual 1-hour token. The issuer must return
``{"access_token": "..."}``.

Failure mode: if the token fetch fails, the hook raises and ``assist`` falls
back to the extractive answer with a visible warning — the read path never
hard-fails on a dead issuer.
"""
from __future__ import annotations

import json
import os
import ssl
import time
import urllib.request

from . import synth_openai

_TOK = {"value": None, "expires": 0.0}


def _token() -> str:
    now = time.time()
    if _TOK["value"] and now < _TOK["expires"]:
        return _TOK["value"]
    url = os.environ["TEAMBRAIN_OIDC_TOKEN_URL"]
    body = (os.environ.get("TEAMBRAIN_OIDC_BODY") or "{}").encode()
    req = urllib.request.Request(url, data=body,
                                 headers={"Content-Type": "application/json"})
    ctx = (ssl._create_unverified_context()
           if os.environ.get("TEAMBRAIN_TLS_INSECURE") == "1" else None)
    with urllib.request.urlopen(req, timeout=30, context=ctx) as r:
        _TOK["value"] = json.loads(r.read().decode())["access_token"]
    _TOK["expires"] = now + float(os.environ.get("TEAMBRAIN_OIDC_TTL") or 3000)
    return _TOK["value"]


def _with_token(fn, *args):
    # synth_openai._chat reads the bearer from OPENAI_API_KEY at call time,
    # so refreshing it here keeps every downstream call authenticated.
    os.environ["OPENAI_API_KEY"] = _token()
    return fn(*args)


def synth(query, role, profile, rows) -> str:
    """``TEAMBRAIN_SYNTH`` hook — cited answer via the OIDC-fronted gateway."""
    return _with_token(synth_openai.synth, query, role, profile, rows)


def summarize_code(code: str, path: str) -> str:
    """``TEAMBRAIN_CODE_SUMMARY`` hook — code→business rules via the gateway."""
    return _with_token(synth_openai.summarize_code, code, path)
