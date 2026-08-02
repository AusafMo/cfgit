"""Seed a synthetic but production-shaped cfgit demo database.

The data is intentionally fake and safe for screenshots. It is modeled after
public patterns for agent handoffs, guardrails, escalation flows, evals, tracing,
knowledge sources, and rollout gates, but it does not copy a vendor config or any
private customer setup.

Typical demo flow:
  python examples/seed_support_demo.py --reset
  python examples/seed_support_demo.py --enrich    # builds branches, PRs, tags, deep history (drives the cfg CLI)
  python examples/seed_support_demo.py --drift
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
from datetime import datetime, timezone
from typing import Any

from pymongo import MongoClient


RUNTIME_COLLECTIONS = [
    "agent_configs",
    "modelgarden_models",
    "policy_rules",
    "tool_registry",
    "routing_policies",
    "escalation_policies",
    "eval_suites",
    "rollout_controls",
    "knowledge_sources",
]
CFGIT_COLLECTIONS = [
    "cfgit_demo_history",
    "cfgit_demo_heads",
    "cfgit_demo_refs",
]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--uri", default="mongodb://localhost:27017/?replicaSet=rs0")
    parser.add_argument("--db", default="cfgit_ui_demo")
    parser.add_argument("--reset", action="store_true")
    parser.add_argument(
        "--drift",
        action="store_true",
        help="apply synthetic out-of-band edits after the base records have been imported",
    )
    parser.add_argument(
        "--enrich",
        action="store_true",
        help=(
            "drive the cfg CLI to build genuine branches, PRs, tags, and deep history "
            "after --reset has seeded the base records"
        ),
    )
    args = parser.parse_args()

    client = MongoClient(args.uri)
    db = client[args.db]
    if args.db in {"admin", "config", "local"}:
        raise SystemExit(f"refusing to seed Mongo system database {args.db!r}")
    if args.reset:
        for name in [*RUNTIME_COLLECTIONS, *CFGIT_COLLECTIONS]:
            db.drop_collection(name)

    now = datetime.now(timezone.utc)
    if args.drift:
        apply_drift(db, now)
        print(f"applied synthetic cfgit demo drift in Mongo database {args.db!r}")
        return

    if args.enrich:
        enrich(db, args)
        print(f"enriched cfgit demo history in Mongo database {args.db!r}")
        return

    seed_base(db, now)
    print(f"seeded synthetic cfgit demo data in Mongo database {args.db!r}")


_DEMO_AUTHOR = "demo.user@example.com"
_CFG_CONFIG = "examples/cfgit-support-demo.toml"


def _cfg(
    args: argparse.Namespace,
    *cmd: str,
    branch: str | None = None,
    tolerate_error: bool = False,
) -> dict[str, Any] | None:
    """Run `cfg --config-file ... --json [--branch B] <cmd...>` with CFG_AUTHOR set.

    Returns the parsed JSON response dict (or None when there is no JSON output).
    Strips any leading human-readable log lines that precede the JSON object/array.
    On non-zero exit, logs stderr and either raises SystemExit or returns None based
    on *tolerate_error*.

    Pass *branch* to commit to a branch other than main (emits --branch before the
    subcommand in the global-flags position).
    """
    global_flags = ["--config-file", _CFG_CONFIG, "--json"]
    if branch:
        global_flags += ["--branch", branch]
    full_cmd = ["cfg", *global_flags, *cmd]
    label = (f"--branch {branch} " if branch else "") + " ".join(cmd)
    print(f"  cfg {label}")
    env = {**os.environ, "CFG_AUTHOR": _DEMO_AUTHOR}
    result = subprocess.run(full_cmd, env=env, capture_output=True, text=True)
    if result.returncode != 0:
        if tolerate_error:
            print(f"    (tolerated non-zero exit {result.returncode}): {result.stderr.strip()[:200]}")
            return None
        print(f"    ERROR (exit {result.returncode}): {result.stderr.strip()[:400]}")
        raise SystemExit(f"cfg command failed: {' '.join(cmd)}")
    raw = result.stdout.strip()
    if not raw:
        return None
    # Strip any leading human-readable log lines before the first JSON character.
    lines = raw.splitlines()
    json_lines: list[str] = []
    found = False
    for line in lines:
        stripped = line.lstrip()
        if not found and (stripped.startswith("{") or stripped.startswith("[")):
            found = True
        if found:
            json_lines.append(line)
    if not json_lines:
        return None
    try:
        return json.loads("\n".join(json_lines))  # type: ignore[return-value]
    except json.JSONDecodeError:
        return None


def _cfg_branch_commit(
    args: argparse.Namespace,
    db: Any,
    collection: str,
    record_id: str,
    updates: dict[str, Any],
    branch: str,
    message: str,
) -> dict[str, Any] | None:
    """Fetch the live document from MongoDB, apply *updates*, write to a tempfile, and
    run `cfg --branch <branch> commit <collection:record_id> --from <tmpfile> -m <msg>`.

    This is the correct way to make branch commits — `cfg set` does not support --branch.
    The *updates* dict is merged (shallow) into the live document before committing.
    Returns the parsed JSON response or None.
    """
    coll = db[collection]
    # We don't know the id_field at runtime, so find by scanning for the record.
    # The config id_fields are known — use a heuristic: find doc where any value == record_id.
    doc = None
    for candidate_field in (
        "config_id", "model_path", "rule_id", "tool_id", "policy_id",
        "escalation_id", "suite_id", "rollout_id", "source_id",
    ):
        doc = coll.find_one({candidate_field: record_id})
        if doc is not None:
            break
    if doc is None:
        print(f"    WARNING: could not find {collection}:{record_id} in MongoDB; skipping branch commit")
        return None
    # Remove non-serialisable BSON types (ObjectId, datetime) from the doc copy.
    serialisable: dict[str, Any] = {}
    for k, v in doc.items():
        if k == "_id":
            continue
        if hasattr(v, "isoformat"):
            serialisable[k] = v.isoformat()
        else:
            serialisable[k] = v
    serialisable.update(updates)
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", prefix="cfgit_enrich_", delete=False
    ) as fh:
        json.dump(serialisable, fh)
        tmppath = fh.name
    try:
        return _cfg(
            args,
            "commit", f"{collection}:{record_id}", "--from", tmppath, "-m", message,
            branch=branch,
        )
    finally:
        try:
            os.unlink(tmppath)
        except OSError:
            pass


def enrich(db: Any, args: argparse.Namespace) -> None:
    """Drive the real cfg CLI to produce genuine cfgit history, branches, PRs, and tags.

    This is idempotent in the sense that init/import errors are tolerated (they are
    benign when the database has already been initialised). All other operations
    (set, branch create, pr create/merge, tag, adopt) will raise on failure.

    All cfg history is authored as demo.user@example.com — never a real personal email.
    """
    now = datetime.now(timezone.utc)

    # ── 0. Ensure base records exist, then init + import ──────────────────────
    print("==> enrich: seeding base records")
    seed_base(db, now)

    print("==> enrich: cfg init (tolerated if already done)")
    _cfg(args, "init", tolerate_error=True)

    print("==> enrich: cfg import --all (tolerated if already done)")
    _cfg(args, "import", "--all", "-m", "initial import", tolerate_error=True)

    # ── 1. Deep history on agent_configs:refund_resolution ───────────────────
    print("==> enrich: deep history — agent_configs:refund_resolution")
    _cfg(
        args,
        "set", "agent_configs:refund_resolution",
        "automation_threshold=0.83",
        "-m", "raise automation threshold to 0.83 after quality gate passed",
    )
    _cfg(
        args,
        "set", "agent_configs:refund_resolution",
        "handoffs=str:billing_disputes,trust_safety_review,support_orchestrator",
        "-m", "add support_orchestrator to handoff chain for supervisor escalations",
    )
    _cfg(
        args,
        "set", "agent_configs:refund_resolution",
        "max_credit_usd=300",
        "-m", "raise max_credit_usd to 300 to cover standard plus-tier orders",
    )

    # ── 2. Deep history on policy_rules:refund_window_standard ───────────────
    print("==> enrich: deep history — policy_rules:refund_window_standard")
    _cfg(
        args,
        "set", "policy_rules:refund_window_standard",
        "rule_text=str:Refunds are allowed within 21 days of fulfillment when the account is in good standing and the order is not already disputed.",
        "-m", "tighten refund window from 30 to 21 days per finance review",
    )
    _cfg(
        args,
        "set", "policy_rules:refund_window_standard",
        "severity=str:high",
        "-m", "escalate severity to high — finance compliance requirement",
    )

    # ── 3. One commit on modelgarden_models:openai/gpt-4.1 ───────────────────
    print("==> enrich: history — modelgarden_models:openai/gpt-4.1")
    _cfg(
        args,
        "set", "modelgarden_models:openai/gpt-4.1",
        "notes=str:promoted to default premium model for trust_safety and billing agents",
        "-m", "mark gpt-4.1 as default premium model and add promotion note",
    )

    # ── 4. System tag ─────────────────────────────────────────────────────────
    print("==> enrich: tag prod-2026-w31")
    _cfg(args, "tag", "prod-2026-w31")

    # ── 5. Branch: refund-policy-tightening (open PR, leave open) ────────────
    print("==> enrich: branch refund-policy-tightening")
    _cfg(args, "branch", "create", "refund-policy-tightening", "-m", "draft: tighten chargeback rule")
    # Use cfg commit --from <tmpfile> because cfg set does not route through --branch.
    _cfg_branch_commit(
        args, db,
        collection="policy_rules",
        record_id="chargeback_no_refund_v1",
        updates={
            "rule_text": (
                "If a payment has an active chargeback, representment, or pre-arbitration case,"
                " agents must not issue any refund or credit. Attach all evidence and escalate"
                " immediately to finance operations."
            ),
            "severity": "critical",
        },
        branch="refund-policy-tightening",
        message="extend chargeback rule to cover pre-arbitration; escalate severity to critical",
    )
    pr_rpt = _cfg(
        args,
        "pr", "create",
        "--head", "refund-policy-tightening",
        "--base", "main",
        "-m", "Tighten chargeback refund boundary — extend rule text and raise severity to critical",
    )
    if pr_rpt:
        pr_id = pr_rpt.get("id", "<unknown>")
        print(f"    opened PR {pr_id} (refund-policy-tightening -> main) — leaving open")

    # ── 6. Branch: model-routing-upgrade (open PR, leave open) ───────────────
    print("==> enrich: branch model-routing-upgrade")
    _cfg(args, "branch", "create", "model-routing-upgrade", "-m", "draft: promote gpt-4.1 to default")
    _cfg_branch_commit(
        args, db,
        collection="modelgarden_models",
        record_id="openai/gpt-4.1",
        updates={
            "tier": "default",
            "notes": "Promoted from premium to default tier — approved by model-ops 2026-w31",
        },
        branch="model-routing-upgrade",
        message="promote gpt-4.1 to default tier for routing upgrade",
    )
    pr_mru = _cfg(
        args,
        "pr", "create",
        "--head", "model-routing-upgrade",
        "--base", "main",
        "-m", "Model routing upgrade — promote openai/gpt-4.1 to default tier",
    )
    if pr_mru:
        pr_id = pr_mru.get("id", "<unknown>")
        print(f"    opened PR {pr_id} (model-routing-upgrade -> main) — leaving open")

    # ── 7. Branch: deprecate-legacy-router (open PR, then merge) ─────────────
    print("==> enrich: branch deprecate-legacy-router")
    _cfg(
        args, "branch", "create", "deprecate-legacy-router",
        "-m", "draft: retire logistics_router via deprecation flag",
    )
    _cfg_branch_commit(
        args, db,
        collection="routing_policies",
        record_id="logistics_router",
        updates={
            "deprecated": True,
            "deprecation_note": (
                "Superseded by global_support_router v2."
                " Scheduled for removal after 2026-w35."
            ),
        },
        branch="deprecate-legacy-router",
        message="flag logistics_router as deprecated — superseded by global_support_router v2",
    )
    pr_dlr = _cfg(
        args,
        "pr", "create",
        "--head", "deprecate-legacy-router",
        "--base", "main",
        "-m", "Deprecate legacy logistics_router — add deprecation flag and removal note",
    )
    if pr_dlr:
        pr_id_dlr = pr_dlr.get("id")
        print(f"    opened PR {pr_id_dlr} (deprecate-legacy-router -> main) — will merge")
        if pr_id_dlr:
            print("==> enrich: merging deprecate-legacy-router PR")
            _cfg(args, "pr", "merge", pr_id_dlr, "-m", "merge: retire logistics_router deprecation flag")
        else:
            print("    WARNING: could not parse PR id from response; skipping merge")

    # ── 8. Adopt: out-of-band edit + cfg adopt ────────────────────────────────
    print("==> enrich: out-of-band edit to escalation_policies:operations_handoff for adopt demo")
    db.escalation_policies.update_one(
        {"escalation_id": "operations_handoff"},
        {
            "$set": {
                "sla_minutes": 90,
                "queues": ["carrier_exceptions", "warehouse_ops", "express_ops"],
                "updated_by": "admin-console-hotfix",
                "updated_at": now,
            }
        },
    )
    _cfg(
        args,
        "adopt", "escalation_policies:operations_handoff",
        "-m", "adopt admin-console hotfix — sla_minutes 120→90, added express_ops queue",
    )

    print("==> enrich: done")


def seed_base(db: Any, now: datetime) -> None:
    _replace_many(
        db.agent_configs,
        "config_id",
        [
            {
                "config_id": "support_orchestrator",
                "is_active": True,
                "role": "Support Orchestrator",
                "model": "openai/gpt-4.1-mini",
                "fallback_models": ["anthropic/claude-3-5-sonnet"],
                "routing_policy": "global_support_router",
                "escalation_policy": "human_handoff_standard",
                "eval_suite": "support_orchestrator_regression",
                "rollout_id": "support_orchestrator_june",
                "tools": ["ticket_lookup", "customer_profile", "kb_search"],
                "knowledge_sources": ["kb_public_help_center", "kb_internal_runbooks"],
                "handoffs": ["refund_resolution", "billing_disputes", "delivery_incidents"],
                "guarded_by": [
                    "privacy_minimization_v2",
                    "refund_window_standard",
                    "regulated_advice_boundary",
                ],
                "policy_refs": ["privacy_minimization_v2", "refund_window_standard"],
                "automation_threshold": 0.74,
                "max_turns": 9,
                "trace_sample_rate": 0.15,
                "phase_contract": (
                    "Classify the customer intent, gather missing identifiers, select the "
                    "specialist agent, and preserve enough context for audit review."
                ),
                "instructions": (
                    "Start every run by reading ticket_lookup and customer_profile. Route "
                    "refunds to refund_resolution, billing failures to billing_disputes, "
                    "and carrier exceptions to delivery_incidents. Do not promise credits, "
                    "legal conclusions, medical advice, or policy exceptions. Escalate when "
                    "confidence is below automation_threshold or a guardrail fires."
                ),
                "updated_at": now,
                "updated_by": "seed",
            },
            {
                "config_id": "refund_resolution",
                "is_active": True,
                "role": "Refund Resolution Specialist",
                "model": "openai/gpt-4.1-mini",
                "fallback_models": ["openai/gpt-4.1"],
                "routing_policy": "refund_router",
                "escalation_policy": "payments_handoff_high_risk",
                "eval_suite": "refund_policy_regression",
                "rollout_id": "refund_agent_june",
                "tools": ["order_ledger", "payment_refund_quote", "ticket_update"],
                "knowledge_sources": ["kb_public_refunds", "kb_internal_payments_runbook"],
                "handoffs": ["billing_disputes", "trust_safety_review"],
                "guarded_by": [
                    "refund_window_standard",
                    "chargeback_no_refund_v1",
                    "privacy_minimization_v2",
                ],
                "policy_refs": ["refund_window_standard", "chargeback_no_refund_v1"],
                "approval_policy": "supervisor_approval_over_250",
                "automation_threshold": 0.81,
                "max_credit_usd": 250,
                "trace_sample_rate": 0.25,
                "phase_contract": (
                    "Determine refund eligibility, quote an auditable refund amount, and "
                    "write the customer-facing rationale before any payment mutation."
                ),
                "instructions": (
                    "Use order_ledger before refund_quote. Apply refund_window_standard and "
                    "deny refunds when chargeback_no_refund_v1 applies. If the amount exceeds "
                    "max_credit_usd or abuse signals are present, hand off to a supervisor."
                ),
                "updated_at": now,
                "updated_by": "seed",
            },
            {
                "config_id": "billing_disputes",
                "is_active": True,
                "role": "Billing Dispute Analyst",
                "model": "anthropic/claude-3-5-sonnet",
                "fallback_models": ["openai/gpt-4.1"],
                "routing_policy": "billing_router",
                "escalation_policy": "payments_handoff_high_risk",
                "eval_suite": "billing_dispute_regression",
                "rollout_id": "billing_disputes_june",
                "tools": ["invoice_lookup", "payment_refund_quote", "ticket_update"],
                "knowledge_sources": ["kb_internal_payments_runbook"],
                "handoffs": ["trust_safety_review"],
                "guarded_by": ["chargeback_no_refund_v1", "privacy_minimization_v2"],
                "policy_refs": ["chargeback_no_refund_v1", "privacy_minimization_v2"],
                "approval_policy": "finance_ops_approval",
                "automation_threshold": 0.86,
                "phase_contract": (
                    "Separate billing-system errors from customer misunderstanding, then "
                    "prepare the adjustment rationale for finance review."
                ),
                "instructions": (
                    "Never reverse a charge directly. Gather invoice id, payment processor "
                    "id, country, and dispute reason. Escalate active chargebacks, tax issues, "
                    "and invoice corrections above 500 USD."
                ),
                "updated_at": now,
                "updated_by": "seed",
            },
            {
                "config_id": "delivery_incidents",
                "is_active": True,
                "role": "Delivery Incident Coordinator",
                "model": "openai/gpt-4.1-mini",
                "fallback_models": ["anthropic/claude-3-5-sonnet"],
                "routing_policy": "logistics_router",
                "escalation_policy": "operations_handoff",
                "eval_suite": "delivery_exception_regression",
                "rollout_id": "delivery_incidents_june",
                "tools": ["shipment_trace", "warehouse_inventory", "ticket_update"],
                "knowledge_sources": ["kb_public_shipping", "kb_internal_ops_runbook"],
                "handoffs": ["refund_resolution"],
                "guarded_by": ["replacement_fraud_boundary", "privacy_minimization_v2"],
                "policy_refs": ["replacement_fraud_boundary"],
                "automation_threshold": 0.79,
                "phase_contract": (
                    "Identify shipment state, choose replacement/refund/no-action path, and "
                    "attach carrier evidence to the ticket."
                ),
                "instructions": (
                    "Use shipment_trace before warehouse_inventory. Offer replacement only "
                    "when carrier status is lost, damaged, or delivered-to-wrong-address. "
                    "Escalate repeat claims, high-value items, and missing proof of delivery."
                ),
                "updated_at": now,
                "updated_by": "seed",
            },
            {
                "config_id": "trust_safety_review",
                "is_active": True,
                "role": "Trust and Safety Reviewer",
                "model": "openai/gpt-4.1",
                "fallback_models": ["anthropic/claude-3-5-sonnet"],
                "routing_policy": "risk_router",
                "escalation_policy": "trust_safety_handoff",
                "eval_suite": "trust_safety_regression",
                "rollout_id": "trust_safety_june",
                "tools": ["customer_risk_score", "ticket_update"],
                "knowledge_sources": ["kb_internal_risk_runbook"],
                "handoffs": [],
                "guarded_by": [
                    "privacy_minimization_v2",
                    "regulated_advice_boundary",
                    "replacement_fraud_boundary",
                ],
                "policy_refs": ["replacement_fraud_boundary", "regulated_advice_boundary"],
                "approval_policy": "mandatory_human_review",
                "automation_threshold": 0.92,
                "phase_contract": (
                    "Produce a risk memo, not a customer reply, for abuse, safety, or legal "
                    "boundary cases."
                ),
                "instructions": (
                    "Do not message the customer. Summarize the risk, cite supporting ticket "
                    "evidence, and assign the correct human queue. Legal, medical, and fraud "
                    "determinations require human review."
                ),
                "updated_at": now,
                "updated_by": "seed",
            },
        ],
    )

    _replace_many(
        db.modelgarden_models,
        "model_path",
        [
            {
                "model_path": "openai/gpt-4.1-mini",
                "provider": "openai",
                "enabled": True,
                "provider_model_id": "gpt-4.1-mini",
                "capabilities": ["tool_use", "structured_output", "vision"],
                "regions": ["us", "eu"],
                "tier": "standard",
                "cost_profile": {"input_per_million_usd": 0.4, "output_per_million_usd": 1.6},
                "latency_budget_ms": 2400,
                "max_context_tokens": 128000,
                "provider_config": {"api_key": "demo-secret-stays-live"},
                "updated_at": now,
                "updated_by": "seed",
            },
            {
                "model_path": "openai/gpt-4.1",
                "provider": "openai",
                "enabled": True,
                "provider_model_id": "gpt-4.1",
                "capabilities": ["tool_use", "structured_output", "long_context"],
                "regions": ["us"],
                "tier": "premium",
                "cost_profile": {"input_per_million_usd": 2.0, "output_per_million_usd": 8.0},
                "latency_budget_ms": 5000,
                "max_context_tokens": 128000,
                "provider_config": {"api_key": "demo-secret-stays-live"},
                "updated_at": now,
                "updated_by": "seed",
            },
            {
                "model_path": "anthropic/claude-3-5-sonnet",
                "provider": "anthropic",
                "enabled": True,
                "provider_model_id": "claude-3-5-sonnet",
                "capabilities": ["tool_use", "structured_output", "long_context"],
                "regions": ["us"],
                "tier": "premium",
                "cost_profile": {"input_per_million_usd": 3.0, "output_per_million_usd": 15.0},
                "latency_budget_ms": 5200,
                "max_context_tokens": 200000,
                "provider_config": {"api_key": "demo-secret-stays-live"},
                "updated_at": now,
                "updated_by": "seed",
            },
        ],
    )

    _replace_many(
        db.policy_rules,
        "rule_id",
        [
            {
                "rule_id": "refund_window_standard",
                "active": True,
                "title": "Standard refund window",
                "applies_to": ["support_orchestrator", "refund_resolution"],
                "severity": "medium",
                "owner": "support-policy",
                "approval_policy": "supervisor_approval_over_250",
                "rule_text": (
                    "Refunds are allowed within 30 days of fulfillment when the account is "
                    "in good standing, the order is not already disputed, and the product "
                    "category is not marked final sale."
                ),
                "customer_copy": (
                    "Eligible orders can be refunded within 30 days when payment and account "
                    "checks pass."
                ),
                "updated_at": now,
                "updated_by": "seed",
            },
            {
                "rule_id": "chargeback_no_refund_v1",
                "active": True,
                "title": "Chargeback refund boundary",
                "applies_to": ["refund_resolution", "billing_disputes"],
                "severity": "high",
                "owner": "finance-ops",
                "approval_policy": "finance_ops_approval",
                "rule_text": (
                    "If a payment has an active chargeback or representment case, agents may "
                    "not issue a refund or credit. They must attach evidence and hand off to "
                    "finance operations."
                ),
                "updated_at": now,
                "updated_by": "seed",
            },
            {
                "rule_id": "privacy_minimization_v2",
                "active": True,
                "title": "PII minimization",
                "applies_to": [
                    "support_orchestrator",
                    "refund_resolution",
                    "billing_disputes",
                    "delivery_incidents",
                    "trust_safety_review",
                ],
                "severity": "high",
                "owner": "privacy",
                "rule_text": (
                    "Agents may read only the fields required by their phase contract. Full "
                    "payment details, government IDs, and authentication secrets must be "
                    "redacted from traces and customer-visible summaries."
                ),
                "updated_at": now,
                "updated_by": "seed",
            },
            {
                "rule_id": "regulated_advice_boundary",
                "active": True,
                "title": "Regulated-advice boundary",
                "applies_to": ["support_orchestrator", "trust_safety_review"],
                "severity": "critical",
                "owner": "legal",
                "approval_policy": "mandatory_human_review",
                "rule_text": (
                    "Agents must not provide legal, tax, medical, or financial advice. They "
                    "must collect context, cite the boundary, and escalate to a human queue."
                ),
                "updated_at": now,
                "updated_by": "seed",
            },
            {
                "rule_id": "replacement_fraud_boundary",
                "active": True,
                "title": "Replacement abuse controls",
                "applies_to": ["delivery_incidents", "trust_safety_review"],
                "severity": "high",
                "owner": "risk-ops",
                "approval_policy": "risk_ops_approval",
                "rule_text": (
                    "Replacement or refund requests with repeat claims, freight forwarding, "
                    "high-value items, or mismatched delivery evidence require trust and "
                    "safety review."
                ),
                "updated_at": now,
                "updated_by": "seed",
            },
        ],
    )

    _replace_many(
        db.tool_registry,
        "tool_id",
        [
            {
                "tool_id": "ticket_lookup",
                "enabled": True,
                "owner": "support-platform",
                "capability": "read_ticket",
                "risk_level": "low",
                "allowed_agents": ["support_orchestrator"],
                "scopes": ["tickets.read", "comments.read"],
                "latency_slo_ms": 350,
                "credentials": {"api_key_ref": "vault://support/ticket-reader"},
                "updated_at": now,
                "updated_by": "seed",
            },
            {
                "tool_id": "customer_profile",
                "enabled": True,
                "owner": "identity-platform",
                "capability": "read_customer_profile",
                "risk_level": "medium",
                "allowed_agents": ["support_orchestrator"],
                "scopes": ["customer.tier.read", "customer.region.read"],
                "redacted_fields": ["full_payment_token", "government_id"],
                "latency_slo_ms": 450,
                "credentials": {"api_key_ref": "vault://identity/profile-reader"},
                "updated_at": now,
                "updated_by": "seed",
            },
            {
                "tool_id": "kb_search",
                "enabled": True,
                "owner": "knowledge-platform",
                "capability": "semantic_knowledge_search",
                "risk_level": "low",
                "allowed_agents": [
                    "support_orchestrator",
                    "refund_resolution",
                    "delivery_incidents",
                ],
                "scopes": ["kb.search"],
                "latency_slo_ms": 800,
                "updated_at": now,
                "updated_by": "seed",
            },
            {
                "tool_id": "order_ledger",
                "enabled": True,
                "owner": "commerce-platform",
                "capability": "read_order_payment_state",
                "risk_level": "medium",
                "allowed_agents": ["refund_resolution"],
                "scopes": ["orders.read", "payments.status.read"],
                "redacted_fields": ["pan", "cvv", "processor_raw_payload"],
                "latency_slo_ms": 700,
                "credentials": {"api_key_ref": "vault://commerce/order-ledger"},
                "updated_at": now,
                "updated_by": "seed",
            },
            {
                "tool_id": "payment_refund_quote",
                "enabled": True,
                "owner": "payments-platform",
                "capability": "quote_refund_or_credit",
                "risk_level": "high",
                "allowed_agents": ["refund_resolution", "billing_disputes"],
                "scopes": ["refunds.quote"],
                "requires_approval": True,
                "approval_policy": "supervisor_approval_over_250",
                "latency_slo_ms": 1200,
                "credentials": {"api_key_ref": "vault://payments/refund-quote"},
                "updated_at": now,
                "updated_by": "seed",
            },
            {
                "tool_id": "invoice_lookup",
                "enabled": True,
                "owner": "billing-platform",
                "capability": "read_invoice_state",
                "risk_level": "medium",
                "allowed_agents": ["billing_disputes"],
                "scopes": ["invoice.read", "tax-region.read"],
                "latency_slo_ms": 650,
                "credentials": {"api_key_ref": "vault://billing/invoice-reader"},
                "updated_at": now,
                "updated_by": "seed",
            },
            {
                "tool_id": "shipment_trace",
                "enabled": True,
                "owner": "logistics-platform",
                "capability": "read_carrier_events",
                "risk_level": "low",
                "allowed_agents": ["delivery_incidents"],
                "scopes": ["shipment.read", "carrier-events.read"],
                "latency_slo_ms": 900,
                "updated_at": now,
                "updated_by": "seed",
            },
            {
                "tool_id": "warehouse_inventory",
                "enabled": True,
                "owner": "fulfillment-platform",
                "capability": "check_replacement_inventory",
                "risk_level": "medium",
                "allowed_agents": ["delivery_incidents"],
                "scopes": ["inventory.read"],
                "latency_slo_ms": 900,
                "updated_at": now,
                "updated_by": "seed",
            },
            {
                "tool_id": "customer_risk_score",
                "enabled": True,
                "owner": "risk-platform",
                "capability": "read_abuse_signals",
                "risk_level": "high",
                "allowed_agents": ["trust_safety_review"],
                "scopes": ["risk.read"],
                "redacted_fields": ["device_fingerprint", "raw_identity_graph"],
                "latency_slo_ms": 1300,
                "credentials": {"api_key_ref": "vault://risk/customer-score"},
                "updated_at": now,
                "updated_by": "seed",
            },
            {
                "tool_id": "ticket_update",
                "enabled": True,
                "owner": "support-platform",
                "capability": "write_ticket_comment_or_tags",
                "risk_level": "medium",
                "allowed_agents": [
                    "refund_resolution",
                    "billing_disputes",
                    "delivery_incidents",
                    "trust_safety_review",
                ],
                "scopes": ["tickets.comment.write", "tickets.tags.write"],
                "requires_approval": False,
                "latency_slo_ms": 500,
                "credentials": {"api_key_ref": "vault://support/ticket-writer"},
                "updated_at": now,
                "updated_by": "seed",
            },
        ],
    )

    _replace_many(
        db.routing_policies,
        "policy_id",
        [
            {
                "policy_id": "global_support_router",
                "active": True,
                "owner": "support-platform",
                "agents": [
                    "support_orchestrator",
                    "refund_resolution",
                    "billing_disputes",
                    "delivery_incidents",
                    "trust_safety_review",
                ],
                "decision_order": [
                    {"intent": "refund_or_return", "agent": "refund_resolution"},
                    {"intent": "invoice_or_payment_error", "agent": "billing_disputes"},
                    {"intent": "delivery_exception", "agent": "delivery_incidents"},
                    {"intent": "abuse_or_regulated_boundary", "agent": "trust_safety_review"},
                ],
                "min_confidence": 0.74,
                "fallback_agent": "support_orchestrator",
                "escalation_policy": "human_handoff_standard",
                "updated_at": now,
                "updated_by": "seed",
            },
            {
                "policy_id": "refund_router",
                "active": True,
                "owner": "payments-platform",
                "agents": ["refund_resolution", "billing_disputes", "trust_safety_review"],
                "decision_order": [
                    {"condition": "active_chargeback", "agent": "billing_disputes"},
                    {"condition": "repeat_refund_claim", "agent": "trust_safety_review"},
                    {"condition": "standard_refund", "agent": "refund_resolution"},
                ],
                "min_confidence": 0.81,
                "fallback_agent": "billing_disputes",
                "escalation_policy": "payments_handoff_high_risk",
                "updated_at": now,
                "updated_by": "seed",
            },
            {
                "policy_id": "billing_router",
                "active": True,
                "owner": "finance-ops",
                "agents": ["billing_disputes", "trust_safety_review"],
                "decision_order": [
                    {"condition": "invoice_correction", "agent": "billing_disputes"},
                    {"condition": "tax_or_chargeback", "agent": "billing_disputes"},
                    {"condition": "fraud_signal", "agent": "trust_safety_review"},
                ],
                "min_confidence": 0.86,
                "fallback_agent": "billing_disputes",
                "escalation_policy": "payments_handoff_high_risk",
                "updated_at": now,
                "updated_by": "seed",
            },
            {
                "policy_id": "logistics_router",
                "active": True,
                "owner": "fulfillment-platform",
                "agents": ["delivery_incidents", "refund_resolution", "trust_safety_review"],
                "decision_order": [
                    {"condition": "lost_or_damaged", "agent": "delivery_incidents"},
                    {"condition": "refund_requested", "agent": "refund_resolution"},
                    {"condition": "repeat_claim", "agent": "trust_safety_review"},
                ],
                "min_confidence": 0.79,
                "fallback_agent": "delivery_incidents",
                "escalation_policy": "operations_handoff",
                "updated_at": now,
                "updated_by": "seed",
            },
            {
                "policy_id": "risk_router",
                "active": True,
                "owner": "risk-ops",
                "agents": ["trust_safety_review"],
                "decision_order": [
                    {"condition": "regulated_advice", "agent": "trust_safety_review"},
                    {"condition": "abuse_or_fraud", "agent": "trust_safety_review"},
                ],
                "min_confidence": 0.92,
                "fallback_agent": "trust_safety_review",
                "escalation_policy": "trust_safety_handoff",
                "updated_at": now,
                "updated_by": "seed",
            },
        ],
    )

    _replace_many(
        db.escalation_policies,
        "escalation_id",
        [
            {
                "escalation_id": "human_handoff_standard",
                "active": True,
                "owner": "support-ops",
                "agents": ["support_orchestrator"],
                "queues": ["tier2_support"],
                "business_hours": "24x5_follow_the_sun",
                "handoff_trigger": [
                    "confidence_below_threshold",
                    "missing_required_identifier",
                    "customer_requests_human",
                    "guardrail_triggered",
                ],
                "context_packet": [
                    "intent",
                    "ticket_id",
                    "customer_tier",
                    "summarized_evidence",
                    "last_tool_results",
                ],
                "sla_minutes": 60,
                "updated_at": now,
                "updated_by": "seed",
            },
            {
                "escalation_id": "payments_handoff_high_risk",
                "active": True,
                "owner": "finance-ops",
                "agents": ["refund_resolution", "billing_disputes"],
                "queues": ["payments_ops", "risk_ops"],
                "business_hours": "24x7",
                "handoff_trigger": [
                    "amount_over_limit",
                    "active_chargeback",
                    "processor_mismatch",
                    "tax_or_legal_question",
                ],
                "context_packet": [
                    "order_id",
                    "invoice_id",
                    "country",
                    "payment_state",
                    "policy_refs",
                    "recommended_next_step",
                ],
                "sla_minutes": 30,
                "updated_at": now,
                "updated_by": "seed",
            },
            {
                "escalation_id": "operations_handoff",
                "active": True,
                "owner": "fulfillment-ops",
                "agents": ["delivery_incidents"],
                "queues": ["carrier_exceptions", "warehouse_ops"],
                "business_hours": "regional_business_hours",
                "handoff_trigger": [
                    "missing_carrier_evidence",
                    "high_value_replacement",
                    "inventory_unavailable",
                ],
                "context_packet": ["shipment_id", "carrier_events", "item_value", "inventory_state"],
                "sla_minutes": 120,
                "updated_at": now,
                "updated_by": "seed",
            },
            {
                "escalation_id": "trust_safety_handoff",
                "active": True,
                "owner": "risk-ops",
                "agents": ["trust_safety_review"],
                "queues": ["trust_safety"],
                "business_hours": "24x7",
                "handoff_trigger": [
                    "regulated_advice_boundary",
                    "repeat_refund_claim",
                    "identity_risk_high",
                    "legal_or_law_enforcement",
                ],
                "context_packet": ["risk_score_band", "evidence_summary", "policy_refs", "do_not_message"],
                "sla_minutes": 20,
                "updated_at": now,
                "updated_by": "seed",
            },
        ],
    )

    _replace_many(
        db.eval_suites,
        "suite_id",
        [
            {
                "suite_id": "support_orchestrator_regression",
                "enabled": True,
                "owner": "ai-quality",
                "agents": ["support_orchestrator"],
                "dataset": "support-routing-golden-v6",
                "sample_count": 420,
                "required_metrics": {
                    "intent_accuracy": 0.93,
                    "handoff_precision": 0.9,
                    "pii_leak_rate": 0.0,
                    "tool_sequence_pass": 0.95,
                },
                "blocking": True,
                "run_on": ["model_change", "routing_policy_change", "prompt_change"],
                "updated_at": now,
                "updated_by": "seed",
            },
            {
                "suite_id": "refund_policy_regression",
                "enabled": True,
                "owner": "ai-quality",
                "agents": ["refund_resolution"],
                "dataset": "refund-boundary-cases-v9",
                "sample_count": 360,
                "required_metrics": {
                    "eligibility_accuracy": 0.96,
                    "over_refund_rate": 0.0,
                    "approval_recall": 0.98,
                    "policy_citation_rate": 0.95,
                },
                "blocking": True,
                "run_on": ["policy_change", "tool_change", "prompt_change"],
                "updated_at": now,
                "updated_by": "seed",
            },
            {
                "suite_id": "billing_dispute_regression",
                "enabled": True,
                "owner": "ai-quality",
                "agents": ["billing_disputes"],
                "dataset": "billing-disputes-v4",
                "sample_count": 210,
                "required_metrics": {
                    "chargeback_escalation_recall": 0.99,
                    "invoice_reason_accuracy": 0.91,
                    "unauthorized_mutation_rate": 0.0,
                },
                "blocking": True,
                "run_on": ["policy_change", "tool_change", "prompt_change"],
                "updated_at": now,
                "updated_by": "seed",
            },
            {
                "suite_id": "delivery_exception_regression",
                "enabled": True,
                "owner": "ai-quality",
                "agents": ["delivery_incidents"],
                "dataset": "carrier-exceptions-v5",
                "sample_count": 240,
                "required_metrics": {
                    "replacement_decision_accuracy": 0.92,
                    "fraud_boundary_recall": 0.96,
                    "evidence_attachment_rate": 0.93,
                },
                "blocking": True,
                "run_on": ["policy_change", "tool_change", "prompt_change"],
                "updated_at": now,
                "updated_by": "seed",
            },
            {
                "suite_id": "trust_safety_regression",
                "enabled": True,
                "owner": "ai-quality",
                "agents": ["trust_safety_review"],
                "dataset": "regulated-boundary-and-abuse-v7",
                "sample_count": 180,
                "required_metrics": {
                    "human_review_recall": 1.0,
                    "customer_message_rate": 0.0,
                    "risk_summary_completeness": 0.94,
                },
                "blocking": True,
                "run_on": ["policy_change", "prompt_change"],
                "updated_at": now,
                "updated_by": "seed",
            },
        ],
    )

    _replace_many(
        db.rollout_controls,
        "rollout_id",
        [
            {
                "rollout_id": "support_orchestrator_june",
                "enabled": True,
                "owner": "support-platform",
                "agents": ["support_orchestrator"],
                "stage": "regional_canary",
                "traffic_percent": 20,
                "allowed_regions": ["us", "ca"],
                "guardrail_actions": ["block_on_pii", "handoff_on_low_confidence"],
                "rollback_on": {
                    "csat_drop_points": 4,
                    "handoff_spike_percent": 12,
                    "pii_incident_count": 1,
                },
                "eval_suite": "support_orchestrator_regression",
                "updated_at": now,
                "updated_by": "seed",
            },
            {
                "rollout_id": "refund_agent_june",
                "enabled": True,
                "owner": "payments-platform",
                "agents": ["refund_resolution"],
                "stage": "limited_audience",
                "traffic_percent": 15,
                "audience": ["consumer_standard", "consumer_plus"],
                "guardrail_actions": ["require_approval_over_limit", "block_chargeback_refund"],
                "rollback_on": {
                    "over_refund_count": 1,
                    "finance_reversal_rate": 0.02,
                    "policy_citation_rate_below": 0.92,
                },
                "eval_suite": "refund_policy_regression",
                "updated_at": now,
                "updated_by": "seed",
            },
            {
                "rollout_id": "billing_disputes_june",
                "enabled": True,
                "owner": "finance-ops",
                "agents": ["billing_disputes"],
                "stage": "internal_shadow",
                "traffic_percent": 0,
                "audience": ["finance_ops_shadow"],
                "guardrail_actions": ["never_mutate_payments", "always_attach_evidence"],
                "eval_suite": "billing_dispute_regression",
                "updated_at": now,
                "updated_by": "seed",
            },
            {
                "rollout_id": "delivery_incidents_june",
                "enabled": True,
                "owner": "fulfillment-platform",
                "agents": ["delivery_incidents"],
                "stage": "regional_canary",
                "traffic_percent": 10,
                "allowed_regions": ["us"],
                "guardrail_actions": ["handoff_high_value_replacements"],
                "eval_suite": "delivery_exception_regression",
                "updated_at": now,
                "updated_by": "seed",
            },
            {
                "rollout_id": "trust_safety_june",
                "enabled": True,
                "owner": "risk-ops",
                "agents": ["trust_safety_review"],
                "stage": "internal_only",
                "traffic_percent": 0,
                "audience": ["risk_ops"],
                "guardrail_actions": ["do_not_message_customer"],
                "eval_suite": "trust_safety_regression",
                "updated_at": now,
                "updated_by": "seed",
            },
        ],
    )

    _replace_many(
        db.knowledge_sources,
        "source_id",
        [
            {
                "source_id": "kb_public_help_center",
                "enabled": True,
                "owner": "support-content",
                "visibility": "public",
                "indexed_at": "2026-06-01T00:00:00Z",
                "freshness_slo_hours": 24,
                "topics": ["account", "shipping", "returns", "billing"],
                "allowed_agents": ["support_orchestrator"],
                "updated_at": now,
                "updated_by": "seed",
            },
            {
                "source_id": "kb_public_refunds",
                "enabled": True,
                "owner": "support-content",
                "visibility": "public",
                "indexed_at": "2026-06-01T00:00:00Z",
                "freshness_slo_hours": 12,
                "topics": ["returns", "refunds", "store-credit"],
                "allowed_agents": ["refund_resolution"],
                "policy_refs": ["refund_window_standard"],
                "updated_at": now,
                "updated_by": "seed",
            },
            {
                "source_id": "kb_public_shipping",
                "enabled": True,
                "owner": "support-content",
                "visibility": "public",
                "indexed_at": "2026-06-01T00:00:00Z",
                "freshness_slo_hours": 12,
                "topics": ["shipping", "delivery-exceptions", "replacements"],
                "allowed_agents": ["delivery_incidents"],
                "policy_refs": ["replacement_fraud_boundary"],
                "updated_at": now,
                "updated_by": "seed",
            },
            {
                "source_id": "kb_internal_runbooks",
                "enabled": True,
                "owner": "support-ops",
                "visibility": "internal",
                "indexed_at": "2026-06-01T00:00:00Z",
                "freshness_slo_hours": 8,
                "topics": ["handoff", "sla", "incident-response"],
                "allowed_agents": ["support_orchestrator"],
                "updated_at": now,
                "updated_by": "seed",
            },
            {
                "source_id": "kb_internal_payments_runbook",
                "enabled": True,
                "owner": "finance-ops",
                "visibility": "internal",
                "indexed_at": "2026-06-01T00:00:00Z",
                "freshness_slo_hours": 4,
                "topics": ["refunds", "chargebacks", "tax", "processor-errors"],
                "allowed_agents": ["refund_resolution", "billing_disputes"],
                "policy_refs": ["chargeback_no_refund_v1", "refund_window_standard"],
                "updated_at": now,
                "updated_by": "seed",
            },
            {
                "source_id": "kb_internal_ops_runbook",
                "enabled": True,
                "owner": "fulfillment-ops",
                "visibility": "internal",
                "indexed_at": "2026-06-01T00:00:00Z",
                "freshness_slo_hours": 8,
                "topics": ["warehouse", "carrier-escalation", "replacement-controls"],
                "allowed_agents": ["delivery_incidents"],
                "policy_refs": ["replacement_fraud_boundary"],
                "updated_at": now,
                "updated_by": "seed",
            },
            {
                "source_id": "kb_internal_risk_runbook",
                "enabled": True,
                "owner": "risk-ops",
                "visibility": "internal",
                "indexed_at": "2026-06-01T00:00:00Z",
                "freshness_slo_hours": 4,
                "topics": ["fraud", "regulated-boundaries", "law-enforcement"],
                "allowed_agents": ["trust_safety_review"],
                "policy_refs": ["regulated_advice_boundary", "replacement_fraud_boundary"],
                "updated_at": now,
                "updated_by": "seed",
            },
        ],
    )


def apply_drift(db: Any, now: datetime) -> None:
    db.agent_configs.update_one(
        {"config_id": "refund_resolution"},
        {
            "$set": {
                "model": "openai/gpt-4.1",
                "fallback_models": ["anthropic/claude-3-5-sonnet", "openai/gpt-4.1-mini"],
                "automation_threshold": 0.76,
                "max_credit_usd": 500,
                "handoffs": ["billing_disputes", "trust_safety_review", "support_orchestrator"],
                "phase_contract": (
                    "Determine refund eligibility, quote an auditable refund amount, and "
                    "allow enterprise courtesy-credit exceptions before payment mutation."
                ),
                "instructions": (
                    "Use order_ledger before refund_quote. Apply refund_window_standard. "
                    "Enterprise accounts may receive a one-time courtesy credit up to 500 USD "
                    "when customer_tier is enterprise_plus and the renewal date is within 14 "
                    "days. Active chargebacks still require finance handoff."
                ),
                "updated_at": now,
                "updated_by": "admin-console-demo",
            }
        },
    )
    db.policy_rules.update_one(
        {"rule_id": "refund_window_standard"},
        {
            "$set": {
                "severity": "high",
                "approval_policy": "finance_ops_approval",
                "rule_text": (
                    "Refunds are allowed within 45 days of fulfillment for standard and plus "
                    "customers, or within 60 days for enterprise_plus customers. Active "
                    "chargebacks remain ineligible and must be handed off to finance."
                ),
                "customer_copy": (
                    "Eligible orders can be refunded within 45 days, with an enterprise "
                    "exception when payment and account checks pass."
                ),
                "updated_at": now,
                "updated_by": "admin-console-demo",
            }
        },
    )
    db.routing_policies.update_one(
        {"policy_id": "global_support_router"},
        {
            "$set": {
                "min_confidence": 0.68,
                "decision_order": [
                    {"intent": "refund_or_return", "agent": "refund_resolution"},
                    {"intent": "vip_retention_credit", "agent": "refund_resolution"},
                    {"intent": "invoice_or_payment_error", "agent": "billing_disputes"},
                    {"intent": "delivery_exception", "agent": "delivery_incidents"},
                    {"intent": "abuse_or_regulated_boundary", "agent": "trust_safety_review"},
                ],
                "updated_at": now,
                "updated_by": "admin-console-demo",
            }
        },
    )
    db.eval_suites.update_one(
        {"suite_id": "refund_policy_regression"},
        {
            "$set": {
                "required_metrics.eligibility_accuracy": 0.92,
                "required_metrics.approval_recall": 0.94,
                "sample_count": 280,
                "updated_at": now,
                "updated_by": "admin-console-demo",
            }
        },
    )
    db.rollout_controls.update_one(
        {"rollout_id": "refund_agent_june"},
        {
            "$set": {
                "traffic_percent": 35,
                "audience": ["consumer_standard", "consumer_plus", "enterprise_plus"],
                "rollback_on.finance_reversal_rate": 0.04,
                "updated_at": now,
                "updated_by": "admin-console-demo",
            }
        },
    )
    db.tool_registry.update_one(
        {"tool_id": "loyalty_credit_issue"},
        {
            "$set": {
                "tool_id": "loyalty_credit_issue",
                "enabled": True,
                "owner": "retention-platform",
                "capability": "issue_non_cash_courtesy_credit",
                "risk_level": "high",
                "allowed_agents": ["refund_resolution"],
                "scopes": ["credits.quote", "credits.issue.pending_approval"],
                "requires_approval": True,
                "approval_policy": "finance_ops_approval",
                "latency_slo_ms": 1300,
                "credentials": {"api_key_ref": "vault://retention/credit-issuer"},
                "updated_at": now,
                "updated_by": "admin-console-demo",
            }
        },
        upsert=True,
    )


def _replace_many(collection: Any, key: str, docs: list[dict[str, Any]]) -> None:
    collection.delete_many({})
    if not docs:
        return
    collection.insert_many(docs)
    collection.create_index(key, unique=True)


if __name__ == "__main__":
    main()
