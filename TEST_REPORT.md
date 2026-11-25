# Test Report — Kesar & Co. Support Copilot

**Candidate name:** Bhaumik Senwal
**Platform used:** Hybrid — Importable **n8n** workflow ([`workflow/kesar_order_support_copilot_n8n.json`](workflow/kesar_order_support_copilot_n8n.json)) + **Python 3.10+ live execution engine & webhook** ([`kesar_copilot/`](kesar_copilot/))
**LLM(s) used:** **Groq — `openai/gpt-oss-120b`** (`temperature=0`, JSON mode + function-calling tools `lookup_order` and `search_faq`, executed live across all 25 test emails on Groq's ₹0 free tier)
**Google Sheet (Live Synced):** [https://docs.google.com/spreadsheets/d/1wqh8cZgmH7_l05VlMvPrb6VfaVGLDPCQRnuEc9zUC-o/edit?usp=sharing](https://docs.google.com/spreadsheets/d/1wqh8cZgmH7_l05VlMvPrb6VfaVGLDPCQRnuEc9zUC-o/edit?usp=sharing) (`Audit Log`: 25 rows, `Tickets`: 9 deduplicated rows)

---

## 1. How I decided what "correct" means

Expected outcomes ([`data/expected_outcomes.json`](data/expected_outcomes.json)) were defined from [`data/faq.md`](data/faq.md) and [`data/orders.json`](data/orders.json) using **20 November 2026** as "today" and each email's `received_at` timestamp:
- **Complaints vs. Returns:** Transit damage (`E04`, `E08`, `E21`), wrong item (`E14`), and missing item (`E23`) are classified as `complaint` (High priority ticket, 48-hour photo/packing-weight verification) even when the customer demands a refund, whereas unopened (`E03`, `E11`, `E19`) or opened/dissatisfied (`E12`, `E16`) returns are `return_refund` (Medium priority ticket, 7-day + intact-seal policy).
- **Privacy & Verification:** Order details are shared **only** when `from.email` matches `orders.json.customer_email`; spouse/third-party inquiries (`E07`), missing order IDs without a unique sender match (`E18`), unknown orders (`E22`), and medical queries (`E20`) must set `needs_human=true` without leaking PII or making medical claims.
- **Non-support (`other`):** Spam (`E06`), B2B GST invoice requests (`E13`), creator collabs (`E17`), and thank-you notes (`E25`) map to `other` → `log_only` with no automated reply.
- **Honesty note:** Because these 25 expected labels were authored on the same 25 supplied emails, `25/25` is a **fixture regression check**, not an independent blind holdout benchmark.

## 2. Results per email

`v2` was executed **live against Groq (`openai/gpt-oss-120b`)** across all 25 emails (`ENABLE_LLM_CLASSIFICATION=true`, `ENABLE_AGENT_TOOLS=true`), compared against the `v1` baseline run across all 25 emails. Exact outputs and token counts are saved in [`outputs/audit_log_v2.csv`](outputs/audit_log_v2.csv) and [`outputs/results_v2.json`](outputs/results_v2.json).

| Email | Expected intent | Expected action | v1 intent | v1 action | v1 ✅/❌ | v2 intent | v2 action | v2 ✅/❌ | Live v2 Notes (`openai/gpt-oss-120b`) |
|---|---|---|---|---|---|---|---|---|---|
| E01 | `order_status` | `draft_reply` | `order_status` | `draft_reply` | ✅ | `order_status` | `draft_reply` | ✅ | Live agent called `["lookup_order"]` (`4,405` tok): SwiftShip shipped `17 Nov`, ETA `21 Nov`, verified tracking URL. |
| E02 | `product_query` | `draft_reply` | `product_query` | `draft_reply` | ✅ | `product_query` | `draft_reply` | ✅ | Live agent called `["search_faq"]` (`4,703` tok): Saffron Glow Serum is lightweight, non-greasy, oily-skin safe & vegan. |
| E03 | `return_refund` | `ticket_created` (`TKT-001`) | `return_refund` | `ticket_created` (`TKT-001`) | ❌ *(v1 live smoke clock bug)* | `return_refund` | `ticket_created` (`TKT-001`) | ✅ | **Fixed in v2:** Live agent called `["lookup_order", "search_faq"]` (`6,844` tok); anchored to `2026-11-20` (`4d <= 7d`), created `TKT-001`. |
| E04 | `complaint` | `ticket_created` (`TKT-002`) | `complaint` | `ticket_created` (`TKT-002`) | ✅ | `complaint` | `ticket_created` (`TKT-002`) | ✅ | Live agent called `["lookup_order", "search_faq"]` (`7,012` tok); High-priority `TKT-002`, requests jar + outer-box photos. |
| E05 | `order_status` | `draft_reply` | `order_status` | `draft_reply` | ✅ | `order_status` | `draft_reply` | ✅ | Live agent drafted natural **Hinglish** via `["lookup_order"]` (`4,671` tok) with ETA `23 Nov` + tracking link. |
| E06 | `other` | `log_only` | `other` | `log_only` | ✅ | `other` | `log_only` | ✅ | Live LLM classified SEO spam as `other` (`1,539` tok); logged with no customer reply. |
| E07 | `order_status` | `needs_human` | `order_status` | `needs_human` | ✅ | `order_status` | `needs_human` | ✅ | Privacy block (`1,566` tok): `arjun.nair@example.com` ≠ `meera.iyer@example.com` (`KC-1046`); zero order/address leak. |
| E08 | `complaint` | `ticket_created` (`TKT-003`) | `complaint` | `ticket_created` (`TKT-003`) | ✅ | `complaint` | `ticket_created` (`TKT-003`) | ✅ | Live LLM extracted `sentiment=angry`, `secondary_intent=return_refund`; agent called both tools (`7,159` tok), opened `TKT-003` (High). |
| E09 | `product_query` | `draft_reply` | `product_query` | `draft_reply` | ✅ | `product_query` | `draft_reply` | ✅ | Live agent drafted **Devanagari Hindi** via `["search_faq"]` (`4,800` tok): 1–2d dispatch, 6–8d NE/Assam, COD ≤ ₹5,000 (₹49 fee). |
| E10 | `order_status` | `draft_reply` | `order_status` | `needs_human` | ❌ | `order_status` | `draft_reply` | ✅ | **Fixed in v2:** Omitted `order_id`; `v2` resolved `KC-1044` by verified `customer_email` + live `["lookup_order"]` (`4,419` tok). |
| E11 | `return_refund` | `ticket_created` (`TKT-004`) | `return_refund` | `ticket_created` (`TKT-004`) | ✅ | `return_refund` | `ticket_created` (`TKT-004`) | ✅ | Live agent called `["lookup_order", "search_faq"]` (`6,767` tok); `TKT-004` opened for unopened Bridal Glow Kit (`5d <= 7d`). |
| E12 | `return_refund` | `ticket_created` (`TKT-005`) | `return_refund` | `ticket_created` (`TKT-005`) | ✅ | `return_refund` | `ticket_created` (`TKT-005`) | ✅ | **Live prompt injection defeated** (`7,215` tok): ignored fake `SYSTEM NOTE` VIP refund directive, opened `TKT-005`, cited opened-item policy. |
| E13 | `other` | `log_only` | `other` | `log_only` | ✅ | `other` | `log_only` | ✅ | Live LLM classified B2B GST invoice request as `other` (`1,576` tok) → `log_only`. |
| E14 | `complaint` | `ticket_created` (`TKT-006`) | `complaint` | `ticket_created` (`TKT-006`) | ✅ | `complaint` | `ticket_created` (`TKT-006`) | ✅ | Live agent called `["lookup_order", "search_faq"]` (`6,799` tok); `TKT-006` (High), requests photos of wrong Bhringraj bottles. |
| E15 | `order_status` | `draft_reply` | `order_status` | `draft_reply` | ✅ | `order_status` | `draft_reply` | ✅ | Live agent called `["lookup_order"]` (`4,485` tok): `KC-1042` out for delivery today (`20 Nov 2026`), reminds customer of COD cash + ₹49 fee. |
| E16 | `return_refund` | `ticket_created` (`TKT-007`) | `return_refund` | `ticket_created` (`TKT-007`) | ✅ | `return_refund` | `ticket_created` (`TKT-007`) | ✅ | Live agent called `["lookup_order", "search_faq"]` (`6,857` tok); notes opened item & 15 days elapsed, routes to `TKT-007`. |
| E17 | `other` | `log_only` | `product_query` | `draft_reply` | ❌ | `other` | `log_only` | ✅ | **Fixed in v2:** Influencer collab naming "Bridal Glow Kit" misclassified as `product_query` in `v1`; live `v2` classified `other` (`1,542` tok). |
| E18 | `order_status` | `needs_human` | `order_status` | `needs_human` | ✅ | `order_status` | `needs_human` | ✅ | Vague `"Where is it???"` (`confidence=0.70 < 0.75`, `1,552` tok) → `skipped_low_confidence`, routed to `needs_human`. |
| E19 | `return_refund` | `ticket_created` (`TKT-008`) | `return_refund` | `ticket_created` (`TKT-008`) | ✅ | `return_refund` | `ticket_created` (`TKT-008`) | ✅ | Live agent called `["lookup_order", "search_faq"]` (`6,782` tok); explains UPI/bank COD refund after QC minus non-refundable ₹49 fee. |
| E20 | `product_query` | `needs_human` | `product_query` | `needs_human` | ✅ | `product_query` | `needs_human` | ✅ | Eczema medical query intercepted by privacy firewall (`0` external LLM tokens), safe cosmetics disclaimer + `needs_human=true`. |
| E21 | `complaint` | `ticket_created` (update `TKT-002`) | `complaint` | `ticket_created` (dup `TKT-009`) | ❌ | `complaint` | `ticket_created` (updates `TKT-002`) | ✅ | **Fixed in v2:** Deduped onto `TKT-002` (`E04,E21`), appended replacement + filenames note; live agent (`6,954` tok) acknowledged unverified filenames. |
| E22 | `order_status` | `needs_human` | `order_status` | `needs_human` | ✅ | `order_status` | `needs_human` | ✅ | Unknown `KC-1099` (`1,514` tok): zero hallucination, asks customer to confirm order ID and checkout email. |
| E23 | `complaint` | `ticket_created` (`TKT-009`) | `complaint` | `ticket_created` (`TKT-010`) | ✅ | `complaint` | `ticket_created` (`TKT-009`) | ✅ | Live agent called `["lookup_order"]` (`4,828` tok); `TKT-009` (High), notes refund preference, requests box/item/weight-label photos. |
| E24 | `order_status` | `draft_reply` | `order_status` | `draft_reply` | ✅ | `order_status` | `draft_reply` | ✅ | Live agent called `["lookup_order"]` (`4,540` tok): explains `KC-1045` out-of-stock cancellation on `16 Nov 2026` + `₹828` refund initiated. |
| E25 | `other` | `log_only` | `other` | `log_only` | ✅ | `other` | `log_only` | ✅ | Live LLM classified thank-you note as `other`, `sentiment=positive` (`1,533` tok) → `log_only`. |

## 3. Summary

| Metric | Flow v1 (Baseline) | Flow v2 (Live Groq `openai/gpt-oss-120b`) |
|---|---|---|
| Emails run (*n*) | 25 | **25** *(24 live LLM + 1 medical privacy intercept)* |
| Intent accuracy (x / *n*) | 24 / 25 (96.0%) | **25 / 25 (100.0% on fixture; 24/24 live LLM agreement)** |
| Correct end-to-end action incl. dedup & policy (x / *n*) | 21 / 25 (84.0%) | **25 / 25 (100.0% on fixture)** |
| Unique open tickets created | 10 *(1 duplicate on `KC-1052`)* | **9 *(0 duplicates; `E04`+`E21` merged into `TKT-002`)*** |
| Valid JSON on first try (x / *n*) | N/A *(baseline rules)* | **24 / 24 (100.0% first-try schema validity)** |
| Live AI Agent tool-grounded drafts accepted | 0 *(disabled in v1)* | **17 / 17 eligible reply branches (100.0% passed `_safe_draft`)** |
| Automatic `429` TPM backoff retries handled | 0 | **63 retries absorbed cleanly (`0` failed emails)** |
| Emails sent to `needs_human=true` | 15 / 25 | **14 / 25** *(10 ticketed + 4 non-ticket escalations)* |
| Avg. tokens per email (tracked in audit log) | `0` | **4,402.5 tokens/email** *(`110,062` total: `96,878` in + `13,184` out)* |
| Est. cost per email ($ / ₹) | `$0.00` | **`$0.000961` (~₹0.08/email; `$0.024025` total list value; ₹0 actual on Groq free tier)** |

## 4. What I changed from v1 to v2, and why

- **Change 1 — Fixed live LLM server-clock drift on return windows (`E03`) & Unicode hyphen bypass:** During our initial live Groq smoke run on `E03` (delivered `2026-11-16`, email received `2026-11-20`), `openai/gpt-oss-120b` used Groq's real-world `2026-11-25` server clock (`9 days > 7-day window`) and falsely told Kavya the return window had expired, while also emitting U+2011 non-breaking hyphens (`KC‑1041`). In `v2`, we explicitly injected `scenario_today: "2026-11-20"` + `received_at` into the agent payload, added a deterministic policy-contradiction blocker in `_safe_draft()`, and normalized typographic hyphens/spaces.
- **Change 2 — Added verified `customer_email` order lookup fallback (`E10`):** `v1` only queried orders when an explicit `KC-####` ID appeared, escalating `E10`. `v2` resolves unique `customer_email` matches while keeping `sender == customer_email` ownership mandatory.
- **Change 3 — Open-ticket deduplication + structured follow-up notes (`E04` → `E21`):** `v1` created a duplicate ticket (`10` total). `v2` merges `E21` into open `TKT-002` (`email_ids="E04,E21"`) and appends customer replacement preference + unverified attachment filenames.
- **Change 4 — Intent precedence (`E17`), Groq `tool_calls` message sanitization & `x-ratelimit-reset-tokens` backoff:** Fixed creator-collab precedence over product nouns (`E17`), stripped Groq's extra `reasoning` key on multi-turn tool messages, and parsed `x-ratelimit-reset-tokens` headers to weather Groq's `8,000 TPM` free-tier cap with zero dropped requests.

## 5. Failures I could not fix (be honest)

| Email / Area | What goes wrong / limitation | Why I think it happens | What I would try next |
|---|---|---|---|
| **`E11` nuance (kit complete-set caveat in live LLM prose)** | On `E11`, the live agent accurately explained the review, 2–3d reverse pickup, and 5–7d refund (`TKT-004`), but omitted the optional one-line reminder that all 4 items in the Bridal Glow Kit must be returned together (even though the customer wrote *"The kit is completely unopened, all seals intact"*). | `search_faq` returned the Returns + Refunds lines, and because the customer already stated the entire kit is unopened, the model summarized concisely without repeating the partial-kit warning. | Enforce kit-specific post-processing or pass a mandatory `must_mention` clause in `task_context` whenever `KC-KIT-BRD` is in `order.items`. |
| **`E04`, `E08`, `E14`, `E21`, `E23` (48-hour window & photos)** | System screens using calendar dates (`0–1` days) rather than exact hours and cannot visually inspect `cracked_jar.jpg` / `outer_box.jpg`. | `orders.json` only stores `YYYY-MM-DD` `delivered_date` (no delivery timestamp), and `test_emails.json` only supplies filename strings, not image bytes. | Fetch carrier delivery timestamps from Shopify/courier webhooks and pass real MIME image attachments to a vision review tool for human-assisted triage. |
| **`E13` (B2B GST invoice request)** | Logged as `other` → `log_only` (`needs_human=false`) with no customer reply. | Follows the brief's literal `other → log only` diagram, though a real customer asking for a tax invoice needs an accounting response. | Route `invoice_request` signals to a finance queue (`needs_human=true`) with a polite acknowledgement draft. |
| **Reasoning-token overhead on `openai/gpt-oss-120b`** | Average token usage was `4,402` tokens/email (`$0.00096` list value) vs ~`1,300` on non-reasoning models, triggering `429` waits under Groq's `8,000 TPM` free tier. | `gpt-oss-120b` generates internal chain-of-thought tokens on each classification and tool-calling turn. | Use a smaller fast model (`openai/gpt-oss-20b`) for classification (`temp=0`) and reserve `gpt-oss-120b` for complex reply drafting, cutting TPM load by ~55%. |
