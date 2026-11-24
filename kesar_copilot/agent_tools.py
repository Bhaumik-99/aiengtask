"""AI reply agent with two strictly read-only business tools (`lookup_order`, `search_faq`).

The model decides which tool(s) to invoke. The server validates arguments,
enforces exact sender-to-order ownership before returning order details, omits
customer PII/addresses, retries transient API errors with exponential backoff,
tracks token/cost telemetry, and verifies factual/policy safety on the final draft.
"""
from __future__ import annotations

import json
import os
import re
import urllib.request
from typing import Any

from kesar_copilot.engine import (
    FAQ_PRODUCTS,
    estimate_cost_usd,
    get_order,
    load_prompt,
    post_chat_with_backoff,
    search_faq,
)

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "lookup_order",
            "description": (
                "Read an order from orders.json only when the sender email matches the checkout email. "
                "Pass order_id as 'KC-####' if known, or null to resolve by the sender's email address."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "order_id": {
                        "type": ["string", "null"],
                        "description": "Order ID such as KC-1041, or null to look up by sender email.",
                    }
                },
                "required": ["order_id"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_faq",
            "description": (
                "Search the approved Kesar & Co. FAQ (`data/faq.md`) for product attributes, prices, "
                "shipping timelines, COD limits/fees, return/refund rules, or damaged/missing item policy."
            ),
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string", "maxLength": 300}},
                "required": ["query"],
                "additionalProperties": False,
            },
        },
    },
]


def execute_tool(name: str, arguments: dict[str, Any], sender_email: str) -> dict[str, Any]:
    """Execute allow-listed read-only tools with server-side ownership & PII filtering."""
    if name == "lookup_order":
        order_id = arguments.get("order_id")
        if order_id is not None and (not isinstance(order_id, str) or not re.fullmatch(r"KC-\d{4}", order_id, re.I)):
            return {"status": "invalid_arguments"}
        order = get_order(order_id=order_id) if order_id else get_order(customer_email=sender_email)
        if order is None:
            return {"status": "not_found_or_ambiguous"}
        if order.get("customer_email", "").casefold() != sender_email.casefold():
            return {"status": "sender_mismatch", "message": "Do not disclose order information."}
        # Deliberately omit customer_email, customer_name, and shipping_address PII,
        # while including non-PII financial/item/shipping fields needed for accurate answers.
        safe_items = [
            {
                "sku": item.get("sku"),
                "name": item.get("name"),
                "qty": item.get("qty"),
                "unit_price": item.get("unit_price"),
            }
            for item in order.get("items", [])
        ]
        return {
            "status": "verified",
            "order_id": order.get("order_id"),
            "order_date": order.get("order_date"),
            "status_label": order.get("status"),
            "courier": order.get("courier"),
            "payment_method": order.get("payment_method"),
            "subtotal": order.get("subtotal"),
            "shipping_fee": order.get("shipping_fee"),
            "cod_fee": order.get("cod_fee"),
            "total": order.get("total"),
            "items": safe_items,
            "shipped_date": order.get("shipped_date"),
            "estimated_delivery": order.get("estimated_delivery"),
            "delivered_date": order.get("delivered_date"),
            "tracking_url": order.get("tracking_url"),
            "cancelled_date": order.get("cancelled_date"),
            "refund_status": order.get("refund_status"),
            "cancellation_reason": order.get("cancellation_reason"),
        }

    if name == "search_faq":
        query = arguments.get("query")
        if not isinstance(query, str) or not query.strip() or len(query) > 300:
            return {"status": "invalid_arguments", "evidence": ""}
        evidence = search_faq(query)
        return {"status": "found" if evidence else "no_evidence", "evidence": evidence}

    return {"status": "tool_not_allowed"}


def _normalize_digits(raw: str) -> str:
    return re.sub(r"[^\d]", "", raw)


def _allowed_numeric_amounts(evidence_text: str) -> set[str]:
    """Extract all integer values present in tool evidence so currency figures are verified."""
    found: set[str] = set()
    for token in re.findall(r"\d[\d,]*", evidence_text):
        norm = _normalize_digits(token)
        if norm:
            found.add(norm)
    return found


def _safe_draft(
    text: Any,
    tool_results: list[dict[str, Any]],
    required_tool: str | None,
    email_text: str = "",
) -> bool:
    """Validate that an agent draft obeys hard business rules and invents no entities.

    Unlike overly rigid word-level whitelists, this validator allows natural English,
    Hindi, and Hinglish phrasing (including negations like 'non-greasy' or 'not vegan')
    while deterministically blocking unauthorized commitments, medical claims,
    phishing prompts, and fabricated URLs, order IDs, ISO dates, or ₹ amounts.
    """
    if not isinstance(text, str):
        return False
    cleaned = text.strip()
    if not (20 <= len(cleaned) <= 1500):
        return False
    lower = cleaned.casefold()

    # 1. Block unauthorized refund/replacement approvals, medical cures, and credential requests.
    forbidden = (
        "refund approved",
        "refund has been approved",
        "your refund is approved",
        "replacement approved",
        "replacement has been shipped",
        "we have issued your refund",
        "we have processed your refund",
        "credited within 24 hours",
        "guaranteed delivery",
        "cure your",
        "cures eczema",
        "cures psoriasis",
        "treat your condition",
        "diagnose",
        "share your otp",
        "send your otp",
        "card pin",
        "share your cvv",
        "your password",
    )
    if any(term in lower for term in forbidden):
        return False

    # 2. Enforce required tool execution & valid evidence status.
    results = [r for r in tool_results if r.get("status") in {"verified", "found"}]
    if required_tool and not any(
        r.get("tool_name") == required_tool and r.get("status") in {"verified", "found"}
        for r in results
    ):
        return False

    # 3. Verify every URL against tool evidence.
    allowed_urls = {r.get("tracking_url") for r in results if r.get("tracking_url")}
    allowed_urls.add("https://kesarandco.example")
    for url in re.findall(r"https?://\S+", cleaned):
        if url.rstrip(".,);]") not in allowed_urls:
            return False

    # 4. Verify every KC-#### order ID against verified tool output.
    allowed_order_ids = {str(r.get("order_id")).upper() for r in results if r.get("order_id")}
    for order_id in re.findall(r"KC-\d{4}", cleaned, re.I):
        if order_id.upper() not in allowed_order_ids:
            return False

    # 5. Verify every ISO date (YYYY-MM-DD) appears in the tool results.
    evidence_text = json.dumps(tool_results, ensure_ascii=False)
    for iso_date in re.findall(r"\b20\d{2}-\d{2}-\d{2}\b", cleaned):
        if iso_date not in evidence_text:
            return False

    # 6. Verify every monetary amount (₹ / INR / Rs) matches a number in tool results.
    allowed_numbers = _allowed_numeric_amounts(evidence_text)
    for amount_match in re.findall(r"(?:₹|INR\s*|Rs\.?\s*)\s?(\d[\d,]*)", cleaned, re.I):
        if _normalize_digits(amount_match) not in allowed_numbers:
            return False

    # 7. Ensure the draft does not hallucinate unreferenced catalog product names.
    combined_context = (evidence_text + " " + email_text).casefold()
    for product in FAQ_PRODUCTS:
        prod_name_lower = product["name"].casefold()
        if prod_name_lower in lower and prod_name_lower not in combined_context:
            return False

    return True


def run_support_agent(
    email: dict[str, Any],
    intent: str,
    task_context: str,
    required_tool: str,
) -> dict[str, Any]:
    """Run a bounded tool-calling reply agent and return a validated, grounded draft."""
    enabled = os.getenv("ENABLE_AGENT_TOOLS", "false").casefold() in {"1", "true", "yes"}
    if not enabled:
        return {"status": "disabled", "tool_calls": [], "draft_body": ""}
    api_key = os.getenv("LLM_API_KEY")
    if not api_key:
        return {"status": "no_api_key", "tool_calls": [], "draft_body": ""}

    base_url = os.getenv("LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    model = os.getenv("LLM_MODEL", "gpt-4o-mini")
    sender_email = email.get("from", {}).get("email", "").strip().casefold()
    email_text = f"{email.get('subject', '')}\n{email.get('body', '')}"
    user_payload = {
        "intent": intent,
        "task_context": task_context,
        "email": {
            "subject": email.get("subject", ""),
            "body": email.get("body", ""),
            "attachment_count": len(email.get("attachments", [])),
        },
    }
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": load_prompt("v2", "agent")},
        {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
    ]

    call_trace: list[str] = []
    tool_results: list[dict[str, Any]] = []
    total_prompt_tokens = 0
    total_completion_tokens = 0
    total_retries = 0

    for _round in range(4):
        payload = {
            "model": model,
            "temperature": 0,
            "messages": messages,
            "tools": TOOLS,
            "tool_choice": "auto",
        }
        result, meta = post_chat_with_backoff(
            base_url + "/chat/completions",
            payload,
            api_key,
            urlopen_fn=urllib.request.urlopen,
        )
        total_retries += meta["retries"]
        total_prompt_tokens += meta["prompt_tokens"]
        total_completion_tokens += meta["completion_tokens"]
        usage_summary = {
            "prompt_tokens": total_prompt_tokens,
            "completion_tokens": total_completion_tokens,
            "total_tokens": total_prompt_tokens + total_completion_tokens,
            "estimated_cost_usd": estimate_cost_usd(total_prompt_tokens, total_completion_tokens),
        }

        if result is None:
            return {
                "status": "failed",
                "tool_calls": call_trace,
                "draft_body": "",
                "retries": total_retries,
                "usage": usage_summary,
            }
        try:
            message = result["choices"][0]["message"]
        except (KeyError, IndexError, TypeError):
            return {
                "status": "failed",
                "tool_calls": call_trace,
                "draft_body": "",
                "retries": total_retries,
                "usage": usage_summary,
            }

        calls = message.get("tool_calls") or []
        if not calls:
            draft_body = message.get("content") or ""
            if not call_trace:
                return {
                    "status": "completed_without_tools",
                    "tool_calls": [],
                    "draft_body": "",
                    "retries": total_retries,
                    "usage": usage_summary,
                }
            if not _safe_draft(draft_body, tool_results, required_tool, email_text=email_text):
                return {
                    "status": "draft_rejected",
                    "tool_calls": call_trace,
                    "draft_body": "",
                    "retries": total_retries,
                    "usage": usage_summary,
                }
            return {
                "status": "completed",
                "tool_calls": call_trace,
                "draft_body": draft_body.strip(),
                "tool_results": tool_results,
                "retries": total_retries,
                "usage": usage_summary,
            }

        messages.append(message)
        for call in calls:
            if len(call_trace) >= 8:
                return {
                    "status": "tool_call_limit",
                    "tool_calls": call_trace,
                    "draft_body": "",
                    "retries": total_retries,
                    "usage": usage_summary,
                }
            call_id = call.get("id", f"call-{len(call_trace) + 1}")
            function = call.get("function", {})
            name = function.get("name", "")
            try:
                args = json.loads(function.get("arguments", "{}"))
            except (TypeError, ValueError):
                args = None
            if not isinstance(args, dict):
                tool_result = {"status": "invalid_arguments"}
            else:
                tool_result = execute_tool(name, args, sender_email)
            call_trace.append(name if name in {"lookup_order", "search_faq"} else "blocked_unknown_tool")
            tool_results.append({"tool_name": name, **tool_result})
            messages.append({
                "role": "tool",
                "tool_call_id": call_id,
                "content": json.dumps(tool_result, ensure_ascii=False),
            })

    return {
        "status": "round_limit",
        "tool_calls": call_trace,
        "draft_body": "",
        "retries": total_retries,
        "usage": {
            "prompt_tokens": total_prompt_tokens,
            "completion_tokens": total_completion_tokens,
            "total_tokens": total_prompt_tokens + total_completion_tokens,
            "estimated_cost_usd": estimate_cost_usd(total_prompt_tokens, total_completion_tokens),
        },
    }


def run_read_only_agent(email: dict[str, Any]) -> dict[str, Any]:
    """Backward-compatible alias used by older callers and tests."""
    return run_support_agent(
        email=email,
        intent="order_status",
        task_context="Look up the customer's order and draft a factual status response.",
        required_tool="lookup_order",
    )
