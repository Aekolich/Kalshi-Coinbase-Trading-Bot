"""
trading_bot.py — ALL-IN-ONE Kalshi DEMO trading engine.

Contains everything in one file:
  - signed HTTP + all API verbs (place, cancel, read orders/fills/positions, market)
  - settlement -> win/loss + CSV logging (settle_from_fills)
  - the continuous trade loop (place -> watch -> settle -> next ticker -> repeat)

GRACEFUL STOP (no Ctrl+C): create an empty file named STOP in this folder
(PowerShell: echo. > STOP). The loop finishes the current position, logs it, exits.

Run:  python trading_bot.py
"""

"""
kalshi_client.py — Kalshi DEMO order client + trade logger (#9 infrastructure)

The API verbs the bot needs to manage an order's full life:
  - place_order       : submit an order (proven working)
  - cancel_order      : pull a resting order off the book
  - get_open_orders   : what orders do I have resting right now?
  - get_fills         : which of my orders executed, at what price?
  - get_positions     : what am I holding right now?
  - get_market        : market details incl. settlement result (YES/NO) after expiry
  - settle_report     : after expiry, compute won/lost + payout and log to CSV

SAFETY:
  - DEMO host only. Demo keys can't touch production.
  - This module does NOT place anything on import. You call the functions.
  - Field names in responses are handled defensively; verify against real output
    the first time you run each read (paste output and we lock the fields).

NOTE on tickers: ALWAYS look markets up (get_open_markets) — never construct a
ticker string. Constructing it caused market_not_found earlier.
"""

import os
import csv
import time
import json
import base64
import uuid
import datetime
import urllib.request
import urllib.error

from dotenv import load_dotenv
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.serialization import load_pem_private_key

# ── DEMO environment (confirmed) ──
REST_ROOT = "/trade-api/v2"
REST_BASE = "https://external-api.demo.kalshi.co" + REST_ROOT

# ── Credentials ──
_script_dir = os.path.dirname(os.path.abspath(__file__))
load_dotenv(dotenv_path=os.path.join(_script_dir, "keys.env"), override=True)
_API_KEY_ID = os.getenv("KALSHI_API_KEY_ID")
with open(os.path.join(_script_dir, "First.txt"), "rb") as _f:
    _private_key = load_pem_private_key(_f.read(), password=None)

TRADE_LOG_CSV = os.path.join(_script_dir, "trade_log.csv")


# ──────────────────────────────────────────────────────────────────
# Low-level signed HTTP
# ──────────────────────────────────────────────────────────────────
def _sign(full_path: str, method: str, ts_ms: str) -> str:
    msg = (ts_ms + method + full_path).encode("utf-8")
    sig = _private_key.sign(
        msg,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                    salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )
    return base64.b64encode(sig).decode("utf-8")


def _headers(full_path: str, method: str) -> dict:
    ts = str(int(time.time() * 1000))
    return {
        "KALSHI-ACCESS-KEY": _API_KEY_ID,
        "KALSHI-ACCESS-SIGNATURE": _sign(full_path, method, ts),
        "KALSHI-ACCESS-TIMESTAMP": ts,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _request(method: str, path: str, body: dict = None) -> dict:
    # Sign the FULL path from the API root; never include host or query string.
    full_path = REST_ROOT + path.split("?")[0]
    url = REST_BASE + path
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, headers=_headers(full_path, method), method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            text = r.read().decode()
            return json.loads(text) if text else {}
    except urllib.error.HTTPError as e:
        detail = e.read().decode()[:400]
        print(f"[KALSHI {method} {path}] HTTP {e.code}: {detail}")
        raise


def _get(path):           return _request("GET", path)
def _post(path, body):    return _request("POST", path, body)
def _delete(path):        return _request("DELETE", path)


# ──────────────────────────────────────────────────────────────────
# Market discovery (look up, never construct)
# ──────────────────────────────────────────────────────────────────
def get_open_markets(series_ticker: str = "KXBTC15M", limit: int = 10) -> list:
    """Return open markets for a series. Use the returned ticker — don't build one."""
    resp = _get(f"/markets?series_ticker={series_ticker}&status=open&limit={limit}")
    return resp.get("markets", [])


def get_market(ticker: str) -> dict:
    """Single market detail. After settlement this carries the result (yes/no)."""
    return _get(f"/markets/{ticker}").get("market", {})


def get_orderbook(ticker: str) -> dict:
    return _get(f"/markets/{ticker}/orderbook")


# ──────────────────────────────────────────────────────────────────
# Account reads
# ──────────────────────────────────────────────────────────────────
def get_balance() -> dict:
    return _get("/portfolio/balance")


def get_open_orders(ticker: str = None) -> list:
    """Orders currently resting. Optionally filter by ticker."""
    path = "/portfolio/orders?status=resting"
    if ticker:
        path += f"&ticker={ticker}"
    return _get(path).get("orders", [])


def get_fills(ticker: str = None, limit: int = 100) -> list:
    """Executions of your orders — what actually traded, at what price."""
    path = f"/portfolio/fills?limit={limit}"
    if ticker:
        path += f"&ticker={ticker}"
    return _get(path).get("fills", [])


def get_positions(ticker: str = None) -> list:
    """What you currently hold."""
    path = "/portfolio/positions"
    if ticker:
        path += f"?ticker={ticker}"
    return _get(path).get("market_positions", _get(path).get("positions", []))


# ──────────────────────────────────────────────────────────────────
# Order actions
# ──────────────────────────────────────────────────────────────────
def place_order(ticker: str, side: str, count: int, price: float,
                tif: str = "good_till_canceled") -> dict:
    """
    side: 'bid' = buy YES, 'ask' = sell YES (== buy NO).
    price: dollars 0.01–0.99. count: whole contracts.
    Returns the create-order response (includes order_id, fill_count, remaining_count).
    """
    body = {
        "ticker": ticker,
        "client_order_id": str(uuid.uuid4()),
        "side": side,
        "count": f"{float(count):.2f}",
        "price": f"{min(max(price, 0.01), 0.99):.2f}",
        "time_in_force": tif,
        "self_trade_prevention_type": "taker_at_cross",
    }
    # NOTE: if this 404s as 'not found' (endpoint), swap to "/portfolio/orders".
    return _post("/portfolio/events/orders", body)


def cancel_order(order_id: str) -> dict:
    """Pull a resting order off the book."""
    return _delete(f"/portfolio/orders/{order_id}")


# ──────────────────────────────────────────────────────────────────
# Settlement → win/loss + CSV log
# ──────────────────────────────────────────────────────────────────
CSV_FIELDS = [
    "logged_at",        # when this row was written (ISO)
    "ticker",           # market ticker
    "ticker_close_ts",  # the market's close/expiry time
    "direction",        # YES / NO (what we bet)
    "purchased_at",     # time the fill happened
    "buy_price",        # price per contract we paid (dollars)
    "contracts",        # how many
    "fill_status",      # filled / unfilled / partial
    "result",           # WON / LOST / NO_POSITION / PENDING
    "payout",           # total $ received at settlement
    "profit",           # payout - cost
]


def _ensure_csv():
    if not os.path.exists(TRADE_LOG_CSV):
        with open(TRADE_LOG_CSV, "w", newline="") as f:
            csv.DictWriter(f, fieldnames=CSV_FIELDS).writeheader()


def log_trade(row: dict):
    """Append one row to the trade log CSV (creates it with headers if missing)."""
    _ensure_csv()
    clean = {k: row.get(k, "") for k in CSV_FIELDS}
    clean["logged_at"] = datetime.datetime.now().isoformat(timespec="seconds")
    with open(TRADE_LOG_CSV, "a", newline="") as f:
        csv.DictWriter(f, fieldnames=CSV_FIELDS).writerow(clean)
    print(f"[LOG] Trade row written to {os.path.basename(TRADE_LOG_CSV)}")


def settle_from_fills(ticker: str) -> dict:
    """
    Auto-settle: reads YOUR real fills for this ticker (verified field names),
    figures out direction/price/contracts, then checks the market result and
    logs win/loss. Use this after a window closes instead of passing args by hand.

    Verified field names (from live demo output):
      fill.outcome_side  -> 'yes' or 'no'  (our direction)
      fill.yes_price_dollars / no_price_dollars -> price paid
      fill.count_fp      -> contracts
      market.result      -> settlement result (UNVERIFIED until a market settles)
    """
    fills = get_fills(ticker)
    if not fills:
        print(f"[SETTLE] No fills for {ticker} — no position taken this window.")
        log_trade({"ticker": ticker, "direction": "-", "fill_status": "unfilled",
                   "result": "NO_POSITION", "payout": 0, "profit": 0, "contracts": 0})
        return {"result": "NO_POSITION"}

    # aggregate this ticker's fills (usually one for our simple bot)
    direction = fills[0].get("outcome_side", "").upper()      # YES / NO
    contracts = sum(float(f.get("count_fp", 0)) for f in fills)
    # price we paid on our side
    if direction == "YES":
        buy_price = float(fills[0].get("yes_price_dollars", 0))
    else:
        buy_price = float(fills[0].get("no_price_dollars", 0))
    purchased_at = fills[0].get("created_time", "")

    mkt = get_market(ticker)
    result = (mkt.get("result") or mkt.get("settlement_result") or "").lower()
    close_ts = mkt.get("close_time") or mkt.get("expiration_time") or ""
    cost = buy_price * contracts

    if result not in ("yes", "no"):
        print(f"[SETTLE] {ticker}: not settled yet (market.result='{result}'). "
              f"Holding {contracts:.0f} {direction} @ ${buy_price:.2f}.")
        summary = {"result": "PENDING", "payout": 0.0, "profit": 0.0}
    else:
        won = (direction.lower() == result)
        payout = (1.00 * contracts) if won else 0.0
        profit = payout - cost
        summary = {"result": "WON" if won else "LOST", "payout": round(payout, 2),
                   "profit": round(profit, 2), "settled_result": result.upper()}
        if won:
            print(f"[SETTLE] {ticker}: settled {result.upper()}. You bet {direction}. WON. "
                  f"Paid ${cost:.2f}, payout ${payout:.2f}, profit +${profit:.2f}.")
        else:
            print(f"[SETTLE] {ticker}: settled {result.upper()}. You bet {direction}. LOST. "
                  f"Out ${cost:.2f}.")

    log_trade({
        "ticker": ticker, "ticker_close_ts": close_ts, "direction": direction,
        "purchased_at": purchased_at, "buy_price": f"{buy_price:.2f}",
        "contracts": int(contracts), "fill_status": "filled",
        "result": summary["result"], "payout": summary["payout"], "profit": summary["profit"],
    })
    return summary


def settle_report(ticker: str, our_direction: str, buy_price: float, contracts: int,
                  fill_status: str = "filled") -> dict:
    """
    After a market settles, figure out win/lost + payout and log it.
    our_direction: 'YES' or 'NO' (the side we actually hold).
    On a binary contract: winning contract pays $1.00, losing pays $0.00.

    Returns a dict summary and writes a CSV row.
    """
    mkt = get_market(ticker)
    # Kalshi marks settled result on the market; field is commonly 'result'
    # ('yes' or 'no'). Verify against a real settled market the first time.
    result = (mkt.get("result") or mkt.get("settlement_result") or "").lower()
    close_ts = mkt.get("close_time") or mkt.get("expiration_time") or ""

    cost = buy_price * contracts

    if result not in ("yes", "no"):
        summary = {"result": "PENDING", "payout": 0.0, "profit": 0.0,
                   "note": f"market not settled yet (result='{result}')"}
    else:
        won = (our_direction.lower() == result)
        payout = (1.00 * contracts) if won else 0.0
        summary = {"result": "WON" if won else "LOST",
                   "payout": round(payout, 2),
                   "profit": round(payout - cost, 2),
                   "settled_result": result.upper()}

    # human message
    if summary["result"] == "WON":
        print(f"[SETTLE] {ticker}: settled {summary['settled_result']}. "
              f"You bet {our_direction}. WON. "
              f"Paid ${cost:.2f} for {contracts}, payout ${summary['payout']:.2f}, "
              f"profit +${summary['profit']:.2f}.")
    elif summary["result"] == "LOST":
        print(f"[SETTLE] {ticker}: settled {summary['settled_result']}. "
              f"You bet {our_direction}. LOST. "
              f"Out ${cost:.2f} ({contracts} contract(s)).")
    else:
        print(f"[SETTLE] {ticker}: {summary['note']}")

    log_trade({
        "ticker": ticker,
        "ticker_close_ts": close_ts,
        "direction": our_direction,
        "purchased_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "buy_price": f"{buy_price:.2f}",
        "contracts": contracts,
        "fill_status": fill_status,
        "result": summary["result"],
        "payout": summary["payout"],
        "profit": summary["profit"],
    })
    return summary


# ── Settings ──
SERIES         = "KXBTC15M"
BET_SIDE       = "bid"        # bid = buy YES  (fixed direction for now)
BET_PRICE      = 0.50         # dollars
BET_CONTRACTS  = 1
POLL_SECONDS   = 10           # how often the loop checks both concerns
MAX_WINDOWS    = 20           # backstop: stop after this many placed windows
SETTLE_RETRIES = 12           # after a window closes, retry settle this many times
SETTLE_WAIT    = 10           # seconds between settle retries (settlement can lag)
STOP_FILE      = os.path.join(os.path.dirname(os.path.abspath(__file__)), "STOP")


def stop_requested():
    return os.path.exists(STOP_FILE)


def current_ticker():
    """Look up the currently-open 15-min market (never construct it)."""
    mkts = get_open_markets(SERIES, limit=10)
    if not mkts:
        return None
    return mkts[0]["ticker"]


def already_in_window(ticker):
    """
    Authoritative check: do we ALREADY have an order or fill on this ticker?
    Reads real Kalshi state (not memory), so it survives bot restarts — this is
    what prevents stacking multiple orders on the same window across restarts.
    """
    try:
        if get_fills(ticker):
            return True
        if get_open_orders(ticker):
            return True
    except Exception as e:
        # if the check fails, be SAFE and assume we're already in (don't double-place)
        print(f"[GUARD] couldn't verify existing orders ({e}) — skipping placement to be safe")
        return True
    return False


def place_bet(ticker):
    """
    Place the bet for this window.
    >>> LIVE FILL-PROTECTION HOOK <<<
    For real money, BEFORE placing: read get_orderbook(ticker), check the
    current ask/spread, and set the limit price smartly (skip if the book is
    bad). On demo the book is empty and a limit order caps price anyway, so we
    place a plain fixed-price limit here.
    """
    # GUARD: never place if we already have an order/fill on this window (survives restarts)
    if already_in_window(ticker):
        print(f"[GUARD] {ticker}: already have an order/fill here — NOT placing again.")
        return None

    print(f"[PLACE] {ticker}: YES @ ${BET_PRICE:.2f} x{BET_CONTRACTS}")
    resp = place_order(ticker, BET_SIDE, BET_CONTRACTS, BET_PRICE)
    oid = resp.get("order_id")
    fc  = float(resp.get("fill_count", 0) or 0)
    if oid:
        print(f"[PLACE] accepted order_id={oid} | filled now: {fc:.0f} | "
              f"resting: {resp.get('remaining_count')}")
    else:
        print(f"[PLACE] no order_id returned: {resp}")
    return resp


def report_status(ticker):
    """Print whether our order on this ticker is filled or still pending."""
    fills = get_fills(ticker)
    if fills:
        f = fills[0]
        print(f"[WATCH] {ticker}: FILLED — {f.get('outcome_side','').upper()} "
              f"x{f.get('count_fp')} @ ${f.get('yes_price_dollars')}")
        return "filled"
    orders = get_open_orders(ticker)
    if orders:
        print(f"[WATCH] {ticker}: PENDING — resting, not filled yet")
        return "pending"
    print(f"[WATCH] {ticker}: no order/fill seen")
    return "none"


def settle_window(ticker):
    """After a window closes, settle + log. Retries because settlement can lag."""
    for attempt in range(1, SETTLE_RETRIES + 1):
        summary = settle_from_fills(ticker)
        if summary.get("result") != "PENDING":
            return summary
        print(f"[SETTLE] {ticker}: pending (try {attempt}/{SETTLE_RETRIES}), "
              f"waiting {SETTLE_WAIT}s...")
        time.sleep(SETTLE_WAIT)
    print(f"[SETTLE] {ticker}: still pending after retries — logged as PENDING.")
    return {"result": "PENDING"}


def main():
    print("=" * 60)
    print("  CONTINUOUS DEMO TRADE LOOP")
    print(f"  Bet: YES @ ${BET_PRICE:.2f} | poll {POLL_SECONDS}s | max {MAX_WINDOWS} windows")
    print("  Graceful stop: create a file named STOP in this folder.")
    print("=" * 60)
    print(f"  Balance: {get_balance().get('balance_dollars')}")

    active_ticker = None     # the ticker we currently have a bet on
    windows_done  = 0        # how many windows we've placed in (backstop counter)
    stopping      = False

    while True:
        # 1) check for graceful stop
        if stop_requested() and not stopping:
            stopping = True
            print("\n[STOP] STOP file found. Will NOT place new bets. "
                  "Finishing current position, then exiting.\n")

        # 2) what's the current open market?
        tkr = current_ticker()

        # 3) has our active window rolled over? -> settle the old one
        if active_ticker and tkr != active_ticker:
            print(f"\n[ROLL] Window {active_ticker} closed (new open: {tkr}).")
            settle_window(active_ticker)        # settle + log the finished bet
            active_ticker = None                # clear so we can bet the new one

            if stopping:
                print("[STOP] Current position finished and logged. Exiting cleanly.")
                break
            if windows_done >= MAX_WINDOWS:
                print(f"[BACKSTOP] Reached MAX_WINDOWS ({MAX_WINDOWS}). Exiting.")
                break

        # 4) if we have no active bet and we're allowed to trade -> place one
        if active_ticker is None and not stopping:
            if tkr is None:
                print("[WAIT] No open market yet — waiting for next ticker...")
            else:
                resp = place_bet(tkr)           # guard inside: won't double-place
                active_ticker = tkr             # watch this window either way
                if resp is not None:            # only count an ACTUAL new placement
                    windows_done += 1
                    print(f"[INFO] Windows placed: {windows_done}/{MAX_WINDOWS}")
                else:
                    print(f"[INFO] Watching existing position on {tkr} (no new order placed)")

        # 5) watch the current bet (filled / pending)
        if active_ticker:
            report_status(active_ticker)

        # 6) if stopping and nothing active, exit now
        if stopping and active_ticker is None:
            print("[STOP] No active position. Exiting cleanly.")
            break

        time.sleep(POLL_SECONDS)

    print("\n[DONE] Trade loop stopped. Trade history is in trade_log.csv")


if __name__ == "__main__":
    main()
