import os
import time
import asyncio
import json
import websockets
import datetime
import urllib.request
import urllib.parse
import base64
import uuid

from dotenv import load_dotenv
from stream import MarketStream
from model import LogisticRegressionModel
from strategy import LogisticTakerStrat

# Reuse the PROVEN order/lifecycle functions (place, settle, log, guards, lookup)
import trading_bot as tb

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.serialization import load_pem_private_key
from cryptography.exceptions import InvalidSignature

# ============================================================
# SECTION 1 — BOOT & KEY LOADING
# ============================================================

script_dir = os.path.dirname(os.path.abspath(__file__))
env_path = os.path.join(script_dir, "keys.env")

load_dotenv(dotenv_path=env_path, override=True)
API_KEY_ID = os.getenv("KALSHI_API_KEY_ID")

PRIVATE_KEY_PATH = os.path.join(script_dir, "First.txt")

with open(PRIVATE_KEY_PATH, "rb") as key_file:
    private_key = load_pem_private_key(key_file.read(), password=None)
print("Key Engine Status: Success. Private key object initialized in RAM")

kalshi_stream = MarketStream(max_len=100)  # FIX (#2): 100 closed 15-min candles, matching research.py
model = LogisticRegressionModel()
strat = LogisticTakerStrat(
    coin="BTC",
    sz=1.0,
    model=model,
    stream=kalshi_stream,
    exchange=None
)
print("[MODEL] Logistic Regression Classifier initialized.")

# FIX (ticker log): remember the last ticker we subscribed to, so we can announce
# when Kalshi rolls over to a new 15-min market. None = nothing subscribed yet.
last_subscribed_ticker = None

# ============================================================
# SECTION 2 — CRYPTOGRAPHIC SIGNING
# ============================================================

def sign_payload(message_string: str) -> str:
    message_bytes = message_string.encode('utf-8')
    raw_signature = private_key.sign(
        message_bytes,
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.DIGEST_LENGTH   # 32 bytes for SHA256
        ),
        hashes.SHA256()
    )
    return base64.b64encode(raw_signature).decode('utf-8')

# ============================================================
# SECTION 3 — TICKER GENERATOR
# ============================================================

def get_current_kalshi_ticker():
    now = datetime.datetime.now(datetime.timezone.utc)
    minute_block = (now.minute // 15) * 15
    expiration_time = now.replace(minute=minute_block, second=0, microsecond=0) + datetime.timedelta(minutes=15)
    date_str = expiration_time.strftime("%y%b%d%H%M").upper()
    return f"KXBTC15M-{date_str}"

# ============================================================
# SECTION 4 — KALSHI WEBSOCKET CONNECTION
# ============================================================

async def connect_to_Kalshi():
    # FIX: Updated demo WebSocket URL — old domain returns 401 immediately
    url = "wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2"

    while True:
        try:
            # FIX: timestamp in milliseconds — confirmed in Kalshi official quick-start docs
            timestamp = str(int(time.time() * 1000))
            path = "/trade-api/ws/v2"

            # Payload: timestamp + HTTP method + path (no spaces, no query string)
            payload_string = timestamp + "GET" + path
            signature = sign_payload(payload_string)

            headers = {
                "KALSHI-ACCESS-KEY": API_KEY_ID,
                "KALSHI-ACCESS-SIGNATURE": signature,
                "KALSHI-ACCESS-TIMESTAMP": timestamp
            }

            async with websockets.connect(url, additional_headers=headers) as websocket:
                print("[NETWORK] Handshake Success: Connected to Kalshi Feed!")

                active_ticker = get_current_kalshi_ticker()

                # FIX (ticker log): announce only when the ticker actually changes,
                # so a reconnect to the SAME market doesn't falsely claim a new one.
                global last_subscribed_ticker
                if active_ticker != last_subscribed_ticker:
                    print(f"[NETWORK] Connected to new ticker on Kalshi: {active_ticker}")
                    last_subscribed_ticker = active_ticker
                else:
                    print(f"[NETWORK] Re-subscribing to current ticker: {active_ticker}")

                subscribe_msg = {
                    "id": 1,
                    "cmd": "subscribe",
                    "params": {
                        "channels": ["orderbook_delta"],   # FIX: singular 'delta' not 'deltas'
                        "market_tickers": [active_ticker]
                    }
                }
                await websocket.send(json.dumps(subscribe_msg))

                async for message in websocket:
                    data = json.loads(message)
                    kalshi_stream.process_message(data)  # FIX (#1): updates contract QUOTE only, not the feature buffer

        except websockets.exceptions.WebSocketException as e:
            print(f"[ERROR] Kalshi WebSocket error: {e}")
        except Exception as e:
            print(f"[ERROR] System Exception: {e}")

        print("[NETWORK] Reconnecting in 5 seconds...")
        await asyncio.sleep(5)

# ============================================================
# SECTION 5 — COINBASE DATA STREAM
# ============================================================

async def stream_btc_price():
    url = "wss://ws-feed.exchange.coinbase.com"
    subscribe_message = {
        "type": "subscribe",
        "product_ids": ["BTC-USD"],
        "channels": ["ticker"]
    }

    while True:
        try:
            async with websockets.connect(url) as websocket:
                await websocket.send(json.dumps(subscribe_message))
                print("[COINBASE] Connected to BTC price feed")

                async for message in websocket:
                    data = json.loads(message)
                    if data.get('type') == 'ticker':
                        price = float(data['price'])
                        kalshi_stream.ingest_price(price)  # FIX (#1): the ONLY source feeding the feature buffer
        except Exception as e:
            print(f"[COINBASE ERROR] {e}")
        await asyncio.sleep(5)

# ============================================================
# SECTION 5.5 — CANDLE WINDOW REFRESH (FIX #2)
# ============================================================
# Keeps the model's 100-candle window current by re-pulling from Coinbase
# every 15 minutes. Decision cadence is 15 min, so REST polling is plenty fast.

async def refresh_candle_window():
    while True:
        await asyncio.sleep(900)  # 15 minutes
        ok = kalshi_stream.refresh_candles()
        if ok:
            print("[STREAM] Candle window refreshed with latest closed 15-min candle.")

# ============================================================
# SECTION 6 — EXECUTION LOOP (MODEL-DRIVEN, REAL ORDERS, async)
# ============================================================
# The model picks the DIRECTION each window; we place a real demo order on that
# side at a fixed price. Async so it never blocks the websocket feeds. Reuses the
# proven place/settle/guard functions from trading_bot.py.
#
#   model prob > up_threshold   -> BET_UP   -> buy YES (side 'bid')
#   model prob < down_threshold -> BET_DOWN -> buy NO  (side 'ask')
#   in between                  -> HOLD     -> place nothing this window
#
# Price is FIXED (demo; cost doesn't matter). The model only drives direction.

FIXED_PRICE = 0.50   # cents we bid, regardless of side (demo placeholder)

async def execution_loop():
    print("[ORCHESTRATOR] MODEL-DRIVEN trade loop started (demo).")
    active_ticker = None
    windows_done = 0
    stopping = False

    while True:
        try:
            if tb.stop_requested() and not stopping:
                stopping = True
                print("\n[STOP] STOP file found. Finishing current position, then halting.\n")

            tkr = tb.current_ticker()   # look up real ticker (never construct)

            # window rolled over -> settle the finished one
            if active_ticker and tkr != active_ticker:
                print(f"\n[ROLL] Window {active_ticker} closed (new open: {tkr}).")
                tb.settle_window(active_ticker)
                active_ticker = None
                if stopping:
                    print("[STOP] Position finished and logged. Halted.")
                    break
                if windows_done >= tb.MAX_WINDOWS:
                    print(f"[BACKSTOP] Reached MAX_WINDOWS ({tb.MAX_WINDOWS}). Halted.")
                    break

            # decide + place for a new window
            if active_ticker is None and not stopping:
                if tkr is None:
                    print("[WAIT] No open market yet...")
                elif len(kalshi_stream.prices) < kalshi_stream.max_len:
                    print("   -> [BRAIN] Waiting for candle window to fill...")
                else:
                    # ── ask the MODEL for a direction ──
                    features = kalshi_stream.get_features()
                    btc_price = kalshi_stream.prices[-1]
                    replay = strat.on_tick(btc_price)   # runs model.predict via strategy
                    direction = replay.direction if replay else None

                    if direction == "BET_UP":
                        side, human = "bid", "YES (model: UP)"
                    elif direction == "BET_DOWN":
                        side, human = "ask", "NO (model: DOWN)"
                    else:
                        side, human = None, None

                    if side is None:
                        prob = f"{replay.prob_up:.2%}" if replay else "N/A"
                        print(f"   -> [SIGNAL] HOLD (prob {prob}) — no trade this window.")
                        active_ticker = tkr   # mark as handled so we don't re-decide every poll
                    else:
                        print(f"   -> [SIGNAL] {direction} | prob {replay.prob_up:.2%} -> bet {human}")
                        # guard inside place_bet prevents double-placing across restarts;
                        # but place_bet defaults to YES, so place directly here with chosen side:
                        if tb.already_in_window(tkr):
                            print(f"[GUARD] {tkr}: already in this window — not placing.")
                        else:
                            resp = tb.place_order(tkr, side, tb.BET_CONTRACTS, FIXED_PRICE)
                            oid = resp.get("order_id")
                            print(f"[PLACE] {tkr}: {human} @ ${FIXED_PRICE:.2f} -> "
                                  f"order_id={oid} filled={resp.get('fill_count')} "
                                  f"resting={resp.get('remaining_count')}")
                            windows_done += 1
                            print(f"[INFO] Windows traded: {windows_done}/{tb.MAX_WINDOWS}")
                        active_ticker = tkr

            if active_ticker:
                tb.report_status(active_ticker)

            if stopping and active_ticker is None:
                print("[STOP] No active position. Halted.")
                break

        except Exception as e:
            print(f"   -> [TRADE LOOP ERROR] {e}")

        await asyncio.sleep(tb.POLL_SECONDS)   # async — does NOT freeze the feeds

    print("[ORCHESTRATOR] Trade loop ended. History in trade_log.csv")

# ============================================================
# SECTION 7 — ENTRY POINT
# ============================================================

async def main_manager():
    print("Booting Kalshi Algorithmic Trading Bot...")

    # FIX (#2): fill the 100-candle window before anything else, so the bot
    # can predict immediately instead of waiting ~25 hours for live candles.
    if not kalshi_stream.backfill_candles():
        print("[ORCHESTRATOR] WARNING: backfill incomplete — model will HOLD until window fills.")

    await asyncio.gather(
        connect_to_Kalshi(),
        stream_btc_price(),
        refresh_candle_window(),   # FIX (#2): keep the window current
        execution_loop()
    )

if __name__ == "__main__":
    asyncio.run(main_manager())