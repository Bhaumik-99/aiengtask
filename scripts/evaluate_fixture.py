"""Evaluate v1 (baseline) and v2 (final live or offline run) against fixture expectations.

By default, evaluates saved `outputs/results_v1.json` and `outputs/results_v2.json`
when present so live LLM run telemetry is preserved without re-triggering API calls.
Pass `--recompute-offline` to force a fresh deterministic offline evaluation.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from kesar_copilot.engine import ROOT, batch, load


def _load_or_compute(version: str, emails: list[dict[str, Any]], recompute_offline: bool) -> dict[str, Any]:
    saved_path = ROOT / "outputs" / f"results_{version}.json"
    if not recompute_offline and saved_path.is_file():
        return json.loads(saved_path.read_text(encoding="utf-8"))
    prev_cls = os.environ.get("ENABLE_LLM_CLASSIFICATION")
    prev_agt = os.environ.get("ENABLE_AGENT_TOOLS")
    try:
        os.environ["ENABLE_LLM_CLASSIFICATION"] = "false"
        os.environ["ENABLE_AGENT_TOOLS"] = "false"
        return batch(version, emails)
    finally:
        if prev_cls is None:
            os.environ.pop("ENABLE_LLM_CLASSIFICATION", None)
        else:
            os.environ["ENABLE_LLM_CLASSIFICATION"] = prev_cls
        if prev_agt is None:
            os.environ.pop("ENABLE_AGENT_TOOLS", None)
        else:
            os.environ["ENABLE_AGENT_TOOLS"] = prev_agt


def _evaluate_state(version: str, state: dict[str, Any], gold: dict[str, dict[str, str]]) -> dict[str, Any]:
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
    audit_rows = state["audit"]
    needs_human_count = sum(1 for row in audit_rows if row.get("needs_human") == "true")
    total_tokens = sum(int(row.get("total_tokens") or 0) for row in audit_rows)
    total_cost_usd = round(sum(float(row.get("estimated_cost_usd") or 0.0) for row in audit_rows), 6)
    agent_drafts_used = sum(1 for row in audit_rows if row.get("agent_draft_used") == "true")
    valid_json_first_try = sum(1 for row in audit_rows if row.get("json_valid_first_try") == "true")
    llm_classified_count = sum(1 for row in audit_rows if row.get("json_valid_first_try") in {"true", "false"})
    return {
        "version": version,
        "sample_count": total,
        "intent_matches": intent_matches,
        "intent_match_rate": round(intent_matches / total, 4) if total else 0,
        "action_matches": action_matches,
        "action_match_rate": round(action_matches / total, 4) if total else 0,
        "unique_tickets": len(state["tickets"]),
        "needs_human_count": needs_human_count,
        "llm_classified_count": llm_classified_count,
        "valid_json_first_try": valid_json_first_try,
        "agent_drafts_used": agent_drafts_used,
        "total_tokens": total_tokens,
        "avg_tokens_per_email": round(total_tokens / total, 1) if total else 0,
        "total_estimated_cost_usd": total_cost_usd,
        "confusion_matrix": {key: dict(value) for key, value in confusion.items()},
        "mismatches": mismatches,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate v1 and v2 fixture outputs")
    parser.add_argument("--recompute-offline", action="store_true", help="Ignore saved results_*.json and run offline")
    args = parser.parse_args()

    emails = load("data/test_emails.json")["emails"]
    expected = load("data/expected_outcomes.json")["items"]
    gold = {row["email_id"]: row for row in expected}

    v1_state = _load_or_compute("v1", emails, args.recompute_offline)
    v2_state = _load_or_compute("v2", emails, args.recompute_offline)
    v1_eval = _evaluate_state("v1", v1_state, gold)
    v2_eval = _evaluate_state("v2", v2_state, gold)

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
            "v2 was executed live against Groq (openai/gpt-oss-120b) with tool-calling enabled."
        ),
    }
    output_path = ROOT / "outputs" / "fixture_evaluation.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "v1": {k: v1_eval[k] for k in ("intent_matches", "action_matches", "unique_tickets")},
        "v2": {
            k: v2_eval[k]
            for k in (
                "sample_count", "intent_matches", "action_matches", "unique_tickets",
                "valid_json_first_try", "agent_drafts_used", "total_tokens", "total_estimated_cost_usd", "mismatches",
            )
        },
    }, indent=2))
    if v2_eval["mismatches"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
