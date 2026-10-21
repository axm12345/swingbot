"""Daily price bars from Yahoo (free) or IBKR."""
from __future__ import annotations

from pathlib import Path

import pandas as pd

COLS = ["open", "high", "low", "close", "volume"]


def from_yahoo(symbol: str, start: str = "2014-01-01") -> pd.DataFrame:
    import yfinance as yf  # optional dependency

    df = yf.download(symbol, start=start, auto_adjust=True, progress=False, multi_level_index=False)
    if df is None or df.empty:
        raise RuntimeError(f"No Yahoo data for {symbol}")
    df = df.rename(columns=str.lower)[COLS]
    df.index = pd.to_datetime(df.index).tz_localize(None)
    return df.dropna()


def from_csv(folder: str | Path, symbol: str) -> pd.DataFrame:
    df = pd.read_csv(Path(folder) / f"{symbol}.csv", parse_dates=["date"], index_col="date")
    df.columns = [c.lower() for c in df.columns]
    return df[COLS].sort_index().dropna()


def load(symbol: str, source: str, broker=None, start: str = "2014-01-01", csv_dir=None) -> pd.DataFrame:
    if source == "yahoo":
        return from_yahoo(symbol, start)
    if source == "ib":
        return broker.daily_bars(symbol)
    if source == "csv":
        return from_csv(csv_dir, symbol)
    raise ValueError(f"unknown data source {source}")
