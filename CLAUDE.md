# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

A scanner for **+EV NO-side trades on Polymarket US NFL game props**, priced against FanDuel, plus an hourly autotrader that acts on it.

**This package can place orders.** That was not true before 2026-09-22. `autotrade` is the only command that can move money; everything else — `scan`, `markets`, `snapshot`, the templates — is strictly read-only, and `polymarket/client.py` still touches nothing but the public gateway.

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
uv run nflprops scan                         # default: baseline odds
uv run nflprops scan --min-edge 5            # only edges >= 5 points
uv run nflprops scan --set dst_td=+850       # override one baseline
uv run nflprops baselines-template           # emit editable data/baselines.toml
uv run nflprops odds-template                # emit data/manual_odds.csv to fill in
uv run nflprops scan --odds-source manual    # scan using hand-entered odds
uv run nflprops scan --json                  # machine-readable output
uv run nflprops scan --no-include-started    # drop games already under way
uv run nflprops snapshot                     # record the slate to Parquet

uv run nflprops autotrade-template           # emit editable data/autotrade.toml
uv run nflprops autotrade                    # DRY RUN: what it would buy
uv run nflprops autotrade --live             # place the orders
uv run nflprops autotrade --live --max-per-market 5
```

`--slippage` adds a cushion *beyond* the modelled taker fee. `--days` sets the kickoff window.

## Autotrade

Hourly, deterministic, read-the-ledger-first. Same scan + ledger + clock always
produces the same orders; there is no sampling and no judgement in the loop. The
judgement is entirely in the operator's `max_buy` numbers.

**Thresholds** live in `data/autotrade.toml`, kept separate from `baselines.toml`
so the scanner's assumptions and the trader's live limits cannot drift into one
another. Set by the operator on 2026-09-22:

| Prop | Limit | Tradeable |
|---|---|---|
| `dst_td`, `two_pt` | 0.69 | yes |
| `pick_six`, `return_td`, `safety` | 0.83 | yes |
| `overtime` | — | **no** (no limit given) |

These are **already fee-adjusted**: the most the operator will pay all-in, fees
included. They are *not* floors, and must never be run through
`edge.max_buy_price` a second time — that subtracts the fee twice and turns 0.69
into 0.6747. The implied floor is `effective_cost(max_buy, theta)`, which inverts
back exactly; `combine.max_buy_floor` is the single place this is encoded and
`tests/unit/test_max_buy.py` asserts the round trip at both observed thetas.

Consequences worth knowing:

- **An offer at exactly the limit is bought, at exactly zero edge.** Deliberate,
  at the operator's instruction. `scan.py` filters edges at `>= -1e-9` rather than
  `> 0` for this reason.
- **D/ST TD is `scope = "game"` here**, unlike `baselines.toml`. 0.69 is a
  whole-game number, so the leg-product derivation is bypassed and the same flat
  cap applies to team-scoped D/ST markets. Since a single team's NO rarely trades
  under 0.69, this trades the whole-game market in practice.

**Guards**, in the order they bite:

1. `data/HALT` exists → the run does nothing and exits 0. Kill switch.
2. Prop not `tradeable`, or has no `max_buy` → never bought (this is `overtime`).
3. Kickoff past, imminent (`--kickoff-buffer`, default 15 min), or unparseable →
   skipped. Checked at planning **and** again immediately before each submission,
   because a slate takes minutes to price against a rate-limited gateway.
4. `--max-per-market` (default $100) enforced against `data/ledger.jsonl`, so it
   holds *across* runs. Without this an hourly job re-buys the same qualifying
   offer every hour.

The cap is measured in **all-in cost**, price plus taker fee, because that is what
leaves the account.

**Ledger.** `data/ledger.jsonl`, append-only, fsynced, one record per order.
Written *before* submission and reconciled after: an order whose response is lost
may well have been accepted, so its capital stays committed until reconciled.
Recording after the fact would understate exposure in exactly the case where
being wrong costs a double position. A raised exception is not a rejection and
does not release the capital.

**Orders are limit, immediate-or-cancel, priced at the level being taken.** Never
market orders: the whole strategy is a price threshold. IOC because a resting
order would still be live after kickoff.

### The order API

`execution/client.py` is written to docs.polymarket.us (read 2026-09-23). The
module docstring has the details; the ones that bite:

- **`price.value` is always the YES price, even on `ORDER_INTENT_BUY_SHORT`.** NO
  at 0.69 is sent as `0.31`. Sending the NO price does not error — it buys at the
  wrong price. `long_price_for_no` is the single place this is encoded, and it
  rounds to the tick in the direction that lowers the NO price.
- **Signature is Ed25519 over `timestamp_ms + METHOD + path`**, body unsigned,
  headers `X-PM-Access-Key` / `X-PM-Timestamp` / `X-PM-Signature`. The venue issues
  a 64-byte key (seed + public key); `credentials.load` keeps the 32-byte seed.
- **`synchronousExecution: true`**, or the response carries no fills at all.
- **Status mapping keeps ambiguity committed.** Nothing filled → `rejected`
  (capital released); 409 or 5xx or no executions → `submitted` (capital held
  until reconciled). A fill records its *actual* size in the ledger, costed at our
  limit price — whether `avgPx` on a short order is long-side is undocumented.
- **Live runs check buying power first** (`GET /v1/account/balances`) and
  `fit_to_budget` trims the plan to it.
- **Fees round to the cent per fill** (banker's rounding). At zero-edge limits a
  tiny fill can pay up to half a cent more than modelled.

Credentials come from the environment only, never from `data/` or anywhere in the
repo: `POLYMARKET_US_KEY_ID` plus `POLYMARKET_US_KEY_FILE` or `POLYMARKET_US_KEY`.
Needs the `execute` extra (`uv sync --extra execute`) for Ed25519.

### Scheduling

`scripts/autotrade_hourly.ps1` pins the working directory to the repo — config
paths are relative, so a wrong cwd silently loads built-in defaults instead of the
operator's limits — and appends to `logs/autotrade-YYYY-MM-DD.log`. It runs
`--dry-run`; change the flag when ready. Registration command is in its header
comment. Not registered as part of the build; that is the operator's call.

## Venue: Polymarket US (public read API)

- **Public gateway:** `https://gateway.polymarket.us` — markets, books, BBO, events, series, sports. **No auth, no KYC.**
- **Authenticated:** `https://api.polymarket.us` — orders, portfolio, balances. Requires KYC via the iOS app and Ed25519-signed requests. Only `execution/client.py` touches it.
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
- **Public endpoints are rate limited to roughly 60 requests/minute.** A slate is
  128 markets, so the scanner never fetches a book it does not need: the event
  payload already carries `bestBidQuote`, and because the NO ladder is cheapest at
  the top and only grows more expensive deeper down, a market whose best NO already
  exceeds `max_buy` cannot qualify at any level. `_get` also retries with backoff.
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

### Baseline odds (the default source)

With no live book feed available, `--odds-source baseline` prices every game off
a per-prop assumed line in `data/baselines.toml`. These six props sit in a narrow
band across games -- a D/ST touchdown prices much the same whoever is playing --
so a per-prop constant is a serviceable stand-in.

Two things keep this honest:

- **Baselines err short** (a smaller `+` number). Since `floor = 1 - implied_yes`,
  a shorter assumed price means a lower floor and a *stricter* buy threshold. Being
  wrong costs missed signals rather than bad fills.
- **Every report prints `Book need`** -- the American price that market must
  actually be at *or longer* to break even, from `edge.required_american_odds`.
  That turns verification into a single comparison against the app instead of
  re-deriving anything, and the header states plainly that floors are assumptions.

`min_edge_pts` is per prop in the TOML; `--min-edge` overrides all of them. This
filters on **edge**, never on size -- qualifying liquidity is still reported in
full, per the rule below.

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

- **Read-only except `execution/`.** Order placement lives in `execution/` and is reached only from `autotrade`. The earlier convention was that execution would go in a separate private repo, mirroring `../pm-wt-selftrader`; the operator chose to build it here on 2026-09-22. Keep the boundary visible: nothing outside `execution/` should import it, and `polymarket/client.py` stays a read client.
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
- **Phase 5.5 (done):** baseline-odds provider as the default source, per-prop
  minimum edge, and a `Book need` column so manual verification is one comparison.
- **Phase 6 (not started):** accumulate snapshots, then measure whether the longshot bias is real and persistent rather than assumed.
- **Phase 7 (in progress):** hourly autotrader. Fee-adjusted `max_buy` thresholds,
  live-game filter, per-market cap against an append-only ledger, dry-run mode,
  Task Scheduler script, and order submission to the documented API — all done
  and tested offline. Remaining: confirm the first live fills against the app,
  then switch the scheduled script from `--dry-run` to `--live`.
