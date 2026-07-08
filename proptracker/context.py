"""Per (date, symbol) market context from stored price history.

Answers "how big was the day, and how big were the deals, relative to this
stock's normal liquidity": close/prev-close move, day volume vs the trailing
ADV_WINDOW-session average volume, and delivery quantity/percentage.
"""
from __future__ import annotations

import sqlite3
from typing import Optional

from . import config

# Preference when a symbol traded in several series on one day (EQ first).
_SERIES_ORDER = (
    "CASE series "
    + " ".join(
        f"WHEN '{s}' THEN {i}" for i, s in enumerate(("EQ", "BE", "BZ", "SM", "ST"))
    )
    + " ELSE 9 END"
)


def best_day_row(conn: sqlite3.Connection, symbol: str, trade_date: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        f"""SELECT * FROM stock_history
            WHERE symbol = ? AND trade_date = ?
            ORDER BY {_SERIES_ORDER} LIMIT 1""",
        (symbol, trade_date),
    ).fetchone()


def trailing_adv(
    conn: sqlite3.Connection, symbol: str, before_date: str, series: Optional[str] = None
):
    """(mean volume, sessions used) over the ADV_WINDOW sessions before the date."""
    if series:
        rows = conn.execute(
            """SELECT trade_date, volume FROM stock_history
               WHERE symbol = ? AND series = ? AND trade_date < ?
               ORDER BY trade_date DESC LIMIT ?""",
            (symbol, series, before_date, config.ADV_WINDOW),
        ).fetchall()
    else:
        rows = conn.execute(
            f"""SELECT trade_date, volume FROM stock_history
                WHERE symbol = ? AND trade_date < ?
                ORDER BY trade_date DESC, {_SERIES_ORDER} ASC""",
            (symbol, before_date),
        ).fetchall()
        # best series per date, newest ADV_WINDOW dates
        picked = []
        for r in rows:
            if not picked or picked[-1]["trade_date"] != r["trade_date"]:
                picked.append(r)
            if len(picked) >= config.ADV_WINDOW:
                break
        rows = picked
    volumes = [r["volume"] for r in rows if r["volume"]]
    if len(volumes) < config.MIN_ADV_DAYS:
        return None, len(volumes)
    return sum(volumes) / len(volumes), len(volumes)


def compute_market_context(conn: sqlite3.Connection, trade_date: str) -> int:
    """(Re)build market_context for every symbol with deals on the date."""
    conn.execute("DELETE FROM market_context WHERE trade_date = ?", (trade_date,))
    symbols = [
        r["symbol"]
        for r in conn.execute(
            "SELECT DISTINCT symbol FROM raw_deals WHERE trade_date = ? ORDER BY symbol",
            (trade_date,),
        )
    ]
    written = 0
    for symbol in symbols:
        day = best_day_row(conn, symbol, trade_date)
        if day is None:
            continue  # no NSE price history (e.g. BSE-only listing)
        adv, adv_days = trailing_adv(conn, symbol, trade_date, day["series"])
        close, prev = day["close"], day["prev_close"]
        day_return = (
            (close - prev) / prev * 100.0 if close is not None and prev else None
        )
        volume_vs_adv = (day["volume"] / adv) if adv else None
        conn.execute(
            """INSERT INTO market_context
               (trade_date, symbol, series, close, prev_close, day_return_pct,
                volume, turnover, deliv_qty, deliv_per, adv20, adv_days, volume_vs_adv)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                trade_date,
                symbol,
                day["series"],
                close,
                prev,
                day_return,
                day["volume"],
                day["turnover"],
                day["deliv_qty"],
                day["deliv_per"],
                adv,
                adv_days,
                volume_vs_adv,
            ),
        )
        written += 1
    conn.commit()
    return written
