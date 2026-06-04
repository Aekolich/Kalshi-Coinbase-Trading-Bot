from dataclasses import dataclass
from typing import Optional
from stream import MarketStream


@dataclass
class Order:
    coin: str
    sz: float
    direction: str  # "BET_UP" or "BET_DOWN"


@dataclass
class TickReplay:
    coin: str
    sz: float
    direction: Optional[str]
    prob_up: float
    last_price: float
    log_returns: float
    volatility: float
    sma: float


class LogisticTakerStrat:

    def __init__(
            self,
            exchange,
            coin: str,
            model,
            stream: MarketStream,
            sz: float,
            up_threshold: float = 0.55,
            down_threshold: float = 0.45,
    ):
        self.exchange = exchange
        self.model = model
        self.stream = stream
        self.coin = coin
        self.sz = sz
        self.up_threshold = up_threshold
        self.down_threshold = down_threshold

    def get_features(self, px: float) -> Optional[dict]:
        self.stream.ingest_price(px)
        return self.stream.get_features()

    def predict(self, features: dict) -> dict:
        return self.model.predict(features)

    def strategy(self, prob_up: float) -> Optional[Order]:
        if prob_up >= self.up_threshold:
            print(f"[STRATEGY] Confidence {prob_up:.2%} — BETTING UP (Going Long)")
            return Order(self.coin, self.sz, direction="BET_UP")
        elif prob_up <= self.down_threshold:
            print(f"[STRATEGY] Confidence {prob_up:.2%} — BETTING DOWN (Going Short)")
            return Order(self.coin, self.sz, direction="BET_DOWN")
        else:
            print(f"[STRATEGY] Confidence {prob_up:.2%} — HOLD, not enough confidence to bet")
            return None

    def execute(self, order: Order) -> None:
        # FIX: Guard against exchange=None.
        # When exchange is None the bot is in paper-trade mode — log the signal
        # but don't attempt market_open, which crashes with AttributeError.
        # FIX (wording): make it explicit that NOTHING is placed in paper mode.
        # This is a simulated signal only — no order, no fill, no money. Real
        # placement/fill/slippage reporting arrives with the order client (#9).
        if self.exchange is None:
            print(f"[STRATEGY] [SIMULATED — no order sent] Signal: {order.direction} | {order.coin} | sz={order.sz}")
            return

        is_buy_up = (order.direction == "BET_UP")
        try:
            r = self.exchange.market_open(self.coin, is_buy_up, float(order.sz))
            print(f"[STRATEGY] Order placed: {r}")
        except Exception as e:
            print(f"[STRATEGY] Error placing order: {e}")

    def on_tick(self, px: float) -> Optional[TickReplay]:
        features = self.get_features(px)
        if features is None:
            return None

        result = self.predict(features)
        prob_up = result["probability"]

        order = self.strategy(prob_up)

        if order is not None:
            self.execute(order)

        return TickReplay(
            coin=self.coin,
            sz=self.sz if order else 0.0,
            direction=order.direction if order else None,
            prob_up=prob_up,
            last_price=px,
            log_returns=features["log_returns"],
            volatility=features["volatility"],
            sma=features["sma"]
        )
  
