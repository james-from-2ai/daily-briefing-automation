# Wrapper for the live tasks dashboard cron. Runs every 2 hours via
# Windows Task Scheduler.
#
# Pure Python (no claude.exe): sync tasks.json from briefing feedback,
# render the dashboard, force-push to docs/ so the deploy-pages
# workflow publishes it.
#
# Manual test:
#   powershell.exe -ExecutionPolicy Bypass -File `
#     "C:\Users\G09jb\Documents\ClaudeCode_onC\daily-briefing-automation\plugins\daily-briefing-cowork\run-tasks-live.ps1"

$ErrorActionPreference = 'Continue'

$RepoRoot = 'C:\Users\G09jb\Documents\ClaudeCode_onC\daily-briefing-automation'
$Helpers  = Join-Path $RepoRoot 'plugins\daily-briefing-cowork\helpers'
$LogDir   = Join-Path $RepoRoot 'plugins\daily-briefing-cowork\logs'
$Stamp    = Get-Date -Format 'yyyy-MM-dd_HHmm'
$LogFile  = Join-Path $LogDir "tasks-live_$Stamp.log"

if (-not (Test-Path $LogDir)) {
    New-Item -ItemType Directory -Path $LogDir | Out-Null
}
Set-Location $RepoRoot

function Write-Log([string]$msg) {
    Add-Content -Path $LogFile -Value $msg -Encoding utf8
}
"=== tasks-live run started $(Get-Date -Format 'u') ===" | Out-File $LogFile -Encoding utf8

function Alert-Failure([string]$reason) {
    Write-Log "FATAL: $reason"
    try {
        $env:WRAPPER_REPO = $RepoRoot
        $env:WRAPPER_LOG = $LogFile
        $env:WRAPPER_REASON = $reason
        $py = "import os, sys; sys.path.insert(0, os.environ['WRAPPER_REPO']); from daily_briefing import alert_slack_failure; alert_slack_failure(Exception('tasks-live: ' + os.environ['WRAPPER_REASON']), 'see log ' + os.environ['WRAPPER_LOG'])"
        & python -X utf8 -c $py *>> $LogFile
    } catch {
        Write-Log "alert_slack_failure helper failed: $_"
    }
}

# Step 1a: scrape Slack DM replies into task_proposals (non-fatal).
Write-Log "--- step 1a: scrape_slack_replies ---"
& python -X utf8 (Join-Path $Helpers 'scrape_slack_replies.py') *>> $LogFile
if ($LASTEXITCODE -ne 0) {
    Write-Log "  scrape_slack_replies exited $LASTEXITCODE (non-fatal, continuing)"
}

# Step 1b: detect new 1:1 slips from Katie + Sarah docs (non-fatal).
Write-Log "--- step 1b: extract_1on1_slips ---"
& python -X utf8 (Join-Path $Helpers 'extract_1on1_slips.py') *>> $LogFile
if ($LASTEXITCODE -ne 0) {
    Write-Log "  extract_1on1_slips exited $LASTEXITCODE (non-fatal, continuing)"
}

# Step 2: sync feedback (task_proposals + acks) into tasks.json
Write-Log "--- step 2: sync_feedback_to_tasks ---"
& python -X utf8 (Join-Path $Helpers 'sync_feedback_to_tasks.py') *>> $LogFile
if ($LASTEXITCODE -ne 0) {
    Alert-Failure "sync_feedback_to_tasks.py exit=$LASTEXITCODE"
    exit $LASTEXITCODE
}

# Step 3: render the dashboard
Write-Log "--- step 3: render_tasks_dashboard ---"
& python -X utf8 (Join-Path $Helpers 'render_tasks_dashboard.py') *>> $LogFile
if ($LASTEXITCODE -ne 0) {
    Alert-Failure "render_tasks_dashboard.py exit=$LASTEXITCODE"
    exit $LASTEXITCODE
}

# Step 4: force-add (gitignored), commit, push so deploy-pages publishes
Write-Log "--- step 4: git push docs/tasks-live.html ---"
$DashFile = 'docs/tasks-live.html'
if (-not (Test-Path (Join-Path $RepoRoot $DashFile))) {
    Alert-Failure "rendered file missing: $DashFile"
    exit 4
}
& git add -f $DashFile *>> $LogFile
# Only commit if the file actually changed (don't spam empty commits).
$diff = & git diff --cached --name-only
if (-not $diff) {
    Write-Log "no changes - skipping commit"
} else {
    & git commit -m "tasks-live: $(Get-Date -Format 'u') refresh" *>> $LogFile
    & git push *>> $LogFile
    if ($LASTEXITCODE -ne 0) {
        Alert-Failure "git push exit=$LASTEXITCODE"
        exit $LASTEXITCODE
    }
    Write-Log "pushed."
}

# Step 5: refresh the Phase-2 tasks.json Drive bridge (non-fatal).
# The scheduled remote-agent briefing can't see this laptop's OneDrive, so
# it reads tasks from this Drive copy. Runs every 2h alongside the dashboard
# refresh; a failure here must NOT affect the (load-bearing) Phase-1 cron.
Write-Log "--- step 5: tasks_bridge (Drive copy for Phase 2) ---"
& python -X utf8 (Join-Path $Helpers 'tasks_bridge.py') *>> $LogFile
if ($LASTEXITCODE -ne 0) {
    Write-Log "  tasks_bridge exited $LASTEXITCODE (non-fatal, continuing)"
}

"=== tasks-live run finished $(Get-Date -Format 'u') (exit=0) ===" |
    Out-File $LogFile -Append -Encoding utf8

# Trim logs older than 30 days
Get-ChildItem $LogDir -Filter 'tasks-live_*.log' |
    Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-30) } |
    Remove-Item -Force -ErrorAction SilentlyContinue

exit 0
