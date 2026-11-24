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
#     -StartWhenAvailable -WakeToRun -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 20)
#   Register-ScheduledTask -TaskName nflprops-extremes-paper -Action $a -Trigger $t -Settings $s
#
# A Sunday start also subscribes Monday night's markets (36 h window), so it
# never "finishes" early; --max-hours 20 ends it Monday morning, and the Monday
# trigger covers the night game. Queue positions carry over through the log.
# -WakeToRun (set on the registered task 2026-11-23) wakes the PC for each trigger, and
# the trader holds it awake while it streams (awake.py). Neither works on
# battery: this laptop's plan blocks wake timers on DC, and closing the lid
# sleeps regardless. Nothing is watched while it sleeps; a restart is a gap.
# Registered triggers are 6 pm Thu, 11 am Sun, 6 pm Mon, not the times above.
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

# Settle step: fetch settlements for filled bids into data\extremes\settlements.jsonl,
# so extremes-report has nothing to catch up on. Public endpoint, no credentials.
# It writes its own file, so it is safe alongside the trader. Two runs:
#   before -- in the background, so a long backlog (~60 markets/min) never
#             delays kickoff; catches last slate's markets that expired late.
#   after  -- once the trader exits, for this slate's games. Also detached: the
#             task must end promptly, or MultipleInstances IgnoreNew skips the
#             next slate's trigger.
# Output goes to logs\extremes-settle-YYYY-MM-DD.log. A failure never blocks the trader.
$settleLog = Join-Path $logDir ("extremes-settle-{0}.log" -f (Get-Date -Format 'yyyy-MM-dd'))
$settleCmd = "uv run nflprops extremes-settle >> `"$settleLog`" 2>&1"
Start-Process -FilePath cmd.exe -ArgumentList '/c', $settleCmd -WorkingDirectory $repo -WindowStyle Hidden

# The market stream is authenticated: needs POLYMARKET_US_KEY_ID and
# POLYMARKET_US_KEY_FILE as user env vars, like the autotrader.
# 'Continue' around the native call: see autotrade_hourly.ps1 for why.
$ErrorActionPreference = 'Continue'
& uv run --extra execute nflprops extremes-paper --hours 36 --max-hours 20 *>&1 |
    ForEach-Object { "$_" } | Add-Content -Path $log -Encoding utf8
$code = $LASTEXITCODE
Start-Process -FilePath cmd.exe -ArgumentList '/c', $settleCmd -WorkingDirectory $repo -WindowStyle Hidden
$ErrorActionPreference = 'Stop'

"=== $((Get-Date).ToUniversalTime().ToString('u')) run end (exit $code) ===" | Add-Content -Path $log -Encoding utf8
exit $code
