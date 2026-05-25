"""Send a reply in-thread to a Gmail thread the agent has drafted a body
for. Constructs proper In-Reply-To + References headers so it threads.

Usage:
    python send_reply.py --thread-id <gmail-thread-id> --body-file /tmp/reply.txt
    python send_reply.py --thread-id <id> --body-text "Hi, quick yes — ..."
"""

from __future__ import annotations
import argparse
import base64
import sys
from email.mime.text import MIMEText
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

from googleapiclient.discovery import build  # noqa: E402

from daily_briefing import google_creds, RECIPIENT_EMAIL  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--thread-id", required=True)
    parser.add_argument("--body-file", default=None)
    parser.add_argument("--body-text", default=None)
    parser.add_argument("--html", action="store_true",
                        help="Treat body as HTML (default: text/plain)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Build the message and print headers, don't send")
    args = parser.parse_args()

    if not args.body_file and not args.body_text:
        parser.error("provide one of --body-file or --body-text")
    body = (args.body_text if args.body_text
            else Path(args.body_file).read_text(encoding="utf-8"))

    creds = google_creds()
    svc = build("gmail", "v1", credentials=creds, cache_discovery=False)

    thread = svc.users().threads().get(
        userId="me", id=args.thread_id, format="metadata",
        metadataHeaders=["Subject", "From", "To", "Cc", "Message-ID", "References"],
    ).execute()
    msgs = thread.get("messages") or []
    if not msgs:
        sys.exit(f"thread {args.thread_id} has no messages")

    latest = msgs[-1]
    headers = {h["name"]: h["value"]
               for h in latest["payload"].get("headers", [])}
    subj = headers.get("Subject", "(no subject)")
    if not subj.lower().startswith("re:"):
        subj = "Re: " + subj
    sender = headers.get("From", "")
    cc = headers.get("Cc", "")
    msg_id = headers.get("Message-ID") or headers.get("Message-Id") or ""
    refs = headers.get("References", "")
    references = (refs + " " + msg_id).strip() if refs else msg_id

    mime = MIMEText(body, "html" if args.html else "plain", "utf-8")
    mime["to"] = sender
    if cc:
        mime["cc"] = cc
    mime["from"] = RECIPIENT_EMAIL
    mime["subject"] = subj
    if msg_id:
        mime["In-Reply-To"] = msg_id
        mime["References"] = references

    if args.dry_run:
        print("--- dry run: would send ---", file=sys.stderr)
        print(f"To: {mime['to']}", file=sys.stderr)
        if cc:
            print(f"Cc: {cc}", file=sys.stderr)
        print(f"Subject: {subj}", file=sys.stderr)
        print(f"In-Reply-To: {msg_id}", file=sys.stderr)
        print(f"--- body ({len(body)} chars) ---", file=sys.stderr)
        print(body[:500], file=sys.stderr)
        return

    raw = base64.urlsafe_b64encode(mime.as_bytes()).decode()
    sent = svc.users().messages().send(
        userId="me",
        body={"raw": raw, "threadId": args.thread_id},
    ).execute()
    print(f"sent message id={sent.get('id')} thread={args.thread_id}",
          file=sys.stderr)


if __name__ == "__main__":
    main()
