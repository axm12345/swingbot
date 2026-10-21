"""Decides what to do today. Pure logic: same code drives the live bot and the backtest."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

import pandas as pd

import risk as R
import strategy as S


@dataclass
class Entry:
    symbol: str
    qty: float
    stop: float
    ref_price: float
    reason: str


@dataclass
class Exit:
    symbol: str
    qty: float
    reason: str


@dataclass
class StopUpdate:
    symbol: str
    qty: float
    new_stop: float
    old_stop: float | None


@dataclass
class Plan:
    entries: list[Entry] = field(default_factory=list)
    exits: list[Exit] = field(default_factory=list)
    stop_updates: list[StopUpdate] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def make_plan(
    state: dict,
    data: dict[str, pd.DataFrame],
    market_row: pd.Series,
    holdings: dict[str, float],
    equity: float,
    cash: float,
    cfg: dict,
    today: dt.date,
    news_blocked: dict[str, str] | None = None,
) -> Plan:
    sp, rp = cfg["strategy"], dict(cfg["risk"])
    rp["stop_atr_for_sizing"] = sp["stop_atr"]
    news_blocked = news_blocked or {}
    plan = Plan()
    meta: dict = state.setdefault("positions", {})

    R.update_account_marks(state, equity, today)

    # Forget positions the broker no longer holds (stop hit, or sold by hand).
    for sym in list(meta):
        if holdings.get(sym, 0) <= 0:
            plan.notes.append(f"{sym}: no longer held (stop-loss filled or sold manually).")
            del meta[sym]

    # 1) Manage what we hold.
    exiting = set()
    for sym, qty in holdings.items():
        if qty <= 0:
            continue
        if sym not in data:
            plan.notes.append(f"{sym}: held but not in the universe, bot leaves it alone.")
            continue
        row = data[sym].iloc[-1]
        m = meta.get(sym)
        if m is None:  # adopted position (e.g. bot state was lost): protect it
            m = meta[sym] = {"stop": S.initial_stop(row["close"], row["atr"], sp),
                             "highest_close": float(row["close"]), "entry_date": str(today)}
            plan.notes.append(f"{sym}: adopted existing position with a stop at {m['stop']}.")
            plan.stop_updates.append(StopUpdate(sym, qty, m["stop"], None))
        do_exit, why = S.exit_signal(row)
        if do_exit:
            plan.exits.append(Exit(sym, qty, why))
            exiting.add(sym)
            continue
        m["highest_close"] = max(m["highest_close"], float(row["close"]))
        new_stop = S.trailed_stop(m["stop"], m["highest_close"], float(row["atr"]), sp)
        if new_stop > m["stop"]:
            plan.stop_updates.append(StopUpdate(sym, qty, new_stop, m["stop"]))
            m["stop"] = new_stop

    # 2) Look for new buys.
    allowed, why = R.entries_allowed(state, equity, rp)
    if not allowed:
        plan.notes.append(why)
        return plan
    if not S.market_ok(market_row):
        plan.notes.append("Market (SPY) below its 200-day average: no new buys, sitting in cash.")
        return plan

    held_after = {s for s, q in holdings.items() if q > 0} - exiting
    slots = rp["max_positions"] - len(held_after)
    if slots <= 0:
        return plan

    candidates = []
    for sym, df in data.items():
        if sym in held_after or sym in exiting:
            continue
        row = df.iloc[-1]
        ok, reason = S.entry_signal(row, sp)
        if not ok:
            continue
        if sym in news_blocked:
            plan.notes.append(f"{sym}: buy skipped, risky headline: {news_blocked[sym]}")
            continue
        candidates.append((S.score(row), sym, row))
    candidates.sort(key=lambda t: t[0], reverse=True)

    free_cash = cash
    for _, sym, row in candidates[:slots]:
        price, atr = float(row["close"]), float(row["atr"])
        qty = R.position_size(equity, free_cash, price, atr, rp)
        if qty <= 0:
            plan.notes.append(f"{sym}: signal, but account too small for even 1 share at ${price:.2f}.")
            continue
        free_cash -= qty * price
        plan.entries.append(Entry(sym, qty, S.initial_stop(price, atr, sp), price,
                                  f"uptrend, 3-mo momentum {row['mom']:.1%}"))
    if not candidates:
        plan.notes.append("No symbol passes the buy rules today. Doing nothing is a valid decision.")
    return plan
