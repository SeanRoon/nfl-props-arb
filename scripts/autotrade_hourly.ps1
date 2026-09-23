# Hourly autotrade run, for Windows Task Scheduler.
#
# Register it (from an elevated prompt, once you have reviewed dry-run output):
#
#   schtasks /Create /TN "nflprops-autotrade" /SC HOURLY `
#     /TR "powershell -NoProfile -ExecutionPolicy Bypass -File C:\Users\Sean\nfl-props-arb\scripts\autotrade_hourly.ps1" `
#     /RL LIMITED
#
# Then fix the settings schtasks cannot set. The defaults skip every run on
# battery, never make up a run missed during sleep, and let a hung run block
# the schedule for 72 hours (a normal run takes ~10 s):
#
#   $t = Get-ScheduledTask -TaskName nflprops-autotrade
#   $t.Settings.DisallowStartIfOnBatteries = $false
#   $t.Settings.StopIfGoingOnBatteries = $false
#   $t.Settings.StartWhenAvailable = $true
#   $t.Settings.ExecutionTimeLimit = 'PT30M'
#   Set-ScheduledTask -InputObject $t
#
# Nothing runs while the machine is asleep; one catch-up run follows wake.
#
# Stop it without touching the scheduler:  New-Item data\HALT
# Start it again:                          Remove-Item data\HALT

$ErrorActionPreference = 'Stop'

# The config paths in this package are RELATIVE (see baselines.DEFAULT_PATH), so a
# wrong working directory silently loads built-in defaults instead of the
# operator's limits. Pin it to the repo rather than trusting the caller's cwd.
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo

$logDir = Join-Path $repo 'logs'
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }
$log = Join-Path $logDir ("autotrade-{0}.log" -f (Get-Date -Format 'yyyy-MM-dd'))

"=== $((Get-Date).ToUniversalTime().ToString('u')) run start ===" | Add-Content -Path $log -Encoding utf8

# LIVE since 2026-09-23 (operator instruction), $100 all-in cap per market.
# Kill switch without touching the scheduler: New-Item data\HALT
# Needs POLYMARKET_US_KEY_ID and POLYMARKET_US_KEY_FILE as user env vars.
& uv run --extra execute nflprops autotrade --live --max-per-market 100 *>&1 | Add-Content -Path $log -Encoding utf8
$code = $LASTEXITCODE

"=== $((Get-Date).ToUniversalTime().ToString('u')) run end (exit $code) ===" | Add-Content -Path $log -Encoding utf8
exit $code
