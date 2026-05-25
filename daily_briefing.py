"""
daily_briefing.py — 2AI daily prioritization + news briefing.

Run this once a day (e.g. 7:30am cron). On each run it:
  1. Pulls today's calendar, recently-modified Drive files, and the most
     recent dated entries from your 1:1 running-notes docs.
  2. Pulls the news-topics sheet from Drive, asks Claude (with web search)
     to do a "deep research" pass on each top-tier topic.
  3. Synthesises a prioritization brief + news briefing.
  4. Renders a single HTML email, uploads a copy as a Google Doc, emails
     it to you, and DMs a Slack summary.

See SETUP.md for the one-time OAuth + Slack token setup.
"""

import base64
import datetime as dt
import hashlib
import json
import os
import re
import sys
import textwrap
import time
import uuid
import urllib.parse
from email.mime.text import MIMEText
from pathlib import Path

import requests

# Force UTF-8 on stdout/stderr so emoji-laced progress prints don't crash
# under Windows' cp1252 console code page (cron / GitHub Actions are
# already UTF-8 — this is a no-op there).
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

# strftime codes for single-digit day / 12-hour without leading zeros.
# Linux/macOS use "%-d" / "%-I"; Windows uses "%#d" / "%#I". Use these
# everywhere instead of hardcoding either dialect.
_NO_PAD_DAY = "%#d" if os.name == "nt" else "%-d"
_NO_PAD_HOUR = "%#I" if os.name == "nt" else "%-I"

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload
import io

import anthropic
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

# ---------- Config — edit these ----------

RECIPIENT_EMAIL = "james@aiaccessinitiative.org"
SLACK_USER_ID = "U09AL2HCDCZ"  # James, used as DM channel_id

# Manager 1:1 running notes (Google Doc file IDs).
ONEONONE_DOCS = {
    "Katie": "1B_Dzeh7Y4v-t56sU30Bw16IQtiEz8ixuNkx6zTfLlXg",
    "Sarah": "1FC-rJcpkfLKf1v7tasO_Qw_q7JiREBbrft6nSgUMQbI",
}

# News-topics spreadsheet (X-Sector → Testing & Experimentation).
NEWS_TOPICS_SHEET_ID = "14KtogU6W-eRD-S6yE48w-XPTGuhyqEa32kdGYAa-BYU"

# Where the daily briefing Docs get filed. Personal My Drive folder
# "Claude Automated Briefing" — private to james@aiaccessinitiative.org.
BRIEFINGS_DRIVE_FOLDER_ID = "1NQACtD1-uhrakMexgbuYGU_qLTDFlo7F"

# Feedback loop. Google Form → Sheet captures ratings; the script reads the
# last FEEDBACK_LOOKBACK_DAYS of rows and feeds them into the critic + the
# synth prompts so the system gets better over time. See SETUP.md §6.
FEEDBACK_SHEET_ID = "1N3gv44ytZXGhsWlKtn2toXlctfVsuvpy9zYXeghwZmk"
FEEDBACK_FORM_URL = "https://docs.google.com/forms/d/e/1FAIpQLSeFEcK8KeQRoW1qM7JdWv_D3VMiNVlVQd0PejZ46RQvs8jFYA/viewform"
FEEDBACK_FORM_DATE_FIELD = "699836171"  # numeric entry ID for the "Briefing date" field
FEEDBACK_LOOKBACK_DAYS = 21

# Interactive dashboard hosted on GitHub Pages. Each run writes a
# new UUID-named HTML file to ./docs/; the deploy workflow uploads
# ./docs/ as the Pages artifact. Email contains the per-day URL.
# Privacy: unguessable filename + noindex meta + robots.txt Disallow,
# so the briefing content isn't findable by search engines or by
# enumerating dates. Site is technically public per github.io.
GITHUB_PAGES_BASE = "https://james-from-2ai.github.io/daily-briefing-automation"
DASHBOARD_DIR = Path(__file__).parent / "docs"
DASHBOARD_COMMENTS_LOOKBACK_DAYS = 14

# Briefing state — the durable layer. Two sheets:
#   STATE_SHEET_ID: every item ever surfaced (priorities, slips, decisions,
#     action items, news topics, white-space items, inbox/funder hits)
#     with status (open/done/dismissed), carry_count (days unack'd), and
#     stable hash key so we can dedup across days.
#   ACK_SHEET_ID:   one row per briefing acknowledgment (an "I saw it") and
#     per-item "mark done" submissions, both written by a Google Apps Script
#     webhook that the "Mark as seen" / "Mark done" email links hit.
# See SETUP.md §6 + §7.
STATE_SHEET_ID = "1VL-WSs0DTdlGMCFEBwkKae7yD7IwxP_xRAcyhwiT-fg"
ACK_SHEET_ID = "1VL-WSs0DTdlGMCFEBwkKae7yD7IwxP_xRAcyhwiT-fg"
ACK_WEBHOOK_URL = "https://script.google.com/macros/s/AKfycbzxmy0UYGBMfvSqYGH05rOuER0hVhCFMeYfYuIAT3K558yF7drzULAvaen2i5W3coeNcg/exec"
STALE_DAYS = 3                   # items carried forward this long get a red flag
MAX_CARRY_ITEMS = 25             # safety cap on carryover section size

CALENDAR_ID = "primary"
TIMEZONE = "America/New_York"
CALENDAR_LOOKAHEAD_DAYS = 7
DRIVE_RECENT_LOOKBACK_HOURS = 30

# Inbox triage — two buckets:
#   "Needs you": last INBOX_LOOKBACK_HOURS, actionable-looking threads
#     where you owe a reply / decision / approval.
#   "Likely to slip through": threads aged INBOX_STALE_MIN_DAYS to
#     INBOX_STALE_MAX_DAYS where you were addressed but haven't sent any
#     reply in the thread yet — risk of falling off your radar.
INBOX_LOOKBACK_HOURS = 24
INBOX_TRIAGE_MAX = 12
INBOX_STALE_MIN_DAYS = 3
INBOX_STALE_MAX_DAYS = 14
INBOX_STALE_MAX = 10

# Funder watchlist — runs every other day. Funder moves drive 2AI
# fundraising directly, but daily funder updates rarely change vs.
# 2-day cadence, and a daily watchlist is the single biggest line
# item in the briefing budget (~$1.50/day in Sonnet + web_search).
# Halves that cost. Use `today.toordinal() % 2 == 0` so the cadence
# is deterministic and doesn't drift across month boundaries.
FUNDER_RUN_PARITY = 0   # 0 = even ordinal days, 1 = odd. Either works.
FUNDER_WATCHLIST = [
    {"name": "Coefficient Giving", "query": "Coefficient Giving announcements last 7 days",
     "url": "https://coefficientgiving.org/"},
    {"name": "Gates Foundation",   "query": "Gates Foundation AI global health last 7 days",
     "url": "https://www.gatesfoundation.org/ideas/"},
    {"name": "Wellcome",           "query": "Wellcome Trust AI global health last 7 days",
     "url": "https://wellcome.org/news"},
    {"name": "Schmidt Sciences",   "query": "Schmidt Sciences AI for science grants last 7 days",
     "url": "https://www.schmidtsciences.org/"},
    {"name": "OpenAI Foundation",  "query": "OpenAI Foundation RFP grants last 7 days",
     "url": "https://openaiglobalaffairs.substack.com/"},
]

# Claude config — three tiers.
#   CLAUDE_MODEL: daily synthesis + critic + inbox triage + news picker +
#     preference digest. Opus for reasoning-heavy work the user feels daily.
#   CLAUDE_RESEARCH_MODEL: daily web-search extraction (news deep-dives,
#     funder watchlist, evidence digest, source proposer). Sonnet because
#     these are dominated by search-result quality, not model synthesis.
#   CLAUDE_ANALYSIS_MODEL: weekly/monthly pattern recognition (trends,
#     white-space, peer-publisher landscape). Opus — these are the
#     judgment-heavy features where the model's reasoning shows through.
# Projected daily cost: ~$1.20 (3 Opus synth calls + 11 Sonnet web-search
# calls). Weekly analytical runs add ~$0.30-0.50 each.
CLAUDE_MODEL = "claude-opus-4-7"
CLAUDE_RESEARCH_MODEL = "claude-sonnet-4-6"
CLAUDE_ANALYSIS_MODEL = "claude-opus-4-7"
NEWS_DEEP_DIVE_TOPICS = 6  # top-N Tier 1/2 topics to research each day
NEWS_DEDUP_LOOKBACK_DAYS = 7

# Weekly white-space analysis: pull recent docs in each program area,
# research what's emerging publicly, flag topics moving in the field that
# aren't in your Drive corpus. Runs only on WHITESPACE_WEEKDAY (0=Mon).
PROGRAM_AREAS = ["health", "agriculture", "education"]
WHITESPACE_WEEKDAY = 0
WHITESPACE_CORPUS_LOOKBACK_DAYS = 45
WHITESPACE_CORPUS_PER_AREA = 12   # how many recent docs to sample per area

# Trends view — looks across N days of indexed news/funder/whitespace items
# in the state sheet and asks Claude to spot emerging patterns, opportunity
# spaces, and underreported areas. Runs weekly.
TRENDS_WEEKDAY = 2                # 2 = Wednesday
TRENDS_LOOKBACK_DAYS = 60

# Source proposer — searches the web for high-quality news/research sources
# not already in your watchlist, asks James to 👍/👎 add. Runs weekly.
SOURCES_WEEKDAY = 4               # 4 = Friday
SOURCES_PROPOSE_N = 4             # how many candidates to surface per run

# Peer-publisher landscape — once a month, look at what these orgs have
# published recently, profile each, then synthesise (a) where each is
# focused / where their gaps are, (b) sector-wide publishing gaps. Heavy:
# runs only on the first weekday of each calendar month.
PEER_PUBLISHERS = [
    {"name": "CGD",                "url": "https://www.cgdev.org/publications"},
    {"name": "PxD",                "url": "https://precisiondev.org/news/"},
    {"name": "Rethink Priorities", "url": "https://rethinkpriorities.org/research/"},
    {"name": "VoxDev",             "url": "https://voxdev.org/"},
    {"name": "GiveWell",           "url": "https://blog.givewell.org/"},
    {"name": "J-PAL",              "url": "https://www.povertyactionlab.org/"},
    {"name": "IPA",                "url": "https://poverty-action.org/"},
    {"name": "Digital Green",      "url": "https://digitalgreen.org/news/"},
    {"name": "Lelapa AI",          "url": "https://lelapa.ai/"},
    {"name": "AI4Bharat",          "url": "https://ai4bharat.iitm.ac.in/"},
    {"name": "Stanford HAI",       "url": "https://hai.stanford.edu/news"},
    {"name": "IFPRI",              "url": "https://www.ifpri.org/blog"},
]
PUBLISHER_LANDSCAPE_LOOKBACK_DAYS = 60

# Evidence digest — twice-weekly pull of new RCTs, studies, preprints from
# consensus.app + preprint servers via Claude web-search.
# Note: the $10/mo Consensus plan grants UI access, not API access. We
# therefore restrict Claude's web_search tool to academic domains to get
# Consensus-style results without consuming Pro messages. To swap to a real
# Consensus API call (when you have access), replace _evidence_call().
EVIDENCE_WEEKDAYS = [1, 3]   # 1=Tue, 3=Thu — "every couple days" cadence
EVIDENCE_STREAMS = [
    {
        "name": "AI performance & capabilities",
        "query": ("new AI model benchmarks, evaluations, capability "
                  "papers from the last 7 days"),
        "domains": ["consensus.app", "arxiv.org", "openreview.net",
                    "metr.org", "epoch.ai", "huggingface.co"],
    },
    {
        "name": "Weather × AI / Health × AI",
        "query": ("new RCTs, preprints, and studies on AI in clinical care, "
                  "global-health, weather forecasting, anticipatory action "
                  "from the last 7 days"),
        "domains": ["consensus.app", "arxiv.org", "biorxiv.org",
                    "medrxiv.org", "thelancet.com", "ai.nejm.org",
                    "ecmwf.int", "ncbi.nlm.nih.gov"],
    },
]
EVIDENCE_ITEMS_PER_STREAM = 4

GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/spreadsheets",
]

TOKEN_PATH = Path("~/.config/2ai-briefing/token.json").expanduser()
CLIENT_SECRET_PATH = Path("~/.config/2ai-briefing/client_secret.json").expanduser()

# James's cowork-managed task system. The briefing READS from these files
# for context (live task list, recent journal entries) and feeds it into
# the prioritization prompt. The briefing NEVER writes to these files —
# tasks/journal are owned by the cowork workflow.
TASKS_JSON_PATH = Path(
    r"C:\Users\G09jb\OneDrive\Documents\0 EVIDENCE ACTION"
    r"\AI for Goodo\Task Prio\tasks.json"
)
JOURNAL_JSON_PATH = Path(
    r"C:\Users\G09jb\OneDrive\Documents\0 EVIDENCE ACTION"
    r"\AI for Goodo\Task Prio\journal.json"
)
JOURNAL_LOOKBACK_DAYS = 7
TASKS_TOP_N = 12  # cap how many tasks we pull into the prompt context

# Weather + market widgets at the top of the briefing.
WEATHER_CITIES = [
    ("NYC",     40.7128,  -74.0060, "🇺🇸"),
    ("SF",      37.7749, -122.4194, "🇺🇸"),
    ("Beijing", 39.9042,  116.4074, "🇨🇳"),
]
STOCK_TICKERS = [("^GSPC", "S&P 500"), ("QQQ", "QQQ")]


# ---------- Google auth ----------

def google_creds() -> Credentials:
    """Load cached OAuth creds, or run the consent flow once."""
    creds = None
    if TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), GOOGLE_SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                str(CLIENT_SECRET_PATH), GOOGLE_SCOPES
            )
            creds = flow.run_local_server(port=0)
        TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")
    return creds


# ---------- External context: cowork task system + widget feeds ----------
#
# These functions are READ-ONLY. The daily briefing pulls task / journal
# context to inform prioritization, and pulls free weather + market data
# for a morning widget strip. None of them write back.


def pull_tasks_json() -> list[dict]:
    """Return the cowork-managed active task list (status != done),
    truncated to TASKS_TOP_N. Gracefully returns [] if the file is
    missing, locked, or malformed — never blocks the briefing.
    """
    if not TASKS_JSON_PATH.exists():
        return []
    try:
        data = json.loads(TASKS_JSON_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return []
    tasks = [t for t in data.get("tasks", []) if t.get("status") != "done"]
    # Preserve cowork's ranking (tasks.json is already ranked).
    return tasks[:TASKS_TOP_N]


def pull_journal_recent() -> list[dict]:
    """Return cowork journal entries from the last JOURNAL_LOOKBACK_DAYS.
    Each entry: timestamp, tasks_completed, tasks_added, energy_note,
    blockers_noted, velocity, raw_quote. Used for context only.
    """
    if not JOURNAL_JSON_PATH.exists():
        return []
    try:
        data = json.loads(JOURNAL_JSON_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return []
    cutoff = dt.datetime.now() - dt.timedelta(days=JOURNAL_LOOKBACK_DAYS)
    out = []
    for e in data.get("entries", []):
        ts_str = e.get("timestamp", "")
        try:
            ts = dt.datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            if ts.replace(tzinfo=None) < cutoff:
                continue
        except (ValueError, TypeError):
            continue
        out.append(e)
    return out


def pull_weather() -> dict[str, str | None]:
    """Open-Meteo (free, no API key). Returns {label: '18°C' | None}.
    Failure of one city doesn't sink the others.
    """
    out: dict[str, str | None] = {}
    for label, lat, lon, _flag in WEATHER_CITIES:
        try:
            r = requests.get(
                "https://api.open-meteo.com/v1/forecast",
                params={
                    "latitude": lat, "longitude": lon,
                    "current": "temperature_2m",
                    "temperature_unit": "celsius",
                    "timezone": "auto",
                },
                timeout=5,
            )
            r.raise_for_status()
            temp = r.json().get("current", {}).get("temperature_2m")
            out[label] = f"{round(temp)}°C" if temp is not None else None
        except Exception:
            out[label] = None
    return out


def pull_stocks() -> dict[str, dict | None]:
    """Previous trading day's close + pct change for each ticker via
    yfinance (scrapes Yahoo Finance). Returns
        {ticker: {"close": float, "pct_change": float}} | None.
    """
    try:
        import yfinance as yf
    except ImportError:
        print("[widgets] yfinance not installed; skipping stocks")
        return {t[0]: None for t in STOCK_TICKERS}
    out: dict[str, dict | None] = {}
    for ticker, _label in STOCK_TICKERS:
        try:
            hist = yf.Ticker(ticker).history(period="5d")
            if len(hist) < 2:
                out[ticker] = None
                continue
            prev = float(hist["Close"].iloc[-2])
            last = float(hist["Close"].iloc[-1])
            out[ticker] = {
                "close": round(last, 2),
                "pct_change": round((last - prev) / prev * 100, 2),
            }
        except Exception:
            out[ticker] = None
    return out


def render_widgets_strip(weather: dict, stocks: dict) -> str:
    """Compact data strip rendered just under the TL;DR. Empty string if
    every feed failed."""
    w_parts: list[str] = []
    for label, _lat, _lon, flag in WEATHER_CITIES:
        v = weather.get(label)
        if v:
            w_parts.append(f"{flag}&nbsp;{label}&nbsp;<strong>{v}</strong>")
    s_parts: list[str] = []
    for ticker, display_label in STOCK_TICKERS:
        v = stocks.get(ticker)
        if v:
            pct = v["pct_change"]
            arrow = "▲" if pct >= 0 else "▼"
            color = "#15803d" if pct >= 0 else "#dc2626"
            s_parts.append(
                f'<span>{display_label}&nbsp;<strong>{v["close"]:,.2f}</strong>'
                f'&nbsp;<span style="color:{color};font-weight:600;">'
                f'{arrow}&nbsp;{pct:+.2f}%</span></span>'
            )
    if not w_parts and not s_parts:
        return ""
    weather_html = " &nbsp;·&nbsp; ".join(w_parts)
    stocks_html = " &nbsp;·&nbsp; ".join(s_parts)
    sep = ' &nbsp;<span style="color:#d1d5db;">|</span>&nbsp; ' if (w_parts and s_parts) else ""
    return (
        '<div class="widgets" style="display:block;'
        'padding:10px 16px;background:#ffffff;border-radius:8px;'
        'border:1px solid #e5e7eb;margin:0 0 18px 0;font-size:13px;'
        f'color:#374151;line-height:1.6;">{weather_html}{sep}{stocks_html}</div>'
    )


# ---------- Calendar ----------

def pull_calendar(creds, today: dt.date):
    """Returns a list of events from now through CALENDAR_LOOKAHEAD_DAYS."""
    svc = build("calendar", "v3", credentials=creds, cache_discovery=False)
    start = dt.datetime.combine(today, dt.time(0, 0)).isoformat() + "Z"
    end = dt.datetime.combine(
        today + dt.timedelta(days=CALENDAR_LOOKAHEAD_DAYS), dt.time(23, 59)
    ).isoformat() + "Z"
    resp = svc.events().list(
        calendarId=CALENDAR_ID,
        timeMin=start,
        timeMax=end,
        singleEvents=True,
        orderBy="startTime",
        maxResults=100,
    ).execute()
    events = []
    for e in resp.get("items", []):
        start_obj = e.get("start", {})
        events.append({
            "summary": e.get("summary", "(no title)"),
            "start": start_obj.get("dateTime") or start_obj.get("date"),
            "end": (e.get("end") or {}).get("dateTime") or (e.get("end") or {}).get("date"),
            "attendees": [a.get("email") for a in e.get("attendees") or []],
            # Wider truncation so prep notes / agendas / pre-read context are
            # available for cross-referencing in synthesize_prioritization.
            "description": (e.get("description") or "")[:1200],
            "location": e.get("location", ""),
        })
    return events


# ---------- Drive ----------

def pull_drive_recent(creds):
    """Files modified in the last DRIVE_RECENT_LOOKBACK_HOURS by anyone shared with me."""
    svc = build("drive", "v3", credentials=creds, cache_discovery=False)
    since = (dt.datetime.utcnow() - dt.timedelta(hours=DRIVE_RECENT_LOOKBACK_HOURS)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    resp = svc.files().list(
        q=f"modifiedTime > '{since}' and trashed = false",
        orderBy="modifiedTime desc",
        pageSize=25,
        fields="files(id,name,mimeType,modifiedTime,owners,webViewLink,lastModifyingUser)",
    ).execute()
    return resp.get("files", [])


def export_doc_text(creds, file_id: str) -> str:
    """Export a Google Doc as plain text."""
    svc = build("drive", "v3", credentials=creds, cache_discovery=False)
    data = svc.files().export(fileId=file_id, mimeType="text/plain").execute()
    return data.decode("utf-8") if isinstance(data, bytes) else str(data)


def pull_1on1_recent_entries(creds, file_id: str, n: int = 2) -> str:
    """Return the last `n` dated meeting sections from a running-notes doc.

    Looks for headers like "May 22, 2026" or "MAY 22, 2026". The doc has the
    most recent meetings at the *top*, so we take the first `n` matches.
    """
    text = export_doc_text(creds, file_id)
    pat = re.compile(
        r"(?im)^\s*#{0,3}\s*"
        r"(January|February|March|April|May|June|July|August|"
        r"September|October|November|December|"
        r"Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)\s+\d{1,2},?\s+202\d\s*$"
    )
    headers = list(pat.finditer(text))
    if not headers:
        # Fallback: first 4k chars.
        return text[:4000]
    cuts = [h.start() for h in headers[: n + 1]]
    if len(cuts) < 2:
        return text[cuts[0]: cuts[0] + 8000]
    return text[cuts[0]: cuts[-1]] if len(cuts) > n else text[cuts[0]:]


def pull_news_topics_sheet(creds) -> str:
    """Return the news-topics sheet as a tab-separated text blob.

    Reads every tab via the Sheets API (Drive's files().export() does not
    support text/plain for native Sheets — Docs only), concatenating with
    a heading per tab so the synth prompt can see tier groupings.
    """
    svc = build("sheets", "v4", credentials=creds, cache_discovery=False)
    meta = svc.spreadsheets().get(spreadsheetId=NEWS_TOPICS_SHEET_ID).execute()
    parts = []
    for s in meta.get("sheets", []):
        title = s["properties"]["title"]
        resp = svc.spreadsheets().values().get(
            spreadsheetId=NEWS_TOPICS_SHEET_ID,
            range=f"'{title}'!A:Z",
        ).execute()
        rows = resp.get("values", [])
        body = "\n".join("\t".join(str(c) for c in r) for r in rows)
        parts.append(f"### {title}\n{body}")
    return "\n\n".join(parts)


def pull_recent_feedback(creds) -> str:
    """Read the last FEEDBACK_LOOKBACK_DAYS of rows from the feedback Sheet.

    Returns a plain-text digest the synthesizer and critic can use as
    context. Empty string if FEEDBACK_SHEET_ID is unset or the sheet is empty.
    """
    if not FEEDBACK_SHEET_ID:
        return ""
    svc = build("sheets", "v4", credentials=creds, cache_discovery=False)
    resp = svc.spreadsheets().values().get(
        spreadsheetId=FEEDBACK_SHEET_ID,
        range="A:Z",
    ).execute()
    rows = resp.get("values", [])
    if len(rows) < 2:
        return ""
    header, body = rows[0], rows[1:]
    cutoff = dt.date.today() - dt.timedelta(days=FEEDBACK_LOOKBACK_DAYS)
    keep = []
    for r in body:
        if not r:
            continue
        try:
            row_date = dt.datetime.strptime(r[0][:10], "%Y-%m-%d").date()
            if row_date < cutoff:
                continue
        except (ValueError, IndexError):
            pass  # keep undated rows just in case
        keep.append(dict(zip(header, r)))
    if not keep:
        return ""
    return json.dumps(keep, indent=2)


def pull_program_area_corpus(creds) -> dict[str, list[dict]]:
    """For each program area, return recent Drive docs with their first chunk.

    Used for white-space analysis: shows the synthesizer what we *have* been
    thinking about so it can find what we *haven't*. Title-keyword matching
    is the cheap heuristic; refine with a tagged folder structure later.
    """
    svc = build("drive", "v3", credentials=creds, cache_discovery=False)
    since = (dt.datetime.utcnow() - dt.timedelta(days=WHITESPACE_CORPUS_LOOKBACK_DAYS)
             ).strftime("%Y-%m-%dT%H:%M:%SZ")
    out = {}
    for area in PROGRAM_AREAS:
        # Match common variants. Add aliases here as your taxonomy evolves.
        aliases = {
            "health": ["health", "medical", "CHW", "maternal", "TB", "malaria"],
            "agriculture": ["agriculture", "agricultural", "farmer", "crop", "ag "],
            "education": ["education", "edtech", "tutoring", "learning"],
        }.get(area, [area])
        q_parts = " or ".join(f"fullText contains '{a}'" for a in aliases)
        q = (f"modifiedTime > '{since}' and trashed = false and "
             f"mimeType = 'application/vnd.google-apps.document' and ({q_parts})")
        resp = svc.files().list(
            q=q,
            orderBy="modifiedTime desc",
            pageSize=WHITESPACE_CORPUS_PER_AREA,
            fields="files(id,name,modifiedTime,webViewLink)",
        ).execute()
        files = resp.get("files", [])
        # Grab first 1500 chars of each so the critic can see what's covered.
        for f in files:
            try:
                f["excerpt"] = export_doc_text(creds, f["id"])[:1500]
            except Exception as e:
                f["excerpt"] = f"[unreadable: {e}]"
        out[area] = files
    return out


# ---------- Inbox triage (Gmail) ----------

def pull_inbox_signals(creds) -> list[dict]:
    """Return inbox threads in two buckets, tagged with `kind`:

    - kind="needs_you": threads from the last INBOX_LOOKBACK_HOURS that
      look actionable (you're in To, sender isn't you, subject has
      decision-y words). Same heuristic as before.
    - kind="stale": threads aged INBOX_STALE_MIN_DAYS to
      INBOX_STALE_MAX_DAYS where you were addressed but haven't replied
      in-thread yet. Detected by walking the thread and checking that
      the most recent message is from someone else.
    """
    svc = build("gmail", "v1", credentials=creds, cache_discovery=False)
    user_email = RECIPIENT_EMAIL.lower()
    out: list[dict] = []
    seen_threads: set[str] = set()

    # ----- bucket 1: needs you (recent, actionable) -----
    since_q = f"newer_than:{max(1, INBOX_LOOKBACK_HOURS // 24)}d"
    q1 = (f"{since_q} to:me -from:me "
          "is:unread OR subject:(? OR decide OR decision OR approve OR review "
          "OR ASAP OR urgent OR EOD OR deadline)")
    resp = svc.users().messages().list(userId="me", q=q1,
                                       maxResults=30).execute()
    for m in (resp.get("messages") or [])[:INBOX_TRIAGE_MAX]:
        full = svc.users().messages().get(
            userId="me", id=m["id"], format="metadata",
            metadataHeaders=["Subject", "From", "Date", "To"]).execute()
        headers = {h["name"]: h["value"] for h in full["payload"]["headers"]}
        thread_id = full.get("threadId")
        seen_threads.add(thread_id)
        out.append({
            "kind": "needs_you",
            "id": m["id"],
            "thread_id": thread_id,
            "subject": headers.get("Subject", "(no subject)"),
            "from": headers.get("From", ""),
            "date": headers.get("Date", ""),
            "snippet": full.get("snippet", "")[:280],
            "link": f"https://mail.google.com/mail/u/0/#inbox/{thread_id}",
        })

    # ----- bucket 2: stale threads where you haven't replied -----
    stale_q = (f"to:me -from:me "
               f"older_than:{INBOX_STALE_MIN_DAYS}d "
               f"newer_than:{INBOX_STALE_MAX_DAYS}d "
               f"-category:promotions -category:social -category:updates "
               f"-in:sent -in:chats")
    resp = svc.users().messages().list(userId="me", q=stale_q,
                                       maxResults=40).execute()
    stale_added = 0
    for m in (resp.get("messages") or []):
        if stale_added >= INBOX_STALE_MAX:
            break
        thread_id = m.get("threadId")
        if not thread_id or thread_id in seen_threads:
            continue
        seen_threads.add(thread_id)
        # Walk the thread; if user appears in the FROM of the most recent
        # message, they're caught up — skip.
        try:
            thread = svc.users().threads().get(
                userId="me", id=thread_id, format="metadata",
                metadataHeaders=["From", "Date", "Subject"]).execute()
        except Exception:
            continue
        msgs = thread.get("messages", [])
        if not msgs:
            continue
        last = msgs[-1]
        last_headers = {h["name"]: h["value"]
                        for h in last["payload"].get("headers", [])}
        if user_email in last_headers.get("From", "").lower():
            continue  # you sent the latest reply; caught up
        first_headers = {h["name"]: h["value"]
                         for h in msgs[0]["payload"].get("headers", [])}
        try:
            last_dt = dt.datetime.fromtimestamp(int(last["internalDate"]) / 1000)
            age_days = (dt.datetime.now() - last_dt).days
        except (KeyError, ValueError, TypeError):
            age_days = None
        out.append({
            "kind": "stale",
            "id": last["id"],
            "thread_id": thread_id,
            "subject": first_headers.get("Subject", "(no subject)"),
            "from": last_headers.get("From", ""),
            "date": last_headers.get("Date", ""),
            "age_days": age_days,
            "snippet": last.get("snippet", "")[:280],
            "link": f"https://mail.google.com/mail/u/0/#inbox/{thread_id}",
        })
        stale_added += 1

    return out


# ---------- State persistence + acknowledgment + carryover ----------
#
# Single source of truth for "what's still open" lives in STATE_SHEET_ID.
# Each row is one briefing item; we identify it by a stable hash of
# (section, normalized-text) so the same priority appearing tomorrow
# matches today's row and just bumps carry_count.

STATE_COLUMNS = [
    "key", "section", "first_seen", "last_seen", "carry_count",
    "status", "acknowledged_on", "text_html", "source",
]
ACK_COLUMNS = [
    "briefing_date", "acknowledged_at", "done_keys",
]


def item_key(section: str, text: str) -> str:
    """Stable hash that survives small wording changes day-to-day."""
    norm = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", text)).strip().lower()
    norm = re.sub(r"[^a-z0-9 ]+", "", norm)[:160]
    return hashlib.sha1(f"{section}::{norm}".encode()).hexdigest()[:12]


def _sheets(creds):
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def read_state(creds) -> list[dict]:
    if not STATE_SHEET_ID:
        return []
    resp = _sheets(creds).spreadsheets().values().get(
        spreadsheetId=STATE_SHEET_ID, range="A:Z").execute()
    rows = resp.get("values", [])
    if not rows:
        return []
    header = rows[0]
    return [dict(zip(header, r + [""] * (len(header) - len(r)))) for r in rows[1:]]


def write_state(creds, items: list[dict]):
    """Overwrites the state sheet with the given items. Caller is responsible
    for merging — we always rewrite to keep it simple and atomic."""
    if not STATE_SHEET_ID:
        return
    values = [STATE_COLUMNS] + [
        [str(it.get(c, "")) for c in STATE_COLUMNS] for it in items
    ]
    sheets = _sheets(creds)
    sheets.spreadsheets().values().clear(
        spreadsheetId=STATE_SHEET_ID, range="A:Z").execute()
    sheets.spreadsheets().values().update(
        spreadsheetId=STATE_SHEET_ID, range="A1",
        valueInputOption="RAW", body={"values": values}).execute()


def read_acks(creds) -> list[dict]:
    if not ACK_SHEET_ID:
        return []
    resp = _sheets(creds).spreadsheets().values().get(
        spreadsheetId=ACK_SHEET_ID, range="A:Z").execute()
    rows = resp.get("values", [])
    if not rows:
        return []
    header = rows[0]
    return [dict(zip(header, r + [""] * (len(header) - len(r)))) for r in rows[1:]]


def merge_into_state(prior: list[dict], today_items: list[dict],
                     today: dt.date) -> list[dict]:
    """Match today's items against prior state by key, bumping last_seen and
    carry_count on re-appearance and inserting new ones. Items present in
    `prior` but NOT in `today_items` and still `open` are kept untouched —
    they roll forward as carryover until acknowledged.
    """
    today_iso = today.isoformat()
    by_key = {p["key"]: dict(p) for p in prior if p.get("key")}
    for it in today_items:
        k = it["key"]
        if k in by_key:
            row = by_key[k]
            # Only bump carry_count if it's a new day for this row.
            if row.get("last_seen") != today_iso:
                try:
                    row["carry_count"] = str(int(row.get("carry_count") or 0) + 1)
                except ValueError:
                    row["carry_count"] = "1"
            row["last_seen"] = today_iso
            row["text_html"] = it["text_html"]   # refresh wording
            row["source"] = it.get("source", row.get("source", ""))
        else:
            by_key[k] = {
                "key": k,
                "section": it["section"],
                "first_seen": today_iso,
                "last_seen": today_iso,
                "carry_count": "0",
                "status": "open",
                "acknowledged_on": "",
                "text_html": it["text_html"],
                "source": it.get("source", ""),
            }
    return list(by_key.values())


def apply_acks_to_state(state: list[dict], acks: list[dict]) -> list[dict]:
    """Read the ack sheet and flip status=done for any item whose key
    appears in done_keys. Also marks 'acknowledged_on' if the briefing-level
    ack exists for an item's last_seen date.
    """
    done_keys = set()
    seen_dates = set()
    for a in acks:
        seen_dates.add(a.get("briefing_date", "")[:10])
        for k in (a.get("done_keys") or "").split(","):
            k = k.strip()
            if k:
                done_keys.add(k)
    for row in state:
        if row.get("status") == "open" and row.get("key") in done_keys:
            row["status"] = "done"
            row["acknowledged_on"] = dt.date.today().isoformat()
        if row.get("status") == "open" and row.get("last_seen") in seen_dates:
            row["acknowledged_on"] = row.get("last_seen") or row["acknowledged_on"]
    return state


def get_carryover(state: list[dict], today: dt.date) -> list[dict]:
    """Items still open from prior days that haven't been acknowledged
    (briefing-level) and aren't already going to be re-flagged today."""
    today_iso = today.isoformat()
    carry = []
    for row in state:
        if row.get("status") != "open":
            continue
        if row.get("last_seen") == today_iso:
            continue   # will reappear in today's fresh items anyway
        if row.get("acknowledged_on"):
            continue
        carry.append(row)
    # Stalest first, then by section priority
    sect_order = {"slip": 0, "decision": 1, "priority": 2, "action_item": 3,
                  "inbox": 4, "funder": 5, "news": 6, "whitespace": 7}
    carry.sort(key=lambda r: (-int(r.get("carry_count") or 0),
                              sect_order.get(r.get("section"), 9)))
    return carry[:MAX_CARRY_ITEMS]


def was_yesterday_acknowledged(acks: list[dict], today: dt.date) -> bool:
    yest = (today - dt.timedelta(days=1)).isoformat()
    return any(a.get("briefing_date", "")[:10] == yest and
               a.get("acknowledged_at") for a in acks)


# ---------- Action-item extraction from 1:1 notes ----------

ACTION_PATTERNS = [
    re.compile(r"\b(JB|James)\s*(to|:|will|should)\s+(.{8,200})", re.I),
    re.compile(r"\*\*([^*]{6,180}?)\*\*", re.S),   # bolded items often = todos
    re.compile(r"^\s*[-*]\s+(.{8,200})\s*$", re.M),
]


def extract_action_items(text: str, source_label: str) -> list[dict]:
    """Pull candidate JB-owned action items out of a 1:1 running-notes blob.

    These get hashed and tracked in state. If the same item reappears in a
    later 1:1 entry with "done" / strikethrough markers, it'll naturally drop
    out; otherwise it ages until manually marked done via the ack link.
    """
    found = []
    seen_norms = set()
    for pat in ACTION_PATTERNS:
        for m in pat.finditer(text):
            raw = m.group(m.lastindex).strip(" :-*\t")
            if len(raw) < 8 or len(raw) > 240:
                continue
            if not re.search(r"\b(JB|James|to|will|should|setup|send|draft|"
                             r"review|prep|aim|push|confirm|finalize)\b", raw, re.I):
                continue
            norm = re.sub(r"\s+", " ", raw.lower())
            if norm in seen_norms:
                continue
            seen_norms.add(norm)
            text_html = f"<li>{raw} <span style='color:#888;font-size:11px;'>({source_label})</span></li>"
            found.append({
                "section": "action_item",
                "key": item_key("action_item", raw),
                "text_html": text_html,
                "source": source_label,
            })
            if len(found) >= 25:
                return found
    return found


# ---------- URL verification + news dedup ----------

def verify_urls(html: str, timeout: float = 4.0) -> tuple[str, list[str]]:
    """HEAD each <a href> in the HTML; if a URL is dead or unreachable,
    strip the link (keep the anchor text) and return a list of pruned URLs.

    This catches Claude-hallucinated links and rotted sources without
    failing the whole briefing.

    Skipped URL prefixes (always-trusted, even if HEAD fails):
      - Our own GitHub Pages dashboard URLs — the script renders them
        *before* the workflow's Pages deploy step, so HEAD returns 404
        at this moment but the link will be live in ~30 sec.
      - Apps Script webhook URLs — Google's Apps Script endpoints
        sometimes 401 a bare HEAD even when the actual GET would work.
      - Google Forms / Drive viewer / Docs links — these are
        authenticated and sometimes redirect-bounce a HEAD probe.
    """
    SKIP_PREFIXES = (
        GITHUB_PAGES_BASE,
        "https://script.google.com/macros/",
        "https://docs.google.com/forms/",
        "https://docs.google.com/document/",
        "https://docs.google.com/spreadsheets/",
        "https://drive.google.com/",
        "https://mail.google.com/",
    )
    bad = []
    def check(url: str) -> bool:
        if not url.startswith(("http://", "https://")):
            return False
        if any(url.startswith(p) for p in SKIP_PREFIXES):
            return True  # always-trusted
        try:
            r = requests.head(url, timeout=timeout, allow_redirects=True)
            if r.status_code >= 400:
                # Some sites 405 HEAD but 200 GET — try a tiny GET.
                r = requests.get(url, timeout=timeout, stream=True)
                return r.status_code < 400
            return True
        except requests.RequestException:
            return False

    def repl(m):
        url, anchor = m.group(1), m.group(2)
        if check(url):
            return m.group(0)
        bad.append(url)
        return anchor  # drop the broken link, keep the text

    cleaned = re.sub(r'<a\s+href="([^"]+)"[^>]*>(.*?)</a>',
                     repl, html, flags=re.S | re.I)
    return cleaned, bad


def recent_news_headlines(state: list[dict], today: dt.date) -> list[str]:
    """Headlines of news items shown in the last NEWS_DEDUP_LOOKBACK_DAYS,
    used by the picker to avoid repeating itself."""
    cutoff = today - dt.timedelta(days=NEWS_DEDUP_LOOKBACK_DAYS)
    out = []
    for row in state:
        if row.get("section") not in ("news", "funder", "whitespace", "evidence"):
            continue
        try:
            seen = dt.datetime.strptime(row.get("last_seen", "")[:10], "%Y-%m-%d").date()
            if seen < cutoff:
                continue
        except ValueError:
            continue
        # Strip HTML, take first sentence.
        plain = re.sub(r"<[^>]+>", " ", row.get("text_html", ""))
        plain = re.sub(r"\s+", " ", plain).strip()
        if plain:
            out.append(plain[:160])
    return out


# ---------- Claude synthesis ----------

def claude() -> anthropic.Anthropic:
    # max_retries=8 so the SDK rides out per-minute TPM bucket resets on
    # 429s with exponential backoff (default of 2 isn't enough on Tier 1).
    # Also gives headroom against transient 529 overloads.
    return anthropic.Anthropic(max_retries=8)  # uses ANTHROPIC_API_KEY


def synthesize_prioritization(calendar, drive_changes, oneonone_notes,
                              inbox_msgs: list[dict] | None = None,
                              tasks_context: list[dict] | None = None,
                              journal_context: list[dict] | None = None,
                              feedback_digest: str = "") -> str:
    """Ask Claude to produce the prioritization section.

    Calendar-aware: the model is asked to cross-reference upcoming meetings
    against 1:1 action items, recent Drive activity, and inbox threads —
    surfacing "prep X before Y meeting" connections that no single input
    would reveal alone.
    """
    feedback_block = (
        f"\n\n## Recent feedback from James — bias toward what landed\n{feedback_digest}\n"
        if feedback_digest else ""
    )
    # Compact inbox for cross-referencing — we only need subject + sender +
    # snippet to spot "this thread is about the same thing as the 10 AM
    # meeting." Inbox triage still happens in its own section.
    inbox_compact = [
        {"subject": m.get("subject", ""),
         "from": m.get("from", ""),
         "kind": m.get("kind", ""),
         "snippet": m.get("snippet", "")[:200]}
        for m in (inbox_msgs or [])
    ]
    # Tasks from James's cowork task system — already ranked. Trim to
    # the fields the model needs (skip cowork's internal metadata).
    tasks_compact = [
        {"id": t.get("id", ""),
         "title": t.get("title", ""),
         "why": (t.get("why") or "")[:600],
         "urgency": t.get("urgency", ""),
         "domain": t.get("domain", ""),
         "added": t.get("added", "")}
        for t in (tasks_context or [])
    ]
    # Recent journal entries — give a sense of velocity and blockers.
    journal_compact = [
        {"timestamp": e.get("timestamp", ""),
         "completed": e.get("tasks_completed", []),
         "added": e.get("tasks_added", []),
         "energy_note": (e.get("energy_note") or "")[:200],
         "blockers": (e.get("blockers_noted") or "")[:200],
         "velocity": e.get("velocity", ""),
         "raw_quote": (e.get("raw_quote") or "")[:300]}
        for e in (journal_context or [])
    ]

    system = textwrap.dedent("""
        You are James Bedford's chief of staff at 2AI (AI for global development).
        James reports up to Katie and works closely with Sarah.

        Your job: produce a *tight* daily prioritization brief by cross-
        referencing inputs, not just summarizing each one in isolation.

        CRITICAL — calendar-aware reasoning. Before drafting, scan each
        of today's calendar events and ask:
          (1) Does any 1:1 note action item map to prep for this meeting?
              (e.g., "10 AM Board prep" + Sarah note "draft Q3 spend slide"
               → "Draft Q3 spend slide before 10 AM Board prep" is a
               priority, not just a calendar cue.)
          (2) Does any recent Drive doc match this meeting's agenda?
              (e.g., shared doc edited overnight + meeting today on the
               same topic → "Re-read X before Y" prep cue.)
          (3) Does any inbox thread reference the same project, person,
              or decision as this meeting? Flag the connection.
          (4) Does the meeting description itself contain action items
              ("Bring decision on X", "Pre-read attached") that James
              should prep for?
          (5) Does any active task from James's task system (provided
              below as "Active tasks") map onto today's calendar or
              today's inbox? If a high-urgency task lines up with a
              meeting, prioritize prep. Treat the task system's
              ranking as a strong signal — those titles + "why"
              fields encode reasoning we should respect, not override.
        Use these connections to make priorities feel inevitable, not
        invented. A priority that names a meeting + a doc + a deadline
        is far stronger than a vague "follow up on X."

        Output sections, in this order, in HTML fragments (no <html>/<body>
        wrapper):

        <h2>Top priorities today</h2>
          Numbered list of 3-5 items. Each is one line: the priority, then
          in italics one phrase on why it's the priority today — citing
          the specific cross-reference where possible (e.g., "...before
          10 AM Sarah 1:1 — she flagged this Tuesday").

        <h2>Gold-standard overreach — if you went all-in</h2>
          For 1-3 of today's top priorities, name the *ambitious* version
          of that priority. This is the "if you had 4 hours and a clear
          head" version — the move that would feel like real progress vs.
          merely "shipping the thing." Examples of the right voice:
            - Base priority: "Send retreat pre-read by EOD."
              Overreach: "Send retreat pre-read with a 3-slide vision deck
              attached — gives Katie an anchor to react to in real time."
            - Base priority: "Draft Q3 spend slide for Sarah 1:1."
              Overreach: "Draft Q3 spend slide + a 1-pager on what we'd do
              with +$200K in Q4 — turns the spend conversation into a
              fundraising conversation."

          Format as a bullet list. Each item: one line of base priority +
          one line of overreach, italicized with "<em>Overreach:</em>"
          prefix. Be specific — name the artifact, the audience, and the
          marginal benefit. If you can't find a meaningful overreach,
          skip the item; don't pad.

        <h2>Likely to slip — flag now</h2>
          Bullet list. For each: project, what evidence suggests slippage
          (commit dates from 1:1 notes, missing prerequisites, calendar
          conflicts, unanswered inbox threads), and the single action that
          would de-risk it.

        <h2>Decisions needed from James</h2>
          Bullet list of decisions surfaced in 1:1 notes, meeting prep
          notes, or inbox that are blocking others.

        <h2>Calendar prep cues</h2>
          For today's meetings only, one line each: meeting → what to
          walk in with. Reference specific docs, action items, or threads
          where applicable. Skip social events / blocked focus time.

        Be specific. Quote action items verbatim where useful. No filler, no
        "I notice that...", no preamble. If a section has nothing to say, write
        "<p><em>Nothing flagged.</em></p>".

        If a feedback digest is provided, treat it as binding: do more of what
        James rated 4-5, less of what he rated 1-2.
    """).strip()

    user = textwrap.dedent(f"""
        ## Calendar (next {CALENDAR_LOOKAHEAD_DAYS} days, including descriptions/agendas)
        {json.dumps(calendar, indent=2, default=str)}

        ## Drive activity (last {DRIVE_RECENT_LOOKBACK_HOURS}h)
        {json.dumps(
            [{"name": f["name"], "modified": f["modifiedTime"],
              "by": (f.get("lastModifyingUser") or {}).get("displayName"),
              "link": f.get("webViewLink")} for f in drive_changes],
            indent=2,
        )}

        ## 1:1 running notes — Katie (most recent entries)
        {oneonone_notes["Katie"]}

        ## 1:1 running notes — Sarah (most recent entries)
        {oneonone_notes["Sarah"]}

        ## Inbox signals (for cross-referencing with calendar events only —
        ## inbox triage runs separately, don't duplicate)
        {json.dumps(inbox_compact, indent=2) if inbox_compact else "(none)"}

        ## Active tasks (from James's cowork task system — already ranked
        ## by urgency. Trust the ranking and the "why" reasoning.)
        {json.dumps(tasks_compact, indent=2) if tasks_compact else "(none)"}

        ## Recent journal entries ({JOURNAL_LOOKBACK_DAYS}-day window —
        ## use to gauge velocity, see what's been getting done vs added,
        ## and pick up patterns from James's raw quotes / blockers)
        {json.dumps(journal_compact, indent=2) if journal_compact else "(none)"}
        {feedback_block}
    """).strip()

    msg = claude().messages.create(
        model=CLAUDE_MODEL,
        max_tokens=4000,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    return msg.content[0].text


def synthesize_whitespace(program_corpus: dict[str, list[dict]],
                          feedback_digest: str = "",
                          prefs_digest: str = "") -> str:
    """For each program area, find emerging topics public but absent from our Drive.

    Two-step: (1) summarise what we *have* been thinking about per area,
    (2) web-search what's emerging in the field, (3) diff and surface 2-4
    white-space items per area. Returns one combined HTML fragment.
    """
    feedback_block = (
        f"\n\nRecent James feedback (bias toward what landed):\n{feedback_digest}"
        if feedback_digest else ""
    )
    prefs_block = (
        f"\n\nTopic prefs (from 👍/👎): {prefs_digest}"
        if prefs_digest else ""
    )

    items_html = [
        "<h2>White-space — what the field is moving on that we're not</h2>",
        "<p style='font-size:12px;color:#888;'>"
        "Compares the last "
        f"{WHITESPACE_CORPUS_LOOKBACK_DAYS} days of 2AI Drive docs in each "
        "program area against what's emerging in the public literature. "
        "Runs weekly, on Mondays.</p>",
    ]

    for area in PROGRAM_AREAS:
        docs = program_corpus.get(area, [])
        corpus_summary = "\n\n".join(
            f"- **{d['name']}** ({d.get('modifiedTime','')[:10]})\n  "
            f"{(d.get('excerpt','') or '').strip()[:800]}"
            for d in docs[:WHITESPACE_CORPUS_PER_AREA]
        ) or "_no recent docs found in this area_"

        msg = claude().messages.create(
            model=CLAUDE_ANALYSIS_MODEL,
            max_tokens=2500,
            tools=[{
                "type": "web_search_20250305",
                "name": "web_search",
                "max_uses": 5,
            }],
            system=textwrap.dedent(f"""
                You are doing a white-space analysis for 2AI's {area} workstream.

                Step 1: Read the corpus summary below — these are the docs 2AI
                  has written/edited in the last {WHITESPACE_CORPUS_LOOKBACK_DAYS}
                  days touching this area. Note what topics, methods, and
                  geographies they cover.

                Step 2: Web-search what's been emerging in AI x {area} for LMICs
                  in the last 30 days. Prefer primary sources: lab announcements,
                  peer-reviewed papers, funder RFPs, deployment reports.

                Step 3: Surface 2-4 specific items (topics, methods, partnerships,
                  publications) where there is real public movement but no mention
                  in 2AI's recent corpus. Each item: one short paragraph + one
                  link + one line starting with "<strong>Why this is white space
                  for 2AI:</strong>".

                Output HTML fragment only (no <html>/<body> wrapper). Start with
                <h3>{area.title()}</h3>. Be ruthlessly specific — vague items
                like "AI is advancing in {area}" are useless.{feedback_block}{prefs_block}
            """).strip(),
            messages=[{"role": "user", "content":
                f"## 2AI's recent {area} corpus\n\n{corpus_summary}"
            }],
        )
        body = "".join(b.text for b in msg.content if hasattr(b, "text"))
        items_html.append(body)

    return "\n\n".join(items_html)


def critique_and_revise(draft_html: str, raw_inputs_summary: str,
                        feedback_digest: str = "") -> str:
    """Run a second Claude pass that critiques the draft and rewrites weak parts.

    The critic sees the same inputs as the synthesizer plus the draft and any
    recent feedback. It returns the *revised* HTML — not a critique on top.
    """
    system = textwrap.dedent("""
        You are the editor checking a daily briefing before it goes to James.
        Your job is to make sure it would actually be useful to him this
        morning. You do not summarise; you ship a revised version.

        Apply this rubric and silently revise:
          1. Specificity — every claim names a person, a doc, a date, or a
             measurable trigger. Strip generic statements.
          2. Action-density — every flagged item ends in something James can
             do in <30 min, or is escalated to a yes/no decision.
          3. Calibration — if a "likely to slip" claim isn't actually supported
             by evidence in the inputs, downgrade or remove it.
          4. Voice — matter-of-fact, evidence-first; no "I notice that…",
             no breathless framing, no padding sentences.
          5. James's recent feedback — if a pattern was rated 1-2, don't repeat
             it; if 4-5, lean into it.
          6. Length — if the briefing is longer than ~700 words excluding the
             news section, cut from the bottom of each section.

        Output: the full revised HTML fragment, ready to drop into the email.
        Do NOT add an "editor's note" or any meta-commentary about what you
        changed. Just ship the revised version.
    """).strip()

    user = textwrap.dedent(f"""
        ## Inputs summary
        {raw_inputs_summary}

        ## Draft to revise
        {draft_html}

        ## Recent feedback from James
        {feedback_digest or "(no feedback recorded yet)"}
    """).strip()

    msg = claude().messages.create(
        model=CLAUDE_MODEL,
        max_tokens=5000,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    return msg.content[0].text


def synthesize_tldr(prioritization_html: str, needs_count: int,
                    stale_count: int, today: dt.date) -> str:
    """One-sentence Axios-style summary for the top of the briefing.

    Reads the post-critic prioritization HTML and the inbox counts,
    returns 15-30 words of plain text (the renderer adds the badge).
    Small Claude call (~$0.02) — adds the most "morning at a glance"
    touch to the redesigned briefing.
    """
    plain = re.sub(r"\s+", " ",
                   re.sub(r"<[^>]+>", " ", prioritization_html)).strip()[:3000]
    inbox_hint = (
        f"\n\nInbox state: {needs_count} needs-reply thread"
        f"{'s' if needs_count != 1 else ''}, "
        f"{stale_count} likely-to-slip thread"
        f"{'s' if stale_count != 1 else ''}."
    )
    msg = claude().messages.create(
        model=CLAUDE_MODEL,
        max_tokens=200,
        system=textwrap.dedent("""
            You are writing the TL;DR strip at the top of James's daily
            briefing — Axios smart-brevity style. ONE sentence. 15-30 words.

            Read the inputs below. Surface the 1-2 things that matter most
            today: the must-do action, the looming decision, or the slip
            flag with the closest deadline. Tight prose: who, what, when.

            Voice:
              - No frame ("today's briefing covers", "James needs to know").
                Start with the action or the subject.
              - Specific names, dates, hours. "Mariam's start date by EOD"
                not "a pending HR decision."
              - Semicolon-joined if two things; period only if one.

            Sample voice (don't copy these — fit the actual content):
              "Mariam start date needs yes/no by EOD; Gates RFP draft 60%
               but blocked on Kanika's cyber section."
              "Three slip flags on the Q3 deck; nothing else urgent today."

            Output plain text only. No quotes, no markdown, no leading
            "TL;DR:" — the renderer adds that.
        """).strip(),
        messages=[{"role": "user", "content": plain + inbox_hint}],
    )
    return msg.content[0].text.strip().strip('"').strip("'")


def synthesize_trends(state: list[dict], today: dt.date,
                      prefs_digest: str = "") -> str:
    """Skim across the last TRENDS_LOOKBACK_DAYS of indexed items and ask
    Claude to spot patterns: emerging trends, opportunity spaces, gaps,
    underreported areas. Runs weekly (TRENDS_WEEKDAY).
    """
    cutoff = today - dt.timedelta(days=TRENDS_LOOKBACK_DAYS)
    relevant = []
    for r in state:
        if r.get("section") not in ("news", "funder", "whitespace"):
            continue
        try:
            seen = dt.datetime.strptime(r.get("last_seen", "")[:10], "%Y-%m-%d").date()
            if seen < cutoff:
                continue
        except ValueError:
            continue
        plain = re.sub(r"\s+", " ",
                       re.sub(r"<[^>]+>", " ", r.get("text_html", ""))).strip()
        if plain:
            relevant.append({
                "section": r.get("section"),
                "first_seen": r.get("first_seen"),
                "last_seen": r.get("last_seen"),
                "text": plain[:280],
            })
    if len(relevant) < 5:
        return ("<h2>Trends + opportunity spaces</h2>\n"
                "<p><em>Not enough indexed news/funder/whitespace items yet "
                f"to spot trends (have {len(relevant)}, need ≥5). Come back in "
                "a couple of weeks once the state sheet has filled up.</em></p>")

    prefs_block = (f"\n\nJames's topic preferences:\n{prefs_digest}"
                   if prefs_digest else "")

    msg = claude().messages.create(
        model=CLAUDE_ANALYSIS_MODEL,
        max_tokens=3000,
        tools=[{
            "type": "web_search_20250305",
            "name": "web_search",
            "max_uses": 6,
        }],
        system=textwrap.dedent(f"""
            You are doing pattern-recognition across {TRENDS_LOOKBACK_DAYS}
            days of items James's daily briefing has surfaced. Goal: find
            things a single-day briefing can't see.

            Read all the items below. Then web-search to validate / extend
            patterns you spot. Produce four sections, each with 2-4 specific
            items. Be concrete — name organisations, papers, geographies,
            funders, dollar amounts.

            <h3>Emerging trends (3+ datapoints converging)</h3>
              Topics that appeared multiple times across the window and where
              there's now a coherent direction of travel.

            <h3>Opportunity spaces for 2AI</h3>
              Places where the field is moving but where 2AI's current
              portfolio (per the items below + your web search of 2AI's
              public presence) has no public position. Each item: what the
              space is, why it's an opportunity, what a 2AI move could look
              like.

            <h3>Likely underreported / under-watched</h3>
              Topics that appeared only 1-2 times in the window but have
              external signal (recent paper, lab announcement, funder move)
              suggesting they deserve more attention.

            <h3>Pattern shifts since last month</h3>
              Things the field used to talk about but isn't anymore, or
              vice versa.

            Output HTML fragment only. Start with <h2>Trends + opportunity
            spaces — last {TRENDS_LOOKBACK_DAYS} days</h2>. No preamble.
            No padding sentences.{prefs_block}
        """).strip(),
        messages=[{"role": "user",
                   "content": json.dumps(relevant[-200:], indent=2)}],
    )
    return "".join(b.text for b in msg.content if hasattr(b, "text"))


def synthesize_evidence_digest(prefs_digest: str = "") -> str:
    """Twice-weekly pull of new RCTs / studies / preprints across two streams.

    Uses Claude's web_search restricted to academic domains (consensus.app
    + arxiv + biorxiv + medrxiv + Lancet AI + NEJM AI + OpenReview + METR
    + Epoch + ECMWF). One pro-message-equivalent call per stream.
    """
    prefs_block = (f"\n\nJames's topic preferences: {prefs_digest}"
                   if prefs_digest else "")

    sections = [
        '<h2>Evidence base — new RCTs, studies, preprints</h2>',
        '<p style="font-size:12px;color:#888;">'
        'Runs Tue + Thu. Two streams: AI capabilities + Weather/health × AI. '
        'Sourced via Claude web-search restricted to consensus.app + arxiv + '
        'biorxiv + medrxiv + Lancet AI + NEJM AI + OpenReview + METR + Epoch.'
        '</p>',
    ]

    for stream in EVIDENCE_STREAMS:
        msg = claude().messages.create(
            model=CLAUDE_RESEARCH_MODEL,
            max_tokens=2500,
            tools=[{
                "type": "web_search_20250305",
                "name": "web_search",
                "max_uses": 6,
                "allowed_domains": stream["domains"],
            }],
            system=textwrap.dedent(f"""
                You are doing an evidence pull for 2AI's biweekly briefing
                in the {stream['name']} stream. Search the allowed academic
                domains for papers indexed or published in the last 7 days.

                Surface {EVIDENCE_ITEMS_PER_STREAM} items. For each, output:

                <div style="border-left:3px solid #5fae5f;padding:6px 12px;margin:14px 0;">
                  <strong>TITLE</strong>
                  &nbsp;<span style="font-size:11px;color:#888;">VENUE · DATE</span>
                  <br><em>Authors:</em> last-name list (cap at 4 + "et al")
                  <br>One-sentence finding in plain language.
                  <br><em>Method / sample:</em> design + n (be precise — "RCT,
                  n=1,847, Kenya primary care" not "large study in Africa").
                  <br><em>Effect size:</em> exact number with CI if reported,
                  otherwise "not yet reported".
                  <br><em>For 2AI:</em> one line — does this update a prior
                  or open a new question? Name the workstream.
                  <br><a href="URL">primary source</a> · <a href="CONSENSUS_URL">consensus.app</a>
                </div>

                Bias toward: RCTs > non-randomised intervention studies >
                observational > preprints > position pieces. Skip anything
                older than 14 days, anything paywalled without a preprint,
                and any AI-hype piece without a concrete result.{prefs_block}
            """).strip(),
            messages=[{"role": "user", "content":
                f"Stream: {stream['name']}\nQuery: {stream['query']}"
            }],
        )
        body = "".join(b.text for b in msg.content if hasattr(b, "text"))
        sections.append(f'<h3>{stream["name"]}</h3>\n{body}')

    return "\n\n".join(sections)


def propose_news_sources_today(news_html: str, funder_html: str,
                               state: list[dict],
                               current_sources: list[str],
                               today: dt.date) -> str:
    """Scan today's news + funder deep-dive citations and recent state
    for outlets that keep showing up but aren't in the current rotation.
    Surfaces 0-3 candidates as a small section with ✅ accept / ❌ skip
    anchors that hit the existing source-action route.

    Cheap (Haiku call, ~$0.02) and runs daily — complements the heavier
    weekly source-proposer that runs Friday.
    """
    # Extract domains from <a href> in today's content + recent state.
    from urllib.parse import urlparse
    def domains_from_html(html: str) -> list[str]:
        urls = re.findall(r'<a\s+href="(https?://[^"]+)"', html, re.I)
        return [urlparse(u).netloc.replace("www.", "") for u in urls if u]
    today_domains = (domains_from_html(news_html) +
                     domains_from_html(funder_html))
    cutoff = today - dt.timedelta(days=7)
    recent_domains: list[str] = []
    for r in state:
        if r.get("section") not in ("news", "funder", "evidence"):
            continue
        try:
            seen = dt.datetime.strptime(r.get("last_seen", "")[:10], "%Y-%m-%d").date()
            if seen < cutoff:
                continue
        except ValueError:
            continue
        recent_domains += domains_from_html(r.get("text_html", ""))
    # Tally
    from collections import Counter
    counts = Counter(today_domains + recent_domains)
    # Filter: drop known rotation members + obvious aggregators / search.
    SKIP_DOMAINS = {
        "google.com", "twitter.com", "x.com", "youtube.com",
        "linkedin.com", "facebook.com", "wikipedia.org",
        "github.com", "medium.com", "substack.com",  # too generic
    }
    rotation = {s.lower() for s in current_sources}
    candidates = []
    for domain, count in counts.most_common(20):
        if count < 2:
            break  # tally is sorted desc
        if domain in SKIP_DOMAINS or any(s in domain for s in rotation):
            continue
        if any(domain.endswith(skip) for skip in SKIP_DOMAINS):
            continue
        candidates.append({"domain": domain, "count": count})
    if not candidates:
        return ""

    msg = claude().messages.create(
        model=DEDUP_MODEL,  # Haiku — classification work, cheap
        max_tokens=900,
        system=textwrap.dedent("""
            You are reviewing news outlets that keep appearing in James's
            daily briefing citations but aren't in his tracking rotation.
            From the candidates below, pick 0-3 that are worth proposing
            as new sources to follow. Use these criteria:

            ✓ Substantive: original reporting / analysis on AI, global
              development, funder behavior, or AI-for-LMIC work
            ✓ Reasonably authoritative (think-tanks, sector publications,
              quality blogs, academic outlets)
            ✗ Skip: generic news aggregators, broad outlets like NYT/BBC
              that already get covered organically, paywalled sites,
              corporate marketing sites, social media

            Output JSON only, no preamble, no markdown fences:
              {"sources": [
                {"domain": "...", "name": "Human-readable name",
                 "why": "one phrase on why it's worth tracking"},
                ...
              ]}
            If none of the candidates pass the bar, return {"sources": []}.
        """).strip(),
        messages=[{"role": "user", "content": json.dumps(candidates, indent=2)}],
    )
    raw = msg.content[0].text
    m = re.search(r'\{[^{}]*"sources"[^{}]*\[.*?\][^{}]*\}', raw, re.S)
    if not m:
        return ""
    try:
        result = json.loads(m.group(0))
    except json.JSONDecodeError:
        return ""
    picks = result.get("sources", [])
    if not picks:
        return ""

    # Render each pick with accept/reject anchors that hit the existing
    # source-action route (appends to the `sources` tab on accept).
    items_html = ['<h2>Sources spotted today — worth tracking?</h2>',
                  '<p style="font-size:12px;color:#888;margin:0 0 12px 0;">'
                  'Outlets cited in today\'s news that aren\'t yet in your '
                  'rotation. Accept to add to tomorrow\'s news picker.</p>']
    new_source_rows: list[dict] = []
    for pick in picks[:3]:
        domain = pick.get("domain", "")
        name = pick.get("name", domain)
        why = pick.get("why", "")
        # Stable ID so the accept/reject route can update the row.
        source_id = f"daily-{today.isoformat()}-{hashlib.sha1(domain.encode()).hexdigest()[:8]}"
        new_source_rows.append({
            "source_id": source_id,
            "proposed_at": dt.datetime.now().isoformat(),
            "status": "pending",
            "source_name": name,
            "source_url": f"https://{domain}",
            "source_query": f"{name} AI announcements last 7 days",
        })
        accept_url = _source_action_url(source_id, "accept")
        reject_url = _source_action_url(source_id, "reject")
        items_html.append(
            f'<div style="margin:10px 0;padding:10px 14px;'
            f'background:#f8fafc;border-left:3px solid #475569;'
            f'border-radius:4px;">'
            f'<strong>{name}</strong> '
            f'<span style="color:#6b7280;font-size:12px;">({domain})</span>'
            f'<br><em>{why}</em>'
            f'<div style="margin-top:6px;font-size:12px;">'
            f'<a href="{accept_url}" style="color:#15803d;'
            f'border-bottom:1px dotted #15803d;text-decoration:none;'
            f'margin-right:14px;">✅ accept</a>'
            f'<a href="{reject_url}" style="color:#dc2626;'
            f'border-bottom:1px dotted #dc2626;text-decoration:none;">'
            f'❌ skip</a></div></div>'
        )
    # Mutate state-write list to be picked up by main's append_source_rows.
    propose_news_sources_today._proposed = new_source_rows
    return "\n".join(items_html)


def propose_2ai_ideas(news_html: str, program_corpus: dict[str, list[dict]] | None,
                      prefs_digest: str = "") -> str:
    """1-3 concrete ideas 2AI could build/test/explore based on today's
    news + recent AI releases vs. what 2AI already works on (Drive
    corpus). Renders as a section card with send-to-tasks buttons per
    idea. Sonnet + web_search, ~$0.30/day.
    """
    # Compact corpus summary by program area.
    corpus_summary = ""
    if program_corpus:
        parts = []
        for area, docs in program_corpus.items():
            if not docs:
                continue
            titles = [d.get("name", "") for d in docs[:8]]
            parts.append(f"**{area.title()}**: {'; '.join(titles)}")
        corpus_summary = "\n".join(parts)
    if not corpus_summary:
        corpus_summary = ("(2AI's recent Drive corpus wasn't pulled today — "
                          "base ideas on the news + general knowledge of "
                          "2AI's AI-for-LMIC focus.)")
    # Extract a digest of today's news for context.
    news_plain = re.sub(r"\s+", " ",
                        re.sub(r"<[^>]+>", " ", news_html or "")).strip()[:4000]
    prefs_block = (f"\n\nJames's topic preferences (from 👍/👎): {prefs_digest}"
                   if prefs_digest else "")

    msg = claude().messages.create(
        model=CLAUDE_RESEARCH_MODEL,
        max_tokens=2500,
        tools=[{
            "type": "web_search_20250305",
            "name": "web_search",
            "max_uses": 4,
        }],
        system=textwrap.dedent(f"""
            You are proposing concrete things 2AI could build / test /
            explore THIS WEEK based on (a) today's news in the briefing,
            (b) what 2AI has been working on recently (corpus below),
            and (c) any major AI releases / capability announcements you
            find via web search in the last 7 days.

            Output 1-3 ideas. Each idea must be:
              • CONCRETE: name the artifact (a 1-pager, a prototype, a
                pilot, a memo, an outreach email), the audience (who
                inside or outside 2AI it goes to), and the next step
                (what James does in the next 30 min if he wants to take
                it on).
              • DIFFERENTIATED: not something 2AI already has in flight
                (cross-check against the corpus titles).
              • TIMELY: tied to something that shipped or changed in
                the last 7 days, not evergreen.
              • RIGHT-SIZED: doable in 1-5 working days, not a quarter.

            Voice: matter-of-fact, evidence-first, no breathless framing.
            No "consider exploring" — pick a stance and recommend.

            Output as HTML fragment, no <html>/<body> wrapper. Start with
            <h2>Implementation ideas — what 2AI could ship this week</h2>.
            For each idea, format as:
              <ul><li>
                <strong>[Title]</strong> — one short paragraph (2-3
                sentences) with the artifact, audience, and next step.
                <a href="URL">primary source</a> for the trigger.
                <em>Effort:</em> 1-2 days / 3-5 days etc.
              </li></ul>

            2AI'S RECENT CORPUS (don't propose anything already in flight):
            {corpus_summary}

            TODAY'S BRIEFING (for context — what's already on James's radar):
            {news_plain[:2000]}
            {prefs_block}
        """).strip(),
        messages=[{"role": "user",
                   "content": "Propose this week's implementation ideas."}],
    )
    return "".join(b.text for b in msg.content if hasattr(b, "text"))


def synthesize_publisher_landscape(prefs_digest: str = "") -> str:
    """Once-a-month deep look at what peer publishers are putting out:
    each org's focus + gaps, plus sector-wide publishing gaps.

    Runs a single big Claude call with agentic web search across all
    PEER_PUBLISHERS; the model decides how many searches it needs.
    """
    pubs_list = "\n".join(f"- {p['name']} — {p['url']}" for p in PEER_PUBLISHERS)
    prefs_block = (f"\n\nJames's topic preferences: {prefs_digest}"
                   if prefs_digest else "")

    msg = claude().messages.create(
        model=CLAUDE_ANALYSIS_MODEL,
        max_tokens=4500,
        tools=[{
            "type": "web_search_20250305",
            "name": "web_search",
            "max_uses": 18,
        }],
        system=textwrap.dedent(f"""
            You are profiling the AI-for-development publishing landscape
            for 2AI's monthly trends review.

            For each peer publisher listed below, web-search their recent
            (last {PUBLISHER_LANDSCAPE_LOOKBACK_DAYS} days) publications:
            blog posts, research papers, working papers, podcasts, newsletters.
            Then produce four sections in HTML fragments. Be specific —
            name pieces, geographies, methods.

            <h3>Per-publisher focus profiles</h3>
              For each publisher, two lines:
                <strong>Name.</strong> Currently focused on: X, Y, Z (with
                  one example link). Apparent gaps vs. their historical
                  range: A, B.
              Order by how active they've been; skip any that have published
              nothing in the window.

            <h3>Where publishers cluster</h3>
              2-4 themes that multiple peer orgs are converging on right now.
              For each: which orgs, what the angle is, why it matters to 2AI.

            <h3>Where individual publishers are uniquely positioned</h3>
              2-4 cases where one org owns a topic no one else is touching.
              Why they own it; what 2AI can learn from their access.

            <h3>Sector-wide publishing gaps</h3>
              3-5 topics where the field SHOULD be publishing but nobody is.
              Each: the gap, why it persists (no funder? no incentives? no
              data?), and whether 2AI could plausibly lead.

            Output HTML fragment only. Start with <h2>Peer publisher landscape
            — last {PUBLISHER_LANDSCAPE_LOOKBACK_DAYS} days</h2>. No padding.

            PUBLISHERS TO PROFILE:
            {pubs_list}{prefs_block}
        """).strip(),
        messages=[{"role": "user",
                   "content": "Begin landscape analysis."}],
    )
    return "".join(b.text for b in msg.content if hasattr(b, "text"))


def propose_new_sources(current_sources: list[str],
                        already_proposed: list[dict],
                        today: dt.date) -> tuple[str, list[dict]]:
    """Web-search for high-quality AI / global-dev news sources not already
    on your watchlist. Returns (HTML fragment with accept/reject buttons,
    list of proposed-source rows to append to the `sources` tab).

    `current_sources`: names + URLs already in the regular rotation.
    `already_proposed`: rows from the sources tab so we don't re-propose
    things James already accepted or rejected.
    """
    skip_list = "\n".join(f"- {s}" for s in current_sources) or "(none yet)"
    already = "\n".join(
        f"- {p.get('source_name','')} ({p.get('status','')})"
        for p in already_proposed
    ) or "(none yet)"

    msg = claude().messages.create(
        model=CLAUDE_RESEARCH_MODEL,
        max_tokens=2500,
        tools=[{
            "type": "web_search_20250305",
            "name": "web_search",
            "max_uses": 6,
        }],
        system=textwrap.dedent(f"""
            You are scouting for new AI / global-development news sources
            for 2AI's daily briefing. Web-search for candidates: Substacks,
            blogs, research-group sites, journals, podcasts that publish
            high-quality work on AI for LMIC health / agriculture / weather /
            education, AI safety, AI labs' global-affairs work, AI-for-good
            philanthropy, or LMIC AI policy.

            Skip anything already in the regular watchlist or already
            proposed (lists below). Find {SOURCES_PROPOSE_N} candidates.

            For each, output strictly this format:

            <div style="border-left:3px solid #1a5fb4;padding:6px 12px;margin:14px 0;">
              <strong>NAME</strong> — <a href="URL">URL</a><br>
              <em>Why it's high-signal:</em> one or two sentences.<br>
              <em>What it would add:</em> one sentence on coverage 2AI
              currently lacks.<br>
              <span data-source-id="STABLE_ID">[buttons]</span>
            </div>

            STABLE_ID should be a short slug derived from NAME (kebab-case,
            no spaces, no special chars). The [buttons] placeholder will be
            replaced by the harness — do not write actual links.

            Output HTML fragment only. Start with <h2>New source candidates —
            add to watchlist?</h2>. No padding.

            ALREADY IN ROTATION:
            {skip_list}

            ALREADY PROPOSED (do not repeat):
            {already}
        """).strip(),
        messages=[{"role": "user",
                   "content": "Find candidates."}],
    )
    raw = "".join(b.text for b in msg.content if hasattr(b, "text"))

    # Replace [buttons] placeholders with real accept/reject links and
    # collect the proposed-source rows for state.
    proposed_rows: list[dict] = []

    def buttonize(m):
        slug = m.group(1)
        proposed_rows.append({
            "source_id": slug,
            "proposed_at": today.isoformat(),
            "status": "proposed",
        })
        return (
            f'<a style="background:#2c7b2c;color:#fff;padding:3px 9px;'
            f'border-radius:3px;font-size:12px;text-decoration:none;" '
            f'href="{_source_action_url(slug, "accept")}">👍 add to watchlist</a> '
            f'&nbsp;'
            f'<a style="background:#999;color:#fff;padding:3px 9px;'
            f'border-radius:3px;font-size:12px;text-decoration:none;" '
            f'href="{_source_action_url(slug, "reject")}">👎 skip</a>'
        )

    annotated = re.sub(
        r'<span data-source-id="([^"]+)">\[buttons\]</span>',
        buttonize, raw,
    )
    return annotated, proposed_rows


def append_source_rows(creds, new_rows: list[dict]):
    """Append newly-proposed source rows to the `sources` tab."""
    if not (ACK_SHEET_ID and new_rows):
        return
    values = [
        [r.get("source_id", ""), r.get("proposed_at", ""),
         r.get("status", "proposed"), "", "", ""]
        for r in new_rows
    ]
    try:
        _sheets(creds).spreadsheets().values().append(
            spreadsheetId=ACK_SHEET_ID,
            range="sources!A:F",
            valueInputOption="RAW",
            body={"values": values},
        ).execute()
    except Exception as e:
        print(f"[sources] couldn't append: {e}")


def extract_items_from_html(html: str, section_map: dict[str, str]) -> list[dict]:
    """Parse the synthesizer's HTML into tracked items.

    section_map maps the H2/H3 heading text → state-section name
    (e.g. {"Top priorities today": "priority", "Likely to slip": "slip"}).
    Only headings whose text matches a key in section_map are indexed.
    """
    items = []
    pattern = re.compile(
        r"<h[23][^>]*>\s*([^<]+?)\s*</h[23]>(.*?)(?=<h[23]\b|\Z)",
        re.S | re.I,
    )
    for m in pattern.finditer(html):
        heading = m.group(1).strip()
        body = m.group(2)
        # Match heading prefix-wise: "Likely to slip — flag now" should match "Likely to slip".
        section = None
        for prefix, sect in section_map.items():
            if heading.lower().startswith(prefix.lower()):
                section = sect
                break
        if not section:
            continue
        for li in re.findall(r"<li[^>]*>(.*?)</li>", body, re.S | re.I):
            text = re.sub(r"\s+", " ", li).strip()
            if len(text) < 8:
                continue
            items.append({
                "section": section,
                "key": item_key(section, text),
                "text_html": f"<li>{text}</li>",
                "source": "synth",
            })
    return items


def synthesize_inbox_triage(
    messages: list[dict],
    oneonone_notes: dict[str, str] | None = None,
    calendar: list[dict] | None = None,
) -> str:
    """Two-bucket inbox triage with inline draft replies for the
    medium/high-complexity items that need decision context.

    'Reply / decide' items that meet the draft gate (real decision, more
    than one-liner reply needed) get an inline draft reply that pulls in
    relevant context from 1:1 running notes + upcoming calendar events
    so James doesn't have to look those up before reading the draft.

    Simple confirms, scheduling, FYI, and acks don't get a draft (per
    the gate in the system prompt). 'Likely to slip through' items
    never get drafts — they're reminders, not action-ready bullets.
    """
    if not messages:
        return "<h2>Inbox — needs you</h2>\n<p><em>Inbox is clear.</em></p>"
    needs_you = [m for m in messages if m.get("kind") == "needs_you"]
    stale = [m for m in messages if m.get("kind") == "stale"]
    if not needs_you and not stale:
        return "<h2>Inbox — needs you</h2>\n<p><em>Inbox is clear.</em></p>"

    # Build the context block (1:1 notes + upcoming calendar) so the
    # model can ground drafts in James's broader picture. Trimmed to
    # keep token budget sane.
    context_lines = ["\n\n## Context for grounding draft replies"]
    if oneonone_notes:
        for name, text in oneonone_notes.items():
            context_lines.append(f"\n### Recent {name} 1:1 notes\n{(text or '')[:2000]}")
    if calendar:
        cal_compact = [
            {"summary": e.get("summary", ""),
             "start": e.get("start", ""),
             "attendees": (e.get("attendees") or [])[:5],
             "description": (e.get("description") or "")[:400]}
            for e in calendar[:15]
        ]
        context_lines.append(
            f"\n### Upcoming calendar (next 7 days)\n{json.dumps(cal_compact, indent=2)}"
        )
    context_block = "\n".join(context_lines) if len(context_lines) > 1 else ""

    user_payload = json.dumps(
        {"needs_you": needs_you, "stale": stale}, indent=2
    ) + context_block

    msg = claude().messages.create(
        model=CLAUDE_MODEL,
        max_tokens=4000,
        system=textwrap.dedent("""
            You are triaging James's inbox into two buckets AND drafting
            reply suggestions for the items that need them. Output a
            tight HTML fragment starting with <h2>Inbox — needs you</h2>.

            Two sub-sections, in this order:

            <h3>Reply / decide</h3>
              Recent threads where someone wants something from James:
              an explicit question, a decision, an approval, a
              stalled-without-him action. Skip pure FYI / newsletters /
              automated mail (do NOT surface them at all). Wrap items
              in <ul>...</ul>. Each item:
                <li><a href="LINK">Subject</a> — sender → recommended
                next action.
                [optional inline draft reply — see gate below]
                </li>

              The "recommended next action" must be concrete and short:
              "Reply yes/no on Mariam start date", "Forward to Shereen",
              "Decline the meeting", "30-sec ack reply". No vague
              "consider replying".

              ===== DRAFT-REPLY GATE — STRICT =====
              Embed a draft reply ONLY when BOTH are true:
                (1) The reply requires medium/high complexity — it
                    needs reasoning, weighing trade-offs, or explaining
                    a decision. Not a one-liner.
                (2) There's a real decision in play — a substantive
                    choice James is making, not a confirmation,
                    scheduling, or acknowledgment.

              Items that GET a draft (examples):
                ✓ "Should we extend Mariam's start date to June 15?"
                ✓ "Here's the draft RFP — thoughts?"
                ✓ "We're proposing X for the retreat agenda — your call"
                ✓ "Worth pushing back on Z, or accept as-is?"

              Items that DON'T get a draft (skip the draft, still list
              the item):
                ✗ "Are you free Tues 3pm?" (pure scheduling)
                ✗ "Confirming our 3pm" (pure FYI/ack)
                ✗ "Thanks!" / "Got it" (no action)
                ✗ Obvious yes/no with no reasoning needed

              When you DO draft a reply, format it inline like this
              (inline styles only — email clients vary on <style>):
                <div style="margin-top:8px;padding:10px 14px;
                background:#f0f9ff;border-left:3px solid #0e7490;
                border-radius:4px;font-size:13px;color:#1f2937;">
                <div style="font-size:10px;text-transform:uppercase;
                letter-spacing:1px;color:#0e7490;font-weight:700;
                margin-bottom:6px;">Draft reply</div>
                <div>[the draft body, 2-4 sentences]</div>
                </div>

              The draft should:
                - Sound like James: matter-of-fact, evidence-first,
                  warm-but-direct, no breathless framing or over-
                  apologizing, no corporate filler ("circling back",
                  "wanted to flag")
                - Be 2-4 sentences
                - Reference relevant context from the 1:1 notes /
                  upcoming calendar / James's pattern of work where it
                  strengthens the reply. Example: "Per Sarah's Tuesday
                  note we're locking retreat dates by Friday —
                  extending Mariam to June 15 would push HR onboarding
                  inside that window. Let's stick to June 1."
                - End with a clear next step or decision

            <h3>Likely to slip through</h3>
              Older threads (3-14 days) where James was addressed but
              hasn't replied. Same skip filter as above for newsletters,
              automated meeting notes (Gemini / Otter / Granola), system
              confirmations (Turn.io / Stripe / SaaS notices), calendar
              invites with no question, and anything James has clearly
              already handled outside email.

              NO draft replies in this section — these are reminders.
              These items need a brief reminder of what they were about
              because they're not fresh. Format:
                <ul><li><a href="LINK">Subject</a> — sender, Nd ago →
                what they wanted in one short phrase + recommended next
                action.</li></ul>
              Order by age, oldest first. Use `age_days` for N.

            If a sub-section ends up empty after filtering, omit its
            <h3> entirely. If BOTH end up empty: <p><em>Inbox is clear.
            </em></p>

            No preamble, no commentary, no padding sentences. Output
            HTML fragment only, no <html>/<body> wrapper.
        """).strip(),
        messages=[{"role": "user", "content": user_payload}],
    )
    return msg.content[0].text


def synthesize_funder_watchlist(recent_headlines: list[str],
                                prefs_digest: str = "") -> str:
    """Tier-0 daily check across FUNDER_WATCHLIST. Always runs, never skipped."""
    dedup_block = (
        "\n\nSkip anything substantively covered already:\n- "
        + "\n- ".join(recent_headlines[-30:])
        if recent_headlines else ""
    )
    prefs_block = (
        f"\n\nJames's topic preferences (from 👍/👎 votes):\n{prefs_digest}"
        if prefs_digest else ""
    )
    items_html = []
    for i, f in enumerate(FUNDER_WATCHLIST):
        # Pace web-search calls to stay under Sonnet's TPM bucket (each call
        # pulls ~5-10K tokens of search context). 20s between iterations keeps
        # us under Tier 1's 30K ITPM with headroom. max_retries on the client
        # is the safety net if a single call goes long.
        if i > 0:
            time.sleep(20)
        msg = claude().messages.create(
            model=CLAUDE_RESEARCH_MODEL,
            max_tokens=1200,
            tools=[{
                "type": "web_search_20250305",
                "name": "web_search",
                "max_uses": 3,
            }],
            system=textwrap.dedent(f"""
                You are scanning {f['name']} for moves in the last 7 days that
                matter to 2AI fundraising or peer landscape. Search the web.

                If nothing material has happened, respond with the single line
                "<p><em>No material updates from {f['name']} in the last 7 days.</em></p>"
                and stop. Do NOT pad with old news.

                If something has happened: one short paragraph (3-5 sentences),
                one inline link to the primary source, end with one line:
                <strong>So what for 2AI:</strong> [action or watchpoint].

                Output HTML fragment only.{dedup_block}{prefs_block}
            """).strip(),
            messages=[{"role": "user", "content":
                f"Funder: {f['name']}\nQuery: {f['query']}\nHome page: {f['url']}"
            }],
        )
        body = "".join(b.text for b in msg.content if hasattr(b, "text"))
        items_html.append(f"<h3>{f['name']}</h3>\n{body}")
    return "<h2>Funder watchlist</h2>\n" + "\n\n".join(items_html)


def synthesize_news_briefing(topics_text: str,
                             recent_headlines: list[str] | None = None,
                             prefs_digest: str = "") -> str:
    """Pick top topics, run deep research with web search, return HTML fragment."""
    dedup_block = (
        "\n\nDo NOT pick topics where the following items have already been "
        "covered in the last 7 days:\n- " + "\n- ".join(recent_headlines[-30:])
        if recent_headlines else ""
    )
    prefs_block = (
        f"\n\nJames's topic preferences (👍/👎 history). Treat as binding "
        f"bias on today's picks:\n{prefs_digest}"
        if prefs_digest else ""
    )
    # First pass: ask Claude to pick today's N research targets from the sheet.
    picker = claude().messages.create(
        model=CLAUDE_MODEL,
        max_tokens=1500,
        system=(
            "You are picking today's deep-research targets for 2AI from a "
            "monitoring topics sheet. Return ONLY a JSON array of "
            f"{NEWS_DEEP_DIVE_TOPICS} objects, each with: "
            '{"topic": str, "query": str, "why_2ai_cares": str}. '
            "Bias toward Tier 1 / Tier 2 and toward topics where things have "
            "actually moved in the last 7 days. `query` should be a tight "
            "web-search query." + dedup_block + prefs_block
        ),
        messages=[{"role": "user", "content": topics_text[:8000]}],
    )
    raw = picker.content[0].text
    m = re.search(r"\[.*\]", raw, re.S)
    targets = json.loads(m.group(0)) if m else []

    # Second pass: for each target, deep research with web search tool.
    items_html = []
    for i, t in enumerate(targets):
        # Pace to stay under the per-minute TPM bucket — see funder loop for
        # the same reasoning. Each deep-dive is heavier (max_uses=5 web
        # searches) so sleep slightly longer.
        if i > 0:
            time.sleep(25)
        research = claude().messages.create(
            model=CLAUDE_RESEARCH_MODEL,
            max_tokens=2500,
            tools=[{
                "type": "web_search_20250305",
                "name": "web_search",
                "max_uses": 5,
            }],
            system=(
                "You are doing a deep-research pass for a 2AI daily briefing. "
                "Search the web for the most recent (last 7 days) developments "
                "on the topic. Return a 4-7 sentence briefing in 2AI's house "
                "voice: matter-of-fact, evidence-first, no breathless framing. "
                "End with one line: <strong>So what for 2AI:</strong> [action "
                "or watchpoint]. Include 1-3 inline links to primary sources "
                "as <a href=...>...</a>. Output HTML fragment only — no "
                "<html>/<body> wrapper, no markdown."
            ),
            messages=[{"role": "user", "content":
                f"Topic: {t['topic']}\nQuery: {t['query']}\n"
                f"Why 2AI cares: {t['why_2ai_cares']}"
            }],
        )
        # web_search may return multiple text blocks; concat.
        body = "".join(b.text for b in research.content if hasattr(b, "text"))
        items_html.append(f"<h3>{t['topic']}</h3>\n{body}")

    return "<h2>News briefing — deep dives</h2>\n" + "\n\n".join(items_html)


# ---------- Render ----------

def _ack_url(briefing_date: dt.date, item_keys: list[str] | None = None) -> str:
    """Build the Apps Script ack URL the email links hit."""
    if not ACK_WEBHOOK_URL:
        return "#"
    q = {"date": briefing_date.isoformat()}
    if item_keys:
        q["keys"] = ",".join(item_keys)
    return f"{ACK_WEBHOOK_URL}?{urllib.parse.urlencode(q)}"


def _vote_url(briefing_date: dt.date, key: str, direction: str) -> str:
    """👍 / 👎 link target — writes to the votes tab via the same webhook."""
    if not ACK_WEBHOOK_URL:
        return "#"
    q = {"date": briefing_date.isoformat(), "key": key, "vote": direction}
    return f"{ACK_WEBHOOK_URL}?{urllib.parse.urlencode(q)}"


THUMBS_TEMPLATE = (
    '<div class="thumbs" style="margin-top:10px;font-size:11.5px;color:#6b7280;">'
    'Tune tomorrow: '
    '<a href="{up}" style="display:inline-block;background:#f3f4f6;color:#374151;'
    'padding:3px 10px;border-radius:12px;border:1px solid #e5e7eb;margin:0 3px;'
    'text-decoration:none;font-weight:500;">👍 more like this</a>'
    '<a href="{down}" style="display:inline-block;background:#f3f4f6;color:#374151;'
    'padding:3px 10px;border-radius:12px;border:1px solid #e5e7eb;margin:0 3px;'
    'text-decoration:none;font-weight:500;">👎 less like this</a>'
    '</div>'
)


def annotate_topics_h3(html: str, section: str,
                       today: dt.date) -> tuple[str, list[dict]]:
    """Inject 👍/👎 after each <h3> block. Used for news + funder where each
    h3 = one topic. Also returns indexable items for the state sheet.
    """
    items: list[dict] = []
    pattern = re.compile(
        r"(<h3[^>]*>\s*([^<]+?)\s*</h3>)(.*?)(?=<h3\b|\Z)",
        re.S | re.I,
    )

    def replace(m):
        h3_full, heading, body = m.group(1), m.group(2).strip(), m.group(3)
        plain_body = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body)).strip()
        key = item_key(section, f"{heading} {plain_body[:160]}")
        if ACK_WEBHOOK_URL:
            thumbs = THUMBS_TEMPLATE.format(
                up=_vote_url(today, key, "up"),
                down=_vote_url(today, key, "down"),
            )
            new_block = h3_full + body.rstrip() + "\n" + thumbs + "\n"
        else:
            new_block = m.group(0)
        items.append({
            "section": section, "key": key, "source": "synth",
            "text_html": f"<p><strong>{heading}.</strong> {plain_body[:200]}…</p>",
            # Saved so apply_semantic_dedup can surgically remove this
            # block from the rendered section HTML if it dupes a
            # historical item.
            "rendered_block": new_block,
        })
        return new_block

    return pattern.sub(replace, html), items


def annotate_topics_li(html: str, section: str,
                       today: dt.date) -> tuple[str, list[dict]]:
    """Inject 👍/👎 inside each <li> block. Used for whitespace where each li
    is one discrete topic."""
    items: list[dict] = []
    pattern = re.compile(r"<li([^>]*)>(.*?)</li>", re.S | re.I)

    def replace(m):
        attrs, body = m.group(1), m.group(2)
        plain = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body)).strip()
        if len(plain) < 8:
            return m.group(0)
        key = item_key(section, plain)
        if ACK_WEBHOOK_URL:
            thumbs = THUMBS_TEMPLATE.format(
                up=_vote_url(today, key, "up"),
                down=_vote_url(today, key, "down"),
            )
            new_block = f"<li{attrs}>{body.rstrip()} {thumbs}</li>"
        else:
            new_block = m.group(0)
        items.append({
            "section": section, "key": key, "source": "synth",
            "text_html": f"<li>{body}</li>",
            "rendered_block": new_block,
        })
        return new_block

    return pattern.sub(replace, html), items


def _task_proposal_url(today: dt.date, key: str, title: str,
                       urgency: str, section: str) -> str:
    """URL to the Apps Script task_proposal route. Embeds title + key +
    urgency hint so a click writes a complete row to the task_proposals
    tab — your cowork session can pick it up on its next run."""
    if not ACK_WEBHOOK_URL:
        return "#"
    q = {
        "task_proposal": title[:200],
        "key": key, "urgency": urgency, "section": section,
        "date": today.isoformat(),
    }
    return f"{ACK_WEBHOOK_URL}?{urllib.parse.urlencode(q)}"


def _ignore_url(today: dt.date, key: str, section: str) -> str:
    """URL fired by the '✕ not a priority' / 'not a decision' anchors.
    Hits the existing ack-route (writes to `done_keys`) so the item
    won't carry forward. The dashboard JS additionally fires a 👎 vote
    on the same key so the synth biases away from re-surfacing similar
    items. Email-based clicks only get the mark-done behavior (no JS).
    """
    if not ACK_WEBHOOK_URL:
        return "#"
    q = {
        "keys": key, "date": today.isoformat(),
        "kind": "ignore",      # consumed by dashboard JS; ignored by Apps Script
        "section": section,
    }
    return f"{ACK_WEBHOOK_URL}?{urllib.parse.urlencode(q)}"


def _action_widgets(buttons: list[str], today: dt.date, key: str,
                    plain_title: str, section: str) -> str:
    """Render the inline button strip after a list-item body. Each button
    is an <a href="webhook?..."> — emails open them via auto-close tab;
    the dashboard JS converts them to background fetches.

    Supported button keys: 'ignore', 'task'. Comment buttons are added
    by the dashboard JS at render time (they need an inline textarea, no
    point rendering in email since email can't host that).
    """
    if not ACK_WEBHOOK_URL or not buttons:
        return ""
    parts: list[str] = []
    section_label = {"priority": "priority", "decision": "decision",
                     "inbox": "thread"}.get(section, "item")
    btn_style = (
        'font-size:11px;color:#6b7280;margin-left:8px;'
        'border-bottom:1px dotted #9ca3af;text-decoration:none;'
    )
    if "ignore" in buttons:
        parts.append(
            f'<a href="{_ignore_url(today, key, section)}" '
            f'style="{btn_style}">✕ not a {section_label}</a>'
        )
    if "task" in buttons:
        urgency = "high" if section in ("priority", "inbox") else "medium"
        parts.append(
            f'<a href="{_task_proposal_url(today, key, plain_title, urgency, section)}" '
            f'style="{btn_style}">📌 send to tasks</a>'
        )
    if not parts:
        return ""
    return ('<span class="action-widgets">'
            + "".join(parts) + "</span>")


# Section-detection prefixes for annotate_prioritization. Matched
# case-insensitively against the h2 heading text. The trailing tuple
# is (section_slug, [button_types]) — empty list = index but no buttons.
_PRIO_SECTION_RULES = [
    ("Top priorities",            ("priority", ["ignore", "task"])),
    ("Gold-standard overreach",   ("priority", ["task"])),   # overreach: aspirational, can't really "ignore"
    ("Likely to slip",            ("slip",     [])),         # has action embedded; skip buttons
    ("Decisions needed",          ("decision", ["ignore", "task"])),
    ("Calendar prep",             ("priority", [])),         # ephemeral, re-derived daily
]


def annotate_prioritization(html: str, today: dt.date) -> tuple[str, list[dict]]:
    """Walk the prioritization HTML, identify each <h2> sub-section, and
    for matched sections: (a) inject inline action buttons inside each
    <li>, and (b) index those items into the items list with the right
    section slug + stable key.

    Replaces the old `extract_items_from_html(prioritization, ...)` call —
    indexing now happens here so the keys match the annotated text.
    """
    items: list[dict] = []
    # Split into chunks at every <h2>...</h2> boundary, preserving the
    # h2 chunks so we can use them to determine the current section.
    sections = re.split(r'(<h2[^>]*>.*?</h2>)', html, flags=re.S | re.I)
    current_rule: tuple[str, list[str]] | None = None
    out: list[str] = []
    for chunk in sections:
        if re.match(r'<h2[^>]*>', chunk, re.I):
            heading = re.sub(r"<[^>]+>", "", chunk).strip()
            current_rule = None
            for prefix, rule in _PRIO_SECTION_RULES:
                if heading.lower().startswith(prefix.lower()):
                    current_rule = rule
                    break
            out.append(chunk)
            continue
        if not current_rule:
            out.append(chunk)
            continue
        section, buttons = current_rule
        # Annotate <li> items in this chunk.
        def _replace_li(m):
            attrs, body = m.group(1), m.group(2)
            plain = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body)).strip()
            if len(plain) < 8:
                return m.group(0)
            key = item_key(section, plain)
            widgets = _action_widgets(buttons, today, key, plain, section)
            new_block = f"<li{attrs}>{body.rstrip()}{widgets}</li>"
            items.append({
                "section": section, "key": key, "source": "synth",
                "text_html": f"<li>{body}</li>",
                "rendered_block": new_block,
            })
            return new_block
        chunk = re.sub(r'<li([^>]*)>(.*?)</li>', _replace_li,
                       chunk, flags=re.S | re.I)
        out.append(chunk)
    return "".join(out), items


def annotate_inbox(html: str, today: dt.date) -> tuple[str, list[dict]]:
    """Inject '📌 send to tasks' buttons inside each <li> in any inbox
    sub-section (reply/decide, likely-to-slip). No 'ignore' — inbox
    items are implicitly dismissed by not replying. Indexes items with
    section='inbox'."""
    items: list[dict] = []
    pattern = re.compile(r'<li([^>]*)>(.*?)</li>', re.S | re.I)

    def replace(m):
        attrs, body = m.group(1), m.group(2)
        plain = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body)).strip()
        if len(plain) < 8:
            return m.group(0)
        key = item_key("inbox", plain)
        widgets = _action_widgets(["task"], today, key, plain[:150], "inbox")
        new_block = f"<li{attrs}>{body.rstrip()}{widgets}</li>"
        items.append({
            "section": "inbox", "key": key, "source": "synth",
            "text_html": f"<li>{body}</li>",
            "rendered_block": new_block,
        })
        return new_block

    return pattern.sub(replace, html), items


def read_votes(creds) -> list[dict]:
    """Read the `votes` tab on the ACK sheet."""
    if not ACK_SHEET_ID:
        return []
    try:
        resp = _sheets(creds).spreadsheets().values().get(
            spreadsheetId=ACK_SHEET_ID, range="votes!A:D").execute()
    except Exception:
        return []   # tab doesn't exist yet
    rows = resp.get("values", [])
    if len(rows) < 2:
        return []
    header = rows[0]
    return [dict(zip(header, r + [""] * (len(header) - len(r)))) for r in rows[1:]]


def read_user_sources(creds) -> list[dict]:
    """Read the `sources` tab — sources James has accepted via 👍 propagate
    here with status='accepted' and are added to the news picker's context."""
    if not ACK_SHEET_ID:
        return []
    try:
        resp = _sheets(creds).spreadsheets().values().get(
            spreadsheetId=ACK_SHEET_ID, range="sources!A:F").execute()
    except Exception:
        return []
    rows = resp.get("values", [])
    if len(rows) < 2:
        return []
    header = rows[0]
    return [dict(zip(header, r + [""] * (len(header) - len(r)))) for r in rows[1:]]


def _source_action_url(source_id: str, action: str) -> str:
    """Apps Script URL for accept/reject of a proposed source."""
    if not ACK_WEBHOOK_URL:
        return "#"
    q = {"source_id": source_id, "action": action}
    return f"{ACK_WEBHOOK_URL}?{urllib.parse.urlencode(q)}"


# ---------- Semantic dedup ----------
#
# Stable hash-based dedup (item_key on normalized text) only catches items
# that re-appear verbatim. It misses semantic dupes: "Gates funds Penda
# $10M" and "Penda Health gets Gates grant" are the same news event with
# different wording. Haiku does this classification cheaply (~$0.02/run).

DEDUP_ELIGIBLE_SECTIONS = {"news", "funder", "whitespace", "evidence"}
DEDUP_LOOKBACK_DAYS = 14
DEDUP_MODEL = "claude-haiku-4-5"


def dedup_with_haiku(today_items: list[dict],
                     state_items: list[dict],
                     today: dt.date) -> dict[str, dict]:
    """Identify today's items that refer to the same underlying event as a
    historical state item from the last DEDUP_LOOKBACK_DAYS. Returns:
        {today_key: {"historical_key": "...", "historical_section": "..."}}

    Only HIGH-confidence matches are returned. Uses Haiku because dedup
    is classification, not synthesis — ~10x cheaper than Sonnet for a
    task it's plenty smart for.
    """
    cutoff = today - dt.timedelta(days=DEDUP_LOOKBACK_DAYS)
    historical = []
    section_by_id: dict[str, str] = {}
    for r in state_items:
        if r.get("section") not in DEDUP_ELIGIBLE_SECTIONS:
            continue
        try:
            seen = dt.datetime.strptime(r.get("last_seen", "")[:10], "%Y-%m-%d").date()
            if seen < cutoff:
                continue
        except ValueError:
            continue
        plain = re.sub(r"\s+", " ",
                       re.sub(r"<[^>]+>", " ", r.get("text_html", ""))).strip()
        if not plain:
            continue
        historical.append({
            "id": r["key"],
            "section": r["section"],
            "age_days": (today - seen).days,
            "text": plain[:280],
        })
        section_by_id[r["key"]] = r["section"]

    candidates = []
    for it in today_items:
        if it.get("section") not in DEDUP_ELIGIBLE_SECTIONS:
            continue
        plain = re.sub(r"\s+", " ",
                       re.sub(r"<[^>]+>", " ", it.get("text_html", ""))).strip()
        if not plain:
            continue
        candidates.append({
            "id": it["key"],
            "section": it["section"],
            "text": plain[:280],
        })

    if not historical or not candidates:
        return {}

    msg = claude().messages.create(
        model=DEDUP_MODEL,
        max_tokens=1500,
        system=textwrap.dedent("""
            You are dedup'ing items in James's daily briefing across days.

            Two items "match" if they refer to the SAME underlying:
              - News event (a specific funder announcement, paper,
                deployment, policy change, personnel move)
              - Or the same observation / finding / trend
              - Same entity + same action + same approximate date window
                = match, even if wording is different

            Match examples (different wording, SAME event):
              ✓ "Gates funds Penda chatbot $10M" ≡ "Penda Health gets
                Gates grant for AI-powered triage"
              ✓ "Anthropic publishes responsible scaling policy" ≡
                "Anthropic's new RSP frames AI safety levels"

            NON-match examples (same topic, DIFFERENT events):
              ✗ "Wellcome announces AI-for-health RFP" vs "Gates
                announces AI-for-health RFP" — different funders
              ✗ "Penda hires CEO" vs "Penda raises Series A" — same
                org, different events
              ✗ "Two papers on AI in malaria" — only match if literally
                the same paper

            Match across sections is allowed: a 'funder' item and a
            'news' item can match if they describe the same event from
            different angles.

            Style: focus on the underlying EVENT or CLAIM, not surface
            wording. If unsure, DON'T match — false positives erase real
            signal.

            Output JSON only, no preamble, no markdown fences:
              {"matches": [
                {"today_id": "...", "historical_id": "..."},
                ...
              ]}
            Only output HIGH-confidence matches. If none: {"matches": []}
        """).strip(),
        messages=[{"role": "user", "content": json.dumps({
            "candidates": candidates,
            "historical": historical,
        }, indent=2)}],
    )
    raw = msg.content[0].text
    m = re.search(r'\{.*?"matches".*?\}', raw, re.S)
    if not m:
        return {}
    try:
        result = json.loads(m.group(0))
    except json.JSONDecodeError:
        return {}

    out: dict[str, dict] = {}
    for match in result.get("matches", []):
        t_id = match.get("today_id")
        h_id = match.get("historical_id")
        if t_id and h_id and h_id in section_by_id:
            out[t_id] = {
                "historical_key": h_id,
                "historical_section": section_by_id[h_id],
            }
    return out


def apply_semantic_dedup(
    today_items: list[dict],
    dedup_map: dict[str, dict],
    section_htmls: dict[str, str],
    state_items: list[dict],
) -> tuple[list[dict], dict[str, str]]:
    """Apply Haiku's dedup matches:
      1. Strip each duped item's rendered_block from the section HTML
         (so today's briefing doesn't show the same event twice).
      2. Drop the duped today_item from today_items.
      3. Inject a synthetic item with the historical key so
         merge_into_state bumps its carry_count and last_seen.
    """
    if not dedup_map:
        return today_items, section_htmls

    state_by_key = {r["key"]: r for r in state_items if r.get("key")}
    today_by_key = {it["key"]: it for it in today_items}
    cleaned_sections = dict(section_htmls)

    # Strip duped blocks from section HTML
    for today_key, match in dedup_map.items():
        it = today_by_key.get(today_key)
        if not it:
            continue
        block = it.get("rendered_block")
        if not block:
            continue
        section = it.get("section")
        if section in cleaned_sections and block in cleaned_sections[section]:
            cleaned_sections[section] = cleaned_sections[section].replace(
                block, "", 1
            )

    # Rebuild today_items without dupes; append historical-key placeholders
    new_today_items = [it for it in today_items if it["key"] not in dedup_map]
    for today_key, match in dedup_map.items():
        h_key = match["historical_key"]
        old = state_by_key.get(h_key)
        if not old:
            continue
        new_today_items.append({
            "section": old["section"],
            "key": h_key,
            "source": "dedup-bump",
            "text_html": old.get("text_html", ""),
        })

    return new_today_items, cleaned_sections


def digest_preferences(votes: list[dict], state: list[dict]) -> str:
    """Resolve each vote against the state sheet (key → item text) and ask
    Claude to summarise James's topic preferences as a short digest. This
    digest is then injected into the news / funder / whitespace prompts so
    next-day picks bias toward what's been thumbed up.
    """
    if not votes:
        return ""
    key_to_text = {r.get("key", ""): r.get("text_html", "") for r in state}
    enriched = []
    for v in votes:
        k = v.get("item_key", "")
        raw = key_to_text.get(k, "(item text not in state — older row)")
        plain = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", raw)).strip()
        enriched.append({
            "vote": v.get("vote"),
            "voted_at": v.get("voted_at", "")[:10],
            "topic": plain[:200],
        })
    msg = claude().messages.create(
        model=CLAUDE_MODEL,
        max_tokens=600,
        system=(
            "You are summarising James's topic preferences from his 👍/👎 "
            "votes on prior briefing items. Produce a short (80-150 word) "
            "digest with two labels: 'More of:' and 'Less of:'. Bias toward "
            "the most recent ~30 votes. Be concrete — name topics, sources, "
            "geographies, funders, tiers — not abstract ('thoughtful' / "
            "'strategic'). Output plain text, no markdown."
        ),
        messages=[{"role": "user", "content": json.dumps(enriched, indent=2)}],
    )
    return msg.content[0].text


def render_carryover(carryover: list[dict], today: dt.date) -> str:
    """Render the 'Pending from earlier' block prepended when prior days
    were not acknowledged."""
    if not carryover:
        return ""
    by_section: dict[str, list[dict]] = {}
    for r in carryover:
        by_section.setdefault(r.get("section", "other"), []).append(r)

    label = {
        "slip":        "Slip flags",
        "decision":    "Decisions still needed",
        "priority":    "Priorities",
        "action_item": "Open action items",
        "inbox":       "Inbox threads still waiting",
        "funder":      "Funder updates you haven't seen",
        "news":        "News items you haven't seen",
        "whitespace":  "White-space items you haven't seen",
    }

    parts = ['<div class="carryover" style="background:#fef2f2;'
             'border:1px solid #fca5a5;border-left:4px solid #dc2626;'
             'padding:16px 20px;border-radius:8px;margin:0 0 24px 0;">'
             '<h2 style="margin:0 0 4px 0;color:#dc2626;font-size:18px;'
             'font-weight:700;letter-spacing:-0.2px;">'
             f'⏰ Pending from earlier — '
             f'{len(carryover)} item{"s" if len(carryover)!=1 else ""} you haven\'t acknowledged'
             '</h2>'
             '<p style="font-size:12.5px;color:#7f1d1d;margin:0 0 14px 0;">'
             'These have rolled forward from prior briefings. They stay here '
             'until you mark them done or dismiss them.</p>']

    sect_order = ["slip", "decision", "priority", "action_item", "inbox",
                  "funder", "news", "whitespace"]
    for sect in sect_order:
        rows = by_section.get(sect, [])
        if not rows:
            continue
        parts.append(
            f'<h3 style="color:#7f1d1d;font-size:13px;margin:14px 0 4px 0;'
            f'text-transform:uppercase;letter-spacing:0.8px;'
            f'font-weight:700;">{label.get(sect, sect.title())}</h3><ul>'
        )
        for r in rows:
            carry = int(r.get("carry_count") or 0)
            stale = carry >= STALE_DAYS
            badge = (f'<span style="background:{"#dc2626" if stale else "#ea580c"};'
                     f'color:#fff;padding:2px 7px;border-radius:10px;font-size:10px;'
                     f'font-weight:600;margin-right:8px;vertical-align:1px;">'
                     f'🔁 {carry}d</span>')
            done_link = (
                f' <a style="font-size:11px;color:#6b7280;margin-left:6px;" '
                f'href="{_ack_url(today, [r["key"]])}">mark done</a>'
                if ACK_WEBHOOK_URL else ""
            )
            inner = r.get("text_html", "").strip()
            # If the stored item is already an <li>, strip the wrapper.
            inner = re.sub(r"^<li>|</li>$", "", inner.strip())
            parts.append(f"<li>{badge}{inner}{done_link}</li>")
        parts.append("</ul>")
    parts.append("</div>")
    return "\n".join(parts)


def render_html(today: dt.date, prioritization: str, news: str,
                whitespace: str = "", inbox: str = "", funder: str = "",
                carryover_html: str = "", trends: str = "",
                sources: str = "", publisher_landscape: str = "",
                evidence: str = "", tldr: str = "",
                widgets_html: str = "",
                dashboard_url: str = "",
                ideas: str = "",
                sources_today: str = "") -> str:
    """Axios smart-brevity layout. Color-coded section cards, TL;DR strip,
    pill-style feedback widgets. The synth functions output their own
    <h2>Section name</h2> headings — we wrap each in a card div tagged
    with its slug so CSS can theme it.
    """
    ack_link = _ack_url(today)
    ack_banner = (
        f'<div class="ack-banner">'
        f'<strong>👁 Mark today as seen.</strong> '
        f'<a href="{ack_link}">I\'ve read this briefing &rarr;</a>'
        f'<span class="ack-banner-note">'
        f'If you don\'t, items roll forward to tomorrow.</span></div>'
        if ACK_WEBHOOK_URL else ""
    )

    tldr_block = (
        f'<div class="tldr"><span class="tldr-label">TL;DR</span>{tldr}</div>'
        if tldr else ""
    )

    dashboard_cta = (
        f'<div style="background:#dbeafe;border-left:4px solid #2563eb;'
        f'padding:12px 16px;border-radius:6px;margin:0 0 18px 0;font-size:13.5px;'
        f'color:#1e3a8a;"><strong>🎛 Interactive dashboard.</strong> '
        f'<a href="{dashboard_url}" style="color:#1e40af;font-weight:600;">'
        f'Open in-page version →</a>'
        f'<div style="font-size:12px;color:#475569;margin-top:4px;">'
        f"Click 👍/👎, leave comments, and send items to your task list "
        f"— all in one tab, no popups.</div></div>"
        if dashboard_url else ""
    )

    def _section(slug, content):
        """Wrap a section in a color-coded card. Empty content → omitted."""
        return f'<section class="card card-{slug}">{content}</section>' if content else ""

    feedback_footer = (
        f'<div class="feedback-footer">'
        f'<strong>👋 Was this useful?</strong> '
        f'<a href="{FEEDBACK_FORM_URL}?usp=pp_url&entry.{FEEDBACK_FORM_DATE_FIELD}={today.isoformat()}">'
        f'Rate today\'s briefing &rarr;</a>'
        f'<div class="feedback-meta">60 seconds — your ratings train '
        f'tomorrow\'s draft. The 👍/👎 next to each item also helps.</div>'
        f'</div>'
        if FEEDBACK_FORM_URL else ""
    )

    # Greeting by time of day.
    hour = dt.datetime.now().hour
    if hour < 12:
        greeting = "Morning"
    elif hour < 17:
        greeting = "Afternoon"
    else:
        greeting = "Evening"

    return textwrap.dedent(f"""\
        <!doctype html>
        <html><head><meta charset="utf-8">
        <title>2AI daily briefing — {today.isoformat()}</title>
        <style>
          /* Axios smart-brevity, color-coded section cards. */
          body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
                         "Helvetica Neue", sans-serif;
            max-width: 760px; margin: 0 auto; padding: 32px 22px 48px;
            color: #111827; line-height: 1.55; background: #fafaf7;
          }}
          .eyebrow {{
            font-size: 11px; text-transform: uppercase; letter-spacing: 1.4px;
            color: #6b7280; font-weight: 700; margin-bottom: 4px;
          }}
          h1.title {{
            font-size: 30px; line-height: 1.15; margin: 0 0 4px 0;
            font-weight: 800; letter-spacing: -0.5px; color: #111827;
          }}
          .subtitle {{ color: #6b7280; font-size: 13px; margin-bottom: 24px; }}
          h2 {{
            font-size: 19px; font-weight: 700; margin: 0 0 12px 0;
            letter-spacing: -0.2px; color: #1f2937;
          }}
          h3 {{
            font-size: 15px; font-weight: 700; margin: 18px 0 6px 0;
            color: #374151;
          }}
          p, li {{ font-size: 14.5px; }}
          em {{ color: #4b5563; font-style: italic; }}
          a {{
            color: #0e7490; text-decoration: none;
            border-bottom: 1px solid rgba(14,116,144,0.35);
          }}
          a:hover {{ border-bottom-color: #0e7490; }}
          ul {{ margin: 8px 0; padding-left: 22px; }}
          li {{ margin: 5px 0; }}
          strong {{ color: #111827; }}

          /* TL;DR strip — Axios's signature yellow */
          .tldr {{
            background: #fef3c7; border-left: 4px solid #d97706;
            padding: 14px 18px; border-radius: 8px; margin: 0 0 22px 0;
            font-size: 14.5px; line-height: 1.5;
          }}
          .tldr-label {{
            display: inline-block; background: #d97706; color: #fff;
            font-size: 10px; font-weight: 700; letter-spacing: 1.4px;
            padding: 3px 9px; border-radius: 4px; margin-right: 10px;
            vertical-align: 2px;
          }}

          /* "Mark today as seen" banner */
          .ack-banner {{
            background: #ecfdf5; border-left: 4px solid #10b981;
            border-radius: 6px; padding: 11px 16px; margin: 0 0 22px 0;
            font-size: 13px; color: #064e3b;
          }}
          .ack-banner-note {{ color: #6b7280; margin-left: 8px; font-size: 12px; }}

          /* Section cards — color-coded left accent + heading color.
             Each section's <h2> inside the card adopts its accent color. */
          .card {{
            background: #ffffff; border-radius: 10px;
            padding: 18px 22px; margin: 18px 0;
            border-left: 4px solid #94a3b8;
            box-shadow: 0 1px 2px rgba(0,0,0,0.04);
          }}
          .card > h2:first-child {{ margin-top: 0; }}
          .card > h2 ~ h2 {{ margin-top: 26px; }}

          .card-priorities {{ border-left-color: #1e3a8a; }}
          .card-priorities h2 {{ color: #1e3a8a; }}
          .card-inbox      {{ border-left-color: #475569; }}
          .card-inbox h2   {{ color: #475569; }}
          .card-funder     {{ border-left-color: #b45309; }}
          .card-funder h2  {{ color: #b45309; }}
          .card-news       {{ border-left-color: #0e7490; }}
          .card-news h2    {{ color: #0e7490; }}
          .card-evidence   {{ border-left-color: #15803d; }}
          .card-evidence h2 {{ color: #15803d; }}
          .card-whitespace {{ border-left-color: #6d28d9; }}
          .card-whitespace h2 {{ color: #6d28d9; }}
          .card-trends     {{ border-left-color: #6d28d9; }}
          .card-trends h2  {{ color: #6d28d9; }}
          .card-publisher  {{ border-left-color: #6d28d9; }}
          .card-publisher h2 {{ color: #6d28d9; }}
          .card-sources    {{ border-left-color: #475569; }}
          .card-sources h2 {{ color: #475569; }}
          .card-ideas      {{ border-left-color: #be185d; }}
          .card-ideas h2   {{ color: #be185d; }}
          .card-sourcestoday {{ border-left-color: #475569; }}
          .card-sourcestoday h2 {{ color: #475569; }}

          /* "So what for 2AI:" callout — the synth prompts already emit
             this as <strong>So what for 2AI:</strong> followed by text.
             We can't pattern-match text in pure CSS, so we just style
             every <strong> in section cards with a subtle accent. */
          .card p strong:first-child {{ color: inherit; }}

          /* Footer */
          .feedback-footer {{
            margin-top: 32px; padding: 18px 20px;
            background: #f1f5f9; border-radius: 10px;
            font-size: 13.5px; color: #334155;
            border-left: 4px solid #94a3b8;
          }}
          .feedback-meta {{
            color: #6b7280; font-size: 12px; margin-top: 6px;
          }}
        </style>
        </head><body>
        <div class="eyebrow">2AI Daily Briefing · {today.strftime(f"%A %B {_NO_PAD_DAY}").upper()}</div>
        <h1 class="title">{greeting}, James.</h1>
        <div class="subtitle">Generated {dt.datetime.now().strftime(f"{_NO_PAD_HOUR}:%M %p")} · auto-piloted</div>
        {dashboard_cta}
        {widgets_html}
        {tldr_block}
        {ack_banner}
        {carryover_html}
        {_section("priorities", prioritization)}
        {_section("inbox", inbox)}
        {_section("ideas", ideas)}
        {_section("funder", funder)}
        {_section("news", news)}
        {_section("sourcestoday", sources_today)}
        {_section("evidence", evidence)}
        {_section("whitespace", whitespace)}
        {_section("trends", trends)}
        {_section("publisher", publisher_landscape)}
        {_section("sources", sources)}
        {feedback_footer}
        </body></html>
    """).strip()


# ---------- Interactive dashboard (GitHub Pages) ----------
#
# Same content as the email, but with webhook anchors converted to
# JS-driven buttons that fire fetch() in background + show toasts.
# Also adds per-item "💬 comment" toggles + a "Send to tasks" button
# alongside each thumbs widget. Hosted on GitHub Pages; the email
# contains a per-day URL pointing here.


def make_interactive_dashboard(email_html: str, dashboard_url: str,
                               webhook_url: str, today: dt.date) -> str:
    """Convert the email HTML into an interactive dashboard variant.

    Strategy: post-process the rendered email so we don't have to
    duplicate render logic. Three transforms:
      1. Convert <a href="webhook?...">label</a> → <button data-...>
         label</button> so JS can intercept clicks.
      2. After each thumbs widget (and key-bearing item), inject a
         "💬 comment" toggle + a "📌 Send to tasks" button. Both use
         data-attributes so the JS runtime can read item key/section.
      3. Inject the JS runtime + a top-banner before </body>.

    Toast-style success: optimistic UI. Apps Script GET hits in no-cors
    mode so we can't see the response, but it processes the row write.
    """
    if not webhook_url:
        # Without a webhook we can't do interactive actions; return
        # email HTML unchanged.
        return email_html

    today_iso = today.isoformat()
    html = email_html

    # Add noindex + robots-block meta tags to keep search engines out.
    html = html.replace(
        '<meta charset="utf-8">',
        '<meta charset="utf-8">\n'
        '<meta name="robots" content="noindex, nofollow, noarchive">',
        1,
    )

    # Inject a banner just below the <h1> title — explains this is
    # the interactive dashboard.
    banner = (
        '<div class="dashboard-banner" style="background:#dbeafe;'
        'border-left:4px solid #2563eb;padding:10px 14px;border-radius:6px;'
        'margin:0 0 18px 0;font-size:13px;color:#1e3a8a;">'
        '<strong>🎛 Interactive dashboard.</strong> '
        'All buttons fire in-page — no popups. '
        '<span style="color:#475569;">Closes when you do.</span></div>'
    )
    html = re.sub(
        r'(<div class="subtitle">[^<]*</div>)',
        r'\1\n' + banner,
        html, count=1,
    )

    # We'll harvest item keys + sections as we rewrite buttons.
    # Pattern: any anchor whose href starts with the webhook URL.
    webhook_anchor_re = re.compile(
        r'<a([^>]*?)href="' + re.escape(webhook_url) + r'\?([^"]*?)"([^>]*?)>(.*?)</a>',
        re.S | re.I,
    )

    def to_button(m):
        pre, query, post, label = m.group(1), m.group(2), m.group(3), m.group(4)
        params = urllib.parse.parse_qs(query)
        attrs = ['data-action="webhook"']
        for k, v in params.items():
            val = v[0] if isinstance(v, list) else v
            attrs.append(f'data-{k}="{val}"')
        # Strip styles that anchors had; the button gets its own.
        return (
            f'<button type="button" '
            f'style="background:none;border:none;padding:0;'
            f'font:inherit;color:#0e7490;cursor:pointer;'
            f'border-bottom:1px solid rgba(14,116,144,0.35);" '
            f'{" ".join(attrs)}>'
            f'{label}</button>'
        )

    html = webhook_anchor_re.sub(to_button, html)

    # Inject the JS runtime + comment-toggle / task-proposal widgets
    # before </body>.
    runtime = _dashboard_runtime_js(webhook_url, today_iso)
    html = html.replace("</body>", runtime + "\n</body>", 1)

    return html


def _dashboard_runtime_js(webhook_url: str, today_iso: str) -> str:
    """The <script> block injected into every dashboard page. Wires up:
      - Button clicks → fetch() in no-cors mode + toast.
      - Per-item 💬 / 📌 affordances injected next to each thumbs widget.
    """
    return (
        '<div id="toast" style="position:fixed;bottom:24px;'
        'left:50%;transform:translateX(-50%);background:#15803d;'
        'color:#fff;padding:10px 18px;border-radius:8px;font-size:13px;'
        'font-family:-apple-system,sans-serif;box-shadow:0 4px 14px '
        'rgba(0,0,0,0.18);opacity:0;transition:opacity 0.25s;'
        'pointer-events:none;z-index:9999;"></div>\n'
        '<script>\n'
        f'const WEBHOOK = "{webhook_url}";\n'
        f'const TODAY = "{today_iso}";\n'
        'function toast(msg, isError) {\n'
        '  const t = document.getElementById("toast");\n'
        '  t.textContent = msg;\n'
        '  t.style.background = isError ? "#dc2626" : "#15803d";\n'
        '  t.style.opacity = "1";\n'
        '  clearTimeout(window._toast_t);\n'
        '  window._toast_t = setTimeout(() => t.style.opacity = "0", 1800);\n'
        '}\n'
        'function fireWebhook(params, label) {\n'
        '  const q = new URLSearchParams(params).toString();\n'
        '  fetch(WEBHOOK + "?" + q, {method: "GET", mode: "no-cors"})\n'
        '    .catch(e => toast("Network error", true));\n'
        '  toast(label || "✓ saved");\n'
        '}\n'
        '// Attach handlers to the converted webhook buttons.\n'
        'document.querySelectorAll("[data-action=\\"webhook\\"]").forEach(btn => {\n'
        '  btn.addEventListener("click", e => {\n'
        '    e.preventDefault();\n'
        '    const params = {};\n'
        '    for (const a of btn.attributes) {\n'
        '      if (a.name.startsWith("data-") && a.name !== "data-action") {\n'
        '        params[a.name.slice(5)] = a.value;\n'
        '      }\n'
        '    }\n'
        '    let label = "✓ saved";\n'
        '    // Ignore: writes mark-done AND fires a 👎 vote so future\n'
        '    // synth biases against re-surfacing the same item.\n'
        '    if (params.kind === "ignore") {\n'
        '      label = "✕ Ignored";\n'
        '      fireWebhook({vote: "down", key: params.keys, date: params.date}, null);\n'
        '    } else if (params.task_proposal) {\n'
        '      label = "📌 Sent to tasks queue";\n'
        '    } else if (params.vote === "up") label = "👍 More like this";\n'
        '    else if (params.vote === "down") label = "👎 Less like this";\n'
        '    else if (params.keys) label = "✓ marked done";\n'
        '    else if (Object.keys(params).length === 1 && params.date) label = "✓ acknowledged";\n'
        '    fireWebhook(params, label);\n'
        '    // Visually dim the parent item if ignoring or marking done.\n'
        '    if (params.kind === "ignore" || params.keys) {\n'
        '      const li = btn.closest("li");\n'
        '      if (li) { li.style.opacity = "0.4"; li.style.textDecoration = "line-through"; }\n'
        '    }\n'
        '  });\n'
        '});\n'
        '// Per-item add-ons: alongside every thumbs widget OR\n'
        '// action-widgets strip, inject a 💬 comment toggle + 📌 send-\n'
        '// to-tasks button (skipping the latter if action-widgets\n'
        '// already has a "send to tasks" anchor). Each reads the key\n'
        '// + section from the nearest button\'s data attrs.\n'
        'document.querySelectorAll(".thumbs, .action-widgets").forEach(thumbsEl => {\n'
        '  const sample = thumbsEl.querySelector("[data-key]");\n'
        '  if (!sample) return;\n'
        '  const key = sample.dataset.key;\n'
        '  // Skip injecting a duplicate task button if already present.\n'
        '  const hasTaskAlready = thumbsEl.querySelector("[data-task_proposal]");\n'
        '  // Try to infer section from URL params; default to "topic".\n'
        '  // The data attributes only carry vote/key/date; section we\n'
        '  // tag in the email render is implicit by parent card class.\n'
        '  let section = "topic";\n'
        '  const card = thumbsEl.closest(".card");\n'
        '  if (card) {\n'
        '    const match = (card.className.match(/card-(\\w+)/) || [])[1];\n'
        '    if (match) section = match;\n'
        '  }\n'
        '  // Build comment + send-to-tasks affordances\n'
        '  const wrap = document.createElement("span");\n'
        '  wrap.style.marginLeft = "8px";\n'
        '  wrap.innerHTML = (\n'
        '    \'<a href="#" class="dash-comment" style="font-size:11px;\'\n'
        '    + \'color:#6b7280;text-decoration:none;border-bottom:1px dotted #9ca3af;\'\n'
        '    + \'margin-right:8px;">💬 comment</a>\'\n'
        '    + (hasTaskAlready ? \'\' :\n'
        '       \'<a href="#" class="dash-task" style="font-size:11px;\'\n'
        '       + \'color:#6b7280;text-decoration:none;border-bottom:1px dotted #9ca3af;\'\n'
        '       + \'">📌 send to tasks</a>\')\n'
        '  );\n'
        '  thumbsEl.appendChild(wrap);\n'
        '  // Comment toggle\n'
        '  wrap.querySelector(".dash-comment").addEventListener("click", e => {\n'
        '    e.preventDefault();\n'
        '    const existing = thumbsEl.parentElement.querySelector(".dash-comment-box");\n'
        '    if (existing) { existing.remove(); return; }\n'
        '    const box = document.createElement("div");\n'
        '    box.className = "dash-comment-box";\n'
        '    box.style.cssText = "margin-top:8px;padding:10px;background:#fffbeb;'
        'border-left:3px solid #d97706;border-radius:4px;font-size:13px;";\n'
        '    box.innerHTML = (\n'
        '      \'<textarea rows="3" style="width:100%;box-sizing:border-box;\'\n'
        '      + \'border:1px solid #e5e7eb;border-radius:4px;padding:6px;\'\n'
        '      + \'font-family:inherit;font-size:13px;" \'\n'
        '      + \'placeholder="Leave a note about this item for next time…"></textarea>\'\n'
        '      + \'<button type="button" class="dash-comment-submit" \'\n'
        '      + \'style="margin-top:6px;background:#d97706;color:#fff;\'\n'
        '      + \'border:none;padding:6px 14px;border-radius:4px;font-size:12px;\'\n'
        '      + \'cursor:pointer;">Save comment</button>\'\n'
        '    );\n'
        '    thumbsEl.parentElement.appendChild(box);\n'
        '    box.querySelector("textarea").focus();\n'
        '    box.querySelector(".dash-comment-submit").addEventListener("click", () => {\n'
        '      const txt = box.querySelector("textarea").value.trim();\n'
        '      if (!txt) { toast("Empty comment", true); return; }\n'
        '      fireWebhook({comment: txt, key: key, section: section, date: TODAY},\n'
        '                  "💬 Comment saved");\n'
        '      box.remove();\n'
        '    });\n'
        '  });\n'
        '  // Send to tasks (only if we injected it — already-present\n'
        '  // anchors are handled by the main webhook click listener).\n'
        '  const dashTask = wrap.querySelector(".dash-task");\n'
        '  if (dashTask) dashTask.addEventListener("click", e => {\n'
        '    e.preventDefault();\n'
        '    // Use the item\'s nearest heading text as title.\n'
        '    const parent = thumbsEl.parentElement;\n'
        '    const h = parent.querySelector("h3");\n'
        '    let title = "(untitled item)";\n'
        '    if (h) title = h.textContent.trim();\n'
        '    else if (parent.firstElementChild) title = parent.firstElementChild.textContent.trim().slice(0, 120);\n'
        '    // Default urgency: high for funder/inbox, medium otherwise.\n'
        '    const urgency = (section === "funder" || section === "inbox") ? "high" : "medium";\n'
        '    fireWebhook({task_proposal: title, urgency: urgency,\n'
        '                 section: section, key: key, date: TODAY},\n'
        '                "📌 Sent to tasks queue");\n'
        '  });\n'
        '});\n'
        '</script>\n'
    )


def save_dashboard(html: str, slug: str) -> Path:
    """Write the dashboard HTML to ./docs/<slug>.html so the Pages
    deploy workflow can upload it. Also writes robots.txt + a blank
    index.html. Returns the file path.

    The slug is passed in (pre-computed in main so the email can link
    to the dashboard before the dashboard is written).
    """
    DASHBOARD_DIR.mkdir(parents=True, exist_ok=True)
    path = DASHBOARD_DIR / f"{slug}.html"
    path.write_text(html, encoding="utf-8")
    # robots.txt — overwritten each run, content is constant.
    (DASHBOARD_DIR / "robots.txt").write_text(
        "User-agent: *\nDisallow: /\n", encoding="utf-8",
    )
    # Empty index — landing the bare URL shouldn't reveal anything.
    (DASHBOARD_DIR / "index.html").write_text(
        '<!doctype html><html><head>'
        '<meta name="robots" content="noindex,nofollow,noarchive">'
        '<title>—</title></head><body></body></html>',
        encoding="utf-8",
    )
    return path


# ---------- Deliver ----------

def upload_drive_doc(creds, html: str, today: dt.date) -> str:
    """Convert the HTML to a Google Doc, upload to Drive, return webViewLink."""
    svc = build("drive", "v3", credentials=creds, cache_discovery=False)
    metadata = {
        "name": f"Daily Briefing — {today.isoformat()}",
        "mimeType": "application/vnd.google-apps.document",
        "parents": [BRIEFINGS_DRIVE_FOLDER_ID],
    }
    media = MediaIoBaseUpload(io.BytesIO(html.encode("utf-8")), mimetype="text/html")
    f = svc.files().create(body=metadata, media_body=media,
                           fields="id,webViewLink").execute()
    return f["webViewLink"]


def send_gmail(creds, html: str, today: dt.date):
    svc = build("gmail", "v1", credentials=creds, cache_discovery=False)
    msg = MIMEText(html, "html", "utf-8")
    msg["to"] = RECIPIENT_EMAIL
    msg["from"] = RECIPIENT_EMAIL
    msg["subject"] = f"Daily briefing — {today.strftime(f'%a %b {_NO_PAD_DAY}')}"
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    svc.users().messages().send(userId="me", body={"raw": raw}).execute()


def post_slack(doc_link: str, today: dt.date, carry_count: int = 0,
               dashboard_url: str = ""):
    token = os.environ.get("SLACK_BOT_TOKEN")
    if not token:
        print("[slack] SLACK_BOT_TOKEN not set, skipping Slack DM")
        return
    client = WebClient(token=token)
    pending = (
        f"\n:clock3: *{carry_count}* item{'s' if carry_count != 1 else ''} "
        f"still pending from earlier briefings."
        if carry_count else ""
    )
    ack = (f"\n:white_check_mark: <{_ack_url(today)}|Mark today as seen>"
           if ACK_WEBHOOK_URL else "")
    dashboard_line = (
        f"\n:control_knobs: <{dashboard_url}|Interactive dashboard> "
        f"(click 👍/👎, leave comments, send items to tasks)"
        if dashboard_url else ""
    )
    try:
        client.chat_postMessage(
            channel=SLACK_USER_ID,
            text=(
                f":sunrise: *Daily briefing — {today.strftime(f'%a %b {_NO_PAD_DAY}')}*\n"
                f"In your inbox + Drive: <{doc_link}|open the Doc>"
                f"{dashboard_line}{pending}{ack}"
            ),
        )
    except SlackApiError as e:
        print(f"[slack] failed: {e.response['error']}")


def alert_slack_failure(error: Exception, traceback_str: str) -> None:
    """Best-effort DM-on-crash alert. Used by the top-level handler when
    main() raises so a failed cron run pings James instead of dying
    silently. Never raises — if Slack is down or token is missing, we
    just log and move on (the original exception is already in stderr).
    """
    token = os.environ.get("SLACK_BOT_TOKEN")
    if not token:
        return
    try:
        client = WebClient(token=token)
        # Last 6 lines of the traceback — enough to identify the failure
        # site without flooding the DM.
        tb_tail = "\n".join(traceback_str.strip().splitlines()[-6:])
        client.chat_postMessage(
            channel=SLACK_USER_ID,
            text=(
                f":rotating_light: *Daily briefing FAILED* — "
                f"{dt.date.today().isoformat()}\n"
                f"`{type(error).__name__}: {str(error)[:300]}`\n"
                f"```{tb_tail}```\n"
                f"Check the run log: "
                f"https://github.com/james-from-2ai/daily-briefing-automation/actions"
            ),
        )
    except Exception as e:
        # The alert path itself can't fail loudly — already crashing.
        print(f"[slack-alert] failed: {e}", file=sys.stderr)


# ---------- Main ----------

def main():
    today = dt.date.today()
    print(f"[{today}] starting daily briefing")

    creds = google_creds()

    # ---- read durable state first ----
    print("  reading state + acks + votes…")
    state = read_state(creds)
    acks = read_acks(creds)
    votes = read_votes(creds)
    state = apply_acks_to_state(state, acks)
    print(f"    {len(state)} state rows; {len(acks)} acks; {len(votes)} votes")

    yesterday_seen = was_yesterday_acknowledged(acks, today)
    print(f"    yesterday acknowledged: {yesterday_seen}")

    prefs_digest = ""
    if votes:
        print("  digesting topic preferences from votes…")
        prefs_digest = digest_preferences(votes, state)
        print(f"    digest ({len(prefs_digest)} chars) ready")

    # ---- pull all inputs ----
    print("  pulling calendar…")
    cal = pull_calendar(creds, today)
    print(f"    {len(cal)} events")

    print("  pulling recent Drive activity…")
    drive = pull_drive_recent(creds)
    print(f"    {len(drive)} files")

    print("  pulling 1:1 docs…")
    oneonones = {name: pull_1on1_recent_entries(creds, fid)
                 for name, fid in ONEONONE_DOCS.items()}

    print("  pulling inbox signals…")
    inbox_msgs = pull_inbox_signals(creds)
    _needs = sum(1 for m in inbox_msgs if m.get("kind") == "needs_you")
    _stale = sum(1 for m in inbox_msgs if m.get("kind") == "stale")
    print(f"    {_needs} needs-reply + {_stale} likely-to-slip")

    print("  pulling recent feedback…")
    feedback = pull_recent_feedback(creds)

    print("  pulling cowork tasks + journal…")
    cowork_tasks = pull_tasks_json()
    cowork_journal = pull_journal_recent()
    print(f"    {len(cowork_tasks)} active tasks · {len(cowork_journal)} journal entries")

    print("  pulling widget feeds (weather + market)…")
    weather = pull_weather()
    stocks = pull_stocks()
    widgets_html = render_widgets_strip(weather, stocks)

    # ---- extract action items from 1:1s into state ----
    action_items = []
    for name, text in oneonones.items():
        action_items.extend(
            extract_action_items(text, f"{name} 1:1 most-recent entries")
        )
    print(f"  extracted {len(action_items)} candidate action items from 1:1 notes")

    # ---- synthesize fresh content ----
    print("  synthesising prioritization (draft)…")
    prioritization_draft = synthesize_prioritization(
        cal, drive, oneonones,
        inbox_msgs=inbox_msgs,
        tasks_context=cowork_tasks,
        journal_context=cowork_journal,
        feedback_digest=feedback,
    )

    print("  triaging inbox + drafting context-aware replies…")
    inbox_html = synthesize_inbox_triage(
        inbox_msgs, oneonone_notes=oneonones, calendar=cal,
    )

    recent_headlines = recent_news_headlines(state, today)
    if today.toordinal() % 2 == FUNDER_RUN_PARITY:
        print(f"  building funder watchlist (dedup against {len(recent_headlines)} recent)…")
        funder_html = synthesize_funder_watchlist(recent_headlines, prefs_digest)
    else:
        print("  skipping funder watchlist (runs every 2 days)")
        funder_html = ""

    print("  pulling news topics sheet…")
    topics = pull_news_topics_sheet(creds)
    print("  running news deep research…")
    news = synthesize_news_briefing(topics, recent_headlines, prefs_digest)

    # ---- daily: propose new sources spotted in today's citations ----
    print("  scanning citations for new sources to track…")
    already_known_sources = ([f["name"] for f in FUNDER_WATCHLIST]
                             + [r.get("source_name", "")
                                for r in read_user_sources(creds)
                                if r.get("status") == "accepted"])
    sources_today_html = propose_news_sources_today(
        news, funder_html, state, already_known_sources, today,
    )
    # The function stashes proposed rows on its own attr for state write.
    daily_source_proposals = getattr(
        propose_news_sources_today, "_proposed", [],
    )
    if hasattr(propose_news_sources_today, "_proposed"):
        delattr(propose_news_sources_today, "_proposed")
    print(f"    {len(daily_source_proposals)} candidate(s) surfaced")

    # ---- daily: 1-3 implementation ideas for 2AI ----
    # Pull program-area corpus (Drive sample) for context. Reused later
    # if whitespace runs today (Monday).
    print("  pulling program-area corpus…")
    program_corpus = pull_program_area_corpus(creds)
    print("  generating 2AI implementation ideas…")
    ideas_html = propose_2ai_ideas(news, program_corpus, prefs_digest)

    whitespace = ""
    if today.weekday() == WHITESPACE_WEEKDAY:
        for area, files in program_corpus.items():
            print(f"    {area}: {len(files)} recent docs")
        print("  running white-space analysis…")
        whitespace = synthesize_whitespace(program_corpus, feedback, prefs_digest)
    else:
        print(f"  skipping white-space (runs on weekday {WHITESPACE_WEEKDAY}, "
              f"today is {today.weekday()})")

    trends = ""
    if today.weekday() == TRENDS_WEEKDAY:
        print("  running cross-window trends analysis…")
        trends = synthesize_trends(state, today, prefs_digest)
    else:
        print(f"  skipping trends (runs on weekday {TRENDS_WEEKDAY})")

    evidence = ""
    if today.weekday() in EVIDENCE_WEEKDAYS:
        print("  pulling evidence digest (consensus.app + preprint servers)…")
        evidence = synthesize_evidence_digest(prefs_digest)
    else:
        print(f"  skipping evidence digest (runs on weekdays {EVIDENCE_WEEKDAYS})")

    sources_html = ""
    new_source_rows: list[dict] = []
    if today.weekday() == SOURCES_WEEKDAY:
        print("  proposing new sources…")
        already = read_user_sources(creds)
        rotation = [f["name"] for f in FUNDER_WATCHLIST] + \
                   [r.get("source_name", "") for r in already
                    if r.get("status") == "accepted"]
        sources_html, new_source_rows = propose_new_sources(rotation, already, today)
        print(f"    proposed {len(new_source_rows)} candidates")
    else:
        print(f"  skipping source-proposer (runs on weekday {SOURCES_WEEKDAY})")

    # First weekday of the month → peer-publisher landscape.
    publisher_landscape = ""
    is_first_weekday = (today.day <= 7 and today.weekday() < 5
                        and not any((today - dt.timedelta(days=i)).month == today.month
                                    and (today - dt.timedelta(days=i)).weekday() < 5
                                    for i in range(1, today.day)))
    if is_first_weekday:
        print("  running monthly peer-publisher landscape…")
        publisher_landscape = synthesize_publisher_landscape(prefs_digest)
    else:
        print("  skipping publisher landscape (runs first weekday of month)")

    # ---- critic pass ----
    print("  critic pass — reviewing draft against feedback…")
    inputs_summary = (
        f"calendar: {len(cal)} events; drive: {len(drive)} files; "
        f"1:1 docs: Katie+Sarah; inbox: {len(inbox_msgs)} threads; "
        f"white-space: {'yes' if whitespace else 'no'}; "
        f"yesterday acknowledged: {yesterday_seen}"
    )
    prioritization = critique_and_revise(prioritization_draft, inputs_summary, feedback)

    # ---- TL;DR strip for the top of the briefing ----
    print("  generating TL;DR…")
    tldr = synthesize_tldr(prioritization, _needs, _stale, today)
    print(f"    {tldr[:120]}{'…' if len(tldr) > 120 else ''}")

    # ---- annotate topic sections with 👍/👎 + index everything into state ----
    print("  injecting 👍/👎 controls + indexing items…")
    news, news_items = annotate_topics_h3(news, "news", today)
    funder_html, funder_items = annotate_topics_h3(funder_html, "funder", today)
    if whitespace:
        whitespace, ws_items = annotate_topics_li(whitespace, "whitespace", today)
    else:
        ws_items = []
    # 2AI ideas: annotate as <li> topics so each idea gets 👍/👎 + JS
    # comment + send-to-tasks affordances.
    if ideas_html:
        ideas_html, ideas_items = annotate_topics_li(ideas_html, "ideas", today)
    else:
        ideas_items = []
    if evidence:
        # Each <div border-left> in the evidence digest is one paper.
        # Reuse the li annotator pattern by retargeting the regex.
        evidence_items: list[dict] = []
        def _evidence_buttonize(m):
            block = m.group(0)
            plain = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", block)).strip()
            if len(plain) < 20:
                return block
            key = item_key("evidence", plain[:200])
            if ACK_WEBHOOK_URL:
                thumbs = THUMBS_TEMPLATE.format(
                    up=_vote_url(today, key, "up"),
                    down=_vote_url(today, key, "down"),
                )
                new_block = block.replace("</div>", f"  {thumbs}\n</div>", 1)
            else:
                new_block = block
            evidence_items.append({
                "section": "evidence", "key": key, "source": "synth",
                "text_html": block,
                "rendered_block": new_block,
            })
            return new_block
        evidence = re.sub(
            r'<div style="border-left:3px solid #5fae5f;[^"]*"[^>]*>.*?</div>',
            _evidence_buttonize, evidence, flags=re.S,
        )
    else:
        evidence_items = []

    # Annotate prioritization + inbox: inject "✕ not a priority" /
    # "📌 send to tasks" anchors and index items in one pass. Replaces
    # the older extract_items_from_html(prioritization/inbox, …) calls.
    prioritization, prio_items = annotate_prioritization(prioritization, today)
    inbox_html, inbox_items_indexed = annotate_inbox(inbox_html, today)

    today_items = []
    today_items += action_items
    today_items += news_items
    today_items += funder_items
    today_items += ws_items
    today_items += evidence_items
    today_items += ideas_items
    today_items += prio_items
    today_items += inbox_items_indexed

    # ---- semantic dedup against last 14 days of state via Haiku ----
    if state:
        print("  semantic dedup against recent state…")
        dedup_map = dedup_with_haiku(today_items, state, today)
        if dedup_map:
            section_htmls = {
                "news": news, "funder": funder_html,
                "whitespace": whitespace, "evidence": evidence,
            }
            today_items, cleaned = apply_semantic_dedup(
                today_items, dedup_map, section_htmls, state,
            )
            news = cleaned["news"]
            funder_html = cleaned["funder"]
            whitespace = cleaned["whitespace"]
            evidence = cleaned["evidence"]
            print(f"    dropped {len(dedup_map)} dupe(s); bumped historical carry_count")
        else:
            print("    no dupes against recent state")
    else:
        print("  skipping semantic dedup — no prior state yet")

    print(f"  indexed {len(today_items)} items for state")
    state = merge_into_state(state, today_items, today)

    # ---- compute carryover (only meaningful if state was populated previously) ----
    carryover = get_carryover(state, today)
    print(f"  {len(carryover)} carryover items from prior unacked days")
    carryover_html = render_carryover(carryover, today)

    # ---- pre-compute dashboard URL so the email can link to it ----
    dashboard_slug = f"{today.isoformat()}-{uuid.uuid4().hex[:16]}"
    dashboard_url = f"{GITHUB_PAGES_BASE}/{dashboard_slug}.html"

    # ---- render, verify URLs, ship ----
    print("  rendering HTML…")
    html = render_html(today, prioritization, news, whitespace,
                       inbox_html, funder_html, carryover_html,
                       trends, sources_html, publisher_landscape,
                       evidence, tldr=tldr, widgets_html=widgets_html,
                       dashboard_url=dashboard_url,
                       ideas=ideas_html,
                       sources_today=sources_today_html)

    print("  verifying cited URLs…")
    html, bad_urls = verify_urls(html)
    if bad_urls:
        print(f"    stripped {len(bad_urls)} dead link(s): {bad_urls[:3]}…")

    out_path = Path(__file__).parent / "output" / f"{today.isoformat()}-briefing.html"
    out_path.parent.mkdir(exist_ok=True)
    out_path.write_text(html, encoding="utf-8")
    print(f"    saved {out_path}")

    # ---- generate interactive dashboard variant for Pages ----
    print("  generating interactive dashboard…")
    dashboard_html = make_interactive_dashboard(
        html, dashboard_url, ACK_WEBHOOK_URL, today,
    )
    dashboard_path = save_dashboard(dashboard_html, dashboard_slug)
    print(f"    {dashboard_url}")
    print(f"    {dashboard_path}")

    print("  persisting state…")
    write_state(creds, state)
    # Merge weekly (Friday) proposer output with daily proposer output —
    # both target the same `sources` tab and use the same accept/reject
    # route, so they can share the write.
    all_source_proposals = new_source_rows + daily_source_proposals
    if all_source_proposals:
        print(f"  appending {len(all_source_proposals)} proposed sources…")
        append_source_rows(creds, all_source_proposals)

    print("  uploading to Drive…")
    doc_link = upload_drive_doc(creds, html, today)
    print(f"    {doc_link}")

    print("  sending email…")
    send_gmail(creds, html, today)

    print("  posting to Slack…")
    post_slack(doc_link, today, carry_count=len(carryover),
               dashboard_url=dashboard_url)

    print(f"[{today}] done. {len(carryover)} items still awaiting ack.")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        # Top-level safety net so cron doesn't fail silently.
        import traceback
        tb_str = traceback.format_exc()
        print(f"FATAL: {e}", file=sys.stderr)
        print(tb_str, file=sys.stderr)
        # Slack DM so James finds out before the missing 7:30 AM briefing.
        alert_slack_failure(e, tb_str)
        sys.exit(1)
