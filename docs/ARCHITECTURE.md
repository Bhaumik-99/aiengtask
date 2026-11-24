# Architecture and Trust Boundaries

## 1. End-to-End System Architecture

```mermaid
flowchart TD
    subgraph TrustZone0["Untrusted External Boundary"]
        INBOUND["Customer Email JSON (Untrusted Subject, Body & Attachment Filenames)"]
    end

    subgraph TrustZone1["Deterministic Ingress & Privacy Firewall"]
        VAL["Schema & Timestamp Validator + Email-ID Idempotency"]
        PII_SCREEN{"Health / Allergy / Eczema / Pregnancy Screen"}
        INBOUND --> VAL --> PII_SCREEN
    end

    subgraph TrustZone2["Classification Layer (Zero Policy Authority)"]
        LLM_CLS["LLM Classifier (temp=0, Backoff Retries, 1-Turn JSON Repair)"]
        RULE_CLS["Deterministic Baseline Classifier"]
        GATE{"Schema Valid, Agreement & Confidence >= 0.75?"}
        PII_SCREEN -- "Safe Content" --> LLM_CLS
        PII_SCREEN -- "Safe Content" --> RULE_CLS
        LLM_CLS --> GATE
        RULE_CLS --> GATE
    end

    subgraph TrustZone3["Authoritative Policy, Ownership & Ticketing Engine"]
        MED_SAFE["Cosmetics Disclaimer + Skip External LLM + needs_human=true"]
        UNCERTAIN["Safe Escalation Draft + needs_human=true"]
        OWNERSHIP{"Lookup Order (KC-#### or Sender Email) & Verify from.email == customer_email"}
        DENY_PII["Block Order Disclosure + needs_human=true"]
        POLICY["Date Math (received_at vs 2026-11-20) + 7d Return / Kit / COD / 48h Damage Rules"]
        DEDUP_TKT["Deduplicated Ticket Create/Update (Open order_id + customer_email)"]
        FAQ_GROUND["Structured FAQ Retriever (All 8 Products & 9 Policy Sections)"]
        OTHER_LOG["other -> log_only (No Outbound Reply)"]

        PII_SCREEN -- "Sensitive Medical" --> MED_SAFE
        GATE -- "Low Confidence / Disagreement" --> UNCERTAIN
        GATE -- "other" --> OTHER_LOG
        GATE -- "product_query" --> FAQ_GROUND
        GATE -- "order_status / return_refund / complaint" --> OWNERSHIP
        OWNERSHIP -- "Mismatch / Unknown" --> DENY_PII
        OWNERSHIP -- "Verified Owner" --> POLICY
        POLICY -- "return_refund / complaint" --> DEDUP_TKT
    end

    subgraph TrustZone4["Sandboxed Read-Only Reply Agent"]
        AGENT["Bounded Tool-Calling Agent (Max 4 Rounds / 8 Calls)"]
        TOOL_ORD[("lookup_order (Read-Only, Strips Address & Email)")]
        TOOL_FAQ[("search_faq (Read-Only, data/faq.md Only)")]
        ENTITY_GUARD{"_safe_draft() Verifier (URLs, KC-####, Dates, INR, SKUs, No Refund Approval)"}
        DET_DRAFT["Deterministic Grounded Draft"]

        FAQ_GROUND -- "Covered by FAQ" --> AGENT
        POLICY -- "order_status" --> AGENT
        DEDUP_TKT --> AGENT
        AGENT <--> TOOL_ORD
        AGENT <--> TOOL_FAQ
        AGENT --> ENTITY_GUARD
        ENTITY_GUARD -- "Rejected / LLM Off" --> DET_DRAFT
    end

    subgraph TrustZone5["Durable Audit, Sheets Sync & Human Approval"]
        SQLITE[("SQLite BEGIN IMMEDIATE / CSV (Audit + Tickets + Tokens & USD Cost)")]
        GSHEETS[("Google Sheets Upsert (Audit Log & Tickets Tabs)")]
        HUMAN["Human Support Operator (Final Send & Refund/QC Approval)"]

        MED_SAFE --> SQLITE
        UNCERTAIN --> SQLITE
        OTHER_LOG --> SQLITE
        DENY_PII --> SQLITE
        FAQ_GROUND -- "Uncovered Topic" --> SQLITE
        ENTITY_GUARD -- "Verified Draft" --> SQLITE
        DET_DRAFT --> SQLITE
        SQLITE --> GSHEETS --> HUMAN
    end
```

## 2. Tool-Calling & Grounding Sequence

```mermaid
sequenceDiagram
    participant W as Webhook / Batch Runner
    participant E as Engine (engine.py)
    participant C as LLM Classifier
    participant A as Reply Agent (agent_tools.py)
    participant T as Read-Only Tools (orders.json / faq.md)
    participant S as SQLite & Google Sheets

    W->>E: Inbound Email Payload
    E->>E: Validate schema, check idempotency & screen medical PII
    opt LLM Classification Enabled & Non-Medical
        E->>C: Subject + Body + JSON Schema (temp=0)
        C-->>E: Structured JSON (or 1-turn Auto-Repair on schema error)
        E->>E: Cross-check intent & order_id against deterministic baseline
    end
    E->>E: Enforce sender == customer_email & compute days_since_delivery
    opt Return/Refund or Complaint
        E->>E: Create ticket or update open ticket for order_id
    end
    opt Agent Tools Enabled
        E->>A: Task context + untrusted email (no customer address PII)
        A->>T: Call lookup_order(order_id) and/or search_faq(query)
        T-->>A: Verified non-PII order fields / approved FAQ lines
        A-->>E: Proposed customer draft (en / hi / hinglish)
        E->>E: Run _safe_draft() entity check (URLs, KC-####, dates, INR, SKUs)
    end
    E->>S: Commit Audit Row + Ticket + Token/Cost Telemetry
    S-->>W: 200 OK (Draft stored; human reviews risky/ticketed cases)
```

## 3. Trust Boundaries

- **Customer content:** `subject` and `body` are untrusted input. Embedded prompt injections (`E12`) cannot register tools, alter `needs_human`, lower ticket priority, or approve refunds.
- **LLM provider:** Optional (`ENABLE_LLM_CLASSIFICATION`, `ENABLE_AGENT_TOOLS`). Sender name/email and attachment filenames are omitted from prompts, and health/allergy/pregnancy/skin-condition messages (`E20`) are never sent to external LLM endpoints.
- **Order ownership:** Server-side checks require exact case-insensitive match between `from.email` and `order.customer_email` before returning order status, items, totals, or tracking links (`E07`). Shipping street addresses are stripped even for verified owners.
- **Read-only tool allow-list:** Only `lookup_order` and `search_faq` exist. Unknown tool names return `tool_not_allowed`. No write/refund/cancel/send tools exist.
- **Entity-level draft guardrail (`_safe_draft`):** Verifies that every URL, `KC-####` ID, `YYYY-MM-DD` date, ₹/INR/Rs currency figure, and catalog product name in an LLM draft is backed by tool outputs, while allowing natural English, Hindi, and Hinglish phrasing.
- **Dual deployment artifacts:** [`workflow/kesar_order_support_copilot_n8n.json`](../workflow/kesar_order_support_copilot_n8n.json) mirrors the exact Python architecture using native n8n Webhook, Code, LangChain Structured/Auto-Fixing Output Parser, AI Agent, Tool, Switch, and Google Sheets nodes.
