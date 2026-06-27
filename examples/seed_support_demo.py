"""Seed a synthetic cfgit demo database.

This fixture is intentionally fake: support agents, model routes, and policy
rules for demo screenshots. It never reads production data.

Typical demo flow:
  python examples/seed_support_demo.py --reset
  cfg --config-file examples/cfgit-support-demo.toml init
  cfg --config-file examples/cfgit-support-demo.toml import --all -m "initial import"
  python examples/seed_support_demo.py --drift
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone

from pymongo import MongoClient


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
    args = parser.parse_args()

    client = MongoClient(args.uri)
    db = client[args.db]
    if args.db in {"admin", "config", "local"}:
        raise SystemExit(f"refusing to seed Mongo system database {args.db!r}")
    if args.reset:
        for name in [
            "agent_configs",
            "modelgarden_models",
            "policy_rules",
            "cfgit_demo_history",
            "cfgit_demo_heads",
            "cfgit_demo_refs",
        ]:
            db.drop_collection(name)

    now = datetime.now(timezone.utc)
    if args.drift:
        apply_drift(db, now)
        print(f"applied synthetic cfgit demo drift in Mongo database {args.db!r}")
        return

    seed_base(db, now)
    print(f"seeded synthetic cfgit demo data in Mongo database {args.db!r}")


def seed_base(db, now: datetime) -> None:
    db.agent_configs.insert_many(
        [
            {
                "config_id": "planner",
                "is_active": True,
                "role": "Support Triage Planner",
                "model": "openai/gpt-4o-mini",
                "tools": ["search", "calendar", "handoff"],
                "fallback_models": ["anthropic/claude-haiku"],
                "phase_contract": (
                    "Produces a support-ticket plan with risk, refund-policy, "
                    "and owner handoff notes."
                ),
                "instructions": (
                    "Classify incoming support tickets, cite the current refund "
                    "policy, and hand off risky cases."
                ),
                "updated_at": now,
                "updated_by": "seed",
            },
            {
                "config_id": "critic",
                "is_active": True,
                "role": "Policy Reviewer",
                "model": "anthropic/claude-haiku",
                "tools": ["diff", "policy_check"],
                "phase_contract": (
                    "Reviews planner decisions for refund-policy compliance and "
                    "escalation risk."
                ),
                "instructions": (
                    "Review planner output from planner, verify refund_window_v1, "
                    "and flag missing restore notes."
                ),
                "updated_at": now,
                "updated_by": "seed",
            },
            {
                "config_id": "router",
                "is_active": True,
                "role": "Support Router",
                "model": "openai/gpt-4o-mini",
                "tools": ["modelgarden"],
                "fallback_models": ["openai/gpt-4o-mini"],
                "phase_contract": (
                    "Routes customer tickets to planner or critic according to "
                    "policy risk."
                ),
                "instructions": (
                    "Use planner for standard tickets and critic when "
                    "refund_window_v1 or escalation rules apply."
                ),
                "updated_at": now,
                "updated_by": "seed",
            },
        ]
    )
    db.modelgarden_models.insert_many(
        [
            {
                "model_path": "openai/gpt-4o-mini",
                "provider": "openai",
                "enabled": True,
                "retry_policy": "standard",
                "price_per_million": 0.15,
                "provider_config": {"api_key": "demo-secret-stays-live"},
            },
            {
                "model_path": "anthropic/claude-haiku",
                "provider": "anthropic",
                "enabled": True,
                "retry_policy": "conservative",
                "price_per_million": 0.8,
                "provider_config": {"api_key": "demo-secret-stays-live"},
            },
        ]
    )
    db.policy_rules.insert_many(
        [
            {
                "rule_id": "refund_window_v1",
                "active": True,
                "title": "Refund window",
                "applies_to": ["planner", "critic", "router"],
                "severity": "medium",
                "rule_text": (
                    "Refunds are allowed within 30 days unless the ticket is "
                    "marked abuse_risk."
                ),
                "updated_at": now,
                "updated_by": "seed",
            },
            {
                "rule_id": "abuse_risk_v1",
                "active": True,
                "title": "Abuse-risk escalation",
                "applies_to": ["critic", "router"],
                "severity": "high",
                "rule_text": (
                    "Cases with repeated refund attempts require human review "
                    "before resolution."
                ),
                "updated_at": now,
                "updated_by": "seed",
            },
        ]
    )


def apply_drift(db, now: datetime) -> None:
    db.agent_configs.update_one(
        {"config_id": "planner"},
        {
            "$set": {
                "model": "openai/gpt-4o-mini-2026-demo",
                "fallback_models": ["anthropic/claude-haiku", "openai/gpt-4o-mini"],
                "phase_contract": (
                    "Produces a support-ticket plan with refund policy, entitlement checks, "
                    "and escalation owner notes."
                ),
                "instructions": (
                    "Classify incoming support tickets, cite refund_window_v1, check "
                    "entitlement tier, and hand off risky cases."
                ),
                "updated_at": now,
                "updated_by": "demo-drift",
            }
        },
    )
    db.policy_rules.update_one(
        {"rule_id": "refund_window_v1"},
        {
            "$set": {
                "rule_text": (
                    "Refunds are allowed within 45 days unless the ticket is marked "
                    "abuse_risk or enterprise_contract_override."
                ),
                "severity": "high",
                "updated_at": now,
                "updated_by": "demo-drift",
            }
        },
    )
    db.agent_configs.update_one(
        {"config_id": "entitlements"},
        {
            "$set": {
                "config_id": "entitlements",
                "is_active": True,
                "role": "Entitlements Resolver",
                "model": "openai/gpt-4o-mini",
                "tools": ["search", "billing", "handoff"],
                "fallback_models": ["anthropic/claude-haiku"],
                "phase_contract": (
                    "Checks plan, renewal date, and regional refund obligations before "
                    "planner response."
                ),
                "instructions": (
                    "Resolve customer entitlement tier and return constraints before "
                    "refunds are discussed."
                ),
                "updated_at": now,
                "updated_by": "demo-drift",
            }
        },
        upsert=True,
    )


if __name__ == "__main__":
    main()
