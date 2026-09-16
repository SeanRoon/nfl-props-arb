---
description: Scan Polymarket US NFL game props for +EV NO trades anchored to FanDuel odds
allowed-tools: Bash, Read, Grep
---

# Scan NFL game props

Find NO-side offers on Polymarket US priced below FanDuel's implied NO floor, after fees.

Repository: `C:\Users\Sean\nfl-props-arb`. Read `CLAUDE.md` there first if you have not this session — the invariants section is load-bearing.

## Run the scan

```
cd /c/Users/Sean/nfl-props-arb && uv run nflprops scan --json
```

If FanDuel is unreachable (it is blocked from this network as of 2026-09-16), fall back to:

```
uv run nflprops odds-template          # then the operator fills in american_odds
uv run nflprops scan --odds-source manual --json
```

If `data/manual_odds.csv` has unfilled rows, say which props are missing odds rather than reporting a partial scan as if it were complete.

## Then verify before reporting

The scanner joins on exact enum keys, but you are the last check before real money. For each opportunity:

1. **Confirm the two markets describe the same event.** Read the Polymarket `question` and the FanDuel `label` side by side. A whole-game "either team" market and a single-team market are different bets. So are "Over 0.5" and "Over 1.5".
2. **For derived floors** (`floor.method == "leg_product"`), restate the multiplication and confirm **both** teams' legs are present. The scanner refuses partial derivations, but say the arithmetic out loud so the operator can check it: `0.889 × 0.889 = 0.7901`.
3. **Report liquidity honestly.** Every qualifying level is listed down to a single share, by design. Many are dust — a 0.01-share order is worth a fraction of a cent. State the fillable size next to the edge; a large edge on 0.01 shares is not an opportunity, and saying so is the point.
4. **Flag anything unfamiliar.** If a Polymarket `sportsMarketType` or a FanDuel market label has not been seen before, mark it **needs human confirmation** rather than presenting it as actionable.

## Report

Rank by edge in points. For each opportunity give: game, prop, scope, FanDuel odds (all legs), the floor and how it was derived, max buy price, best NO ask, edge in points, ROI, and total fillable shares.

Close with anything that could not be priced and why (`no_fanduel_quote`, `incomplete_legs`).

Keep these honest and do not soften them:

- This is **not arbitrage**. It is FanDuel-anchored +EV and inherits FanDuel's judgment.
- Polymarket US books for these props are **thin**. Report size, never bury it.
- The edge is only as good as the odds. If odds were hand-entered, say when they were entered — lines move.

Never place an order. This repo is read-only by design.
