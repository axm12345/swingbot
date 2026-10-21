# SwingBot

A once-a-day swing trading bot for Interactive Brokers. It holds liquid, more volatile
ETFs for days to weeks, always with a stop-loss sitting at the broker, and it sits in
cash when the market is weak. It does **not** promise weekly profit, and nothing can.
Its job is to keep losses small and let trends run.

## What it does each day

1. Pulls daily prices for the ETFs in `config.yaml` and for SPY.
2. **Market check.** If SPY is below its 200-day average, it buys nothing.
3. **Sells** anything that closed below its 50-day average.
4. **Raises stop-losses** on winners (stops never move down).
5. **Buys** the strongest uptrending ETFs (max 4 positions), sized so a stop-out costs about 1% of the account. Every buy is sent together with its stop-loss.
6. **News filter.** A scary headline (downgrade, lawsuit, probe, etc.) blocks a new buy in that symbol. News never triggers a trade by itself.
7. **Safety switches.**
   - Down 3% this week: no new buys until next week. No panic selling, no rushing to make the week green.
   - Down 15% from the peak: the bot stops buying entirely until you look at what happened and run `reset-halt`.

Orders are market-on-open, so they fill when the US market opens.

## Setup (one time)

1. **Install Python 3.10+**, then in this folder run:
   ```
   pip install -r requirements.txt
   ```
2. **Test it on history first** (no IBKR needed):
   ```
   python backtest.py
   python backtest.py --start 2020-01-01 --capital 500
   ```
   Read the table. Compare it honestly against "Hold SPY", especially "Worst drop from peak" and "Weeks with a loss".
3. **When IBKR approves you,** install **TWS** (or IB Gateway) from IBKR's site and log in to your **paper trading** account.
4. **In TWS, enable the API.** Go to *Edit → Global Configuration → API → Settings*:
   - tick **Enable ActiveX and Socket Clients**
   - set the socket port to **7497**
   - untick **Read-Only API**
   - add `127.0.0.1` to trusted IPs
5. **Try a dry run.** It shows its decisions and sends nothing:
   ```
   python bot.py run --dry-run
   ```
6. **Run it for real on paper:**
   ```
   python bot.py run
   python bot.py status
   ```

## Running it daily

Run `python bot.py run` once a day while TWS is open and logged in, **between the US close and the next US open**. In Bangkok, around **19:00** works all year. Running it twice on the same day is safe, because it won't double-buy.

- **Windows:** Task Scheduler → Create Basic Task → Daily 19:00 → Program `python`, arguments `bot.py run`, start in = this folder.
- **Mac/Linux:** `crontab -e`, then add `0 19 * * 1-5 cd /path/to/swingbot && python bot.py run`

Your computer and TWS must be on at that time. TWS logs out once a day, so set its auto-restart in *Configure → Lock and Exit*.

## Adding money

Whenever you deposit, tell the bot so it doesn't mistake the deposit for profit:
```
python bot.py deposit 100      # added $100
python bot.py deposit -50      # withdrew $50
```
If you deposit THB, convert it to USD inside IBKR, because the bot trades US ETFs.

## Checking results

```
python bot.py report
```
This shows profit per week (deposits excluded) next to SPY. Logs are in `logs/`, every order is in `journal/trades.csv`.

## Going live (only after weeks of paper trading)

Wait until the paper results over at least 2–3 months are something you'd accept with real money, losses included. Then:
1. Set `mode: live` and `port: 7496` in `config.yaml`.
2. Log TWS into your real account.
3. Run `python bot.py run --live`. The bot refuses to trade live without both the config change and the flag.

Start with an amount you could lose entirely without it hurting you.

## Things to know

- **Small accounts.** With whole shares, a $500 account can't buy a $500 ETF and still respect the 30% cap. The bot will say "account too small" and skip it. That's working as intended.
- **Market data.** `data_source: yahoo` is free and fine for daily bars. `ib` may need a paid US market data subscription.
- **News filter.** It needs at least one news provider enabled on your IBKR account. If none is available, the bot just skips this check.
- **Changing settings.** You can change any setting in `config.yaml`, but re-run the backtest after every change. Tuning settings until the backtest looks amazing usually makes the future worse, not better.
- **Taxes and rules.** Tax and remittance rules (Thailand, India, US dividend withholding) are your responsibility.

This is a tool, not financial advice. Past backtest results don't predict future returns.
