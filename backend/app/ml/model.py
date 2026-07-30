"""NumPy-based walk-forward model training and inference module."""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import numpy as np

from app.data_sources.binance_public import Candle
from app.ml.features import FEATURE_CONTRACT, build_ml_features

logger = logging.getLogger(__name__)

WEIGHTS_FILE = Path(__file__).resolve().parent / "trained_weights.json"


def get_weights_filepath(symbol: str = "", timeframe: str = "") -> Path:
    """Get the path to the trained weights file, isolating by symbol/timeframe if provided."""
    if symbol and timeframe:
        clean_sym = symbol.upper().replace("/", "").replace("-", "").strip()
        clean_tf = timeframe.lower().strip()
        return Path(__file__).resolve().parent / f"trained_weights_{clean_sym}_{clean_tf}.json"
    return WEIGHTS_FILE


class RidgeRegression:
    def __init__(self, alpha: float = 1.0):
        self.alpha = alpha
        self.weights = None
        self.intercept = 0.0

    def fit(self, X: np.ndarray, y: np.ndarray) -> RidgeRegression:
        n_samples, n_features = X.shape
        X_mean = X.mean(axis=0)
        y_mean = y.mean()

        X_centered = X - X_mean
        y_centered = y - y_mean

        # Ridge closed form: beta = (X_centered.T @ X_centered + alpha * I)^-1 @ X_centered.T @ y_centered
        I = np.identity(n_features)
        A = X_centered.T @ X_centered + self.alpha * I
        try:
            self.weights = np.linalg.inv(A) @ X_centered.T @ y_centered
        except np.linalg.LinAlgError:
            self.weights = np.linalg.lstsq(A, X_centered.T @ y_centered, rcond=None)[0]

        self.intercept = y_mean - X_mean @ self.weights
        return self

    def predict(self, X: np.ndarray) -> np.ndarray:
        if self.weights is None:
            raise ValueError("Model is not fitted yet.")
        return X @ self.weights + self.intercept


def train_walk_forward_model(
    symbol: str,
    timeframe: str,
    candles: list[Candle],
    ticker: dict[str, Any],
    order_book: dict[str, Any],
    train_fraction: float = 0.7,
    ridge_alpha: float = 10.0,
) -> dict[str, Any]:
    """Train a research model on the Bare Eye causal feature contract."""
    n = len(candles)
    if n < 60:
        raise ValueError(f"Insufficient candles for training. Need at least 60, got {n}")

    # Build features step-by-step. A single live order-book snapshot cannot
    # represent historical microstructure; using it for every historical row
    # would leak present-day information into training. Exclude microstructure
    # fields unless timestamped depth history is supplied in a future version.
    feature_list = []
    targets = []

    # Features require at least 50 candles for indicators
    start_idx = 50
    feature_names = None

    for i in range(start_idx, n - 1):
        slice_candles = candles[: i + 1]
        feat = build_ml_features(
            symbol=symbol,
            timeframe=timeframe,
            candles=slice_candles,
            ticker=ticker,
            order_book=order_book,
            extra_context={},
        )
        # Target: Log return of next close
        next_close = float(candles[i + 1].close)
        curr_close = float(candles[i].close)
        target = np.log(next_close / curr_close)

        # Filter numeric features
        numeric_feats = {
            k: v for k, v in feat.items()
            if isinstance(v, (int, float)) and not k.startswith("micro_")
        }
        if feature_names is None:
            feature_names = list(numeric_feats.keys())

        vec = [numeric_feats.get(name, 0.0) for name in feature_names]
        feature_list.append(vec)
        targets.append(target)

    X = np.array(feature_list)
    y = np.array(targets)

    n_samples = len(X)
    if n_samples < 10:
        raise ValueError("Too few samples to perform train-test split.")

    split = int(n_samples * train_fraction)
    X_train, X_test = X[:split], X[split:]
    y_train, y_test = y[:split], y[split:]

    # Scale features based on training set
    X_mean = X_train.mean(axis=0)
    X_std = X_train.std(axis=0)
    X_std[X_std == 0] = 1.0  # Avoid division by zero

    X_train_scaled = (X_train - X_mean) / X_std
    X_test_scaled = (X_test - X_mean) / X_std

    model = RidgeRegression(alpha=ridge_alpha)
    model.fit(X_train_scaled, y_train)

    train_preds = model.predict(X_train_scaled)
    test_preds = model.predict(X_test_scaled)

    # Calculate metrics
    train_ic = float(np.corrcoef(train_preds, y_train)[0, 1]) if np.std(train_preds) > 0 and np.std(y_train) > 0 else 0.0
    test_ic = float(np.corrcoef(test_preds, y_test)[0, 1]) if np.std(test_preds) > 0 and np.std(y_test) > 0 else 0.0

    # Save weights & scaling parameters
    weights_dict = {
        "symbol": symbol,
        "timeframe": timeframe,
        "feature_contract": FEATURE_CONTRACT,
        "feature_names": feature_names,
        "weights": model.weights.tolist(),
        "intercept": float(model.intercept),
        "mean": X_mean.tolist(),
        "std": X_std.tolist(),
        "train_ic": train_ic,
        "test_ic": test_ic,
        "training_limitations": [
            "Historical order-book snapshots were unavailable; microstructure features were excluded.",
            "Historical public WebSocket execution tape was unavailable; completed-kline taker notional is the explicit fallback.",
            "Validate with purged walk-forward folds and realistic fees/slippage before capital use.",
        ],
    }

    weights_file = get_weights_filepath(symbol, timeframe)
    with open(weights_file, "w", encoding="utf-8") as f:
        json.dump(weights_dict, f, indent=2)

    logger.info(f"Successfully trained ML model for {symbol} {timeframe}. Test IC: {test_ic:.4f}")
    return weights_dict


def predict_probability_from_model(
    features: dict[str, Any], symbol: str = "", timeframe: str = ""
) -> dict[str, Any] | None:
    """Predict directional probabilities if a trained weights file is available."""
    weights_file = get_weights_filepath(symbol, timeframe)
    if not weights_file.exists():
        weights_file = WEIGHTS_FILE
        if not weights_file.exists():
            return None

    try:
        with open(weights_file, "r", encoding="utf-8") as f:
            weights_data = json.load(f)

        if weights_data.get("feature_contract") != FEATURE_CONTRACT:
            logger.warning(
                "Suppressing model artifact with legacy or unknown feature contract: %s",
                weights_data.get("feature_contract", "missing"),
            )
            return None

        # Validate symbol/timeframe metadata to prevent cross-market/timeframe application
        trained_symbol = weights_data.get("symbol", "").upper().strip()
        trained_tf = weights_data.get("timeframe", "").strip()
        if symbol and trained_symbol != symbol.upper().strip():
            logger.warning(f"Model symbol mismatch: weights trained for {trained_symbol}, requested {symbol}")
            return None
        if timeframe and trained_tf != timeframe:
            logger.warning(f"Model timeframe mismatch: weights trained for {trained_tf}, requested {timeframe}")
            return None

        # Validate out-of-sample performance: reject models with weak test IC
        test_ic = float(weights_data.get("test_ic", 0.0))
        MIN_TEST_IC = 0.05
        if test_ic < MIN_TEST_IC:
            logger.warning(
                f"Model out-of-sample validation result is weak (Test IC: {test_ic:.4f} < {MIN_TEST_IC}). "
                f"Suppressing model prediction."
            )
            return None

        feature_names = weights_data["feature_names"]
        weights = np.array(weights_data["weights"])
        intercept = weights_data["intercept"]
        mean = np.array(weights_data["mean"])
        std = np.array(weights_data["std"])

        # Construct vector
        vec = []
        for name in feature_names:
            vec.append(float(features.get(name, 0.0)))

        X = np.asarray(vec, dtype=float)
        if not (len(feature_names) == len(weights) == len(mean) == len(std)):
            raise ValueError("Model artifact has inconsistent feature dimensions.")
        if not np.isfinite(X).all() or not np.isfinite(weights).all() or not np.isfinite(mean).all() or not np.isfinite(std).all():
            raise ValueError("Model artifact or inference features contain non-finite values.")
        safe_std = np.where(np.abs(std) < 1e-12, 1.0, std)
        X_scaled = (X - mean) / safe_std

        raw_pred = X_scaled @ weights + intercept

        # Map to probability via sigmoid with scaling factor
        logit = float(np.clip(raw_pred * 100.0, -30.0, 30.0))
        prob_up = 1.0 / (1.0 + np.exp(-logit))  # Return forecasts are small; use a bounded logit scale.
        prob_up = max(0.01, min(0.99, prob_up))
        prob_down = 1.0 - prob_up

        return {
            "probability_up": round(float(prob_up), 4),
            "probability_down": round(float(prob_down), 4),
            "model_status": (
                "Bare Eye causal research model "
                f"(Out-of-sample Test IC: {test_ic:.4f})"
            ),
            "model": "Bare Eye Causal Ridge Regression",
            "feature_contract": FEATURE_CONTRACT,
            "test_ic": test_ic,
        }
    except Exception as e:
        logger.error(f"Error predicting using ML model: {e}")
        return None
