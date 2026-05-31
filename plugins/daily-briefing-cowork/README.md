# daily-briefing-cowork plugin

Local-laptop version of the 2AI daily briefing. Synthesis runs in a
Claude Code session's reasoning (billed against your Claude.ai
subscription) instead of via the ~14 `anthropic.Anthropic()` API
calls that `daily_briefing.py` made on GitHub Actions. Cost drops
from ~$4/day to ~$0.02/day (Haiku dedup only).

Also includes a **live tasks dashboard** that refreshes every 2 hours
on a separate, pure-Python cron — no LLM in that loop.

## Status

✅ **Phase 1 in production.** Both crons firing daily on Windows Task
Scheduler:
- `DailyBriefingCowork` — 07:30 daily, produces email + Slack +
  Drive Doc + GitHub Pages dashboard.
- `TasksLiveRefresh` — every 2 hours from 08:00 to 22:00, refreshes
  `tasks-live.html` from `tasks.json` + briefing feedback.

🔧 **Phase 2 built — pending env secrets + test fire.** Scheduled
remote-agent variant. Code is in place (`helpers/phase2_bootstrap.py`,
`helpers/tasks_bridge.py`, skill Step 0); reuses the Phase-1 helpers
unchanged with Google creds surfaced as an env secret (MCP can't send
email or do the Sheets state core — see `PHASE2_DEPLOY.md`). Remaining:
James sets the routine env secrets, then a one-off test fire before the
daily cron is armed. Runbook: `PHASE2_DEPLOY.md`.

## Architecture

### Morning briefing (07:30 daily)

```
Windows Task Scheduler
  ↓
run-cowork-briefing.ps1
  ↓
  pipes commands/daily-briefing.md → claude.exe -p --dangerously-skip-permissions
  ↓
claude (headless) follows the skill prompt:
  1. helpers/pull_inputs.py       → /tmp/briefing-inputs.json
  2. agent synthesizes each section (no API calls)
  3. helpers/persist_state.py     → annotate + dedup + carryover + Sheets write
  4. helpers/render_artifacts.py  → email.html + dashboard.html
  5. helpers/deliver.py           → Drive + Gmail + Slack + git push
  ↓
Pages workflow auto-deploys docs/*.html → dashboard URL goes live
```

### Live tasks dashboard (every 2h, pure Python)

```
Windows Task Scheduler
  ↓
run-tasks-live.ps1   (no claude.exe — fully deterministic)
  ↓
  1. helpers/sync_feedback_to_tasks.py → tasks.json
     · accept:K  → promote suggestion to active task
     · reject:K  → mark proposal rejected (tasks.json untouched)
     · bare K    → mark matching task done
  2. helpers/render_tasks_dashboard.py → docs/tasks-live.html
  3. git add -f docs/tasks-live.html + commit + push
  ↓
deploy-pages.yml workflow publishes → tasks-live URL refreshes
```

### Followup (manual, after morning briefing)

```
claude /daily-briefing-followup    (manual invocation)
  ↓
commands/daily-briefing-followup.md instructs:
  1. helpers/load_today_context.py → today's state + acks/votes/comments
  2. agent walks through items interactively
  3. per-action helpers: send_reply.py, promote_task.py, add_comment.py
```

## File layout

```
plugins/daily-briefing-cowork/
├── .claude-plugin/
│   └── plugin.json                  ← Claude Code plugin manifest
├── README.md                        ← you are here
├── PHASE2_SETUP.md                  ← Phase 2 prereqs + rationale (build steps superseded)
├── PHASE2_DEPLOY.md                 ← Phase 2 as-built deploy runbook + routine spec
├── run-cowork-briefing.ps1          ← 07:30 daily briefing wrapper
├── run-tasks-live.ps1               ← every-2h tasks dashboard wrapper
├── commands/
│   ├── daily-briefing.md            ← morning briefing skill prompt
│   └── daily-briefing-followup.md   ← interactive Q&A prompt
├── helpers/
│   ├── phase2_bootstrap.py          ← Phase 2: decode creds from env + git auth (cloud)
│   ├── tasks_bridge.py              ← Phase 2: tasks.json ↔ Drive bridge (write + read)
│   ├── pull_inputs.py               ← briefing: read all upstream sources → JSON
│   ├── persist_state.py             ← briefing: annotate + dedup + state-sheet
│   ├── render_artifacts.py          ← briefing: email + dashboard HTML
│   ├── deliver.py                   ← briefing: Drive + Gmail + Slack + push
│   ├── sync_feedback_to_tasks.py    ← tasks-live: Sheet → tasks.json sync
│   ├── render_tasks_dashboard.py    ← tasks-live: docs/tasks-live.html
│   ├── load_today_context.py        ← followup: today-filtered state
│   ├── send_reply.py                ← followup: Gmail in-thread reply
│   ├── promote_task.py              ← followup: write to task_proposals
│   └── add_comment.py               ← followup: write to comments tab
└── logs/                            ← per-run logs, auto-pruned after 30 days
```

All helpers import from `../daily_briefing.py` at the repo root —
that file remains the shared engine (OAuth flow, Sheet readers,
HTML rendering, state-sheet schema). The cowork helpers are thin
orchestrators on top.

## Interactive Slack — one-time setup (gotchas)

Replying to the AutomatedBriefing DM (`done S1` / `task P2` / `note D1 …`)
requires the Slack app to be configured so the bot can receive + read your
replies. If replies aren't being picked up, check, in order:

1. **App Home → Show Tabs → Chat/Messages Tab:** "Allow users to send Slash
   commands and messages from the chat tab" must be **checked**.
2. **Agents & AI Apps → Agent or Assistant:** must be **OFF**. When on, it
   *replaces the messages tab* with an assistant pane and routes your
   messages into assistant threads (delivered via Events API, not the
   `conversations.history` poll the scraper uses).
3. **Reinstall the app to the workspace** (OAuth & Permissions →
   "Reinstall to Workspace"). **This is the step people miss** — the two
   settings above only take effect after a reinstall. The persistent
   "Sending messages to this app has been turned off" banner = not yet
   reinstalled.
4. **Bot scopes** (no user-token scopes needed): `im:history`, `im:read`,
   `im:write`, `chat:write`. All present by default.

Note: replies often arrive as **thread replies**, not top-level DM messages
— `scrape_slack_replies.py` walks threads on the bot's posts, so both work.
Reinstalling the same workspace keeps the bot token, so `SLACK_BOT_TOKEN`
stays valid (if posting ever breaks post-reinstall, the token rotated —
update the env var).

## Where to edit configuration

| What | Where | Edit how |
|---|---|---|
| News topics (daily picker source) | Google Sheet `14KtogU6W-eRD-S6yE48w-XPTGuhyqEa32kdGYAa-BYU` | Open the sheet, edit rows |
| Funder watchlist (every-other-day) | `daily_briefing.py` `FUNDER_WATCHLIST` (line ~132) | Code edit |
| Peer publishers (monthly landscape) | `daily_briefing.py` `PEER_PUBLISHERS` (line ~185) | Code edit |
| Accepted news sources | `sources` tab on state sheet | Briefing dashboard ✅ / ❌ buttons |
| 1:1 docs (Katie, Sarah) | `daily_briefing.py` `ONEONONE_DOCS` (line ~63) | Code edit |
| Apps Script webhook URL | `daily_briefing.py` `ACK_WEBHOOK_URL` (line ~103) | Code edit |
| State sheet ID | `daily_briefing.py` `STATE_SHEET_ID` (line ~101) | Code edit |
| tasks.json location | `daily_briefing.py` `TASKS_JSON_PATH` (line ~243) | Code edit |

Note: moving `FUNDER_WATCHLIST` + `PEER_PUBLISHERS` out of code into
Sheets is on the roadmap so all source-config can be edited without
a commit.

## Running manually

```powershell
# Morning briefing (takes 10-20 min, sends real comms)
Start-ScheduledTask -TaskName 'DailyBriefingCowork'

# Live tasks dashboard refresh (~10s, no comms)
Start-ScheduledTask -TaskName 'TasksLiveRefresh'

# Tail the latest log
Get-Content (Get-ChildItem 'plugins\daily-briefing-cowork\logs\*.log' `
  | Sort LastWriteTime -Desc | Select -First 1) -Wait
```

## Failure handling

Both wrappers DM Slack via `daily_briefing.alert_slack_failure` on
any step error. Silent failures (the original "task fired but
nothing happened" mode) are no longer reachable — every wrapper
path either succeeds and writes a "finished" line, or fails loudly
with a `FATAL:` line and a Slack DM.

The morning briefing has additional resilience:
- `upload_drive_doc` retries on 5xx and is non-fatal (briefing
  still ships if Drive is flaky)
- `read_acks` reads the correct `acks` tab (was the wrong tab
  pre-fix; that bug caused "marked done items kept coming back")
- Email-version anchors point at the dashboard, not the webhook
  (the "Sorry, unable to open" `/u/1/` issue)

## Activation (one-time setup)

Both Task Scheduler entries are already registered. To re-register
from scratch (e.g., on a new machine):

```powershell
# Morning briefing — 07:30 daily, Interactive logon required
$Action = New-ScheduledTaskAction -Execute 'powershell.exe' `
  -Argument '-ExecutionPolicy Bypass -WindowStyle Hidden -File ' +
            '"<repo-root>\plugins\daily-briefing-cowork\run-cowork-briefing.ps1"'
$Trigger = New-ScheduledTaskTrigger -Daily -At 7:30am
$Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
  -DontStopIfGoingOnBatteries -StartWhenAvailable -WakeToRun `
  -ExecutionTimeLimit (New-TimeSpan -Minutes 90)
$Principal = New-ScheduledTaskPrincipal -UserId "$env:USERNAME" `
  -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName 'DailyBriefingCowork' `
  -Action $Action -Trigger $Trigger -Settings $Settings -Principal $Principal
```

```powershell
# Tasks-live — every 2h from 08:00 to 22:00
$Action = New-ScheduledTaskAction -Execute 'powershell.exe' `
  -Argument '-ExecutionPolicy Bypass -WindowStyle Hidden -File ' +
            '"<repo-root>\plugins\daily-briefing-cowork\run-tasks-live.ps1"'
$Trigger = New-ScheduledTaskTrigger -Once -At (Get-Date "08:00") `
  -RepetitionInterval (New-TimeSpan -Hours 2) `
  -RepetitionDuration (New-TimeSpan -Hours 14)
$Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
  -DontStopIfGoingOnBatteries -StartWhenAvailable -WakeToRun `
  -ExecutionTimeLimit (New-TimeSpan -Minutes 10)
$Principal = New-ScheduledTaskPrincipal -UserId "$env:USERNAME" `
  -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName 'TasksLiveRefresh' `
  -Action $Action -Trigger $Trigger -Settings $Settings -Principal $Principal
```

## Uninstall

```powershell
Unregister-ScheduledTask -TaskName 'DailyBriefingCowork' -Confirm:$false
Unregister-ScheduledTask -TaskName 'TasksLiveRefresh'    -Confirm:$false
```

The GitHub Actions cron (the original Python pipeline) remains
available via `workflow_dispatch` as an emergency fallback if your
laptop is off for several days. See `.github/workflows/daily-briefing.yml`.
