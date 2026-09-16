# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

A scanner for **+EV NO-side trades on Polymarket US NFL game props**, priced against FanDuel. **Read-only — no order placement.**

The premise is that Polymarket's game props carry **longshot bias**: the market overprices low-probability YES outcomes (a safety, a pick six, overtime). FanDuel prices the same events sharply, with vig. Buying NO on Polymarket below FanDuel's implied NO price means buying something worth at least what you paid, with FanDuel's entire hold as the safety margin.

Name it accurately: this is **not riskless arbitrage**. It is FanDuel-anchored +EV, and it inherits FanDuel's judgment. The tool reports; the operator trades.

Six target props: **D/ST touchdown, pick six, kickoff/punt return TD, safety, overtime, successful 2-pt conversion.**

## Commands

Python 3.12 managed by `uv`. The venv lives at `.venv` and is created on first `uv sync`.

```
uv sync --extra scrape                       # install deps (scrape extra adds curl_cffi)
uv run pytest                                # unit tests (offline, fixture-driven)
uv run ruff check .                          # lint
uv run mypy src                              # type-check

uv run nflprops markets                      # list priceable Polymarket props (debug)
uv run nflprops markets --books              # ...with live NO asks
uv run nflprops odds-template                # emit data/manual_odds.csv to fill in
uv run nflprops scan --odds-source manual    # scan using hand-entered odds
uv run nflprops scan --json                  # machine-readable output
uv run nflprops snapshot                     # record the slate to Parquet
```

`--slippage` adds a cushion *beyond* the modelled taker fee. `--days` sets the kickoff window.

## Venue: Polymarket US (public read API)

- **Public gateway:** `https://gateway.polymarket.us` — markets, books, BBO, events, series, sports. **No auth, no KYC.**
- **Authenticated:** `https://api.polymarket.us` — orders, portfolio, balances. Requires KYC via the iOS app and Ed25519-signed requests. **Nothing in this repo touches it.**
- Polymarket US is a **separate CFTC-regulated exchange** from international Polymarket, with its own order book and its own liquidity. Prices must come from the `.us` host; an international quote is not fillable from a US account. A prior project (`../pm-wt-selftrader`) stalled on exactly this distinction.
- Discovery: `GET /v1/events?tagSlug=nfl&closed=false&startDateMin=...&startDateMax=...` returns events with **nested markets**, so one request covers a whole slate.

## Non-obvious invariants

These were established by probing the live API on 2026-09-16 and are expensive to rediscover. Fixtures in `tests/fixtures/` are the recorded evidence.

- **The book is quoted in YES (long) terms, and NO ask = `1 - bestBid`.** A resting YES bid at 0.35 *is* a resting NO offer at 0.65, so buying NO crosses against YES **bids**. Verified: `shortQuote == 1 - bestBid` held across all 126 markets with a two-sided book, 0 violations. `edge.no_levels_from_yes_bids` is the single place this is encoded.
- **The book field is `offers`, not `asks`.** Reading `asks` silently yields an empty ladder.
- **Never index `outcomePrices` positionally.** The `outcomes` order varies per market — a live sample had 59 markets as `["No","Yes"]` and 69 as `["Yes","No"]`. Use `marketSides` and its `long` boolean instead; the side with `long: true` is always "Yes" (0 violations across 128 markets).
- **Fees are real and exceed a flat 0.5% buffer.** Polymarket US charges takers `shares × theta × p × (1-p)`; makers pay nothing. NFL props carry `feeCoefficient = 0.06`. Against a +650 floor of 0.86667 the true break-even NO price is **0.85942**, so the intuitive "86.2%" threshold is *negative* EV by ~0.25 points. `edge.max_buy_price` solves the quadratic rather than subtracting a guess.
- **Every target market is an `Over 0.5` line** ("at least one"). A 1.5 line asks for *two* occurrences and must never be matched against a FanDuel "will it happen" price — `props.REQUIRED_LINE` enforces this and skips the rest.
- **`tagSlug=nfl` leaks non-NFL teams** (Celtic FC and a "Tigers" entry both appeared), which is why `teams.TEAMS` is an explicit 32-team list rather than derived at runtime.
- **Polymarket lists NFL away-team-first** (`ordering: "away"` in `/v1/sports`), so ticker `nfl-det-buf-2026-09-17` is DET **at** BUF.

## Architecture

```
odds/fanduel ─→ legs ─┐
odds/manual  ─→       ├→ scan ─→ combine ─→ edge ─→ report (rich / JSON)
polymarket/client ────┘                              │
  events → props → book (YES bids → NO ladder)       ↓
                                        data/scans/YYYY-MM-DD/HHMM.parquet
```

- **`props.py`** — the taxonomy. Polymarket tags every market with an explicit `sportsMarketType`, so the Polymarket side needs **no text parsing**: `PM_MARKET_TYPES` is an exact enum→(prop, scope) map. Scope is encoded in the name (`football_game_*` vs `football_team_*`).
- **`edge.py`** — pure math, no I/O. Odds conversion, the fee model, break-even solving, and the YES-bid→NO-ladder mirror.
- **`combine.py`** — the leg-product rule (below).
- **`teams.py`** — 32-team alias resolution. Shared cities ("Los Angeles", "New York") resolve to `None` rather than guessing.
- **`odds/`** — provider protocol with two implementations. Everything downstream depends only on the protocol, so a broken scraper degrades the tool to manual entry instead of killing it.
- **`scan.py`** — orchestration; returns `Opportunity` records.
- **`report.py` / `snapshot.py`** — rendering and Parquet persistence.

### The leg-product rule

FanDuel prices D/ST touchdowns **per team**; Polymarket lists one **whole-game** market. Whole-game NO means "neither team does it":

```
floor(GAME) = product over teams of (1 - implied_yes(team))
```

Both teams at 80% NO → `0.80 × 0.80` = **0.64**.

Two properties, both favourable:
- **Vig compounds.** Each complement is already deflated by its own leg's vig, so the product is deflated further and the floor lands stricter.
- **Independence is an approximation.** The two teams' events are positively correlated (a sloppy, turnover-heavy game lifts both), so true "neither" is somewhat *higher* than the product — again conservative. It is not an exact identity.

**A partial leg set is never derived from.** One missing leg would *overstate* the floor and manufacture an edge that does not exist, so `leg_product_floor` raises `IncompleteLegsError` and the market is reported as unpriced. A direct whole-game quote, when FanDuel offers one, always beats the product.

### Liquidity is reported, never filtered

Every resting NO offer at or below the fee-adjusted threshold is shown, **down to a single share** (operator mandate). Thin size is information for the operator, not grounds for suppressing a signal. In practice many qualifying levels are dust — 0.01-share orders worth a fraction of a cent — and the `Shares` column is what distinguishes those from a real fill.

## FanDuel access: currently blocked

`odds/fanduel.py` targets the undocumented endpoints FanDuel's own web client uses (`sbapi.fanduel.com/api/event-page`, `/api/content-managed-page`). Scraping these is contrary to FanDuel's terms of service.

**`sbapi.fanduel.com` no longer exists.** It refuses the TLS handshake (`SSLV3_ALERT_HANDSHAKE_FAILURE`) from every client tried — Python `ssl`, curl/schannel, and curl_cffi with eight fingerprints (Chrome, Edge, Firefox, Safari, Android). The uniformity was the clue:

- DNS is healthy; system resolver and Cloudflare DoH agree on the A records.
- Against the *same* CloudFront IP, SNI `sbapi.fanduel.com` is rejected while SNI `sportsbook.fanduel.com` completes a TLS 1.3 handshake.

So the CDN simply does not serve that hostname any more. This is a **retired endpoint, not a block** — the widely-circulated `sbapi.fanduel.com` recipe is stale. Egress here is a residential Verizon FiOS IP in Pittsburgh (PA is a legal FanDuel state), so IP reputation is not the issue either.

The successor is partly identified: `api.sportsbook.fanduel.com` resolves and completes TLS, but every guessed path returns an nginx `default backend - 404`. Routes recovered from the site's JS bundles (`/api/sports/fixedodds/readonly/v1/getMarketPrices`, `/api/sports/navigation/facet/v1.0/search`, `/api/v1/event-selections`) 404 there too, so they are served from another host — likely a state/region-specific one.

**The way to settle it is to observe the real browser** rather than guess: load the NFL page in Playwright and record which host and path actually serves prop odds.

Consequences:
- `--odds-source manual` is the working path today.
- `parse_event_page` is written to the known payload shape but is **unverified against a live response**. Validate it against real data before trusting scraped output.
- If access is ever restored, re-check the parser first: `classify_market_name` is covered by tests, the payload walk is not.

## Conventions

- **Read-only.** No order-placement code in this repo, mirroring `../pm-wt-selftrader`. Execution, if it ships, goes in a separate private repo.
- **Tests are offline.** Network calls live in production code; tests run against recorded fixtures in `tests/fixtures/`. Don't add tests that hit live endpoints.
- **NO side only.** The strategy buys NO; YES-side edges are out of scope by design.
- **Floors come from the YES price**, never a book's own NO quote, so FanDuel's vig always sits on our side.
- **Explicit UTF-8 on every file read/write.** Polymarket payloads contain bytes that break Windows' cp1252 default.
- **Git workflow:** one coherent commit per logical unit, message focused on the *why*.

## Phase status

- **Phase 1 (done):** live endpoint probe; invariants established and recorded as fixtures.
- **Phase 2 (done):** `edge` + `combine` math with golden tests.
- **Phase 3 (done):** Polymarket US discovery and book client; 128 props across 16 games, 0 skipped.
- **Phase 4 (done):** manual odds provider, scan pipeline, rich/JSON reporting, Parquet snapshots.
- **Phase 5 (blocked):** FanDuel endpoint `sbapi.fanduel.com` is retired, not blocked; successor route unidentified. Scraper and parser unvalidated. Next move is browser observation, not more guessing.
- **Phase 6 (not started):** accumulate snapshots, then measure whether the longshot bias is real and persistent rather than assumed.
