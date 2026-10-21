"""Trading rules. Pure functions on daily bars, no broker code here.

The idea (trend-following swing trading):
  * Only buy when the overall market (SPY) is above its 200-day average.
  * Buy ETFs that are in an uptrend (price > 50-day avg, 20-day avg > 50-day avg)
    with positive 3-month momentum, ranked by momentum per unit of volatility.
  * Don't chase: skip anything stretched far above its 20-day average.
  * Skip anything that just gapped violently (probably news we can't judge).
  * Every position gets a stop-loss at the broker; the stop only ever moves up.
  * Sell when the price closes below the 50-day average or the stop is hit.
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd


def add_indicators(df: pd.DataFrame, p: dict) -> pd.DataFrame:
    """df needs columns open, high, low, close (daily, oldest first)."""
    df = df.copy()
    c = df["close"]
    df["sma_fast"] = c.rolling(p["sma_fast"]).mean()
    df["sma_slow"] = c.rolling(p["sma_slow"]).mean()
    df["sma_regime"] = c.rolling(p["sma_regime"]).mean()
    prev_close = c.shift(1)
    tr = pd.concat(
        [df["high"] - df["low"], (df["high"] - prev_close).abs(), (df["low"] - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    df["atr"] = tr.rolling(p["atr_len"]).mean()
    df["mom"] = c / c.shift(p["mom_len"]) - 1
    df["gap_atr"] = (df["open"] - prev_close).abs() / df["atr"].shift(1)
    return df


def _ok(*vals) -> bool:
    return all(v is not None and not (isinstance(v, float) and math.isnan(v)) for v in vals)


def market_ok(row: pd.Series) -> bool:
    """Market regime filter on the SPY row."""
    if not _ok(row.get("sma_regime")):
        return False
    return row["close"] > row["sma_regime"]


def entry_signal(row: pd.Series, p: dict) -> tuple[bool, str]:
    if not _ok(row.get("sma_slow"), row.get("atr"), row.get("mom")):
        return False, "not enough history"
    if row["close"] <= row["sma_slow"]:
        return False, "below 50-day average"
    if row["sma_fast"] <= row["sma_slow"]:
        return False, "20-day avg below 50-day avg"
    if row["mom"] <= 0:
        return False, "negative 3-month momentum"
    if row["close"] > row["sma_fast"] + p["max_extension_atr"] * row["atr"]:
        return False, "too extended, not chasing"
    if _ok(row.get("gap_atr")) and row["gap_atr"] > p["shock_gap_atr"]:
        return False, "violent gap today (news shock?)"
    return True, "uptrend"


def score(row: pd.Series) -> float:
    """Momentum per unit of volatility. Higher is better."""
    vol = row["atr"] / row["close"]
    return float(row["mom"] / vol) if vol > 0 else -np.inf


def exit_signal(row: pd.Series) -> tuple[bool, str]:
    if _ok(row.get("sma_slow")) and row["close"] < row["sma_slow"]:
        return True, "closed below 50-day average"
    return False, ""


def initial_stop(price: float, atr: float, p: dict) -> float:
    return round(price - p["stop_atr"] * atr, 2)


def trailed_stop(current_stop: float, highest_close: float, atr: float, p: dict) -> float:
    """Stops only move up, never down."""
    return round(max(current_stop, highest_close - p["trail_atr"] * atr), 2)
