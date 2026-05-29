# PowerShell wrapper that Windows Task Scheduler invokes daily at 07:30
# to fire the cowork daily briefing. Keeps the schtasks action line
# simple ("powershell.exe -File <this script>") and centralises logging.
#
# Manual test (use this to fire a briefing right now):
#   powershell.exe -ExecutionPolicy Bypass -File `
#     "C:\Users\G09jb\Documents\ClaudeCode_onC\daily-briefing-automation\plugins\daily-briefing-cowork\run-cowork-briefing.ps1"

$ErrorActionPreference = 'Continue'

$RepoRoot   = 'C:\Users\G09jb\Documents\ClaudeCode_onC\daily-briefing-automation'
$SkillPath  = Join-Path $RepoRoot 'plugins\daily-briefing-cowork\skills\daily-briefing.md'
$LogDir     = Join-Path $RepoRoot 'plugins\daily-briefing-cowork\logs'
$Stamp      = Get-Date -Format 'yyyy-MM-dd_HHmm'
$LogFile    = Join-Path $LogDir "$Stamp.log"

if (-not (Test-Path $LogDir)) {
    New-Item -ItemType Directory -Path $LogDir | Out-Null
}

Set-Location $RepoRoot

# Initialise the log up-front so every code path can append to it.
function Write-Log([string]$msg) {
    Add-Content -Path $LogFile -Value $msg -Encoding utf8
}
"=== cowork briefing run started $(Get-Date -Format 'u') ===" | Out-File $LogFile -Encoding utf8

# Fail-loud helper: write to log AND DM Slack so a broken cron run can't
# vanish silently. Reuses daily_briefing.alert_slack_failure (which already
# knows our user ID + token convention). Paths go through env vars to
# avoid Python's `\U` unicode-escape parsing of Windows backslashes.
function Alert-Failure([string]$reason) {
    Write-Log "FATAL: $reason"
    try {
        $env:WRAPPER_REPO = $RepoRoot
        $env:WRAPPER_LOG = $LogFile
        $env:WRAPPER_REASON = $reason
        $py = "import os, sys; sys.path.insert(0, os.environ['WRAPPER_REPO']); from daily_briefing import alert_slack_failure; alert_slack_failure(Exception('cowork wrapper: ' + os.environ['WRAPPER_REASON']), 'see log ' + os.environ['WRAPPER_LOG'])"
        & python -X utf8 -c $py *>> $LogFile
    } catch {
        Write-Log "alert_slack_failure helper itself failed: $_"
    }
}

# Resolve claude.exe. PATH lookup is unreliable under Task Scheduler's
# session, so try the known absolute install first, then PATH, then a
# couple of common alternative install locations. Fail loudly if none
# work — silent "exit 0 with empty log" was the previous failure mode.
$ClaudeCandidates = @(
    'C:\Users\G09jb\.local\bin\claude.exe',
    "$env:USERPROFILE\.local\bin\claude.exe",
    (Get-Command claude -ErrorAction SilentlyContinue).Source,
    "$env:LOCALAPPDATA\Programs\claude\claude.exe",
    "$env:APPDATA\npm\claude.cmd"
)
$ClaudeExe = $ClaudeCandidates | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1

if (-not $ClaudeExe) {
    Alert-Failure ("claude.exe not found. Tried: " + ($ClaudeCandidates -join '; '))
    exit 2
}
Write-Log "claude.exe: $ClaudeExe"

# Invoke the skill via its proper slash command. The plugin lives in
# the repo (not installed at ~/.claude/plugins/), so --plugin-dir
# points claude at it for this session only. claude discovers the
# /daily-briefing command via .claude-plugin/plugin.json + commands/.
#
# IMPORTANT: do NOT pipe the skill markdown via stdin — that worked
# but pre-loaded 34KB into the prompt with no caching, and "Unknown
# command: /daily-briefing" was the symptom when we tried this before
# restructuring the plugin to Claude Code's expected format
# (.claude-plugin/plugin.json + commands/ instead of manifest.json +
# skills/).
#
# --dangerously-skip-permissions is required for unattended runs (the
# skill calls python helpers that write files, hit Google APIs, push
# git, etc). NOT --permission-mode (that flag doesn't exist in this
# CLI; passing it silently exited claude in <10s).
#
# Pipe '' to stdin to suppress the harmless "no stdin data" warning.
# All claude streams append to the log.
$PluginDir = Join-Path $RepoRoot 'plugins\daily-briefing-cowork'
if (-not (Test-Path (Join-Path $PluginDir '.claude-plugin\plugin.json'))) {
    Alert-Failure "plugin manifest missing: $PluginDir\.claude-plugin\plugin.json"
    exit 3
}
Write-Log "plugin dir: $PluginDir"

'' | & $ClaudeExe -p "/daily-briefing" --plugin-dir $PluginDir --dangerously-skip-permissions *>> $LogFile
$claudeExit = $LASTEXITCODE

"=== cowork briefing run finished $(Get-Date -Format 'u') (exit=$claudeExit) ===" |
    Out-File $LogFile -Append -Encoding utf8

if ($claudeExit -ne 0) {
    Alert-Failure "claude.exe exited with $claudeExit (see log)"
}

# Trim logs older than 30 days
Get-ChildItem $LogDir -Filter '*.log' |
    Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-30) } |
    Remove-Item -Force -ErrorAction SilentlyContinue

exit $claudeExit
