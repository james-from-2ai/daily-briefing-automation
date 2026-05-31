"""Post the briefing's actionable items to Slack with short codes, and
persist the code→item-key map so reply commands can resolve them.

Part of interactive Slack: the morning DM lists today's top priorities,
slips, and decisions as `P1`/`S1`/`D1`, with a one-line reply cheatsheet.
James replies (e.g. `done S1`, `task P2`, `note D1 ...`) and the 2-hourly
scrape_slack_replies.py resolves the code via the `slack_items` tab and
acts on the right item.

Input JSON (--items-file): a list of objects the agent emits after
synthesis:
    [{"code": "P1", "type": "priority", "title": "...", "key": "<item_key>"}, ...]
(`code` optional — assigned here if missing, grouped by type.)

Usage:
    python post_slack_actions.py --items-file /tmp/slack-action-items.json
    python post_slack_actions.py --items-file ... --dry-run
"""

from __future__ import annotations
import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

from daily_briefing import (  # noqa: E402
    google_creds, _sheets, ACK_SHEET_ID, SLACK_USER_ID, item_key,
)

SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")
SLACK_ITEMS_TAB = "slack_items"
TYPE_ORDER = ["priority", "slip", "decision"]
TYPE_PREFIX = {"priority": "P", "slip": "S", "decision": "D"}
TYPE_LABEL = {"priority": "Priorities", "slip": "Slips",
              "decision": "Decisions"}
# Key scheme MUST match propose_tasks_from_briefing.py so `done S1` on a slip
# marks the proposed task done, and priorities resolve to their state key.
_KEY_SECTION = {"priority": "priority", "slip": "briefing-slip",
                "decision": "briefing-decision"}


def _assign_codes(items: list[dict]) -> list[dict]:
    """Assign P1/S1/D1… per type if codes are missing; keep provided ones."""
    counters = {t: 0 for t in TYPE_PREFIX}
    out = []
    for it in items:
        typ = (it.get("type") or "").strip().lower()
        if typ not in TYPE_PREFIX:
            continue
        code = (it.get("code") or "").strip()
        if not code:
            counters[typ] += 1
            code = f"{TYPE_PREFIX[typ]}{counters[typ]}"
        title = (it.get("title") or "").strip()
        # Compute the resolve key the same way the proposer does, unless the
        # agent supplied one explicitly.
        key = (it.get("key") or "").strip() or item_key(_KEY_SECTION[typ], title)
        out.append({**it, "type": typ, "code": code, "key": key,
                    "title": title})
    return out


def _persist_map(sheets, items: list[dict], today: str, dry_run: bool) -> None:
    """Overwrite the slack_items tab with today's code→key map."""
    values = [["date", "code", "key", "type", "title"]]
    for it in items:
        values.append([today, it["code"], it.get("key", ""),
                       it["type"], (it.get("title") or "")[:300]])
    if dry_run:
        print(f"  [dry-run] would persist {len(items)} coded items to "
              f"{SLACK_ITEMS_TAB}", file=sys.stderr)
        return
    try:
        sheets.spreadsheets().batchUpdate(
            spreadsheetId=ACK_SHEET_ID,
            body={"requests": [{"addSheet": {
                "properties": {"title": SLACK_ITEMS_TAB}}}]},
        ).execute()
    except Exception:
        pass  # tab already exists
    sheets.spreadsheets().values().clear(
        spreadsheetId=ACK_SHEET_ID, range=f"{SLACK_ITEMS_TAB}!A:Z").execute()
    sheets.spreadsheets().values().update(
        spreadsheetId=ACK_SHEET_ID, range=f"{SLACK_ITEMS_TAB}!A1",
        valueInputOption="RAW", body={"values": values}).execute()


def _format_message(items: list[dict]) -> str:
    lines = [
        ":dart: *Act on today's briefing* — reply here within ~2h:",
        "`done S1` ✓ done · `task P2` add to tasks · `note D1 …` log a note",
    ]
    for typ in TYPE_ORDER:
        group = [it for it in items if it["type"] == typ]
        if not group:
            continue
        lines.append(f"\n*{TYPE_LABEL[typ]}*")
        for it in group:
            lines.append(f"`{it['code']}`  {(it.get('title') or '').strip()}")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--items-file", required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    p = Path(args.items_file)
    if not p.exists():
        print(f"  no items file at {p}; skipping Slack actions", file=sys.stderr)
        return
    items = _assign_codes(json.loads(p.read_text(encoding="utf-8")) or [])
    if not items:
        print("  no actionable items; skipping Slack actions", file=sys.stderr)
        return

    today = dt.date.today().isoformat()
    creds = google_creds()
    sheets = _sheets(creds)
    _persist_map(sheets, items, today, args.dry_run)

    msg = _format_message(items)
    if args.dry_run or not SLACK_BOT_TOKEN:
        print(f"  [{'dry-run' if args.dry_run else 'no-token'}] Slack actions "
              f"message ({len(items)} items):\n{msg}", file=sys.stderr)
        return

    from slack_sdk import WebClient
    from slack_sdk.errors import SlackApiError
    try:
        WebClient(token=SLACK_BOT_TOKEN).chat_postMessage(
            channel=SLACK_USER_ID, text=msg)
        print(f"  posted {len(items)} coded action items to Slack",
              file=sys.stderr)
    except SlackApiError as e:
        print(f"  slack post failed: {e.response['error']}", file=sys.stderr)


if __name__ == "__main__":
    main()
