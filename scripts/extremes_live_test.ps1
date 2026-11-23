# LIVE 1-cent extremes test on CIN@WAS. PLACES REAL ORDERS.
#
# Repeats the 105-bid test (first placed 2026-11-23 ~02:45 UTC, cancelled
# unfilled) on exactly the same market sides. Placed well ahead of kickoff, as
# the paper trader joins. 1 share a bid, so at most $1.05 can be lost. Started
# by hand, detached, 2026-11-23 (the one-time task once planned was dropped):
#
#   Start-Process powershell -WindowStyle Hidden -ArgumentList '-NoProfile',
#     '-ExecutionPolicy','Bypass','-File','C:\Users\Sean\nfl-props-arb\scripts\extremes_live_test.ps1'
#
# It exits on its own once the game's markets finish (or after 30 h). Bids
# expire at kickoff + 24 h regardless. Afterwards:
#   uv run nflprops extremes-settle; uv run nflprops extremes-live-report
# Stop it:  New-Item data\HALT  (also stops the paper trader), then
#           uv run --extra execute nflprops extremes-live-cancel --live

$ErrorActionPreference = 'Stop'

$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo

$logDir = Join-Path $repo 'logs'
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }
$log = Join-Path $logDir ("extremes-live-{0}.log" -f (Get-Date -Format 'yyyy-MM-dd'))

"=== $((Get-Date).ToUniversalTime().ToString('u')) live test start ===" | Add-Content -Path $log -Encoding utf8

# Needs POLYMARKET_US_KEY_ID and POLYMARKET_US_KEY_FILE as user env vars.
# 'Continue' around the native call: see autotrade_hourly.ps1 for why.
$ErrorActionPreference = 'Continue'
& uv run --extra execute nflprops extremes-live --live --game cin-was `
    --max-exposure 1.05 --max-hours 30 `
    --keys-from data/extremes/live-test-2026-11-23.jsonl *>&1 |
    ForEach-Object { "$_" } | Add-Content -Path $log -Encoding utf8
$code = $LASTEXITCODE
$ErrorActionPreference = 'Stop'

"=== $((Get-Date).ToUniversalTime().ToString('u')) live test end (exit $code) ===" | Add-Content -Path $log -Encoding utf8
exit $code
