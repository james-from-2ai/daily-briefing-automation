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
# knows our user ID + token convention).
function Alert-Failure([string]$reason) {
    Write-Log "FATAL: $reason"
    try {
        # One-liner python; errors here are themselves logged but don't
        # raise (the outer reason is what matters).
        $py = "import sys; sys.path.insert(0, r'$RepoRoot'); from daily_briefing import alert_slack_failure; alert_slack_failure(Exception('cowork wrapper: $reason'), 'see log $LogFile')"
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

# Load the skill prompt verbatim and feed it to claude.exe in headless
# mode. Bypassing permissions because the cron run is unattended and
# needs to write files, run python helpers, push to git, etc.
if (-not (Test-Path $SkillPath)) {
    Alert-Failure "skill prompt missing: $SkillPath"
    exit 3
}
$Prompt = Get-Content -Path $SkillPath -Raw -Encoding UTF8
Write-Log ("skill prompt loaded ({0} chars)" -f $Prompt.Length)

# Run claude. --permission-mode bypassPermissions is required for
# unattended runs (skill calls python helpers that write files, hit
# Google APIs, push git, etc). All streams to the log.
& $ClaudeExe -p $Prompt --permission-mode bypassPermissions *>> $LogFile
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
