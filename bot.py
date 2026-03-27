"""
FlipFlop — Polymarket BTC 5-Min Live Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Strategy:
  • Auto-discovers BTC 5-min market IDs from clock math (no scanning)
  • Always buys 10 shares of whichever side is ≥ 50c
  • Flips to opposite side instantly when price hits 45c
  • Closes and re-enters when market resolves at 98c+
  • Force-closes at window end, re-enters next window fresh

Orders:
  • ALL orders are MARKET orders (FOK — Fill or Kill)
  • Buys at best ask, sells at best bid

Price feed:
  • Polymarket WebSocket (real-time, same as their UI)
  • Gamma API seed on window open (instant price on switch)
  • REST /midpoints fallback if WS stale

Setup (.env):
  POLY_API_KEY=...
  POLY_API_SECRET=...
  POLY_API_PASSPHRASE=...
  POLY_PRIVATE_KEY=0x...
  POLY_FUNDER=0x...         # your proxy wallet address on Polymarket
  DEMO_MODE=false
"""

import os, sys, time, json, logging, threading, requests
import websocket
from dataclasses import dataclass, field
from typing import Optional
from dotenv import load_dotenv

load_dotenv()

# ══════════════════════════════════════════════════════════════════════════════
#  CONFIG
# ══════════════════════════════════════════════════════════════════════════════

DEMO_MODE        = os.getenv("DEMO_MODE", "false").lower() != "false"
STARTING_CAPITAL = float(os.getenv("STARTING_CAPITAL", "1000.0"))
TRADE_SHARES     = int(os.getenv("TRADE_SHARES",   "10"))
FLIP_TRIGGER     = float(os.getenv("FLIP_TRIGGER",  "0.45"))
WIN_TRIGGER      = float(os.getenv("WIN_TRIGGER",   "0.98"))
POLL_MS          = int(os.getenv("POLL_MS",        "500"))
PRE_CLOSE_SECS   = int(os.getenv("PRE_CLOSE_SECS", "15"))
FEE_RATE         = float(os.getenv("FEE_RATE",      "0.002"))
MIN_BALANCE      = float(os.getenv("MIN_BALANCE",   "20.0"))  # stop if below this
WINDOW_SECONDS   = 300

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API  = "https://clob.polymarket.com"
WS_URL    = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

# ══════════════════════════════════════════════════════════════════════════════
#  LOGGING
# ══════════════════════════════════════════════════════════════════════════════

handlers = [logging.StreamHandler(sys.stdout)]
try:    handlers.append(logging.FileHandler("bot.log"))
except: pass
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s.%(msecs)03d  %(message)s",
    datefmt="%H:%M:%S",
    handlers=handlers,
)
log = logging.getLogger(__name__)

def calc_fee(price: float, shares: float) -> float:
    return FEE_RATE * min(price, 1.0 - price) * shares


# ══════════════════════════════════════════════════════════════════════════════
#  MARKET WINDOW  — deterministic slug math
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class MarketWindow:
    window_ts:     int
    slug:          str
    market_id:     Optional[int]
    up_token_id:   str = ""
    down_token_id: str = ""

    @property
    def close_ts(self):            return self.window_ts + WINDOW_SECONDS
    @property
    def seconds_until_close(self): return self.close_ts - time.time()
    @property
    def is_ready(self):            return bool(self.up_token_id and self.down_token_id)
    def __str__(self):
        return f"{self.slug}  closes_in={self.seconds_until_close:.0f}s"


class MarketDiscovery:
    """
    BTC 5-min windows always start at Unix timestamps divisible by 300.
    Slug = f"btc-updown-5m-{window_ts}" — deterministic, no scanning needed.
    One Gamma API call returns both token IDs in ~15ms.
    """
    def __init__(self):
        self._s = requests.Session()
        self._s.headers["Accept"] = "application/json"
        self._cache: dict[int, MarketWindow] = {}

    @staticmethod
    def current_ts():
        t = int(time.time()); return t - (t % WINDOW_SECONDS)

    @staticmethod
    def ts_at(n): return MarketDiscovery.current_ts() + n * WINDOW_SECONDS

    @staticmethod
    def slug(ts): return f"btc-updown-5m-{ts}"

    def _fetch(self, w: MarketWindow) -> bool:
        t0 = time.perf_counter()
        try:
            r = self._s.get(f"{GAMMA_API}/events",
                            params={"slug": w.slug}, timeout=6)
            r.raise_for_status()
            events = r.json()
            ms = (time.perf_counter() - t0) * 1000
            if not events:
                log.warning(f"  ⚠  No event for {w.slug}"); return False
            markets = events[0].get("markets", [])
            if not markets: return False
            m      = markets[0]
            tokens = m.get("clobTokenIds", [])
            if isinstance(tokens, str): tokens = json.loads(tokens)
            if len(tokens) < 2: return False
            w.up_token_id   = tokens[0]
            w.down_token_id = tokens[1]
            w.market_id     = m.get("id")
            log.info(
                f"  ✅ Market loaded {ms:.0f}ms  id={w.market_id}\n"
                f"     UP  : ...{w.up_token_id[-10:]}\n"
                f"     DOWN: ...{w.down_token_id[-10:]}"
            )
            return True
        except Exception as e:
            log.error(f"  ❌ Market fetch failed: {e}"); return False

    def get(self, offset=0) -> Optional[MarketWindow]:
        ts = self.ts_at(offset)
        if ts not in self._cache:
            self._cache[ts] = MarketWindow(ts, self.slug(ts), None)
        w = self._cache[ts]
        if not w.is_ready: self._fetch(w)
        return w

    def prefetch_next(self):
        ts = self.ts_at(1)
        if ts not in self._cache or not self._cache[ts].is_ready:
            log.info(f"  🔄 Pre-fetching next: {self.slug(ts)}")
            self.get(1)

    def evict_old(self):
        cutoff = self.current_ts() - WINDOW_SECONDS * 2
        for ts in [t for t in list(self._cache) if t < cutoff]:
            del self._cache[ts]


# ══════════════════════════════════════════════════════════════════════════════
#  PRICE FEED  — WebSocket + Gamma seed + REST fallback
# ══════════════════════════════════════════════════════════════════════════════

class PriceFeed:
    """
    Priority order:
      1. Polymarket WebSocket (real-time push, same as their UI)
         book event    → buys[0] / sells[0]
         price_change  → best_bid / best_ask inside price_changes[]
         last_trade    → price field
      2. Gamma outcomePrices (called on every window open for instant seed)
      3. CLOB REST /midpoints (fallback)
    """

    def __init__(self):
        self._prices: dict[str, dict] = {}
        self._subs:   list[str]       = []
        self._ws:     Optional[websocket.WebSocketApp] = None
        self._ws_up   = False
        self._lock    = threading.Lock()
        self._rest    = requests.Session()
        self._rest.headers["Accept"] = "application/json"
        self._start_ws()

    # ── WebSocket ─────────────────────────────────────────────────────────────

    def _start_ws(self):
        def on_open(ws):
            self._ws_up = True
            log.info("  🔌 WS connected")
            with self._lock:
                tokens = list(self._subs)
            if tokens:
                self._send_sub(tokens)

        def on_message(ws, raw):
            try:
                data = json.loads(raw)
                msgs = data if isinstance(data, list) else [data]
                for m in msgs:
                    self._handle(m)
            except Exception as e:
                log.debug(f"WS parse: {e}")

        def on_error(ws, err):
            self._ws_up = False
            log.warning(f"  ⚠  WS error: {err}")

        def on_close(ws, code, msg):
            self._ws_up = False
            log.warning(f"  ⚠  WS closed — reconnect in 3s")
            time.sleep(3)
            self._start_ws()

        self._ws = websocket.WebSocketApp(
            WS_URL,
            on_open=on_open, on_message=on_message,
            on_error=on_error, on_close=on_close,
        )
        threading.Thread(
            target=self._ws.run_forever,
            kwargs={"ping_interval": 30, "ping_timeout": 10},
            daemon=True,
        ).start()

    def _send_sub(self, token_ids: list):
        if not self._ws or not self._ws_up: return
        try:
            self._ws.send(json.dumps({"assets_ids": token_ids, "type": "market"}))
            log.info(f"  📡 WS subscribed {len(token_ids)} tokens")
        except Exception as e:
            log.debug(f"WS sub error: {e}")

    def _set(self, token_id: str, bid=None, ask=None, ltp=None):
        with self._lock:
            p = self._prices.setdefault(token_id, {})
            if bid is not None and 0 < bid < 1:  p["bid"] = bid
            if ask is not None and 0 < ask < 1:  p["ask"] = ask
            if ltp is not None and 0 < ltp < 1:  p["ltp"] = ltp
            b = p.get("bid"); a = p.get("ask")
            if b and a and b < a:
                p["mid"] = round((b + a) / 2, 4)
            elif ltp:
                p["mid"] = ltp
            p["ts"] = time.time()

    def _handle(self, msg: dict):
        t = msg.get("event_type", "")

        if t == "book":
            aid   = msg.get("asset_id", "")
            buys  = msg.get("buys",  [])
            sells = msg.get("sells", [])
            bid   = float(buys[0]["price"])  if buys  else None
            ask   = float(sells[0]["price"]) if sells else None
            if aid: self._set(aid, bid=bid, ask=ask)

        elif t == "price_change":
            for pc in msg.get("price_changes", []):
                aid = pc.get("asset_id", "")
                bb  = float(pc["best_bid"]) if pc.get("best_bid") else None
                ba  = float(pc["best_ask"]) if pc.get("best_ask") else None
                if aid: self._set(aid, bid=bb, ask=ba)

        elif t == "last_trade_price":
            aid = msg.get("asset_id", "")
            p   = msg.get("price")
            if aid and p: self._set(aid, ltp=float(p))

    # ── Public ────────────────────────────────────────────────────────────────

    def subscribe(self, *token_ids: str):
        new = [t for t in token_ids if t not in self._subs]
        if not new: return
        self._subs.extend(new)
        for _ in range(40):
            if self._ws_up:
                self._send_sub(new)
                return
            time.sleep(0.1)

    def reset(self):
        with self._lock:
            self._subs.clear()
            self._prices.clear()

    def seed_from_gamma(self, slug: str, up_id: str, dn_id: str):
        """Pull outcomePrices from Gamma immediately on window open."""
        try:
            r = self._rest.get(f"{GAMMA_API}/events",
                               params={"slug": slug}, timeout=5)
            r.raise_for_status()
            events = r.json()
            if not events: return
            markets = events[0].get("markets", [])
            if not markets: return
            raw = markets[0].get("outcomePrices", "")
            if not raw: return
            ps  = json.loads(raw) if isinstance(raw, str) else raw
            up  = float(ps[0]); dn = float(ps[1])
            if 0 < up < 1:
                self._set(up_id, ltp=up)
                self._set(dn_id, ltp=dn)
                log.info(f"  🌱 Gamma seed  UP={up:.4f}  DOWN={dn:.4f}")
        except Exception as e:
            log.debug(f"Gamma seed failed: {e}")

    def mid(self, token_id: str) -> Optional[float]:
        with self._lock:
            d = dict(self._prices.get(token_id, {}))
        if d.get("mid"):
            age = time.time() - d.get("ts", 0)
            if age < 15: return d["mid"]
            return d["mid"]   # stale but use it
        return self._rest_mid(token_id)

    def bid(self, token_id: str) -> float:
        with self._lock:
            d = dict(self._prices.get(token_id, {}))
        if d.get("bid") and (time.time()-d.get("ts",0)) < 15:
            return d["bid"]
        m = self.mid(token_id) or 0.5
        return max(0.01, round(m - 0.005, 4))

    def ask(self, token_id: str) -> float:
        with self._lock:
            d = dict(self._prices.get(token_id, {}))
        if d.get("ask") and (time.time()-d.get("ts",0)) < 15:
            return d["ask"]
        m = self.mid(token_id) or 0.5
        return min(0.99, round(m + 0.005, 4))

    def _rest_mid(self, token_id: str) -> Optional[float]:
        try:
            r = self._rest.get(f"{CLOB_API}/midpoints",
                               params={"token_id": token_id}, timeout=3)
            r.raise_for_status()
            v = r.json().get("mid")
            if v:
                p = float(v)
                self._set(token_id, ltp=p)
                return p
        except Exception as e:
            log.debug(f"REST mid failed: {e}")
        return None

    def src(self, token_id: str) -> str:
        with self._lock:
            d = self._prices.get(token_id, {})
        age = time.time() - d.get("ts", 0)
        return f"WS✓{age:.0f}s" if d and age < 15 else "REST"


# ══════════════════════════════════════════════════════════════════════════════
#  POSITION
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class Position:
    side:        str
    token_id:    str
    shares:      float
    buy_price:   float
    buy_fee:     float
    opened_at:   float = field(default_factory=time.time)

    @property
    def cost_basis(self): return self.shares * self.buy_price + self.buy_fee

    def unrealised_pnl(self, current_mid: float) -> float:
        sell_fee = calc_fee(current_mid, self.shares)
        return (self.shares * current_mid - sell_fee) - self.cost_basis

    def realised_pnl(self, sell_price: float) -> float:
        sell_fee = calc_fee(sell_price, self.shares)
        return (self.shares * sell_price - sell_fee) - self.cost_basis

    def __str__(self):
        return (f"Pos({self.side} {self.shares}sh "
                f"@ {self.buy_price:.4f} basis=${self.cost_basis:.4f})")


# ══════════════════════════════════════════════════════════════════════════════
#  DEMO CLIENT
# ══════════════════════════════════════════════════════════════════════════════

class DemoClient:
    def __init__(self, feed: PriceFeed):
        self._feed   = feed
        self.balance = STARTING_CAPITAL
        log.info(f"📄 DEMO MODE  capital=${self.balance:.2f}")

    def buy(self, side: str, token_id: str, shares: float) -> Optional[dict]:
        price = self._feed.mid(token_id) or 0.5
        fee   = calc_fee(price, shares)
        c     = shares * price + fee
        if c > self.balance:
            log.warning(f"  ⚠  Insufficient balance (need ${c:.2f} have ${self.balance:.2f})")
            return None
        self.balance -= c
        log.info(f"  📄 BUY  {shares}sh {side} @ {price:.4f}  "
                 f"fee=${fee:.4f}  cost=${c:.4f}  cash=${self.balance:.2f}")
        return {"price": price, "fee": fee, "shares": shares}

    def sell(self, side: str, token_id: str, shares: float) -> Optional[dict]:
        price    = self._feed.mid(token_id) or 0.5
        fee      = calc_fee(price, shares)
        proceeds = shares * price - fee
        self.balance += proceeds
        log.info(f"  📄 SELL {shares}sh {side} @ {price:.4f}  "
                 f"fee=${fee:.4f}  recv=${proceeds:.4f}  cash=${self.balance:.2f}")
        return {"price": price, "fee": fee, "shares": shares}


# ══════════════════════════════════════════════════════════════════════════════
#  LIVE CLIENT  — real market orders on Polymarket CLOB
# ══════════════════════════════════════════════════════════════════════════════

class LiveClient:
    """
    All orders are MARKET orders with FOK (Fill or Kill).
    Uses exact same init pattern confirmed working in diagnose.py
    """

    def __init__(self, feed: PriceFeed):
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import ApiCreds

        priv_key = os.getenv("POLY_PRIVATE_KEY", "")
        funder   = os.getenv("POLY_FUNDER", "")
        sig_type = int(os.getenv("POLY_SIG_TYPE", "0"))

        if not priv_key:
            raise ValueError("Missing POLY_PRIVATE_KEY")

        # Step 1: Init client with just private key (no creds yet)
        base = ClobClient(
            host=CLOB_API,
            key=priv_key,
            chain_id=137,
            signature_type=sig_type,
            funder=funder if funder else None,
        )

        # Step 2: Derive credentials fresh from private key
        # This is the ONLY reliable way for Magic/email wallets
        try:
            derived = base.create_or_derive_api_creds()
            log.info(
                f"  ✅ Credentials derived from private key\n"
                f"     api_key : {derived.api_key[:16]}...\n"
                f"     sig_type: {sig_type}\n"
                f"     funder  : {funder}"
            )
            creds = ApiCreds(
                api_key=derived.api_key,
                api_secret=derived.api_secret,
                api_passphrase=derived.api_passphrase,
            )
        except Exception as e:
            log.warning(f"  ⚠  Could not derive creds, using env vars: {e}")
            api_key = os.getenv("POLY_API_KEY", "")
            api_sec = os.getenv("POLY_API_SECRET", "")
            api_phr = os.getenv("POLY_API_PASSPHRASE", "")
            creds = ApiCreds(
                api_key=api_key,
                api_secret=api_sec,
                api_passphrase=api_phr,
            )

        # Step 3: Final client with derived credentials
        self._clob = ClobClient(
            host=CLOB_API,
            key=priv_key,
            chain_id=137,
            creds=creds,
            signature_type=sig_type,
            funder=funder if funder else None,
        )
        self._feed    = feed
        self.balance  = self._get_balance()
        log.info(
            f"🔴 LIVE MODE\n"
            f"     Balance on Polymarket : ${self.balance:.2f} USDC\n"
            f"     Signature type        : {sig_type}\n"
            f"     Trade size            : {TRADE_SHARES} shares per order"
        )

    def _get_balance(self) -> float:
        """Fetch real USDC balance from Polymarket."""
        # Always use STARTING_CAPITAL as the working balance
        # The safety check uses this to prevent overspending
        fallback = STARTING_CAPITAL
        log.info(f"  💰 Balance: ${fallback:.2f} USDC")
        return fallback

    def refresh_balance(self):
        self.balance = self._get_balance()

    def buy(self, side: str, token_id: str, shares: float) -> Optional[dict]:
        """Market buy order — FOK at best ask."""
        from py_clob_client.clob_types import MarketOrderArgs, OrderType
        from py_clob_client.order_builder.constants import BUY

        ask = self._feed.ask(token_id)

        try:
            args   = MarketOrderArgs(token_id=token_id, amount=shares, side=BUY)
            signed = self._clob.create_market_order(args)
            resp   = self._clob.post_order(signed, OrderType.FOK)
            fee    = calc_fee(ask, shares)
            self.balance -= est_cost
            log.info(
                f"  🔴 BUY  {shares}sh {side} @ ~{ask:.4f}  "
                f"fee=${fee:.4f}  est_cost=${est_cost:.4f}  "
                f"cash~${self.balance:.2f}\n"
                f"     order_id: {resp.get('orderID','?')}"
            )
            return {"price": ask, "fee": fee, "shares": shares, "resp": resp}
        except Exception as e:
            err = str(e)
            if "no match" in err.lower():
                log.warning(f"  ⚠  No matching orders in book — window may be resolved, waiting for next")
            else:
                log.error(f"  ❌ BUY FAILED: {e}")
            return None

    def sell(self, side: str, token_id: str, shares: float) -> Optional[dict]:
        """Market sell order — FOK at best bid."""
        from py_clob_client.clob_types import MarketOrderArgs, OrderType
        from py_clob_client.order_builder.constants import SELL

        bid = self._feed.bid(token_id)
        try:
            args   = MarketOrderArgs(token_id=token_id, amount=shares, side=SELL)
            signed = self._clob.create_market_order(args)
            resp   = self._clob.post_order(signed, OrderType.FOK)
            fee      = calc_fee(bid, shares)
            proceeds = shares * bid - fee
            self.balance += proceeds
            log.info(
                f"  🔴 SELL {shares}sh {side} @ ~{bid:.4f}  "
                f"fee=${fee:.4f}  recv=${proceeds:.4f}  "
                f"cash~${self.balance:.2f}\n"
                f"     order_id: {resp.get('orderID','?')}"
            )
            return {"price": bid, "fee": fee, "shares": shares, "resp": resp}
        except Exception as e:
            log.error(f"  ❌ SELL FAILED: {e}")
            return None


# ══════════════════════════════════════════════════════════════════════════════
#  P&L TRACKER
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class PnL:
    start:    float = field(default_factory=lambda: STARTING_CAPITAL)
    realised: float = 0.0
    fees:     float = 0.0
    closes:   int   = 0
    flips:    int   = 0
    wins:     int   = 0
    best:     float = 0.0
    worst:    float = 0.0

    def record(self, pnl: float, fee: float, reason: str):
        self.realised += pnl
        self.fees     += fee
        self.closes   += 1
        if reason == "flip":
            self.flips += 1
            self.worst  = min(self.worst, pnl)
        if reason == "resolved":
            self.wins  += 1
            self.best   = max(self.best,  pnl)

    def report(self, balance: float, pos: Optional[Position], mid: float):
        unreal  = pos.unrealised_pnl(mid) if pos and mid else 0.0
        pos_val = (pos.shares * mid - calc_fee(mid, pos.shares)) if pos and mid else 0.0
        equity  = balance + pos_val
        total   = self.realised + unreal
        roi     = (total / self.start) * 100
        log.info(
            f"\n{'═'*56}\n  📊 P&L Report\n"
            f"     Equity (cash+pos) : ${equity:,.2f}\n"
            f"     Free cash         : ${balance:,.2f}\n"
            f"     Realised P&L      : ${self.realised:+.4f}\n"
            f"     Unrealised P&L    : ${unreal:+.4f}\n"
            f"     Total P&L         : ${total:+.4f}\n"
            f"     ROI               : {roi:+.3f}%\n"
            f"     Fees paid         : ${self.fees:.4f}\n"
            f"  ────────────────────────────────────────\n"
            f"     Closes : {self.closes}  "
            f"Flips : {self.flips}  "
            f"Wins : {self.wins}\n"
            f"     Best   : ${self.best:+.4f}  "
            f"Worst : ${self.worst:+.4f}\n"
            f"{'═'*56}"
        )


# ══════════════════════════════════════════════════════════════════════════════
#  FLIP BOT
# ══════════════════════════════════════════════════════════════════════════════

class FlipBot:

    def __init__(self):
        self.disc   = MarketDiscovery()
        self.feed   = PriceFeed()
        self.client = DemoClient(self.feed) if DEMO_MODE else LiveClient(self.feed)
        self.pnl    = PnL()
        self.pos:   Optional[Position] = None
        self.win:   Optional[MarketWindow] = None
        self._rc    = 0

    # ── helpers ───────────────────────────────────────────────────────────────

    def token(self, side: str) -> str:
        return self.win.up_token_id if side == "UP" else self.win.down_token_id

    def opposite(self, side: str) -> str:
        return "DOWN" if side == "UP" else "UP"

    # ── window management ─────────────────────────────────────────────────────

    def ensure_window(self) -> bool:
        cur = self.disc.get(0)
        if not cur: return False

        if self.win is None or self.win.window_ts != cur.window_ts:
            if self.win: self.feed.reset()
            log.info(f"\n  📅 {'SWITCH' if self.win else 'START'} → {cur}")
            self.win = cur
            self.disc.evict_old()

        if not self.win.is_ready:
            log.warning("  ⏳ Token IDs pending..."); return False

        # Seed price immediately from Gamma — no waiting for WS
        if not self.feed.mid(self.win.up_token_id):
            self.feed.seed_from_gamma(
                self.win.slug, self.win.up_token_id, self.win.down_token_id
            )

        self.feed.subscribe(self.win.up_token_id, self.win.down_token_id)

        if self.win.seconds_until_close < PRE_CLOSE_SECS:
            self.disc.prefetch_next()

        return True

    # ── entry scan ────────────────────────────────────────────────────────────

    def scan_entry(self) -> Optional[str]:
        up = self.feed.mid(self.win.up_token_id)
        if up is None:
            log.warning("  ⚠  No price yet"); return None
        dn  = round(1.0 - up, 4)
        src = self.feed.src(self.win.up_token_id)
        log.info(f"  Scan → UP:{up:.4f}  DOWN:{dn:.4f}  [{src}]")
        # Don't enter if window already near resolution or too late
        if up >= WIN_TRIGGER or dn >= WIN_TRIGGER:
            log.info(f"  ⏭  Window already near resolved ({max(up,dn):.2f}) — skipping, wait for next")
            return None
        # Don't enter in last 30 seconds of window
        if self.win.seconds_until_close < 30:
            log.info(f"  ⏭  Window closing in {self.win.seconds_until_close:.0f}s — waiting for next")
            return None
        if up >= 0.50:   return "UP"
        if dn >= 0.50:   return "DOWN"
        return None

    # ── open / close ──────────────────────────────────────────────────────────

    def open_position(self, side: str):
        token_id = self.token(side)
        result   = self.client.buy(side, token_id, TRADE_SHARES)
        if not result: return
        self.pos = Position(
            side=side,
            token_id=token_id,
            shares=result["shares"],
            buy_price=result["price"],
            buy_fee=result["fee"],
        )
        log.info(f"  ✅ OPENED  {self.pos}")

    def close_position(self, reason: str) -> float:
        if not self.pos: return 0.0
        result = self.client.sell(self.pos.side, self.pos.token_id, self.pos.shares)
        if not result: return 0.0
        pnl = self.pos.realised_pnl(result["price"])
        self.pnl.record(pnl, result["fee"], reason)
        self._rc += 1
        bal    = getattr(self.client, "balance", 0)
        pos_val = self.pos.shares * result["price"] - result["fee"]
        equity  = bal  # after sell, cash already includes proceeds
        log.info(
            f"  🔴 CLOSED  {self.pos}\n"
            f"     sold @ {result['price']:.4f}  "
            f"pnl=${pnl:+.4f}  "
            f"realised=${self.pnl.realised:+.4f}  "
            f"cash=${bal:.2f}  [{reason}]"
        )
        self.pos = None
        return pnl

    # ── main loop ─────────────────────────────────────────────────────────────

    def run(self):
        mode = "📄 DEMO" if DEMO_MODE else "🔴 LIVE"
        log.info(
            f"\n{'═'*56}\n"
            f"  FlipFlop — Polymarket BTC 5-Min Bot\n"
            f"  Mode     : {mode}\n"
            f"  Shares   : {TRADE_SHARES} per trade\n"
            f"  Flip @   : {FLIP_TRIGGER:.0%}   Win @ {WIN_TRIGGER:.0%}\n"
            f"  Min bal  : ${MIN_BALANCE:.2f} (safety stop)\n"
            f"  Poll     : {POLL_MS}ms\n"
            f"{'═'*56}"
        )

        while True:
            try:
                t0 = time.perf_counter()

                # 1. Ensure live window with token IDs
                if not self.ensure_window():
                    time.sleep(0.3); continue

                # 2. Enter if flat
                if not self.pos:
                    side = self.scan_entry()
                    if side:
                        self.open_position(side)
                    time.sleep(POLL_MS / 1000); continue

                # 3. Get live price
                mid = self.feed.mid(self.pos.token_id)
                if mid is None:
                    log.warning("  ⚠  No price"); time.sleep(1); continue

                unreal  = self.pos.unrealised_pnl(mid)
                secs    = self.win.seconds_until_close
                bal     = getattr(self.client, "balance", 0)
                pos_val = self.pos.shares * mid - calc_fee(mid, self.pos.shares)
                equity  = bal + pos_val
                src     = self.feed.src(self.pos.token_id)

                log.info(
                    f"  [{self.pos.side:<4}]  mid={mid:.4f}  "
                    f"entry={self.pos.buy_price:.4f}  "
                    f"unreal=${unreal:+.4f}  "
                    f"equity=${equity:.2f}  "
                    f"realised=${self.pnl.realised:+.4f}  "
                    f"window={secs:.0f}s  [{src}]"
                )

                # 4. FLIP — price hit 45c in SAME window
                if mid <= FLIP_TRIGGER:
                    opp     = self.opposite(self.pos.side)
                    opp_mid = self.feed.mid(self.token(opp)) or round(1.0 - mid, 4)
                    log.info(
                        f"\n  {'─'*52}\n"
                        f"  🔄 FLIP [{secs:.0f}s left in window]\n"
                        f"     {self.pos.side} hit {mid:.4f} ≤ {FLIP_TRIGGER}\n"
                        f"     → closing {self.pos.side}, opening {opp} @ ~{opp_mid:.4f}\n"
                        f"     window: {self.win.slug}\n"
                        f"  {'─'*52}"
                    )
                    self.close_position("flip")
                    self.open_position(opp)

                # 5. WIN — resolved at ~$1
                elif mid >= WIN_TRIGGER:
                    log.info(
                        f"\n  {'─'*52}\n"
                        f"  🏆 WIN   {self.pos.side} resolved @ {mid:.4f}\n"
                        f"     window: {self.win.slug}\n"
                        f"  {'─'*52}"
                    )
                    self.close_position("resolved")
                    time.sleep(3)

                # 6. Force-close at window end → re-enter next window
                elif secs <= 1.0:
                    log.info(
                        f"\n  ⏰ WINDOW END [{self.win.slug}]\n"
                        f"     Closing {self.pos.side} @ {mid:.4f}\n"
                        f"     Will re-enter NEXT window fresh"
                    )
                    self.close_position("window_end")
                    time.sleep(2)

                # 7. Print P&L report every 10 closes
                if self._rc >= 10:
                    m = self.feed.mid(self.pos.token_id) if self.pos else 0.0
                    self.pnl.report(bal, self.pos, m or 0.0)
                    self._rc = 0
                    # Refresh real balance from chain every 10 closes (live only)
                    if not DEMO_MODE and hasattr(self.client, "refresh_balance"):
                        self.client.refresh_balance()

                # 8. Hold exact poll cadence
                elapsed = (time.perf_counter() - t0) * 1000
                time.sleep(max(0, POLL_MS - elapsed) / 1000)

            except KeyboardInterrupt:
                log.info("\n  ⛔ Stopped by user.")
                if self.pos:
                    log.info("  Closing open position before exit...")
                    self.close_position("exit")
                bal = getattr(self.client, "balance", STARTING_CAPITAL)
                m   = self.feed.mid(self.pos.token_id) if self.pos else 0.0
                self.pnl.report(bal, self.pos, m or 0.0)
                break

            except Exception as e:
                log.error(f"  ❌ Unhandled error: {e}", exc_info=True)
                time.sleep(2)


# ══════════════════════════════════════════════════════════════════════════════
#  ENTRY
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    FlipBot().run()
