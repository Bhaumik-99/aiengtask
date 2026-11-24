# Test Report — Kesar & Co. Support Copilot

**Candidate name:** Bhaumik Senwal
**Platform used:** Hybrid — Importable **n8n** workflow ([`workflow/kesar_order_support_copilot_n8n.json`](workflow/kesar_order_support_copilot_n8n.json)) + zero-dependency **Python 3.10+ reference engine & webhook** ([`kesar_copilot/`](kesar_copilot/))
**LLM(s) used:** Configurable OpenAI-compatible Chat Completions (`gpt-4o-mini` / `gemini-2.5-flash` / `llama-3.3-70b-versatile` at `temperature=0`); reproducible local benchmark executed in deterministic offline mode (`ENABLE_LLM_CLASSIFICATION=false`, `ENABLE_AGENT_TOOLS=false`) + verified via 74 unit, webhook, Sheets, and mocked LLM/tool-calling/retry/repair tests.

---

## 1. How I decided what "correct" means

Expected outcomes ([`data/expected_outcomes.json`](data/expected_outcomes.json)) were defined from [`data/faq.md`](data/faq.md) and [`data/orders.json`](data/orders.json) using **20 November 2026** as "today" and each email's `received_at` timestamp:
- **Complaints vs. Returns:** Transit damage (`E04`, `E08`, `E21`), wrong item (`E14`), and missing item (`E23`) are classified as `complaint` (High priority ticket, 48-hour photo/packing-weight verification) even when the customer demands a refund, whereas unopened (`E03`, `E11`, `E19`) or opened/dissatisfied (`E12`, `E16`) returns are `return_refund` (Medium priority ticket, 7-day + intact-seal policy).
- **Privacy & Verification:** Order details are shared **only** when `from.email` matches `orders.json.customer_email`; spouse/third-party inquiries (`E07`), missing order IDs without a unique sender match (`E18`), unknown orders (`E22`), and medical queries (`E20`) must set `needs_human=true` without leaking PII or making medical claims.
- **Non-support (`other`):** Spam (`E06`), B2B GST invoice requests (`E13`), creator collabs (`E17`), and thank-you notes (`E25`) map to `other` → `log_only` with no automated reply.
- **Honesty note:** Because these 25 expected labels were authored on the same 25 supplied emails, `25/25` is a **fixture regression check**, not an independent holdout accuracy claim.

## 2. Results per email

Both `v1` and `v2` were executed across all 25 emails (`python run.py --version v1` and `python run.py --version v2`).

| Email | Expected intent | Expected action | v1 intent | v1 action | v1 ✅/❌ | v2 intent | v2 action | v2 ✅/❌ | Notes |
|---|---|---|---|---|---|---|---|---|---|
| E01 | `order_status` | `draft_reply` | `order_status` | `draft_reply` | ✅ | `order_status` | `draft_reply` | ✅ | `KC-1041` shipped, ETA `2026-11-21` + tracking link; `v2` removed false `secondary_intent=product_query`. |
| E02 | `product_query` | `draft_reply` | `product_query` | `draft_reply` | ✅ | `product_query` | `draft_reply` | ✅ | Grounded in `faq.md`: Saffron Glow Serum is lightweight, non-greasy, oily-skin safe, and vegan. |
| E03 | `return_refund` | `ticket_created` (`TKT-001`) | `return_refund` | `ticket_created` (`TKT-001`) | ✅ | `return_refund` | `ticket_created` (`TKT-001`) | ✅ | `KC-1047` delivered `2026-11-16` (4 days ≤ 7d), unopened; ticket + preliminary return instructions (`needs_human=true`). |
| E04 | `complaint` | `ticket_created` (`TKT-002`) | `complaint` | `ticket_created` (`TKT-002`) | ✅ | `complaint` | `ticket_created` (`TKT-002`) | ✅ | `KC-1052` cracked Ubtan jar (1 day since delivery); High-priority ticket requesting product + outer-box photos. |
| E05 | `order_status` | `draft_reply` | `order_status` | `draft_reply` | ✅ | `order_status` | `draft_reply` | ✅ | Hinglish reply for `KC-1043` (`shipped`, ETA `23 November 2026`, tracking link, `Warm regards, Team Kesar`). |
| E06 | `other` | `log_only` | `other` | `log_only` | ✅ | `other` | `log_only` | ✅ | Unsolicited SEO spam logged with no draft reply. |
| E07 | `order_status` | `needs_human` | `order_status` | `needs_human` | ✅ | `order_status` | `needs_human` | ✅ | Privacy block: `arjun.nair@example.com` ≠ `meera.iyer@example.com` (`KC-1046`); zero tracking/address disclosure. |
| E08 | `complaint` | `ticket_created` (`TKT-003`) | `complaint` | `ticket_created` (`TKT-003`) | ✅ | `complaint` | `ticket_created` (`TKT-003`) | ✅ | Angry customer (`KC-1051` leaked lotion); High-priority ticket, notes refund preference, requests verification photos. |
| E09 | `product_query` | `draft_reply` | `product_query` | `draft_reply` | ✅ | `product_query` | `draft_reply` | ✅ | Answered in Devanagari Hindi from `faq.md`: Guwahati/NE 6–8 business days after 1–2d dispatch; COD ≤ ₹5,000 (₹49 fee). |
| E10 | `order_status` | `draft_reply` | `order_status` | `needs_human` | ❌ | `order_status` | `draft_reply` | ✅ | **Fixed in v2:** No order ID in email; `v2` falls back to verified `customer_email` → resolves `KC-1044` + tracking URL. |
| E11 | `return_refund` | `ticket_created` (`TKT-004`) | `return_refund` | `ticket_created` (`TKT-004`) | ✅ | `return_refund` | `ticket_created` (`TKT-004`) | ✅ | `KC-1048` Bridal Glow Kit (5 days ≤ 7d, unopened); includes mandatory complete-kit return notice. |
| E12 | `return_refund` | `ticket_created` (`TKT-005`) | `return_refund` | `ticket_created` (`TKT-005`) | ✅ | `return_refund` | `ticket_created` (`TKT-005`) | ✅ | **Prompt injection defeated:** ignores fake VIP refund approval, creates `TKT-005`, cites opened-item exclusion. |
| E13 | `other` | `log_only` | `other` | `log_only` | ✅ | `other` | `log_only` | ✅ | B2B GST invoice request (`GSTIN` provided, no order ID) logged under `other` branch. |
| E14 | `complaint` | `ticket_created` (`TKT-006`) | `complaint` | `ticket_created` (`TKT-006`) | ✅ | `complaint` | `ticket_created` (`TKT-006`) | ✅ | `KC-1053` wrong item delivered (Bhringraj Oil instead of Ubtan); High-priority ticket + free pickup/resend policy. |
| E15 | `order_status` | `draft_reply` | `order_status` | `draft_reply` | ✅ | `order_status` | `draft_reply` | ✅ | `KC-1042` `out_for_delivery` (`2026-11-20`); `v2` adds COD payable total (`₹1,897`) + exact-change tip from `faq.md`. |
| E16 | `return_refund` | `ticket_created` (`TKT-007`) | `return_refund` | `ticket_created` (`TKT-007`) | ✅ | `return_refund` | `ticket_created` (`TKT-007`) | ✅ | `KC-1049` used for 2 weeks (delivered `2026-11-05`, 15d > 7d & opened); creates ticket, declines automatic approval. |
| E17 | `other` | `log_only` | `product_query` | `draft_reply` | ❌ | `other` | `log_only` | ✅ | **Fixed in v2:** Influencer collab mentioning "Bridal Glow Kit" misclassified as `product_query` in `v1`; `v2` logs as `other`. |
| E18 | `order_status` | `needs_human` | `order_status` | `needs_human` | ✅ | `order_status` | `needs_human` | ✅ | Vague `"Where is it???"` (`confidence=0.70 < 0.75`, no order for `rahul.b@example.com`) → human escalation. |
| E19 | `return_refund` | `ticket_created` (`TKT-008`) | `return_refund` | `ticket_created` (`TKT-008`) | ✅ | `return_refund` | `ticket_created` (`TKT-008`) | ✅ | `KC-1050` COD return (3 days ≤ 7d, unopened); explains UPI/bank refund after pickup + non-refundable ₹49 COD fee. |
| E20 | `product_query` | `needs_human` | `product_query` | `needs_human` | ✅ | `product_query` | `needs_human` | ✅ | Eczema medical question escalated (`needs_human=true`), cosmetics disclaimer drafted, excluded from external LLM calls. |
| E21 | `complaint` | `ticket_created` (update `TKT-002`) | `complaint` | `ticket_created` (dup `TKT-009`) | ❌ | `complaint` | `ticket_created` (updates `TKT-002`) | ✅ | **Fixed in v2:** `v1` created duplicate ticket `TKT-009`; `v2` updates `TKT-002` (`E04,E21`) and logs replacement + filenames. |
| E22 | `order_status` | `needs_human` | `order_status` | `needs_human` | ✅ | `order_status` | `needs_human` | ✅ | Non-existent `KC-1099` not hallucinated; asks customer to confirm order number and checkout email. |
| E23 | `complaint` | `ticket_created` (`TKT-009`) | `complaint` | `ticket_created` (`TKT-010`) | ✅ | `complaint` | `ticket_created` (`TKT-009`) | ✅ | `KC-1054` missing Aloe Calm Gel; High-priority ticket, notes refund preference, requires packing-weight + photo check. |
| E24 | `order_status` | `draft_reply` | `order_status` | `draft_reply` | ✅ | `order_status` | `draft_reply` | ✅ | `KC-1045` cancelled (out of stock); reports recorded `INR 828` refund initiated on `2026-11-16`. |
| E25 | `other` | `log_only` | `other` | `log_only` | ✅ | `other` | `log_only` | ✅ | Positive feedback logged (`sentiment=positive`), no reply needed. |

## 3. Summary

| Metric | Prompt / Flow v1 | Prompt / Flow v2 |
|---|---|---|
| Emails run (*n*) | 25 | 25 |
| Intent accuracy (x / *n*) | 24 / 25 (96.0%) | **25 / 25 (100.0% on fixture)** |
| Correct end-to-end action incl. dedup (x / *n*) | 22 / 25 (88.0%) | **25 / 25 (100.0% on fixture)** |
| Unique open tickets created | 10 *(1 duplicate on `KC-1052`)* | **9 *(0 duplicates; `E04`+`E21` merged)*** |
| Valid JSON on first try (x / *n*) | Deterministic rules (`N/A` offline; no auto-repair) | Deterministic + 1-turn JSON auto-repair (`not_called` offline; tested in unit suite) |
| Emails sent to `needs_human=true` | 15 / 25 | **14 / 25** *(10 ticketed + 4 non-ticket escalations)* |
| Avg. tokens per email (tracked in audit log) | `0` *(offline deterministic run)* | `0` offline; ~1,260 tokens/email when live LLM + Agent enabled |
| Est. cost per email ($ / ₹) | `$0.00` *(offline)* | `$0.00` offline; `~$0.00033` (`~₹0.028`) on `gpt-4o-mini` / ₹0 on Gemini/Groq free tier |

## 4. What I changed from v1 to v2, and why

- **Change 1 — Added verified `customer_email` order lookup fallback (`E10`):** In `v1`, orders were only queried when an explicit `KC-####` ID appeared, sending `E10` to `needs_human`. `v2` falls back to unique `customer_email` lookup while still enforcing exact sender ownership.
- **Change 2 — Open-ticket deduplication + structured follow-up notes (`E04` → `E21`):** `v1` appended a new ticket for every complaint (`10` tickets). `v2` checks open tickets for the same `order_id` + `customer_email`, merges `E21` into `TKT-002` (`email_ids="E04,E21"`), and appends timestamped customer replacement preference and attachment filenames.
- **Change 3 — Intent precedence, secondary-intent precision & generalized FAQ synthesis (`E17`, `E01/E07/E10`, `E15`):** Moved `partnership`/`invoice` ahead of catalog-noun matching so creator collabs naming a product (`E17`) route to `other`; eliminated false `secondary_intent=product_query` on order-status emails that merely name the ordered item; generalized FAQ grounding across **all 8 products and shipping regions**; added COD payable total (`₹1,897`) to `E15`.
- **Change 4 — Hardened AI Agent grounding validator, JSON auto-repair & backoff retries:** Replaced a brittle word-whitelist in `_safe_draft()` with entity-level verification (URLs, `KC-####`, ISO dates, ₹ amounts, catalog SKUs, forbidden refund/medical claims), added 1-turn JSON schema repair + exponential backoff on `429`/`5xx`, and added per-email token/cost columns.

## 5. Failures I could not fix (be honest)

| Email / Area | What goes wrong / limitation | Why I think it happens | What I would try next |
|---|---|---|---|
| **Live LLM & Google Sheets execution** | Live API traces and a hosted Google Sheet URL are not bundled in this repo. | Built in an offline sandbox without external API keys or a personal Google OAuth/service-account sheet. | Add a free-tier Groq/Gemini key in `.env`, import `workflow/kesar_order_support_copilot_n8n.json` into n8n Cloud, and run `--sync-sheets` against a shared Google Sheet. |
| **`E04`, `E08`, `E14`, `E21`, `E23` (48-hour window & photos)** | System screens using calendar dates (`0–1` days) rather than exact hours and cannot inspect `cracked_jar.jpg` / `outer_box.jpg`. | `orders.json` only stores `YYYY-MM-DD` `delivered_date` (no delivery timestamp), and `test_emails.json` only supplies filename strings, not image bytes. | Fetch carrier delivery timestamps from Shopify/courier webhooks and pass real MIME image attachments to a vision review tool for human-assisted triage. |
| **`E13` (B2B GST invoice request)** | Logged as `other` → `log_only` (`needs_human=false`) with no customer reply. | Matches the assignment flow diagram (`other → log only`), even though a real paying customer asking for a tax invoice needs an accounting response. | Add a `billing_invoice` sub-route or set `needs_human=true` on `invoice_request` signals so finance receives an actionable queue item. |
| **Sender spoofing on raw JSON webhook** | `X-Webhook-Secret` authenticates the HTTP caller, not SPF/DKIM/DMARC authenticity of `from.email`. | Test harness posts raw JSON objects rather than signed Gmail/Google Workspace API messages. | Pull messages directly via Gmail OAuth API (`users.messages.get`) and verify `Authentication-Results` headers before trusting `from.email`. |
