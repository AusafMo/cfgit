# Copyright 2026 Mohammad Ausaf. Licensed under the Apache License, Version 2.0.
"""Small structured diff helper."""
from __future__ import annotations

from typing import Any


def diff_values(before: Any, after: Any, path: str = "") -> list[dict[str, Any]]:
    if before == after:
        return []
    if isinstance(before, dict) and isinstance(after, dict):
        changes: list[dict[str, Any]] = []
        keys = sorted(set(before) | set(after))
        for key in keys:
            child_path = f"{path}.{key}" if path else str(key)
            if key not in before:
                changes.append({"path": child_path, "op": "add", "before": None, "after": after[key]})
            elif key not in after:
                changes.append({"path": child_path, "op": "remove", "before": before[key], "after": None})
            else:
                changes.extend(diff_values(before[key], after[key], child_path))
        return changes
    return [{"path": path or "$", "op": "change", "before": before, "after": after}]


def format_diff(changes: list[dict[str, Any]]) -> str:
    if not changes:
        return "no changes"
    lines: list[str] = []
    for change in changes:
        lines.append(f"{change['op']} {change['path']}")
        if change["op"] != "add":
            lines.append(f"  before: {_short(change['before'])}")
        if change["op"] != "remove":
            lines.append(f"  after:  {_short(change['after'])}")
    return "\n".join(lines)


def _short(value: Any, limit: int = 240) -> str:
    text = repr(value)
    if len(text) > limit:
        return text[: limit - 3] + "..."
    return text
