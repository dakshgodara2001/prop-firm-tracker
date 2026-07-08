"""Post-deal return tracking.

For each firm-stock-day aggregate, measure the stock's close k *sessions*
after the deal against the firm's own entry price (their buy/sell VWAP for
directional trades, the day's close for round trips):

    r_k = (close(T+k) - base) / base * 100      k in (1, 3, 5, 10)

Horizons fill in progressively as new bhavcopies are stored; run-daily calls
update_deal_returns() over a trailing window so yesterday's deals gain r1
today, r3 three sessions later, and so on. Sessions come from the symbol's own
rows in stock_history, so exchange holidays need no special handling.
"""
from __future__ import annotations

import sqlite3
from datetime import timedelta
from typing import Dict, List, Optional, Tuple

from . import config
from .aggregate import CLEAN_BUY, CLEAN_SELL, NET_BUY, NET_SELL
from .dates import ist_now, parse_iso

HORIZONS = (1, 3, 5, 10)

_SERIES_RANK = "CASE series WHEN 'EQ' THEN 0 WHEN 'BE' THEN 1 WHEN 'BZ' THEN 2 " \
               "WHEN 'SM' THEN 3 WHEN 'ST' THEN 4 ELSE 9 END"


def _closes_from(conn: sqlite3.Connection, symbol: str, start_date: str) -> List[Tuple[str, float]]:
    """[(date, close)] from start_date onward, best series per date, ascending."""
    rows = conn.execute(
        f"""SELECT trade_date, close FROM stock_history
            WHERE symbol = ? AND trade_date >= ? AND close IS NOT NULL
            ORDER BY trade_date ASC, {_SERIES_RANK} ASC""",
        (symbol, start_date),
    ).fetchall()
    closes: List[Tuple[str, float]] = []
    for r in rows:  # rows are ordered best-series-first within each date
        if not closes or closes[-1][0] != r["trade_date"]:
            closes.append((r["trade_date"], r["close"]))
    return closes


def _base_price(agg: sqlite3.Row, day_close: Optional[float]) -> Tuple[Optional[float], str]:
    cls = agg["classification"]
    if cls in (CLEAN_BUY, NET_BUY) and agg["vwap_buy"]:
        return agg["vwap_buy"], "vwap_buy"
    if cls in (CLEAN_SELL, NET_SELL) and agg["vwap_sell"]:
        return agg["vwap_sell"], "vwap_sell"
    if day_close:
        return day_close, "close"
    return None, ""


def update_deal_returns(
    conn: sqlite3.Connection,
    as_of: Optional[str] = None,
    lookback_days: Optional[int] = None,
) -> int:
    """(Re)compute returns for aggregates in the trailing window. Returns rows written."""
    lookback = config.RETURNS_LOOKBACK_DAYS if lookback_days is None else lookback_days
    end = parse_iso(as_of) if as_of else ist_now().date()
    cutoff = (end - timedelta(days=lookback)).isoformat()

    aggs = conn.execute(
        "SELECT * FROM daily_firm_stock_agg WHERE trade_date >= ? AND trade_date <= ?",
        (cutoff, end.isoformat()),
    ).fetchall()

    now = ist_now().isoformat(timespec="seconds")
    written = 0
    series_cache: Dict[Tuple[str, str], List[Tuple[str, float]]] = {}
    for agg in aggs:
        key = (agg["symbol"], agg["trade_date"])
        closes = series_cache.get(key)
        if closes is None:
            closes = _closes_from(conn, agg["symbol"], agg["trade_date"])
            series_cache[key] = closes
        # closes[0] must be the deal day itself, otherwise T+k is ambiguous.
        if not closes or closes[0][0] != agg["trade_date"]:
            continue
        base, base_kind = _base_price(agg, closes[0][1])
        if not base:
            continue
        values = {}
        for k in HORIZONS:
            values[f"r{k}"] = (
                (closes[k][1] - base) / base * 100.0 if len(closes) > k else None
            )
        conn.execute(
            """INSERT INTO deal_returns
               (agg_id, trade_date, symbol, base_price, base_kind, r1, r3, r5, r10, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(agg_id) DO UPDATE SET
                 base_price=excluded.base_price, base_kind=excluded.base_kind,
                 r1=excluded.r1, r3=excluded.r3, r5=excluded.r5, r10=excluded.r10,
                 updated_at=excluded.updated_at""",
            (
                agg["id"],
                agg["trade_date"],
                agg["symbol"],
                base,
                base_kind,
                values["r1"],
                values["r3"],
                values["r5"],
                values["r10"],
                now,
            ),
        )
        written += 1
    conn.commit()
    return written
