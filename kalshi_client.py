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


# ──────────────────────────────────────────────────────────────────
# Quick self-test (reads only — places nothing)
# ──────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("kalshi_client.py — read-only self test (no orders placed)")
    print("balance:", get_balance())
    mkts = get_open_markets()
    print(f"open KXBTC15M markets: {len(mkts)}")
    if mkts:
        t = mkts[0]["ticker"]
        print("  using:", t)
        print("  open orders:", get_open_orders(t))
        print("  fills:", get_fills(t))
        print("  positions:", get_positions(t))
