from __future__ import annotations

from copy import deepcopy
from typing import Any


SUPPORTED_OPS = {"add", "replace", "remove"}


def apply_json_patch(doc: dict[str, Any], patch: list[dict[str, Any]]) -> dict[str, Any]:
    result = deepcopy(doc)
    for op in patch:
        operation = op.get("op")
        path = op.get("path")
        if operation not in SUPPORTED_OPS:
            raise ValueError(f"unsupported JSON Patch op: {operation}")
        if not isinstance(path, str) or not path.startswith("/"):
            raise ValueError("JSON Patch path must start with /")
        if operation == "remove":
            _remove(result, path)
        elif operation == "replace":
            _replace(result, path, op.get("value"))
        elif operation == "add":
            _add(result, path, op.get("value"))
    return result


def patch_paths(patch: list[dict[str, Any]]) -> list[str]:
    paths: list[str] = []
    for op in patch:
        path = op.get("path")
        if not isinstance(path, str) or not path.startswith("/"):
            raise ValueError("JSON Patch path must start with /")
        paths.append(path)
    return paths


def _add(doc: Any, path: str, value: Any) -> None:
    parent, key = _parent(doc, path)
    if isinstance(parent, list):
        if key == "-":
            parent.append(value)
            return
        index = _list_index(parent, key, allow_end=True)
        parent.insert(index, value)
        return
    if not isinstance(parent, dict):
        raise ValueError(f"cannot add into non-container at {path}")
    parent[key] = value


def _replace(doc: Any, path: str, value: Any) -> None:
    parent, key = _parent(doc, path)
    if isinstance(parent, list):
        parent[_list_index(parent, key)] = value
        return
    if not isinstance(parent, dict) or key not in parent:
        raise ValueError(f"cannot replace missing path {path}")
    parent[key] = value


def _remove(doc: Any, path: str) -> None:
    parent, key = _parent(doc, path)
    if isinstance(parent, list):
        parent.pop(_list_index(parent, key))
        return
    if not isinstance(parent, dict) or key not in parent:
        raise ValueError(f"cannot remove missing path {path}")
    del parent[key]


def _parent(doc: Any, path: str) -> tuple[Any, str]:
    parts = _parts(path)
    if not parts:
        raise ValueError("root-level JSON Patch operations are not supported")
    current = doc
    for part in parts[:-1]:
        if isinstance(current, list):
            current = current[_list_index(current, part)]
        elif isinstance(current, dict) and part in current:
            current = current[part]
        else:
            raise ValueError(f"path does not exist: {path}")
    return current, parts[-1]


def _parts(path: str) -> list[str]:
    return [_decode(part) for part in path.split("/")[1:]]


def _decode(part: str) -> str:
    return part.replace("~1", "/").replace("~0", "~")


def _list_index(items: list[Any], raw: str, *, allow_end: bool = False) -> int:
    if raw == "-" and allow_end:
        return len(items)
    try:
        index = int(raw)
    except ValueError as exc:
        raise ValueError(f"invalid list index: {raw}") from exc
    upper = len(items) if allow_end else len(items) - 1
    if index < 0 or index > upper:
        raise ValueError(f"list index out of range: {raw}")
    return index
