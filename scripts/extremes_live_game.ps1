# LIVE 1-cent extremes test on ONE game. PLACES REAL ORDERS.
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\extremes_live_game.ps1 -Game chi-det
#
# The 105-bid split first run on CIN@WAS 2026-11-23 (prop 16 YES / 15 NO, spread
# 41 YES, total 31 YES, moneyline 1 + 1), 1 share a bid, so at most $1.05 can be
# lost per game. Own log per game (data\extremes\live-<game>.jsonl) and its own
# raw stream capture beside it; a second run on the same game exits (runlock.py).
# Placed at once, well ahead of kickoff, as the paper trader joins. It exits when
# the game's markets finish (or after -MaxHours). Afterwards:
#   uv run --extra execute nflprops extremes-live-reconcile --log data\extremes\live-<game>.jsonl
#   uv run --extra execute nflprops extremes-live-audit     --log data\extremes\live-<game>.jsonl
#   uv run nflprops extremes-settle; uv run nflprops extremes-live-report --log data\extremes\live-<game>.jsonl
# Stop: New-Item data\HALT (stops every trader), then extremes-live-cancel --live --log ...

param(
    [Parameter(Mandatory = $true)][string]$Game,
    [string]$MaxHours = '36'
)
$ErrorActionPreference = 'Stop'

$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo

$logDir = Join-Path $repo 'logs'
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }
$log = Join-Path $logDir ("extremes-live-{0}-{1}.log" -f $Game, (Get-Date -Format 'yyyy-MM-dd'))

"=== $((Get-Date).ToUniversalTime().ToString('u')) live $Game start ===" | Add-Content -Path $log -Encoding utf8

$ErrorActionPreference = 'Continue'
& uv run --extra execute nflprops extremes-live --live --game $Game `
    --max-exposure 1.05 --max-hours $MaxHours --log "data/extremes/live-$Game.jsonl" `
    --quota PROP:YES=16 --quota PROP:NO=15 --quota SPREAD:YES=41 --quota TOTAL:YES=31 `
    --quota MONEYLINE:YES=1 --quota MONEYLINE:NO=1 *>&1 |
    ForEach-Object { "$_" } | Add-Content -Path $log -Encoding utf8
$code = $LASTEXITCODE
$ErrorActionPreference = 'Stop'

"=== $((Get-Date).ToUniversalTime().ToString('u')) live $Game end (exit $code) ===" | Add-Content -Path $log -Encoding utf8
exit $code
