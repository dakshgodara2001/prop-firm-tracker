"""Stock attention scoring, plain-English explanations, and cluster detection.

The **attention score** (0-100, one per stock per day) ranks which stocks
deserve a look today, purely by how much tracked-firm activity they saw and
how unusual the day was. It carries no directional meaning and predicts
nothing — a five-firm churn frenzy ranks high as a liquidity/event flag, and
the flow label (clean buy / churn / …) is what says *what kind* of attention
it was.

    score = 100 × weighted_mean(components)     weights: config.ATTENTION_WEIGHTS

    firms    — how many tracked firms appeared
    multi    — any multi-firm participation
    gross    — total gross traded value
    net      — |net| directional value
    clean    — directional share of activity (1 = no churn, 0 = all churn)
    context  — unusual volume (× ADV20) and |day move|, when history exists
    repeat   — appearances in the trailing ATTENTION_REPEAT_WINDOW_DAYS

Components with missing inputs are skipped and the remaining weights
renormalized; the breakdown is stored as JSON. Each stock also gets a short
plain-English explanation used verbatim in the report.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import timedelta
from typing import Dict, Optional

from . import config
from .aggregate import (
    CLEAN_BUY,
    CLEAN_SELL,
    DIRECTIONAL_CLASSES,
    MIXED,
    NET_BUY,
    NET_SELL,
    ROUND_TRIP,
)
from .dates import ist_now, parse_iso


# ---------------------------------------------------------------------------
# Attention score
# ---------------------------------------------------------------------------


def attention_components(stock: sqlite3.Row, ctx: Optional[sqlite3.Row],
                         repeat_count: int) -> dict:
    gross = stock["gross_value"]
    components = {
        "firms": min(1.0, stock["firm_count"] / config.ATTENTION_FIRMS_CAP),
        "multi": 1.0 if stock["firm_count"] >= 2 else 0.0,
        "gross": min(1.0, gross / (config.ATTENTION_GROSS_CAP_CR * 1e7)),
        "net": min(1.0, abs(stock["net_value"]) / (config.ATTENTION_NET_CAP_CR * 1e7)),
        "clean": ((gross - stock["churn_gross_value"]) / gross) if gross > 0 else 0.0,
        "repeat": min(1.0, repeat_count / config.ATTENTION_REPEAT_CAP),
    }
    ctx_parts = []
    vol_vs_adv = ctx["volume_vs_adv"] if ctx is not None else None
    day_move = ctx["day_return_pct"] if ctx is not None else None
    if vol_vs_adv is not None:
        ctx_parts.append(min(1.0, vol_vs_adv / config.ATTENTION_VOL_ADV_CAP))
    if day_move is not None:
        ctx_parts.append(min(1.0, abs(day_move) / config.ATTENTION_MOVE_CAP_PCT))
    components["context"] = sum(ctx_parts) / len(ctx_parts) if ctx_parts else None
    components["inputs"] = {
        "firm_count": stock["firm_count"],
        "gross_value": gross,
        "net_value": stock["net_value"],
        "churn_gross_value": stock["churn_gross_value"],
        "volume_vs_adv": vol_vs_adv,
        "day_return_pct": day_move,
        "repeat_count": repeat_count,
    }
    return components


def attention_from_components(components: dict) -> float:
    total = 0.0
    total_weight = 0.0
    for name, weight in config.ATTENTION_WEIGHTS.items():
        value = components.get(name)
        if value is None:
            continue
        total += weight * value
        total_weight += weight
    return round(100.0 * (total / total_weight), 1) if total_weight else 0.0


# ---------------------------------------------------------------------------
# Plain-English explanations
# ---------------------------------------------------------------------------


def _cr(value: float) -> str:
    return f"₹{abs(value) / 1e7:,.2f} Cr"


def _firm_phrase(names_csv: str, limit: int = 3) -> str:
    names = [n for n in names_csv.split(", ") if n]
    if not names:
        return "tracked firms"
    if len(names) == 1:
        return names[0]
    if len(names) <= limit:
        return ", ".join(names[:-1]) + " and " + names[-1]
    return f"{', '.join(names[:limit])} and {len(names) - limit} more"


def build_explanation(stock: sqlite3.Row, ctx: Optional[sqlite3.Row],
                      repeat_count: int) -> str:
    name = stock["security_name"] or stock["symbol"]
    firms = _firm_phrase(stock["firm_names"])
    cls = stock["classification"]
    gross, net = stock["gross_value"], stock["net_value"]

    if cls == ROUND_TRIP:
        text = (
            f"{name} was touched by {firms}. Combined gross activity was {_cr(gross)} "
            f"but net was only {_cr(net)}, so this looks like liquidity/churn rather "
            "than directional accumulation."
        )
    elif cls == CLEAN_BUY:
        text = (
            f"{name} was picked by {firms} with a clean net buy of {_cr(net)}. "
            "No same-day selling was disclosed, so this is directional activity "
            "worth tracking."
        )
    elif cls == CLEAN_SELL:
        text = (
            f"{name} was sold by {firms} — a clean net sell of {_cr(net)} with no "
            "same-day buying disclosed. Directional exit worth tracking."
        )
    elif cls == NET_BUY:
        text = (
            f"{name}: {firms} ended net buyers of {_cr(net)} after trading both sides "
            f"(gross {_cr(gross)}) — a partial accumulation rather than a clean one."
        )
    elif cls == NET_SELL:
        text = (
            f"{name}: {firms} ended net sellers of {_cr(net)} after trading both sides "
            f"(gross {_cr(gross)}) — a partial reduction rather than a clean exit."
        )
    elif cls == MIXED:
        text = (
            f"{name} had tracked firms on both sides ({firms}) with largely offsetting "
            "positions — disagreement between firms, not simple churn."
        )
    else:  # pragma: no cover - future classes
        text = f"{name} saw tracked-firm activity of {_cr(gross)} gross ({firms})."

    if cls != ROUND_TRIP and stock["churn_firm_count"]:
        text += f" {stock['churn_firm_count']} other tracked firm(s) churned it intraday."

    vol_vs_adv = ctx["volume_vs_adv"] if ctx is not None else None
    day_move = ctx["day_return_pct"] if ctx is not None else None
    if vol_vs_adv is not None and day_move is not None:
        text += (
            f" Volume ran {vol_vs_adv:,.1f}× the 20-day average with the stock "
            f"{day_move:+.1f}% on the day."
        )
    elif vol_vs_adv is not None:
        text += f" Volume ran {vol_vs_adv:,.1f}× the 20-day average."
    elif day_move is not None:
        text += f" The stock moved {day_move:+.1f}% on the day."

    if stock["first_time"]:
        text += " First appearance in stored history."
    elif repeat_count:
        text += (
            f" Also appeared on {repeat_count} day(s) in the last "
            f"{config.ATTENTION_REPEAT_WINDOW_DAYS}."
        )
    return text


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def _repeat_count(conn: sqlite3.Connection, symbol: str, trade_date: str) -> int:
    window_start = (
        parse_iso(trade_date) - timedelta(days=config.ATTENTION_REPEAT_WINDOW_DAYS)
    ).isoformat()
    return conn.execute(
        """SELECT COUNT(DISTINCT trade_date) FROM daily_stock_agg
           WHERE symbol = ? AND trade_date < ? AND trade_date >= ?""",
        (symbol, trade_date, window_start),
    ).fetchone()[0]


def compute_attention(conn: sqlite3.Connection, trade_date: str) -> int:
    """(Re)score every touched stock of the date. Returns rows written."""
    stocks = conn.execute(
        "SELECT * FROM daily_stock_agg WHERE trade_date = ?", (trade_date,)
    ).fetchall()
    contexts = {
        r["symbol"]: r
        for r in conn.execute(
            "SELECT * FROM market_context WHERE trade_date = ?", (trade_date,)
        )
    }
    conn.execute("DELETE FROM attention_scores WHERE trade_date = ?", (trade_date,))
    now = ist_now().isoformat(timespec="seconds")
    written = 0
    for stock in stocks:
        ctx = contexts.get(stock["symbol"])
        repeat = _repeat_count(conn, stock["symbol"], trade_date)
        components = attention_components(stock, ctx, repeat)
        score = attention_from_components(components)
        explanation = build_explanation(stock, ctx, repeat)
        conn.execute(
            """INSERT INTO attention_scores
               (trade_date, symbol, score, components, explanation, computed_at)
               VALUES (?,?,?,?,?,?)""",
            (trade_date, stock["symbol"], score, json.dumps(components), explanation, now),
        )
        written += 1
    conn.commit()
    return written


# ---------------------------------------------------------------------------
# Multi-firm clusters
# ---------------------------------------------------------------------------


def detect_clusters(conn: sqlite3.Connection, trade_date: str) -> int:
    """(Re)build multi-firm clusters for the date. Returns clusters found.

    BUY/SELL = several tracked firms held directional positions the same way;
    CHURN = several firms round-tripped the same stock (event-day flag).
    """
    conn.execute("DELETE FROM clusters WHERE trade_date = ?", (trade_date,))
    aggs = conn.execute(
        """SELECT a.*, f.name AS firm_name
           FROM daily_firm_stock_agg a JOIN firms f ON f.id = a.firm_id
           WHERE a.trade_date = ?""",
        (trade_date,),
    ).fetchall()

    by_symbol: Dict[str, list] = {}
    for a in aggs:
        by_symbol.setdefault(a["symbol"], []).append(a)

    written = 0
    for symbol, rows in sorted(by_symbol.items()):
        buyers, sellers, churners = {}, {}, {}
        for a in rows:
            if a["classification"] in DIRECTIONAL_CLASSES:
                bucket = buyers if a["net_qty"] > 0 else sellers
            elif a["classification"] == ROUND_TRIP:
                bucket = churners
            else:
                continue
            # A firm may appear via both BULK and BLOCK rows; merge per firm.
            entry = bucket.setdefault(a["firm_name"], {"net": 0.0, "gross": 0.0})
            entry["net"] += a["net_value"]
            entry["gross"] += a["buy_value"] + a["sell_value"]

        for side, bucket, min_firms in (
            ("BUY", buyers, config.CLUSTER_MIN_FIRMS),
            ("SELL", sellers, config.CLUSTER_MIN_FIRMS),
            ("CHURN", churners, config.CHURN_CLUSTER_MIN_FIRMS),
        ):
            if len(bucket) < min_firms:
                continue
            # comma-separated: these names land inside markdown table cells
            names = ", ".join(sorted(bucket))
            combined = sum(
                e["gross"] if side == "CHURN" else e["net"] for e in bucket.values()
            )
            conn.execute(
                """INSERT INTO clusters
                   (trade_date, symbol, side, firm_count, firm_names, combined_net_value)
                   VALUES (?,?,?,?,?,?)""",
                (trade_date, symbol, side, len(bucket), names, combined),
            )
            written += 1
    conn.commit()
    return written
