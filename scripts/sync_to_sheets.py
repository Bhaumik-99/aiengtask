"""Sync generated batch output JSON to Google Sheets (`Audit Log` and `Tickets` tabs).

Supports `--sheet-url` or `--sheet-id` (or `GOOGLE_SHEET_ID` in `.env`), and
`--init-tabs` to automatically create and initialize the `Audit Log` and `Tickets`
worksheets with the required template headers on a blank shared Google Sheet!
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from kesar_copilot.engine import load_dotenv  # noqa: E402
from kesar_copilot.google_sheets import (  # noqa: E402
    AUDIT_COLUMNS,
    TICKET_COLUMNS,
    sync_state_to_sheets,
)

load_dotenv()

EXTENDED_AUDIT_COLUMNS = AUDIT_COLUMNS + [
    "json_valid_first_try",
    "agent_status",
    "agent_tool_calls",
    "agent_draft_used",
    "llm_retries",
    "total_tokens",
    "estimated_cost_usd",
]


def _extract_sheet_id(raw: str) -> str:
    cleaned = raw.strip()
    match = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", cleaned)
    return match.group(1) if match else cleaned


def _ensure_tabs_and_headers(spreadsheet: Any) -> None:
    existing_titles = {ws.title: ws for ws in spreadsheet.worksheets()}

    for tab_name, headers in (("Audit Log", EXTENDED_AUDIT_COLUMNS), ("Tickets", TICKET_COLUMNS)):
        if tab_name in existing_titles:
            ws = existing_titles[tab_name]
        else:
            ws = spreadsheet.add_worksheet(title=tab_name, rows=100, cols=len(headers) + 2)
        values = ws.get_all_values()
        if not values or not any(cell.strip() for cell in values[0]):
            ws.update([headers], f"A1:{_col_letter(len(headers))}1")
            try:
                ws.freeze(rows=1)
                ws.format(f"A1:{_col_letter(len(headers))}1", {"textFormat": {"bold": True}})
            except Exception:
                pass

    # Clean up default empty 'Sheet1' if both required tabs now exist
    all_ws = spreadsheet.worksheets()
    if len(all_ws) > 2:
        for ws in all_ws:
            if ws.title == "Sheet1" and not ws.get_all_values():
                try:
                    spreadsheet.del_worksheet(ws)
                except Exception:
                    pass


def _col_letter(num: int) -> str:
    res = ""
    while num:
        num, rem = divmod(num - 1, 26)
        res = chr(65 + rem) + res
    return res


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync Kesar audit and ticket output to Google Sheets")
    parser.add_argument("--input", default="outputs/results_v2.json", help="batch output JSON path")
    parser.add_argument("--sheet", default="", help="Google Sheet URL or Spreadsheet ID (overrides GOOGLE_SHEET_ID)")
    parser.add_argument("--init-tabs", action="store_true", default=True, help="Auto-create Audit Log & Tickets tabs/headers if empty")
    args = parser.parse_args()

    path = Path(args.input)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    if not path.exists():
        raise SystemExit(f"Input file not found: {path}")

    sheet_id = _extract_sheet_id(args.sheet or os.getenv("GOOGLE_SHEET_ID", ""))
    cred_file = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "").strip()
    if not sheet_id:
        raise SystemExit("Provide --sheet <URL_OR_ID> or set GOOGLE_SHEET_ID in .env")
    if not cred_file:
        raise SystemExit("Set GOOGLE_SERVICE_ACCOUNT_FILE in .env")

    import gspread  # type: ignore

    client = gspread.service_account(filename=cred_file)
    if args.init_tabs:
        spreadsheet = client.open_by_key(sheet_id)
        _ensure_tabs_and_headers(spreadsheet)

    state = json.loads(path.read_text(encoding="utf-8"))
    result = sync_state_to_sheets(state, client=client, spreadsheet_id=sheet_id)
    result["sheet_url"] = f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit"
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
