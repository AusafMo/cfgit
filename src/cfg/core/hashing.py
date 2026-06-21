# Copyright 2026 Mohammad Ausaf. Licensed under the Apache License, Version 2.0.
"""Canonical record hashing for cfgit."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
import fnmatch
import hashlib
import json
import math
import unicodedata
from typing import Any

from cfg.core.config import CollectionConfig


def stored_doc(doc: dict[str, Any], coll: CollectionConfig) -> dict[str, Any]:
    """Return the doc shape stored in history.

    Secret fields are removed. Ignored fields stay stored but do not hash.
    """
    out = deepcopy(doc)
    for path in coll.secret_fields:
        _drop_path(out, path)
    return out


def hash_doc(doc: dict[str, Any], coll: CollectionConfig) -> str:
    stripped = strip_for_hash(doc, coll)
    payload = json.dumps(_normalize(stripped), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def strip_for_hash(doc: dict[str, Any], coll: CollectionConfig) -> dict[str, Any]:
    out = deepcopy(doc)
    for key in list(out.keys()):
        if key in coll.ignore_fields:
            out.pop(key, None)
            continue
        if any(fnmatch.fnmatchcase(key, pat) for pat in coll.ignore_patterns):
            out.pop(key, None)
    for path in coll.ignore_paths:
        _drop_path(out, path)
    for path in coll.secret_fields:
        _drop_path(out, path)
    return out


def _drop_path(doc: dict[str, Any], dotted: str) -> None:
    parts = [p for p in dotted.split(".") if p]
    if not parts:
        return
    cur: Any = doc
    for part in parts[:-1]:
        if not isinstance(cur, dict) or part not in cur:
            return
        cur = cur[part]
    if isinstance(cur, dict):
        cur.pop(parts[-1], None)


def _normalize(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(k): _normalize(v)
            for k, v in value.items()
            if v is not None
        }
    if isinstance(value, (list, tuple)):
        return [_normalize(v) for v in value]
    if _is_bson_decimal128(value):
        return _normalize(value.to_decimal())
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            raise ValueError("cannot hash NaN or Infinity")
        if value.is_integer():
            return int(value)
        return Decimal(str(value)).normalize().to_eng_string()
    if isinstance(value, Decimal):
        if value == value.to_integral_value():
            return int(value)
        return value.normalize().to_eng_string()
    if isinstance(value, datetime):
        dt = value
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        dt = dt.astimezone(timezone.utc)
        dt = dt.replace(microsecond=(dt.microsecond // 1000) * 1000)
        return dt.isoformat().replace("+00:00", "Z")
    return str(value)


def _is_bson_decimal128(value: Any) -> bool:
    cls = value.__class__
    return cls.__name__ == "Decimal128" and cls.__module__.startswith("bson.")
