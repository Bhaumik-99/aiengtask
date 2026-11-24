import unittest

from kesar_copilot.google_sheets import SheetsConfigurationError, sync_state_to_sheets, AUDIT_COLUMNS, TICKET_COLUMNS


class FakeWorksheet:
    def __init__(self, title, headers):
        self.title = title
        self.rows = [headers[:]]

    def get_all_values(self):
        return [row[:] for row in self.rows]

    def append_row(self, values, value_input_option=None):
        self.rows.append(values[:])

    def update(self, values, range_name):
        # Our fake only needs to support the full-row update used by the adapter.
        import re
        match = re.match(r"A(\d+):[A-Z]+(\d+)$", range_name)
        row_index = int(match.group(1)) - 1
        self.rows[row_index] = values[0][:]


class FakeSpreadsheet:
    def __init__(self):
        self.tabs = {
            "Audit Log": FakeWorksheet("Audit Log", AUDIT_COLUMNS),
            "Tickets": FakeWorksheet("Tickets", TICKET_COLUMNS),
        }

    def worksheet(self, name):
        return self.tabs[name]


class FakeClient:
    def __init__(self, spreadsheet):
        self.spreadsheet = spreadsheet

    def open_by_key(self, key):
        self.key = key
        return self.spreadsheet


class GoogleSheetsTests(unittest.TestCase):
    def setUp(self):
        self.book = FakeSpreadsheet()
        self.client = FakeClient(self.book)
        self.state = {
            "audit": [{
                "email_id": "E01", "received_at": "2026-11-20T09:02:00+05:30",
                "from_email": "a@example.com", "intent": "order_status", "secondary_intent": None,
                "order_id": "KC-1041", "language": "en", "sentiment": "neutral", "confidence": 0.96,
                "order_found": "true", "action_taken": "draft_reply", "needs_human": "false",
                "ticket_id": "", "reply_draft": "Draft", "prompt_version": "v2",
                "processed_at": "now", "error": "", "notes": "",
            }],
            "tickets": [{
                "ticket_id": "TKT-001", "created_at": "now", "updated_at": "now", "order_id": "KC-1047",
                "customer_email": "k@example.com", "email_ids": "E03", "intent": "return_refund",
                "priority": "Medium", "status": "Open", "summary": "return request", "notes": "review",
            }],
        }

    def test_sync_inserts_audit_and_ticket_rows(self):
        result = sync_state_to_sheets(self.state, client=self.client, spreadsheet_id="sheet-123")
        self.assertEqual(result["audit_log"]["inserted"], 1)
        self.assertEqual(result["tickets"]["inserted"], 1)
        self.assertEqual(self.book.tabs["Audit Log"].rows[1][0], "E01")
        self.assertEqual(self.book.tabs["Tickets"].rows[1][0], "TKT-001")

    def test_sync_updates_existing_rows_instead_of_duplicating(self):
        sync_state_to_sheets(self.state, client=self.client, spreadsheet_id="sheet-123")
        self.state["audit"][0]["reply_draft"] = "Updated draft"
        self.state["tickets"][0]["notes"] = "Added follow-up note"
        result = sync_state_to_sheets(self.state, client=self.client, spreadsheet_id="sheet-123")
        self.assertEqual(result["audit_log"]["updated"], 1)
        self.assertEqual(result["tickets"]["updated"], 1)
        self.assertEqual(len(self.book.tabs["Audit Log"].rows), 2)
        self.assertEqual(len(self.book.tabs["Tickets"].rows), 2)
        self.assertEqual(self.book.tabs["Audit Log"].rows[1][13], "Updated draft")

    def test_sync_requires_expected_headers(self):
        bad_book = FakeSpreadsheet()
        bad_book.tabs["Audit Log"] = FakeWorksheet("Audit Log", ["email_id", "intent"])
        bad_client = FakeClient(bad_book)
        with self.assertRaises(SheetsConfigurationError):
            sync_state_to_sheets(self.state, client=bad_client, spreadsheet_id="sheet-123")

    def test_sync_requires_credentials_without_injected_client(self):
        with self.assertRaises(SheetsConfigurationError):
            sync_state_to_sheets(self.state, spreadsheet_id="", service_account_file="")


if __name__ == "__main__":
    unittest.main(verbosity=2)
