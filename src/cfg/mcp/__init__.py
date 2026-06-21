# Copyright 2026 Mohammad Ausaf. Licensed under the Apache License, Version 2.0.
"""MCP server — the agent surface (SPEC §12).

Every tool returns a UNIFORM ENVELOPE carrying what CLI exit codes carry:
    {status: ok|dirty|conflict|needs_approval|declined|not_found|error|invariant_violation,
     code, message, data}
Tools: status, log, diff, impact, show, points, commit, adopt, set, restore, tag,
redact, approval_status. There is intentionally NO approve/deny tool — an agent can
observe an approval, never grant it (SPEC §5.18, §11).
"""
