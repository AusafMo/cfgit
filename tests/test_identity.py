from __future__ import annotations

from pathlib import Path

import pytest

from cfg.cli.main import main
from cfg.core.config import EnvConfig, IdentityConfig, IdentityTokenConfig, load_config
from cfg.core.identity import IdentityError, hash_token, resolve_identity, token_fingerprint


class FakeAdapter:
    def __init__(self, principal: str | None = None):
        self.principal = principal

    def authenticated_principal(self) -> str | None:
        return self.principal

    def backend_name(self) -> str:
        return "fake"


def test_open_identity_is_self_asserted_with_display_fingerprint() -> None:
    env = EnvConfig(name="dev", database="fake", uri="", db="demo")

    identity = resolve_identity(env, FakeAdapter(), explicit_author="alice@example.com")

    assert identity.author == "alice@example.com"
    assert identity.authenticated is False
    assert identity.source == "self_asserted"
    assert identity.display.startswith("alice@example.com#")
    assert len(identity.fingerprint) == 5


def test_authenticated_identity_accepts_memorable_token_by_full_hash(monkeypatch: pytest.MonkeyPatch) -> None:
    token = "imkanyewest"
    env = EnvConfig(
        name="prod",
        database="fake",
        uri="",
        db="demo",
        identity=IdentityConfig(
            mode="authenticated",
            sources=("token",),
            tokens=(IdentityTokenConfig(author="alice@example.com", token_hash=hash_token(token), name="alice-main"),),
        ),
    )
    monkeypatch.setenv("CFGIT_IDENTITY_TOKEN", token)

    identity = resolve_identity(env, FakeAdapter())

    assert identity.author == "alice@example.com"
    assert identity.authenticated is True
    assert identity.source == "token"
    assert identity.principal == "alice-main"
    assert identity.history_meta()["fingerprint"] == identity.fingerprint


def test_authenticated_identity_never_accepts_short_fingerprint_as_token(monkeypatch: pytest.MonkeyPatch) -> None:
    token = "imkanyewest"
    env = EnvConfig(
        name="prod",
        database="fake",
        uri="",
        db="demo",
        identity=IdentityConfig(
            mode="authenticated",
            sources=("token",),
            tokens=(IdentityTokenConfig(author="alice@example.com", token_hash=hash_token(token)),),
        ),
    )
    monkeypatch.setenv("CFGIT_IDENTITY_TOKEN", token_fingerprint(hash_token(token), chars=5))

    with pytest.raises(IdentityError, match="too short"):
        resolve_identity(env, FakeAdapter())


def test_verified_identity_rejects_author_string_theft(monkeypatch: pytest.MonkeyPatch) -> None:
    env = EnvConfig(
        name="prod",
        database="fake",
        uri="",
        db="demo",
        identity=IdentityConfig(
            mode="authenticated",
            sources=("token",),
            tokens=(IdentityTokenConfig(author="alice@example.com", token_hash=hash_token("imkanyewest")),),
        ),
    )
    monkeypatch.setenv("CFGIT_IDENTITY_TOKEN", "imkanyewest")

    with pytest.raises(IdentityError, match="does not match verified identity"):
        resolve_identity(env, FakeAdapter(), explicit_author="bob@example.com")


def test_authenticated_identity_can_come_from_db_principal() -> None:
    env = EnvConfig(
        name="prod",
        database="fake",
        uri="",
        db="demo",
        identity=IdentityConfig(
            mode="authenticated",
            sources=("db_principal",),
            principal_map={"alice_db": "alice@example.com"},
        ),
    )

    identity = resolve_identity(env, FakeAdapter("alice_db"))

    assert identity.author == "alice@example.com"
    assert identity.authenticated is True
    assert identity.source == "db_principal"
    assert identity.principal == "alice_db"


def test_permissions_mode_authenticated_alias_sets_identity_mode(tmp_path: Path) -> None:
    path = tmp_path / ".cfg.toml"
    path.write_text(
        """
        [project]
        name = "demo"

        [[collection]]
        name = "records"
        id_field = "id"

        [env.prod]
        database = "mongo"
        uri = "env:CFGIT_TEST_MONGO"
        db = "demo"

        [env.prod.permissions]
        mode = "authenticated"
        admins = ["alice@example.com"]
        """,
        encoding="utf-8",
    )

    project = load_config(path)

    assert project.envs["prod"].identity.mode == "authenticated"
    assert project.envs["prod"].permissions.mode == "restricted"


def test_identity_token_hash_config_validation(tmp_path: Path) -> None:
    path = tmp_path / ".cfg.toml"
    path.write_text(
        """
        [[collection]]
        name = "records"
        id_field = "id"

        [env.prod]
        database = "mongo"
        uri = "env:CFGIT_TEST_MONGO"
        db = "demo"

        [env.prod.identity]
        mode = "authenticated"
        sources = ["token"]
        tokens = [
          { author = "alice@example.com", sha256 = "sha256:abc" },
        ]
        """,
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="sha256"):
        load_config(path)


def test_identity_hash_command_does_not_need_config(capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["--json", "identity-hash", "imkanyewest"])

    assert code == 0
    out = capsys.readouterr().out
    assert '"sha256": "sha256:' in out
    assert '"fingerprint": "' in out
