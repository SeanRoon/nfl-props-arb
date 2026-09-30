"""Paper-trading 1-cent bids on both sides of every NFL market, held through the game.

The idea (operator, 2026-09-30): rest a YES bid at 0.01 and a NO bid at 0.01 in
every market, and leave them up during play. Now and then a market wicks to an
extreme it should not have reached. A 0.01 fill costs a cent and pays a dollar,
and makers pay no fee, so the strategy breaks even at a 1% hit rate among fills.

This module decides, from the venue's own trade prints and book snapshots,
which virtual bids *would* have filled. It places nothing. Pure: no network, no
disk, no clock of its own.

Conventions, all in long (YES) terms because that is how the venue quotes:

* The **YES bid** rests at long 0.01. It is hit only by a trade whose taker
  *sells* long at or below 0.01.
* The **NO bid** at 0.01 is a long *ask* at 0.99. It is hit only by a trade whose
  taker *buys* long at or above 0.99.
* A print at 0.01 where the taker was buying lifted an offer. It cannot have
  touched a bid, so it counts under neither model.
* A print *through* our price (0.005 on the half-cent moneyline ticks) means the
  whole 0.01 level was swept first, so the bid counts as filled in full.

Two fill models, reported side by side:

* **optimistic**: every share traded at our price fills us, as though we were at
  the front of the queue.
* **queue-aware**: we join behind the depth already resting at our price. Only
  volume beyond that fills us. Depth that shrinks without trading means orders
  ahead of us left, which moves us up; depth added later sits behind us. A book
  snapshot can land before the trade print that emptied it, so the depth
  reduction and the trade can both count once. That leans slightly optimistic.

Real 1-cent queues are deep: 43,798 shares in one prop's recorded book, 27,960
on the PIT-CLE moneyline (2026-09-30). The queue-aware number decides whether
this is worth trading. The optimistic one is an upper bound.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

from .props import GameKey

YES_PRICE = 0.01        # long price of the YES bid
NO_LONG_PRICE = 0.99    # long price of the NO bid (a NO bid at 0.01)
COST_PER_SHARE = 0.01   # either side, maker, no fee
DEFAULT_SHARES = 100.0
EPS = 1e-9

SIDES = ("YES", "NO")
TERMINAL_STATES = ("EXPIRED", "SETTLED", "CLOSED", "RESOLVED")


# --- wire parsing ------------------------------------------------------------


def _px(node: Any) -> float | None:
    if not node:
        return None
    try:
        return float(node["value"])
    except (KeyError, TypeError, ValueError):
        return None


@dataclass(frozen=True)
class Trade:
    slug: str
    trade_id: str
    price: float          # long price
    qty: float
    taker_side: str       # "BUY" or "SELL", long side
    trade_time: str


@dataclass(frozen=True)
class Book:
    slug: str
    bids: dict[float, float]     # long px -> qty
    offers: dict[float, float]
    state: str
    low_px: float | None = None
    low_set: str | None = None
    high_px: float | None = None
    high_set: str | None = None

    def depth(self, side: str) -> float:
        """Resting quantity at our price on our side of the book."""
        if side == "YES":
            return sum(q for p, q in self.bids.items() if abs(p - YES_PRICE) < EPS)
        return sum(q for p, q in self.offers.items() if abs(p - NO_LONG_PRICE) < EPS)

    def would_cross(self, side: str) -> bool:
        """Is the opposite side already at our price, so a post-only bid is refused?"""
        if side == "YES":
            return bool(self.offers) and min(self.offers) <= YES_PRICE + EPS
        return bool(self.bids) and max(self.bids) >= NO_LONG_PRICE - EPS


def parse_trade(msg: dict[str, Any]) -> Trade | None:
    """A `SUBSCRIPTION_TYPE_TRADE` message, or None if it is not one."""
    t = msg.get("trade")
    if not isinstance(t, dict):
        return None
    price = _px(t.get("price"))
    qty = _px(t.get("quantity"))  # labelled USD on the wire, but it is shares
    side = str((t.get("taker") or {}).get("side") or "")
    if price is None or qty is None or not t.get("marketSlug"):
        return None
    return Trade(
        slug=t["marketSlug"],
        trade_id=str(t.get("id") or ""),
        price=price,
        qty=qty,
        taker_side="SELL" if side.endswith("SELL") else "BUY" if side.endswith("BUY") else "",
        trade_time=str(t.get("tradeTime") or ""),
    )


def parse_book(msg: dict[str, Any]) -> Book | None:
    """A `SUBSCRIPTION_TYPE_MARKET_DATA` message. Each one is a full snapshot."""
    d = msg.get("marketData")
    if not isinstance(d, dict) or not d.get("marketSlug"):
        return None

    def ladder(rows: Any) -> dict[float, float]:
        out: dict[float, float] = {}
        for r in rows or []:
            px = _px(r.get("px"))
            if px is None:
                continue
            try:
                out[px] = out.get(px, 0.0) + float(r.get("qty") or 0.0)
            except (TypeError, ValueError):
                continue
        return out

    stats = d.get("stats") or {}
    return Book(
        slug=d["marketSlug"],
        bids=ladder(d.get("bids")),
        offers=ladder(d.get("offers")),
        state=str(d.get("state") or ""),
        low_px=_px(stats.get("lowPx")),
        low_set=stats.get("lowSetTime"),
        high_px=_px(stats.get("highPx")),
        high_set=stats.get("highSetTime"),
    )


def is_terminal(state: str) -> bool:
    return any(word in state for word in TERMINAL_STATES)


# --- markets -----------------------------------------------------------------


@dataclass(frozen=True)
class MarketMeta:
    slug: str
    event_ticker: str
    game: str
    market_type: str          # MONEYLINE / SPREAD / TOTAL / PROP
    sports_market_type: str
    kickoff: str
    title: str


def markets_from_events(events: list[dict[str, Any]]) -> list[MarketMeta]:
    """Every open market of every NFL *game* event. Futures are left out."""
    out: list[MarketMeta] = []
    for ev in events:
        ticker = ev.get("ticker") or ""
        game = GameKey.from_ticker(ticker)
        if game is None:
            continue
        for m in ev.get("markets") or []:
            if m.get("closed") or not m.get("slug"):
                continue
            v2 = str(m.get("sportsMarketTypeV2") or "")
            out.append(
                MarketMeta(
                    slug=m["slug"],
                    event_ticker=ticker,
                    game=str(game),
                    market_type=v2.removeprefix("SPORTS_MARKET_TYPE_") or "UNKNOWN",
                    sports_market_type=str(m.get("sportsMarketType") or ""),
                    kickoff=str(ev.get("startDate") or ""),
                    title=str(m.get("question") or m.get("title") or ""),
                )
            )
    return out


# --- virtual bids ------------------------------------------------------------


@dataclass
class VirtualBid:
    slug: str
    side: str
    event_ticker: str
    game: str
    market_type: str
    sports_market_type: str
    kickoff: str
    title: str
    shares: float
    joined_at: str
    queue_at_join: float
    would_cross: bool
    ahead: float = 0.0
    volume_at_price: float = 0.0
    optimistic_filled: float = 0.0
    queue_filled: float = 0.0
    first_touch: str | None = None
    stats_touch: bool = False

    @property
    def key(self) -> tuple[str, str]:
        return (self.slug, self.side)

    def hit_by(self, trade: Trade) -> bool:
        """Did this trade execute against our side of the book, at or through our price?"""
        if self.side == "YES":
            return trade.taker_side == "SELL" and trade.price <= YES_PRICE + EPS
        return trade.taker_side == "BUY" and trade.price >= NO_LONG_PRICE - EPS

    def through(self, trade: Trade) -> bool:
        if self.side == "YES":
            return trade.price < YES_PRICE - EPS
        return trade.price > NO_LONG_PRICE + EPS


def join(meta: MarketMeta, book: Book, now: datetime, shares: float) -> list[VirtualBid]:
    """Both virtual bids for one market, placed behind the depth resting now."""
    bids = []
    for side in SIDES:
        depth = book.depth(side)
        bids.append(
            VirtualBid(
                slug=meta.slug,
                side=side,
                event_ticker=meta.event_ticker,
                game=meta.game,
                market_type=meta.market_type,
                sports_market_type=meta.sports_market_type,
                kickoff=meta.kickoff,
                title=meta.title,
                shares=shares,
                joined_at=now.isoformat(),
                queue_at_join=depth,
                would_cross=book.would_cross(side),
                ahead=depth,
            )
        )
    return bids


def on_trade(bid: VirtualBid, trade: Trade) -> bool:
    """Apply one trade. True if it hit our side (and so changed something)."""
    if not bid.hit_by(trade):
        return False
    if bid.first_touch is None:
        bid.first_touch = trade.trade_time
    bid.volume_at_price += trade.qty
    if bid.through(trade):
        bid.optimistic_filled = bid.shares
        bid.queue_filled = bid.shares
        bid.ahead = 0.0
        return True
    bid.optimistic_filled = min(bid.shares, bid.optimistic_filled + trade.qty)
    consumed = min(bid.ahead, trade.qty)
    bid.ahead -= consumed
    bid.queue_filled = min(bid.shares, bid.queue_filled + trade.qty - consumed)
    return True


def on_depth(bid: VirtualBid, book: Book) -> None:
    """Depth at our price shrank: whoever left was ahead of us, or we cannot tell."""
    if bid.queue_filled < bid.shares:
        bid.ahead = min(bid.ahead, book.depth(bid.side))


def stats_touched(bid: VirtualBid, book: Book) -> bool:
    """Did the session low/high reach our price after we joined?

    A backstop for trades the stream missed (a reconnect gap). It never fills
    anything; it only flags that the trade stream and the venue's stats disagree.
    """
    if bid.side == "YES":
        px, when = book.low_px, book.low_set
        hit = px is not None and px <= YES_PRICE + EPS
    else:
        px, when = book.high_px, book.high_set
        hit = px is not None and px >= NO_LONG_PRICE - EPS
    # Both are UTC ISO-8601 with different fractional/offset suffixes; the
    # first 19 characters (to the second) compare correctly as strings.
    return bool(hit and when and when[:19] > bid.joined_at[:19])


def pnl(filled: float, side: str, settlement: float) -> float:
    """Dollar P&L of `filled` shares bought at a cent, given the long settlement price."""
    payout = settlement if side == "YES" else 1.0 - settlement
    return filled * (payout - COST_PER_SHARE)


# --- the tracker: messages in, log records out -------------------------------


@dataclass
class Tracker:
    """Every virtual bid, driven by stream messages. Emits records to append to the log."""

    shares: float = DEFAULT_SHARES
    metas: dict[str, MarketMeta] = field(default_factory=dict)
    bids: dict[tuple[str, str], VirtualBid] = field(default_factory=dict)
    states: dict[str, str] = field(default_factory=dict)
    seen_trades: set[str] = field(default_factory=set)

    def add_markets(self, metas: list[MarketMeta]) -> list[str]:
        """Register markets; returns the slugs that are new."""
        new = [m.slug for m in metas if m.slug not in self.metas]
        for m in metas:
            self.metas.setdefault(m.slug, m)
        return new

    def handle(self, msg: dict[str, Any], now: datetime) -> list[dict[str, Any]]:
        ts = now.isoformat()
        trade = parse_trade(msg)
        if trade is not None:
            return self._trade(trade, ts)
        book = parse_book(msg)
        if book is not None:
            return self._book(book, now)
        return []

    def _trade(self, trade: Trade, ts: str) -> list[dict[str, Any]]:
        if trade.trade_id and trade.trade_id in self.seen_trades:
            return []
        if trade.trade_id:
            self.seen_trades.add(trade.trade_id)
        out = []
        for side in SIDES:
            bid = self.bids.get((trade.slug, side))
            if bid is None or not on_trade(bid, trade):
                continue
            out.append(
                {
                    "event": "trade", "ts": ts, "slug": bid.slug, "side": side,
                    "trade_id": trade.trade_id, "trade_time": trade.trade_time,
                    "px": trade.price, "qty": trade.qty, "taker": trade.taker_side,
                    "through": bid.through(trade), "ahead": bid.ahead,
                    "volume_at_price": bid.volume_at_price,
                    "optimistic_filled": bid.optimistic_filled,
                    "queue_filled": bid.queue_filled,
                }
            )
        return out

    def _book(self, book: Book, now: datetime) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        ts = now.isoformat()
        if book.state and self.states.get(book.slug) != book.state:
            self.states[book.slug] = book.state
            out.append({"event": "state", "ts": ts, "slug": book.slug, "state": book.state})
        meta = self.metas.get(book.slug)
        if meta is None:
            return out
        if (book.slug, "YES") not in self.bids:
            if book.state and book.state != "MARKET_STATE_OPEN":
                return out  # nothing to join; a halted or expired market takes no bids
            for bid in join(meta, book, now, self.shares):
                self.bids[bid.key] = bid
                out.append({"event": "join", "ts": ts, **asdict(bid)})
            return out
        for side in SIDES:
            bid = self.bids[(book.slug, side)]
            on_depth(bid, book)
            if not bid.stats_touch and bid.first_touch is None and stats_touched(bid, book):
                bid.stats_touch = True
                out.append(
                    {
                        "event": "stats_touch", "ts": ts, "slug": bid.slug, "side": side,
                        "low_px": book.low_px, "low_set": book.low_set,
                        "high_px": book.high_px, "high_set": book.high_set,
                    }
                )
        return out

    def replay(self, records: list[dict[str, Any]]) -> None:
        """Rebuild state from the log, so a restart keeps queue positions."""
        fields = set(VirtualBid.__dataclass_fields__)
        for rec in records:
            ev = rec.get("event")
            if ev == "join":
                bid = VirtualBid(**{k: v for k, v in rec.items() if k in fields})
                self.bids.setdefault(bid.key, bid)
            elif ev == "trade":
                hit = self.bids.get((rec["slug"], rec["side"]))
                if rec.get("trade_id"):
                    self.seen_trades.add(rec["trade_id"])
                if hit is None:
                    continue
                bid = hit
                bid.ahead = rec["ahead"]
                bid.volume_at_price = rec["volume_at_price"]
                bid.optimistic_filled = rec["optimistic_filled"]
                bid.queue_filled = rec["queue_filled"]
                bid.first_touch = bid.first_touch or rec.get("trade_time")
            elif ev == "stats_touch":
                touched = self.bids.get((rec["slug"], rec["side"]))
                if touched is not None:
                    touched.stats_touch = True
            elif ev == "state":
                self.states[rec["slug"]] = rec["state"]

    def open_slugs(self) -> list[str]:
        """Tracked markets not yet known to be finished."""
        return [s for s in self.metas if not is_terminal(self.states.get(s, ""))]

    def summary(self) -> dict[str, float]:
        bids = list(self.bids.values())
        return {
            "markets": len(self.metas),
            "joined": len(bids),
            "touched": sum(1 for b in bids if b.first_touch),
            "optimistic_fills": sum(1 for b in bids if b.optimistic_filled > 0),
            "queue_fills": sum(1 for b in bids if b.queue_filled > 0),
            "finished": len(self.metas) - len(self.open_slugs()),
        }


# --- report ------------------------------------------------------------------


def bids_from_log(records: list[dict[str, Any]]) -> dict[tuple[str, str], VirtualBid]:
    t = Tracker()
    t.replay(records)
    return t.bids


def settlements_from_log(records: list[dict[str, Any]]) -> dict[str, float]:
    return {
        r["slug"]: float(r["settlement_px"])
        for r in records
        if r.get("event") == "settle" and r.get("settlement_px") is not None
    }


def report_rows(
    bids: dict[tuple[str, str], VirtualBid], settlements: dict[str, float]
) -> list[dict[str, Any]]:
    """One row per market type x side x would_cross, both fill models."""
    groups: dict[tuple[str, str, bool], list[VirtualBid]] = defaultdict(list)
    for b in bids.values():
        groups[(b.market_type, b.side, b.would_cross)].append(b)
    rows = []
    for (mtype, side, crossed), members in sorted(groups.items()):
        row: dict[str, Any] = {
            "market_type": mtype, "side": side, "would_cross": crossed,
            "bids": len(members),
            "touched": sum(1 for b in members if b.first_touch),
            "stats_only": sum(1 for b in members if b.stats_touch and not b.first_touch),
        }
        for model in ("optimistic", "queue"):
            filled = [b for b in members if getattr(b, f"{model}_filled") > 0]
            settled = [b for b in filled if b.slug in settlements]
            shares = sum(getattr(b, f"{model}_filled") for b in settled)
            won = [
                b for b in settled
                if (settlements[b.slug] if b.side == "YES" else 1 - settlements[b.slug]) > 0.5
            ]
            row[f"{model}_fills"] = len(filled)
            row[f"{model}_unsettled"] = len(filled) - len(settled)
            row[f"{model}_cost"] = shares * COST_PER_SHARE
            row[f"{model}_pnl"] = sum(
                pnl(getattr(b, f"{model}_filled"), b.side, settlements[b.slug]) for b in settled
            )
            row[f"{model}_wins"] = len(won)
            row[f"{model}_hit_rate"] = len(won) / len(settled) if settled else None
        rows.append(row)
    return rows


def filled_rows(
    bids: dict[tuple[str, str], VirtualBid], settlements: dict[str, float]
) -> list[dict[str, Any]]:
    """Every bid with any optimistic fill, sorted by dollar P&L (queue-aware first)."""
    rows = []
    for b in bids.values():
        if b.optimistic_filled <= 0:
            continue
        s = settlements.get(b.slug)
        rows.append(
            {
                "slug": b.slug, "game": b.game, "market_type": b.market_type, "side": b.side,
                "title": b.title, "would_cross": b.would_cross, "first_touch": b.first_touch,
                "queue_at_join": b.queue_at_join, "volume_at_price": b.volume_at_price,
                "optimistic_filled": b.optimistic_filled, "queue_filled": b.queue_filled,
                "settlement": s,
                "optimistic_pnl": pnl(b.optimistic_filled, b.side, s) if s is not None else None,
                "queue_pnl": pnl(b.queue_filled, b.side, s) if s is not None else None,
            }
        )
    def by_pnl(r: dict[str, Any]) -> tuple[float, float]:
        return (-float(r["queue_pnl"] or 0.0), -float(r["optimistic_pnl"] or 0.0))

    rows.sort(key=by_pnl)
    return rows
