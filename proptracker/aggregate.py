"""Aggregation and trade classification, at two levels.

Firm level — (firm, symbol, deal type) per day:

  CLEAN_BUY  — only buys disclosed
  CLEAN_SELL — only sells disclosed
  ROUND_TRIP — both sides and the residual is small:
               |net| <= ROUND_TRIP_NET_RATIO * gross  (intraday churn, noise)
  NET_BUY    — both sides but a meaningful net long remains
  NET_SELL   — both sides but a meaningful net short/sold remains

Stock level — (symbol) per day across every tracked firm (the discovery
view: "which stocks did tracked firms touch?"). The label describes the
*directional* flow only; round-trip churn is carried alongside as
liquidity activity, never as bullish/bearish:

  CLEAN_BUY  — tracked firms only bought (all of them cleanly)
  CLEAN_SELL — tracked firms only sold
  NET_BUY    — buy-side flow dominates (partial: some selling too)
  NET_SELL   — sell-side flow dominates
  MIXED      — firms took directional positions on *both* sides that
               largely offset (disagreement, not churn)
  ROUND_TRIP — every firm's activity was round-trip churn
"""
from __future__ import annotations

import sqlite3
from typing import Optional

from . import config

CLEAN_BUY = "CLEAN_BUY"
CLEAN_SELL = "CLEAN_SELL"
NET_BUY = "NET_BUY"
NET_SELL = "NET_SELL"
ROUND_TRIP = "ROUND_TRIP"
MIXED = "MIXED"  # stock-level only: directional firms on both sides

# Firm-level classes that represent a held (directional) position vs churn.
DIRECTIONAL_CLASSES = (CLEAN_BUY, CLEAN_SELL, NET_BUY, NET_SELL)
NOISE_CLASSES = (ROUND_TRIP,)
# Stock-level flow labels that are directional (everything except churn-only).
STOCK_DIRECTIONAL_CLASSES = (CLEAN_BUY, CLEAN_SELL, NET_BUY, NET_SELL, MIXED)

# Human labels shared by the report, dashboard, alerts and CLI.
CLASS_LABELS = {
    CLEAN_BUY: "Clean buy",
    CLEAN_SELL: "Clean sell",
    NET_BUY: "Partial net buy",
    NET_SELL: "Partial net sell",
    MIXED: "Mixed",
    ROUND_TRIP: "Round-trip",
}


def classify(buy_qty: int, sell_qty: int, round_trip_ratio: Optional[float] = None) -> str:
    ratio_limit = config.ROUND_TRIP_NET_RATIO if round_trip_ratio is None else round_trip_ratio
    if buy_qty > 0 and sell_qty == 0:
        return CLEAN_BUY
    if sell_qty > 0 and buy_qty == 0:
        return CLEAN_SELL
    gross = buy_qty + sell_qty
    net = buy_qty - sell_qty
    if gross == 0:
        return ROUND_TRIP  # degenerate; cannot occur for rows built from real deals
    if abs(net) <= ratio_limit * gross:
        return ROUND_TRIP
    return NET_BUY if net > 0 else NET_SELL


def classify_stock(
    dir_buy_qty: int,
    dir_sell_qty: int,
    all_buyers_clean: bool,
    all_sellers_clean: bool,
    round_trip_ratio: Optional[float] = None,
) -> str:
    """Stock-level flow label from the directional (non-churn) firm positions."""
    ratio_limit = config.ROUND_TRIP_NET_RATIO if round_trip_ratio is None else round_trip_ratio
    if dir_buy_qty == 0 and dir_sell_qty == 0:
        return ROUND_TRIP  # churn only
    if dir_sell_qty == 0:
        return CLEAN_BUY if all_buyers_clean else NET_BUY
    if dir_buy_qty == 0:
        return CLEAN_SELL if all_sellers_clean else NET_SELL
    gross = dir_buy_qty + dir_sell_qty
    net = dir_buy_qty - dir_sell_qty
    if abs(net) <= ratio_limit * gross:
        return MIXED
    return NET_BUY if net > 0 else NET_SELL


def aggregate_stocks_date(conn: sqlite3.Connection, trade_date: str) -> int:
    """(Re)build the stock-first daily_stock_agg view for one date.

    Rolls daily_firm_stock_agg up per symbol across firms and deal types,
    separating directional flow from round-trip churn, and flags symbols
    appearing in tracked-firm deals for the first time in stored history.
    Returns rows written.
    """
    conn.execute("DELETE FROM daily_stock_agg WHERE trade_date = ?", (trade_date,))
    rows = conn.execute(
        """SELECT a.*, f.name AS firm_name
           FROM daily_firm_stock_agg a JOIN firms f ON f.id = a.firm_id
           WHERE a.trade_date = ?""",
        (trade_date,),
    ).fetchall()

    stocks: dict = {}
    for r in rows:
        s = stocks.setdefault(
            r["symbol"],
            {
                "firms": set(),
                "buy_qty": 0, "sell_qty": 0,
                "buy_value": 0.0, "sell_value": 0.0,
                "dir_buy_qty": 0, "dir_sell_qty": 0,
                "churn_firms": set(), "churn_gross_value": 0.0,
                "buyers_all_clean": True, "sellers_all_clean": True,
            },
        )
        s["firms"].add(r["firm_name"])
        s["buy_qty"] += r["buy_qty"]
        s["sell_qty"] += r["sell_qty"]
        s["buy_value"] += r["buy_value"]
        s["sell_value"] += r["sell_value"]
        if r["classification"] == ROUND_TRIP:
            s["churn_firms"].add(r["firm_name"])
            s["churn_gross_value"] += r["buy_value"] + r["sell_value"]
        elif r["net_qty"] > 0:
            s["dir_buy_qty"] += r["net_qty"]
            if r["classification"] != CLEAN_BUY:
                s["buyers_all_clean"] = False
        elif r["net_qty"] < 0:
            s["dir_sell_qty"] += -r["net_qty"]
            if r["classification"] != CLEAN_SELL:
                s["sellers_all_clean"] = False

    written = 0
    for symbol, s in sorted(stocks.items()):
        name_row = conn.execute(
            """SELECT security_name FROM raw_deals
               WHERE trade_date = ? AND symbol = ? AND security_name != ''
               LIMIT 1""",
            (trade_date, symbol),
        ).fetchone()
        first_time = not conn.execute(
            "SELECT 1 FROM daily_firm_stock_agg WHERE symbol = ? AND trade_date < ? LIMIT 1",
            (symbol, trade_date),
        ).fetchone()
        conn.execute(
            """INSERT INTO daily_stock_agg
               (trade_date, symbol, security_name, firm_count, firm_names,
                buy_qty, sell_qty, buy_value, sell_value, gross_value, net_value,
                net_qty, dir_buy_qty, dir_sell_qty, churn_firm_count,
                churn_gross_value, classification, first_time)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                trade_date,
                symbol,
                name_row["security_name"] if name_row else "",
                len(s["firms"]),
                ", ".join(sorted(s["firms"])),
                s["buy_qty"],
                s["sell_qty"],
                s["buy_value"],
                s["sell_value"],
                s["buy_value"] + s["sell_value"],
                s["buy_value"] - s["sell_value"],
                s["buy_qty"] - s["sell_qty"],
                s["dir_buy_qty"],
                s["dir_sell_qty"],
                len(s["churn_firms"]),
                s["churn_gross_value"],
                classify_stock(
                    s["dir_buy_qty"], s["dir_sell_qty"],
                    s["buyers_all_clean"], s["sellers_all_clean"],
                ),
                1 if first_time else 0,
            ),
        )
        written += 1
    conn.commit()
    return written


def aggregate_date(conn: sqlite3.Connection, trade_date: str) -> int:
    """(Re)build daily_firm_stock_agg for one date. Returns rows written."""
    conn.execute("DELETE FROM daily_firm_stock_agg WHERE trade_date = ?", (trade_date,))
    rows = conn.execute(
        """SELECT m.firm_id, r.symbol, r.deal_type, r.side,
                  SUM(r.quantity)                          AS qty,
                  SUM(r.quantity * COALESCE(r.price, 0))   AS value,
                  COUNT(*)                                 AS trades
           FROM matched_deals m
           JOIN raw_deals r ON r.id = m.raw_deal_id
           WHERE r.trade_date = ? AND m.firm_id IS NOT NULL
             AND r.side IN ('BUY', 'SELL')
           GROUP BY m.firm_id, r.symbol, r.deal_type, r.side""",
        (trade_date,),
    ).fetchall()

    buckets = {}
    for r in rows:
        key = (r["firm_id"], r["symbol"], r["deal_type"])
        b = buckets.setdefault(
            key,
            {"buy_qty": 0, "sell_qty": 0, "buy_value": 0.0, "sell_value": 0.0,
             "buy_trades": 0, "sell_trades": 0},
        )
        side = r["side"].lower()
        b[f"{side}_qty"] += r["qty"]
        b[f"{side}_value"] += r["value"]
        b[f"{side}_trades"] += r["trades"]

    written = 0
    for (firm_id, symbol, deal_type), b in sorted(buckets.items(), key=lambda kv: kv[0][1]):
        buy_qty, sell_qty = b["buy_qty"], b["sell_qty"]
        conn.execute(
            """INSERT INTO daily_firm_stock_agg
               (trade_date, firm_id, symbol, deal_type,
                buy_qty, sell_qty, buy_value, sell_value, buy_trades, sell_trades,
                net_qty, gross_qty, net_value, vwap_buy, vwap_sell, classification)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                trade_date,
                firm_id,
                symbol,
                deal_type,
                buy_qty,
                sell_qty,
                b["buy_value"],
                b["sell_value"],
                b["buy_trades"],
                b["sell_trades"],
                buy_qty - sell_qty,
                buy_qty + sell_qty,
                b["buy_value"] - b["sell_value"],
                (b["buy_value"] / buy_qty) if buy_qty else None,
                (b["sell_value"] / sell_qty) if sell_qty else None,
                classify(buy_qty, sell_qty),
            ),
        )
        written += 1
    conn.commit()
    return written
