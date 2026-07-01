"""OIDC synth front: token fetch, TTL cache, refresh, delegation."""
from __future__ import annotations

import io
import json

import pytest

from teambrain import synth_oidc, synth_openai


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def issuer(monkeypatch):
    """Fake OIDC issuer: counts calls, returns tok-<n>."""
    calls = {"n": 0, "bodies": []}

    def fake_urlopen(req, timeout=None, context=None):
        calls["n"] += 1
        calls["bodies"].append(json.loads(req.data.decode()))
        return FakeResponse(json.dumps({"access_token": f"tok-{calls['n']}"}).encode())

    monkeypatch.setattr(synth_oidc.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setenv("TEAMBRAIN_OIDC_TOKEN_URL", "https://issuer.test/token")
    monkeypatch.setenv("TEAMBRAIN_OIDC_BODY",
                       '{"tenant_id":"t1","user_id":"u1","roles":["admin"]}')
    monkeypatch.setattr(synth_oidc, "_TOK", {"value": None, "expires": 0.0})
    return calls


def test_token_fetch_and_cache(issuer):
    assert synth_oidc._token() == "tok-1"
    assert synth_oidc._token() == "tok-1"          # cached, no second call
    assert issuer["n"] == 1
    assert issuer["bodies"][0]["tenant_id"] == "t1"


def test_token_refresh_after_expiry(issuer):
    assert synth_oidc._token() == "tok-1"
    synth_oidc._TOK["expires"] = 0.0               # simulate the hour passing
    assert synth_oidc._token() == "tok-2"
    assert issuer["n"] == 2


def test_synth_delegates_with_fresh_bearer(issuer, monkeypatch):
    seen = {}

    def fake_synth(query, role, profile, rows):
        import os
        seen["key"] = os.environ.get("OPENAI_API_KEY")
        return "answer"

    monkeypatch.setattr(synth_openai, "synth", fake_synth)
    assert synth_oidc.synth("q", "developer", {}, [{"title": "t"}]) == "answer"
    assert seen["key"] == "tok-1"                  # bearer injected before the call


def test_issuer_down_raises_so_assist_falls_back(monkeypatch):
    monkeypatch.setenv("TEAMBRAIN_OIDC_TOKEN_URL", "https://issuer.test/token")
    monkeypatch.setattr(synth_oidc, "_TOK", {"value": None, "expires": 0.0})
    monkeypatch.setattr(synth_oidc.urllib.request, "urlopen",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("down")))
    with pytest.raises(OSError):
        synth_oidc.synth("q", "developer", {}, [{"title": "t"}])
    # assist._synthesize catches hook exceptions and falls back to extractive.
