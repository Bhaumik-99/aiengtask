# Architecture and Trust Boundaries

```text
Inbound provider event / 25 supplied fixture emails
        |
        v
Payload validation + email-ID idempotency + medical privacy screen
        |
        v
LLM structured classification (temp=0, 1-turn JSON repair, backoff retries)
        +---- cross-checked with deterministic baseline ----+----> disagreement -> needs_human
        |
        v
Heuristic / pilot confidence gate (0.75)
        |
        v
Deterministic order lookup (KC-#### or unique customer_email) + sender ownership gate
        |          mismatch / not_found -> zero order facts disclosed
        v
Deterministic policy & date-arithmetic engine (Scenario date: 2026-11-20 via received_at)
        |-- return_refund / complaint -> deduplicated ticket create/update + needs_human=true
        |-- medical / uncovered FAQ / ambiguous -> needs_human=true
        |-- other -> log_only (no customer reply)
        |-- grounded reply drafting (order_status, product_query, return_refund, complaint)
                     |
                     v
             Optional AI Reply Agent (`kesar_copilot/agent_tools.py`)
                |-- model calls read-only `lookup_order` and/or `search_faq`
                |-- server enforces sender ownership & strips shipping_address PII
                |-- `_safe_draft()` validates URLs, KC-####, ISO dates, ₹ amounts & SKUs
                |-- deterministic policy/FAQ draft fallback when disabled or rejected
        |
        v
SQLite / CSV primary persistence (with token & USD cost telemetry)
        |
        +---- optional idempotent Google Sheets upsert (`Audit Log` + `Tickets`)
        v
Human review before any outbound email or refund/replacement approval
```

## Trust Boundaries

- **Customer content:** `subject` and `body` are untrusted input. Embedded prompt injections (`E12`) cannot register tools, alter `needs_human`, lower ticket priority, or approve refunds.
- **LLM provider:** Optional (`ENABLE_LLM_CLASSIFICATION`, `ENABLE_AGENT_TOOLS`). Sender name/email and attachment filenames are omitted from prompts, and health/allergy/pregnancy/skin-condition messages (`E20`) are never sent to external LLM endpoints.
- **Order ownership:** Server-side checks require exact case-insensitive match between `from.email` and `order.customer_email` before returning order status, items, totals, or tracking links (`E07`). Shipping street addresses are stripped even for verified owners.
- **Read-only tool allow-list:** Only `lookup_order` and `search_faq` exist. Unknown tool names return `tool_not_allowed`. No write/refund/cancel/send tools exist.
- **Entity-level draft guardrail (`_safe_draft`):** Verifies that every URL, `KC-####` ID, `YYYY-MM-DD` date, ₹/INR/Rs currency figure, and catalog product name in an LLM draft is backed by tool outputs, while allowing natural English, Hindi, and Hinglish phrasing.
- **Dual deployment artifacts:** [`workflow/kesar_order_support_copilot_n8n.json`](../workflow/kesar_order_support_copilot_n8n.json) mirrors the exact Python architecture using native n8n Webhook, Code, LangChain Structured/Auto-Fixing Output Parser, AI Agent, Tool, Switch, and Google Sheets nodes.
