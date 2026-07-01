"""team-brain init: config building + the interactive flow writing a paste file."""
import builtins
import json
import os

from teambrain import init as tb_init


def test_build_config_solo_sqlite():
    env, block = tb_init.build_config(db="", embed="none", synth=False, key="")
    assert env == {}                                   # no env → SQLite fallback
    assert block["mcpServers"]["team-brain"] == {"command": "team-brain"}


def test_build_config_shared_postgres_with_synth():
    env, block = tb_init.build_config(
        db="postgresql://u:p@h/db", embed="local", synth=True, key="sk-x")
    assert env["MEMENTO_DB_URL"] == "postgresql://u:p@h/db"
    assert env["TEAMBRAIN_EMBED"] == "local"
    assert env["TEAMBRAIN_SYNTH"] == "teambrain.synth_claude:synth"
    assert env["ANTHROPIC_API_KEY"] == "sk-x"
    assert block["mcpServers"]["team-brain"]["env"] is env


def test_build_config_openai_gateway():
    env, _ = tb_init.build_config(
        db="postgresql://u:p@h/db", embed="local", synth="openai", key="",
        synth_url="http://llm.internal/v1", synth_model="llama3.1")
    assert env["TEAMBRAIN_SYNTH"] == "teambrain.synth_openai:synth"
    assert env["TEAMBRAIN_CODE_SUMMARY"] == "teambrain.synth_openai:summarize_code"
    assert env["OPENAI_BASE_URL"] == "http://llm.internal/v1"
    assert env["TEAMBRAIN_SYNTH_MODEL"] == "llama3.1"
    assert "OPENAI_API_KEY" not in env and "TEAMBRAIN_TLS_INSECURE" not in env


def test_build_config_oidc_gateway():
    env, _ = tb_init.build_config(
        db="postgresql://u:p@h/db", embed="local", synth="oidc", key="",
        synth_url="https://gw.test/v1", synth_model="meta-llama/llama-3.1-8b-instruct",
        oidc_url="https://issuer.test/token",
        oidc_body='{"tenant_id":"t"}', tls_insecure=True)
    assert env["TEAMBRAIN_SYNTH"] == "teambrain.synth_oidc:synth"
    assert env["TEAMBRAIN_OIDC_TOKEN_URL"] == "https://issuer.test/token"
    assert env["TEAMBRAIN_OIDC_BODY"] == '{"tenant_id":"t"}'
    assert env["TEAMBRAIN_TLS_INSECURE"] == "1"


def test_main_oidc_flow(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    answers = iter([
        "postgresql://u:p@h/db", "proset", "local",       # dsn, namespace, embedder
        "oidc-gateway", "https://gw.test/v1", "m-llama",  # synth, base url, model
        "https://issuer.test/token", "", "y",             # token url, body(default), tls
    ])
    monkeypatch.setattr(builtins, "input", lambda *_: next(answers))
    assert tb_init.main([]) == 0
    env = json.loads((tmp_path / "team-brain.mcp.json").read_text())[
        "mcpServers"]["team-brain"]["env"]
    assert env["TEAMBRAIN_SYNTH"] == "teambrain.synth_oidc:synth"
    assert env["TEAMBRAIN_OIDC_TOKEN_URL"] == "https://issuer.test/token"
    assert "tenant_id" in env["TEAMBRAIN_OIDC_BODY"]      # default body kept
    assert env["TEAMBRAIN_TLS_INSECURE"] == "1"


def test_main_writes_paste_file(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    answers = iter(["postgresql://u:p@h/db", "proset", "local", "n"])  # dsn, namespace, embedder, synth?=n
    monkeypatch.setattr(builtins, "input", lambda *_: next(answers))

    assert tb_init.main([]) == 0
    out = tmp_path / "team-brain.mcp.json"
    assert out.exists()
    cfg = json.loads(out.read_text())
    env = cfg["mcpServers"]["team-brain"]["env"]
    assert env["MEMENTO_DB_URL"] == "postgresql://u:p@h/db"
    assert env["TEAMBRAIN_NAMESPACE"] == "proset"
    assert "start saving team-brain" in capsys.readouterr().out
