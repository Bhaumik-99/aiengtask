"""Safely replay fixture emails to a webhook; dry-run is the default."""
from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FIXTURE = PROJECT_ROOT / "data" / "test_emails.json"


def main() -> None:
    parser = argparse.ArgumentParser(description="Replay fixture emails to a webhook (dry-run by default)")
    parser.add_argument("--url", required=True, help="Webhook URL")
    parser.add_argument("--file", default=str(DEFAULT_FIXTURE))
    parser.add_argument("--only", default="", help="Comma-separated email IDs, e.g. E01,E04")
    parser.add_argument("--delay", type=float, default=0.0, help="Delay between requests in seconds")
    parser.add_argument("--send", action="store_true", help="Actually send requests; otherwise only preview")
    parser.add_argument("--allow-external", action="store_true", help="Permit sending fixture PII to a non-local host")
    parser.add_argument("--header", action="append", default=[], help="HTTP header as Name:Value; repeatable")
    args = parser.parse_args()

    if args.delay < 0:
        parser.error("--delay must be zero or greater")
    parsed = urllib.parse.urlparse(args.url)
    if parsed.scheme != "http" and parsed.scheme != "https":
        parser.error("--url must use http or https")
    local_hosts = {"localhost", "127.0.0.1", "::1"}
    if args.send and parsed.hostname not in local_hosts and not args.allow_external:
        parser.error("Refusing to send fixture customer data to a non-local host without --allow-external")

    fixture = json.loads(Path(args.file).read_text(encoding="utf-8"))
    emails = fixture.get("emails", [])
    if args.only:
        wanted = {part.strip() for part in args.only.split(",") if part.strip()}
        emails = [email for email in emails if email.get("id") in wanted]
    if not emails:
        parser.error("No fixture emails selected")

    headers = {"Content-Type": "application/json"}
    for raw_header in args.header:
        if ":" not in raw_header:
            parser.error("Each --header must use Name:Value format")
        name, value = raw_header.split(":", 1)
        headers[name.strip()] = value.strip()

    for email in emails:
        if not args.send:
            print(f"DRY-RUN {email.get('id')} -> {args.url}")
            continue
        request = urllib.request.Request(
            args.url,
            data=json.dumps(email, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                body = response.read().decode("utf-8", errors="replace")
                print(email.get("id"), response.status, body[:160])
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            print(email.get("id"), "ERROR", type(exc).__name__)
        if args.delay:
            time.sleep(args.delay)


if __name__ == "__main__":
    main()
