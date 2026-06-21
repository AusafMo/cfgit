# Copyright 2026 Mohammad Ausaf. Licensed under the Apache License, Version 2.0.
"""Identity resolution for cfgit.

The short fingerprint is for display only. Authentication always uses either a
database-authenticated principal or a full SHA-256 token hash comparison.
"""
from __future__ import annotations

from dataclasses import dataclass
import getpass
import hashlib
import hmac
import os
import subprocess
from typing import Any

from cfg.core.config import EnvConfig, IdentityConfig


MIN_TOKEN_LENGTH = 8


class IdentityError(ValueError):
    """The environment requires verified identity and cfgit could not prove it."""


@dataclass(frozen=True)
class Identity:
    author: str
    mode: str
    source: str
    authenticated: bool
    fingerprint: str
    principal: str | None = None
    credential: str | None = None

    @property
    def display(self) -> str:
        return f"{self.author}#{self.fingerprint}"

    def history_meta(self) -> dict[str, Any]:
        meta: dict[str, Any] = {
            "mode": self.mode,
            "author": self.author,
            "source": self.source,
            "authenticated": self.authenticated,
            "fingerprint": self.fingerprint,
        }
        if self.principal:
            meta["principal"] = self.principal
        if self.credential:
            meta["credential"] = self.credential
        return meta


def resolve_identity(
    env: EnvConfig,
    adapter: Any,
    *,
    explicit_author: str | None = None,
) -> Identity:
    cfg = env.identity
    if cfg.mode == "open":
        return self_asserted_identity(resolve_self_asserted_author(explicit_author), cfg=cfg)

    errors: list[str] = []
    for source in cfg.sources:
        if source == "token":
            identity, error = _identity_from_token(cfg, explicit_author=explicit_author)
        elif source == "db_principal":
            identity, error = _identity_from_db_principal(cfg, adapter, explicit_author=explicit_author)
        else:  # config validation should prevent this
            identity, error = None, f"unsupported identity source {source}"
        if identity is not None:
            return identity
        if error:
            errors.append(error)

    detail = "; ".join(errors) if errors else "no configured identity source produced a verified identity"
    raise IdentityError(
        f"{env.name} requires {cfg.mode} identity, but cfgit could not verify the caller: {detail}"
    )


def resolve_self_asserted_author(explicit: str | None = None) -> str:
    if explicit:
        return explicit
    if os.environ.get("CFG_AUTHOR"):
        return os.environ["CFG_AUTHOR"]
    try:
        out = subprocess.check_output(["git", "config", "user.email"], text=True).strip()
        if out:
            return out
    except Exception:
        pass
    return getpass.getuser()


def self_asserted_identity(author: str, *, cfg: IdentityConfig) -> Identity:
    return _identity(
        author=author,
        cfg=cfg,
        mode=cfg.mode,
        source="self_asserted",
        authenticated=False,
    )


def hash_token(token: str) -> str:
    return "sha256:" + hashlib.sha256(token.encode("utf-8")).hexdigest()


def token_fingerprint(token_hash: str, *, chars: int = 5) -> str:
    return _hash_hex(token_hash)[:chars]


def _identity_from_token(
    cfg: IdentityConfig,
    *,
    explicit_author: str | None,
) -> tuple[Identity | None, str | None]:
    raw = os.environ.get(cfg.token_env)
    if not raw:
        return None, f"{cfg.token_env} is not set"
    token = raw.strip()
    if len(token) < MIN_TOKEN_LENGTH:
        return None, (
            f"{cfg.token_env} is too short; identity tokens need at least {MIN_TOKEN_LENGTH} "
            "characters, and short fingerprints are display-only"
        )
    hashed = hash_token(token)
    for item in cfg.tokens:
        if hmac.compare_digest(hashed, item.token_hash):
            _reject_author_mismatch(explicit_author, item.author)
            credential = item.name or f"token:{token_fingerprint(item.token_hash, chars=cfg.fingerprint_chars)}"
            return (
                Identity(
                    author=item.author,
                    mode=cfg.mode,
                    source="token",
                    authenticated=True,
                    fingerprint=token_fingerprint(item.token_hash, chars=cfg.fingerprint_chars),
                    principal=credential,
                    credential=credential,
                ),
                None,
            )
    return None, f"{cfg.token_env} did not match any configured identity token hash"


def _identity_from_db_principal(
    cfg: IdentityConfig,
    adapter: Any,
    *,
    explicit_author: str | None,
) -> tuple[Identity | None, str | None]:
    getter = getattr(adapter, "authenticated_principal", None)
    if not callable(getter):
        return None, f"{adapter.backend_name()} adapter does not expose an authenticated DB principal"
    principal = getter()
    if not principal:
        return None, f"{adapter.backend_name()} connection has no authenticated DB principal"
    author = cfg.principal_map.get(principal) or cfg.principal_map.get(principal.lower()) or principal
    _reject_author_mismatch(explicit_author, author)
    return (
        _identity(
            author=author,
            cfg=cfg,
            mode=cfg.mode,
            source="db_principal",
            authenticated=True,
            principal=principal,
        ),
        None,
    )


def _identity(
    *,
    author: str,
    cfg: IdentityConfig,
    mode: str,
    source: str,
    authenticated: bool,
    principal: str | None = None,
    credential: str | None = None,
) -> Identity:
    return Identity(
        author=author,
        mode=mode,
        source=source,
        authenticated=authenticated,
        principal=principal,
        credential=credential,
        fingerprint=_fingerprint(author=author, source=source, principal=principal, chars=cfg.fingerprint_chars),
    )


def _fingerprint(*, author: str, source: str, principal: str | None, chars: int) -> str:
    value = "\0".join([source, author, principal or ""])
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:chars]


def _reject_author_mismatch(explicit_author: str | None, verified_author: str) -> None:
    if explicit_author and explicit_author.strip().lower() != verified_author.strip().lower():
        raise IdentityError(
            f"--author {explicit_author} does not match verified identity {verified_author}; "
            "verified modes do not accept self-asserted author strings"
        )


def _hash_hex(value: str) -> str:
    return value.lower().removeprefix("sha256:")
