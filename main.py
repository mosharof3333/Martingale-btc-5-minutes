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

# ── Global Stats Dashboard ────────────────────────────────────────────────────
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
            self.current_streak = (self.current_streak + 1) if self.last_result == "WIN" else 1
            self.last_result = "WIN"
            self.total_profit_usd += profit
        else:
            self.losses += 1
            self.current_streak = (self.current_streak - 1) if self.last_result == "LOSS" else -1
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
        print("\n" + "="*85)
        print("📊 CANDLE BOT SUMMARY DASHBOARD")
        print("="*85)
        print(f"Windows processed : {self.total_windows:3d}   |   Wins: {self.wins:3d}   |   Losses: {self.losses:3d}   |   No fills: {self.no_fills:2d}")
        print(f"Flips triggered   : {self.flips:3d}   |   Win rate: {win_rate:5.1f}%   |   Est. P&L: ${self.total_profit_usd:7.2f}")
        print(f"Current streak    : {self.current_streak:3d}   |   Max streak: {self.max_streak:3d}   |   Last: {self.last_result or 'N/A'}")
        print(f"TP price: {TAKE_PROFIT_PRICE}   |   DRY_RUN: {DRY_RUN}")
        print("="*85 + "\n")


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
        print("[client] ✅ Connected successfully")

    # (All other methods: _parse_token_ids, fetch_market_for_slot, find_next_window_market,
    #  is_accepting_orders, place_limit_buy, place_limit_sell, cancel_order, get_filled_size,
    #  get_previous_candle_color) — copy them exactly from the previous full script I gave you.

    # For brevity here, assume they are pasted in. If you need the full client again, let me know.

# ── Main Functions (place_orders + monitor) — use the versions from my previous message ──

# ... paste place_candle_based_orders and monitor_window here from the earlier complete script ...

# ── Main Loop (FIXED with proper global) ──────────────────────────────────────
current_window: Optional[WindowState] = None   # ← Global declaration at module level

client = PolymarketClient()

async def main():
    global current_window   # ← This line is critical!
    print("🤖 Candle-based 5m BTC Bot with Dashboard started\n")
    stats.print_dashboard()

    while True:
        try:
            if current_window is None or current_window.resolved:
                current_window = await place_candle_based_orders(client)

            if current_window and not current_window.resolved:
                await monitor_window(client, current_window)

            await asyncio.sleep(5)
        except Exception as e:
            print(f"[main loop] Error: {e}")
            await asyncio.sleep(10)


if __name__ == "__main__":
    asyncio.run(main())
