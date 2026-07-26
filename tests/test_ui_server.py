from __future__ import annotations

import socket

import pytest

from cfg.cli.main import _has_option
from cfg.ui.server import DEFAULT_HOST, _bind_server


def test_has_option_detects_space_and_equals_forms() -> None:
    assert _has_option(["ui", "--port", "9000"], "--port")
    assert _has_option(["ui", "--port=9000"], "--port")
    assert not _has_option(["ui", "--host", "127.0.0.1"], "--port")


def test_bind_server_fails_when_explicit_port_is_busy() -> None:
    with _busy_port() as port:
        with pytest.raises(OSError, match="already in use"):
            _bind_server(
                host=DEFAULT_HOST,
                port=port,
                config_file=None,
                env="dev",
                author=None,
                allow_port_fallback=False,
            )


def test_bind_server_falls_back_when_port_is_not_explicit() -> None:
    with _busy_port() as port:
        server = _bind_server(
            host=DEFAULT_HOST,
            port=port,
            config_file=None,
            env="dev",
            author=None,
            allow_port_fallback=True,
        )
        try:
            assert server.server_address[1] != port
        finally:
            server.server_close()


def test_ui_contains_branch_and_pr_controls() -> None:
    from cfg.ui.server import UI_HTML

    for marker in ('id="branch"', 'id="newBranch"', 'id="draftCommit"', 'id="openPr"', 'id="mergePr"'):
        assert marker in UI_HTML
    assert "branch_create" in UI_HTML
    assert "pr_merge" in UI_HTML


def test_ui_contains_recent_activity_history() -> None:
    from cfg.ui.server import UI_HTML

    assert "Recent activity" in UI_HTML
    assert "recent_history" in UI_HTML
    assert "live drift" in UI_HTML
    assert "Select a recent entry" in UI_HTML


def test_ui_contains_agent_coordination_manager() -> None:
    from cfg.ui.server import UI_HTML

    for marker in ('id="agents"', "/api/agent/state", "/api/agent/action", "Agent coordination"):
        assert marker in UI_HTML
    assert "end_session" in UI_HTML
    assert "close_intent" in UI_HTML
    assert "release" in UI_HTML


def test_ui_contains_remedy_card_surface() -> None:
    from cfg.ui.server import UI_HTML

    # the self-teaching remedy card + its render/copy wiring exist
    assert 'id="remedy"' in UI_HTML
    assert "function showRemedy" in UI_HTML
    assert "res.next" in UI_HTML  # after()/afterBranch() surface the envelope's next block
    assert "navigator.clipboard" in UI_HTML  # commands are copy-to-clipboard


class _busy_port:
    def __enter__(self) -> int:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((DEFAULT_HOST, 0))
        self.sock.listen(1)
        return int(self.sock.getsockname()[1])

    def __exit__(self, *_exc_info) -> None:
        self.sock.close()
