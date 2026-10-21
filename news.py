"""News as a safety filter: a scary headline blocks NEW buys in that symbol.

The bot never buys or sells *because of* a headline. Trading on news is a race
against professionals with faster feeds; using news to stay out of trouble is not.
"""
from __future__ import annotations

import logging

log = logging.getLogger("swingbot")


def blocked_symbols(broker, symbols: list[str], cfg: dict) -> dict[str, str]:
    ncfg = cfg.get("news", {})
    if not ncfg.get("enabled") or broker is None:
        return {}
    words = [w.lower() for w in ncfg.get("risk_words", [])]
    blocked: dict[str, str] = {}
    for sym in symbols:
        try:
            for h in broker.headlines(sym, ncfg.get("lookback_hours", 36)):
                text = h.lower()
                hit = next((w for w in words if w in text), None)
                if hit:
                    blocked[sym] = f'"{h[:120]}" (matched "{hit}")'
                    break
        except Exception as e:  # news is optional; never crash the bot over it
            log.warning("News check failed for %s: %s", sym, e)
    return blocked
