import os
import json
import time
import asyncio
import datetime
import requests
from dataclasses import dataclass, field
from typing import List, Dict, Optional

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY, SELL

# ── Config ────────────────────────────────────────────────────────────────────
INITIAL_BET_PER_LEVEL = float(os.getenv("INITIAL_BET_PER_LEVEL", 2.0))
FLIP_BET_USD          = float(os.getenv("FLIP_BET_USD", 6.0))
ENTRY_PRICES          = [0.40, 0.30, 0.20]
FLIP_PRICE            = float(os.getenv("FLIP_PRICE", 0.80))
STOP_LOSS_PRICE       = float(os.getenv("STOP_LOSS_PRICE", 0.15))
TAKE_PROFIT_PRICE     = float(os.getenv("TAKE_PROFIT_PRICE", 0.98))
POLL_INTERVAL_SEC     = int(os.getenv("POLL_INTERVAL_SEC", 1))
SHOW_DASHBOARD        = os.getenv("SHOW_DASHBOARD", "true").lower() == "true"

INTERVAL_SEC = 300
SLUG_PREFIX  = "btc-updown-5m-"
GAMMA_API    = "https://gamma-api.polymarket.com"
CLOB_API     = "https://clob.polymarket.com"

DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"

# ── Stats Dashboard ───────────────────────────────────────────────────────────
class BotStats:
    def __init__(self):
        self.reset()

    def reset(self):
        self.total_windows = 0
        self.wins = 0
        self.losses = 0
        self.flips = 0
        self.no_fills = 0
        self.total_profit_usd = 0.0
        self.current_streak = 0
        self.max_streak = 0
        self.last_result = None
        self.today = datetime.date.today()

    def record_result(self, is_win: bool, profit: float = 0.0, flipped: bool = False, no_fill: bool = False):
        if datetime.date.today() != self.today:
            print("🌅 New day — resetting stats")
            self.reset()

        self.total_windows += 1
        if no_fill:
            self.no_fills += 1
            self.last_result = "NO FILL"
        elif is_win:
            self.wins += 1
            self.current_streak = self.current_streak + 1 if self.last_result == "WIN" else 1
            self.last_result = "WIN"
            self.total_profit_usd += profit
        else:
            self.losses += 1
            self.current_streak = self.current_streak - 1 if self.last_result == "LOSS" else -1
            self.last_result = "LOSS"
            self.total_profit_usd += profit

        if abs(self.current_streak) > self.max_streak:
            self.max_streak = abs(self.current_streak)
        if flipped:
            self.flips += 1

    def print_dashboard(self):
        if not SHOW_DASHBOARD:
            return
        win_rate = (self.wins / self.total_windows * 100) if self.total_windows > 0 else 0.0
        print("\n" + "="*88)
        print("📊 CANDLE BOT SUMMARY DASHBOARD")
        print("="*88)
        print(f"Windows : {self.total_windows:3d} | Wins: {self.wins:3d} | Losses: {self.losses:3d} | NoFill: {self.no_fills:2d}")
        print(f"Flips   : {self.flips:3d} | Win Rate: {win_rate:5.1f}% | Est. P&L: ${self.total_profit_usd:7.2f}")
        print(f"Streak  : {self.current_streak:3d} (max {self.max_streak}) | Last: {self.last_result or 'N/A'}")
        print(f"TP: {TAKE_PROFIT_PRICE} | DRY_RUN: {DRY_RUN}")
        print("="*88 + "\n")


stats = BotStats()

# ── Window State ──────────────────────────────────────────────────────────────
@dataclass
class WindowState:
    slot_ts: int
    end_ts: int
    initial_side: str
    initial_token_id: str
    opposite_token_id: str
    initial_order_ids: List[str] = field(default_factory=list)
    flip_order_id: Optional[str] = None
    flipped: bool = False
    tp_orders: Dict[str, str] = field(default_factory=dict)
    resolved: bool = False
    filled_amount: float = 0.0


# ── Polymarket Client ─────────────────────────────────────────────────────────
class PolymarketClient:
    def __init__(self):
        pk = os.getenv("PRIVATE_KEY")
        funder = os.getenv("FUNDER")
        sig_type = int(os.getenv("SIGNATURE_TYPE", 2))

        if not pk or not funder:
            raise ValueError("PRIVATE_KEY and FUNDER env vars are required")

        print(f"[client] Initializing | signature_type={sig_type} | DRY_RUN={DRY_RUN}")
        self.clob = ClobClient(
            host=CLOB_API,
            key=pk,
            chain_id=137,
            funder=funder,
            signature_type=sig_type,
        )
        self.clob.set_api_creds(self.clob.create_or_derive_api_creds())
        print("[client] ✅ Connected")

    @staticmethod
    def _parse_token_ids(market: Dict):
        clob = market.get("clobTokenIds")
        if isinstance(clob, str):
            try:
                clob = json.loads(clob)
            except:
                clob = None
        if isinstance(clob, list) and len(clob) >= 2:
            return str(clob[0]), str(clob[1])
        return None, None

    def fetch_market_for_slot(self, slot_ts: int):
        slug = f"{SLUG_PREFIX}{slot_ts}"
        try:
            r = requests.get(f"{GAMMA_API}/events", params={"slug": slug}, timeout=8)
            if r.status_code != 200:
                return None
            data = r.json()
            events = data if isinstance(data, list) else [data]
            for event in events:
                if event.get("slug") != slug:
                    continue
                for m in event.get("markets", []):
                    yes_t, no_t = self._parse_token_ids(m)
                    if yes_t and no_t:
                        return {
                            "yes_token_id": yes_t,
                            "no_token_id": no_t,
                            "slot_ts": slot_ts,
                            "end_ts": slot_ts + INTERVAL_SEC,
                        }
        except Exception as e:
            print(f"[fetch_market] Error for slot {slot_ts}: {e}")
        return None

    def find_next_window_market(self):
        """Aggressive: find the soonest ready or just-opened window (no skipping)."""
        now = int(time.time())
        current_slot = (now // INTERVAL_SEC) * INTERVAL_SEC

        for offset in range(0, 5):  # Check current + next 4 slots
            ts = current_slot + offset * INTERVAL_SEC
            m = self.fetch_market_for_slot(ts)
            if m:
                dt_str = datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc).strftime("%H:%M:%S UTC")
                status = "JUST OPENED" if current_slot <= ts <= now + INTERVAL_SEC else "NEXT"
                print(f"[find_next] ✅ Found {status} window: slot_ts={ts} ({dt_str})")
                return m

        # Fallback aggressive scan
        print("[find_next] Direct lookup missed — scanning active events...")
        try:
            r = requests.get(f"{GAMMA_API}/events", params={"active": "true", "closed": "false", "limit": 100}, timeout=12)
            if r.status_code == 200:
                candidates = []
                for event in r.json():
                    slug = event.get("slug", "")
                    if not slug.startswith(SLUG_PREFIX):
                        continue
                    try:
                        ts = int(slug.rsplit("-", 1)[-1])
                    except:
                        continue
                    if ts + INTERVAL_SEC < now:  # fully past
                        continue
                    for m in event.get("markets", []):
                        yes_t, no_t = self._parse_token_ids(m)
                        if yes_t and no_t:
                            candidates.append((ts, {
                                "yes_token_id": yes_t,
                                "no_token_id": no_t,
                                "slot_ts": ts,
                                "end_ts": ts + INTERVAL_SEC,
                            }))
                if candidates:
                    candidates.sort(key=lambda x: x[0])
                    ts, m = candidates[0]
                    dt_str = datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc).strftime("%H:%M:%S UTC")
                    print(f"[find_next] ✅ Found via scan: slot_ts={ts} ({dt_str})")
                    return m
        except Exception as e:
            print(f"[find_next] Scan error: {e}")

        print("[find_next] ❌ No window ready yet")
        return None

    def is_accepting_orders(self, token_id: str) -> bool:
        try:
            price = self.clob.get_price(token_id, side="BUY")
            p = float(price.get("price") if isinstance(price, dict) else price)
            return p > 0
        except:
            return False

    def place_limit_buy(self, token_id: str, price: float, amount_usd: float, label: str) -> Optional[str]:
        min_shares = 5.0
        min_usd = min_shares * price
        effective_usd = max(amount_usd, min_usd + 0.05)
        size = round(effective_usd / price, 2)

        if DRY_RUN:
            oid = f"DRY-BUY-{label}-{int(time.time())}"
            print(f"[DRY] BUY {label} @ {price} (${effective_usd:.2f}) → {oid}")
            return oid

        try:
            order = OrderArgs(token_id=token_id, price=price, size=size, side=BUY)
            signed = self.clob.create_order(order)
            resp = self.clob.post_order(signed, OrderType.GTC)
            oid = resp.get("orderID") or resp.get("order_id") if isinstance(resp, dict) else None
            print(f"[ORDER] BUY {label} @ {price} → {oid}")
            return oid
        except Exception as e:
            print(f"[ORDER FAILED] BUY {label}: {e}")
            return None

    def place_limit_sell(self, token_id: str, price: float, size: float, label: str) -> Optional[str]:
        if DRY_RUN:
            print(f"[DRY] SELL {label} @ {price} size={size}")
            return f"DRY-SELL-{label}"
        try:
            order = OrderArgs(token_id=token_id, price=price, size=size, side=SELL)
            signed = self.clob.create_order(order)
            resp = self.clob.post_order(signed, OrderType.GTC)
            oid = resp.get("orderID") or resp.get("order_id") if isinstance(resp, dict) else None
            print(f"[TP] SELL {label} @ {price}")
            return oid
        except Exception as e:
            print(f"[TP FAILED] {label}: {e}")
            return None

    def cancel_order(self, order_id: str, label: str):
        if DRY_RUN:
            print(f"[DRY] CANCEL {label}")
            return
        try:
            self.clob.cancel(order_id=order_id)
            print(f"[CANCEL] {label}")
        except Exception as e:
            print(f"[CANCEL FAILED] {label}: {e}")

    def get_filled_size(self, order_id: str) -> float:
        if DRY_RUN:
            return 0.0
        try:
            status = self.clob.get_order(order_id)
            raw = status.get("size_matched") or status.get("sizeFilled") or status.get("filled") or "0"
            return float(raw)
        except:
            return 0.0

    def get_previous_candle_color(self) -> str:
        try:
            r = requests.get(
                "https://api.binance.com/api/v3/klines",
                params={"symbol": "BTCUSDT", "interval": "5m", "limit": 2},
                timeout=5
            )
            if r.status_code == 200:
                data = r.json()
                if len(data) >= 2:
                    prev = data[-2]
                    o = float(prev[1])
                    c = float(prev[4])
                    return "GREEN" if c > o else "RED" if c < o else "NEUTRAL"
        except Exception as e:
            print(f"[candle] Error: {e}")
        return "NEUTRAL"


# ── Placement (Fast start on window open) ─────────────────────────────────────
async def place_candle_based_orders(client: PolymarketClient) -> Optional[WindowState]:
    market = client.find_next_window_market()
    if not market:
        return None

    color = client.get_previous_candle_color()
    if color == "NEUTRAL":
        print("[candle] Neutral candle — skipping")
        return None

    initial_side = "UP" if color == "GREEN" else "DOWN"
    initial_token = market["yes_token_id"] if initial_side == "UP" else market["no_token_id"]
    opposite_token = market["no_token_id"] if initial_side == "UP" else market["yes_token_id"]

    print(f"\n{'='*85}")
    print(f"🚀 {color} bias → Placing {initial_side} DCA orders as soon as window opens")
    print(f"Entries: ${INITIAL_BET_PER_LEVEL} @ {ENTRY_PRICES} | SL flip @ {STOP_LOSS_PRICE} → ${FLIP_BET_USD} @ {FLIP_PRICE}")
    print(f"{'='*85}")

    # Fast retry until orderbook is live
    for attempt in range(40):  # max \~40 seconds
        if client.is_accepting_orders(initial_token):
            print(f"[premarket] ✅ Orderbook live — placing DCA orders now")
            break
        if attempt % 8 == 0 and attempt > 0:
            print(f"[premarket] Orderbook not live yet (attempt {attempt+1})")
        await asyncio.sleep(1)

    # Place the 3 DCA limit buys immediately
    initial_order_ids = []
    for price in ENTRY_PRICES:
        oid = client.place_limit_buy(initial_token, price, INITIAL_BET_PER_LEVEL, f"{initial_side}-DCA-{int(price*100)}c")
        if oid:
            initial_order_ids.append(oid)

    if not initial_order_ids:
        print("[premarket] ❌ Failed to place orders")
        return None

    return WindowState(
        slot_ts=market["slot_ts"],
        end_ts=market["end_ts"],
        initial_side=initial_side,
        initial_token_id=initial_token,
        opposite_token_id=opposite_token,
        initial_order_ids=initial_order_ids,
    )


# ── Monitoring ────────────────────────────────────────────────────────────────
async def monitor_window(client: PolymarketClient, state: WindowState):
    global current_window
    print(f"[monitor] Watching {state.initial_side} bias window | poll={POLL_INTERVAL_SEC}s")

    while True:
        now = int(time.time())
        if now >= state.end_ts:
            print("[monitor] ⏰ Window closed")
            for oid in state.initial_order_ids:
                client.cancel_order(oid, "expired")
            if state.flip_order_id:
                client.cancel_order(state.flip_order_id, "expired-flip")

            filled = sum(client.get_filled_size(oid) for oid in state.initial_order_ids)
            if state.flipped and state.flip_order_id:
                filled += client.get_filled_size(state.flip_order_id)

            no_fill = filled < 0.01
            is_win = not no_fill
            profit = filled * (TAKE_PROFIT_PRICE - 0.35) if not no_fill else 0.0

            stats.record_result(is_win=is_win, profit=profit, flipped=state.flipped, no_fill=no_fill)
            stats.print_dashboard()

            state.resolved = True
            current_window = None
            return

        # Fast SL flip check
        if not state.flipped:
            try:
                price_data = client.clob.get_price(state.initial_token_id, side="BUY")
                curr_price = float(price_data.get("price") if isinstance(price_data, dict) else price_data)
                if curr_price <= STOP_LOSS_PRICE:
                    print(f"🔴 SL HIT @ {curr_price:.2f} — FLIPPING!")
                    for oid in state.initial_order_ids:
                        client.cancel_order(oid, f"{state.initial_side}-SL")
                    flip_label = "DOWN" if state.initial_side == "UP" else "UP"
                    state.flip_order_id = client.place_limit_buy(
                        state.opposite_token_id, FLIP_PRICE, FLIP_BET_USD, f"FLIP-{flip_label}"
                    )
                    state.flipped = True
            except:
                pass

        # Place TP on any fill
        filled_initial = sum(client.get_filled_size(oid) for oid in state.initial_order_ids)
        if filled_initial > 0.01 and state.initial_token_id not in state.tp_orders:
            tp_id = client.place_limit_sell(state.initial_token_id, TAKE_PROFIT_PRICE, round(filled_initial, 2), f"TP-{state.initial_side}")
            if tp_id:
                state.tp_orders[state.initial_token_id] = tp_id

        if state.flipped and state.flip_order_id:
            filled_flip = client.get_filled_size(state.flip_order_id)
            if filled_flip > 0.01 and state.opposite_token_id not in state.tp_orders:
                tp_id = client.place_limit_sell(state.opposite_token_id, TAKE_PROFIT_PRICE, round(filled_flip, 2), "TP-FLIP")
                if tp_id:
                    state.tp_orders[state.opposite_token_id] = tp_id

        await asyncio.sleep(POLL_INTERVAL_SEC)


# ── Main Loop ─────────────────────────────────────────────────────────────────
current_window: Optional[WindowState] = None
client = PolymarketClient()

async def main():
    global current_window
    print("🤖 Candle 5m BTC Bot (Fast Entry + Dashboard) started\n")
    stats.print_dashboard()

    while True:
        try:
            if current_window is None or current_window.resolved:
                current_window = await place_candle_based_orders(client)

            if current_window and not current_window.resolved:
                await monitor_window(client, current_window)

            await asyncio.sleep(3)   # fast cycle when idle
        except Exception as e:
            print(f"[main loop] Error: {e}")
            await asyncio.sleep(10)


if __name__ == "__main__":
    asyncio.run(main())
