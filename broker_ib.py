"""Thin wrapper around Interactive Brokers (TWS / IB Gateway) via ib_async."""
from __future__ import annotations

import datetime as dt
import logging

import pandas as pd
from ib_async import IB, MarketOrder, Stock, StopOrder, util

log = logging.getLogger("swingbot")

LIVE_PORTS = {7496, 4001}
PAPER_PORTS = {7497, 4002}


class IBBroker:
    def __init__(self, host: str, port: int, client_id: int):
        self.ib = IB()
        self.ib.connect(host, port, clientId=client_id, timeout=20)
        self._contracts: dict[str, Stock] = {}
        accts = self.ib.managedAccounts()
        self.account = accts[0] if accts else ""
        log.info("Connected to IBKR account %s on port %s", self.account, port)

    def close(self):
        self.ib.disconnect()

    # ---------- contracts & data ----------
    def contract(self, symbol: str) -> Stock:
        if symbol not in self._contracts:
            c = Stock(symbol, "SMART", "USD")
            self.ib.qualifyContracts(c)
            self._contracts[symbol] = c
        return self._contracts[symbol]

    def daily_bars(self, symbol: str) -> pd.DataFrame:
        bars = self.ib.reqHistoricalData(
            self.contract(symbol), endDateTime="", durationStr="2 Y", barSizeSetting="1 day",
            whatToShow="ADJUSTED_LAST", useRTH=True, formatDate=1,
        )
        if not bars:
            raise RuntimeError(f"IBKR returned no bars for {symbol} (market data subscription?)")
        df = util.df(bars)
        df.index = pd.to_datetime(df["date"])
        return df[["open", "high", "low", "close", "volume"]]

    # ---------- account ----------
    def _usd_rate(self) -> float:
        """How many base-currency units one USD is worth (1.0 if base is USD)."""
        for v in self.ib.accountValues(self.account):
            if v.tag == "ExchangeRate" and v.currency == "USD":
                return float(v.value)
        return 1.0

    def equity_and_cash_usd(self) -> tuple[float, float]:
        vals = {(v.tag, v.currency): v.value for v in self.ib.accountSummary(self.account)}
        base = next((cur for (tag, cur) in vals if tag == "NetLiquidation"), "USD")
        equity = float(vals[("NetLiquidation", base)])
        cash = float(vals.get(("TotalCashValue", base), 0))
        rate = self._usd_rate() if base != "USD" else 1.0
        return equity / rate, cash / rate

    def holdings(self) -> dict[str, float]:
        out = {}
        for p in self.ib.positions(self.account):
            if p.contract.secType == "STK" and p.position != 0:
                out[p.contract.symbol] = float(p.position)
        return out

    # ---------- orders ----------
    def open_trades(self):
        self.ib.reqOpenOrders()
        return [t for t in self.ib.openTrades() if not t.isDone()]

    def stop_trades(self) -> dict[str, object]:
        return {t.contract.symbol: t for t in self.open_trades()
                if t.order.orderType == "STP" and t.order.action == "SELL"}

    def pending_symbols(self) -> set[str]:
        return {t.contract.symbol for t in self.open_trades() if t.order.orderType != "STP"}

    def buy_with_stop(self, symbol: str, qty: float, stop: float):
        """Market-on-open buy with a GTC stop-loss attached, so it's protected the moment it fills."""
        c = self.contract(symbol)
        parent = MarketOrder("BUY", qty, tif="OPG")
        parent.orderId = self.ib.client.getReqId()
        parent.transmit = False
        child = StopOrder("SELL", qty, stop, tif="GTC")
        child.parentId = parent.orderId
        child.transmit = True
        self.ib.placeOrder(c, parent)
        self.ib.placeOrder(c, child)

    def sell_on_open(self, symbol: str, qty: float):
        self.ib.placeOrder(self.contract(symbol), MarketOrder("SELL", qty, tif="OPG"))

    def set_stop(self, symbol: str, qty: float, stop: float):
        existing = self.stop_trades().get(symbol)
        if existing:
            existing.order.auxPrice = stop
            existing.order.totalQuantity = qty
            self.ib.placeOrder(existing.contract, existing.order)
        else:
            self.ib.placeOrder(self.contract(symbol), StopOrder("SELL", qty, stop, tif="GTC"))

    def cancel_stop(self, symbol: str):
        t = self.stop_trades().get(symbol)
        if t:
            self.ib.cancelOrder(t.order)

    def sleep(self, s: float):
        self.ib.sleep(s)

    # ---------- news ----------
    def headlines(self, symbol: str, hours: int) -> list[str]:
        providers = self.ib.reqNewsProviders()
        if not providers:
            return []
        codes = "+".join(p.code for p in providers)
        end = dt.datetime.now(dt.timezone.utc)
        start = end - dt.timedelta(hours=hours)
        items = self.ib.reqHistoricalNews(self.contract(symbol).conId, codes, start, end, 20)
        return [n.headline for n in items or []]
