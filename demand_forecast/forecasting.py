"""
Forecasting Engine
==================
Ensemble of SARIMA + LSTM.  Model weights are determined dynamically by
comparing in-sample MAPE so the better-performing model gets more influence.

Public API
----------
predict_demand(df, item_id, horizon_months) -> (dates, values)
check_stock_and_alert(df, item_id, demand, dates, threshold) -> [str]
"""

import logging
import os
import warnings

# Must be set before tensorflow/statsmodels are imported
os.environ.setdefault("TF_ENABLE_ONEDNN_OPTS", "0")
os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "3")
os.environ.setdefault("TF_KERAS", "1")
warnings.filterwarnings("ignore")

import numpy as np
import pandas as pd
from sklearn.preprocessing import MinMaxScaler
from statsmodels.tools.sm_exceptions import ConvergenceWarning
from statsmodels.tsa.statespace.sarimax import SARIMAX
from statsmodels.tsa.stattools import adfuller

warnings.filterwarnings("ignore", category=ConvergenceWarning)

import tensorflow as tf

tf.get_logger().setLevel("ERROR")

from tensorflow.keras.callbacks import EarlyStopping
from tensorflow.keras.layers import LSTM, Dense, Dropout, Input
from tensorflow.keras.models import Sequential

logger = logging.getLogger("demand_forecast.forecasting")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_HORIZON = 6
WINDOW_SIZE = 6
LSTM_EPOCHS = 200
MIN_SAMPLES_FOR_LSTM = WINDOW_SIZE + 4   # need at least this many monthly obs
SARIMA_SEARCH_SPACE = range(0, 2)        # p/d/q and P/D/Q each 0-1


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mape(y_true: np.ndarray, y_pred: np.ndarray) -> float:
    """Mean Absolute Percentage Error, ignoring zero-demand months."""
    mask = y_true != 0
    if not mask.any():
        return float("inf")
    return float(np.mean(np.abs((y_true[mask] - y_pred[mask]) / y_true[mask])) * 100)


def _is_stationary(series: pd.Series) -> bool:
    clean = series.dropna()
    if len(clean) < 4:
        return True   # Not enough data — skip differencing
    return adfuller(clean)[1] < 0.05


def _make_stationary(series: pd.Series, period: int = 12) -> pd.Series:
    """Seasonal difference the series if non-stationary."""
    if not _is_stationary(series):
        diffed = series.diff(period).dropna()
        if len(diffed) >= MIN_SAMPLES_FOR_LSTM:
            return diffed
    return series


def _prepare_monthly(df: pd.DataFrame, item_id: str) -> pd.Series:
    """Extract and aggregate monthly demand for a single item."""
    item_df = df[df["item_id"] == item_id].copy()
    item_df.sort_values("transaction_date", inplace=True)
    item_df["year_month"] = item_df["transaction_date"].dt.to_period("M")
    monthly = (
        item_df.groupby("year_month")["quantity"]
        .sum()
        .reset_index()
        .set_index("year_month")
        .asfreq("M", fill_value=0)
    )
    # Forward-fill and clip negatives
    monthly["quantity"] = monthly["quantity"].ffill().clip(lower=0)
    return monthly["quantity"]


# ---------------------------------------------------------------------------
# SARIMA
# ---------------------------------------------------------------------------

def _fit_sarima(series: pd.Series, horizon: int):
    """
    Grid search over (p,d,q)×(P,D,Q,12) SARIMA and return the forecast
    from the best-AIC model.  Returns (forecast_array | None, model | None).
    """
    best_aic = np.inf
    best_model = None

    for p in SARIMA_SEARCH_SPACE:
        for d in SARIMA_SEARCH_SPACE:
            for q in SARIMA_SEARCH_SPACE:
                for P in SARIMA_SEARCH_SPACE:
                    for D in SARIMA_SEARCH_SPACE:
                        for Q in SARIMA_SEARCH_SPACE:
                            try:
                                model = SARIMAX(
                                    series,
                                    order=(p, d, q),
                                    seasonal_order=(P, D, Q, 12),
                                    enforce_stationarity=False,
                                    enforce_invertibility=False,
                                )
                                res = model.fit(disp=False, maxiter=200, method="lbfgs")
                                if res.aic < best_aic:
                                    best_aic = res.aic
                                    best_model = res
                            except Exception:
                                pass  # noqa: E722

    if best_model is None:
        return None, None

    forecast = best_model.forecast(steps=horizon)
    return np.clip(np.array(forecast), 0, None), best_model


# ---------------------------------------------------------------------------
# LSTM
# ---------------------------------------------------------------------------

def _prepare_sequences(scaled_data: np.ndarray, window: int):
    X, y = [], []
    for i in range(len(scaled_data) - window):
        X.append(scaled_data[i : i + window])
        y.append(scaled_data[i + window])
    return np.array(X), np.array(y)


def _build_lstm(window: int) -> Sequential:
    model = Sequential([
        Input(shape=(window, 1)),
        LSTM(128, return_sequences=True, activation="relu"),
        Dropout(0.3),
        LSTM(64, activation="relu"),
        Dropout(0.2),
        Dense(1),
    ])
    model.compile(optimizer="adam", loss="mean_squared_error")
    return model


def _fit_lstm(series: pd.Series, horizon: int) -> np.ndarray | None:
    """Fit a windowed LSTM and auto-regressively forecast ``horizon`` steps."""
    if len(series) < MIN_SAMPLES_FOR_LSTM:
        logger.warning(
            "Insufficient data for LSTM (%d samples); skipping.", len(series)
        )
        return None

    scaler = MinMaxScaler()
    scaled = scaler.fit_transform(series.values.reshape(-1, 1))
    X, y = _prepare_sequences(scaled, WINDOW_SIZE)
    if len(X) == 0:
        return None

    X = X.reshape((X.shape[0], WINDOW_SIZE, 1))
    model = _build_lstm(WINDOW_SIZE)

    model.fit(
        X, y,
        epochs=LSTM_EPOCHS,
        validation_split=0.2,
        verbose=0,
        callbacks=[
            EarlyStopping(monitor="val_loss", patience=10, restore_best_weights=True)
        ],
    )

    last_window = scaled[-WINDOW_SIZE:].reshape(1, WINDOW_SIZE, 1)
    preds = []
    for _ in range(horizon):
        pred = model.predict(last_window, verbose=0)[0, 0]
        preds.append(pred)
        last_window = np.append(last_window[:, 1:, :], [[[pred]]], axis=1)

    forecast = scaler.inverse_transform(np.array(preds).reshape(-1, 1)).flatten()
    return np.clip(forecast, 0, None)


# ---------------------------------------------------------------------------
# Ensemble
# ---------------------------------------------------------------------------

def _ensemble(
    sarima_fc: np.ndarray | None,
    lstm_fc: np.ndarray | None,
    series: pd.Series,
    horizon: int,
) -> np.ndarray:
    """
    Combine SARIMA and LSTM forecasts.  Weights are computed dynamically
    from in-sample MAPE on the last ``horizon`` observations.
    Falls back to whichever model produced a result if one fails.
    """
    if sarima_fc is None and lstm_fc is None:
        raise RuntimeError("Both SARIMA and LSTM failed to produce a forecast.")

    if sarima_fc is None:
        return lstm_fc
    if lstm_fc is None:
        return sarima_fc

    # Dynamic weighting based on hold-out MAPE
    if len(series) > horizon:
        actuals = series.values[-horizon:]
        s_mape = _mape(
            actuals,
            sarima_fc[-horizon:] if len(sarima_fc) >= horizon else sarima_fc,
        )
        l_mape = _mape(
            actuals,
            lstm_fc[-horizon:] if len(lstm_fc) >= horizon else lstm_fc,
        )
        logger.info("SARIMA MAPE=%.2f%%  LSTM MAPE=%.2f%%", s_mape, l_mape)

        total = s_mape + l_mape
        if total == 0:
            sarima_w, lstm_w = 0.5, 0.5
        else:
            # Lower MAPE → higher weight
            sarima_w = l_mape / total
            lstm_w = s_mape / total
    else:
        sarima_w, lstm_w = 0.6, 0.4

    logger.info("Ensemble weights | SARIMA=%.2f  LSTM=%.2f", sarima_w, lstm_w)
    return sarima_w * sarima_fc + lstm_w * lstm_fc


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def predict_demand(
    df: pd.DataFrame,
    item_id: str,
    horizon_months: int = DEFAULT_HORIZON,
) -> tuple[list, np.ndarray]:
    """
    Fit ensemble model and return future demand forecast.

    Parameters
    ----------
    df            : DataFrame with columns [transaction_date, item_id, quantity]
    item_id       : Item to forecast
    horizon_months: Number of months ahead to forecast

    Returns
    -------
    (future_dates, predicted_demand)
    """
    series = _prepare_monthly(df, item_id)
    stationary = _make_stationary(series)

    logger.info("Fitting SARIMA | item=%s obs=%d", item_id, len(stationary))
    sarima_fc, _ = _fit_sarima(stationary, horizon_months)

    logger.info("Fitting LSTM | item=%s obs=%d", item_id, len(stationary))
    lstm_fc = _fit_lstm(stationary, horizon_months)

    forecast = _ensemble(sarima_fc, lstm_fc, series, horizon_months)

    last_period = series.index[-1]
    future_dates = [
        last_period.to_timestamp() + pd.DateOffset(months=i)
        for i in range(1, horizon_months + 1)
    ]
    return future_dates, forecast


def check_stock_and_alert(
    df: pd.DataFrame,
    item_id: str,
    predicted_demand: np.ndarray,
    future_months: list,
    threshold: float = 0.0,
) -> list[str]:
    """
    Compare predicted demand against current on-hand stock.

    Parameters
    ----------
    threshold : Safety buffer — alert when demand exceeds
                ``current_stock * (1 - threshold)`` if threshold < 1,
                or when demand exceeds ``current_stock + threshold`` if > 1.
                Default 0.0 means alert when demand strictly exceeds stock.
    """
    current_stock = float(df[df["item_id"] == item_id]["quantity"].sum())
    alerts = []

    for i, demand in enumerate(predicted_demand):
        demand = float(demand)
        label = future_months[i].strftime("%Y-%m") if future_months else f"Month +{i+1}"

        reorder_needed = demand - current_stock
        if reorder_needed > threshold:
            alerts.append({
                "level": "warning",
                "period": label,
                "message": (
                    f"Reorder {reorder_needed:.0f} units of item '{item_id}' "
                    f"by {label} — predicted demand ({demand:.0f}) "
                    f"exceeds stock ({current_stock:.0f})."
                ),
            })
        else:
            alerts.append({
                "level": "ok",
                "period": label,
                "message": (
                    f"Stock sufficient for '{item_id}' in {label}. "
                    f"Predicted demand: {demand:.0f}, Available: {current_stock:.0f}."
                ),
            })

    return alerts