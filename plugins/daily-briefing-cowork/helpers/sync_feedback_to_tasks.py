"""Sync briefing-dashboard feedback (task_proposals + acks) into the
local tasks.json. Runs deterministically every ~2 hours via the live-
tasks cron. "Active James request" rule: tasks.json is mutated ONLY in
response to James's explicit dashboard clicks (📌 send to tasks, ✕
not a priority, ✅ mark done). Nothing in this script invents tasks or
rerankings — it only mirrors his clicks.

Two flows:

  1) PROMOTE: task_proposals with status="pending" → append to
     tasks.json's `tasks` array as new todo items. Update the Sheet
     row status to "promoted" so we don't re-add on the next cron.

  2) DONE: briefing acks contain done_keys (per-item dismissals from
     the briefing dashboard or carryover "mark done"). For each
     done_key that matches a task's `briefing_key`, move that task
     from `tasks` to `completed` with status="done".

Usage:
    python sync_feedback_to_tasks.py
    python sync_feedback_to_tasks.py --dry-run    # show what would change

Exits 0 on success. Logs to stderr.
"""

from __future__ import annotations
import argparse
import datetime as dt
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

from daily_briefing import (  # noqa: E402
    google_creds, read_acks, _sheets, TASKS_JSON_PATH, ACK_SHEET_ID,
)


def _slugify(text: str) -> str:
    """Stable kebab-case slug from a title. Used for new task IDs."""
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:60] or "untitled"


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _read_proposals(creds) -> tuple[list[dict], list[str]]:
    """Return (rows, header) from the task_proposals tab.

    The Apps Script writes columns:
      A:date  B:title  C:key  D:urgency  E:section  F:status
    (status starts as "pending"; we update to "promoted" after import.)
    """
    if not ACK_SHEET_ID:
        return [], []
    try:
        resp = _sheets(creds).spreadsheets().values().get(
            spreadsheetId=ACK_SHEET_ID, range="task_proposals!A:Z"
        ).execute()
    except Exception as e:
        print(f"  couldn't read task_proposals: {e}", file=sys.stderr)
        return [], []
    rows = resp.get("values", [])
    if len(rows) < 2:
        return [], rows[0] if rows else []
    header = rows[0]
    parsed = [dict(zip(header, r + [""] * (len(header) - len(r))))
              for r in rows[1:]]
    # Carry the absolute row index (1-based, +1 for header) so we can
    # write back the status change to the right cell.
    for i, row in enumerate(parsed, start=2):
        row["_row_index"] = i
    return parsed, header


def _update_proposal_status(creds, header: list[str],
                            row_index: int, new_status: str) -> None:
    """Write `new_status` to the status column for one task_proposals row."""
    if not ACK_SHEET_ID:
        return
    try:
        status_col = header.index("status")
    except ValueError:
        # Fall back to column F (6) which matches Apps Script's writer.
        status_col = 5
    # A1 column letter from zero-based col index
    col_letter = chr(ord("A") + status_col)
    range_ = f"task_proposals!{col_letter}{row_index}:{col_letter}{row_index}"
    try:
        _sheets(creds).spreadsheets().values().update(
            spreadsheetId=ACK_SHEET_ID, range=range_,
            valueInputOption="RAW",
            body={"values": [[new_status]]},
        ).execute()
    except Exception as e:
        print(f"  couldn't update proposal row {row_index}: {e}",
              file=sys.stderr)


def _load_tasks_json() -> dict:
    if not TASKS_JSON_PATH.exists():
        return {"last_updated": _now_iso(),
                "tasks": [], "completed": [], "backlog": []}
    try:
        return json.loads(TASKS_JSON_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as e:
        print(f"  tasks.json unreadable ({e}); refusing to overwrite",
              file=sys.stderr)
        raise


def _save_tasks_json(data: dict, dry_run: bool) -> None:
    data["last_updated"] = _now_iso()
    if dry_run:
        print("  [dry-run] would write tasks.json", file=sys.stderr)
        return
    # Atomic write: write to tmp + rename, so a crash mid-write doesn't
    # corrupt the file (which tasks-cowork is reading concurrently).
    tmp = TASKS_JSON_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    tmp.replace(TASKS_JSON_PATH)


def _promote(tasks_data: dict, proposals: list[dict], header: list[str],
             creds, dry_run: bool) -> int:
    """Append pending proposals to tasks.json; mark Sheet rows promoted."""
    existing_ids = {t.get("id") for t in tasks_data.get("tasks", [])}
    existing_keys = {t.get("briefing_key") for t in tasks_data.get("tasks", [])}
    added = 0
    for p in proposals:
        if (p.get("status") or "").lower() != "pending":
            continue
        title = (p.get("title") or "").strip()
        if not title:
            continue
        briefing_key = (p.get("key") or "").strip()
        # Dedup: skip if a task with this briefing_key (or same title-id)
        # already exists.
        candidate_id = _slugify(title)
        if (briefing_key and briefing_key in existing_keys) or \
           candidate_id in existing_ids:
            print(f"  skip dup proposal: {title[:60]!r}", file=sys.stderr)
            if not dry_run:
                _update_proposal_status(creds, header, p["_row_index"],
                                        "promoted-dup")
            continue
        task = {
            "id": candidate_id,
            "title": title,
            "why": (f"Promoted from briefing dashboard on "
                    f"{p.get('date', _now_iso()[:10])}. "
                    f"Section: {p.get('section', '?')}."),
            "domain": "work",
            "urgency": (p.get("urgency") or "medium").lower(),
            "blocked_by": None,
            "unblocks": [],
            "status": "todo",
            "added": _now_iso(),
            "updated": _now_iso(),
            "briefing_key": briefing_key,
        }
        tasks_data.setdefault("tasks", []).append(task)
        existing_ids.add(candidate_id)
        if briefing_key:
            existing_keys.add(briefing_key)
        added += 1
        print(f"  + promoted: {title[:80]}", file=sys.stderr)
        if not dry_run:
            _update_proposal_status(creds, header, p["_row_index"],
                                    "promoted")
    return added


def _mark_done(tasks_data: dict, acks: list[dict], dry_run: bool) -> int:
    """Move tasks from `tasks` to `completed` for any matching done_keys."""
    done_keys: set[str] = set()
    for a in acks:
        for k in (a.get("done_keys") or "").split(","):
            k = k.strip()
            if k:
                done_keys.add(k)
    if not done_keys:
        return 0

    still_active: list[dict] = []
    moved = 0
    for t in tasks_data.get("tasks", []):
        bk = t.get("briefing_key", "")
        tid = t.get("id", "")
        if (bk and bk in done_keys) or (tid and tid in done_keys):
            t["status"] = "done"
            t["updated"] = _now_iso()
            t["completed_at"] = _now_iso()
            tasks_data.setdefault("completed", []).append(t)
            moved += 1
            print(f"  ✓ done: {t.get('title', '')[:80]}", file=sys.stderr)
        else:
            still_active.append(t)
    tasks_data["tasks"] = still_active
    return moved


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="Print changes without writing anything.")
    args = parser.parse_args()

    creds = google_creds()
    proposals, header = _read_proposals(creds)
    acks = read_acks(creds)
    print(f"  {len(proposals)} task_proposals rows · {len(acks)} ack rows",
          file=sys.stderr)

    tasks_data = _load_tasks_json()
    pre_active = len(tasks_data.get("tasks", []))

    added = _promote(tasks_data, proposals, header, creds, args.dry_run)
    moved = _mark_done(tasks_data, acks, args.dry_run)

    print(f"  active tasks: {pre_active} → "
          f"{len(tasks_data.get('tasks', []))} "
          f"(+{added} promoted, -{moved} done)", file=sys.stderr)

    if added or moved:
        _save_tasks_json(tasks_data, args.dry_run)
        print(f"  {'[dry-run] ' if args.dry_run else ''}"
              f"tasks.json updated", file=sys.stderr)
    else:
        print("  no changes", file=sys.stderr)


if __name__ == "__main__":
    main()
