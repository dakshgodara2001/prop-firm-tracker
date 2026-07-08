"""Historical firm performance summary.

Aggregates everything stored so far per firm: how often they appear, how much
of their activity is round-trip churn vs directional positions, and how their
directional calls worked out (direction-adjusted post-deal returns: a sell
followed by a falling price counts as a win).
"""
from __future__ import annotations

import sqlite3
from typing import List, Optional

from .aggregate import DIRECTIONAL_CLASSES


def firm_performance(conn: sqlite3.Connection, since: Optional[str] = None) -> List[dict]:
    params: list = []
    where = ""
    if since:
        where = "WHERE a.trade_date >= ?"
        params.append(since)
    rows = conn.execute(
        f"""SELECT f.name AS firm, a.trade_date, a.symbol, a.classification,
                   a.net_qty, a.net_value, a.buy_value, a.sell_value,
                   r.r1, r.r5
            FROM daily_firm_stock_agg a
            JOIN firms f ON f.id = a.firm_id
            LEFT JOIN deal_returns r ON r.agg_id = a.id
            {where}""",
        params,
    ).fetchall()

    firms: dict = {}
    for r in rows:
        s = firms.setdefault(
            r["firm"],
            {
                "firm": r["firm"],
                "days": set(),
                "symbols": set(),
                "rows": 0,
                "signal_rows": 0,
                "churn_rows": 0,
                "gross_value": 0.0,
                "net_value": 0.0,
                "adj_r1": [],
                "adj_r5": [],
            },
        )
        s["days"].add(r["trade_date"])
        s["symbols"].add(r["symbol"])
        s["rows"] += 1
        s["gross_value"] += r["buy_value"] + r["sell_value"]
        s["net_value"] += r["net_value"]
        if r["classification"] in DIRECTIONAL_CLASSES:
            s["signal_rows"] += 1
            sign = 1 if r["net_qty"] > 0 else -1
            if r["r1"] is not None:
                s["adj_r1"].append(r["r1"] * sign)
            if r["r5"] is not None:
                s["adj_r5"].append(r["r5"] * sign)
        else:
            s["churn_rows"] += 1

    out = []
    for s in firms.values():
        adj1, adj5 = s.pop("adj_r1"), s.pop("adj_r5")
        s["days"] = len(s["days"])
        s["symbols"] = len(s["symbols"])
        s["churn_share"] = s["churn_rows"] / s["rows"] if s["rows"] else 0.0
        s["avg_adj_r1"] = sum(adj1) / len(adj1) if adj1 else None
        s["avg_adj_r5"] = sum(adj5) / len(adj5) if adj5 else None
        s["hit_rate_r5"] = (
            sum(1 for x in adj5 if x > 0) / len(adj5) if adj5 else None
        )
        s["n_r5"] = len(adj5)
        out.append(s)
    out.sort(key=lambda s: -s["gross_value"])
    return out
