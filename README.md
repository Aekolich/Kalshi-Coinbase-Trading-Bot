# Kalshi + Coinbase BTC Trading Bot

This is a bot I built that watches Bitcoin's price on Coinbase and trades Kalshi's 15-minute BTC markets, which pay out based on whether BTC ends higher or lower than where it started. It runs on a loop and keeps placing trades on its own.

Fair warning up front: it doesn't make money. I get into why at the bottom. This was a personal project to figure out how all of this actually works, and it's not financial advice.

## How it's put together

I split it into a few files so each part does one thing:

- `stream.py` pulls 15-minute BTC candles from Coinbase and keeps a rolling window of the last 100. It backfills on startup so I'm not sitting there for a day waiting for enough data to build up. This is also where the features get calculated.
- `model.py` is a logistic regression model that outputs the probability that the next 15 minutes closes up.
- `strategy.py` takes that probability and decides whether to actually trade, after checking a confidence threshold and a volatility filter.
- `kalshi_client.py` handles the Kalshi side: signing requests, placing orders, pulling quotes.
- `main.py` ties it together and runs the loop.
- The research notebook is where I backtested and tried things out.

The basic flow is: pull data, build features, model makes a prediction, strategy decides, order goes to Kalshi.

## What the model looks at

All calculated from the price window:

- Log returns (how fast it's moving)
- Volatility (standard deviation over the window)
- RSI
- Moving-average ratio (short SMA over long SMA, for trend)

## Running it

You need your own Kalshi API keys. They're not in the repo, so you have to add them yourself. Then:

```
pip install numpy requests websockets scikit-learn
python main.py
```

It backfills the data, connects, and starts checking for trades.

## Does it work? No.

The whole pipeline runs fine. Data comes in, features get built, the model predicts, orders go out, everything logs. It just doesn't make money once you factor in fees.
