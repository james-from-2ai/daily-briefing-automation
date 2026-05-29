"""Render the email HTML + interactive dashboard HTML from per-section
files the agent has written. Wraps render_html + make_interactive_dashboard
+ verify_urls from daily_briefing.py.

Usage:
    python render_artifacts.py \\
      --tldr "..." --prioritization-file ... --inbox-file ... \\
      [more --*-file flags] \\
      --out-email /tmp/briefing-email.html \\
      --out-dashboard /tmp/briefing-dashboard.html \\
      --dashboard-url-out /tmp/dashboard-url.txt

TODO before production:
  - Implement arg parsing for all section files
  - Read widgets_html and carryover_html from intermediate files
  - Wire dashboard_url computation + propagate to skill via stdout/--dashboard-url-out
"""

from __future__ import annotations
import argparse
import datetime as dt
import sys
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

from daily_briefing import (  # noqa: E402
    render_html, make_interactive_dashboard, verify_urls, save_dashboard,
    redirect_webhooks_to_dashboard,
    ACK_WEBHOOK_URL, GITHUB_PAGES_BASE,
)


def _read_or_empty(path: str | None) -> str:
    if not path:
        return ""
    p = Path(path)
    return p.read_text(encoding="utf-8") if p.exists() else ""


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tldr", default="")
    parser.add_argument("--tldr-file", default=None,
                        help="Read TL;DR from file instead of --tldr (avoids "
                             "shell-quoting issues on long strings)")
    parser.add_argument("--widgets-file", default=None)
    parser.add_argument("--carryover-file", default=None)
    parser.add_argument("--prioritization-file", required=True)
    parser.add_argument("--inbox-file", default=None)
    parser.add_argument("--funder-file", default=None)
    parser.add_argument("--news-file", default=None)
    parser.add_argument("--ideas-file", default=None)
    parser.add_argument("--sources-today-file", default=None)
    parser.add_argument("--evidence-file", default=None)
    parser.add_argument("--whitespace-file", default=None)
    parser.add_argument("--trends-file", default=None)
    parser.add_argument("--publisher-file", default=None)
    parser.add_argument("--sources-file", default=None)
    parser.add_argument("--out-email", required=True)
    parser.add_argument("--out-dashboard", required=True)
    parser.add_argument("--dashboard-url-out", required=True)
    args = parser.parse_args()

    today = dt.date.today()
    dashboard_slug = f"{today.isoformat()}-{uuid.uuid4().hex[:16]}"
    dashboard_url = f"{GITHUB_PAGES_BASE}/{dashboard_slug}.html"

    tldr = args.tldr
    if args.tldr_file:
        p = Path(args.tldr_file)
        if p.exists():
            tldr = p.read_text(encoding="utf-8").strip()

    email_html = render_html(
        today=today,
        prioritization=_read_or_empty(args.prioritization_file),
        news=_read_or_empty(args.news_file),
        whitespace=_read_or_empty(args.whitespace_file),
        inbox=_read_or_empty(args.inbox_file),
        funder=_read_or_empty(args.funder_file),
        carryover_html=_read_or_empty(args.carryover_file),
        trends=_read_or_empty(args.trends_file),
        sources=_read_or_empty(args.sources_file),
        publisher_landscape=_read_or_empty(args.publisher_file),
        evidence=_read_or_empty(args.evidence_file),
        tldr=tldr,
        widgets_html=_read_or_empty(args.widgets_file),
        dashboard_url=dashboard_url,
        ideas=_read_or_empty(args.ideas_file),
        sources_today=_read_or_empty(args.sources_today_file),
    )

    cleaned_email, bad_urls = verify_urls(email_html)
    if bad_urls:
        print(f"stripped {len(bad_urls)} dead link(s)", file=sys.stderr)

    # Dashboard variant: keep webhook hrefs (JS overlay intercepts clicks).
    dashboard_html = make_interactive_dashboard(
        cleaned_email, dashboard_url, ACK_WEBHOOK_URL, today,
    )
    save_dashboard(dashboard_html, dashboard_slug)

    # Email variant: rewrite webhook hrefs to dashboard anchors so the
    # email's per-item buttons land on the dashboard (no more "Sorry,
    # unable to open" Apps Script errors from /u/1/ multi-account).
    email_for_send = redirect_webhooks_to_dashboard(cleaned_email, dashboard_url)

    Path(args.out_email).write_text(email_for_send, encoding="utf-8")
    Path(args.out_dashboard).write_text(dashboard_html, encoding="utf-8")
    Path(args.dashboard_url_out).write_text(dashboard_url, encoding="utf-8")
    print(f"email → {args.out_email}", file=sys.stderr)
    print(f"dashboard → {args.out_dashboard}", file=sys.stderr)
    print(f"dashboard URL → {dashboard_url}", file=sys.stderr)


if __name__ == "__main__":
    main()
