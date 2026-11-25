"""Optional Google Sheets sink for the assignment's Audit Log and Tickets tabs.

Local JSON/SQLite state remains the source of truth. Sheets sync is an optional
integration and deliberately fails closed when credentials or expected tabs are
missing. Configure a service account and share the spreadsheet with its email.
"""
from __future__ import annotations

import os
from typing import Any

AUDIT_COLUMNS = [
    "email_id", "received_at", "from_email", "intent", "secondary_intent",
    "order_id", "language", "sentiment", "confidence", "order_found",
    "action_taken", "needs_human", "ticket_id", "reply_draft", "prompt_version",
    "processed_at", "error", "notes",
]
TICKET_COLUMNS = [
    "ticket_id", "created_at", "updated_at", "order_id", "customer_email",
    "email_ids", "intent", "priority", "status", "summary", "notes",
]


class SheetsConfigurationError(RuntimeError):
    pass


def _row_from_record(record: dict[str, Any], columns: list[str]) -> list[Any]:
    return [record.get(column, "") for column in columns]


def _upsert(worksheet: Any, records: list[dict[str, Any]], columns: list[str], key: str) -> dict[str, int]:
    """Idempotent single-process upsert by the tab's stable key column."""
    existing = worksheet.get_all_values()
    if not existing:
        raise SheetsConfigurationError(
            f"Tab '{worksheet.title}' is empty; import the provided CSV template headers first."
        )
    headers = existing[0]
    missing = [column for column in columns if column not in headers]
    if missing:
        raise SheetsConfigurationError(f"Tab '{worksheet.title}' is missing columns: {', '.join(missing)}")
    key_index = headers.index(key)
    row_index_by_key: dict[str, tuple[int, list[str]]] = {}
    for row_number, row in enumerate(existing[1:], start=2):
        value = row[key_index] if key_index < len(row) else ""
        if value:
            row_index_by_key[value] = (row_number, row)

    inserted = 0
    updated = 0
    batch_updates: list[dict[str, Any]] = []
    rows_to_append: list[list[Any]] = []
    end_letter = _column_letter(len(headers))

    for record in records:
        identifier = str(record.get(key) or "")
        if not identifier:
            raise SheetsConfigurationError(f"Cannot sync a {worksheet.title} row without {key}.")
        if identifier in row_index_by_key:
            row_number, old_row = row_index_by_key[identifier]
            values = [
                record.get(column, old_row[index] if index < len(old_row) else "")
                for index, column in enumerate(headers)
            ]
            batch_updates.append({"range": f"A{row_number}:{end_letter}{row_number}", "values": [values]})
            row_index_by_key[identifier] = (row_number, values)
            updated += 1
        else:
            values = _row_from_record(record, headers)
            rows_to_append.append(values)
            inserted += 1
            row_index_by_key[identifier] = (len(existing) + inserted, values)

    if batch_updates:
        if hasattr(worksheet, "batch_update"):
            worksheet.batch_update(batch_updates, value_input_option="USER_ENTERED")
        else:
            for item in batch_updates:
                worksheet.update(item["values"], item["range"])

    if rows_to_append:
        if hasattr(worksheet, "append_rows"):
            worksheet.append_rows(rows_to_append, value_input_option="USER_ENTERED")
        else:
            for row_vals in rows_to_append:
                worksheet.append_row(row_vals, value_input_option="USER_ENTERED")

    return {"inserted": inserted, "updated": updated}


def _column_letter(number: int) -> str:
    result = ""
    while number:
        number, remainder = divmod(number - 1, 26)
        result = chr(65 + remainder) + result
    return result


def sync_state_to_sheets(
    state: dict[str, list[dict[str, Any]]],
    *,
    client: Any | None = None,
    spreadsheet_id: str | None = None,
    service_account_file: str | None = None,
) -> dict[str, Any]:
    """Sync local state to pre-created Audit Log and Tickets tabs.

    `client` is injectable for tests. No Google network call happens unless the
    caller invokes this function with credentials or an explicit test client.
    """
    sheet_id = (os.getenv("GOOGLE_SHEET_ID", "") if spreadsheet_id is None else spreadsheet_id).strip()
    credential_file = (os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "") if service_account_file is None else service_account_file).strip()
    if client is None:
        if not sheet_id or not credential_file:
            raise SheetsConfigurationError(
                "Set GOOGLE_SHEET_ID and GOOGLE_SERVICE_ACCOUNT_FILE before syncing."
            )
        try:
            import gspread  # type: ignore
        except ImportError as exc:
            raise SheetsConfigurationError(
                "Install the optional Google Sheets dependencies with `pip install -r requirements-sheets.txt`."
            ) from exc
        try:
            client = gspread.service_account(filename=credential_file)
        except Exception as exc:
            raise SheetsConfigurationError("Could not authenticate the Google service account.") from exc

    try:
        spreadsheet = client.open_by_key(sheet_id) if sheet_id else client.open(os.getenv("GOOGLE_SHEET_TITLE", "Kesar Support"))
        audit_ws = spreadsheet.worksheet("Audit Log")
        tickets_ws = spreadsheet.worksheet("Tickets")
        audit_result = _upsert(audit_ws, state.get("audit", []), AUDIT_COLUMNS, "email_id")
        ticket_result = _upsert(tickets_ws, state.get("tickets", []), TICKET_COLUMNS, "ticket_id")
    except SheetsConfigurationError:
        raise
    except Exception as exc:
        raise SheetsConfigurationError(
            "Google Sheets sync failed. Check spreadsheet sharing, tab names, headers and service-account access."
        ) from exc

    return {
        "audit_log": audit_result,
        "tickets": ticket_result,
        "spreadsheet": "configured",
    }
