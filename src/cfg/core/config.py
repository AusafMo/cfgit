# Copyright 2026 Mohammad Ausaf. Licensed under the Apache License, Version 2.0.
"""Project configuration loader for cfgit.

This is the single place that maps user-facing .cfg.toml names to internal
engine names. Keep it DB-neutral.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import os
from typing import Any

try:  # pragma: no cover - py311+ path is normal
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore[no-redef]


@dataclass(frozen=True)
class CollectionConfig:
    name: str
    id_field: str
    live_when: dict[str, Any] = field(default_factory=dict)
    ignore_fields: tuple[str, ...] = ()
    ignore_patterns: tuple[str, ...] = ()
    ignore_paths: tuple[str, ...] = ()
    secret_fields: tuple[str, ...] = ()


@dataclass(frozen=True)
class HistoryConfig:
    history_collection: str = "config_history"
    heads_collection: str = "config_heads"


@dataclass(frozen=True)
class BranchesConfig:
    enabled: bool = False
    refs_collection: str = "cfgit_refs"
    default_branch: str = "main"


@dataclass(frozen=True)
class PermissionConfig:
    mode: str = "open"
    admins: tuple[str, ...] = ()
    writers: tuple[str, ...] = ()
    admin_actions: tuple[str, ...] = ()


@dataclass(frozen=True)
class IdentityTokenConfig:
    author: str
    token_hash: str
    name: str | None = None


@dataclass(frozen=True)
class IdentityConfig:
    mode: str = "open"
    sources: tuple[str, ...] = ("db_principal", "token")
    token_env: str = "CFGIT_IDENTITY_TOKEN"
    tokens: tuple[IdentityTokenConfig, ...] = ()
    principal_map: dict[str, str] = field(default_factory=dict)
    fingerprint_chars: int = 5


@dataclass(frozen=True)
class ConnectionsConfig:
    enabled: bool = False
    ai_provider: str = "openai"
    share_with_ai: tuple[str, ...] = ()
    warn_level: str = "none"
    links: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class SecretsConfig:
    block_fields: tuple[str, ...] = ()
    block_values: tuple[str, ...] = ()
    on_match: str = "refuse"


@dataclass(frozen=True)
class EnvConfig:
    name: str
    database: str
    uri: str
    db: str
    runtime_uri: str | None = None
    runtime_db: str | None = None
    history_uri: str | None = None
    history_db: str | None = None
    needs_approval: bool = False
    identity: IdentityConfig = field(default_factory=IdentityConfig)
    permissions: PermissionConfig = field(default_factory=PermissionConfig)


@dataclass(frozen=True)
class ProjectConfig:
    name: str
    path: Path
    history: HistoryConfig
    collections: tuple[CollectionConfig, ...]
    envs: dict[str, EnvConfig]
    branches: BranchesConfig = field(default_factory=BranchesConfig)
    author_from: str = "git"
    connections: ConnectionsConfig = field(default_factory=ConnectionsConfig)
    secrets: SecretsConfig = field(default_factory=SecretsConfig)

    def collection(self, name: str) -> CollectionConfig:
        for coll in self.collections:
            if coll.name == name:
                return coll
        raise KeyError(f"unknown collection: {name}")


def load_config(path: str | Path | None = None) -> ProjectConfig:
    cfg_path = _resolve_config_path(path)
    with cfg_path.open("rb") as f:
        raw = tomllib.load(f)

    collections = tuple(_load_collection(item) for item in raw.get("collection", []))
    if not collections:
        raise ValueError("config has no [[collection]] entries")

    envs: dict[str, EnvConfig] = {}
    for name, data in (raw.get("env") or {}).items():
        permission_raw, identity_raw = _split_identity_permissions(data)
        envs[name] = EnvConfig(
            name=name,
            database=str(data.get("database", "")),
            uri=_resolve_uri(str(data.get("uri", "")), env_name=name),
            db=str(data.get("db", "")),
            runtime_uri=_resolve_optional_uri(data.get("runtime_uri"), env_name=name),
            runtime_db=str(data["runtime_db"]) if data.get("runtime_db") is not None else None,
            history_uri=_resolve_optional_uri(data.get("history_uri"), env_name=name),
            history_db=str(data["history_db"]) if data.get("history_db") is not None else None,
            needs_approval=bool(data.get("needs_approval", False)),
            identity=_load_identity(identity_raw),
            permissions=_load_permissions(permission_raw),
        )

    history_raw = raw.get("history") or {}
    branches_raw = raw.get("branches") or {}
    author_raw = raw.get("author") or {}
    connections_raw = raw.get("connections") or {}
    secrets_raw = raw.get("secrets") or {}
    return ProjectConfig(
        name=str((raw.get("project") or {}).get("name", cfg_path.parent.name)),
        path=cfg_path,
        history=HistoryConfig(
            history_collection=str(history_raw.get("history_collection", "config_history")),
            heads_collection=str(history_raw.get("heads_collection", "config_heads")),
        ),
        branches=_load_branches(branches_raw),
        collections=collections,
        envs=envs,
        author_from=str(author_raw.get("from", "git")),
        connections=_load_connections(connections_raw),
        secrets=_load_secrets(secrets_raw),
    )


def _resolve_config_path(path: str | Path | None) -> Path:
    if path is not None:
        return Path(path).expanduser().resolve()
    env_path = os.environ.get("CFG_CONFIG")
    if env_path:
        return Path(env_path).expanduser().resolve()
    # A local ./.cfg.toml always wins — this preserves prior behavior exactly.
    local = Path(".cfg.toml")
    if local.exists():
        return local.resolve()
    # Otherwise walk up from cwd toward the filesystem root. This only ADDS discovery where the
    # old code hard-failed; it never overrides a local config.
    cwd = Path.cwd()
    for parent in cwd.parents:
        candidate = parent / ".cfg.toml"
        if candidate.exists():
            return candidate.resolve()
    example = Path("examples/.cfg.toml")
    if example.exists():
        return example.resolve()
    raise FileNotFoundError("no .cfg.toml found (searched cwd and parent directories)")


def _load_collection(data: dict[str, Any]) -> CollectionConfig:
    ignore_patterns = (*data.get("ignore_patterns", ()), *data.get("ignore_globs", ()))
    secret_fields = (*data.get("secret_fields", ()), *data.get("strip_on_store", ()))
    return CollectionConfig(
        name=str(data["name"]),
        id_field=str(data["id_field"]),
        live_when=dict(data.get("live_when") or {}),
        ignore_fields=tuple(str(x) for x in data.get("ignore_fields", ())),
        ignore_patterns=tuple(str(x) for x in ignore_patterns),
        ignore_paths=tuple(str(x) for x in data.get("ignore_paths", ())),
        secret_fields=tuple(str(x) for x in secret_fields),
    )


def _load_branches(data: dict[str, Any]) -> BranchesConfig:
    default_branch = str(data.get("default_branch", "main")).strip() or "main"
    return BranchesConfig(
        enabled=bool(data.get("enabled", False)),
        refs_collection=str(data.get("refs_collection", "cfgit_refs")).strip() or "cfgit_refs",
        default_branch=default_branch,
    )


def _load_permissions(data: dict[str, Any]) -> PermissionConfig:
    mode = str(data.get("mode", "open"))
    if mode not in {"open", "restricted"}:
        raise ValueError("permissions.mode must be open or restricted")
    return PermissionConfig(
        mode=mode,
        admins=tuple(str(x) for x in data.get("admins", ())),
        writers=tuple(str(x) for x in data.get("writers", ())),
        admin_actions=tuple(str(x) for x in data.get("admin_actions", ())),
    )


def _load_identity(data: dict[str, Any]) -> IdentityConfig:
    mode = str(data.get("mode", "open")).lower()
    if mode not in {"open", "authenticated", "enforced"}:
        raise ValueError("identity.mode must be open, authenticated, or enforced")
    raw_sources = data.get("sources", data.get("source"))
    if raw_sources is None:
        sources = ("db_principal", "token")
    elif isinstance(raw_sources, str):
        sources = (raw_sources,)
    else:
        sources = tuple(str(item) for item in raw_sources)
    for source in sources:
        if source not in {"db_principal", "token"}:
            raise ValueError("identity.sources may contain only db_principal or token")
    fingerprint_chars = int(data.get("fingerprint_chars", 5))
    if not 4 <= fingerprint_chars <= 12:
        raise ValueError("identity.fingerprint_chars must be between 4 and 12")
    return IdentityConfig(
        mode=mode,
        sources=sources,
        token_env=str(data.get("token_env", "CFGIT_IDENTITY_TOKEN")),
        tokens=tuple(_load_identity_token(item) for item in _identity_token_items(data)),
        principal_map={str(k): str(v) for k, v in dict(data.get("principal_map") or {}).items()},
        fingerprint_chars=fingerprint_chars,
    )


def _load_identity_token(data: dict[str, Any]) -> IdentityTokenConfig:
    author = str(data.get("author") or "").strip()
    token_hash = str(data.get("sha256") or data.get("hash") or data.get("token_hash") or "").strip()
    if not author or not token_hash:
        raise ValueError("identity token entries require author and sha256/hash")
    return IdentityTokenConfig(
        author=author,
        token_hash=_normalize_token_hash(token_hash),
        name=str(data["name"]) if data.get("name") is not None else None,
    )


def _identity_token_items(data: dict[str, Any]) -> list[dict[str, Any]]:
    raw = data.get("tokens", data.get("token", ()))
    if isinstance(raw, dict):
        return [raw]
    return [dict(item) for item in raw]


def _normalize_token_hash(value: str) -> str:
    raw = value.lower()
    if raw.startswith("sha256:"):
        hex_value = raw[7:]
    else:
        hex_value = raw
        raw = f"sha256:{raw}"
    if len(hex_value) != 64 or any(ch not in "0123456789abcdef" for ch in hex_value):
        raise ValueError("identity token hash must be a sha256:<64 hex> value")
    return raw


def _split_identity_permissions(data: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    permissions = dict(data.get("permissions") or {})
    identity = dict(data.get("identity") or {})
    if data.get("identity_mode") is not None and identity.get("mode") is None:
        identity["mode"] = data["identity_mode"]

    permission_mode = str(permissions.get("mode", "open")).lower()
    if permission_mode in {"authenticated", "enforced"}:
        identity.setdefault("mode", permission_mode)
        permissions["mode"] = "restricted"
    return permissions, identity


def _load_connections(data: dict[str, Any]) -> ConnectionsConfig:
    return ConnectionsConfig(
        enabled=bool(data.get("enabled", False)),
        ai_provider=str(data.get("ai_provider", "openai")),
        share_with_ai=tuple(str(x) for x in data.get("share_with_ai", ())),
        warn_level=str(data.get("warn_level", "none")),
        links=tuple(dict(item) for item in data.get("links", ())),
    )


def _load_secrets(data: dict[str, Any]) -> SecretsConfig:
    on_match = str(data.get("on_match", "refuse"))
    if on_match not in {"refuse", "warn"}:
        raise ValueError("secrets.on_match must be refuse or warn")
    block_fields = (*data.get("block_fields", ()), *data.get("deny_field_globs", ()))
    block_values = (*data.get("block_values", ()), *data.get("deny_value_regex", ()))
    return SecretsConfig(
        block_fields=tuple(str(x) for x in block_fields),
        block_values=tuple(str(x) for x in block_values),
        on_match=on_match,
    )


def _resolve_uri(raw: str, *, env_name: str) -> str:
    if raw.startswith("env:"):
        key = raw[4:]
        value = os.environ.get(key)
        return value or ""
    return raw


def _resolve_optional_uri(raw: Any, *, env_name: str) -> str | None:
    if raw is None:
        return None
    return _resolve_uri(str(raw), env_name=env_name)
