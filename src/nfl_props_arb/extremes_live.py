"""LIVE 1-cent extremes: real resting bids, shadowed by the queue-aware paper model.

The paper trader (`extremes.py`) estimates which 1-cent bids *would* fill. This
module places them for real and keeps that same estimate alongside every real
order, so the two P&Ls are measured on identical bids and any gap between them
is the model's error, not a difference in what was traded.

The venue decides real fills; nothing here can make them match the model. What
it does is give the real order and its shadow the same life:

* **Same decision.** A market side is decided once, at the first OPEN book
  snapshot, exactly when the paper trader joins: rest a bid unless the slice is
  excluded, the book already crosses our price (post-only would be refused, the
  paper's `would_cross`), or the exposure cap is full.
* **Same order.** Post-only, YES at long 0.01 or NO at long 0.99, `shares` per
  side, held through the game. Never cancelled or re-priced by this module.
* **Same queue start.** The shadow joins when the venue confirms the order is
  resting (its first update on the private order stream), against the latest
  book at that moment -- not at decision time, since a batch takes a moment to
  rest and the queue moves meanwhile.
* **Our own share is not ahead of us.** Once our order rests, the book's depth
  at our price includes it. The shadow subtracts our unfilled remainder from
  that depth, at join and on every depth update. (If a snapshot predates our
  order, this under-counts the queue by our size -- one share against queues of
  thousands.)
* **Same end.** The shadow stops counting when the venue cancels or expires the
  real order (a GTD expiry, or the venue cancelling an unfunded bid). A real
  fill does *not* stop it: the shadow is an independent estimate, and stopping
  it on a real fill would bias it toward the real result.

Real fills come from the private stream's `cumQuantity`. Our own fill also
prints on the public trade stream, where the shadow sees it like any other
trade -- which is the point.

Pure: no network, no disk, no clock of its own. `extremes_live_run.py` owns those.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta
from typing import Any

from .autotrade import parse_kickoff
from .extremes import (
    COST_PER_SHARE,
    EPS,
    NO_LONG_PRICE,
    SIDES,
    YES_PRICE,
    Book,
    MarketMeta,
    Tracker,
    Trade,
    VirtualBid,
    is_terminal,
    on_depth,
    on_trade,
    parse_book,
    parse_trade,
    pnl,
)

DEFAULT_LIVE_SHARES = 1.0
DEFAULT_MAX_EXPOSURE = 200.0
# Slices the paper results showed to lose reliably (operator, 2026-11-23).
DEFAULT_SKIP = frozenset({("SPREAD", "NO"), ("TOTAL", "NO")})
# GTD expiry, measured from kickoff. Long enough that the market is settled
# first, so the shadow and the order both live until the end, as the paper bid
# does; short enough that the venue cleans up if this machine dies.
EXPIRY_AFTER_KICKOFF = timedelta(hours=24)

LONG_PRICE = {"YES": YES_PRICE, "NO": NO_LONG_PRICE}
DEAD_WORDS = ("CANCEL", "EXPIRE", "REJECT")


def parse_skip(values: list[str]) -> frozenset[tuple[str, str]]:
    """`SPREAD:NO` style slices -> {("SPREAD", "NO")}. Raises on a malformed one."""
    out = set()
    for v in values:
        mtype, sep, side = v.strip().upper().partition(":")
        if not sep or side not in SIDES or not mtype:
            raise ValueError(f"bad slice {v!r}; expected TYPE:SIDE, e.g. SPREAD:NO")
        out.add((mtype, side))
    return frozenset(out)


def intent_keys(records: list[dict[str, Any]]) -> set[tuple[str, str]]:
    """Every (slug, side) an earlier live log sent a bid for."""
    return {(str(r["slug"]), str(r["side"])) for r in records if r.get("event") == "intent"}


def parse_quotas(values: list[str]) -> dict[tuple[str, str], int]:
    """`PROP:YES=16` style quotas -> {("PROP", "YES"): 16}. Raises on a malformed one."""
    out: dict[tuple[str, str], int] = {}
    for v in values:
        slice_, sep, n = v.partition("=")
        if not sep:
            raise ValueError(f"bad quota {v!r}; expected TYPE:SIDE=N, e.g. PROP:YES=16")
        (key,) = parse_skip([slice_])
        try:
            out[key] = int(n)
        except ValueError as exc:
            raise ValueError(f"bad quota count in {v!r}") from exc
    return out


def is_dead(state: str) -> bool:
    """Removed from the book by the venue without (further) filling."""
    return any(w in state for w in DEAD_WORDS)


def own_adjusted(book: Book, side: str, own: float) -> Book:
    """The book with our own unfilled `own` shares taken out of our price level."""
    if own <= EPS:
        return book
    px = LONG_PRICE[side]
    ladder = dict(book.bids if side == "YES" else book.offers)
    for p in list(ladder):
        if abs(p - px) < EPS:
            ladder[p] = max(0.0, ladder[p] - own)
            break
    return replace(book, bids=ladder) if side == "YES" else replace(book, offers=ladder)


def parse_order_updates(msg: dict[str, Any]) -> list[dict[str, Any]]:
    """Order dicts from a private-stream message: one update, or a snapshot's list.

    Each comes back with `_exec_id` when the message was an execution, so a
    repeated delivery can be told apart from a new one.
    """
    out: list[dict[str, Any]] = []
    upd = msg.get("orderSubscriptionUpdate")
    if isinstance(upd, dict):
        ex = upd.get("execution")
        if isinstance(ex, dict) and isinstance(ex.get("order"), dict):
            out.append({**ex["order"], "_exec_id": str(ex.get("id") or "")})
    snap = msg.get("orderSubscriptionSnapshot")
    if isinstance(snap, dict):
        out.extend(o for o in snap.get("orders") or [] if isinstance(o, dict))
    return out


def _qty(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


@dataclass
class LiveOrder:
    slug: str
    side: str
    shares: float
    order_id: str = ""
    status: str = "intended"   # intended | placed | rejected | unknown
    state: str = ""            # venue ORDER_STATE_*
    cum_qty: float = 0.0
    message: str = ""

    @property
    def key(self) -> tuple[str, str]:
        return (self.slug, self.side)

    @property
    def live(self) -> bool:
        # Not a suffix test: PARTIALLY_FILLED ends in FILLED too, and is still resting.
        return (self.status == "placed" and not is_dead(self.state)
                and self.state != "ORDER_STATE_FILLED")

    @property
    def unfilled(self) -> float:
        return max(0.0, self.shares - self.cum_qty) if self.live else 0.0


@dataclass(frozen=True)
class Intent:
    """A bid to place: what the placement worker sends."""

    slug: str
    side: str
    long_price: float
    shares: float
    expires_at: datetime


class LiveTracker:
    """Market and private-stream messages in; log records and orders to place out."""

    def __init__(
        self,
        *,
        shares: float = DEFAULT_LIVE_SHARES,
        skip: frozenset[tuple[str, str]] = DEFAULT_SKIP,
        max_exposure: float = DEFAULT_MAX_EXPOSURE,
        quotas: dict[tuple[str, str], int] | None = None,
        allowed_keys: set[tuple[str, str]] | None = None,
    ) -> None:
        """`quotas`, if given, is the most bids per (TYPE, SIDE) slice across the
        whole log, replayed ones included; slices without a quota get none.
        `allowed_keys`, if given, limits bidding to those (slug, side) pairs --
        to repeat an earlier test on exactly the same bids."""
        self.shares = shares
        self.skip = skip
        self.max_exposure = max_exposure
        self.quotas = quotas
        self.allowed_keys = allowed_keys
        self.slice_count: dict[tuple[str, str], int] = {}
        self.metas: dict[str, MarketMeta] = {}
        self.books: dict[str, Book] = {}
        self.states: dict[str, str] = {}
        self.decided: set[tuple[str, str]] = set()      # never place this key again
        self.skipped: dict[tuple[str, str], str] = {}
        self.orders: dict[tuple[str, str], LiveOrder] = {}
        self.by_id: dict[str, tuple[str, str]] = {}
        self.orphans: dict[str, list[dict[str, Any]]] = {}  # updates seen before the id
        self.seen_execs: set[str] = set()
        self.shadows: dict[tuple[str, str], VirtualBid] = {}
        self.stopped: set[tuple[str, str]] = set()
        self.pending_join: set[tuple[str, str]] = set()
        self.seen_trades: set[str] = set()
        self._contrib: dict[tuple[str, str], float] = {}
        self.exposure = 0.0

    # --- exposure: dollars that could still be lost ----------------------------

    def _risk(self, key: tuple[str, str]) -> float:
        o = self.orders.get(key)
        if o is None or o.status == "rejected" or is_terminal(self.states.get(key[0], "")):
            return 0.0
        if o.status == "placed" and is_dead(o.state):
            return o.cum_qty * COST_PER_SHARE
        return o.shares * COST_PER_SHARE

    def _touch(self, key: tuple[str, str]) -> None:
        new = self._risk(key)
        self.exposure += new - self._contrib.get(key, 0.0)
        self._contrib[key] = new

    def _touch_slug(self, slug: str) -> None:
        for side in SIDES:
            if (slug, side) in self.orders:
                self._touch((slug, side))

    # --- markets ---------------------------------------------------------------

    def add_markets(self, metas: list[MarketMeta]) -> list[str]:
        new = [m.slug for m in metas if m.slug not in self.metas]
        for m in metas:
            self.metas.setdefault(m.slug, m)
        return new

    def open_slugs(self) -> list[str]:
        return [s for s in self.metas if not is_terminal(self.states.get(s, ""))]

    def handle_market(
        self, msg: dict[str, Any], now: datetime
    ) -> tuple[list[dict[str, Any]], list[Intent]]:
        trade = parse_trade(msg)
        if trade is not None:
            return self._trade(trade, now.isoformat()), []
        book = parse_book(msg)
        if book is not None:
            return self._book(book, now)
        return [], []

    def _trade(self, trade: Trade, ts: str) -> list[dict[str, Any]]:
        if trade.trade_id and trade.trade_id in self.seen_trades:
            return []
        if trade.trade_id:
            self.seen_trades.add(trade.trade_id)
        out = []
        for side in SIDES:
            key = (trade.slug, side)
            bid = self.shadows.get(key)
            if bid is None or key in self.stopped or not on_trade(bid, trade):
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

    def _book(self, book: Book, now: datetime) -> tuple[list[dict[str, Any]], list[Intent]]:
        out: list[dict[str, Any]] = []
        intents: list[Intent] = []
        ts = now.isoformat()
        if book.state and self.states.get(book.slug) != book.state:
            self.states[book.slug] = book.state
            out.append({"event": "state", "ts": ts, "slug": book.slug, "state": book.state})
            if is_terminal(book.state):
                self._touch_slug(book.slug)
        self.books[book.slug] = book
        meta = self.metas.get(book.slug)
        if meta is None:
            return out, intents
        for side in SIDES:
            key = (book.slug, side)
            bid = self.shadows.get(key)
            if bid is not None and key not in self.stopped:
                on_depth(bid, own_adjusted(book, side, self._own(key)))
            elif key in self.pending_join:
                out.extend(self._join(key, now))
        if book.state == "MARKET_STATE_OPEN":
            for side in SIDES:
                rec, intent = self._decide(meta, book, side, now)
                if rec:
                    out.append(rec)
                if intent:
                    intents.append(intent)
        return out, intents

    def _own(self, key: tuple[str, str]) -> float:
        o = self.orders.get(key)
        return o.unfilled if o else 0.0

    def _decide(
        self, meta: MarketMeta, book: Book, side: str, now: datetime
    ) -> tuple[dict[str, Any] | None, Intent | None]:
        key = (meta.slug, side)
        if key in self.decided or (meta.market_type, side) in self.skip:
            return None, None
        if self.allowed_keys is not None and key not in self.allowed_keys:
            return None, None

        def skip(reason: str, final: bool = True) -> tuple[dict[str, Any] | None, None]:
            if final:
                self.decided.add(key)
            if self.skipped.get(key) == reason:
                return None, None  # already logged; a cap skip is retried quietly
            self.skipped[key] = reason
            return ({"event": "skip", "ts": now.isoformat(), "slug": meta.slug, "side": side,
                     "reason": reason, "final": final, "market_type": meta.market_type}, None)

        if book.would_cross(side):
            return skip("would_cross")
        kickoff = parse_kickoff(meta.kickoff)
        if kickoff is None:
            return skip("no_kickoff")
        expires = kickoff + EXPIRY_AFTER_KICKOFF
        if expires <= now + timedelta(minutes=5):
            return skip("past_expiry")
        slice_ = (meta.market_type, side)
        if self.quotas is not None and self.slice_count.get(slice_, 0) >= self.quotas.get(
            slice_, 0
        ):
            return skip("quota", final=False)  # a later run may raise the quota
        cost = self.shares * COST_PER_SHARE
        if self.exposure + cost > self.max_exposure + EPS:
            return skip("cap", final=False)  # the cap can free up as markets finish
        self.slice_count[slice_] = self.slice_count.get(slice_, 0) + 1
        self.decided.add(key)
        self.skipped.pop(key, None)
        self.orders[key] = LiveOrder(slug=meta.slug, side=side, shares=self.shares)
        self._touch(key)
        return None, Intent(meta.slug, side, LONG_PRICE[side], self.shares, expires)

    # --- placement results -----------------------------------------------------

    def intent_record(self, intent: Intent, now: datetime) -> dict[str, Any]:
        """Logged *before* the order is sent: a key with one is never placed again."""
        meta = self.metas[intent.slug]
        return {"event": "intent", "ts": now.isoformat(), "slug": intent.slug,
                "side": intent.side, "long_price": intent.long_price, "shares": intent.shares,
                "expires_at": intent.expires_at.isoformat(), "market_type": meta.market_type,
                "game": meta.game, "title": meta.title}

    def placed(
        self, intent: Intent, order_id: str, now: datetime
    ) -> list[dict[str, Any]]:
        key = (intent.slug, intent.side)
        o = self.orders[key]
        o.status, o.order_id = "placed", order_id
        self.by_id[order_id] = key
        self._touch(key)
        out: list[dict[str, Any]] = [{"event": "placed", "ts": now.isoformat(),
                                      "slug": intent.slug, "side": intent.side,
                                      "order_id": order_id}]
        for update in self.orphans.pop(order_id, []):
            out.extend(self._apply_order(key, update, now))
        return out

    def failed(
        self, intent: Intent, status: str, message: str, now: datetime,
        order_id: str = "",
    ) -> list[dict[str, Any]]:
        """`rejected`: the venue refused it, nothing rests. `unknown`: it may rest.

        An unknown order keeps its exposure and is never retried; if its id is
        known, updates for it are still applied.
        """
        key = (intent.slug, intent.side)
        o = self.orders[key]
        o.status, o.message = status, message
        if order_id:
            o.order_id = order_id
            self.by_id[order_id] = key
        self._touch(key)
        return [{"event": "place_failed", "ts": now.isoformat(), "slug": intent.slug,
                 "side": intent.side, "status": status, "message": message[:400],
                 "order_id": order_id}]

    # --- the private order stream ----------------------------------------------

    def handle_private(self, msg: dict[str, Any], now: datetime) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for update in parse_order_updates(msg):
            order_id = str(update.get("id") or "")
            if not order_id:
                continue
            key = self.by_id.get(order_id)
            if key is None:
                # The ack can beat the batch reply that tells us the id; hold it.
                # Orders this tool never placed also land here and stay ignored.
                held = self.orphans.setdefault(order_id, [])
                if len(held) < 20:
                    held.append(update)
                continue
            out.extend(self._apply_order(key, update, now))
        return out

    def _apply_order(
        self, key: tuple[str, str], update: dict[str, Any], now: datetime
    ) -> list[dict[str, Any]]:
        exec_id = str(update.get("_exec_id") or "")
        if exec_id and exec_id in self.seen_execs:
            return []
        if exec_id:
            self.seen_execs.add(exec_id)
        o = self.orders[key]
        if o.status == "unknown":
            o.status = "placed"  # it exists after all
        state = str(update.get("state") or o.state)
        cum = max(o.cum_qty, _qty(update.get("cumQuantity")))
        out: list[dict[str, Any]] = []
        if state != o.state or cum > o.cum_qty + EPS:
            o.state, o.cum_qty = state, cum
            self._touch(key)
            out.append({"event": "order", "ts": now.isoformat(), "slug": key[0],
                        "side": key[1], "order_id": o.order_id, "state": state,
                        "cum_qty": cum, "exec_id": exec_id})
        if "REJECT" in state:
            o.status = "rejected"
            self._touch(key)
            return out
        if key not in self.shadows and key not in self.pending_join:
            out.extend(self._join(key, now))
        if is_dead(state) and key in self.shadows and key not in self.stopped:
            self.stopped.add(key)
            out.append({"event": "shadow_stop", "ts": now.isoformat(), "slug": key[0],
                        "side": key[1], "reason": state})
        return out

    def _join(self, key: tuple[str, str], now: datetime) -> list[dict[str, Any]]:
        book = self.books.get(key[0])
        meta = self.metas.get(key[0])
        if book is None or meta is None:
            self.pending_join.add(key)
            return []
        self.pending_join.discard(key)
        adjusted = own_adjusted(book, key[1], self._own(key))
        depth = adjusted.depth(key[1])
        bid = VirtualBid(
            slug=meta.slug, side=key[1], event_ticker=meta.event_ticker, game=meta.game,
            market_type=meta.market_type, sports_market_type=meta.sports_market_type,
            kickoff=meta.kickoff, title=meta.title, shares=self.orders[key].shares,
            joined_at=now.isoformat(), queue_at_join=depth,
            would_cross=False, ahead=depth,
        )
        self.shadows[key] = bid
        return [{"event": "join", "ts": now.isoformat(), **asdict(bid),
                 "order_id": self.orders[key].order_id}]

    # --- restart ---------------------------------------------------------------

    def replay(self, records: list[dict[str, Any]]) -> None:
        """Rebuild from the log. Every key with an `intent` is never placed again."""
        shadow = Tracker()
        shadow.replay(records)  # join/trade/state records share the paper format
        self.shadows = shadow.bids
        self.seen_trades = shadow.seen_trades
        self.states = dict(shadow.states)
        for rec in records:
            ev = rec.get("event")
            key = (str(rec.get("slug")), str(rec.get("side")))
            if ev == "intent":
                self.decided.add(key)
                slice_ = (str(rec.get("market_type")), key[1])
                self.slice_count[slice_] = self.slice_count.get(slice_, 0) + 1
                self.orders[key] = LiveOrder(slug=key[0], side=key[1],
                                             shares=float(rec["shares"]))
            elif ev == "skip":
                if rec.get("final", True):
                    self.decided.add(key)
                self.skipped[key] = str(rec.get("reason"))
            elif ev == "placed" and key in self.orders:
                o = self.orders[key]
                o.status, o.order_id = "placed", str(rec["order_id"])
                self.by_id[o.order_id] = key
            elif ev == "place_failed" and key in self.orders:
                o = self.orders[key]
                o.status, o.message = str(rec["status"]), str(rec.get("message") or "")
                if rec.get("order_id"):
                    o.order_id = str(rec["order_id"])
                    self.by_id[o.order_id] = key
            elif ev == "order" and key in self.orders:
                o = self.orders[key]
                o.state = str(rec["state"])
                o.cum_qty = max(o.cum_qty, float(rec["cum_qty"]))
                if o.status == "unknown":
                    o.status = "placed"
                if "REJECT" in o.state:
                    o.status = "rejected"
                if rec.get("exec_id"):
                    self.seen_execs.add(str(rec["exec_id"]))
            elif ev == "shadow_stop":
                self.stopped.add(key)
        for key in self.orders:
            self._touch(key)
        # An order acked while no book was cached joins at the next snapshot.
        self.pending_join = {
            k for k, o in self.orders.items()
            if o.status == "placed" and o.state and k not in self.shadows
            and "REJECT" not in o.state
        }

    def summary(self) -> dict[str, float]:
        orders = list(self.orders.values())
        return {
            "markets": len(self.metas),
            "placed": sum(o.status == "placed" for o in orders),
            "resting": sum(o.live for o in orders),
            "rejected": sum(o.status == "rejected" for o in orders),
            "unknown": sum(o.status == "unknown" for o in orders),
            "real_fills": sum(o.cum_qty > 0 for o in orders),
            "shadow_fills": sum(b.queue_filled > 0 for b in self.shadows.values()),
            "exposure": round(self.exposure, 2),
        }


# --- report ------------------------------------------------------------------


def compare_rows(
    tracker: LiveTracker, settlements: dict[str, float]
) -> list[dict[str, Any]]:
    """One row per real order that rested: real fill and shadow fill side by side."""
    rows = []
    for key, o in tracker.orders.items():
        if o.status != "placed":
            continue
        b = tracker.shadows.get(key)
        meta_type = b.market_type if b else (
            tracker.metas[key[0]].market_type if key[0] in tracker.metas else "UNKNOWN"
        )
        s = settlements.get(key[0])
        shadow_q = b.queue_filled if b else 0.0
        shadow_o = b.optimistic_filled if b else 0.0
        rows.append({
            "slug": key[0], "side": key[1], "market_type": meta_type,
            "game": b.game if b else "", "title": b.title if b else "",
            "order_id": o.order_id, "state": o.state,
            "queue_at_join": b.queue_at_join if b else None,
            "real_filled": o.cum_qty, "shadow_queue_filled": shadow_q,
            "shadow_optimistic_filled": shadow_o,
            "settlement": s,
            "real_pnl": pnl(o.cum_qty, key[1], s) if s is not None else None,
            "shadow_pnl": pnl(shadow_q, key[1], s) if s is not None else None,
            "agree": (o.cum_qty > EPS) == (shadow_q > EPS),
            "shadowed": b is not None,
            "stopped": key in tracker.stopped,
        })
    return rows


def compare_groups(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for r in rows:
        groups.setdefault((r["market_type"], r["side"]), []).append(r)
    out = []
    for (mtype, side), rs in sorted(groups.items()):
        settled = [r for r in rs if r["settlement"] is not None]
        out.append({
            "market_type": mtype, "side": side, "orders": len(rs),
            "real_fills": sum(r["real_filled"] > EPS for r in rs),
            "shadow_fills": sum(r["shadow_queue_filled"] > EPS for r in rs),
            "both": sum(r["real_filled"] > EPS and r["shadow_queue_filled"] > EPS for r in rs),
            "real_only": sum(r["real_filled"] > EPS >= r["shadow_queue_filled"] for r in rs),
            "shadow_only": sum(r["shadow_queue_filled"] > EPS >= r["real_filled"] for r in rs),
            "real_pnl": sum(r["real_pnl"] for r in settled),
            "shadow_pnl": sum(r["shadow_pnl"] for r in settled),
            "unsettled": sum(
                1 for r in rs
                if r["settlement"] is None
                and (r["real_filled"] > EPS or r["shadow_queue_filled"] > EPS)
            ),
        })
    return out
