# Copyright 2026 Mohammad Ausaf. Licensed under the Apache License, Version 2.0.
"""Enforce SPEC §1 inviolable rule 1: first-party core packages stay SDK-free.

The DB drivers live only in cfg.adapters. Vendor LLM/http clients live only in
plugins/cfg_impact. This greps source text so it catches dependency creep early.
"""
from __future__ import annotations

import pathlib
import re

CFG = pathlib.Path(__file__).resolve().parent.parent / "src" / "cfg"

FORBIDDEN = [
    r"\bimport\s+pymongo\b", r"\bfrom\s+pymongo\b",
    r"\bimport\s+motor\b", r"\bfrom\s+motor\b",
    r"\bimport\s+psycopg\b", r"\bfrom\s+psycopg\b",
    r"\bimport\s+sqlalchemy\b", r"\bfrom\s+sqlalchemy\b",
    r"\bimport\s+anthropic\b", r"\bfrom\s+anthropic\b",
    r"\bimport\s+openai\b", r"\bfrom\s+openai\b",
]


def test_first_party_runtime_packages_have_no_driver_or_llm_sdk() -> None:
    offenders: list[str] = []
    for path in CFG.rglob("*.py"):
        if "adapters" in path.relative_to(CFG).parts:
            continue
        text = path.read_text(encoding="utf-8")
        for pat in FORBIDDEN:
            if re.search(pat, text):
                offenders.append(f"{path}: matches {pat!r}")
    assert not offenders, (
        "cfg runtime packages outside adapters must not import DB drivers or LLM SDKs. Offenders:\n"
        + "\n".join(offenders)
    )


def test_core_does_not_import_optional_plugins() -> None:
    offenders: list[str] = []
    for path in (CFG / "core").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for forbidden in ("cfg_impact", "cfg_agent"):
            if forbidden in text:
                offenders.append(f"{path.relative_to(CFG.parent)} imports {forbidden}")
    assert offenders == []
