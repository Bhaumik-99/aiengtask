"""Minimal authenticated JSON webhook with SQLite persistence.

This is a local prototype endpoint, not a substitute for a signed Gmail/Outlook
provider webhook. Deploy behind TLS, rate limiting and provider verification.
"""
from __future__ import annotations

import hmac
import json
import logging
import os
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from kesar_copilot.engine import EmailValidationError, process, validate_email

ROOT = Path(__file__).resolve().parent
DEFAULT_DB_PATH = ROOT / "data" / "copilot.sqlite3"
MAX_BODY_BYTES = 1_000_000
LOGGER = logging.getLogger("kesar.webhook")


def resolve_db_path(raw_path: str | None = None) -> Path:
    candidate = Path(raw_path or os.getenv("COPILOT_DB", str(DEFAULT_DB_PATH))).expanduser()
    if not candidate.is_absolute():
        candidate = ROOT / candidate
    candidate.parent.mkdir(parents=True, exist_ok=True)
    return candidate


def init_db(db_path: Path | None = None) -> None:
    path = db_path or resolve_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=10)
    try:
        db.execute("PRAGMA busy_timeout = 10000")
        db.execute("CREATE TABLE IF NOT EXISTS audit (email_id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
        db.execute("CREATE TABLE IF NOT EXISTS tickets (ticket_id TEXT PRIMARY KEY, order_id TEXT, payload TEXT NOT NULL)")
        db.commit()
    finally:
        db.close()


def _read_state(db: sqlite3.Connection) -> dict[str, list[dict[str, Any]]]:
    audit_rows = db.execute("SELECT payload FROM audit ORDER BY rowid").fetchall()
    ticket_rows = db.execute("SELECT payload FROM tickets ORDER BY rowid").fetchall()
    return {
        "audit": [json.loads(row[0]) for row in audit_rows],
        "tickets": [json.loads(row[0]) for row in ticket_rows],
    }


def create_handler(db_path: Path):
    """Create an isolated handler class for the given SQLite database path."""

    class Handler(BaseHTTPRequestHandler):
        server_version = "KesarCopilot/0.3"

        def _respond(self, status: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802 - stdlib handler method name
            if self.path != "/health":
                return self._respond(404, {"error": "not_found"})
            try:
                db = sqlite3.connect(db_path, timeout=3)
                try:
                    db.execute("SELECT 1").fetchone()
                finally:
                    db.close()
                return self._respond(200, {"ok": True, "database": "ok"})
            except sqlite3.Error:
                return self._respond(503, {"ok": False, "database": "unavailable"})

        def do_POST(self) -> None:  # noqa: N802 - stdlib handler method name
            if self.path != "/webhook/support-email":
                return self._respond(404, {"error": "not_found"})

            secret = os.getenv("WEBHOOK_SECRET", "")
            supplied_secret = self.headers.get("X-Webhook-Secret", "")
            if len(secret) < 32:
                return self._respond(503, {"error": "webhook_secret_not_configured_securely"})
            if not supplied_secret or not hmac.compare_digest(supplied_secret, secret):
                return self._respond(401, {"error": "unauthorized"})

            content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            if content_type != "application/json":
                return self._respond(415, {"error": "content_type_must_be_application_json"})
            try:
                content_length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                return self._respond(400, {"error": "invalid_content_length"})
            if content_length <= 0:
                return self._respond(400, {"error": "empty_request_body"})
            if content_length > MAX_BODY_BYTES:
                return self._respond(413, {"error": "payload_too_large"})

            try:
                payload = json.loads(self.rfile.read(content_length))
                email = validate_email(payload)
            except (json.JSONDecodeError, UnicodeDecodeError):
                return self._respond(400, {"error": "invalid_json"})
            except EmailValidationError as exc:
                return self._respond(400, {"error": str(exc)})

            try:
                db = sqlite3.connect(db_path, timeout=10)
                try:
                    db.execute("PRAGMA busy_timeout = 10000")
                    db.execute("BEGIN IMMEDIATE")
                    state = _read_state(db)
                    was_duplicate = any(row.get("email_id") == email["id"] for row in state["audit"])
                    result = process(email, state, version="v2")
                    db.execute(
                        "INSERT OR IGNORE INTO audit(email_id, payload) VALUES(?, ?)",
                        (result["email_id"], json.dumps(result, ensure_ascii=False)),
                    )
                    for ticket in state["tickets"]:
                        db.execute(
                            "INSERT INTO tickets(ticket_id, order_id, payload) VALUES(?, ?, ?) "
                            "ON CONFLICT(ticket_id) DO UPDATE SET order_id=excluded.order_id, payload=excluded.payload",
                            (ticket["ticket_id"], ticket.get("order_id", ""), json.dumps(ticket, ensure_ascii=False)),
                        )
                    db.commit()
                finally:
                    db.close()
                sheets_sync: dict[str, Any] = {"status": "not_configured"}
                if os.getenv("GOOGLE_SHEET_ID") and os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE"):
                    try:
                        from kesar_copilot.google_sheets import sync_state_to_sheets
                        sheets_sync = {"status": "ok", "result": sync_state_to_sheets(state)}
                    except Exception:
                        # Local SQLite commit has succeeded; report the separate sink failure.
                        LOGGER.exception("Google Sheets sync failed after local persistence")
                        sheets_sync = {"status": "failed", "retry_required": True}
                return self._respond(200, {
                    "email_id": result["email_id"],
                    "action": result["action_taken"],
                    "needs_human": result["needs_human"],
                    "ticket_id": result["ticket_id"],
                    "idempotent_replay": was_duplicate,
                    "sheets_sync": sheets_sync,
                })
            except (sqlite3.Error, OSError):
                LOGGER.exception("Webhook persistence failure")
                return self._respond(503, {"error": "temporarily_unavailable"})
            except Exception:  # Keep internal exception details out of the response.
                LOGGER.exception("Webhook processing failure")
                return self._respond(500, {"error": "processing_failed"})

        def log_message(self, fmt: str, *args: Any) -> None:
            # Avoid writing customer email addresses or bodies into the default access log.
            LOGGER.info("HTTP request handled")

    return Handler


def create_server(host: str | None = None, port: int | None = None, db_path: Path | None = None) -> ThreadingHTTPServer:
    path = db_path or resolve_db_path()
    init_db(path)
    bind_host = host or os.getenv("HOST", "127.0.0.1")
    bind_port = int(port if port is not None else os.getenv("PORT", "8080"))
    return ThreadingHTTPServer((bind_host, bind_port), create_handler(path))


if __name__ == "__main__":
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    server = create_server()
    print(f"Kesar Copilot webhook listening on http://{server.server_address[0]}:{server.server_address[1]}")
    print("Use TLS and provider-signed webhook verification before public deployment.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
