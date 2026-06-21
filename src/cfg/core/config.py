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
class PermissionConfig:
    mode: str = "open"
    admins: tuple[str, ...] = ()
    writers: tuple[str, ...] = ()
    admin_actions: tuple[str, ...] = ()


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
    permissions: PermissionConfig = field(default_factory=PermissionConfig)


@dataclass(frozen=True)
class ProjectConfig:
    name: str
    path: Path
    history: HistoryConfig
    collections: tuple[CollectionConfig, ...]
    envs: dict[str, EnvConfig]
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
            permissions=_load_permissions(data.get("permissions") or {}),
        )

    history_raw = raw.get("history") or {}
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
        collections=collections,
        envs=envs,
        author_from=str(author_raw.get("from", "git")),
        connections=_load_connections(connections_raw),
        secrets=_load_secrets(secrets_raw),
    )


def _resolve_config_path(path: str | Path | None) -> Path:
    if path is not None:
        return Path(path).expanduser().resolve()
    local = Path(".cfg.toml")
    if local.exists():
        return local.resolve()
    example = Path("examples/.cfg.toml")
    if example.exists():
        return example.resolve()
    raise FileNotFoundError("no .cfg.toml found")


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
        if value:
            return value
        if env_name == "dev":
            fallback = os.environ.get("MONGODB_URI")
            if fallback:
                return fallback
        return ""
    return raw


def _resolve_optional_uri(raw: Any, *, env_name: str) -> str | None:
    if raw is None:
        return None
    return _resolve_uri(str(raw), env_name=env_name)
