"""Build the full Drive change-log expand page and return its URL.

The briefing's "📁 Shared Drive activity" section is a capped summary (top
6 editors, 5 docs each, 7 days). This produces the *comprehensive* version
James can click into: a longer window, every editor, every file they
touched, grouped and timestamped. Deterministic — no agent, no LLM.

Writes the page via publish_extra_page.publish_page and prints its URL to
stdout so the skill can link to it from the Drive-activity section.

Usage:
    python build_drive_log.py [--days 14] [--max-files-per-editor 60]
"""

from __future__ import annotations
import argparse
import datetime as dt
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

from daily_briefing import google_creds, pull_drive_audit  # noqa: E402
from publish_extra_page import publish_page  # noqa: E402

FULL_LOG_DEFAULT_DAYS = 14
MAX_FILES_PER_EDITOR = 60  # safety cap so one prolific editor can't bloat it


def _mime_icon(mime: str) -> str:
    if "spreadsheet" in mime:
        return " 📊"
    if "presentation" in mime:
        return " 📽"
    if "document" in mime:
        return " 📄"
    if "folder" in mime:
        return " 📁"
    return ""


def render_full_drive_log(audit: dict, max_per_editor: int) -> str:
    by_editor = audit.get("by_editor") or {}
    lookback = audit.get("lookback_days", FULL_LOG_DEFAULT_DAYS)
    if not by_editor:
        return (f"<p><em>No Drive activity in the last {lookback} days.</em></p>")

    # Rank editors by file count desc, then most-recent edit.
    def sort_key(item):
        edits = item[1]
        latest = max((e.get("modifiedTime", "") for e in edits), default="")
        return (-len(edits), latest)
    ranked = sorted(by_editor.items(), key=sort_key)

    total_files = audit.get("total_files", 0)
    parts = [
        f"<h2>📁 Full Drive change log — last {lookback} days</h2>",
        f'<p style="font-size:12.5px;color:#6b7280;">Every stakeholder, every '
        f'file touched in the window ({total_files} files, {len(ranked)} '
        f'editors). Capped at {max_per_editor} files per editor.</p>',
    ]
    for editor, edits in ranked:
        edits_sorted = sorted(edits, key=lambda e: e.get("modifiedTime", ""),
                              reverse=True)[:max_per_editor]
        more = len(edits) - len(edits_sorted)
        parts.append(
            f'<h3>{re.sub(r"<", "&lt;", editor)} '
            f'<span style="color:#6b7280;font-size:11px;font-weight:400;">· '
            f'{len(edits)} file{"s" if len(edits) != 1 else ""}</span></h3><ul>'
        )
        for e in edits_sorted:
            mt = (e.get("modifiedTime") or "")[:16].replace("T", " ")
            link = e.get("webViewLink") or "#"
            name = re.sub(r"<", "&lt;", e.get("name", "(no name)"))
            icon = _mime_icon(e.get("mimeType", ""))
            parts.append(
                f'<li><a href="{link}">{name}</a>{icon} '
                f'<span style="color:#9ca3af;font-size:11px;">· {mt}</span></li>'
            )
        if more > 0:
            parts.append(f'<li style="color:#9ca3af;">…and {more} more</li>')
        parts.append("</ul>")
    return "\n".join(parts)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=FULL_LOG_DEFAULT_DAYS)
    parser.add_argument("--max-files-per-editor", type=int,
                        default=MAX_FILES_PER_EDITOR)
    args = parser.parse_args()

    creds = google_creds()
    audit = pull_drive_audit(creds, lookback_days=args.days)
    fragment = render_full_drive_log(audit, args.max_files_per_editor)
    url = publish_page(f"Full Drive change log", "drive-log", fragment)
    print(url)
    print(f"[drive-log] {audit.get('total_files', 0)} files, "
          f"{len(audit.get('by_editor') or {})} editors → {url}",
          file=sys.stderr)


if __name__ == "__main__":
    main()
