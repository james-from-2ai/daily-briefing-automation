# PowerShell wrapper that Windows Task Scheduler invokes daily at 07:30
# to fire the cowork daily briefing. Keeps the schtasks action line
# simple ("powershell.exe -File <this script>") and centralises logging.
#
# Manual test:
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

# Load the skill prompt verbatim and feed it to claude.exe in headless
# mode. Bypassing permissions because the cron run is unattended and
# needs to write files, run python helpers, push to git, etc.
$Prompt = Get-Content -Path $SkillPath -Raw -Encoding UTF8

# `claude` is expected on PATH; if it's not, hardcode the absolute path
# returned by `(Get-Command claude).Source` here.
$ClaudeExe = (Get-Command claude -ErrorAction SilentlyContinue).Source
if (-not $ClaudeExe) {
    $ClaudeExe = "$env:LOCALAPPDATA\Programs\claude\claude.exe"
}

"=== cowork briefing run started $(Get-Date -Format 'u') ===" | Out-File $LogFile -Encoding utf8

# Run claude. --permission-mode bypassPermissions is needed for
# unattended runs (skill calls python helpers that write files + push
# git). Output goes to the log so we can diagnose failures.
& $ClaudeExe -p $Prompt --permission-mode bypassPermissions *>> $LogFile

"=== cowork briefing run finished $(Get-Date -Format 'u') (exit=$LASTEXITCODE) ===" | Out-File $LogFile -Append -Encoding utf8

# Trim logs older than 30 days
Get-ChildItem $LogDir -Filter '*.log' |
    Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-30) } |
    Remove-Item -Force -ErrorAction SilentlyContinue

exit $LASTEXITCODE
