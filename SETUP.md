# Setup — `daily_briefing.py`

One-time setup, ~30–45 min. After this, the script runs unattended on a daily cron.

## 0. Dependencies

```bash
pip install -r requirements.txt
```

## 1. Anthropic API key

```bash
export ANTHROPIC_API_KEY="sk-ant-..."
```
Add it to your shell profile (or to `~/.config/2ai-briefing/env`) so cron picks it up too.

## 2. Google OAuth (Drive + Calendar + Gmail + Docs)

The script needs OAuth credentials with these four APIs enabled.

1. Go to <https://console.cloud.google.com>. Create a project (or pick an existing one). The project just holds your OAuth client — nothing is billed if you only call the APIs from your own account.
2. **APIs & Services → Library**: enable each of
   - Google Drive API
   - Google Calendar API
   - Gmail API
   - Google Docs API
3. **OAuth consent screen**:
   - User type: **External**.
   - App name: "2AI daily briefing" (or whatever).
   - Add yourself (`james@aiaccessinitiative.org`) under "Test users". You don't need to publish or get verified — Test User access is enough for personal automation.
   - Scopes: leave default; the script declares its own.
4. **Credentials → Create credentials → OAuth client ID**:
   - Application type: **Desktop app**.
   - Download the JSON. Save it as `~/.config/2ai-briefing/client_secret.json`.

5. First-run consent. From a machine with a browser:
   ```bash
   mkdir -p ~/.config/2ai-briefing
   # client_secret.json should already be in there
   python daily_briefing.py
   ```
   A browser window opens; sign in with `james@aiaccessinitiative.org`, click through "unverified app" (it's your own app), allow all four scopes. The refresh token is cached to `~/.config/2ai-briefing/token.json`. After this, the script runs headless.

   If you want to do the consent flow on a different machine than where the cron runs, just copy `token.json` across once. The refresh token doesn't expire under normal use.

## 3. Slack bot token (optional but you asked for it)

If you already have a Slack bot in the workspace, reuse its token. Otherwise:

1. <https://api.slack.com/apps> → **Create New App** → "From scratch". Name it "2AI Daily Briefer", pick the AIAI workspace.
2. **OAuth & Permissions → Bot Token Scopes**: add `chat:write` and `im:write`.
3. Install to workspace, copy the `xoxb-...` Bot User OAuth Token.
4. DM the bot once from your account so it can DM you back (Slack requires this on first contact).
5. Export the token where cron will see it:
   ```bash
   export SLACK_BOT_TOKEN="xoxb-..."
   ```

If `SLACK_BOT_TOKEN` is unset the script skips the Slack DM with a warning — email + Drive still work.

## 4. Verify it works

```bash
python daily_briefing.py
```

You should see:
```
[2026-MM-DD] starting daily briefing
  pulling calendar…
  …
  sending email…
[2026-MM-DD] done
```

Check `output/<date>-briefing.html` locally, your inbox, Drive (folder "02 Dept X - Testing and Experimentation"), and Slack.

## 5. Feedback loop (optional but designed in)

The script gets better over time by reading your ratings of past briefings and biasing the synth + critic prompts toward what landed.

1. Create a new Google Form ("2AI Briefing Feedback"). Fields:
   - `Briefing date` (short answer; pre-fills from the email link)
   - `Prioritization rating` (linear scale 1-5)
   - `News rating` (linear scale 1-5)
   - `What helped today` (paragraph)
   - `What didn't` (paragraph)
2. **Responses** tab → link to a Google Sheet. Open the linked Sheet; the script reads it directly.
3. In `daily_briefing.py`, set:
   ```python
   FEEDBACK_SHEET_ID = "<id from the Sheet URL>"
   FEEDBACK_FORM_URL = "https://docs.google.com/forms/d/e/.../viewform"
   ```
4. In the Form's "Get prefilled link" view, set the `Briefing date` field to anything, click "Get link", copy. The script appends `&entry.<id>=<today>` automatically by passing `entry.briefing_date`. If your field ID is different, edit the URL template in `render_html()`.
5. (Optional) Add the Form to your phone home screen so rating takes 20 seconds.

After ~10 days of ratings, the critic pass starts noticeably skewing toward your taste.

## 6. State + acknowledgment loop

The script keeps a durable record of every item it surfaces, so:
- Yesterday's flags don't vanish if you didn't read the briefing — they roll forward.
- Action items pulled from your 1:1 notes are tracked across days with a "this has been open N days" badge.
- News topics and white-space items don't repeat across consecutive briefings.

Two Google Sheets back this, plus one tiny Apps Script web app.

### State sheets

1. Create a new Google Sheet called **`2AI Briefing State`**. Two tabs:
   - `Sheet1` (the state itself). The script writes its own header on first run.
   - `acks` — rename a second tab to this. Header row, columns A–C:
     ```
     briefing_date   acknowledged_at   done_keys
     ```
2. Copy the spreadsheet ID from the URL. Set both:
   ```python
   STATE_SHEET_ID = "<id>"
   ACK_SHEET_ID   = "<id>"   # same sheet, the script reads tab "acks" by name
   ```
   (If you'd rather split them across two files, that works too — just give each its own ID.)

### Sheet tabs

The state spreadsheet needs four tabs total (the script reads them by name):

| Tab     | Purpose                                | Header row                                                              |
| ------- | -------------------------------------- | ----------------------------------------------------------------------- |
| `Sheet1` | All briefing items (state)             | _(script writes its own on first run)_                                  |
| `acks`   | "Mark as seen" / "mark done" events    | `briefing_date`  `acknowledged_at`  `done_keys`                         |
| `votes`  | 👍/👎 votes on topic items             | `voted_at`  `briefing_date`  `item_key`  `vote`                         |
| `sources`| Proposed and accepted news sources     | `source_id`  `proposed_at`  `status`  `source_name`  `source_url`  `source_query` |

Create all four tabs now; leave them empty apart from the header rows above.

### Webhook (Apps Script, ~5 min)

The "Mark as seen", "mark done", "👍 more / 👎 less", and source accept/reject links all hit one endpoint that routes by parameters.

1. From the State sheet: **Extensions → Apps Script**. Paste:
   ```javascript
   const SS = SpreadsheetApp.getActive();
   const ACK     = SS.getSheetByName('acks');
   const VOTES   = SS.getSheetByName('votes');
   const SOURCES = SS.getSheetByName('sources');

   function doGet(e) {
     const p = e.parameter;
     const now = new Date().toISOString();
     let msg;

     if (p.source_id && p.action) {
       // Accept/reject a proposed source. Update the row's status in place.
       const rows = SOURCES.getDataRange().getValues();
       for (let i = 1; i < rows.length; i++) {
         if (rows[i][0] === p.source_id) {
           SOURCES.getRange(i + 1, 3).setValue(p.action);  // status column
           break;
         }
       }
       msg = p.action === 'accept'
         ? '👍 Added to watchlist. Will appear in tomorrow\'s briefing.'
         : '👎 Skipped. Won\'t propose again.';
     } else if (p.vote && p.key) {
       VOTES.appendRow([now, p.date || '', p.key, p.vote]);
       msg = p.vote === 'up'
         ? '👍 More like this. Got it — tomorrow\'s picks will lean in.'
         : '👎 Less like this. Got it — tomorrow\'s picks will steer away.';
     } else {
       const date = p.date || now.slice(0, 10);
       ACK.appendRow([date, now, p.keys || '']);
       msg = p.keys
         ? '✅ ' + p.keys.split(',').length + ' item(s) marked done.'
         : '✅ Briefing acknowledged.';
     }
     return HtmlService.createHtmlOutput(
       '<p style="font-family:sans-serif;font-size:16px;">' + msg + '</p>'
     );
   }
   ```
2. **Deploy → New deployment → Web app**. Execute as: **Me**. Who has access: **Anyone** (the URL is unguessable — that's the gate; nothing sensitive flows through it).
3. Copy the web-app URL. Set in `daily_briefing.py`:
   ```python
   ACK_WEBHOOK_URL = "https://script.google.com/macros/s/AKfy.../exec"
   ```
4. Smoke test:
   - `<webhook>?date=2026-05-23` → "✅ Briefing acknowledged.", row in `acks`.
   - `<webhook>?vote=up&key=abc123&date=2026-05-23` → "👍 More like this", row in `votes`.
   - `<webhook>?source_id=test&action=accept` → "👍 Added to watchlist", row in `sources` (if one with `source_id=test` exists).

### How carryover works

- Every run writes today's items (priorities, slip flags, decisions, action items, inbox, funder, news, white-space) to the state sheet with a stable hash key.
- If yesterday wasn't acknowledged (no row in `acks` matching yesterday's date), today's briefing includes a red banner at the top: **"⏰ Pending from earlier — N items you haven't acknowledged."**
- Items keep their `carry_count` (days unack'd). Anything past `STALE_DAYS` (3 by default) gets a red badge instead of orange.
- Clicking "Mark today as seen" acknowledges the whole briefing — items still won't disappear, but the red banner goes away tomorrow.
- Clicking an item's "mark done" link writes its key to `acks.done_keys`. Next run flips that row's status to `done` and it stops carrying forward.
- The script will never silently drop an item. To get rid of one without doing it, click "mark done" — it's an explicit dismissal.

## 7. Program-area constants

The weekly white-space analysis (`PROGRAM_AREAS = ["health", "agriculture", "education"]`) uses title- and full-text keyword matching against your Drive. If you organise program docs into named folders, swap the matching in `pull_program_area_corpus()` to `parentId = '<folder-id>'` queries — much higher signal-to-noise.

Edit the alias map in that function to match your taxonomy (e.g. add "MNH" or "CHW" to the `health` list).

## 8. Schedule it for 7:30am daily

### Option A — cron (laptop or server that's awake at 7:30am)

```bash
crontab -e
# Add:
30 7 * * * cd /path/to/2ai-hack-monsoon && /usr/bin/env -i \
    HOME="$HOME" PATH="/usr/local/bin:/usr/bin" \
    ANTHROPIC_API_KEY="sk-ant-..." SLACK_BOT_TOKEN="xoxb-..." \
    /path/to/python daily_briefing.py >> ~/briefing.log 2>&1
```

### Option B — GitHub Actions (recommended; runs even when your laptop is closed)

Create `.github/workflows/daily-briefing.yml`:

```yaml
name: Daily briefing
on:
  schedule:
    - cron: "30 11 * * *"   # 7:30am ET = 11:30 UTC (adjust for DST)
  workflow_dispatch: {}
jobs:
  run:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.11" }
      - run: pip install -r requirements.txt
      - name: Restore Google token
        run: |
          mkdir -p ~/.config/2ai-briefing
          echo "${{ secrets.GOOGLE_CLIENT_SECRET }}" > ~/.config/2ai-briefing/client_secret.json
          echo "${{ secrets.GOOGLE_TOKEN }}"         > ~/.config/2ai-briefing/token.json
      - run: python daily_briefing.py
        env:
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
          SLACK_BOT_TOKEN:   ${{ secrets.SLACK_BOT_TOKEN }}
```

Add the four secrets in the repo's **Settings → Secrets and variables → Actions**:
`ANTHROPIC_API_KEY`, `SLACK_BOT_TOKEN`, `GOOGLE_CLIENT_SECRET` (paste the whole JSON), `GOOGLE_TOKEN` (paste the whole JSON from `~/.config/2ai-briefing/token.json` after the local consent flow).

> Heads-up: GitHub's scheduled crons can run 5–15 min late. 7:30am ET requested → set `30 11 * * *` and accept "by 7:45am".

## Tuning knobs

All hardcoded near the top of `daily_briefing.py`:

### Sections and cadence

| Section | Runs | What it does |
| --- | --- | --- |
| Prioritization + slip flags + decisions | Daily | Synthesis from calendar + Drive activity + 1:1 notes. |
| Carryover banner | Daily (if anything pending) | Items from prior days you haven't acknowledged. |
| Inbox triage | Daily | Buckets recent Gmail threads into decide-today / this-week / FYI. |
| Funder watchlist | Daily | Tier-0 check across the 5 named funders. |
| News deep-dives | Daily | 6 topics from the Opal monitoring sheet, web-search powered. |
| Evidence digest (RCTs / preprints) | Twice weekly — Tue + Thu | Two streams: AI capabilities + Weather/health × AI. Sources via Claude web-search across consensus.app, arxiv, biorxiv, medrxiv, Lancet AI, NEJM AI, OpenReview, METR. |
| 👍/👎 controls | Daily (on topic items) | Click to bias tomorrow's picks. |
| White-space (program areas) | Weekly — Mon | What the field's moving on that 2AI's Drive isn't. |
| Trends + opportunity spaces | Weekly — Wed | Pattern-spots across 60 days of indexed items. |
| New source proposer | Weekly — Fri | 4 candidates with accept/reject; accepted ones join the rotation. |
| Peer-publisher landscape | Monthly — first weekday | Profiles CGD/PxD/Rethink/J-PAL/etc. and sector-wide publishing gaps. |

### Consensus / evidence digest — note on API

The $10/mo Consensus consumer plan grants Pro-message and Deep-review access in the **web UI**, not via a public API. The script therefore uses Claude's `web_search` tool with `allowed_domains` set to `consensus.app` + the preprint servers it indexes (arxiv, biorxiv, medrxiv, Lancet AI, NEJM AI, OpenReview, METR, Epoch). This gives a Consensus-flavoured pull without consuming your Pro messages.

If you upgrade to Consensus API access later (or get an enterprise key), replace the body of `synthesize_evidence_digest()` with a direct API call. The function signature stays the same; the rest of the pipeline (👍/👎, state, dedup) needs no changes. Hold onto your monthly 15 Deep reviews for the heaviest synthesis questions — those are best done in the web UI directly, not on autopilot.

### Tuning knobs

| Constant | What it does |
| --- | --- |
| `ONEONONE_DOCS` | Add / swap manager 1:1 docs as new managers join. |
| `NEWS_TOPICS_SHEET_ID` | Point to a different topics sheet if you reorganise. |
| `BRIEFINGS_DRIVE_FOLDER_ID` | Where the daily Doc gets uploaded. |
| `NEWS_DEEP_DIVE_TOPICS` | 6 by default. Each topic = one Claude web-search call (~$0.05). |
| `NEWS_DEDUP_LOOKBACK_DAYS` | News topics surfaced in the last N days are excluded from today's picker. |
| `FUNDER_WATCHLIST` | Tier-0 daily check across these funders. Each adds one Claude web-search call. |
| `PEER_PUBLISHERS` | Monthly peer-publisher landscape covers this list. Add/remove orgs here. |
| `INBOX_TRIAGE_MAX` | Cap on Gmail threads triaged per run (12 = ~30 sec of Claude time). |
| `STALE_DAYS` | Items unack'd for this many days flip from orange to red in the carryover banner. |
| `MAX_CARRY_ITEMS` | Safety cap on the size of the "Pending from earlier" section. |
| `WHITESPACE_WEEKDAY` / `TRENDS_WEEKDAY` / `SOURCES_WEEKDAY` | Which weekday each weekly section runs (0=Mon … 6=Sun). |
| `TRENDS_LOOKBACK_DAYS` | How far back the trends pass scans the state sheet. |
| `SOURCES_PROPOSE_N` | How many source candidates the proposer surfaces per run. |
| `PUBLISHER_LANDSCAPE_LOOKBACK_DAYS` | How far back the monthly publisher landscape looks. |
| `CLAUDE_MODEL` / `CLAUDE_RESEARCH_MODEL` | Synth and research models. Sonnet for research cuts cost ~5x. |
| `CALENDAR_LOOKAHEAD_DAYS` | How far ahead the script considers when flagging slippage. |

## Cost back-of-envelope

Per daily run (no white-space, no carryover overflow):
- 1 prioritization call (Opus, ~3k in / 1.5k out)
- 1 critic pass (Opus, full draft in + revised draft out)
- 1 inbox triage call (Opus, small)
- 1 news picker call + 6 news deep-research calls (Opus + web search)
- 5 funder watchlist calls (Opus + web search)

≈ **$1.00–$1.50/day**, ~$30–45/mo. Monday's white-space run adds 3 more research calls ($0.30). Swapping `CLAUDE_RESEARCH_MODEL` to Sonnet halves the bill.

## Troubleshooting

- **"invalid_grant" from Google**: token expired (rare, but happens if you don't run for ~6 months, or revoke access). Delete `~/.config/2ai-briefing/token.json` and rerun once with a browser.
- **Gmail send fails with 403**: Gmail API not enabled in the GCP project, or `gmail.send` scope missing from `token.json`. Delete the token and re-consent.
- **Drive upload to folder fails**: the folder ID in `BRIEFINGS_DRIVE_FOLDER_ID` is a shared-drive folder — your account needs write access. Move it to a folder you own, or grant your account edit access.
- **Slack DM fails with `channel_not_found`**: DM the bot once from Slack first; bots can't initiate DMs to users who've never interacted with them.
- **Topics sheet returns empty / garbled**: it's an `.xlsx` (Excel) and not a native Google Sheet. The export-as-text helper handles both, but if formatting is messy, "Save as Google Sheet" once in the UI.
