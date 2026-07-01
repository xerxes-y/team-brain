"""Every endpoint is env-configurable: embed server, GitHub Enterprise."""
from __future__ import annotations

import pytest

from teambrain.embed import OpenAIEmbedder
from teambrain.connectors.pr import GitHubClient


def test_embed_url_separate_from_chat_gateway(monkeypatch):
    monkeypatch.setenv("OPENAI_BASE_URL", "https://llm-gateway.internal/v1")
    monkeypatch.setenv("TEAMBRAIN_EMBED_BASE_URL", "http://embed.internal:8080/v1")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    e = OpenAIEmbedder()
    assert e._base == "http://embed.internal:8080/v1"    # not the chat gateway


def test_embed_falls_back_to_openai_base_url(monkeypatch):
    monkeypatch.setenv("OPENAI_BASE_URL", "http://localhost:11434/v1")
    monkeypatch.delenv("TEAMBRAIN_EMBED_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    e = OpenAIEmbedder()
    assert e._base == "http://localhost:11434/v1"
    assert e._key == ""                                  # local server: keyless OK


def test_embed_key_precedence_and_openai_requires_key(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "shared")
    monkeypatch.setenv("TEAMBRAIN_EMBED_API_KEY", "embed-only")
    monkeypatch.setenv("TEAMBRAIN_EMBED_BASE_URL", "http://embed.internal/v1")
    assert OpenAIEmbedder()._key == "embed-only"

    monkeypatch.delenv("TEAMBRAIN_EMBED_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("TEAMBRAIN_EMBED_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    with pytest.raises(RuntimeError):                    # api.openai.com needs a key
        OpenAIEmbedder()


def test_github_enterprise_base_url(monkeypatch):
    monkeypatch.setenv("GITHUB_BASE_URL", "https://ghe.acme.com/api/v3/")
    assert GitHubClient().base_url == "https://ghe.acme.com/api/v3"
    monkeypatch.delenv("GITHUB_BASE_URL", raising=False)
    assert GitHubClient().base_url == "https://api.github.com"
    assert GitHubClient(base_url="https://x.test/api").base_url == "https://x.test/api"
