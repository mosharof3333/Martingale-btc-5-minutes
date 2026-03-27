"""
Polymarket BTC 5m Pre-Market Dual Limit Bot
============================================

Strategy (5m only):
  1. While the current 5m window is live, find the NEXT window's market
     (Polymarket pre-creates ~24h ahead).
  2. Place two limit BUY orders on the next window at ENTRY_PRICE (default $0.33):
       - BUY UP  @ 0.33  for $bet_amount
       - BUY DOWN @ 0.33  for $bet_amount
  3. Poll every POLL_INTERVAL_SEC seconds once the next window goes live.
     Whichever side fills first → cancel the other → place TP limit SELL at 0.99.
  4. No stop loss — losing positions expire worthless at window close.

Martingale on consecutive losses:
  - Base bet = $BASE_BET_USD (default $1)
  - Two consecutive losses → multiply current bet by 2x
  - Any win resets bet back to $BASE_BET_USD
  - Neither side fills (no fill) → not counted as a loss, bet unchanged

Env vars:
  PRIVATE_KEY        Wallet private key
  FUNDER             Proxy wallet address
  SIGNATURE_TYPE     0=EOA, 1=Magic/email, 2=browser (default 2)
  BASE_BET_USD       Base order size in USD (default 1.0)
  ENTRY_PRICE        Limit price for both UP and DOWN (default 0.33)
  TAKE_PROFIT_PRICE  TP limit sell price (default 0.99)
  POLL_INTERVAL_SEC  Fill-check polling interval in seconds (default 10)
  DRY_RUN            "true" to simulate without placing real orders (default true)
"""

import os
import json
import time
import asyncio
import traceback
import datetime
import requests
from dataclasses import dataclass
from typing import Optional, Dict, Tuple, List

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY, SELL


# ── Config ────────────────────────────────────────────────────────────────────

BASE_BET_USD      = float(os.getenv("BASE_BET_USD", 1.0))
ENTRY_PRICE       = float(os.getenv("ENTRY_PRICE", 0.33))
TAKE_PROFIT_PRICE = float(os.getenv("TAKE_PROFIT_PRICE", 0.99))
POLL_INTERVAL_SEC = int(os.getenv("POLL_INTERVAL_SEC", 10))
SIGNATURE_TYPE    = int(os.getenv("SIGNATURE_TYPE", 2))
DRY_RUN           = os.getenv("DRY_RUN", "true").lower() == "true"

INTERVAL_SEC = 300   # 5 minutes
SLUG_PREFIX  = "btc-updown-5m-"
GAMMA_API    = "https://gamma-api.polymarket.com"
CLOB_API     = "https://clob.polymarket.com"


# ── State dataclasses ─────────────────────────────────────────────────────────

@dataclass
class WindowState:
    """Everything we need to track for one 5m window cycle."""
    slot_ts: int
    end_ts: int
    yes_token_id: str        # UP token
    no_token_id: str         # DOWN token
    bet_amount: float
    up_order_id: Optional[str] = None
    down_order_id: Optional[str] = None
    filled_side: Optional[str] = None   # "UP" or "DOWN"
    filled_shares: float = 0.0
    tp_order_id: Optional[str] = None
    resolved: bool = False


@dataclass
class MartingaleTracker:
    base_bet: float = BASE_BET_USD
    current_bet: float = BASE_BET_USD
    consecutive_losses: int = 0

    def record_win(self):
        print(f"[martingale] ✅ WIN — resetting bet from ${self.current_bet:.2f} → ${self.base_bet:.2f}")
        self.consecutive_losses = 0
        self.current_bet = self.base_bet

    def record_loss(self):
        self.consecutive_losses += 1
        if self.consecutive_losses >= 2:
            prev = self.current_bet
            self.current_bet = self.current_bet * 2
            print(f"[martingale] ❌ LOSS #{self.consecutive_losses} — doubling bet: ${prev:.2f} → ${self.current_bet:.2f}")
        else:
            print(f"[martingale] ❌ LOSS #1 — bet stays at ${self.current_bet:.2f} (need 2 consecutive to double)")

    def record_no_fill(self):
        print(f"[martingale] ⏸  NO FILL — bet unchanged at ${self.current_bet:.2f}")


# ── Globals ───────────────────────────────────────────────────────────────────

martingale = MartingaleTracker()
current_window: Optional[WindowState] = None


# ── Polymarket Client ─────────────────────────────────────────────────────────

class PolymarketClient:

    def __init__(self):
        pk = os.getenv("PRIVATE_KEY")
        funder = os.getenv("FUNDER")
        if not pk or not funder:
            raise ValueError("PRIVATE_KEY and FUNDER env vars are required")
        self.clob = ClobClient(
            host=CLOB_API,
            key=pk,
            chain_id=137,
            funder=funder,
            signature_type=SIGNATURE_TYPE,
        )
        self.clob.set_api_creds(self.clob.create_or_derive_api_creds())
        print(f"[client] Connected | DRY_RUN={DRY_RUN} | "
              f"base_bet=${BASE_BET_USD} | entry={ENTRY_PRICE} | TP={TAKE_PROFIT_PRICE}")

    # ── Market discovery ──────────────────────────────────────────────────────

    @staticmethod
    def _parse_token_ids(market: Dict) -> Tuple[Optional[str], Optional[str]]:
        """
        clobTokenIds is returned as a JSON-encoded string by the Gamma API:
          e.g. "[\"token_up\", \"token_down\"]"
        index 0 = YES/UP,  index 1 = NO/DOWN
        """
        clob = market.get("clobTokenIds")
        if isinstance(clob, str):
            try:
                clob = json.loads(clob)
            except Exception:
                clob = None
        if isinstance(clob, list) and len(clob) >= 2:
            return str(clob[0]), str(clob[1])
        # Fallback: tokens list
        yes_id = no_id = None
        for t in market.get("tokens", []):
            tid = str(t.get("token_id") or t.get("clobTokenId") or "")
            outcome = str(t.get("outcome", "")).lower()
            if outcome in ["yes", "up", "1"]:
                yes_id = tid
            elif outcome in ["no", "down", "0"]:
                no_id = tid
        return yes_id, no_id

    @staticmethod
    def _end_ts_from_market(market_obj: Dict, event_obj: Dict, slot_ts: int) -> int:
        """Parse end timestamp from market or event endDate, fallback to slot_ts + interval."""
        raw = market_obj.get("endDate") or event_obj.get("endDate")
        if raw:
            try:
                d = datetime.datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
                return int(d.timestamp())
            except Exception:
                pass
        return slot_ts + INTERVAL_SEC

    def fetch_market_for_slot(self, slot_ts: int) -> Optional[Dict]:
        """
        GET /events?slug=btc-updown-5m-{slot_ts}
        Returns dict with yes_token_id, no_token_id, slot_ts, end_ts, question.
        """
        slug = f"{SLUG_PREFIX}{slot_ts}"
        try:
            r = requests.get(f"{GAMMA_API}/events", params={"slug": slug}, timeout=8)
            if r.status_code != 200:
                return None
            data = r.json()
            if not data:
                return None
            events = data if isinstance(data, list) else [data]
            for event in events:
                if event.get("slug") != slug:
                    continue
                for m in event.get("markets", []):
                    yes_t, no_t = self._parse_token_ids(m)
                    if yes_t and no_t:
                        end_ts = self._end_ts_from_market(m, event, slot_ts)
                        return {
                            "yes_token_id": yes_t,
                            "no_token_id":  no_t,
                            "slot_ts":      slot_ts,
                            "end_ts":       end_ts,
                            "question":     m.get("question") or event.get("title", ""),
                        }
        except Exception as e:
            print(f"[fetch_market] Error for {slug}: {e}")
        return None

    def find_next_window_market(self) -> Optional[Dict]:
        """
        Find the market for the NEXT 5m window (current + 1 interval).
        Pre-market orders are placed here while the current window is still live.
        """
        now        = int(time.time())
        current_ts = (now // INTERVAL_SEC) * INTERVAL_SEC
        next_ts    = current_ts + INTERVAL_SEC
        dt_str     = datetime.datetime.fromtimestamp(next_ts, tz=datetime.timezone.utc).strftime("%H:%M UTC")

        print(f"[find_next] Searching next window: slot_ts={next_ts} ({dt_str})")

        # Stage 1: direct slug lookups for next and +2 windows
        for ts in [next_ts, next_ts + INTERVAL_SEC]:
            m = self.fetch_market_for_slot(ts)
            if m:
                print(f"[find_next] ✅ Found via slug: {m['question'][:100]}")
                return m

        # Stage 2: scan active events, find the soonest future window
        print(f"[find_next] Slug lookup missed — scanning active events ...")
        try:
            r = requests.get(
                f"{GAMMA_API}/events",
                params={"active": "true", "closed": "false", "limit": 50},
                timeout=15,
            )
            if r.status_code == 200:
                candidates: List[Tuple[int, Dict]] = []
                for event in r.json():
                    slug = event.get("slug", "")
                    if not slug.startswith(SLUG_PREFIX):
                        continue
                    try:
                        ts = int(slug.rsplit("-", 1)[-1])
                    except ValueError:
                        continue
                    if ts <= now:   # skip current and past windows
                        continue
                    for m in event.get("markets", []):
                        yes_t, no_t = self._parse_token_ids(m)
                        if yes_t and no_t:
                            end_ts = self._end_ts_from_market(m, event, ts)
                            candidates.append((ts, {
                                "yes_token_id": yes_t,
                                "no_token_id":  no_t,
                                "slot_ts":      ts,
                                "end_ts":       end_ts,
                                "question":     m.get("question") or event.get("title", ""),
                            }))
                if candidates:
                    candidates.sort(key=lambda x: x[0])
                    ts, m = candidates[0]
                    print(f"[find_next] ✅ Found via scan: slot_ts={ts} {m['question'][:80]}")
                    return m
        except Exception as e:
            print(f"[find_next] Scan error: {e}")

        print(f"[find_next] ❌ No next window market found.")
        return None

    # ── Order operations ──────────────────────────────────────────────────────

    def is_accepting_orders(self, token_id: str) -> bool:
        """
        Check if a token is currently accepting orders via the CLOB price endpoint.
        Pre-market windows that aren't open yet will return 404 / no price.
        If we can fetch a price, the orderbook is live and orders are accepted.
        """
        try:
            price = self.clob.get_price(token_id, side="BUY")
            if isinstance(price, dict):
                p = float(price.get("price") or price.get("value") or 0)
            else:
                p = float(price)
            return p > 0
        except Exception:
            return False

    def place_limit_buy(self, token_id: str, price: float, amount_usd: float,
                        label: str) -> Optional[str]:
        """
        GTC limit BUY order.
        size (shares) = amount_usd / price
        Returns order_id or None.
        """
        size = round(amount_usd / price, 2)
        if DRY_RUN:
            oid = f"DRY-BUY-{label}-{int(time.time())}"
            print(f"[DRY RUN] LIMIT BUY {label} | price={price} size={size} (${amount_usd}) → {oid}")
            return oid
        try:
            order  = OrderArgs(token_id=token_id, price=price, size=size, side=BUY)
            signed = self.clob.create_order(order)
            resp   = self.clob.post_order(signed, OrderType.GTC)
            oid    = (resp.get("orderID") or resp.get("order_id")) if isinstance(resp, dict) else None
            print(f"[ORDER] LIMIT BUY {label} | price={price} size={size} (${amount_usd}) → {oid}")
            return oid
        except Exception as e:
            print(f"[ORDER FAILED] LIMIT BUY {label}: {e}")
            return None

    def place_limit_sell(self, token_id: str, price: float, size: float,
                         label: str) -> Optional[str]:
        """GTC limit SELL order for take profit. Returns order_id or None."""
        if DRY_RUN:
            oid = f"DRY-SELL-{label}-{int(time.time())}"
            print(f"[DRY RUN] LIMIT SELL {label} | price={price} size={size} → {oid}")
            return oid
        try:
            order  = OrderArgs(token_id=token_id, price=price, size=size, side=SELL)
            signed = self.clob.create_order(order)
            resp   = self.clob.post_order(signed, OrderType.GTC)
            oid    = (resp.get("orderID") or resp.get("order_id")) if isinstance(resp, dict) else None
            print(f"[TP ORDER] LIMIT SELL {label} | price={price} size={size} → {oid}")
            return oid
        except Exception as e:
            print(f"[TP FAILED] {label}: {e}")
            return None

    def cancel_order(self, order_id: str, label: str):
        """Cancel an open order by ID."""
        if DRY_RUN:
            print(f"[DRY RUN] CANCEL {label} | order_id={order_id}")
            return
        try:
            resp = self.clob.cancel(order_id=order_id)
            print(f"[CANCEL] {label} | order_id={order_id} → {resp}")
        except Exception as e:
            print(f"[CANCEL FAILED] {label} | order_id={order_id}: {e}")

    def get_filled_size(self, order_id: str) -> float:
        """Return filled share quantity for an order (0.0 if unfilled or error)."""
        if DRY_RUN:
            return 0.0   # DRY_RUN: simulate no fill unless overridden
        try:
            status = self.clob.get_order(order_id)
            if not status:
                return 0.0
            raw = (status.get("size_matched") or status.get("sizeFilled")
                   or status.get("filled") or "0")
            return float(raw)
        except Exception as e:
            print(f"[FILL CHECK] Error for {order_id}: {e}")
            return 0.0


# ── Pre-market order placement ────────────────────────────────────────────────

async def place_premarket_orders(client: PolymarketClient) -> Optional[WindowState]:
    """
    Find the next window market and place limit BUY orders on both sides.
    Waits until the market is actually accepting orders before placing.
    Returns a WindowState tracking both order IDs, or None if it fails.
    """
    market = client.find_next_window_market()
    if not market:
        return None

    bet     = martingale.current_bet
    slot_ts = market["slot_ts"]
    end_ts  = market["end_ts"]
    dt_str  = datetime.datetime.fromtimestamp(slot_ts, tz=datetime.timezone.utc).strftime("%H:%M UTC")

    print(f"\n{'='*55}")
    print(f" Pre-market orders for window {dt_str}")
    print(f" Bet: ${bet:.2f} each side | Entry: {ENTRY_PRICE} | TP: {TAKE_PROFIT_PRICE}")
    print(f" UP  token: {market['yes_token_id'][:28]}...")
    print(f" DOWN token: {market['no_token_id'][:28]}...")
    print(f"{'='*55}")

    # Wait until the market is accepting orders (orderbook is live)
    # Pre-market windows can exist in Gamma API before CLOB accepts orders for them
    max_wait = 120   # seconds — don't wait more than 2 minutes
    waited   = 0
    while not client.is_accepting_orders(market["yes_token_id"]):
        now_ts = int(time.time())
        if now_ts >= slot_ts:
            # Window has started — CLOB should be accepting orders by now
            if waited >= max_wait:
                print(f"[premarket] ⚠️  Market still not accepting orders after {max_wait}s — placing anyway ...")
                break
        secs_to_open = max(slot_ts - now_ts, 0)
        print(f"[premarket] Orderbook not live yet — {secs_to_open}s until window opens, waiting 5s ...")
        await asyncio.sleep(5)
        waited += 5

    up_id   = client.place_limit_buy(market["yes_token_id"], ENTRY_PRICE, bet, "UP")
    down_id = client.place_limit_buy(market["no_token_id"],  ENTRY_PRICE, bet, "DOWN")

    if not up_id and not down_id:
        print("[premarket] ❌ Both orders failed — skipping window.")
        return None

    state = WindowState(
        slot_ts=slot_ts,
        end_ts=end_ts,
        yes_token_id=market["yes_token_id"],
        no_token_id=market["no_token_id"],
        bet_amount=bet,
        up_order_id=up_id,
        down_order_id=down_id,
    )
    print(f"[premarket] ✅ UP order={up_id} | DOWN order={down_id}")
    return state


# ── Window monitoring ─────────────────────────────────────────────────────────

async def monitor_window(client: PolymarketClient, state: WindowState):
    """
    Poll for fills until the window expires.
    - First fill → cancel other side → wait 5s → place TP limit sell at 0.99
    - Window expires with fill → check if TP filled (win) or not (loss)
    - Window expires with no fill → no-fill (not counted as loss)
    """
    global current_window

    end_dt  = datetime.datetime.fromtimestamp(state.end_ts, tz=datetime.timezone.utc)
    slot_dt = datetime.datetime.fromtimestamp(state.slot_ts, tz=datetime.timezone.utc)
    print(f"\n[monitor] Watching {slot_dt.strftime('%H:%M')}–{end_dt.strftime('%H:%M UTC')} | "
          f"UP={state.up_order_id} DOWN={state.down_order_id}")

    while True:
        now = int(time.time())

        # ── Window expired ────────────────────────────────────────────────────
        if now >= state.end_ts:
            if state.filled_side is None:
                # No fill on either side
                print(f"[monitor] ⏰ Window closed — no fills. Cancelling any remaining orders.")
                if state.up_order_id:
                    client.cancel_order(state.up_order_id, "UP-expired")
                if state.down_order_id:
                    client.cancel_order(state.down_order_id, "DOWN-expired")
                martingale.record_no_fill()
            else:
                # We had a fill + TP was placed — check if TP filled (win) or not (loss)
                tp_filled = client.get_filled_size(state.tp_order_id) if state.tp_order_id else 0.0
                if tp_filled > 0:
                    print(f"[monitor] ✅ WIN — TP filled {tp_filled} shares before window closed")
                    martingale.record_win()
                else:
                    print(f"[monitor] ❌ LOSS — window closed, TP unfilled (position expired worthless)")
                    if state.tp_order_id:
                        client.cancel_order(state.tp_order_id, "TP-cleanup")
                    martingale.record_loss()

            print(f"[monitor] Next bet: ${martingale.current_bet:.2f} | "
                  f"Consecutive losses: {martingale.consecutive_losses}")
            state.resolved = True
            current_window = None
            return

        # ── Check for fills ───────────────────────────────────────────────────
        if state.filled_side is None:
            up_filled   = client.get_filled_size(state.up_order_id)   if state.up_order_id   else 0.0
            down_filled = client.get_filled_size(state.down_order_id) if state.down_order_id else 0.0

            filled_side   = None
            filled_shares = 0.0
            cancel_id     = None

            if up_filled > 0 and down_filled > 0:
                # Edge case: both filled — keep the larger, cancel the other
                if up_filled >= down_filled:
                    filled_side, filled_shares = "UP", up_filled
                    cancel_id = state.down_order_id
                    print(f"[monitor] ⚠️  Both sides filled simultaneously — keeping UP ({up_filled:.2f} shares)")
                else:
                    filled_side, filled_shares = "DOWN", down_filled
                    cancel_id = state.up_order_id
                    print(f"[monitor] ⚠️  Both sides filled simultaneously — ke
