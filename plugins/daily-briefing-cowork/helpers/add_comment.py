"""Add a comment to a briefing item via the Apps Script webhook. Writes
to the `comments` Sheet tab — same mechanism the dashboard 💬 button
uses. Comments become binding guidance the next autonomous run's prompts
pick up via prefs_digest.

Usage:
    python add_comment.py --item-key <12-char-key> --section news \\
      --text "Skip future items on this funder — already covered on 5/20"
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
    parser.add_argument("--item-key", required=True)
    parser.add_argument("--section", required=True)
    parser.add_argument("--text", required=True)
    parser.add_argument("--date", default=None,
                        help="Briefing date (defaults to today)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if not ACK_WEBHOOK_URL:
        sys.exit("ACK_WEBHOOK_URL is not set — can't add comment")

    date = args.date or dt.date.today().isoformat()
    params = {
        "comment": args.text[:2000],
        "key": args.item_key,
        "section": args.section,
        "date": date,
    }
    url = f"{ACK_WEBHOOK_URL}?{urllib.parse.urlencode(params)}"

    if args.dry_run:
        print(f"--- dry run --- {url}", file=sys.stderr)
        return

    r = requests.get(url, timeout=15)
    print(f"add_comment: {r.status_code} — {r.text[:200]}", file=sys.stderr)
    r.raise_for_status()


if __name__ == "__main__":
    main()
