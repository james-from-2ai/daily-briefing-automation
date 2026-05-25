"""Promote a briefing item to a task proposal. Writes via the Apps Script
webhook to the `task_proposals` Sheet tab — same mechanism the dashboard
"📌 send to tasks" button uses. James's separate tasks-cowork session
picks it up on its next run.

Usage:
    python promote_task.py \\
      --title "Schedule retreat planning kickoff" \\
      --urgency high \\
      --section priority \\
      --item-key <12-char-state-key>
"""

from __future__ import annotations
import argparse
import datetime as dt
import sys
import urllib.parse
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

import requests  # noqa: E402

from daily_briefing import ACK_WEBHOOK_URL  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--title", required=True)
    parser.add_argument("--urgency", default="medium",
                        choices=["high", "medium", "low"])
    parser.add_argument("--section", default="priority")
    parser.add_argument("--item-key", default="",
                        help="State-sheet key of the item being promoted")
    parser.add_argument("--date", default=None,
                        help="Briefing date (defaults to today)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if not ACK_WEBHOOK_URL:
        sys.exit("ACK_WEBHOOK_URL is not set — can't promote")

    date = args.date or dt.date.today().isoformat()
    params = {
        "task_proposal": args.title[:200],
        "key": args.item_key,
        "urgency": args.urgency,
        "section": args.section,
        "date": date,
    }
    url = f"{ACK_WEBHOOK_URL}?{urllib.parse.urlencode(params)}"

    if args.dry_run:
        print(f"--- dry run --- {url}", file=sys.stderr)
        return

    r = requests.get(url, timeout=15)
    print(f"promote_task: {r.status_code} — {r.text[:200]}", file=sys.stderr)
    r.raise_for_status()


if __name__ == "__main__":
    main()
