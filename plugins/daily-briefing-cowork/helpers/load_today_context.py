"""Load everything the followup skill needs to know about today's briefing:
the state-sheet items for today, recent acks/votes/comments/task_proposals,
the cowork tasks.json, and today's dashboard URL.

Usage:
    python load_today_context.py --out /tmp/today-context.json
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
    google_creds, read_state, read_acks, read_votes,
    pull_tasks_json, _sheets, ACK_SHEET_ID, GITHUB_PAGES_BASE,
)

DOCS_DIR = REPO_ROOT / "docs"


def _read_tab(creds, tab: str) -> list[dict]:
    """Generic Sheets read for tabs daily_briefing.py doesn't expose."""
    if not ACK_SHEET_ID:
        return []
    try:
        resp = _sheets(creds).spreadsheets().values().get(
            spreadsheetId=ACK_SHEET_ID, range=f"{tab}!A:Z").execute()
    except Exception as e:
        print(f"  couldn't read {tab}: {e}", file=sys.stderr)
        return []
    rows = resp.get("values", [])
    if not rows:
        return []
    header = rows[0]
    return [dict(zip(header, r + [""] * (len(header) - len(r)))) for r in rows[1:]]


def _today_dashboard_url(today: dt.date) -> str | None:
    """Find today's dashboard file in docs/ and return its public URL.
    Returns None if no dashboard for today exists yet."""
    if not DOCS_DIR.exists():
        return None
    prefix = today.isoformat()
    matches = sorted(DOCS_DIR.glob(f"{prefix}-*.html"),
                     key=lambda p: p.stat().st_mtime, reverse=True)
    if not matches:
        return None
    return f"{GITHUB_PAGES_BASE}/{matches[0].name}"


def _filter_today(rows: list[dict], today: dt.date, date_field: str) -> list[dict]:
    iso = today.isoformat()
    return [r for r in rows if (r.get(date_field) or "")[:10] == iso]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    today = dt.date.today()
    creds = google_creds()

    full_state = read_state(creds)
    state_today = [r for r in full_state
                   if (r.get("last_seen") or "")[:10] == today.isoformat()]

    acks = read_acks(creds)
    votes = read_votes(creds)
    comments = _read_tab(creds, "comments")
    task_proposals = _read_tab(creds, "task_proposals")

    context = {
        "today": today.isoformat(),
        "dashboard_url": _today_dashboard_url(today),
        "state_today": state_today,
        "state_open_carryover": [
            r for r in full_state
            if r.get("status") == "open"
            and not r.get("acknowledged_on")
            and (r.get("last_seen") or "")[:10] != today.isoformat()
        ],
        "acks_today": _filter_today(acks, today, "briefing_date"),
        "votes_today": _filter_today(votes, today, "date"),
        "comments_today": _filter_today(comments, today, "date"),
        "task_proposals_today": _filter_today(task_proposals, today, "date"),
        "cowork_tasks": pull_tasks_json(),
    }

    Path(args.out).write_text(
        json.dumps(context, indent=2, default=str), encoding="utf-8",
    )
    print(f"Wrote {args.out}", file=sys.stderr)
    print(f"  state today: {len(context['state_today'])} items", file=sys.stderr)
    print(f"  carryover open: {len(context['state_open_carryover'])} items",
          file=sys.stderr)
    print(f"  acks today: {len(context['acks_today'])}", file=sys.stderr)
    print(f"  votes today: {len(context['votes_today'])}", file=sys.stderr)
    print(f"  comments today: {len(context['comments_today'])}", file=sys.stderr)
    print(f"  task_proposals today: {len(context['task_proposals_today'])}",
          file=sys.stderr)
    print(f"  cowork tasks: {len(context['cowork_tasks'])}", file=sys.stderr)
    print(f"  dashboard: {context['dashboard_url']}", file=sys.stderr)


if __name__ == "__main__":
    main()
