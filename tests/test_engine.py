import copy
import io
import json
import os
import unittest
import urllib.error
from unittest.mock import patch

from kesar_copilot.engine import (
    CONFIDENCE_THRESHOLD,
    EmailValidationError,
    batch,
    choose_classification,
    classify,
    get_order,
    llm_classify,
    load,
    process,
    valid_classification,
    validate_email,
)


class FakeHTTPResponse:
    def __init__(self, payload):
        self._bytes = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self._bytes


class CopilotTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.emails = load("data/test_emails.json")["emails"]
        cls.by_id = {email["id"]: email for email in cls.emails}

    def setUp(self):
        # Isolate fast deterministic unit tests from any local .env live flags;
        # individual LLM tests explicitly enable flags via patch.dict(os.environ, ...).
        self._env_patch = patch.dict(
            os.environ,
            {"ENABLE_LLM_CLASSIFICATION": "false", "ENABLE_AGENT_TOOLS": "false"},
            clear=False,
        )
        self._env_patch.start()
        self.addCleanup(self._env_patch.stop)

    def test_all_25_fixture_emails_process(self):
        state = batch("v2", self.emails)
        self.assertEqual(len(state["audit"]), 25)
        self.assertEqual(len(state["tickets"]), 9)

    def test_v1_vs_v2_demonstrates_measurable_iteration(self):
        v1_state = batch("v1", self.emails)
        v2_state = batch("v2", self.emails)
        self.assertEqual(len(v1_state["tickets"]), 10)  # E21 duplicated before v2 dedup
        self.assertEqual(len(v2_state["tickets"]), 9)
        v1_by_id = {row["email_id"]: row for row in v1_state["audit"]}
        v2_by_id = {row["email_id"]: row for row in v2_state["audit"]}
        self.assertEqual(v1_by_id["E10"]["action_taken"], "needs_human")
        self.assertEqual(v2_by_id["E10"]["action_taken"], "draft_reply")
        self.assertEqual(v1_by_id["E17"]["intent"], "product_query")
        self.assertEqual(v2_by_id["E17"]["intent"], "other")

    def test_fixture_intents_match_expected_scenarios(self):
        expected = {
            "E01": "order_status", "E02": "product_query", "E03": "return_refund",
            "E04": "complaint", "E05": "order_status", "E06": "other",
            "E07": "order_status", "E08": "complaint", "E09": "product_query",
            "E10": "order_status", "E11": "return_refund", "E12": "return_refund",
            "E13": "other", "E14": "complaint", "E15": "order_status",
            "E16": "return_refund", "E17": "other", "E18": "order_status",
            "E19": "return_refund", "E20": "product_query", "E21": "complaint",
            "E22": "order_status", "E23": "complaint", "E24": "order_status", "E25": "other",
        }
        actual = {email_id: classify(self.by_id[email_id])["intent"] for email_id in expected}
        self.assertEqual(actual, expected)

    def test_order_status_does_not_hallucinate_secondary_product_intent(self):
        for email_id in ("E01", "E07", "E10"):
            result = classify(self.by_id[email_id], version="v2")
            self.assertIsNone(result["secondary_intent"], f"Unexpected secondary_intent for {email_id}")

    def test_order_status_with_real_product_question_sets_secondary_intent(self):
        mixed = copy.deepcopy(self.by_id["E01"])
        mixed["body"] += "\nAlso, is the serum vegan and suitable for oily skin?"
        result = classify(mixed, version="v2")
        self.assertEqual(result["intent"], "order_status")
        self.assertEqual(result["secondary_intent"], "product_query")

    def test_sender_lookup_resolves_unique_order(self):
        self.assertEqual(get_order(customer_email="vikram.singh@example.com")["order_id"], "KC-1044")

    def test_order_status_without_explicit_id_uses_verified_sender(self):
        state = batch("v2", [self.by_id["E10"]])
        audit = state["audit"][0]
        self.assertEqual(audit["order_id"], "KC-1044")
        self.assertEqual(audit["action_taken"], "draft_reply")
        self.assertIn("track.example.com/BX5521044", audit["reply_draft"])

    def test_cod_order_status_includes_payable_total_and_exact_change_tip(self):
        audit = batch("v2", [self.by_id["E15"]])["audit"][0]
        self.assertEqual(audit["action_taken"], "draft_reply")
        self.assertIn("₹1,897", audit["reply_draft"])
        self.assertIn("exact change", audit["reply_draft"].lower())

    def test_another_persons_order_is_never_disclosed(self):
        audit = batch("v2", [self.by_id["E07"]])["audit"][0]
        self.assertEqual(audit["order_lookup_status"], "sender_mismatch")
        self.assertEqual(audit["order_found"], "false")
        self.assertNotIn("track.example.com", audit["reply_draft"])
        self.assertNotIn("delivery address", audit["reply_draft"].lower())

    def test_unknown_order_id_is_sent_for_human_review(self):
        audit = batch("v2", [self.by_id["E22"]])["audit"][0]
        self.assertEqual(audit["action_taken"], "needs_human")
        self.assertEqual(audit["order_found"], "false")
        self.assertNotIn("track.example.com", audit["reply_draft"])

    def test_hindi_delivery_and_cod_query(self):
        audit = batch("v2", [self.by_id["E09"]])["audit"][0]
        self.assertEqual(audit["intent"], "product_query")
        self.assertEqual(audit["language"], "hi")
        self.assertIn("6–8 बिज़नेस दिनों", audit["reply_draft"])
        self.assertIn("₹49", audit["reply_draft"])

    def test_hinglish_order_status_reply_matches_customer_language_and_brand_signoff(self):
        audit = batch("v2", [self.by_id["E05"]])["audit"][0]
        self.assertEqual(audit["language"], "hinglish")
        self.assertIn("shipped status mein", audit["reply_draft"])
        self.assertIn("23 November 2026", audit["reply_draft"])
        self.assertIn("Warm regards,\nTeam Kesar", audit["reply_draft"])

    def test_generalized_faq_answers_other_catalog_products_and_escalates_unknowns(self):
        lotion_q = copy.deepcopy(self.by_id["E02"])
        lotion_q.update({
            "id": "E30",
            "subject": "Is Sandalwood Body Lotion vegan?",
            "body": "Hi, is your Sandalwood Body Lotion vegan and what is its price?",
        })
        lotion_audit = batch("v2", [lotion_q])["audit"][0]
        self.assertEqual(lotion_audit["action_taken"], "draft_reply")
        self.assertIn("beeswax", lotion_audit["reply_draft"].lower())
        self.assertIn("not vegan", lotion_audit["reply_draft"].lower())
        self.assertIn("₹749", lotion_audit["reply_draft"])

        intl_q = copy.deepcopy(self.by_id["E02"])
        intl_q.update({
            "id": "E31",
            "subject": "Shipping to Dubai",
            "body": "Do you ship your products internationally outside India to Dubai?",
        })
        intl_audit = batch("v2", [intl_q])["audit"][0]
        self.assertEqual(intl_audit["action_taken"], "draft_reply")
        self.assertIn("within India only", intl_audit["reply_draft"])

        coupon_q = copy.deepcopy(self.by_id["E02"])
        coupon_q.update({
            "id": "E32",
            "subject": "Coupon code for Aloe gel?",
            "body": "Do you have a discount coupon code or loyalty points for Aloe Calm Gel?",
        })
        coupon_audit = batch("v2", [coupon_q])["audit"][0]
        self.assertEqual(coupon_audit["action_taken"], "needs_human")
        self.assertEqual(coupon_audit["needs_human"], "true")

    def test_missing_item_refund_preference_is_recorded_not_approved(self):
        audit = batch("v2", [self.by_id["E23"]])["audit"][0]
        self.assertEqual(audit["action_taken"], "ticket_created")
        self.assertIn("prefer a refund rather than a resend", audit["reply_draft"])
        self.assertIn("packing-weight record", audit["reply_draft"])
        self.assertIn("Please share clear photos", audit["reply_draft"])
        self.assertNotIn("refund is approved", audit["reply_draft"].lower())

    def test_wrong_catalog_item_delivery_is_routed_to_fulfilment_review(self):
        audit = batch("v2", [self.by_id["E14"]])["audit"][0]
        self.assertEqual(audit["action_taken"], "ticket_created")
        self.assertEqual(audit["policy_decision"], "fulfilment_review")
        self.assertIn("wrong items were delivered", audit["reply_draft"])
        self.assertIn("pickup of the incorrect items", audit["reply_draft"])
        self.assertIn("photos", audit["reply_draft"].lower())

    def test_damaged_ordered_product_is_not_misidentified_as_wrong_item(self):
        audit = batch("v2", [self.by_id["E04"]])["audit"][0]
        self.assertEqual(audit["policy_decision"], "damaged_item_review")
        self.assertNotIn("wrong items were delivered", audit["reply_draft"])

    def test_kit_return_requires_complete_set(self):
        audit = batch("v2", [self.by_id["E11"]])["audit"][0]
        self.assertEqual(audit["policy_decision"], "eligible_for_review")
        self.assertIn("complete set", audit["reply_draft"].lower())
        self.assertNotIn("refund has been approved", audit["reply_draft"].lower())

    def test_partnership_is_not_misclassified_as_product_query(self):
        audit = batch("v2", [self.by_id["E17"]])["audit"][0]
        self.assertEqual(audit["intent"], "other")
        self.assertEqual(audit["action_taken"], "log_only")
        self.assertEqual(audit["reply_draft"], "")

    def test_invoice_request_is_logged_without_unimplemented_outbound_route(self):
        audit = batch("v2", [self.by_id["E13"]])["audit"][0]
        self.assertEqual(audit["action_taken"], "log_only")
        self.assertEqual(audit["reply_draft"], "")
        self.assertIn("invoice", audit["notes"].lower())

    def test_ambiguous_low_confidence_message_is_not_automated(self):
        audit = batch("v2", [self.by_id["E18"]])["audit"][0]
        self.assertLess(audit["confidence"], CONFIDENCE_THRESHOLD)
        self.assertEqual(audit["action_taken"], "needs_human")
        self.assertEqual(audit["order_lookup_status"], "not_requested")

    def test_cancelled_order_uses_recorded_refund_status(self):
        audit = batch("v2", [self.by_id["E24"]])["audit"][0]
        self.assertIn("cancelled", audit["reply_draft"].lower())
        self.assertIn("Refund of INR 828 initiated", audit["reply_draft"])
        self.assertNotIn("tracking:", audit["reply_draft"].lower())

    def test_cod_return_explains_route_without_approving_refund(self):
        audit = batch("v2", [self.by_id["E19"]])["audit"][0]
        self.assertIn("UPI or bank transfer", audit["reply_draft"])
        self.assertNotIn("refund has been approved", audit["reply_draft"].lower())
        self.assertEqual(audit["policy_decision"], "eligible_for_review")

    def test_prompt_injection_cannot_approve_refund(self):
        audit = batch("v2", [self.by_id["E12"]])["audit"][0]
        self.assertNotIn("approved your refund", audit["reply_draft"].lower())
        self.assertIn("opened or used", audit["reply_draft"].lower())
        self.assertEqual(audit["needs_human"], "true")

    def test_attachment_filenames_are_not_treated_as_verified_images(self):
        audit = batch("v2", [self.by_id["E21"]])["audit"][0]
        self.assertIn("cannot inspect image contents", audit["reply_draft"])
        self.assertIn("must be verified by a person", audit["notes"])

    def test_medical_question_is_escalated_without_treatment_claim(self):
        audit = batch("v2", [self.by_id["E20"]])["audit"][0]
        self.assertEqual(audit["action_taken"], "needs_human")
        self.assertIn("not medicines", audit["reply_draft"])
        self.assertNotIn("will cure", audit["reply_draft"].lower())

    def test_ticket_followup_updates_existing_ticket_and_appends_followup_note(self):
        state = batch("v2", [self.by_id["E04"], self.by_id["E21"]])
        self.assertEqual(len(state["tickets"]), 1)
        ticket = state["tickets"][0]
        self.assertEqual(ticket["email_ids"], "E04,E21")
        self.assertIn("Follow-up E21", ticket["notes"])
        self.assertIn("customer requested replacement", ticket["notes"])
        self.assertIn("cracked_jar.jpg", ticket["notes"])

    def test_unidentified_complaints_do_not_merge_into_blank_order_ticket(self):
        first = copy.deepcopy(self.by_id["E04"])
        first.update({"id": "E26", "subject": "Damaged item", "body": "My product arrived damaged. Please help."})
        second = copy.deepcopy(self.by_id["E04"])
        second.update({"id": "E27", "subject": "Missing item", "body": "An item is missing from my package. Please investigate."})
        for email in (first, second):
            email["from"] = {"name": "Unknown Customer", "email": "unknown.customer@example.com"}
        state = batch("v2", [first, second])
        self.assertEqual(len(state["tickets"]), 2)
        self.assertEqual([ticket["email_ids"] for ticket in state["tickets"]], ["E26", "E27"])

    def test_reprocessing_same_email_is_idempotent(self):
        state = {"audit": [], "tickets": []}
        first = process(self.by_id["E04"], state)
        second = process(self.by_id["E04"], state)
        self.assertEqual(first, second)
        self.assertEqual(len(state["audit"]), 1)
        self.assertEqual(len(state["tickets"]), 1)

    def test_classification_validator_rejects_bad_shapes(self):
        self.assertFalse(valid_classification([]))
        valid = classify(self.by_id["E01"])
        self.assertTrue(valid_classification(valid))
        invalid = copy.deepcopy(valid)
        invalid["confidence"] = True
        self.assertFalse(valid_classification(invalid))
        same_sec = copy.deepcopy(valid)
        same_sec["secondary_intent"] = same_sec["intent"]
        self.assertFalse(valid_classification(same_sec))

    def test_llm_classify_repairs_invalid_json_and_retries_429_with_token_tracking(self):
        rate_limited = urllib.error.HTTPError(
            url="https://api.openai.com/v1/chat/completions",
            code=429,
            msg="Too Many Requests",
            hdrs=None,
            fp=io.BytesIO(b"{}"),
        )
        bad_first = {
            "choices": [{"message": {"content": "{not valid json"}}],
            "usage": {"prompt_tokens": 500, "completion_tokens": 30, "total_tokens": 530},
        }
        repaired_second = {
            "choices": [{
                "message": {
                    "content": json.dumps({
                        "intent": "order_status",
                        "secondary_intent": None,
                        "order_id": "KC-1041",
                        "sentiment": "neutral",
                        "language": "en",
                        "confidence": 0.94,
                        "summary": "Customer asking when order KC-1041 will arrive.",
                    })
                }
            }],
            "usage": {"prompt_tokens": 620, "completion_tokens": 60, "total_tokens": 680},
        }
        state = {"audit": [], "tickets": []}
        with patch.dict(os.environ, {
            "LLM_API_KEY": "test-key",
            "ENABLE_LLM_CLASSIFICATION": "true",
            "ENABLE_AGENT_TOOLS": "false",
            "LLM_RETRY_BASE_SECONDS": "0",
        }, clear=False):
            with patch(
                "kesar_copilot.engine.urllib.request.urlopen",
                side_effect=[rate_limited, FakeHTTPResponse(bad_first), FakeHTTPResponse(repaired_second)],
            ) as mocked:
                audit = process(self.by_id["E01"], state, version="v2")

        self.assertEqual(mocked.call_count, 3)
        self.assertEqual(audit["classification_source"], "rules_llm_agreement")
        self.assertEqual(audit["json_valid_first_try"], "false")
        self.assertEqual(audit["llm_retries"], 1)
        self.assertEqual(audit["prompt_tokens"], 1120)
        self.assertEqual(audit["completion_tokens"], 90)
        self.assertEqual(audit["total_tokens"], 1210)
        self.assertGreater(float(audit["estimated_cost_usd"]), 0.0)

    def test_llm_classify_loads_external_prompt_markdown(self):
        good = {
            "choices": [{
                "message": {
                    "content": json.dumps({
                        "intent": "order_status",
                        "secondary_intent": None,
                        "order_id": "KC-1041",
                        "sentiment": "neutral",
                        "language": "en",
                        "confidence": 0.95,
                        "summary": "Order status check for KC-1041.",
                    })
                }
            }],
            "usage": {"prompt_tokens": 400, "completion_tokens": 50},
        }
        with patch.dict(os.environ, {"LLM_API_KEY": "test-key", "LLM_RETRY_BASE_SECONDS": "0"}, clear=False):
            with patch("kesar_copilot.engine.urllib.request.urlopen", return_value=FakeHTTPResponse(good)) as mocked:
                res = llm_classify(self.by_id["E01"])
        self.assertIsNotNone(res)
        sent_payload = json.loads(mocked.call_args.args[0].data)
        self.assertIn("untrusted customer input", sent_payload["messages"][0]["content"].lower())

    def test_email_validator_rejects_malformed_nested_sender(self):
        malformed = copy.deepcopy(self.by_id["E01"])
        malformed["from"] = "customer@example.com"
        with self.assertRaises(EmailValidationError):
            validate_email(malformed)

    def test_email_validator_requires_timezone_aware_timestamp(self):
        malformed = copy.deepcopy(self.by_id["E01"])
        malformed["received_at"] = "2026-11-20T10:00:00"
        with self.assertRaises(EmailValidationError):
            validate_email(malformed)

    def test_audit_marks_heuristic_confidence_as_uncalibrated(self):
        audit = batch("v2", [self.by_id["E01"]])["audit"][0]
        self.assertEqual(audit["confidence_is_calibrated"], "false")

    def test_sensitive_health_content_is_not_sent_to_optional_agent(self):
        email = self.by_id["E20"]
        state = {"audit": [], "tickets": []}
        with patch.dict(os.environ, {"ENABLE_AGENT_TOOLS": "true", "LLM_API_KEY": "test-key"}, clear=False):
            with patch("kesar_copilot.agent_tools.urllib.request.urlopen", side_effect=AssertionError("must not send health content")):
                audit = process(email, state, version="v2")
        self.assertEqual(audit["agent_status"], "skipped_sensitive_content")

    def test_v1_never_calls_optional_agent_tools(self):
        state = {"audit": [], "tickets": []}
        with patch.dict(os.environ, {"ENABLE_AGENT_TOOLS": "true", "LLM_API_KEY": "test-key"}, clear=False):
            with patch("kesar_copilot.agent_tools.urllib.request.urlopen", side_effect=AssertionError("v1 must remain deterministic")):
                audit = process(self.by_id["E01"], state, version="v1")
        self.assertEqual(audit["agent_status"], "disabled_for_v1")

    def test_llm_self_reported_confidence_is_not_trusted(self):
        email = self.by_id["E01"]
        model_result = dict(classify(email))
        model_result["confidence"] = 1.0
        with patch.dict(os.environ, {"ENABLE_LLM_CLASSIFICATION": "true"}, clear=False):
            with patch("kesar_copilot.engine.llm_classify", return_value=model_result):
                selected, source, disagreement = choose_classification(email)
        self.assertEqual(source, "rules_llm_agreement")
        self.assertFalse(disagreement)
        self.assertEqual(selected["confidence"], classify(email)["confidence"])

    def test_llm_disagreement_forces_human_review(self):
        email = self.by_id["E01"]
        model_result = dict(classify(email))
        model_result["intent"] = "product_query"
        with patch.dict(os.environ, {"ENABLE_LLM_CLASSIFICATION": "true"}, clear=False):
            with patch("kesar_copilot.engine.llm_classify", return_value=model_result):
                selected, source, disagreement = choose_classification(email)
        self.assertTrue(disagreement)
        self.assertEqual(source, "rules_llm_disagreement")
        self.assertLess(selected["confidence"], CONFIDENCE_THRESHOLD)
        state = {"audit": [], "tickets": []}
        with patch.dict(os.environ, {"ENABLE_LLM_CLASSIFICATION": "true"}, clear=False):
            with patch("kesar_copilot.engine.llm_classify", return_value=model_result):
                audit = process(email, state, version="v2")
        self.assertEqual(audit["action_taken"], "needs_human")

    def test_unverified_return_still_creates_human_ticket(self):
        email = copy.deepcopy(self.by_id["E22"])
        email["id"] = "E26"
        email["subject"] = "Damaged product KC-1099"
        email["body"] = "My product is damaged. Please help with a refund for order KC-1099."
        state = batch("v2", [email])
        self.assertEqual(len(state["tickets"]), 1)
        self.assertEqual(state["audit"][0]["action_taken"], "ticket_created")
        self.assertEqual(state["audit"][0]["order_found"], "false")
        self.assertEqual(state["audit"][0]["needs_human"], "true")

    def test_other_branch_is_log_only_with_no_customer_draft(self):
        for email_id in ("E06", "E13", "E17", "E25"):
            audit = batch("v2", [self.by_id[email_id]])["audit"][0]
            self.assertEqual(audit["action_taken"], "log_only")
            self.assertEqual(audit["reply_draft"], "")

    def test_agent_draft_is_used_only_after_relevant_tool_completion(self):
        email = self.by_id["E01"]
        state = {"audit": [], "tickets": []}
        agent_result = {
            "status": "completed",
            "tool_calls": ["lookup_order"],
            "draft_body": "Your order is currently shipped, and the recorded estimated delivery date is 2026-11-21.",
            "usage": {"prompt_tokens": 500, "completion_tokens": 40, "total_tokens": 540, "estimated_cost_usd": 0.0001},
        }
        with patch.dict(os.environ, {"ENABLE_AGENT_TOOLS": "true", "LLM_API_KEY": "test-key"}, clear=False):
            with patch("kesar_copilot.agent_tools.run_support_agent", return_value=agent_result) as mocked:
                audit = process(email, state, version="v2")
        self.assertTrue(mocked.called)
        self.assertEqual(audit["agent_draft_used"], "true")
        self.assertIn("estimated delivery date is 2026-11-21", audit["reply_draft"])

    def test_agent_draft_on_return_preserves_ticket_and_needs_human(self):
        email = self.by_id["E03"]
        state = {"audit": [], "tickets": []}
        agent_result = {
            "status": "completed",
            "tool_calls": ["lookup_order", "search_faq"],
            "draft_body": "Order KC-1047 was delivered on 2026-11-16 and meets our 7-day unopened return criteria. A specialist will arrange pickup within 2-3 business days.",
            "usage": {"prompt_tokens": 700, "completion_tokens": 55, "total_tokens": 755, "estimated_cost_usd": 0.00014},
        }
        with patch.dict(os.environ, {"ENABLE_AGENT_TOOLS": "true", "LLM_API_KEY": "test-key"}, clear=False):
            with patch("kesar_copilot.agent_tools.run_support_agent", return_value=agent_result):
                audit = process(email, state, version="v2")
        self.assertEqual(audit["action_taken"], "ticket_created")
        self.assertEqual(audit["needs_human"], "true")
        self.assertEqual(audit["ticket_id"], "TKT-001")
        self.assertEqual(audit["agent_draft_used"], "true")

    def test_psoriasis_and_acne_queries_are_escalated_without_llm_disclosure(self):
        for email_id, condition in (("E28", "psoriasis"), ("E29", "acne")):
            email = copy.deepcopy(self.by_id["E20"])
            email["id"] = email_id
            email["subject"] = f"Product advice for {condition}"
            email["body"] = f"I have {condition}. Is Kumkumadi Night Oil suitable for me?"
            state = {"audit": [], "tickets": []}
            with patch.dict(os.environ, {
                "ENABLE_LLM_CLASSIFICATION": "true", "ENABLE_AGENT_TOOLS": "true", "LLM_API_KEY": "test-key"
            }, clear=False):
                with patch("kesar_copilot.engine.llm_classify", side_effect=AssertionError("health content must not be sent")):
                    with patch("kesar_copilot.agent_tools.urllib.request.urlopen", side_effect=AssertionError("health content must not be sent")):
                        audit = process(email, state, version="v2")
            self.assertEqual(audit["intent"], "product_query")
            self.assertEqual(audit["action_taken"], "needs_human")
            self.assertEqual(audit["agent_status"], "skipped_sensitive_content")

    def test_sensitive_health_content_skips_both_optional_llm_calls(self):
        email = self.by_id["E20"]
        state = {"audit": [], "tickets": []}
        with patch.dict(os.environ, {
            "ENABLE_AGENT_TOOLS": "true", "ENABLE_LLM_CLASSIFICATION": "true", "LLM_API_KEY": "test-key"
        }, clear=False):
            with patch("kesar_copilot.engine.llm_classify", side_effect=AssertionError("health content must not be sent")):
                with patch("kesar_copilot.agent_tools.urllib.request.urlopen", side_effect=AssertionError("health content must not be sent")):
                    audit = process(email, state, version="v2")
        self.assertEqual(audit["classification_source"], "rules_sensitive_fallback")
        self.assertEqual(audit["agent_status"], "skipped_sensitive_content")

    def test_low_confidence_complaint_still_creates_ticket(self):
        email = copy.deepcopy(self.by_id["E04"])
        model_result = dict(classify(email))
        model_result["intent"] = "order_status"
        with patch.dict(os.environ, {"ENABLE_LLM_CLASSIFICATION": "true"}, clear=False):
            with patch("kesar_copilot.engine.llm_classify", return_value=model_result):
                state = batch("v2", [email])
        self.assertEqual(len(state["tickets"]), 1)
        self.assertEqual(state["audit"][0]["action_taken"], "ticket_created")
        self.assertEqual(state["audit"][0]["needs_human"], "true")


if __name__ == "__main__":
    unittest.main(verbosity=2)
