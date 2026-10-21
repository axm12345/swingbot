"""Offline tests: synthetic prices + a fake broker. Run: python -m pytest -q"""
import datetime as dt
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import backtest as BT  # noqa: E402
import bot  # noqa: E402
import planner as P  # noqa: E402
import risk as R  # noqa: E402
import strategy as S  # noqa: E402

CFG = yaml.safe_load((ROOT / "config.yaml").read_text())


def synth(seed: int, n: int = 1500, drift: float = 0.0004, vol: float = 0.015, end=None) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    end = end or (pd.Timestamp.today().normalize() - pd.offsets.BDay(1))
    idx = pd.bdate_range(end=end, periods=n)
    r = rng.normal(drift, vol, n)
    close = 100 * np.exp(np.cumsum(r))
    opn = close * np.exp(rng.normal(0, vol / 3, n))
    high = np.maximum(opn, close) * (1 + np.abs(rng.normal(0, vol / 2, n)))
    low = np.minimum(opn, close) * (1 - np.abs(rng.normal(0, vol / 2, n)))
    return pd.DataFrame({"open": opn, "high": high, "low": low, "close": close, "volume": 1e6}, index=idx)


def frames():
    syms = CFG["universe"] + [CFG["market_symbol"]]
    return {s: synth(i, drift=0.0006 if i % 2 else 0.0001, vol=0.012 + 0.002 * (i % 4)) for i, s in enumerate(syms)}


# ---------- risk ----------
def test_position_size_risks_about_one_percent():
    p = dict(CFG["risk"], stop_atr_for_sizing=2.0)
    qty = R.position_size(10_000, 10_000, price=50, atr=1.0, p=p)
    assert qty == 50  # $100 risk / $2 stop distance
    assert qty * 2.0 <= 10_000 * 0.01 + 1e-9


def test_position_size_respects_cash_and_cap():
    p = dict(CFG["risk"], stop_atr_for_sizing=2.0)
    assert R.position_size(1000, 1000, price=500, atr=0.5, p=p) == 0  # 30% cap = $300 < 1 share
    assert R.position_size(10_000, 100, price=50, atr=0.1, p=p) == 1  # limited by cash


def test_weekly_limit_and_drawdown_halt():
    st = {}
    R.update_account_marks(st, 1000, dt.date(2026, 10, 12))
    assert R.entries_allowed(st, 980, CFG["risk"])[0]
    assert not R.entries_allowed(st, 960, CFG["risk"])[0]  # -4% this week
    R.update_account_marks(st, 1000, dt.date(2026, 10, 19))  # new week resets
    assert R.entries_allowed(st, 1000, CFG["risk"])[0]
    assert not R.entries_allowed(st, 840, CFG["risk"])[0]  # -16% from peak
    assert st["halted"]


def test_deposit_is_not_profit():
    st = {"week_start_equity": 1000, "peak_equity": 1000}
    R.record_deposit(st, 500)
    assert st["peak_equity"] == 1500 and st["week_start_equity"] == 1500


def test_stop_never_moves_down():
    p = CFG["strategy"]
    assert S.trailed_stop(95.0, 96.0, 1.0, p) == 95.0
    assert S.trailed_stop(95.0, 110.0, 1.0, p) == 107.0


# ---------- planner ----------
def test_no_buys_when_market_unhealthy():
    fr = {s: S.add_indicators(df, CFG["strategy"]) for s, df in frames().items()}
    spy_row = fr["SPY"].iloc[-1].copy()
    spy_row["close"] = spy_row["sma_regime"] * 0.9
    plan = P.make_plan({}, {s: fr[s] for s in CFG["universe"]}, spy_row, {}, 1000, 1000, CFG, dt.date.today())
    assert not plan.entries


def test_news_blocks_buy():
    fr = {s: S.add_indicators(df, CFG["strategy"]) for s, df in frames().items()}
    spy_row = fr["SPY"].iloc[-1].copy()
    spy_row["close"] = spy_row["sma_regime"] * 1.1
    data = {s: fr[s] for s in CFG["universe"]}
    base = P.make_plan({}, data, spy_row, {}, 100_000, 100_000, CFG, dt.date.today())
    if not base.entries:
        pytest.skip("no entries on this synthetic day")
    sym = base.entries[0].symbol
    plan = P.make_plan({}, data, spy_row, {}, 100_000, 100_000, CFG, dt.date.today(), {sym: "SEC probe"})
    assert sym not in {e.symbol for e in plan.entries}


# ---------- backtest ----------
def test_backtest_runs_and_never_overspends():
    fr = frames()
    start = fr["SPY"].index[300].strftime("%Y-%m-%d")
    res = BT.run_backtest(CFG, fr, start, 1000)
    eq = res["equity"]["equity"]
    assert len(eq) > 1000 and (eq > 0).all()
    BT.print_report(res, 1000)


# ---------- full bot run with a fake broker ----------
class FakeBroker:
    def __init__(self, cfg):
        self.account = "DU1234567"
        self.orders = []
        self.stops = {}

    def equity_and_cash_usd(self):
        return 2000.0, 2000.0

    def holdings(self):
        return {}

    def stop_trades(self):
        return {}

    def pending_symbols(self):
        return set()

    def buy_with_stop(self, s, q, stop):
        self.orders.append(("BUY", s, q, stop))

    def sell_on_open(self, s, q):
        self.orders.append(("SELL", s, q))

    def set_stop(self, s, q, stop):
        self.orders.append(("STOP", s, q, stop))

    def cancel_stop(self, s):
        pass

    def headlines(self, s, h):
        return ["Analyst downgrade hits shares"] if s == "XBI" else []

    def sleep(self, s):
        pass

    def close(self):
        pass


def test_bot_run_end_to_end(tmp_path, monkeypatch):
    fr = frames()
    monkeypatch.setattr(bot.D, "load", lambda sym, *a, **k: fr[sym])
    monkeypatch.setattr(bot, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(bot, "JOURNAL", tmp_path / "journal")
    fb = FakeBroker(CFG)
    args = type("A", (), {"live": False, "dry_run": False, "force": False})()
    bot.setup_logging()
    bot.cmd_run(args, broker_factory=lambda cfg: fb)
    assert all(o[0] == "BUY" and o[1] != "XBI" for o in fb.orders)
    assert (tmp_path / "journal" / "equity.csv").exists()
    # Second run on the same data must not double-buy.
    n = len(fb.orders)
    bot.cmd_run(args, broker_factory=lambda cfg: fb)
    assert len(fb.orders) == n


def test_live_requires_flag_and_live_port():
    cfg = dict(CFG, mode="live", ib=dict(CFG["ib"], port=7496))
    with pytest.raises(SystemExit):
        bot.safety_check(cfg, live_flag=False)
    with pytest.raises(SystemExit):
        bot.safety_check(dict(CFG), live_flag=True)
    with pytest.raises(SystemExit):
        bot.safety_check(dict(CFG, ib=dict(CFG["ib"], port=7496)), live_flag=False)
