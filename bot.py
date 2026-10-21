"""SwingBot: once-a-day swing trading bot for Interactive Brokers.

Run it once per day, any time after the US market closes and before it opens
(in Bangkok: roughly 05:00-19:30). It looks at yesterday's closing data,
decides, and sends orders that execute at the next US open.

Commands:
    python bot.py run --dry-run   show what it would do, send nothing
    python bot.py run             paper trading (default config)
    python bot.py run --live      real money (also needs mode: live in config.yaml)
    python bot.py status          account, positions, safety switches
    python bot.py deposit 200     tell the bot you added $200 (use -200 for a withdrawal)
    python bot.py reset-halt      re-enable buying after the drawdown kill switch
    python bot.py report          weekly results vs simply holding SPY
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import logging
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

import data as D
import news as N
import planner as P
import risk as R
import strategy as S

ROOT = Path(__file__).resolve().parent
STATE_FILE = ROOT / "state.json"
JOURNAL = ROOT / "journal"
NY = ZoneInfo("America/New_York")
log = logging.getLogger("swingbot")


# ---------------- setup ----------------
def setup_logging():
    (ROOT / "logs").mkdir(exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    log.setLevel(logging.INFO)
    if not log.handlers:
        fh = logging.FileHandler(ROOT / "logs" / "bot.log", encoding="utf-8")
        fh.setFormatter(fmt)
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        log.addHandler(fh)
        log.addHandler(sh)


def load_cfg() -> dict:
    return yaml.safe_load((ROOT / "config.yaml").read_text())


def load_state() -> dict:
    return json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}


def save_state(state: dict):
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, default=str))
    tmp.replace(STATE_FILE)


def safety_check(cfg: dict, live_flag: bool):
    from broker_ib import LIVE_PORTS, PAPER_PORTS

    mode, port = cfg["mode"], cfg["ib"]["port"]
    if mode == "live":
        if not live_flag:
            sys.exit("config says live, but you didn't pass --live. Refusing.")
        if port not in LIVE_PORTS:
            sys.exit(f"mode is live but port {port} is a paper port. Fix config.yaml.")
    else:
        if live_flag:
            sys.exit("--live passed but config.yaml says mode: paper. Change the config first, on purpose.")
        if port not in PAPER_PORTS:
            sys.exit(f"mode is paper but port {port} is a LIVE port. Refusing.")


def make_broker(cfg: dict):
    from broker_ib import IBBroker

    return IBBroker(cfg["ib"]["host"], cfg["ib"]["port"], cfg["ib"]["client_id"])


def check_account_type(broker, cfg):
    paper_acct = broker.account.startswith("DU")
    if cfg["mode"] == "paper" and broker.account and not paper_acct:
        sys.exit(f"Connected account {broker.account} is not a paper account (paper ids start with DU). Refusing.")
    if cfg["mode"] == "live" and paper_acct:
        sys.exit("mode is live but you're logged into a paper account.")


def completed_bars(df):
    """Drop today's bar if the US session isn't finished (it would be a partial bar)."""
    now = dt.datetime.now(NY)
    if len(df) and df.index[-1].date() == now.date() and now.time() < dt.time(16, 15):
        return df.iloc[:-1]
    return df


def load_market_data(cfg, broker):
    sp = cfg["strategy"]
    symbols = list(dict.fromkeys(cfg["universe"] + [cfg["market_symbol"]]))
    out = {}
    for sym in symbols:
        df = completed_bars(D.load(sym, cfg["data_source"], broker))
        out[sym] = S.add_indicators(df, sp)
    return out


def journal(name: str, row: dict):
    JOURNAL.mkdir(exist_ok=True)
    f = JOURNAL / name
    new = not f.exists()
    with f.open("a", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(row))
        if new:
            w.writeheader()
        w.writerow(row)


# ---------------- commands ----------------
def cmd_run(args, broker_factory=make_broker):
    cfg = load_cfg()
    safety_check(cfg, args.live)
    state = load_state()
    broker = broker_factory(cfg)
    try:
        check_account_type(broker, cfg)
        mkt = load_market_data(cfg, broker)
        spy = mkt[cfg["market_symbol"]]
        bar_date = spy.index[-1].date()
        age = (dt.datetime.now(NY).date() - bar_date).days
        if age > 5:
            sys.exit(f"Latest data is from {bar_date}, {age} days old. Data problem; not trading.")
        if state.get("last_bar_date") == str(bar_date) and not args.force and not args.dry_run:
            log.info("Already ran for the %s close. Nothing to do (use --force to run again).", bar_date)
            return

        equity, cash = broker.equity_and_cash_usd()
        holdings = {s: q for s, q in broker.holdings().items()}
        universe_data = {s: mkt[s] for s in cfg["universe"]}
        blocked = N.blocked_symbols(broker, cfg["universe"], cfg)

        plan_state = json.loads(json.dumps(state)) if args.dry_run else state
        plan = P.make_plan(plan_state, universe_data, spy.iloc[-1], holdings, equity, cash,
                           cfg, bar_date, blocked)

        log.info("=== %s | data through %s | equity $%.2f | cash $%.2f | %s ===",
                 cfg["mode"].upper(), bar_date, equity, cash, "DRY RUN" if args.dry_run else "LIVE ORDERS")
        for n in plan.notes:
            log.info("note: %s", n)
        for e in plan.exits:
            log.info("SELL %s x%s at next open (%s)", e.symbol, e.qty, e.reason)
        for u in plan.stop_updates:
            log.info("STOP %s -> %.2f (was %s)", u.symbol, u.new_stop, u.old_stop)
        for e in plan.entries:
            log.info("BUY  %s x%s at next open, stop %.2f (%s)", e.symbol, e.qty, e.stop, e.reason)
        if not (plan.exits or plan.stop_updates or plan.entries):
            log.info("No orders today.")
        if args.dry_run:
            return

        execute(plan, broker, state, bar_date)
        held_or_entering = set(holdings) | {e.symbol for e in plan.entries}
        for sym in broker.stop_trades():
            if sym not in held_or_entering:
                broker.cancel_stop(sym)
                log.info("Cancelled leftover stop order for %s", sym)

        state["last_bar_date"] = str(bar_date)
        save_state(state)
        journal("equity.csv", {"date": str(bar_date), "equity": round(equity, 2), "cash": round(cash, 2),
                               "spy_close": round(float(spy.iloc[-1]["close"]), 2),
                               "net_deposits": state.get("net_deposits", 0.0)})
    finally:
        broker.close()


def execute(plan: P.Plan, broker, state: dict, bar_date):
    now = dt.datetime.now().isoformat(timespec="seconds")
    pending = broker.pending_symbols()
    for e in plan.exits:
        if e.symbol in pending:
            log.info("%s already has a pending order, skipping sell", e.symbol)
            continue
        broker.cancel_stop(e.symbol)
        broker.sell_on_open(e.symbol, e.qty)
        state["positions"].pop(e.symbol, None)
        journal("trades.csv", {"time": now, "bar_date": bar_date, "action": "SELL", "symbol": e.symbol,
                               "qty": e.qty, "price_ref": "", "stop": "", "reason": e.reason})
    for u in plan.stop_updates:
        broker.set_stop(u.symbol, u.qty, u.new_stop)
        journal("trades.csv", {"time": now, "bar_date": bar_date, "action": "STOP", "symbol": u.symbol,
                               "qty": u.qty, "price_ref": "", "stop": u.new_stop, "reason": f"raised from {u.old_stop}"})
    for e in plan.entries:
        if e.symbol in pending:
            log.info("%s already has a pending order, skipping buy", e.symbol)
            continue
        broker.buy_with_stop(e.symbol, e.qty, e.stop)
        state["positions"][e.symbol] = {"stop": e.stop, "highest_close": e.ref_price, "entry_date": str(bar_date)}
        journal("trades.csv", {"time": now, "bar_date": bar_date, "action": "BUY", "symbol": e.symbol,
                               "qty": e.qty, "price_ref": e.ref_price, "stop": e.stop, "reason": e.reason})
    broker.sleep(2)  # let TWS acknowledge


def cmd_status(args):
    cfg = load_cfg()
    state = load_state()
    broker = make_broker(cfg)
    try:
        equity, cash = broker.equity_and_cash_usd()
        print(f"Account {broker.account} ({cfg['mode']}): equity ${equity:,.2f}, cash ${cash:,.2f}")
        rp = dict(cfg["risk"])
        ok, why = R.entries_allowed(dict(state), equity, rp)
        print(f"Buying allowed: {ok} ({why})")
        print(f"Week start equity: {state.get('week_start_equity')}, peak: {state.get('peak_equity')}")
        stops = broker.stop_trades()
        for sym, qty in broker.holdings().items():
            st = stops.get(sym)
            print(f"  {sym}: {qty} shares, stop {st.order.auxPrice if st else 'NONE <- check this!'}")
    finally:
        broker.close()


def cmd_deposit(args):
    state = load_state()
    R.record_deposit(state, args.amount)
    save_state(state)
    print(f"Recorded {args.amount:+.2f}. Net deposits now {state['net_deposits']:.2f}.")


def cmd_reset_halt(args):
    state = load_state()
    state["halted"] = False
    state.pop("peak_equity", None)  # peak restarts from current equity on next run
    save_state(state)
    print("Halt cleared. Peak equity will restart from the next run's equity.")


def cmd_report(args):
    import pandas as pd

    f = JOURNAL / "equity.csv"
    if not f.exists():
        sys.exit("No equity history yet. Run the bot for a while first.")
    df = pd.read_csv(f, parse_dates=["date"]).drop_duplicates("date", keep="last").set_index("date")
    # Profit excludes deposits.
    df["pnl"] = df["equity"] - df["net_deposits"]
    wk = df.resample("W-FRI").last().dropna()
    wk["bot_week"] = wk["pnl"].diff()
    wk["spy_week_pct"] = wk["spy_close"].pct_change() * 100
    print(wk[["equity", "bot_week", "spy_week_pct"]].round(2).to_string())
    total = df["pnl"].iloc[-1] - df["pnl"].iloc[0]
    start_eq = df["equity"].iloc[0]
    spy_ret = df["spy_close"].iloc[-1] / df["spy_close"].iloc[0] - 1
    print(f"\nBot profit since start: ${total:,.2f}  |  SPY over same period: {spy_ret:.2%} "
          f"(= ${start_eq * spy_ret:,.2f} on your starting equity)")
    print(f"Positive weeks: {(wk['bot_week'] > 0).sum()} of {wk['bot_week'].notna().sum()}")


def main(argv=None):
    setup_logging()
    ap = argparse.ArgumentParser(description="SwingBot for Interactive Brokers")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--dry-run", action="store_true")
    r.add_argument("--live", action="store_true")
    r.add_argument("--force", action="store_true")
    sub.add_parser("status")
    d = sub.add_parser("deposit")
    d.add_argument("amount", type=float)
    sub.add_parser("reset-halt")
    sub.add_parser("report")
    args = ap.parse_args(argv)
    {"run": cmd_run, "status": cmd_status, "deposit": cmd_deposit,
     "reset-halt": cmd_reset_halt, "report": cmd_report}[args.cmd](args)


if __name__ == "__main__":
    main()
