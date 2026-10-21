"""Position sizing and the account-level safety switches."""
from __future__ import annotations

import datetime as dt
import math


def week_key(day: dt.date) -> str:
    y, w, _ = day.isocalendar()
    return f"{y}-W{w:02d}"


def position_size(equity: float, cash: float, price: float, atr: float, p: dict) -> float:
    """How many shares to buy so a stop-out loses about risk_per_trade of the account."""
    if price <= 0 or atr <= 0 or cash <= 0:
        return 0
    stop_distance = p["stop_atr_for_sizing"] * atr
    qty = (equity * p["risk_per_trade"]) / stop_distance
    qty = min(qty, equity * p["max_position_pct"] / price, cash * 0.98 / price)
    if p.get("fractional_shares"):
        return math.floor(qty * 1000) / 1000
    return math.floor(qty)


def update_account_marks(state: dict, equity: float, today: dt.date) -> None:
    """Track week-start equity and peak equity for the safety switches."""
    wk = week_key(today)
    if state.get("week") != wk:
        state["week"] = wk
        state["week_start_equity"] = equity
    state["peak_equity"] = max(state.get("peak_equity", equity), equity)


def entries_allowed(state: dict, equity: float, p: dict) -> tuple[bool, str]:
    if state.get("halted"):
        return False, "HALTED after max drawdown. Review, then run `python bot.py reset-halt`."
    peak = state.get("peak_equity", equity)
    if peak > 0 and equity < peak * (1 - p["max_drawdown"]):
        state["halted"] = True
        return False, f"Max drawdown hit ({equity:.2f} vs peak {peak:.2f}). Bot halted new buys."
    wse = state.get("week_start_equity", equity)
    if wse > 0 and equity < wse * (1 - p["weekly_loss_limit"]):
        return False, f"Weekly loss limit hit ({equity:.2f} vs week start {wse:.2f}). No buys until next week."
    return True, "ok"


def record_deposit(state: dict, amount: float) -> None:
    """Deposits/withdrawals must not look like profits/losses to the safety switches."""
    for k in ("week_start_equity", "peak_equity"):
        if k in state:
            state[k] = max(0.0, state[k] + amount)
    state["net_deposits"] = state.get("net_deposits", 0.0) + amount
