"""Slack reply scraper — captures James's replies to the AutomatedBriefing
DM (top-level OR threaded) and turns each into a `task_proposals` row.
Runs as part of the tasks-live 2-hour cron, so anything you type in
Slack on the move appears in the live tasks dashboard's "💡 Suggested
tasks" section within ~2 hours.

Cursor tracking: the last-processed Slack message timestamp is stored
in a `slack_cursor` tab on the state sheet so we never re-import.

Usage:
    python scrape_slack_replies.py
    python scrape_slack_replies.py --dry-run

Why this design (one of many):
- No webhook endpoint to maintain — pure poll from cron.
- No LLM in the loop — the reply text becomes the task title verbatim.
  James can be terse ("call ronan about retreat") or verbose. The
  tasks-live dashboard's ✅ add confirm step gives him a chance to
  edit or dismiss before it lands in tasks.json.
- Free-form: any non-bot message in the DM channel counts. No special
  prefix or command syntax to remember.
"""

from __future__ import annotations
import argparse
import datetime as dt
import hashlib
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

from daily_briefing import (  # noqa: E402
    google_creds, _sheets, ACK_SHEET_ID, SLACK_USER_ID,
)

SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")
# Optional: if the bot lacks im:write scope (needed for conversations.open),
# set SLACK_DM_CHANNEL_ID in env to the resolved DM channel ID (DXXXXX).
# You can find it once by opening the DM in Slack web and reading the URL,
# or by running `slack-sdk users.conversations` with an admin token. After
# this env var is set, the scraper skips conversations.open and reads
# directly via the channel ID using the im:history scope the bot already has.
SLACK_DM_CHANNEL_ID = os.environ.get("SLACK_DM_CHANNEL_ID", "")
# How far back to look if no cursor exists yet (first run).
DEFAULT_LOOKBACK_HOURS = 72
# Cap per run so a flood of replies doesn't drown the proposals tab.
MAX_PER_RUN = 30


def _read_cursor(sheets) -> str:
    """Return the last-processed Slack ts (e.g. '1714612345.001'), or '' if missing."""
    try:
        resp = sheets.spreadsheets().values().get(
            spreadsheetId=ACK_SHEET_ID, range="slack_cursor!A:B"
        ).execute()
        rows = resp.get("values", [])
        if len(rows) >= 2 and len(rows[1]) >= 2:
            return rows[1][1] or ""
    except Exception:
        pass
    return ""


def _write_cursor(sheets, ts: str, dry_run: bool) -> None:
    if dry_run:
        print(f"  [dry-run] would write cursor={ts}", file=sys.stderr)
        return
    # Ensure the tab + header exist.
    try:
        sheets.spreadsheets().batchUpdate(
            spreadsheetId=ACK_SHEET_ID,
            body={"requests": [{"addSheet": {
                "properties": {"title": "slack_cursor"}
            }}]},
        ).execute()
    except Exception:
        pass
    sheets.spreadsheets().values().update(
        spreadsheetId=ACK_SHEET_ID, range="slack_cursor!A1:B2",
        valueInputOption="RAW",
        body={"values": [
            ["channel", "last_seen_ts"],
            [SLACK_USER_ID, ts],
        ]},
    ).execute()


def _resolve_dm_channel(client, user_id: str) -> str:
    """Resolve a user ID (UXXXXX) to its DM channel ID (DXXXXX).

    Priority:
      1. SLACK_DM_CHANNEL_ID env var (set by James once, no Slack API call).
      2. conversations.open(users=user_id) — idempotent, needs im:write
         scope. If the bot is missing that scope, falls through.

    Returns "" if neither path works; caller then skips the run with a
    clear message about which scope to add (one-time fix)."""
    if SLACK_DM_CHANNEL_ID:
        return SLACK_DM_CHANNEL_ID
    try:
        resp = client.conversations_open(users=user_id)
        ch = (resp.data or {}).get("channel") or {}
        return ch.get("id", "")
    except Exception as e:
        msg = str(e)
        if "missing_scope" in msg or "im:write" in msg:
            print(f"  conversations.open needs the im:write scope "
                  f"(your bot has im:history but not im:write). Two fixes: "
                  f"(a) add im:write in Slack admin + reinstall the bot, "
                  f"OR (b) set SLACK_DM_CHANNEL_ID env var to the DM "
                  f"channel ID once (look it up in Slack web URL).",
                  file=sys.stderr)
        else:
            print(f"  conversations_open({user_id}) failed: {e}",
                  file=sys.stderr)
        return ""


def _fetch_messages(client, channel: str, oldest_ts: str) -> list[dict]:
    """Pull recent messages from the DM + their thread replies.
    Returns a flat list of {ts, user, text, thread_ts} where user != bot."""
    all_msgs: list[dict] = []

    # Top-level channel history. `oldest` is exclusive in Slack API.
    try:
        cur = oldest_ts or _hours_ago(DEFAULT_LOOKBACK_HOURS)
        resp = client.conversations_history(
            channel=channel, oldest=cur, limit=200,
        )
        msgs = resp.data.get("messages", []) or []
    except Exception as e:
        print(f"  conversations_history failed: {e}", file=sys.stderr)
        return []

    bot_user = None
    try:
        bot_resp = client.auth_test()
        bot_user = (bot_resp.data or {}).get("user_id")
    except Exception:
        pass

    for m in msgs:
        # Skip bot's own messages (AutomatedBriefing posts).
        if m.get("bot_id") or (bot_user and m.get("user") == bot_user):
            continue
        # Skip system messages (joins/leaves/etc).
        if m.get("subtype") in ("channel_join", "channel_leave", "bot_message"):
            continue
        all_msgs.append({
            "ts": m.get("ts", ""),
            "user": m.get("user", ""),
            "text": (m.get("text") or "").strip(),
            "thread_ts": m.get("thread_ts", ""),
            "is_top_level": True,
        })

    # Thread replies on bot messages (where James typically replies).
    bot_msgs = [m for m in msgs if m.get("bot_id") or
                (bot_user and m.get("user") == bot_user)]
    for parent in bot_msgs:
        thread_ts = parent.get("thread_ts") or parent.get("ts")
        if not thread_ts or parent.get("reply_count", 0) == 0:
            continue
        try:
            tr = client.conversations_replies(
                channel=channel, ts=thread_ts, limit=100,
                oldest=(oldest_ts or _hours_ago(DEFAULT_LOOKBACK_HOURS)),
            )
            for m in (tr.data.get("messages", []) or []):
                if m.get("bot_id") or (bot_user and m.get("user") == bot_user):
                    continue
                if m.get("ts") == thread_ts:
                    continue
                all_msgs.append({
                    "ts": m.get("ts", ""),
                    "user": m.get("user", ""),
                    "text": (m.get("text") or "").strip(),
                    "thread_ts": thread_ts,
                    "is_top_level": False,
                })
        except Exception as e:
            print(f"  conversations_replies({thread_ts}) failed: {e}",
                  file=sys.stderr)

    # Dedup + filter: drop anything ≤ oldest_ts, sort ascending.
    seen_ts: set[str] = set()
    cleaned: list[dict] = []
    for m in sorted(all_msgs, key=lambda x: x.get("ts", "")):
        ts = m.get("ts", "")
        if not ts or ts in seen_ts:
            continue
        if oldest_ts and ts <= oldest_ts:
            continue
        if not m.get("text"):
            continue
        seen_ts.add(ts)
        cleaned.append(m)
    return cleaned


def _hours_ago(hours: int) -> str:
    """Slack ts format: 'seconds.microseconds' since epoch."""
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours)
    return f"{int(cutoff.timestamp())}.000"


def _proposal_key(text: str, ts: str) -> str:
    """Stable 12-char key for the proposal — used by the live dashboard's
    accept/reject buttons. Includes ts to avoid collisions on dup text."""
    norm = (text + "|" + ts).lower().strip()
    return hashlib.sha1(norm.encode()).hexdigest()[:12]


def _append_proposals(sheets, new_rows: list[list], dry_run: bool) -> None:
    if not new_rows:
        return
    if dry_run:
        for r in new_rows:
            print(f"  [dry-run] proposal: {r[1][:80]}", file=sys.stderr)
        return
    sheets.spreadsheets().values().append(
        spreadsheetId=ACK_SHEET_ID, range="task_proposals!A:F",
        valueInputOption="RAW", body={"values": new_rows},
    ).execute()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if not SLACK_BOT_TOKEN:
        print("  SLACK_BOT_TOKEN not set; skipping Slack scrape",
              file=sys.stderr)
        return
    if not ACK_SHEET_ID:
        print("  ACK_SHEET_ID not set; skipping", file=sys.stderr)
        return

    from slack_sdk import WebClient  # imported lazily for clean --help
    client = WebClient(token=SLACK_BOT_TOKEN)
    creds = google_creds()
    sheets = _sheets(creds)

    # SLACK_USER_ID is James's user ID (UXXXXX). conversations.history
    # needs the DM channel ID (DXXXXX) — resolve once per run.
    dm_channel = _resolve_dm_channel(client, SLACK_USER_ID)
    if not dm_channel:
        print(f"  couldn't resolve DM channel for {SLACK_USER_ID}; "
              f"aborting", file=sys.stderr)
        return
    print(f"  dm channel: {dm_channel}", file=sys.stderr)

    cursor = _read_cursor(sheets)
    print(f"  slack cursor: {cursor or '(first run — '
          f'looking back {DEFAULT_LOOKBACK_HOURS}h)'}", file=sys.stderr)

    msgs = _fetch_messages(client, dm_channel, cursor)
    if not msgs:
        print("  no new replies from James", file=sys.stderr)
        return

    print(f"  found {len(msgs)} new message(s) from James", file=sys.stderr)
    msgs = msgs[:MAX_PER_RUN]

    today_iso = dt.date.today().isoformat()
    rows = []
    for m in msgs:
        title = m["text"][:200]
        key = _proposal_key(title, m["ts"])
        # Apps Script's task_proposals schema:
        # A:date  B:title  C:key  D:urgency  E:section  F:status
        rows.append([
            today_iso, title, key, "medium",
            "slack-reply", "pending",
        ])
        print(f"  + slack-suggest: {title[:80]}", file=sys.stderr)

    _append_proposals(sheets, rows, args.dry_run)
    # Advance cursor to the latest ts even if we capped at MAX_PER_RUN —
    # the unprocessed tail would otherwise be picked up next run, but
    # they're capped because we got flooded; better to lose-by-cap than
    # spin forever.
    latest_ts = msgs[-1]["ts"]
    _write_cursor(sheets, latest_ts, args.dry_run)
    print(f"  cursor advanced to {latest_ts}", file=sys.stderr)


if __name__ == "__main__":
    main()
