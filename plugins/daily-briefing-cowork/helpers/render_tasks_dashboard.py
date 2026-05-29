"""Render docs/tasks-live.html — the live tasks dashboard. Pure
deterministic rendering: reads tasks.json + briefing acks/proposals,
emits a stable-URL HTML page with interactive widgets that talk to the
existing Apps Script webhook (✅ mark done, ✕ dismiss).

Usage:
    python render_tasks_dashboard.py
    python render_tasks_dashboard.py --out /tmp/tasks-live.html
"""

from __future__ import annotations
import argparse
import datetime as dt
import html as html_escape
import json
import re
import sys
import urllib.parse
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

from daily_briefing import (  # noqa: E402
    TASKS_JSON_PATH, ACK_WEBHOOK_URL, GITHUB_PAGES_BASE,
)


URGENCY_ORDER = {"urgent": 0, "high": 1, "medium": 2, "low": 3}
URGENCY_COLOR = {
    "urgent": ("#dc2626", "#fef2f2"),   # red
    "high":   ("#ea580c", "#fff7ed"),   # orange
    "medium": ("#0e7490", "#ecfeff"),   # cyan
    "low":    ("#6b7280", "#f9fafb"),   # gray
}


def _esc(s: str) -> str:
    return html_escape.escape(s or "", quote=True)


def _ack_url(task: dict) -> str:
    """Webhook URL that marks this task done via the existing ack route.
    Sends the task's briefing_key if present (so the briefing's
    apply_acks_to_state also picks it up), else the task id."""
    if not ACK_WEBHOOK_URL:
        return "#"
    key = task.get("briefing_key") or task.get("id", "")
    q = {
        "keys": key,
        "date": dt.date.today().isoformat(),
        "kind": "task_done",
        "source": "tasks-live",
    }
    return f"{ACK_WEBHOOK_URL}?{urllib.parse.urlencode(q)}"


def _render_task(task: dict) -> str:
    urg = (task.get("urgency") or "medium").lower()
    border, bg = URGENCY_COLOR.get(urg, URGENCY_COLOR["medium"])
    title = _esc(task.get("title", "(no title)"))
    why = _esc(task.get("why", ""))
    domain = _esc(task.get("domain", ""))
    added = _esc((task.get("added") or "")[:10])
    blocked_by = task.get("blocked_by") or ""
    blocked_label = (
        f'<span style="background:#fef3c7;color:#92400e;padding:1px 6px;'
        f'border-radius:8px;font-size:10px;margin-left:6px;">⛔ blocked: '
        f'{_esc(str(blocked_by))[:40]}</span>'
        if blocked_by else ""
    )
    age_days = ""
    try:
        added_dt = dt.datetime.fromisoformat(
            (task.get("added") or "").replace("Z", "+00:00")
        )
        delta = (dt.datetime.now(dt.timezone.utc) - added_dt).days
        if delta > 0:
            age_days = (
                f'<span style="color:#6b7280;font-size:11px;'
                f'margin-left:8px;">{delta}d old</span>'
            )
    except (ValueError, AttributeError):
        pass

    return f'''
<div id="task-{_esc(task.get('id', ''))}" class="task-card" data-task-id="{_esc(task.get('id', ''))}" style="
    border-left:4px solid {border}; background:{bg};
    padding:10px 14px; margin:8px 0; border-radius:4px;
    font-size:14px; line-height:1.45;">
  <div style="display:flex;align-items:baseline;justify-content:space-between;gap:12px;">
    <div class="task-title" style="font-weight:600;color:#111827;flex:1;">{title}</div>
    <div style="font-size:11px;color:{border};text-transform:uppercase;
         letter-spacing:0.7px;font-weight:700;">{_esc(urg)}</div>
  </div>
  <div class="task-why" style="color:#374151;font-size:12.5px;margin-top:4px;">{why}</div>
  <div style="margin-top:8px;font-size:11.5px;color:#6b7280;">
    <span>{_esc(domain)}</span>
    {age_days}
    {blocked_label}
    <a href="#" class="mark-done"
       data-webhook-url="{_ack_url(task)}"
       style="float:right;color:{border};text-decoration:none;
              border-bottom:1px dotted {border};font-weight:500;cursor:pointer;">
      ✅ mark done
    </a>
  </div>
</div>
'''


def _render_proposals(proposals: list[dict]) -> str:
    pending = [p for p in proposals
               if (p.get("status") or "").lower() == "pending"]
    if not pending:
        return ""
    items = []
    for p in pending:
        items.append(f'''
<li style="margin:6px 0;">
  <strong>{_esc(p.get("title", "")[:200])}</strong>
  <span style="color:#6b7280;font-size:11px;">
    · {_esc(p.get("urgency", "medium"))}
    · added {_esc(p.get("date", "")[:10])}
  </span>
  <div style="color:#92400e;font-size:11.5px;margin-top:2px;">
    ⏳ awaiting next 2-hour sync to promote into tasks.json
  </div>
</li>''')
    return f'''
<section style="background:#fffbeb;border:1px solid #fde68a;border-radius:8px;
         padding:14px 18px;margin:18px 0;">
  <h2 style="margin:0 0 8px 0;color:#92400e;font-size:16px;">
    ⏳ Pending — sent from briefing, not yet promoted
  </h2>
  <ul style="margin:0;padding-left:20px;">{"".join(items)}</ul>
</section>
'''


def _render_recently_done(completed: list[dict], days: int = 7) -> str:
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)
    recent = []
    for t in completed[-200:]:
        try:
            done_at = dt.datetime.fromisoformat(
                (t.get("completed_at") or t.get("updated") or "")
                .replace("Z", "+00:00")
            )
            if done_at >= cutoff:
                recent.append((done_at, t))
        except (ValueError, AttributeError):
            continue
    if not recent:
        return ""
    recent.sort(key=lambda x: x[0], reverse=True)
    items = []
    for done_at, t in recent[:30]:
        items.append(
            f'<li style="color:#6b7280;text-decoration:line-through;'
            f'margin:3px 0;font-size:13px;">{_esc(t.get("title", ""))} '
            f'<span style="font-size:11px;color:#9ca3af;">'
            f'· {done_at.strftime("%a %b %d")}</span></li>'
        )
    return f'''
<details style="margin:18px 0;">
  <summary style="cursor:pointer;color:#374151;font-weight:600;font-size:14px;
           padding:6px 0;">Recently done ({len(recent)} in last {days}d)</summary>
  <ul style="margin:8px 0;padding-left:22px;">{"".join(items)}</ul>
</details>
'''


def _read_proposals_for_render(creds) -> list[dict]:
    """Cheap read of task_proposals — only the pending ones are
    surfaced; we don't need history for rendering."""
    if not ACK_WEBHOOK_URL:
        return []
    try:
        from daily_briefing import _sheets, ACK_SHEET_ID
        resp = _sheets(creds).spreadsheets().values().get(
            spreadsheetId=ACK_SHEET_ID, range="task_proposals!A:Z"
        ).execute()
    except Exception:
        return []
    rows = resp.get("values", [])
    if len(rows) < 2:
        return []
    header = rows[0]
    return [dict(zip(header, r + [""] * (len(header) - len(r))))
            for r in rows[1:]]


def render(tasks_data: dict, proposals: list[dict]) -> str:
    active = list(tasks_data.get("tasks", []))
    active.sort(key=lambda t: (URGENCY_ORDER.get(
        (t.get("urgency") or "medium").lower(), 9), t.get("added", "")))

    now = dt.datetime.now(dt.timezone.utc)
    now_local = dt.datetime.now()

    # Group by urgency for visual hierarchy.
    sections_html = []
    for urg in ["urgent", "high", "medium", "low"]:
        group = [t for t in active
                 if (t.get("urgency") or "medium").lower() == urg]
        if not group:
            continue
        border, _ = URGENCY_COLOR[urg]
        sections_html.append(
            f'<h3 style="color:{border};font-size:13px;'
            f'text-transform:uppercase;letter-spacing:0.8px;margin:18px 0 4px;">'
            f'{urg.title()} ({len(group)})</h3>'
        )
        sections_html.extend(_render_task(t) for t in group)

    no_tasks = (
        '<p style="color:#6b7280;font-size:14px;text-align:center;'
        'padding:24px 0;">No active tasks. 🎉</p>'
        if not active else ""
    )

    return f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>Live tasks — {len(active)} active</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica,
          Arial, sans-serif; max-width: 760px; margin: 0 auto;
          padding: 24px 18px 80px; color: #111827; background: #fafafa; }}
  h1 {{ font-size: 22px; margin: 0 0 4px; letter-spacing: -0.3px; }}
  .meta {{ color: #6b7280; font-size: 12.5px; margin-bottom: 14px; }}
  a {{ color: #0e7490; }}
  details > summary {{ list-style: none; }}
  details > summary::-webkit-details-marker {{ display: none; }}
  /* Inline-done state: applied client-side when "mark done" is clicked. */
  .task-card.is-done {{ opacity: 0.55; }}
  .task-card.is-done .task-title,
  .task-card.is-done .task-why {{ text-decoration: line-through; }}
  .task-card.is-done .mark-done {{ pointer-events: none; opacity: 0.5; }}
  #toast {{ position: fixed; bottom: 24px; left: 50%; transform: translateX(-50%);
            background: #111827; color: white; padding: 10px 18px;
            border-radius: 999px; font-size: 13px; opacity: 0;
            transition: opacity 0.2s; pointer-events: none; z-index: 9999; }}
  #toast.show {{ opacity: 1; }}
  #toast.err  {{ background: #b91c1c; }}
</style>
</head>
<body>
  <h1>📋 Live tasks</h1>
  <div class="meta">
    {len(active)} active · last refreshed {now_local.strftime("%a %b %d %I:%M %p")}
    · <a href="#" onclick="location.reload();return false;">refresh</a>
    · <a href="https://github.com/james-from-2ai/daily-briefing-automation">repo</a>
  </div>
  {_render_proposals(proposals)}
  <main>
    {no_tasks}
    {"".join(sections_html)}
  </main>
  {_render_recently_done(tasks_data.get("completed", []))}
  <footer style="margin-top:32px;padding-top:12px;border-top:1px solid #e5e7eb;
          color:#9ca3af;font-size:11px;text-align:center;">
    Auto-refreshes every 2 hours from local tasks.json +
    briefing dashboard feedback. Source of truth: tasks.json
    ({_esc(str(TASKS_JSON_PATH))[:80]}…).
  </footer>
  <div id="toast"></div>
  <script>
    // Intercept "mark done" clicks: fire the Apps Script webhook silently
    // in the background (no-cors fetch, no response needed), mark the
    // task visually done on the page. Never opens a new tab.
    // Next 2-hour cron picks up the ack and moves the task to completed
    // in tasks.json.
    (function () {{
      function toast(msg, isErr) {{
        var t = document.getElementById('toast');
        if (!t) return;
        t.textContent = msg;
        t.className = isErr ? 'show err' : 'show';
        setTimeout(function () {{ t.className = ''; }}, 2400);
      }}
      document.querySelectorAll('a.mark-done').forEach(function (a) {{
        a.addEventListener('click', function (e) {{
          e.preventDefault();
          var url = a.getAttribute('data-webhook-url');
          if (!url || url === '#') {{
            toast('No webhook configured', true);
            return;
          }}
          var card = a.closest('.task-card');
          // Immediate visual feedback — don't wait for the fetch.
          if (card) card.classList.add('is-done');
          toast('✅ marked done — syncs to tasks.json next refresh');
          // Fire and forget (no-cors so the Apps Script auto-close page
          // doesn't even render; we never read the response).
          fetch(url, {{ mode: 'no-cors', credentials: 'omit' }}).catch(function () {{
            toast('Webhook may have failed — check next refresh', true);
            if (card) card.classList.remove('is-done');
          }});
        }});
      }});
    }})();
  </script>
</body>
</html>
'''


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=None,
                        help="Output path (default: docs/tasks-live.html)")
    args = parser.parse_args()

    out_path = (Path(args.out) if args.out
                else REPO_ROOT / "docs" / "tasks-live.html")
    out_path.parent.mkdir(exist_ok=True)

    if not TASKS_JSON_PATH.exists():
        print(f"  tasks.json missing at {TASKS_JSON_PATH}", file=sys.stderr)
        tasks_data = {"tasks": [], "completed": []}
    else:
        tasks_data = json.loads(TASKS_JSON_PATH.read_text(encoding="utf-8"))

    try:
        creds = google_creds()
        proposals = _read_proposals_for_render(creds)
    except Exception as e:
        print(f"  couldn't read proposals (continuing without): {e}",
              file=sys.stderr)
        proposals = []

    html = render(tasks_data, proposals)
    out_path.write_text(html, encoding="utf-8")
    public_url = f"{GITHUB_PAGES_BASE}/{out_path.name}"
    print(f"  wrote {out_path}  ({len(html)} bytes)", file=sys.stderr)
    print(f"  public URL → {public_url}", file=sys.stderr)


# imported lazily by main to keep --help fast
from daily_briefing import google_creds  # noqa: E402


if __name__ == "__main__":
    main()
