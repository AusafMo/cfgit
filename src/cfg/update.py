# Copyright 2026 Mohammad Ausaf. Licensed under the Apache License, Version 2.0.
"""Update check: is a newer cfgit on PyPI?

One engine, two surfaces — a deterministic CLI stderr nudge (npm/gh/brew style) and an MCP tool
that lets Claude offer the upgrade interactively. Both share this logic, so a 30-day snooze taken
in either place quiets both.

Principles:
- Never upgrades anything. It detects and reports; the human runs `pip install -U cfgit`.
- Best-effort and fail-silent: any network/parse/permission error means "say nothing", never an
  error to the caller.
- Throttled: hits PyPI at most once per `CHECK_INTERVAL` (default daily), cached in a state file.
- Snoozable: "don't ask for 30 days" persists to the state file and is honored by both surfaces.
- Kill switch: `CFGIT_NO_UPDATE_CHECK=1` disables it entirely (opt-out, like npm's config).
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
from typing import Any
import urllib.error
import urllib.request

PACKAGE = "cfgit"
PYPI_URL = f"https://pypi.org/pypi/{PACKAGE}/json"
GITHUB_REPO = "AusafMo/cfgit"
GITHUB_LATEST_RELEASE_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
RELEASES_PAGE = f"https://github.com/{GITHUB_REPO}/releases"
_NOTES_MAX_LINES = 5
STATE_DIR = Path.home() / ".cfgit"
STATE_FILE = STATE_DIR / "update-check.json"
CHECK_INTERVAL_S = 24 * 60 * 60           # network check at most once/day
DEFAULT_SNOOZE_DAYS = 30
_TIMEOUT_S = 2.0
_DISABLE_ENV = "CFGIT_NO_UPDATE_CHECK"


@dataclass(frozen=True)
class UpdateStatus:
    installed: str | None
    latest: str | None
    update_available: bool
    checked: bool            # did we actually reach PyPI this call?
    snoozed: bool
    disabled: bool
    message: str | None      # a ready-to-show nudge line, or None when there's nothing to say
    notes: str | None = None       # short "what's new" excerpt from the GitHub release, if fetched
    notes_url: str | None = None   # link to the full release notes

    def to_json(self) -> dict[str, Any]:
        return {
            "installed": self.installed,
            "latest": self.latest,
            "update_available": self.update_available,
            "checked": self.checked,
            "snoozed": self.snoozed,
            "disabled": self.disabled,
            "message": self.message,
            "notes": self.notes,
            "notes_url": self.notes_url,
        }


def installed_version() -> str | None:
    """Resolve the installed cfgit version, robust to editable/dev installs."""
    try:
        from importlib.metadata import PackageNotFoundError, version

        try:
            return version(PACKAGE)
        except PackageNotFoundError:
            pass
    except Exception:  # noqa: BLE001
        pass
    # dev fallback: read version straight from pyproject.toml at the repo root
    try:
        import tomllib  # py311+

        root = Path(__file__).resolve().parents[2]
        data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
        return str(data["project"]["version"])
    except Exception:  # noqa: BLE001
        return None


def check(*, force: bool = False, now: float | None = None) -> UpdateStatus:
    """Return the current update status, honoring the kill switch, throttle, and snooze.

    `force=True` bypasses the throttle (but not the kill switch) — used by an explicit
    `cfg check-update`. `now` is injectable for tests.
    """
    installed = installed_version()
    if _disabled():
        return UpdateStatus(installed, None, False, False, False, True, None)

    ts = _now(now)
    state = _load_state()

    if not force and _snoozed(state, ts):
        latest = state.get("latest")
        return UpdateStatus(installed, latest, _gt(latest, installed), False, True, False, None)

    if not force and _checked_recently(state, ts):
        latest = state.get("latest")
        available = _gt(latest, installed)
        # reuse the release notes cached from the last real check — no network on this path
        notes = state.get("notes") if available else None
        return _status(installed, latest, available, checked=False, notes=notes)

    latest = _fetch_latest()
    if latest is None:
        # network failed — say nothing, don't rewrite last_checked so we retry next time
        return UpdateStatus(installed, state.get("latest"), False, False, False, False, None)

    available = _gt(latest, installed)
    notes = _fetch_release_notes() if available else None
    state["latest"] = latest
    state["last_checked"] = ts
    state["notes"] = notes
    _save_state(state)
    return _status(installed, latest, available, checked=True, notes=notes)


def _status(installed, latest, available, *, checked, notes):
    return UpdateStatus(
        installed=installed,
        latest=latest,
        update_available=available,
        checked=checked,
        snoozed=False,
        disabled=False,
        message=_nudge(installed, latest, notes) if available else None,
        notes=notes if available else None,
        notes_url=RELEASES_PAGE if available else None,
    )


def snooze(days: int = DEFAULT_SNOOZE_DAYS, *, now: float | None = None) -> dict[str, Any]:
    """Record a 'don't ask for N days' snooze. Honored by both the CLI and the MCP tool."""
    ts = _now(now)
    state = _load_state()
    state["snooze_until"] = ts + days * 24 * 60 * 60
    _save_state(state)
    return {"state": "snoozed", "days": days, "snooze_until": state["snooze_until"]}


def _nudge(installed: str | None, latest: str | None, notes: str | None = None) -> str:
    head = f"cfgit {latest} is available (you have {installed or 'an older version'})."
    tail = f"Upgrade with `pip install -U cfgit`. (Set {_DISABLE_ENV}=1 to silence this.)"
    if notes:
        return f"{head}\nWhat's new:\n{notes}\n{tail}\nFull notes: {RELEASES_PAGE}"
    return f"{head} {tail}"


def _fetch_release_notes() -> str | None:
    """Short 'what's new' excerpt from the latest GitHub release (the notes we author at release
    time). Best-effort: any failure returns None and the nudge degrades to the plain line."""
    try:
        req = urllib.request.Request(
            GITHUB_LATEST_RELEASE_URL,
            headers={"Accept": "application/vnd.github+json", "User-Agent": "cfgit-update-check"},
        )
        with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:  # noqa: S310 - fixed https URL
            data = json.loads(resp.read().decode("utf-8"))
        return _excerpt(str(data.get("body") or ""))
    except (urllib.error.URLError, TimeoutError, ValueError, KeyError, OSError):
        return None
    except Exception:  # noqa: BLE001 - notes are optional; never raise
        return None


def _excerpt(body: str) -> str | None:
    """Pull the first few bullet/highlight lines out of a release body, dropping headers/blanks."""
    lines: list[str] = []
    for raw in body.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        # normalize common markdown bullets to a plain dash
        if line[:2] in ("- ", "* ", "• "):
            line = "  • " + line[2:].strip()
        elif not line.startswith(("•", "  •")):
            line = "  " + line
        lines.append(line)
        if len(lines) >= _NOTES_MAX_LINES:
            break
    return "\n".join(lines) if lines else None


def _disabled() -> bool:
    return os.environ.get(_DISABLE_ENV, "") not in ("", "0", "false", "no")


def _fetch_latest() -> str | None:
    try:
        req = urllib.request.Request(PYPI_URL, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:  # noqa: S310 - fixed https URL
            data = json.loads(resp.read().decode("utf-8"))
        return str(data["info"]["version"])
    except (urllib.error.URLError, TimeoutError, ValueError, KeyError, OSError):
        return None
    except Exception:  # noqa: BLE001 - update check must never raise
        return None


def _load_state() -> dict[str, Any]:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError, OSError):
        return {}


def _save_state(state: dict[str, Any]) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except OSError:
        pass  # can't persist (read-only home, etc.) — degrade to "check every time", never error


def _snoozed(state: dict[str, Any], ts: float) -> bool:
    until = state.get("snooze_until")
    return isinstance(until, (int, float)) and ts < until


def _checked_recently(state: dict[str, Any], ts: float) -> bool:
    last = state.get("last_checked")
    return isinstance(last, (int, float)) and (ts - last) < CHECK_INTERVAL_S


def _now(now: float | None) -> float:
    if now is not None:
        return now
    import time

    return time.time()


def _gt(latest: str | None, installed: str | None) -> bool:
    """Is `latest` a strictly newer release than `installed`? Tolerant PEP 440-ish compare."""
    if not latest or not installed:
        return False
    try:
        from packaging.version import Version

        return Version(latest) > Version(installed)
    except Exception:  # noqa: BLE001 - packaging may be absent; fall back to a tuple compare
        return _parse(latest) > _parse(installed)


def _parse(v: str) -> tuple[int, ...]:
    parts: list[int] = []
    for chunk in v.split("."):
        num = ""
        for ch in chunk:
            if ch.isdigit():
                num += ch
            else:
                break
        parts.append(int(num) if num else 0)
    return tuple(parts)
