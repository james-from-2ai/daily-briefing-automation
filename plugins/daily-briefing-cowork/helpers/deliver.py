"""Ship the briefing: Drive Doc upload, Gmail send, Slack DM, and push
the dashboard to GitHub (which triggers Pages deploy via existing workflow
or branch-based serving).

Usage:
    python deliver.py \\
      --email-html /tmp/briefing-email.html \\
      --dashboard-html /tmp/briefing-dashboard.html \\
      --dashboard-url-file /tmp/dashboard-url.txt \\
      [--carry-count N]

TODO before production:
  - Implement git push step (the cowork skill triggers Pages deploy this way)
  - Decide on commit author/message convention
  - Wire alert_slack_failure for top-level error handling
"""

from __future__ import annotations
import argparse
import datetime as dt
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

from daily_briefing import (  # noqa: E402
    google_creds, upload_drive_doc, send_gmail, post_slack,
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--email-html", required=True)
    parser.add_argument("--dashboard-html", required=True)
    parser.add_argument("--dashboard-url-file", required=True)
    parser.add_argument("--carry-count", type=int, default=0)
    parser.add_argument("--skip-pages-push", action="store_true",
                        help="Skip the git commit + push step (useful during testing)")
    args = parser.parse_args()

    today = dt.date.today()
    creds = google_creds()
    email_html = Path(args.email_html).read_text(encoding="utf-8")
    dashboard_url = Path(args.dashboard_url_file).read_text(encoding="utf-8").strip()

    # 1. Drive Doc
    doc_link = upload_drive_doc(creds, email_html, today)
    print(f"  Drive: {doc_link}", file=sys.stderr)

    # 2. Gmail
    send_gmail(creds, email_html, today)
    print("  Gmail sent", file=sys.stderr)

    # 3. Slack
    post_slack(doc_link, today, carry_count=args.carry_count,
               dashboard_url=dashboard_url)
    print("  Slack posted", file=sys.stderr)

    # 4. Push dashboard to GitHub (triggers Pages deploy)
    if not args.skip_pages_push:
        # The dashboard HTML is already written to repo's docs/ dir by
        # save_dashboard() during render_artifacts. Now commit + push.
        cwd = str(REPO_ROOT)
        try:
            subprocess.run(["git", "add", "docs/"], cwd=cwd, check=True)
            subprocess.run([
                "git", "commit", "-m",
                f"dashboard: cowork run {today.isoformat()}",
                "--allow-empty",
            ], cwd=cwd, check=True)
            subprocess.run(["git", "push"], cwd=cwd, check=True)
            print("  GitHub Pages: pushed", file=sys.stderr)
        except subprocess.CalledProcessError as e:
            print(f"  GitHub Pages push FAILED: {e}", file=sys.stderr)
            raise


if __name__ == "__main__":
    main()
