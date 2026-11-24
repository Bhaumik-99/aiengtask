# Setup and runbook

## 1. Requirements

- Python 3.10+
- Standard library for batch mode, webhook and local tests
- Optional Google Sheets dependency: `pip install -r requirements-sheets.txt`

## 2. Verify local package

```bash
python -m unittest discover -s tests -v
python scripts/evaluate_fixture.py
python run.py --version v1
python run.py --version v2
```

The offline test run makes no model or Google network calls.

## 3. Optional LLM classification and reply agent

Set the provider credentials as environment variables in your shell or deployment system. `.env.example` is a reference only; no dotenv library is installed.

```bash
export LLM_API_KEY='your-key'
export LLM_BASE_URL='https://api.openai.com/v1'
export LLM_MODEL='gpt-4o-mini'
export ENABLE_LLM_CLASSIFICATION=true
export ENABLE_AGENT_TOOLS=true
python run.py --version v2
```

`ENABLE_LLM_CLASSIFICATION` makes the model return structured classification JSON. A rules/LLM intent or order-ID disagreement triggers human review. The model confidence is not trusted.

`ENABLE_AGENT_TOOLS` enables a bounded function-calling loop. The agent chooses between `lookup_order` and `search_faq`; only these read-only functions are executable. A customer draft is accepted only after the relevant tool returned evidence and the draft passed basic grounding checks. The agent runs on normal verified order-status and FAQ questions only. Health/medical/allergy/reaction queries are never sent to the model provider. Provider calls have a 20-second timeout; failures use deterministic fallback drafts or human review.

## 4. Google Sheets (optional)

1. Create a Google Sheet named **Kesar Support**.
2. Create tabs exactly named **Audit Log** and **Tickets**.
3. Import `templates/audit_log_template.csv` into Audit Log and `templates/tickets_template.csv` into Tickets so the first row contains the expected headers.
4. In Google Cloud Console, enable Google Sheets API and Google Drive API. Create a service account and download its JSON key.
5. Share the spreadsheet with the service account's `client_email` as an Editor.
6. Store the JSON key outside version control; the provided `.gitignore` excludes `service-account*.json`.
7. Install and configure:

```bash
pip install -r requirements-sheets.txt
export GOOGLE_SHEET_ID='your-spreadsheet-id'
export GOOGLE_SERVICE_ACCOUNT_FILE='./service-account.json'
python run.py --version v2 --sync-sheets
```

For webhook mode, once both variables are present, each successful local SQLite commit is followed by a Sheets sync attempt. If that sync fails, local data stays persisted and the response reports `sheets_sync.status = failed`; no background retry worker exists. The adapter uses idempotent upsert by `email_id` / `ticket_id`, but is only mock-tested and intended for the assignment-sized fixture.

## 5. Webhook

Generate a secret with at least 32 characters:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
export WEBHOOK_SECRET='paste-generated-secret-here'
python webhook.py
```

- `GET /health`
- `POST /webhook/support-email`
- Header: `X-Webhook-Secret`
- Content-Type: `application/json`
- DB path: `data/copilot.sqlite3` (`COPILOT_DB` override)
- Bind: `127.0.0.1:8080` (`HOST`/`PORT` override)

This is a local prototype. A shared secret authenticates a caller to this endpoint but does not prove the sender's `from.email` is genuine. A real Gmail/Outlook integration must verify provider signatures/tokens and use trusted provider identity; deploy behind TLS, rate limits and monitoring.

## 6. Test email sender script

```bash
python scripts/send_test_emails.py --dry-run
python scripts/send_test_emails.py --url http://127.0.0.1:8080/webhook/support-email --dry-run
```

Actual POSTing requires an explicit `--send` flag and a configured webhook secret. External hosts are blocked unless `--allow-external` is explicitly supplied.

## 7. Important limitations

- No live LLM, Google Sheets account, Shopify, Gmail/Outlook, outbound email provider or ticketing system is included.
- Fixture expected labels are developer-authored regression expectations, not independent benchmark ground truth.
- The tool-loop test uses mocked provider responses, not a live model.
- The webhook shared-secret check is not provider event signature verification.
- The Sheets adapter is mock-tested; live service-account permission and quota behaviour remain unverified.
- Attachment names are not the image bytes; no visual inspection occurs.
- Human approval is required for refund/replacement decisions and sensitive/ambiguous cases.
