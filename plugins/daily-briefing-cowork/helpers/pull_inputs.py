"""Thin CLI wrapper: pull every input the daily-briefing skill needs and
emit one big JSON object. Imports from daily_briefing.py so the I/O logic
stays in one place; the cowork skill is just the orchestrator.

Usage:
    python pull_inputs.py --out /tmp/briefing-inputs.json

TODO before production:
  - Wire up every pull_* function from daily_briefing.py (see imports below)
  - Decide which inputs to include vs. defer (e.g., should we skip funder
    pull entirely on odd-ordinal days to save the helper a Drive query)
  - Add structured error handling — if Calendar API 429s, fail loudly with
    a useful message so the skill knows to back off and retry
"""

from __future__ import annotations
import argparse
import datetime as dt
import json
import sys
from pathlib import Path

# Make the parent repo importable so we can reuse daily_briefing.py's
# pull functions without duplication.
REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

# These imports are the SAME pull functions the Python version uses —
# OAuth, sheet reads, calendar API, etc. We're just calling them from
# the agent's orchestration layer instead of from main().
import re  # noqa: E402

from daily_briefing import (  # noqa: E402
    google_creds, pull_calendar, pull_drive_recent, pull_1on1_recent_entries,
    pull_inbox_signals, pull_news_topics_sheet, pull_recent_feedback,
    pull_tasks_json, pull_journal_recent, pull_weather, pull_stocks,
    pull_program_area_corpus, read_state, read_acks, read_votes,
    read_user_sources, apply_acks_to_state,
    ONEONONE_DOCS, FUNDER_WATCHLIST, PEER_PUBLISHERS, EVIDENCE_STREAMS,
)


def _recently_dismissed(state: list[dict], acks: list[dict]) -> list[dict]:
    """Items James has marked done / acknowledged — the agent must NOT
    re-surface these unless the underlying situation has materially changed.
    Returns compact {section, text, status} dicts."""
    applied = apply_acks_to_state([dict(r) for r in state], acks)
    out = []
    for r in applied:
        if r.get("status") == "done" or r.get("acknowledged_on"):
            plain = re.sub(r"\s+", " ",
                           re.sub(r"<[^>]+>", " ", r.get("text_html", ""))).strip()
            if plain:
                out.append({
                    "section": r.get("section", ""),
                    "text": plain[:200],
                    "status": "done" if r.get("status") == "done" else "acknowledged",
                })
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True,
                        help="Path to write the inputs JSON")
    args = parser.parse_args()

    today = dt.date.today()
    creds = google_creds()

    state = read_state(creds)
    acks = read_acks(creds)

    # TODO: this is a stub assembly. Real version needs error handling +
    # graceful degradation (one failed pull shouldn't sink the whole run).
    inputs = {
        "today": today.isoformat(),
        "weekday": today.weekday(),
        "is_funder_day": today.toordinal() % 2 == 0,
        "calendar": pull_calendar(creds, today),
        "drive": pull_drive_recent(creds),
        "oneonones": {name: pull_1on1_recent_entries(creds, fid)
                      for name, fid in ONEONONE_DOCS.items()},
        "inbox_signals": pull_inbox_signals(creds),
        "news_topics_text": pull_news_topics_sheet(creds),
        "recent_feedback": pull_recent_feedback(creds),
        "state": state,
        "acks": acks,
        # Items James has already dismissed/acknowledged — the agent must
        # not re-surface these unless the situation has materially changed.
        "recently_dismissed": _recently_dismissed(state, acks),
        "votes": read_votes(creds),
        "user_sources": read_user_sources(creds),
        "tasks_json": pull_tasks_json(),
        "journal_recent": pull_journal_recent(),
        "weather": pull_weather(),
        "stocks": pull_stocks(),
        "program_corpus": pull_program_area_corpus(creds),
        # Constants the agent needs but doesn't have to re-derive:
        "funder_watchlist": FUNDER_WATCHLIST,
        "peer_publishers": PEER_PUBLISHERS,
        "evidence_streams": EVIDENCE_STREAMS,
    }

    Path(args.out).write_text(
        json.dumps(inputs, indent=2, default=str), encoding="utf-8",
    )
    print(f"Wrote {args.out}", file=sys.stderr)
    print(f"  calendar: {len(inputs['calendar'])} events", file=sys.stderr)
    print(f"  drive: {len(inputs['drive'])} files", file=sys.stderr)
    print(f"  inbox: {len(inputs['inbox_signals'])} threads", file=sys.stderr)
    print(f"  state: {len(inputs['state'])} rows", file=sys.stderr)


if __name__ == "__main__":
    main()
