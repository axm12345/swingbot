"""Backtest the exact same planner on historical daily data.

    python backtest.py                     # Yahoo data, settings from config.yaml
    python backtest.py --source csv --csv-dir mydata
    python backtest.py --start 2020-01-01 --capital 500

Fills: signals at the close, orders fill at the next day's open, stops fill at
the stop price (or the open if the price gapped below it). Commissions and
slippage from config.yaml are charged. Past results don't predict the future.
"""
from __future__ import annotations

import argparse
import datetime as dt

import numpy as np
import pandas as pd
import yaml

import data as D
import planner as P
import strategy as S


def commission(qty: float, c: dict) -> float:
    return max(c["commission_min"], qty * c["commission_per_share"])


def run_backtest(cfg: dict, frames: dict[str, pd.DataFrame], start: str, capital: float,
                 auto_reset_halt: bool = False) -> dict:
    sp, costs = cfg["strategy"], cfg["costs"]
    slip = costs["slippage_pct"]
    mkt_sym = cfg["market_symbol"]
    ind = {s: S.add_indicators(df, sp) for s, df in frames.items()}
    dates = ind[mkt_sym].loc[start:].index

    cash = capital
    pos: dict[str, dict] = {}  # sym -> {"qty", "stop", "entry_px"}
    state: dict = {}
    pending: P.Plan | None = None
    equity_rows, trades = [], []

    for d in dates:
        bar = {s: df.loc[d] for s, df in ind.items() if d in df.index}

        # --- morning: execute yesterday's plan at today's open ---
        if pending is not None:
            for e in pending.exits:
                if e.symbol in pos and e.symbol in bar:
                    p = pos.pop(e.symbol)
                    px = bar[e.symbol]["open"] * (1 - slip)
                    cash += p["qty"] * px - commission(p["qty"], costs)
                    trades.append({"symbol": e.symbol, "exit_date": d, "pnl": (px - p["entry_px"]) * p["qty"],
                                   "how": "rule"})
            for u in pending.stop_updates:
                if u.symbol in pos:
                    pos[u.symbol]["stop"] = u.new_stop
            for e in pending.entries:
                if e.symbol not in bar:
                    continue
                px = bar[e.symbol]["open"] * (1 + slip)
                qty = e.qty
                max_qty = (cash - commission(qty, costs)) / px
                if qty > max_qty:
                    qty = np.floor(max_qty) if not cfg["risk"].get("fractional_shares") else max_qty
                if qty <= 0:
                    continue
                cash -= qty * px + commission(qty, costs)
                pos[e.symbol] = {"qty": qty, "stop": e.stop, "entry_px": px}

        # --- during the day: stop-losses ---
        for sym in list(pos):
            if sym not in bar:
                continue
            b, p = bar[sym], pos[sym]
            if b["low"] <= p["stop"]:
                px = min(b["open"], p["stop"]) * (1 - slip)
                cash += p["qty"] * px - commission(p["qty"], costs)
                trades.append({"symbol": sym, "exit_date": d, "pnl": (px - p["entry_px"]) * p["qty"], "how": "stop"})
                del pos[sym]

        # --- evening: mark to market and plan tomorrow ---
        equity = cash + sum(p["qty"] * bar[s]["close"] for s, p in pos.items() if s in bar)
        equity_rows.append({"date": d, "equity": equity, "invested": len(pos) > 0})
        data = {s: ind[s].loc[:d] for s in cfg["universe"] if s in ind and d in ind[s].index}
        holdings = {s: p["qty"] for s, p in pos.items()}
        pending = P.make_plan(state, data, bar[mkt_sym], holdings, equity, cash, cfg, d.date())
        if state.get("halted"):
            state.setdefault("halt_dates", []).append(d.date())
            if auto_reset_halt:  # simulate you reviewing and running reset-halt the next day
                state["halted"] = False
                state["peak_equity"] = equity
            elif "halted_on" not in state:
                state["halted_on"] = d.date()

    eq = pd.DataFrame(equity_rows).set_index("date")
    spy = ind[mkt_sym].loc[dates, "close"]
    bench = capital * spy / spy.iloc[0]
    tr = pd.DataFrame(trades)
    return {"equity": eq, "bench": bench, "trades": tr, "halted_on": state.get("halted_on"),
            "halt_count": len(set(state.get("halt_dates", []))) if auto_reset_halt else 0,
            "commission_total": None}


def stats(series: pd.Series) -> dict:
    years = (series.index[-1] - series.index[0]).days / 365.25
    total = series.iloc[-1] / series.iloc[0] - 1
    cagr = (1 + total) ** (1 / years) - 1 if years > 0 else np.nan
    dd = (series / series.cummax() - 1).min()
    wk = series.resample("W-FRI").last().pct_change().dropna()
    mo = series.resample("ME").last().pct_change().dropna()
    return {
        "Total return": f"{total:.1%}",
        "Per year (CAGR)": f"{cagr:.1%}",
        "Worst drop from peak": f"{dd:.1%}",
        "Weeks with a profit": f"{(wk > 0).mean():.0%} of {len(wk)}",
        "Weeks with a loss": f"{(wk < 0).mean():.0%} of {len(wk)}",
        "Worst week": f"{wk.min():.1%}",
        "Best week": f"{wk.max():.1%}",
        "Months with a profit": f"{(mo > 0).mean():.0%} of {len(mo)}",
    }


def print_report(res: dict, capital: float):
    eq, bench, tr = res["equity"]["equity"], res["bench"], res["trades"]
    b, s = stats(eq), stats(bench)
    print(f"\nBacktest {eq.index[0].date()} -> {eq.index[-1].date()}, starting with ${capital:,.0f}\n")
    print(f"{'':26}{'SwingBot':>14}{'Hold SPY':>14}")
    for k in b:
        print(f"{k:26}{b[k]:>14}{s[k]:>14}")
    print(f"{'Final value':26}{'$' + format(eq.iloc[-1], ',.0f'):>14}{'$' + format(bench.iloc[-1], ',.0f'):>14}")
    if len(tr):
        wins = (tr["pnl"] > 0).mean()
        print(f"\nTrades: {len(tr)} | winners: {wins:.0%} | stopped out: {(tr['how'] == 'stop').mean():.0%}"
              f" | avg win ${tr.loc[tr.pnl > 0, 'pnl'].mean():.2f} | avg loss ${tr.loc[tr.pnl <= 0, 'pnl'].mean():.2f}")
    print(f"Time invested: {res['equity']['invested'].mean():.0%} of days (cash weeks count as neither profit nor loss)")
    if res.get("halted_on"):
        print(f"!! Max-drawdown kill switch fired on {res['halted_on']}; the bot stopped buying after that.")
    if res.get("halt_count"):
        print(f"Kill switch fired {res['halt_count']} time(s); this run assumed you reset it each time.")
    print()


def main(argv=None):
    cfg = yaml.safe_load(open("config.yaml"))
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default="yahoo", choices=["yahoo", "csv"])
    ap.add_argument("--csv-dir", default="data")
    ap.add_argument("--start", default=cfg["backtest"]["start"])
    ap.add_argument("--capital", type=float, default=cfg["backtest"]["start_capital"])
    ap.add_argument("--save", default="backtest_equity.csv")
    ap.add_argument("--no-halt", action="store_true",
                    help="research mode: assume you reset the drawdown kill switch whenever it fires")
    ap.add_argument("--commission-min", type=float,
                    help="minimum commission per order (IBKR Fixed = 1.00, Tiered = 0.35, 0 = free)")
    a = ap.parse_args(argv)
    if a.commission_min is not None:
        cfg["costs"]["commission_min"] = a.commission_min
        if a.commission_min == 0:
            cfg["costs"]["commission_per_share"] = 0.0
    print(f"Commission: ${cfg['costs']['commission_min']:.2f} min per order"
          f"{' | kill switch auto-reset (research mode)' if a.no_halt else ''}")
    warmup = (pd.Timestamp(a.start) - pd.Timedelta(days=420)).strftime("%Y-%m-%d")
    syms = list(dict.fromkeys(cfg["universe"] + [cfg["market_symbol"]]))
    frames = {s: D.load(s, a.source, start=warmup, csv_dir=a.csv_dir) for s in syms}
    res = run_backtest(cfg, frames, a.start, a.capital, auto_reset_halt=a.no_halt)
    print_report(res, a.capital)
    out = res["equity"].assign(spy_hold=res["bench"])
    out.to_csv(a.save)
    print(f"Daily equity saved to {a.save}")


if __name__ == "__main__":
    main()
