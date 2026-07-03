from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ResourceRef:
    collection: str
    record_id: str | None = None
    path: str | None = None

    @property
    def scope(self) -> str:
        if self.record_id is None:
            return "collection"
        if self.path is None:
            return "record"
        return "field"

    def format(self) -> str:
        if self.record_id is None:
            return f"{self.collection}:*"
        if self.path is None:
            return f"{self.collection}:{self.record_id}"
        return f"{self.collection}:{self.record_id}:{self.path}"


def parse_resource(raw: str) -> ResourceRef:
    value = str(raw or "").strip()
    if ":" not in value:
        raise ValueError("resource must look like collection:id, collection:*, or collection:id:/path")
    collection, rest = value.split(":", 1)
    if not collection or not rest:
        raise ValueError("resource collection and id are required")
    if rest == "*":
        return ResourceRef(collection=collection)
    if ":/" in rest:
        record_id, path = rest.split(":/", 1)
        if not record_id or not path:
            raise ValueError("field resource must include record id and JSON path")
        return ResourceRef(collection=collection, record_id=record_id, path="/" + path.strip("/"))
    return ResourceRef(collection=collection, record_id=rest)


def paths_overlap(left: str | None, right: str | None) -> bool:
    if left is None or right is None:
        return True
    left_parts = _path_parts(left)
    right_parts = _path_parts(right)
    shorter = min(len(left_parts), len(right_parts))
    return left_parts[:shorter] == right_parts[:shorter]


def resources_overlap(left: ResourceRef | str, right: ResourceRef | str) -> bool:
    left_ref = parse_resource(left) if isinstance(left, str) else left
    right_ref = parse_resource(right) if isinstance(right, str) else right
    if left_ref.collection != right_ref.collection:
        return False
    if left_ref.record_id is None or right_ref.record_id is None:
        return True
    if left_ref.record_id != right_ref.record_id:
        return False
    return paths_overlap(left_ref.path, right_ref.path)


def _path_parts(path: str) -> tuple[str, ...]:
    normalized = "/" + path.strip("/")
    if normalized == "/":
        return ()
    return tuple(part for part in normalized.split("/") if part)
