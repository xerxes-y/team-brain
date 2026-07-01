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
