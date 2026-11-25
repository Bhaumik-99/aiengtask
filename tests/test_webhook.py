import json
import os
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

import webhook
from kesar_copilot.engine import load


class WebhookIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.email = load("data/test_emails.json")["emails"][3]

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tempdir.name) / "copilot.sqlite3"
        self.server = webhook.create_server(host="127.0.0.1", port=0, db_path=self.db_path)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.env = patch.dict(os.environ, {
            "WEBHOOK_SECRET": "s" * 40,
            "ENABLE_AGENT_TOOLS": "false",
            "ENABLE_LLM_CLASSIFICATION": "false",
            "GOOGLE_SHEET_ID": "",
            "GOOGLE_SERVICE_ACCOUNT_FILE": "",
        }, clear=False)
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.tempdir.cleanup()

    def post(self, body, secret="s" * 40, content_type="application/json"):
        data = json.dumps(body).encode("utf-8")
        request = Request(
            self.base_url + "/webhook/support-email",
            data=data,
            headers={"Content-Type": content_type, "X-Webhook-Secret": secret},
            method="POST",
        )
        try:
            with urlopen(request, timeout=3) as response:
                return response.status, json.loads(response.read())
        except HTTPError as error:
            return error.code, json.loads(error.read())

    def test_valid_request_persists_audit_and_ticket(self):
        status, payload = self.post(self.email)
        self.assertEqual(status, 200)
        self.assertFalse(payload["idempotent_replay"])
        db = sqlite3.connect(self.db_path)
        try:
            audit_count = db.execute("SELECT COUNT(*) FROM audit").fetchone()[0]
            ticket_count = db.execute("SELECT COUNT(*) FROM tickets").fetchone()[0]
        finally:
            db.close()
        self.assertEqual(audit_count, 1)
        self.assertEqual(ticket_count, 1)

    def test_duplicate_webhook_delivery_is_idempotent(self):
        first_status, first = self.post(self.email)
        second_status, second = self.post(self.email)
        self.assertEqual(first_status, 200)
        self.assertEqual(second_status, 200)
        self.assertFalse(first["idempotent_replay"])
        self.assertTrue(second["idempotent_replay"])
        db = sqlite3.connect(self.db_path)
        try:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM audit").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM tickets").fetchone()[0], 1)
        finally:
            db.close()

    def test_invalid_secret_is_rejected(self):
        status, payload = self.post(self.email, secret="wrong-secret")
        self.assertEqual(status, 401)
        self.assertEqual(payload["error"], "unauthorized")

    def test_malformed_nested_sender_is_rejected_as_400(self):
        invalid = dict(self.email)
        invalid["from"] = "customer@example.com"
        status, payload = self.post(invalid)
        self.assertEqual(status, 400)
        self.assertIn("from must be an object", payload["error"])

    def test_non_json_content_type_is_rejected(self):
        status, payload = self.post(self.email, content_type="text/plain")
        self.assertEqual(status, 415)
        self.assertEqual(payload["error"], "content_type_must_be_application_json")

    def test_health_endpoint_checks_database(self):
        with urlopen(self.base_url + "/health", timeout=3) as response:
            payload = json.loads(response.read())
            self.assertEqual(response.status, 200)
        self.assertEqual(payload, {"ok": True, "database": "ok"})

    def test_weak_webhook_secret_is_rejected_as_misconfiguration(self):
        with patch.dict(os.environ, {"WEBHOOK_SECRET": "too-short"}, clear=False):
            status, payload = self.post(self.email, secret="too-short")
        self.assertEqual(status, 503)
        self.assertEqual(payload["error"], "webhook_secret_not_configured_securely")

    def test_invalid_json_is_rejected(self):
        request = Request(
            self.base_url + "/webhook/support-email",
            data=b"{not-json",
            headers={"Content-Type": "application/json", "X-Webhook-Secret": "s" * 40},
            method="POST",
        )
        try:
            with urlopen(request, timeout=3) as response:
                status, payload = response.status, json.loads(response.read())
        except HTTPError as error:
            status, payload = error.code, json.loads(error.read())
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], "invalid_json")

    def test_response_reports_sheets_not_configured_when_no_credentials(self):
        status, payload = self.post(self.email)
        self.assertEqual(status, 200)
        self.assertEqual(payload["sheets_sync"], {"status": "not_configured"})

    def test_sheets_failure_does_not_rollback_local_webhook_persistence(self):
        from kesar_copilot.google_sheets import SheetsConfigurationError
        with patch.dict(os.environ, {
            "GOOGLE_SHEET_ID": "sheet-id", "GOOGLE_SERVICE_ACCOUNT_FILE": "service-account.json"
        }, clear=False):
            with self.assertLogs("kesar.webhook", level="ERROR"):
                with patch("kesar_copilot.google_sheets.sync_state_to_sheets", side_effect=SheetsConfigurationError("mock failure")):
                    status, payload = self.post(self.email)
        self.assertEqual(status, 200)
        self.assertEqual(payload["sheets_sync"]["status"], "failed")
        db = sqlite3.connect(self.db_path)
        try:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM audit").fetchone()[0], 1)
        finally:
            db.close()



if __name__ == "__main__":
    unittest.main(verbosity=2)
