# 1-cent extremes PAPER trader, for Windows Task Scheduler. Places no orders.
#
# Streams every NFL market through the games and exits on its own once they have
# all finished (or after --max-hours). Start it an hour or so before each window.
# Times below are local (ET):
#
#   $a = New-ScheduledTaskAction -Execute powershell `
#     -Argument '-NoProfile -ExecutionPolicy Bypass -File C:\Users\Sean\nfl-props-arb\scripts\extremes_paper.ps1'
#   $t = @(
#     New-ScheduledTaskTrigger -Weekly -DaysOfWeek Thursday -At 7:00pm
#     New-ScheduledTaskTrigger -Weekly -DaysOfWeek Sunday   -At 12:00pm
#     New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday   -At 7:00pm
#   )
#   $s = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
#     -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 20)
#   Register-ScheduledTask -TaskName nflprops-extremes-paper -Action $a -Trigger $t -Settings $s
#
# A Sunday start also subscribes Monday night's markets (36 h window), so it
# never "finishes" early; --max-hours 20 ends it Monday morning, and the Monday
# trigger covers the night game. Queue positions carry over through the log.
# The machine must stay awake: nothing is watched while it sleeps, and the
# restart is logged as a gap.
#
# Stop it:  New-Item data\HALT   (also stops the autotrader -- same kill switch)

$ErrorActionPreference = 'Stop'

# Relative paths (data/, logs/) resolve against the repo, not the caller's cwd.
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo

$logDir = Join-Path $repo 'logs'
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }
$log = Join-Path $logDir ("extremes-{0}.log" -f (Get-Date -Format 'yyyy-MM-dd'))

"=== $((Get-Date).ToUniversalTime().ToString('u')) run start ===" | Add-Content -Path $log -Encoding utf8

# The market stream is authenticated: needs POLYMARKET_US_KEY_ID and
# POLYMARKET_US_KEY_FILE as user env vars, like the autotrader.
# 'Continue' around the native call: see autotrade_hourly.ps1 for why.
$ErrorActionPreference = 'Continue'
& uv run --extra execute nflprops extremes-paper --hours 36 --max-hours 20 *>&1 |
    ForEach-Object { "$_" } | Add-Content -Path $log -Encoding utf8
$code = $LASTEXITCODE
$ErrorActionPreference = 'Stop'

"=== $((Get-Date).ToUniversalTime().ToString('u')) run end (exit $code) ===" | Add-Content -Path $log -Encoding utf8
exit $code
