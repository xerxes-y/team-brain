"""Shared test fixtures.

Tests run fully offline against memento's local SQLite store in a temp dir — no
Postgres, no network, no credentials. We point ``teambrain.store`` at an
isolated MemoryStore so each test session is hermetic.
"""
from __future__ import annotations

import pytest

from teambrain import store as _store


@pytest.fixture
def temp_store(tmp_path, monkeypatch):
    """A fresh memento SQLite store wired into teambrain.store for the test.

    Skips (rather than errors) when memento isn't importable — e.g. CI where the
    engine repo couldn't be fetched — so pure-logic tests still run."""
    _store._bootstrap_memento()
    try:
        import memento_memory
    except Exception:
        pytest.skip("memento (MemoryStorePG engine) not available")
    st = memento_memory.MemoryStore(db_path=str(tmp_path / "tb.sqlite3"))
    monkeypatch.setattr(_store, "_STORE", st)
    return st
