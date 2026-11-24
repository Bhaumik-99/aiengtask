"""Evaluate both v1 (baseline) and v2 (final) against fixture regression expectations.

v2 must match 25/25 intents and 25/25 actions with 9 deduplicated tickets.
v1 metrics are recorded alongside v2 to document the engineering iteration.
"""
from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from kesar_copilot.engine import ROOT, batch, load


def _evaluate_version(version: str, emails: list[dict[str, Any]], gold: dict[str, dict[str, str]]) -> dict[str, Any]:
    state = batch(version, emails)
    actual = {row["email_id"]: row for row in state["audit"]}
    if set(gold) != set(actual):
        raise SystemExit(f"Fixture/label IDs differ for {version}: expected={len(gold)} actual={len(actual)}")

    intent_matches = 0
    action_matches = 0
    confusion: dict[str, Counter[str]] = defaultdict(Counter)
    mismatches = []
    for email_id, expected_row in gold.items():
        observed = actual[email_id]
        expected_intent = expected_row["intent"]
        observed_intent = observed["intent"]
        confusion[expected_intent][observed_intent] += 1
        intent_ok = expected_intent == observed_intent
        action_ok = expected_row["action"] == observed["action_taken"]
        intent_matches += int(intent_ok)
        action_matches += int(action_ok)
        if not (intent_ok and action_ok):
            mismatches.append({
                "email_id": email_id,
                "expected_intent": expected_intent,
                "actual_intent": observed_intent,
                "expected_action": expected_row["action"],
                "actual_action": observed["action_taken"],
            })

    total = len(gold)
    needs_human_count = sum(1 for row in state["audit"] if row.get("needs_human") == "true")
    return {
        "version": version,
        "sample_count": total,
        "intent_matches": intent_matches,
        "intent_match_rate": round(intent_matches / total, 4) if total else 0,
        "action_matches": action_matches,
        "action_match_rate": round(action_matches / total, 4) if total else 0,
        "unique_tickets": len(state["tickets"]),
        "needs_human_count": needs_human_count,
        "confusion_matrix": {key: dict(value) for key, value in confusion.items()},
        "mismatches": mismatches,
    }


def main() -> None:
    emails = load("data/test_emails.json")["emails"]
    expected = load("data/expected_outcomes.json")["items"]
    gold = {row["email_id"]: row for row in expected}

    v1_eval = _evaluate_version("v1", emails, gold)
    v2_eval = _evaluate_version("v2", emails, gold)

    report = {
        "evaluation_type": "developer-authored fixture regression expectations",
        "not_a_holdout_benchmark": True,
        "sample_count": v2_eval["sample_count"],
        "intent_matches": v2_eval["intent_matches"],
        "intent_match_rate": v2_eval["intent_match_rate"],
        "action_matches": v2_eval["action_matches"],
        "action_match_rate": v2_eval["action_match_rate"],
        "unique_tickets": v2_eval["unique_tickets"],
        "mismatches": v2_eval["mismatches"],
        "v1_baseline": v1_eval,
        "v2_final": v2_eval,
        "interpretation": (
            "These labels were written for regression testing against the 25 supplied fixture examples. "
            "v1 captures initial baseline gaps (Hindi query, sender-email order fallback, collab precedence, "
            "and duplicate ticket creation); v2 resolves all 25 fixture scenarios."
        ),
    }
    output_path = ROOT / "outputs" / "fixture_evaluation.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "v1": {k: v1_eval[k] for k in ("intent_matches", "action_matches", "unique_tickets")},
        "v2": {k: v2_eval[k] for k in ("sample_count", "intent_matches", "intent_match_rate", "action_matches", "action_match_rate", "unique_tickets", "mismatches")},
    }, indent=2))
    if v2_eval["mismatches"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
