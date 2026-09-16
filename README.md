# nfl-props-arb

Scanner for **+EV NO-side trades on Polymarket US NFL game props**, priced against FanDuel odds. **Read-only — this repo contains no order-placement code.**

Polymarket's game props carry longshot bias: low-probability YES outcomes (a safety, a pick six, overtime) trade rich. FanDuel prices the same events sharply, with vig. Buying NO on Polymarket below FanDuel's implied NO price means FanDuel's entire hold becomes the margin of safety.

This is **not riskless arbitrage** — it is FanDuel-anchored +EV, and it inherits FanDuel's judgment.

Six props: D/ST touchdown, pick six, kickoff/punt return TD, safety, overtime, successful 2-pt conversion.

## Quick start

```bash
uv sync --extra scrape
uv run pytest                                 # offline tests
uv run nflprops markets                       # what's priceable right now
uv run nflprops odds-template                 # emit data/manual_odds.csv
uv run nflprops scan --odds-source manual     # scan
```

## How the edge is computed

FanDuel at +650 implies 13.33% YES. Because vig inflates that, true NO is at least `1 - 0.1333 = 0.8667` — a conservative floor.

Polymarket US charges takers `shares × theta × p × (1-p)` (theta = 0.06 on these markets), so the real break-even NO price against that floor is **0.8594**, not the 0.862 a flat 0.5% buffer suggests. The scanner solves for it rather than guessing.

Where FanDuel prices a prop per team but Polymarket lists one whole-game market, the floor is the product of the per-team NO complements (`0.80 × 0.80 = 0.64`). A partial leg set is never derived from — it would overstate the floor and invent an edge.

All qualifying liquidity is reported down to a single share; size is information, not a filter.

See [CLAUDE.md](CLAUDE.md) for API invariants and architecture.

## Status

Polymarket US market data is public (no auth, no KYC) and works. FanDuel's endpoints refuse TLS from the development machine, so `--odds-source manual` is the working path today.
