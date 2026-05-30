"""Extract James-owned commitments from recent Katie + Sarah 1:1 docs,
flag the ones that haven't been actioned, and emit those as
task_proposals rows (status=pending). They surface in the live tasks
dashboard's "💡 Suggested tasks" section, awaiting James's ✅ add.

This is pure detection — nothing is auto-added to tasks.json. Same
"only changed by active James click" rule the rest of the system
follows.

Slip detection logic (intentionally simple, no LLM):
  1. Pull recent 1:1 doc text (last N entries each).
  2. Run extract_action_items() — the existing regex-based pattern
     matcher in daily_briefing.py. Returns list of {section, key,
     text_html, source}.
  3. For each candidate:
       a) Skip if its key already exists in state with status='done'
          (you already acked it).
       b) Skip if it already exists as a task in tasks.json
          (briefing_key match) — no point suggesting what's tracked.
       c) Skip if the same key was suggested in the last 14 days and
          either dismissed or already promoted (de-dup; otherwise
          dismissed items would resurface every cron).
       d) Skip if the candidate text is short / generic.
  4. Survivors become new task_proposals rows.

Runs as part of the tasks-live 2-hour cron. Non-fatal if anything
fails — wrapper logs and continues.

Usage:
    python extract_1on1_slips.py
    python extract_1on1_slips.py --dry-run
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
    google_creds, pull_1on1_recent_entries, read_state, read_acks,
    apply_acks_to_state, extract_action_items, item_key,
    _sheets, ACK_SHEET_ID, TASKS_JSON_PATH, ONEONONE_DOCS,
)


# Skip suggestions where the same key has been seen recently — avoids
# resurfacing dismissed items every 2 hours.
DEDUP_LOOKBACK_DAYS = 14
MAX_PER_RUN = 8
MIN_TEXT_LENGTH = 18


def _read_task_proposals_recent(creds) -> set[str]:
    """Return the set of proposal keys touched in the last N days,
    regardless of status (pending/promoted/rejected). De-dups across
    runs."""
    if not ACK_SHEET_ID:
        return set()
    try:
        resp = _sheets(creds).spreadsheets().values().get(
            spreadsheetId=ACK_SHEET_ID, range="task_proposals!A:Z"
        ).execute()
    except Exception:
        return set()
    rows = resp.get("values", [])
    if len(rows) < 2:
        return set()
    header = rows[0]
    try:
        date_idx = header.index("date")
        key_idx = header.index("key")
    except ValueError:
        return set()
    cutoff = (dt.date.today() - dt.timedelta(days=DEDUP_LOOKBACK_DAYS)).isoformat()
    seen: set[str] = set()
    for r in rows[1:]:
        d = (r[date_idx] if date_idx < len(r) else "")[:10]
        k = r[key_idx] if key_idx < len(r) else ""
        if k and d >= cutoff:
            seen.add(k)
    return seen


def _existing_task_briefing_keys() -> set[str]:
    """All briefing_keys currently in tasks.json (active + completed)."""
    if not TASKS_JSON_PATH.exists():
        return set()
    try:
        data = json.loads(TASKS_JSON_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return set()
    keys: set[str] = set()
    for bucket in ("tasks", "completed", "backlog"):
        for t in data.get(bucket, []):
            bk = t.get("briefing_key")
            if bk:
                keys.add(bk)
    return keys


def _plain(text_html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text_html or "")).strip()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    creds = google_creds()

    # 1. Pull recent 1:1 entries from each tracked doc (Katie, Sarah).
    extracted: list[dict] = []
    for name, doc_id in ONEONONE_DOCS.items():
        try:
            text = pull_1on1_recent_entries(creds, doc_id)
        except Exception as e:
            print(f"  couldn't read {name} 1:1: {e}", file=sys.stderr)
            continue
        if not text:
            continue
        items = extract_action_items(text, f"{name} 1:1")
        # extract_action_items returns {section: "action_item", key, text_html,
        # source}. Stamp the 1:1 owner on each.
        for it in items:
            it["owner"] = name
            extracted.append(it)

    print(f"  raw action items extracted: {len(extracted)}", file=sys.stderr)
    if not extracted:
        return

    # 2. Build the dedup filters.
    state = read_state(creds)
    acks = read_acks(creds)
    state = apply_acks_to_state(state, acks)
    done_keys: set[str] = {r.get("key") for r in state
                           if r.get("status") == "done"}
    # Same-text-key dedup: any item whose key already appears in state
    # (regardless of status) shouldn't be re-suggested — the briefing
    # already tracks it.
    state_keys: set[str] = {r.get("key") for r in state if r.get("key")}
    recent_proposal_keys = _read_task_proposals_recent(creds)
    task_briefing_keys = _existing_task_briefing_keys()

    print(f"  filters: {len(done_keys)} done · {len(state_keys)} in-state · "
          f"{len(recent_proposal_keys)} recent-proposals · "
          f"{len(task_briefing_keys)} in-tasks.json", file=sys.stderr)

    # 3. Filter.
    survivors: list[dict] = []
    seen_in_run: set[str] = set()
    for it in extracted:
        key = it.get("key", "")
        if not key or key in seen_in_run:
            continue
        plain = _plain(it.get("text_html", ""))
        if len(plain) < MIN_TEXT_LENGTH:
            continue
        if key in done_keys:
            continue
        if key in state_keys:
            # Already surfaced by the briefing — let that path drive it.
            continue
        if key in recent_proposal_keys:
            continue
        if key in task_briefing_keys:
            continue
        seen_in_run.add(key)
        survivors.append(it)

    survivors = survivors[:MAX_PER_RUN]
    print(f"  surviving slips → suggestions: {len(survivors)}",
          file=sys.stderr)
    if not survivors:
        return

    # 4. Append as task_proposals rows. Schema (Apps Script writer):
    #    A:date  B:title  C:key  D:urgency  E:section  F:status
    today_iso = dt.date.today().isoformat()
    rows = []
    for it in survivors:
        plain = _plain(it.get("text_html", ""))[:200]
        owner = it.get("owner", "?")
        title = f"[{owner} 1:1] {plain}"[:200]
        rows.append([
            today_iso, title, it["key"], "medium",
            f"1on1-{owner.lower()}", "pending",
        ])
        print(f"  + slip-suggest: {title[:80]}", file=sys.stderr)

    if args.dry_run:
        print(f"  [dry-run] would append {len(rows)} rows", file=sys.stderr)
        return

    if not ACK_SHEET_ID:
        print("  ACK_SHEET_ID not set; skipping write", file=sys.stderr)
        return

    _sheets(creds).spreadsheets().values().append(
        spreadsheetId=ACK_SHEET_ID, range="task_proposals!A:F",
        valueInputOption="RAW", body={"values": rows},
    ).execute()
    print(f"  wrote {len(rows)} suggestions", file=sys.stderr)


if __name__ == "__main__":
    main()
