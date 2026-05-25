# daily-briefing-cowork plugin (Phase 1 scaffold)

Cowork version of the daily briefing. Synthesis runs in a Claude Code
agent's own reasoning context (billed under Claude.ai subscription)
instead of via `anthropic.Anthropic()` API calls in `daily_briefing.py`.

## Status

✅ **Phase 1 complete (pending production cron-fire).** All helpers
wired up and parsing. End-to-end plumbing smoke-tested against live
APIs. Awaiting first real Windows-Task-Scheduler run for quality
comparison against the Python pipeline.

## Architecture

```
Windows Task Scheduler (or manual invoke)
  ↓
  claude -p "/daily-briefing"
  ↓
skills/daily-briefing.md (agent reads instructions)
  ↓
  python helpers/pull_inputs.py        → JSON of all inputs
  agent synthesizes each section
  python helpers/render_artifacts.py   → email.html + dashboard.html
  python helpers/persist_state.py      → state-sheet updates  [TODO]
  python helpers/deliver.py            → Drive + Gmail + Slack + git push
  ↓
artifacts live, agent exits

(later, manually)
  claude /daily-briefing-followup
  ↓
skills/daily-briefing-followup.md
  ↓
  python helpers/load_today_context.py  [TODO]
  agent walks through items interactively
  per-action helpers (send_reply.py, promote_task.py, etc.)  [TODO]
```

## File layout

```
plugins/daily-briefing-cowork/
├── manifest.json              ← plugin metadata, skill registry
├── README.md                  ← you are here
├── run-cowork-briefing.ps1    ← PowerShell wrapper for Task Scheduler
├── skills/
│   ├── daily-briefing.md            ← autonomous run prompt
│   └── daily-briefing-followup.md   ← interactive Q&A prompt
├── helpers/
│   ├── pull_inputs.py         ← reads calendar/Drive/inbox/state into JSON
│   ├── persist_state.py       ← annotates section HTMLs + dedup + carryover + Sheets write
│   ├── render_artifacts.py    ← renders email.html + dashboard.html
│   ├── deliver.py             ← Drive + Gmail + Slack + git push
│   ├── load_today_context.py  ← (followup) state filtered to today + acks/votes/comments
│   ├── send_reply.py          ← (followup) Gmail in-thread reply
│   ├── promote_task.py        ← (followup) writes to task_proposals via webhook
│   └── add_comment.py         ← (followup) writes to comments tab via webhook
└── logs/                      ← per-run log, auto-pruned after 30 days
```

## How to test (when the scaffold is fleshed out)

Local manual test:
```bash
cd /path/to/daily-briefing-automation
claude -p "/daily-briefing"
```

Inspect: did the email arrive, did the dashboard URL work, did the
state-sheet rows look right? Compare against the same day's Python
output for parity.

Interactive followup test (after a successful autonomous run):
```bash
claude /daily-briefing-followup
```

Walk through what the agent surfaces. Verify that:
- It correctly identifies what you've already actioned via the dashboard
- "Send this drafted reply" actually sends via Gmail
- "Promote to tasks" appends to the task_proposals Sheet tab

## Activating locally — Windows Task Scheduler runbook

The wrapper `run-cowork-briefing.ps1` (sibling to this README) is what
the scheduled task should invoke. It:

- reads the skill prompt from `skills/daily-briefing.md`
- feeds it to `claude.exe -p` in headless mode with
  `--permission-mode bypassPermissions`
- writes `plugins/daily-briefing-cowork/logs/<timestamp>.log` for each run
- auto-prunes logs older than 30 days

### One-time manual smoke test

Before registering with Task Scheduler, run the wrapper by hand and
verify the email arrives + the dashboard URL works:

```powershell
powershell.exe -ExecutionPolicy Bypass -File `
  "C:\Users\G09jb\Documents\ClaudeCode_onC\daily-briefing-automation\plugins\daily-briefing-cowork\run-cowork-briefing.ps1"
```

Then check `plugins/daily-briefing-cowork/logs/<latest>.log` for errors,
and compare the resulting email/dashboard against the same morning's
Python-version briefing.

### Register the scheduled task

Run this PowerShell snippet as your normal user (NOT elevated — Claude
Code needs to inherit your interactive session OAuth + Anthropic creds):

```powershell
$Action = New-ScheduledTaskAction `
  -Execute 'powershell.exe' `
  -Argument '-ExecutionPolicy Bypass -WindowStyle Hidden -File "C:\Users\G09jb\Documents\ClaudeCode_onC\daily-briefing-automation\plugins\daily-briefing-cowork\run-cowork-briefing.ps1"'

$Trigger = New-ScheduledTaskTrigger -Daily -At 7:30am

$Settings = New-ScheduledTaskSettingsSet `
  -AllowStartIfOnBatteries `
  -DontStopIfGoingOnBatteries `
  -StartWhenAvailable `
  -WakeToRun `
  -ExecutionTimeLimit (New-TimeSpan -Minutes 30)

# Principal: run as the currently-logged-on user, only when logged on.
# Required because Claude Code needs your interactive session — won't
# work with "run whether logged on or not" since that uses a non-
# interactive session that can't auth Claude Code.
$Principal = New-ScheduledTaskPrincipal `
  -UserId "$env:USERNAME" `
  -LogonType Interactive `
  -RunLevel Limited

Register-ScheduledTask `
  -TaskName 'DailyBriefingCowork' `
  -Action $Action -Trigger $Trigger -Settings $Settings -Principal $Principal `
  -Description 'Cowork daily briefing — fires Claude Code at 07:30 to produce + send the briefing.'
```

### Confirm + force-run for testing

```powershell
# Verify it registered
Get-ScheduledTask -TaskName 'DailyBriefingCowork' | Format-List *

# Force-run NOW (without waiting until 07:30)
Start-ScheduledTask -TaskName 'DailyBriefingCowork'

# Tail the latest log while it runs
Get-Content (Get-ChildItem 'C:\Users\G09jb\Documents\ClaudeCode_onC\daily-briefing-automation\plugins\daily-briefing-cowork\logs\*.log' |
  Sort-Object LastWriteTime -Desc | Select-Object -First 1) -Wait
```

### Caveats

- **Laptop must be powered on at 07:30** with you logged in. WakeToRun
  is set, so a sleeping laptop should wake; a powered-off laptop won't.
  The existing GitHub Actions cron stays armed as a fallback for laptop-
  off mornings (don't disable it until 5+ consecutive clean cowork runs).
- **Claude Code session auth.** If `claude` requires re-auth (rare),
  the run will hang. The 30-min ExecutionTimeLimit kills the task and
  leaves an error in the log — you'll notice no briefing landed in
  Slack + can re-auth manually.
- **Avoid laptop sleep/lock at 07:30 during early days.** Lid-closed
  with display sleep is usually fine; full hibernate breaks WakeToRun
  on some hardware.

### Disabling the GitHub Actions cron (after ~5 clean runs)

Once cowork runs are reliably landing daily, comment out the schedule
in `.github/workflows/`:

```yaml
on:
  # schedule:           # ← comment these out
  #   - cron: "30 11 * * *"
  #   - cron: "30 12 * * *"
  workflow_dispatch: {}   # keep the manual-trigger fallback
```

This leaves the workflow available for emergency manual runs (e.g.
laptop dead, traveling) but stops the duplicate auto-cron.

### Uninstalling

```powershell
Unregister-ScheduledTask -TaskName 'DailyBriefingCowork' -Confirm:$false
```

## Why this isn't a one-session build

Each section's prompt in `daily_briefing.py` is the product of iteration —
careful wording, examples, edge-case handling. Porting them faithfully to
a single skill prompt takes time + comparison runs to ensure no quality
regression. Phase 1 = scaffold + minimum viable run. Production-quality
iteration follows.
