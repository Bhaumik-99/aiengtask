# Engineering Self-Review — Kesar & Co. Copilot (V4 — Live Groq Verified)

This self-review summarizes the repository state after executing the full 25-email `v2` pipeline **live against Groq (`openai/gpt-oss-120b`)**.

## Current Readiness Summary

| Dimension | Status | Notes |
|---|---|---|
| **Core workflow & policy accuracy** | Strong (Live-Verified) | `v2` resolved all 25 fixture scenarios (`9` deduplicated tickets, `7` safe drafts, `4` non-ticket human escalations, `4` log-only rows) with `24/24` agreement between live `openai/gpt-oss-120b` and deterministic policy checks. |
| **Privacy & adversarial safety** | Strong (Live-Verified) | Server-side sender ownership checks (`E07`), live prompt-injection defeat (`E12`), medical escalation + external LLM exclusion (`E20`), and honest attachment-filename handling (`E21`). |
| **LLM classification, retries & repair** | Strong (Live-Verified) | `24/24` first-try valid JSON on `openai/gpt-oss-120b`, `63` free-tier `429` TPM rate-limit windows absorbed automatically via `x-ratelimit-reset-tokens` backoff (`0` failures), and `110,062` tokens (`$0.024025` list value) logged across `outputs/audit_log_v2.csv`. |
| **AI agent & tool grounding** | Strong (Live-Verified) | `17/17` eligible reply branches called live `lookup_order` and/or `search_faq` tools and passed `_safe_draft()` entity + scenario-clock contradiction checks across English, Hindi (`E09`), and Hinglish (`E05`). |
| **`v1` → `v2` iteration & test suite** | Strong | Empirical `v1` → `v2` progression (including catching live server-clock drift on `E03` and U+2011 hyphen normalization) documented in `TEST_REPORT.md` and backed by 74 automated tests. |
| **Submission artefacts** | Complete & Live-Synced | Includes importable `workflow/kesar_order_support_copilot_n8n.json` (zero secrets), live-synced Google Sheet (`Audit Log` + `Tickets`), walkthrough screen recording (`screenshots/walkthrough.mp4`), and PNG previews in `screenshots/`. |

## Remaining Honest Limitations

1. **Fixture regression ≠ production accuracy:** `25/25` on `data/test_emails.json` verifies scenario coverage, not blind holdout accuracy; `0.75` remains an uncalibrated pilot heuristic until calibrated on held-out production traffic.
2. **Date-level 48-hour screen & filename-only attachments:** `orders.json` provides `YYYY-MM-DD` delivery dates (not timestamps) and `test_emails.json` provides attachment filenames (not image bytes), so damaged/wrong/missing cases (`E04`, `E08`, `E14`, `E21`, `E23`) always require human verification.
3. **Single-process Sheets upsert:** `kesar_copilot/google_sheets.py` is live-tested on the 25-email batch (`1wqh8cZgmH7_l05VlMvPrb6VfaVGLDPCQRnuEc9zUC-o`), but full-sheet read/upsert should be replaced with a transactional outbox + batched writes at 1,000 emails/day.
4. **Upstream email sender authentication:** A production deployment must verify Gmail/Outlook OAuth metadata and SPF/DKIM/DMARC rather than trusting raw `from.email` strings on an HTTP webhook.
