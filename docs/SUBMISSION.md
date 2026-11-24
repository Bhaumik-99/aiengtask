# Submission checklist and known gaps

## Included in this package

- Python source and runnable batch processor
- Optional LLM classification and function-calling reply agent
- Two read-only tools (`lookup_order`, `search_faq`)
- Deterministic policy engine and human-review routing
- SQLite webhook persistence
- Optional Google Sheets adapter for the assignment's Audit Log and Tickets tabs
- Original 25-email fixture, order fixture, FAQ and schema
- V1/V2 JSON/CSV outputs, test report, architecture and setup notes
- Local tests and fixture regression evaluator

## Still needs your account / manual work

- Create the Google Sheet and configure service-account credentials; live sheet is not included or tested.
- Export an n8n/Make/Zapier workflow if the reviewer requires an automation-platform artefact. This package currently implements a Python service rather than those platform exports.
- Add workflow and Google Sheet screenshots after configuration.
- Record the requested five-minute walkthrough, including three test emails, one ticket creation, and one known limitation.
- Confirm the exact application deadline in the invitation email.

Do not claim the missing artefacts or live integrations are complete. The assignment explicitly values honest reporting of what does not work yet.
