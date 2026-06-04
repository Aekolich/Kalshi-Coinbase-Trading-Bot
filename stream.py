import numpy as np # Helps with speed and vectorization
from collections import deque # a "deck" is a high-performance list optimized for adding or removing items from both ends instantly.
import time # Used for time-based print throttling
import requests # FIX (#2): used to pull historical 15-min candles from Coinbase (same API research.py uses)

class MarketStream:
    # Using a constructor to start our object in a usable state
    def __init__(self, max_len=100):
        # FIX: renamed self.price -> self.prices so main.py and stream.py agree on the name
        # FIX (#2): self.prices now holds CLOSED 15-MINUTE CANDLE CLOSES (not raw ticks),
        # so the live window matches exactly what research.py trained on. max_len should be 100.
        self.prices = deque(maxlen=max_len) # Sliding window of 15-min BTC closes in USD
        self.max_len = max_len
        self._last_print = 0.0  # Timestamp of last terminal update — gate for 1 print/sec

        # FIX (#1): Kalshi contract quote stored SEPARATELY from the price buffer.
        # These are cents (0-100), not BTC spot, so they must never enter self.prices.
        self.kalshi_yes_ask = None
        self.kalshi_yes_bid = None
        self.kalshi_last_update = 0.0  # Timestamp of last Kalshi quote update

        # FIX (#2): latest live tick price, used ONLY for the cosmetic terminal feed.
        # This is decoupled from the model window — it feeds nothing into get_features().
        self.latest_tick = None

    # FIX (#2): ingest_price is now COSMETIC ONLY.
    # Coinbase ticks come here just so we can print a live "watch it work" feed.
    # They do NOT enter self.prices anymore — the model window is candles, set by backfill/refresh below.
    def ingest_price(self, price: float):
        if price > 0:
            self.latest_tick = price

            # Time-based print gate: only update terminal once per second
            # Better than % 10 because it's consistent regardless of tick speed
            now = time.time()
            if now - self._last_print >= 1.0:
                # FIX (#2): once the candle window is full, drop the "(Buffer: x/100)" text.
                if len(self.prices) < self.max_len:
                    print(f"[STREAM] BTC: ${price:,.2f} (Buffer: {len(self.prices)}/{self.max_len})")
                else:
                    print(f"[STREAM] BTC: ${price:,.2f}")
                self._last_print = now

    # FIX: Added process_message — called by connect_to_Kalshi() in main.py
    # Kalshi orderbook_delta messages carry a 'yes' ask price we can use as a proxy signal.
    # We extract it and feed it into the price buffer via ingest_price.
    # FIX (#1): Kalshi quotes now update kalshi_yes_ask/bid ONLY — they no longer
    # call ingest_price(), because mixing cents into a USD candle buffer corrupts every feature.
    def process_message(self, data: dict):
        try:
            msg_type = data.get("type")
            # Kalshi sends a 'subscribed' ack first — skip it silently
            if msg_type == "subscribed":
                return
            # orderbook_delta messages contain market_result with yes/no prices
            market_result = data.get("msg", {})
            yes_ask = market_result.get("yes_ask")
            yes_bid = market_result.get("yes_bid")

            updated = False
            if yes_ask is not None:
                self.kalshi_yes_ask = float(yes_ask)
                updated = True
            if yes_bid is not None:
                self.kalshi_yes_bid = float(yes_bid)
                updated = True

            if updated:
                self.kalshi_last_update = time.time()
                # NOTE: deliberately NOT calling self.ingest_price() here — that was the bug.
        except Exception as e:
            print(f"[STREAM] process_message error (non-fatal): {e}")

    # FIX (#1): accessor so execution code (later, #9) can read the contract quote
    # without ever touching the feature buffer.
    def get_kalshi_quote(self):
        return {
            "yes_ask": self.kalshi_yes_ask,
            "yes_bid": self.kalshi_yes_bid,
            "age_sec": (time.time() - self.kalshi_last_update) if self.kalshi_last_update else None,
        }

    # FIX (#2): pull the most recent closed 15-min candles from Coinbase and load them
    # into self.prices. Uses the SAME Advanced Trade API and FIFTEEN_MINUTE granularity
    # as research.py, so live features are built from identical data to training.
    # Returns True on success, False on failure (so main.py can decide whether to proceed).
    def _fetch_candles(self, symbol: str = "BTC-USD", count: int = None) -> list:
        count = count or self.max_len
        # FIX (#2b): request a few EXTRA candles. Coinbase often returns one fewer
        # closed candle than the span implies (the current forming candle isn't a
        # closed one), which left the window stuck at 99/100. Over-fetch, then trim
        # to the most recent `count` closes below.
        fetch_count = count + 5
        end = int(time.time())
        start = end - (fetch_count * 900)  # 900 seconds = 15 minutes per candle

        url = f"https://api.coinbase.com/api/v3/brokerage/market/products/{symbol}/candles"
        params = {
            "start": str(start),
            "end": str(end),
            "granularity": "FIFTEEN_MINUTE",
            "limit": fetch_count,
        }
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
        raw = resp.json().get("candles", [])

        # Coinbase returns newest-first; we want oldest -> newest closes.
        candles = sorted(raw, key=lambda c: int(c["start"]))
        closes = [float(c["close"]) for c in candles if float(c["close"]) > 0]
        return closes

    # FIX (#2): called ONCE on boot. Fills the window so the bot can predict immediately
    # instead of waiting ~25 hours for 100 live candles to accumulate.
    def backfill_candles(self, symbol: str = "BTC-USD") -> bool:
        try:
            closes = self._fetch_candles(symbol, count=self.max_len)
            self.prices.clear()
            for c in closes[-self.max_len:]:
                self.prices.append(c)
            print(f"[STREAM] Backfilled {len(self.prices)}/{self.max_len} closed 15-min candles.")
            return len(self.prices) >= self.max_len
        except Exception as e:
            print(f"[STREAM] backfill_candles error: {e}")
            return False

    # FIX (#2): called periodically (every ~15 min) by main.py to keep the window current.
    # Re-pulls recent candles and rebuilds the window so the newest closed candle is included.
    def refresh_candles(self, symbol: str = "BTC-USD") -> bool:
        try:
            closes = self._fetch_candles(symbol, count=self.max_len)
            if not closes:
                return False
            self.prices.clear()
            for c in closes[-self.max_len:]:
                self.prices.append(c)
            return True
        except Exception as e:
            print(f"[STREAM] refresh_candles error (non-fatal): {e}")
            return False

    # Use Numpy to calculate indicators
    def get_features(self):
        # Change this to 'if len(self.prices) < 5:' temporarily if you want to bypass
        # waiting for 100 entries during immediate live code testing
        if len(self.prices) < self.max_len:
            return None

        arr = np.array(self.prices)

        # 1. Simple Moving Average (The Trend Context)
        sma = np.mean(arr)

        # 2. Log Returns (The Velocity)
        # We calculate the natural log of the ratio of the last price to the first price
        # in our current window to see the "percentage-like" change
        # Added a safety check to prevent dividing by zero or passing invalid math values
        if arr[0] > 0 and arr[-1] > 0:
            log_returns = np.log(arr[-1] / arr[0])
        else:
            log_returns = 0.0

        # 3. Volatility (The Risk/Jumpy-ness)
        volatility = np.std(arr)

        # FIX (#4): RSI — Relative Strength Index over the window.
        diffs = np.diff(arr)
        gains = diffs[diffs > 0].sum()
        losses = -diffs[diffs < 0].sum()
        if losses == 0:
            rsi = 100.0
        elif gains == 0:
            rsi = 0.0
        else:
            rs = gains / losses
            rsi = 100.0 - (100.0 / (1.0 + rs))

        # FIX (#4): SMA ratio — short(20) SMA / long(100) SMA.
        short_window = 20
        short_sma = np.mean(arr[-short_window:])
        if sma > 0:
            sma_ratio = short_sma / sma
        else:
            sma_ratio = 1.0

        return {
            "sma": sma,
            "log_returns": log_returns,
            "volatility": volatility,
            "rsi": rsi,
            "sma_ratio": sma_ratio,
        }