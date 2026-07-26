# Copyright 2026 Mohammad Ausaf. Licensed under the Apache License, Version 2.0.
"""Self-teaching remedies for cfgit outcomes.

Every terminal state and every refused/errored operation maps to a `Next`: a plain-language
explanation plus the exact next command(s) to run. One table, rendered three ways — human prose
in the CLI and UI, structured JSON for agents and scripts — so the guidance lives at the moment
of the outcome, not only in a doc read once at session start.

This module is pure: it imports nothing from adapters, the engine, or any interface. It reasons
over strings (state, error class name, message) so it can be unit-tested in isolation and
attached by both the CLI ladder and the shared `envelope()` without an import cycle.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Next:
    """The next action for an outcome. `commands` may contain `{record}` placeholders."""

    why: str
    remedy: str
    commands: tuple[str, ...] = ()
    docs: str | None = None

    def substitute(self, record: str | None) -> "Next":
        token = record or "<collection:id>"
        return Next(
            why=self.why,
            remedy=self.remedy,
            commands=tuple(cmd.replace("{record}", token) for cmd in self.commands),
            docs=self.docs,
        )

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {"why": self.why, "remedy": self.remedy, "commands": list(self.commands)}
        if self.docs:
            out["docs"] = self.docs
        return out


# --- terminal states (a commit/adopt/restore returned this in `data["state"]`) -------------
#
# Only states where the operator plausibly needs to DO something get an entry. Pure-success
# states (committed, adopted, imported, restored, tagged, merged, switched, clean, ready,
# unchanged, deleted, exists) intentionally have no remedy — `next_for` returns None and
# surfaces stay quiet.

_STATE_REMEDIES: dict[str, Next] = {
    "changed_outside_cfgit": Next(
        why="The live record differs from what cfgit last recorded — something wrote it outside cfgit.",
        remedy="Look at the drift, then fold it into history (adopt) or throw it away (restore), then retry.",
        commands=(
            "cfg diff {record} =HEAD =live",
            "cfg adopt {record} -m 'adopt out-of-band change'",
            "cfg restore {record} =HEAD -m 'discard drift'",
        ),
        docs="SKILL.md#reconcile-drift",
    ),
    "noop": Next(
        why="The document you committed is identical to the current version — nothing changed.",
        remedy="If you expected a change, check your input; otherwise there is nothing to do.",
        commands=("cfg diff {record} =HEAD =live",),
    ),
    "blocked": Next(
        why="A batch was refused before any write: at least one target drifted, is missing, or tripped the secret policy. Nothing was applied.",
        remedy="Inspect the `failed` list, adopt drift / fix missing targets, then rerun the same command.",
        commands=("cfg status", "cfg adopt {record} -m 'adopt drift before batch'"),
        docs="SKILL.md#reconcile-drift",
    ),
    "partial": Next(
        why="A batch started writing and then hit an error partway — some records were applied, some were not.",
        remedy="Inspect `results`/`failed`/`pending`, then rerun the same command; already-applied records are no-ops.",
        commands=("cfg status",),
    ),
    "missing": Next(
        why="No live record exists for this id in this environment.",
        remedy="Check the collection and id are right and you are on the intended --env; import it if it should be tracked.",
        commands=("cfg status {record}", "cfg import {record} -m 'start tracking'"),
    ),
    "new": Next(
        why="This record exists live but cfgit is not tracking it yet (no history).",
        remedy="Import it to start versioning.",
        commands=("cfg import {record} -m 'start tracking'",),
    ),
    "not_found": Next(
        why="cfgit could not find what you asked for (record, ref, or tag).",
        remedy="Confirm the id/ref, or list history to find a valid one.",
        commands=("cfg log {record}",),
    ),
    "stale": Next(
        why="The branch moved after this PR was created, so the merge base is out of date.",
        remedy="Recreate the PR from the current branch tip, or rebase the branch, then merge.",
        commands=(),
        docs="SKILL.md",
    ),
    "would_commit": Next(
        why="Dry run: this is the field-level delta cfgit would write. Nothing was written.",
        remedy="Rerun without --dry-run to apply.",
        commands=("cfg commit {record} --from <file> -m '<why>'",),
    ),
    "dry_run": Next(
        why="Dry run: this is what a restore would change. Nothing was written.",
        remedy="Rerun without --dry-run to apply.",
        commands=(),
    ),
}


# --- error classes (an exception flowed through envelope()/the CLI ladder) ------------------
#
# Keyed by the exception class name the handlers already discriminate on. Some (identity,
# permission) are refined further by message substring in `_error_remedy`.

_STALE_REMEDY = Next(
    why="The record moved between cfgit reading it and writing it (a concurrent commit or an out-of-band write).",
    remedy="Adopt the current live state, then retry your operation.",
    commands=("cfg diff {record} =HEAD =live", "cfg adopt {record} -m 'adopt out-of-band change'"),
    docs="SKILL.md#reconcile-drift",
)

_ERROR_REMEDIES: dict[str, Next] = {
    "StaleHead": _STALE_REMEDY,
    "StaleLive": _STALE_REMEDY,
    "AmbiguousConfig": Next(
        why="cfgit found history for this database under a different env name than the one you used, or more than one live record matched an id.",
        remedy="Re-run with the env name that originally wrote the history, or fix .cfg.toml so this database always uses one stable env name / a unique id_field.",
        commands=("cfg --env <original-env> log {record}",),
        docs="CONFIGURATION.md",
    ),
    "AtomicityUnavailable": Next(
        why="The backend cannot make this mutation atomically (runtime and cfgit history are not co-located on a transactional deployment).",
        remedy="Point runtime and history at the same transactional cluster (Mongo replica set / one Postgres host), then retry.",
        commands=("cfg doctor --status",),
        docs="ADAPTERS.md",
    ),
    "NoSuchConfig": Next(
        why="cfgit needed a live record (or a valid ref) and could not find one.",
        remedy="Check the collection/id and --env; for a bad ref, list history to find a valid seq or tag.",
        commands=("cfg status {record}", "cfg log {record}"),
    ),
    "SecretBlocked": Next(
        why="The document contains secret-like content and the secret policy refused to store it in history.",
        remedy="Run doctor for paste-ready secret_fields to strip it from history, or rerun with --allow-secret if the value must stay in the record.",
        commands=("cfg doctor",),
        docs="CONFIGURATION.md",
    ),
    "BranchingDisabled": Next(
        why="This command needs branching, which is not enabled for this project/env.",
        remedy="Enable branching in .cfg.toml, or run the operation on the main branch.",
        commands=(),
        docs="CONFIGURATION.md",
    ),
}

# PermissionDenied / IdentityError are message-refined. Keyed by a substring of the message.
_IDENTITY_REMEDIES: tuple[tuple[str, Next], ...] = (
    (
        "does not match verified identity",
        Next(
            why="You passed --author, but this env verifies identity from a token / DB principal — a self-asserted author is not accepted.",
            remedy="Drop --author; let cfgit read your verified identity (CFGIT_IDENTITY_TOKEN or your DB credential).",
            commands=("cfg whoami",),
            docs="IDENTITY_AND_ATTRIBUTION.md",
        ),
    ),
    (
        "",  # default IdentityError: could not verify the caller
        Next(
            why="This env requires a verified identity and cfgit could not prove who you are.",
            remedy="Provide the full identity token (not the short fingerprint) in the configured env var, e.g. CFGIT_IDENTITY_TOKEN, or set up your DB principal, then retry.",
            commands=("cfg whoami",),
            docs="IDENTITY_AND_ATTRIBUTION.md",
        ),
    ),
)

_PERMISSION_REMEDIES: tuple[tuple[str, Next], ...] = (
    (
        "identity required",
        Next(
            why="This env verifies identity before it lets you write, and yours is not verified.",
            remedy="Provide a verified identity (CFGIT_IDENTITY_TOKEN or DB principal), then retry.",
            commands=("cfg whoami",),
            docs="IDENTITY_AND_ATTRIBUTION.md",
        ),
    ),
    (
        "admin permission required",
        Next(
            why="This action is admin-only in this env and your identity is not an admin.",
            remedy="Ask an admin to run it, or add your author to [env.<name>.permissions] admins in .cfg.toml.",
            commands=("cfg whoami",),
            docs="IDENTITY_AND_ATTRIBUTION.md",
        ),
    ),
    (
        "writer permission required",
        Next(
            why="This env is restricted and your identity is not in the writers/admins list.",
            remedy="Ask an admin to grant you writer access, or add your author to [env.<name>.permissions] writers in .cfg.toml.",
            commands=("cfg whoami",),
            docs="IDENTITY_AND_ATTRIBUTION.md",
        ),
    ),
)


def _error_remedy(error_class: str | None, message: str | None) -> Next | None:
    if error_class == "IdentityError":
        return _match_message(_IDENTITY_REMEDIES, message)
    if error_class == "PermissionDenied":
        return _match_message(_PERMISSION_REMEDIES, message)
    if error_class:
        return _ERROR_REMEDIES.get(error_class)
    return None


def _match_message(table: tuple[tuple[str, Next], ...], message: str | None) -> Next | None:
    text = (message or "").lower()
    fallback: Next | None = None
    for needle, nxt in table:
        if needle == "":
            fallback = nxt
            continue
        if needle.lower() in text:
            return nxt
    return fallback


def next_for(
    *,
    status: str | None = None,
    code: int | None = None,
    state: str | None = None,
    error_class: str | None = None,
    record: str | None = None,
    message: str | None = None,
) -> Next | None:
    """Resolve the remedy for an outcome, with `{record}` substituted. Returns None when the
    outcome needs no guidance (clean success, or an unknown state)."""
    nxt: Next | None = None
    if error_class:
        nxt = _error_remedy(error_class, message)
    if nxt is None and state:
        nxt = _STATE_REMEDIES.get(state)
    if nxt is None:
        return None
    return nxt.substitute(record)


def render_text(nxt: Next) -> str:
    """Human prose for the CLI/UI: why → remedy → copy-paste commands."""
    lines = [nxt.why, f"→ {nxt.remedy}"]
    if nxt.commands:
        lines.append("  Try:")
        lines.extend(f"    {cmd}" for cmd in nxt.commands)
    if nxt.docs:
        lines.append(f"  Docs: {nxt.docs}")
    return "\n".join(lines)


# --- awareness: the wrong-mode trap ---------------------------------------------------------


def open_mode_on_guarded_env(*, needs_approval: bool, identity_mode: str) -> bool:
    """True when a prod-shaped env (needs_approval) is still in unverified `open` identity mode,
    so writes succeed silently and unaudited (authenticated:false)."""
    return bool(needs_approval) and identity_mode == "open"


OPEN_MODE_WARNING = (
    "this env has needs_approval=true but identity.mode=\"open\": writes succeed UNAUDITED "
    "(authenticated:false). Set [env.<name>.identity] mode=\"authenticated\" and provision "
    "CFGIT_IDENTITY_TOKEN (or a DB principal) so history records who authorized each write."
)
