"""Auto-propose tasks from the briefing's slips + decisions-needed.

Today, a slip or decision only becomes a task suggestion if James clicks
"📌 send to tasks" on it in the dashboard. This closes that loop: the
briefing proactively writes its "Likely to slip" + "Decisions needed"
items into the SAME `task_proposals` tab the dashboard 📌 button feeds, so
they show up in the tasks-live "💡 Suggested tasks" section with ✅/✕.
sync_feedback_to_tasks.py then promotes the ones James accepts. Nothing is
auto-added to tasks.json — these are suggestions, by design.

Reuses the full existing machinery; this script only POPULATES proposals.
The agent (which just synthesized the priorities) emits the items as JSON;
this appends them, deduped by a stable key against everything already in
the tab so the same slip isn't re-proposed every morning.

Input JSON (--proposals-file): a list of objects:
    [{"title": "...", "section": "slip|decision", "urgency": "high|medium|low"}, ...]

Usage:
    python propose_tasks_from_briefing.py --proposals-file /tmp/briefing-task-proposals.json
    python propose_tasks_from_briefing.py --proposals-file ... --dry-run
"""

from __future__ import annotations
import argparse
import datetime as dt
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

from daily_briefing import (  # noqa: E402
    google_creds, _sheets, ACK_SHEET_ID, item_key,
)

VALID_SECTIONS = {"slip", "decision"}


def _existing_proposal_keys(sheets) -> set[str]:
    """All keys (col C) already in task_proposals, ANY status — so we never
    re-propose an item we've proposed before (even if rejected/promoted)."""
    try:
        resp = sheets.spreadsheets().values().get(
            spreadsheetId=ACK_SHEET_ID, range="task_proposals!A:F"
        ).execute()
    except Exception:
        return set()
    rows = resp.get("values", [])
    keys: set[str] = set()
    for r in rows[1:]:
        if len(r) >= 3 and r[2].strip():
            keys.add(r[2].strip())
    return keys


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--proposals-file", required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    p = Path(args.proposals_file)
    if not p.exists():
        print(f"  no proposals file at {p}; nothing to do", file=sys.stderr)
        return
    items = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(items, list) or not items:
        print("  proposals file empty; nothing to do", file=sys.stderr)
        return

    if not ACK_SHEET_ID:
        print("  ACK_SHEET_ID not set; skipping", file=sys.stderr)
        return

    creds = google_creds()
    sheets = _sheets(creds)
    existing = _existing_proposal_keys(sheets)

    today_iso = dt.date.today().isoformat()
    rows: list[list] = []
    seen_this_run: set[str] = set()
    skipped = 0
    for it in items:
        title = (it.get("title") or "").strip()
        section = (it.get("section") or "").strip().lower()
        if not title or section not in VALID_SECTIONS:
            skipped += 1
            continue
        # Stable key so the same slip/decision dedups day-to-day.
        key = item_key(f"briefing-{section}", title)
        if key in existing or key in seen_this_run:
            skipped += 1
            continue
        seen_this_run.add(key)
        urgency = (it.get("urgency") or "medium").strip().lower()
        # task_proposals schema: A:date B:title C:key D:urgency E:section F:status
        rows.append([today_iso, title, key, urgency,
                     f"briefing-{section}", "pending"])
        print(f"  + propose ({section}): {title[:80]}", file=sys.stderr)

    if not rows:
        print(f"  nothing new to propose ({skipped} skipped as dup/invalid)",
              file=sys.stderr)
        return

    if args.dry_run:
        print(f"  [dry-run] would append {len(rows)} proposal(s)",
              file=sys.stderr)
        return

    sheets.spreadsheets().values().append(
        spreadsheetId=ACK_SHEET_ID, range="task_proposals!A:F",
        valueInputOption="RAW", body={"values": rows},
    ).execute()
    print(f"  appended {len(rows)} task proposal(s) "
          f"({skipped} skipped as dup/invalid)", file=sys.stderr)


if __name__ == "__main__":
    main()
