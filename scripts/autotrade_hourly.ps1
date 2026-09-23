# Hourly autotrade run, for Windows Task Scheduler.
#
# Register it (from an elevated prompt, once you have reviewed dry-run output):
#
#   schtasks /Create /TN "nflprops-autotrade" /SC HOURLY `
#     /TR "powershell -NoProfile -ExecutionPolicy Bypass -File C:\Users\Sean\nfl-props-arb\scripts\autotrade_hourly.ps1" `
#     /RL LIMITED
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

"=== $(Get-Date -Format 'u') run start ===" | Add-Content -Path $log -Encoding utf8

# --dry-run by default. Change to --live only when you mean it.
& uv run nflprops autotrade --dry-run *>&1 | Add-Content -Path $log -Encoding utf8
$code = $LASTEXITCODE

"=== $(Get-Date -Format 'u') run end (exit $code) ===" | Add-Content -Path $log -Encoding utf8
exit $code
