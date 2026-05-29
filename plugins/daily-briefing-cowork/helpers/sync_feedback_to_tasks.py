"""Sync briefing-dashboard feedback into the local tasks.json. Runs
every ~2 hours via the live-tasks cron. STRICT rule: tasks.json is
mutated ONLY in response to James's explicit clicks (✅ add on a
suggestion, ✕ dismiss a suggestion, or ✅ mark done on a task).
Briefing "📌 send to tasks" creates a SUGGESTION — it does NOT
auto-add to tasks.json. Auto-promote was removed because James was
seeing tasks he hadn't actually confirmed.

The flows, all triggered by acks-tab entries with prefixed keys:

  1) ACCEPT: ack with done_keys="accept:<proposal-row-key>" →
     find matching pending proposal, add to tasks.json as a todo,
     update proposal's Sheet row to status="promoted".

  2) REJECT: ack with done_keys="reject:<proposal-row-key>" →
     mark proposal status="rejected" so it stops showing as
     suggested. tasks.json is NOT modified.

  3) DONE: ack with bare done_keys=<task-id-or-briefing-key> →
     move matching task from `tasks` to `completed`. Same as before.

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


def _collect_ack_keys(acks: list[dict]) -> tuple[set[str], set[str], set[str]]:
    """Split acks into three buckets by prefix:
      accept:<K>   → James clicked ✅ add on a suggestion
      reject:<K>   → James clicked ✕ dismiss on a suggestion
      <K>          → James clicked ✅ mark done on an active task
    """
    accept: set[str] = set()
    reject: set[str] = set()
    done: set[str] = set()
    for a in acks:
        for k in (a.get("done_keys") or "").split(","):
            k = k.strip()
            if not k:
                continue
            if k.startswith("accept:"):
                accept.add(k[len("accept:"):])
            elif k.startswith("reject:"):
                reject.add(k[len("reject:"):])
            else:
                done.add(k)
    return accept, reject, done


def _accept(tasks_data: dict, proposals: list[dict], header: list[str],
            accept_keys: set[str], creds, dry_run: bool) -> int:
    """Promote a proposal to tasks.json ONLY if James clicked ✅ add.
    Matches proposals by their briefing-item `key` column (column C in
    the task_proposals tab — same key the briefing emitted on its
    📌 send-to-tasks click)."""
    if not accept_keys:
        return 0
    existing_ids = {t.get("id") for t in tasks_data.get("tasks", [])}
    existing_keys = {t.get("briefing_key")
                     for t in tasks_data.get("tasks", [])
                     if t.get("briefing_key")}
    added = 0
    for p in proposals:
        proposal_key = (p.get("key") or "").strip()
        if not proposal_key or proposal_key not in accept_keys:
            continue
        # Already promoted on a prior cron? Skip + don't double-add.
        if (p.get("status") or "").lower() not in ("", "pending"):
            continue
        title = (p.get("title") or "").strip()
        if not title:
            continue
        candidate_id = _slugify(title)
        if proposal_key in existing_keys or candidate_id in existing_ids:
            print(f"  skip dup proposal: {title[:60]!r}", file=sys.stderr)
            if not dry_run:
                _update_proposal_status(creds, header, p["_row_index"],
                                        "promoted-dup")
            continue
        task = {
            "id": candidate_id,
            "title": title,
            "why": (f"Confirmed via tasks-live dashboard from a "
                    f"{p.get('date', _now_iso()[:10])} briefing suggestion "
                    f"(section: {p.get('section', '?')})."),
            "domain": "work",
            "urgency": (p.get("urgency") or "medium").lower(),
            "blocked_by": None,
            "unblocks": [],
            "status": "todo",
            "added": _now_iso(),
            "updated": _now_iso(),
            "briefing_key": proposal_key,
            "provenance": "user-confirmed-suggestion",
        }
        tasks_data.setdefault("tasks", []).append(task)
        existing_ids.add(candidate_id)
        existing_keys.add(proposal_key)
        added += 1
        print(f"  + confirmed: {title[:80]}", file=sys.stderr)
        if not dry_run:
            _update_proposal_status(creds, header, p["_row_index"],
                                    "promoted")
    return added


def _reject(proposals: list[dict], header: list[str],
            reject_keys: set[str], creds, dry_run: bool) -> int:
    """Mark a proposal as rejected. tasks.json is NOT touched."""
    if not reject_keys:
        return 0
    rejected = 0
    for p in proposals:
        proposal_key = (p.get("key") or "").strip()
        if not proposal_key or proposal_key not in reject_keys:
            continue
        if (p.get("status") or "").lower() not in ("", "pending"):
            continue
        rejected += 1
        print(f"  ✕ rejected: {p.get('title', '')[:80]}", file=sys.stderr)
        if not dry_run:
            _update_proposal_status(creds, header, p["_row_index"],
                                    "rejected")
    return rejected


def _mark_done(tasks_data: dict, done_keys: set[str], dry_run: bool) -> int:
    """Move tasks from `tasks` to `completed` for any matching done_keys.
    Matches by briefing_key OR task id."""
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
    accept_keys, reject_keys, done_keys = _collect_ack_keys(acks)
    print(f"  {len(proposals)} proposals · {len(acks)} acks "
          f"({len(accept_keys)} accept · {len(reject_keys)} reject · "
          f"{len(done_keys)} done)", file=sys.stderr)

    tasks_data = _load_tasks_json()
    pre_active = len(tasks_data.get("tasks", []))

    added = _accept(tasks_data, proposals, header,
                    accept_keys, creds, args.dry_run)
    rejected = _reject(proposals, header, reject_keys, creds, args.dry_run)
    moved = _mark_done(tasks_data, done_keys, args.dry_run)

    print(f"  active tasks: {pre_active} → "
          f"{len(tasks_data.get('tasks', []))} "
          f"(+{added} confirmed, -{moved} done, {rejected} suggestions dismissed)",
          file=sys.stderr)

    if added or moved:
        _save_tasks_json(tasks_data, args.dry_run)
        print(f"  {'[dry-run] ' if args.dry_run else ''}"
              f"tasks.json updated", file=sys.stderr)
    else:
        print("  no changes to tasks.json", file=sys.stderr)


if __name__ == "__main__":
    main()
