"""Sync an already generated batch result JSON to the configured Google Sheet."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from kesar_copilot.google_sheets import sync_state_to_sheets


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync Kesar audit and ticket output to Google Sheets")
    parser.add_argument("--input", default="outputs/results_v2.json", help="batch output JSON path")
    args = parser.parse_args()
    path = Path(args.input)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    if not path.exists():
        raise SystemExit(f"Input file not found: {path}")
    state = json.loads(path.read_text(encoding="utf-8"))
    result = sync_state_to_sheets(state)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
