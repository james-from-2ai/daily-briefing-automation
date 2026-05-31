"""Publish a standalone "expand" page to GitHub Pages and return its URL.

Used for the briefing's click-to-expand deep views (full news sweep, full
Drive change log). Each is its own dated, unguessable, noindex page under
docs/; the briefing links to it. Because deliver.py force-adds
docs/<today>-*.html, these pages ride out on the same push that ships the
dashboard — no deliver.py change needed.

Reused by build_drive_log.py (deterministic) and called directly from the
skill for the agent-written news sweep.

CLI:
    python publish_extra_page.py --title "Full news sweep" \\
        --suffix news-full --content-file /tmp/section-news-full.html
    # prints the page URL to stdout
"""

from __future__ import annotations
import argparse
import datetime as dt
import sys
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

from daily_briefing import save_dashboard, GITHUB_PAGES_BASE, _NO_PAD_DAY  # noqa: E402

# Lightweight shell — same visual language as the email/dashboard, but
# self-contained. noindex + unguessable slug keep it off search engines,
# matching the main dashboard's privacy posture.
_SHELL = """<!doctype html>
<html><head><meta charset="utf-8">
<meta name="robots" content="noindex, nofollow, noarchive">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} — 2AI</title>
<style>
  body {{ font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
    max-width:820px;margin:0 auto;padding:32px 22px 56px;color:#111827;
    line-height:1.55;background:#fafaf7; }}
  .eyebrow {{ font-size:11px;text-transform:uppercase;letter-spacing:1.4px;
    color:#6b7280;font-weight:700;margin-bottom:4px; }}
  h1 {{ font-size:26px;font-weight:800;letter-spacing:-0.4px;margin:0 0 4px; }}
  .subtitle {{ color:#6b7280;font-size:13px;margin-bottom:24px; }}
  h2 {{ font-size:18px;font-weight:700;color:#1f2937;margin:24px 0 10px; }}
  h3 {{ font-size:15px;font-weight:700;color:#374151;margin:18px 0 6px; }}
  p,li {{ font-size:14.5px; }} em {{ color:#4b5563; }}
  a {{ color:#0e7490;text-decoration:none;border-bottom:1px solid rgba(14,116,144,0.35); }}
  ul {{ margin:8px 0;padding-left:22px; }} li {{ margin:5px 0; }}
  .back {{ display:inline-block;margin-bottom:18px;font-size:13px; }}
</style></head><body>
<div class="eyebrow">2AI Daily Briefing · {datestr}</div>
<h1>{title}</h1>
<div class="subtitle">Generated {gen} · expanded view</div>
{content}
</body></html>"""


def publish_page(title: str, suffix: str, content_html: str,
                 today: dt.date | None = None) -> str:
    """Wrap an HTML fragment in the shell, save to docs/, return its URL."""
    today = today or dt.date.today()
    slug = f"{today.isoformat()}-{suffix}-{uuid.uuid4().hex[:12]}"
    html = _SHELL.format(
        title=title,
        datestr=today.strftime(f"%A %B {_NO_PAD_DAY}").upper(),
        gen=dt.datetime.now().strftime("%H:%M"),
        content=content_html,
    )
    save_dashboard(html, slug)
    return f"{GITHUB_PAGES_BASE}/{slug}.html"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--title", required=True)
    parser.add_argument("--suffix", required=True,
                        help="Slug suffix, e.g. 'news-full' or 'drive-log'")
    parser.add_argument("--content-file", required=True)
    args = parser.parse_args()
    content = Path(args.content_file).read_text(encoding="utf-8")
    url = publish_page(args.title, args.suffix, content)
    # URL on stdout so the skill can capture it and link to it.
    print(url)


if __name__ == "__main__":
    main()
