# Engineering Self-Review — Kesar & Co. Copilot (V4)

This self-review separates **code, safety, and workflow completeness** in the repository from **user-account steps** (live API credentials, shared Google Sheet link, and Loom recording) that only the candidate can attach before emailing the submission.

## Current Readiness Summary

| Dimension | Status | Notes |
|---|---|---|
| **Core workflow & policy accuracy** | Strong | `v2` resolves all 25 fixture scenarios (9 deduplicated tickets, 7 safe drafts, 4 non-ticket human escalations, 4 log-only rows) and generalizes across all 8 FAQ products and regional shipping tiers. |
| **Privacy & adversarial safety** | Strong | Server-side sender ownership checks (`E07`), prompt-injection immunity (`E12`), medical escalation + LLM exclusion (`E20`), and honest attachment-filename handling (`E21`). |
| **LLM classification, retries & repair** | Strong (mock-verified) | Loads versioned prompts from `prompts/`, enforces `schemas/llm_output.schema.json`, implements exponential backoff + jitter on `429`/`5xx`, runs 1-turn JSON auto-repair, and logs token/cost columns. |
| **AI agent & tool grounding** | Strong (mock-verified) | Bounded tool-calling loop (`lookup_order` + `search_faq`) across reply branches with entity-level validation (URLs, `KC-####`, dates, ₹ amounts, catalog SKUs) supporting English, Hindi, and Hinglish. |
| **`v1` → `v2` iteration & test suite** | Strong | Genuine, measurable `v1` vs `v2` progression documented in `TEST_REPORT.md` (25-row template table) backed by 74 unit, webhook, Sheets, and workflow-blueprint tests. |
| **Submission artefacts** | Ready for final user links | Includes importable `workflow/kesar_order_support_copilot_n8n.json` (zero secrets) and rendered previews in `screenshots/`. Candidate still needs to paste their own live Google Sheet view link and 5-minute Loom URL. |

## Remaining Honest Limitations

1. **No bundled live provider credentials:** Live LLM traces and live Google Sheets writes require user-supplied credentials (`.env` / `service-account.json` or n8n OAuth). Do not claim live benchmark accuracy without running against a fixed live model on an independent holdout dataset.
2. **Fixture regression ≠ production accuracy:** `25/25` on `data/test_emails.json` tests regression against known scenarios, not blind generalization; `0.75` remains an uncalibrated pilot heuristic until calibrated on held-out production traffic.
3. **Date-level 48-hour screen & filename-only attachments:** `orders.json` provides `YYYY-MM-DD` delivery dates (not timestamps) and `test_emails.json` provides attachment filenames (not image bytes), so damaged/wrong/missing cases (`E04`, `E08`, `E14`, `E21`, `E23`) always require human verification.
4. **Upstream email sender authentication:** A production deployment must verify Gmail/Outlook OAuth metadata and SPF/DKIM/DMARC rather than trusting raw `from.email` strings on an HTTP webhook.
