import io
import json
import os
import unittest
import urllib.error
from unittest.mock import patch

from kesar_copilot.agent_tools import _safe_draft, execute_tool, run_read_only_agent, run_support_agent
from kesar_copilot.engine import load


class FakeResponse:
    def __init__(self, payload):
        self.payload = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self.payload


class AgentToolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.emails = load("data/test_emails.json")["emails"]
        cls.email = cls.emails[0]

    def test_order_lookup_tool_enforces_sender_ownership(self):
        result = execute_tool("lookup_order", {"order_id": "KC-1046"}, "arjun@example.com")
        self.assertEqual(result["status"], "sender_mismatch")
        self.assertNotIn("tracking_url", result)

    def test_order_lookup_tool_returns_safe_fields_including_total_and_items_to_owner(self):
        result = execute_tool("lookup_order", {"order_id": "KC-1042"}, "rohit.mehta@example.com")
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["total"], 1897)
        self.assertEqual(result["cod_fee"], 49)
        self.assertIn("tracking_url", result)
        self.assertNotIn("shipping_address", result)
        self.assertNotIn("customer_email", result)

    def test_unknown_write_tool_is_blocked(self):
        self.assertEqual(
            execute_tool("approve_refund", {"order_id": "KC-1041"}, "ananya.rao@example.com")["status"],
            "tool_not_allowed",
        )

    def test_tool_agent_executes_tool_call_then_finishes(self):
        email = dict(self.email)
        email["from"] = {"name": "Ananya", "email": "ananya.rao@example.com"}
        first = {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [{
            "id": "call-1", "type": "function", "function": {"name": "lookup_order", "arguments": '{"order_id":"KC-1041"}'}
        }]}}], "usage": {"prompt_tokens": 350, "completion_tokens": 25}}
        second = {"choices": [{"message": {
            "role": "assistant",
            "content": "Your order KC-1041 has shipped via SwiftShip and should reach you by November 21, 2026. Track it here: https://track.example.com/SS7781041.",
        }}], "usage": {"prompt_tokens": 480, "completion_tokens": 38}}
        with patch.dict(os.environ, {"ENABLE_AGENT_TOOLS": "true", "LLM_API_KEY": "test-key", "LLM_RETRY_BASE_SECONDS": "0"}, clear=False):
            with patch("kesar_copilot.agent_tools.urllib.request.urlopen", side_effect=[FakeResponse(first), FakeResponse(second)]) as mocked:
                result = run_read_only_agent(email)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["tool_calls"], ["lookup_order"])
        self.assertEqual(mocked.call_count, 2)
        self.assertEqual(result["usage"]["total_tokens"], 893)
        second_request = json.loads(mocked.call_args_list[1].args[0].data)
        tool_message = next(message for message in second_request["messages"] if message["role"] == "tool")
        self.assertIn("verified", tool_message["content"])

    def test_tool_agent_is_disabled_by_default(self):
        with patch.dict(os.environ, {"ENABLE_AGENT_TOOLS": "false"}, clear=False):
            result = run_read_only_agent(self.email)
        self.assertEqual(result, {"status": "disabled", "tool_calls": [], "draft_body": ""})

    def test_product_query_agent_allows_natural_negation_and_hinglish(self):
        email = self.emails[1]
        first = {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [{
            "id": "faq-call", "type": "function", "function": {
                "name": "search_faq", "arguments": '{"query":"Saffron serum oily skin vegan"}'
            }
        }]}}]}
        second = {"choices": [{"message": {
            "role": "assistant",
            "content": "The Saffron Glow Face Serum (₹1,299) is lightweight, will not make your skin greasy, works well for oily skin, and is 100% vegan."
        }}]}
        with patch.dict(os.environ, {"ENABLE_AGENT_TOOLS": "true", "LLM_API_KEY": "test-key"}, clear=False):
            with patch("kesar_copilot.agent_tools.urllib.request.urlopen", side_effect=[FakeResponse(first), FakeResponse(second)]):
                result = run_support_agent(email, "product_query", "Answer the FAQ question.", "search_faq")
        self.assertEqual(result["status"], "completed")
        self.assertIn("will not make your skin greasy", result["draft_body"])

    def test_hinglish_agent_draft_passes_entity_guardrail(self):
        tool_res = [{"tool_name": "lookup_order", **execute_tool("lookup_order", {"order_id": "KC-1043"}, "priya.sharma@example.com")}]
        hinglish = "Aapka order KC-1043 abhi shipped hai aur 2026-11-23 tak deliver hone ki umeed hai. Tracking link: https://track.example.com/SS7781043"
        self.assertTrue(_safe_draft(hinglish, tool_res, "lookup_order", email_text="Aloe Calm Gel KC-1043"))

    def test_safe_draft_blocks_hallucinated_urls_orders_dates_amounts_and_approvals(self):
        tool_res = [{"tool_name": "lookup_order", **execute_tool("lookup_order", {"order_id": "KC-1041"}, "ananya.rao@example.com")}]
        # Hallucinated URL
        self.assertFalse(_safe_draft("Track KC-1041 at https://evil.example.com/123.", tool_res, "lookup_order"))
        # Hallucinated Order ID
        self.assertFalse(_safe_draft("Order KC-9999 is shipped and arriving on 2026-11-21.", tool_res, "lookup_order"))
        # Hallucinated ISO Date
        self.assertFalse(_safe_draft("Order KC-1041 is arriving on 2026-12-25.", tool_res, "lookup_order"))
        # Hallucinated ₹ Amount
        self.assertFalse(_safe_draft("Order KC-1041 total is ₹9,999 and arrives 2026-11-21.", tool_res, "lookup_order"))
        # Unauthorized refund approval
        self.assertFalse(_safe_draft("Your refund has been approved for order KC-1041.", tool_res, "lookup_order"))
        # Hallucinated unrelated product name
        self.assertFalse(_safe_draft("Your Bhringraj Hair Oil in KC-1041 arrives on 2026-11-21.", tool_res, "lookup_order"))

    def test_agent_retries_transient_503_before_succeeding(self):
        transient = urllib.error.HTTPError(
            url="https://api.openai.com/v1/chat/completions",
            code=503,
            msg="Service Unavailable",
            hdrs=None,
            fp=io.BytesIO(b"{}"),
        )
        first = {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [{
            "id": "call-1", "type": "function", "function": {"name": "lookup_order", "arguments": '{"order_id":"KC-1041"}'}
        }]}}]}
        second = {"choices": [{"message": {"role": "assistant", "content": "Order KC-1041 is currently shipped and estimated for 2026-11-21."}}]}
        with patch.dict(os.environ, {"ENABLE_AGENT_TOOLS": "true", "LLM_API_KEY": "test-key", "LLM_RETRY_BASE_SECONDS": "0"}, clear=False):
            with patch("kesar_copilot.agent_tools.urllib.request.urlopen", side_effect=[transient, FakeResponse(first), FakeResponse(second)]):
                result = run_read_only_agent(self.email)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["retries"], 1)

    def test_agent_without_required_tool_cannot_produce_customer_draft(self):
        final_only = {"choices": [{"message": {"role": "assistant", "content": "Your order is on the way and should arrive soon."}}]}
        with patch.dict(os.environ, {"ENABLE_AGENT_TOOLS": "true", "LLM_API_KEY": "test-key"}, clear=False):
            with patch("kesar_copilot.agent_tools.urllib.request.urlopen", return_value=FakeResponse(final_only)):
                result = run_read_only_agent(self.email)
        self.assertEqual(result["status"], "completed_without_tools")
        self.assertEqual(result["draft_body"], "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
