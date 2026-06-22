from __future__ import annotations

import pytest

from cfg.adapters.base import HistoryEnvMismatch


def test_postgres_query_history_reports_env_mismatch() -> None:
    pytest.importorskip("psycopg")
    from cfg.adapters.postgres import PostgresAdapter

    adapter = object.__new__(PostgresAdapter)
    adapter.env_name = "dev"
    adapter.history_table = '"cfgit_history"'
    adapter.heads_table = '"cfgit_heads"'
    adapter.conn = _FakeConn([[], [{"env": "prod"}], []])

    with pytest.raises(HistoryEnvMismatch) as caught:
        adapter.query_history(collection="demo", record_id="alpha")

    message = str(caught.value)
    assert "demo:alpha" in message
    assert "env='dev'" in message
    assert "prod" in message
    assert len(adapter.conn.executed) == 3


class _FakeConn:
    def __init__(self, results: list[list[dict[str, str]]]):
        self.results = results
        self.executed: list[tuple[str, list[str]]] = []

    def cursor(self):
        return _FakeCursor(self)


class _FakeCursor:
    def __init__(self, conn: _FakeConn):
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *_exc_info) -> None:
        return None

    def execute(self, sql: str, params: list[str]) -> None:
        self.conn.executed.append((sql, params))

    def fetchall(self):
        return self.conn.results.pop(0)
