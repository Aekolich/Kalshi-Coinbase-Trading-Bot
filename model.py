import math

class LogisticRegressionModel:
    def __init__(self):
        # ---------------------------------------------------------
        # THE WEIGHTS (Your "Trained" linear parameters)
        # For now, we hardcode these. Later, your research.py 
        # offline backtester will find the optimal numbers for these.
        # ---------------------------------------------------------
        self.weights = {
            "log_returns": 5.0,   # Strong positive weight (momentum)
            "volatility": -2.0,   # Negative weight (high volatility reduces certainty)
            # FIX (#4): two new features. Placeholders — overwrite after research.
            "rsi": 0.0,           # momentum oscillator (0-100)
            "sma_ratio": 0.0,     # short(20) SMA / long(100) SMA
        }
        self.bias = -0.1          # Base threshold adjustment

        # FIX (#3): SCALING CONSTANTS — placeholders for YOU to fill in after research.
        # The model must see features on the SAME scale it was trained on. Live, raw
        # volatility is ~400 while log_returns is ~0.02, so without scaling the volatility
        # term swamps everything and sigmoid pins to 0.00. The fix is to standardize each
        # feature: scaled = (raw - mean) / std, using mean/std measured from training data.
        #
        # Left as mean=0, std=1 for now, which makes scaling a NO-OP (subtracting 0 and
        # dividing by 1 changes nothing). Paste your research's printed mean/std here to
        # activate it. NOTE: confidence will stay ~0.00 until real std values are entered.
        # FIX (#4): expanded to all 4 features.
        self.feature_mean = {"log_returns": 0.0, "volatility": 0.0, "rsi": 0.0, "sma_ratio": 0.0}
        self.feature_std  = {"log_returns": 1.0, "volatility": 1.0, "rsi": 1.0, "sma_ratio": 1.0}

        # FIX (#5): manual thresholds, single source of truth across all files.
        # These MUST match strategy.py and research.py (all 0.55 / 0.45).
        # No more auto-derived percentiles — you set these two numbers by hand.
        self.up_threshold   = 0.55
        self.down_threshold = 0.45

        print("[MODEL] Logistic Regression Classifier initialized.")

    def sigmoid(self, x: float) -> float:
        """
        The core of Logistic Regression.
        Squashes any real number into a probability between 0.0 and 1.0.
        """
        # math.exp(-x) can overflow if x is a massive negative number, 
        # so we cap it for safety in production.
        x = max(min(x, 100), -100) 
        return 1.0 / (1.0 + math.exp(-x))

    # FIX (#3): apply standardization to one raw feature using the saved constants.
    # Guards against std=0 (would divide by zero) by falling back to 1.0.
    def scale_feature(self, name: str, raw_value: float) -> float:
        mean = self.feature_mean.get(name, 0.0)
        std = self.feature_std.get(name, 1.0)
        if std == 0:
            std = 1.0
        return (raw_value - mean) / std

    def predict(self, features: dict) -> dict:
        """
        Takes live indicators from stream.py, applies the linear formula, 
        and squashes it into a classification probability.
        """
        if not features:
            return {"probability": 0.5, "signal": "HOLD"}
        
        try:
            # Extract features (defaulting to 0 if missing)
            # FIX (#4): all 4 model features now. Order matches research.py FEATURE_NAMES.
            ret = features.get("log_returns", 0.0)
            vol = features.get("volatility", 0.0)
            rsi = features.get("rsi", 0.0)
            smar = features.get("sma_ratio", 0.0)

            # FIX (#3): scale the RAW features before the linear equation, so they match
            # the scale the weights were trained on. While constants are placeholders
            # (mean=0, std=1) this leaves the numbers unchanged.
            ret_scaled = self.scale_feature("log_returns", ret)
            vol_scaled = self.scale_feature("volatility", vol)
            rsi_scaled = self.scale_feature("rsi", rsi)
            smar_scaled = self.scale_feature("sma_ratio", smar)

            # FIX (#3 + #4): print scaled features so you can confirm they land in the
            # ~ -3 to +3 range once real constants are pasted in. Remove later if noisy.
            print(f"[MODEL] Scaled features -> log_returns: {ret_scaled:.4f} | "
                  f"volatility: {vol_scaled:.4f} | rsi: {rsi_scaled:.4f} | sma_ratio: {smar_scaled:.4f}")

            # 1. The Linear Equation (z = w1*x1 + w2*x2 + ... + bias)
            # FIX (#3): uses SCALED features. FIX (#4): all 4 features.
            z = ((ret_scaled  * self.weights["log_returns"]) +
                 (vol_scaled  * self.weights["volatility"]) +
                 (rsi_scaled  * self.weights["rsi"]) +
                 (smar_scaled * self.weights["sma_ratio"]) +
                 self.bias)

            # 2. The Classification Filter (Sigmoid)
            probability = self.sigmoid(z)

            # 3. Translate Probability into a Trading Signal
            # FIX (#5): use the manual thresholds (0.55 / 0.45), matching strategy.py
            # and research.py. >= up = BUY_YES, <= down = BUY_NO, between = HOLD.
            if probability >= self.up_threshold:
                signal = "BUY_YES"
            elif probability <= self.down_threshold:
                signal = "BUY_NO"
            else:
                signal = "HOLD"

            return {
                "probability": probability, 
                "raw_z_score": z,
                "signal": signal
            }

        except Exception as e:
            print(f"[MODEL ERROR] Prediction failed: {e}")
            return {"probability": 0.5, "signal": "HOLD"}