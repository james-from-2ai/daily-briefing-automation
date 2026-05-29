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


def _proposal_action_url(proposal_key: str, action: str) -> str:
    """Webhook URL the JS click handler hits to mark a suggestion as
    accepted ('add') or rejected ('dismiss'). Uses the existing
    acks-route convention with prefixed keys so no Apps Script change
    is needed — sync_feedback_to_tasks parses the prefix on its next run."""
    if not ACK_WEBHOOK_URL:
        return "#"
    prefix = {"add": "accept", "dismiss": "reject"}.get(action, "reject")
    q = {
        "keys": f"{prefix}:{proposal_key}",
        "date": dt.date.today().isoformat(),
        "kind": f"proposal_{action}",
        "source": "tasks-live",
    }
    return f"{ACK_WEBHOOK_URL}?{urllib.parse.urlencode(q)}"


def _render_proposals(proposals: list[dict]) -> str:
    pending = [p for p in proposals
               if (p.get("status") or "").lower() in ("", "pending")]
    if not pending:
        return ""
    items = []
    for p in pending:
        proposal_key = _esc((p.get("key") or "").strip())
        if not proposal_key:
            continue  # can't act without a key
        title = _esc(p.get("title", "")[:200])
        urgency = _esc((p.get("urgency") or "medium"))
        section = _esc(p.get("section", "?"))
        date = _esc((p.get("date") or "")[:10])
        items.append(f'''
<div class="suggestion-card" data-proposal-key="{proposal_key}" style="
    background:#fff; border:1px solid #fde68a; border-left:4px solid #f59e0b;
    border-radius:4px; padding:10px 14px; margin:8px 0;">
  <div style="font-weight:600;color:#111827;">{title}</div>
  <div style="font-size:11.5px;color:#6b7280;margin-top:3px;">
    {urgency} · from {section} on {date}
  </div>
  <div style="margin-top:8px;display:flex;gap:10px;">
    <a href="#" class="suggest-add"
       data-webhook-url="{_proposal_action_url(proposal_key, "add")}"
       style="color:#15803d;text-decoration:none;font-weight:600;font-size:12.5px;
              border:1px solid #15803d;padding:3px 10px;border-radius:4px;
              cursor:pointer;">✅ add to tasks</a>
    <a href="#" class="suggest-dismiss"
       data-webhook-url="{_proposal_action_url(proposal_key, "dismiss")}"
       style="color:#6b7280;text-decoration:none;font-size:12.5px;
              border:1px solid #d1d5db;padding:3px 10px;border-radius:4px;
              cursor:pointer;">✕ dismiss</a>
  </div>
</div>''')
    if not items:
        return ""
    return f'''
<section style="background:#fffbeb;border-radius:8px;padding:14px 18px;
         margin:18px 0;">
  <h2 style="margin:0 0 4px 0;color:#92400e;font-size:16px;">
    💡 Suggested tasks — your approval needed
  </h2>
  <p style="color:#92400e;font-size:11.5px;margin:0 0 10px 0;">
    Sent from your briefing dashboard's 📌. Nothing here goes into tasks.json
    until you click ✅ add. Dismissing keeps tasks.json untouched too.
  </p>
  {"".join(items)}
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

    # Build-stamp for cache-busting verification. If James sees this
    # ID on the page, he's on the freshly-deployed version, not a
    # stale browser-cached copy.
    build_id = now.strftime("%Y%m%d-%H%M%S") + "Z"
    return f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate">
<meta http-equiv="Pragma" content="no-cache">
<meta http-equiv="Expires" content="0">
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
  /* Inline state changes applied client-side on button click. */
  .task-card.is-done {{ opacity: 0.55; }}
  .task-card.is-done .task-title,
  .task-card.is-done .task-why {{ text-decoration: line-through; }}
  .task-card.is-done .mark-done {{ pointer-events: none; opacity: 0.5; }}
  .suggestion-card.is-confirmed {{
      border-left-color: #15803d !important; background: #f0fdf4 !important;
  }}
  .suggestion-card.is-confirmed .suggest-add,
  .suggestion-card.is-confirmed .suggest-dismiss {{
      pointer-events: none; opacity: 0.4;
  }}
  .suggestion-card.is-dismissed {{ opacity: 0.4; }}
  .suggestion-card.is-dismissed .suggest-add,
  .suggestion-card.is-dismissed .suggest-dismiss {{
      pointer-events: none;
  }}
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
    <br><span style="font-family:monospace;">build {build_id}</span>
    · inline-mark-done JS active
  </footer>
  <div id="toast"></div>
  <script>
    // All interactive buttons use the same pattern: e.preventDefault()
    // so no new tab opens, fire the webhook silently via no-cors fetch,
    // update the UI inline, show a toast. Next cron syncs tasks.json.
    (function () {{
      function toast(msg, isErr) {{
        var t = document.getElementById('toast');
        if (!t) return;
        t.textContent = msg;
        t.className = isErr ? 'show err' : 'show';
        setTimeout(function () {{ t.className = ''; }}, 2400);
      }}

      function fireSilently(url) {{
        return fetch(url, {{ mode: 'no-cors', credentials: 'omit' }});
      }}

      function wire(selector, handler) {{
        document.querySelectorAll(selector).forEach(function (a) {{
          a.addEventListener('click', function (e) {{
            e.preventDefault();
            var url = a.getAttribute('data-webhook-url');
            if (!url || url === '#') {{
              toast('No webhook configured', true);
              return;
            }}
            handler(a, url);
          }});
        }});
      }}

      // Active task → ✅ mark done.
      wire('a.mark-done', function (a, url) {{
        var card = a.closest('.task-card');
        if (card) card.classList.add('is-done');
        toast('✅ marked done — syncs to tasks.json next refresh');
        fireSilently(url).catch(function () {{
          toast('Webhook may have failed — check next refresh', true);
          if (card) card.classList.remove('is-done');
        }});
      }});

      // Suggested task → ✅ add to tasks.json.
      wire('a.suggest-add', function (a, url) {{
        var card = a.closest('.suggestion-card');
        if (card) card.classList.add('is-confirmed');
        toast('✅ added — appears in tasks.json on next refresh');
        fireSilently(url).catch(function () {{
          toast('Webhook may have failed — check next refresh', true);
          if (card) card.classList.remove('is-confirmed');
        }});
      }});

      // Suggested task → ✕ dismiss (tasks.json NOT touched).
      wire('a.suggest-dismiss', function (a, url) {{
        var card = a.closest('.suggestion-card');
        if (card) card.classList.add('is-dismissed');
        toast('✕ dismissed — stays out of tasks.json');
        fireSilently(url).catch(function () {{
          toast('Webhook may have failed — check next refresh', true);
          if (card) card.classList.remove('is-dismissed');
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
