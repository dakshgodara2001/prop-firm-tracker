"""Morning-brief intelligence shared by the report and the dashboard.

Everything a desk brief needs beyond the raw tables: enriched per-stock rows
(deal types, recent-appearance counts), day KPIs, a one-paragraph stance,
ranked top picks, day-over-day changes, and the follow-up checklist. Keeping
it here means the markdown brief and the web command center always tell the
same story.
"""
from __future__ import annotations

import sqlite3
from datetime import timedelta
from typing import Dict, List, Optional

from . import config
from .aggregate import CLASS_LABELS, ROUND_TRIP, STOCK_DIRECTIONAL_CLASSES
from .dates import parse_iso


def _cr(value: float) -> str:
    return f"₹{abs(value) / 1e7:,.1f} Cr"


def enrich_stocks(conn: sqlite3.Connection, trade_date: str) -> List[dict]:
    """Master per-stock rows for one date, attention-ranked, with deal types
    and recent-appearance counts folded in."""
    window_start = (
        parse_iso(trade_date) - timedelta(days=config.ATTENTION_REPEAT_WINDOW_DAYS)
    ).isoformat()
    rows = conn.execute(
        """SELECT s.*, a.score AS attention, a.explanation,
                  mc.close, mc.day_return_pct, mc.volume_vs_adv, mc.deliv_per,
                  (SELECT GROUP_CONCAT(DISTINCT f.deal_type)
                     FROM daily_firm_stock_agg f
                    WHERE f.symbol = s.symbol AND f.trade_date = s.trade_date)
                    AS deal_types,
                  (SELECT COUNT(DISTINCT h.trade_date)
                     FROM daily_stock_agg h
                    WHERE h.symbol = s.symbol AND h.trade_date < s.trade_date
                      AND h.trade_date >= ?) AS recent_count
           FROM daily_stock_agg s
           LEFT JOIN attention_scores a
             ON a.trade_date = s.trade_date AND a.symbol = s.symbol
           LEFT JOIN market_context mc
             ON mc.trade_date = s.trade_date AND mc.symbol = s.symbol
           WHERE s.trade_date = ?
           ORDER BY COALESCE(a.score, 0) DESC, s.gross_value DESC""",
        (window_start, trade_date),
    ).fetchall()
    return [dict(r) for r in rows]


def day_kpis(conn: sqlite3.Connection, trade_date: str, stocks: List[dict]) -> dict:
    firms = {name for s in stocks for name in s["firm_names"].split(", ") if name}
    gross = sum(s["gross_value"] for s in stocks)
    churn_gross = sum(
        s["gross_value"] for s in stocks if s["classification"] == ROUND_TRIP
    ) + sum(
        s["churn_gross_value"] for s in stocks if s["classification"] != ROUND_TRIP
    )
    directional_net = sum(
        abs(s["net_value"]) for s in stocks
        if s["classification"] in STOCK_DIRECTIONAL_CLASSES
    )
    alerts = conn.execute(
        "SELECT COUNT(*) FROM alerts WHERE trade_date = ?", (trade_date,)
    ).fetchone()[0]
    return {
        "stocks": len(stocks),
        "firms": len(firms),
        "gross": gross,
        "directional_net": directional_net,
        "churn_share": (churn_gross / gross) if gross else 0.0,
        "new_mentions": sum(1 for s in stocks if s["first_time"]),
        "multi_firm": sum(1 for s in stocks if s["firm_count"] >= 2),
        "repeat_mentions": sum(
            1 for s in stocks if not s["first_time"] and (s["recent_count"] or 0) > 0
        ),
        "alerts": alerts,
    }


def stance_line(stocks: List[dict], kpis: dict) -> str:
    """One opinionated sentence a desk head would say out loud."""
    if not stocks:
        return "No tracked-firm activity — nothing to do here today."
    directional = [s for s in stocks if s["classification"] in STOCK_DIRECTIONAL_CLASSES]
    parts = []
    if directional:
        lead = max(directional, key=lambda s: abs(s["net_value"]))
        parts.append(
            f"{len(directional)} directional name(s), led by {lead['symbol']} "
            f"({CLASS_LABELS.get(lead['classification'], lead['classification']).lower()} "
            f"of {_cr(lead['net_value'])})"
        )
    else:
        parts.append("no clean directional flow")
    if kpis["churn_share"] >= 0.9:
        parts.append(
            f"a churn-heavy session ({kpis['churn_share']:.0%} of {_cr(kpis['gross'])} "
            "gross was round-trip)"
        )
    elif kpis["churn_share"] > 0:
        parts.append(f"churn was {kpis['churn_share']:.0%} of gross")
    event_days = [
        s for s in stocks
        if s["classification"] == ROUND_TRIP and (s["volume_vs_adv"] or 0) >= 10
    ]
    if event_days:
        parts.append(
            f"{len(event_days)} event-scale liquidity day(s) "
            f"({', '.join(s['symbol'] for s in event_days[:3])}) worth knowing about"
        )
    return f"{kpis['stocks']} stock(s) touched by {kpis['firms']} firm(s): " + "; ".join(parts) + "."


def top_picks(stocks: List[dict], limit: int = 5) -> List[dict]:
    """Ranked 'what to actually look at': directional names by attention first,
    then event-scale churn days (flagged as liquidity, not direction)."""
    directional = [s for s in stocks if s["classification"] in STOCK_DIRECTIONAL_CLASSES]
    events = [
        s for s in stocks
        if s["classification"] == ROUND_TRIP
        and ((s["attention"] or 0) >= config.HIGH_ATTENTION_MIN or (s["volume_vs_adv"] or 0) >= 20)
    ]
    picks = []
    for s in sorted(directional, key=lambda s: -(s["attention"] or 0)):
        picks.append({**s, "pick_kind": "directional"})
    for s in sorted(events, key=lambda s: -(s["attention"] or 0)):
        picks.append({**s, "pick_kind": "liquidity_event"})
    return picks[:limit]


def previous_trade_date(conn: sqlite3.Connection, trade_date: str) -> Optional[str]:
    row = conn.execute(
        "SELECT MAX(trade_date) AS d FROM daily_stock_agg WHERE trade_date < ?",
        (trade_date,),
    ).fetchone()
    return row["d"] if row and row["d"] else None


def changes_vs_previous(conn: sqlite3.Connection, trade_date: str,
                        stocks: List[dict]) -> dict:
    """Plain-English deltas against the previous stored session."""
    prev_date = previous_trade_date(conn, trade_date)
    result = {"prev_date": prev_date, "bullets": []}
    if not prev_date:
        return result
    prev = {
        r["symbol"]: r
        for r in conn.execute(
            "SELECT * FROM daily_stock_agg WHERE trade_date = ?", (prev_date,)
        )
    }
    today = {s["symbol"]: s for s in stocks}
    bullets = result["bullets"]

    for symbol, s in today.items():
        p = prev.get(symbol)
        if p is None:
            continue
        if p["classification"] == ROUND_TRIP and s["classification"] in STOCK_DIRECTIONAL_CLASSES:
            bullets.append(
                f"**{symbol}** turned directional — pure round-trip on {prev_date}, "
                f"{CLASS_LABELS.get(s['classification'], s['classification']).lower()} of "
                f"{_cr(s['net_value'])} today."
            )
        elif s["gross_value"] >= 2 * p["gross_value"] and s["gross_value"] > 10e7:
            bullets.append(
                f"**{symbol}** escalating — gross {_cr(s['gross_value'])} vs "
                f"{_cr(p['gross_value'])} on {prev_date}"
                f"{', now ' + str(s['firm_count']) + ' firms' if s['firm_count'] > p['firm_count'] else ''}."
            )
        elif s["firm_count"] >= p["firm_count"] + 2:
            bullets.append(
                f"**{symbol}** drew a crowd — {s['firm_count']} firms today vs "
                f"{p['firm_count']} on {prev_date}."
            )
        else:
            bullets.append(
                f"**{symbol}** active again ({(s['recent_count'] or 0) + 1} sessions in "
                f"the last {config.ATTENTION_REPEAT_WINDOW_DAYS} days)."
            )

    dropped = [
        p for sym, p in prev.items() if sym not in today
    ]
    dropped.sort(key=lambda p: -p["gross_value"])
    if dropped:
        names = ", ".join(f"**{p['symbol']}**" for p in dropped[:4])
        more = f" (+{len(dropped) - 4} more)" if len(dropped) > 4 else ""
        bullets.append(f"Gone quiet after {prev_date}: {names}{more}.")
    return result


def follow_up_checklist(conn: sqlite3.Connection, trade_date: str,
                        stocks: List[dict], watchlist: List[dict]) -> List[str]:
    """Concrete actions for the trading day — checkable, capped, no fluff."""
    items: List[str] = []
    for s in watchlist[:3]:
        firms = s["firm_names"]
        items.append(
            f"Check news/results/filings on **{s['symbol']}** before open — "
            f"{CLASS_LABELS.get(s['classification'], s['classification']).lower()} of "
            f"{_cr(s['net_value'])} by {firms}."
        )
    events = [
        s for s in stocks
        if s["classification"] == ROUND_TRIP and (s["volume_vs_adv"] or 0) >= 20
    ]
    for s in events[:2]:
        items.append(
            f"Identify the event behind **{s['symbol']}**'s churn day — "
            f"{_cr(s['gross_value'])} gross on {s['volume_vs_adv']:.0f}× normal volume "
            "(results / index / block window?)."
        )
    prev_date = previous_trade_date(conn, trade_date)
    if prev_date:
        prev_directional = [
            r["symbol"]
            for r in conn.execute(
                "SELECT symbol FROM daily_stock_agg WHERE trade_date = ? "
                "AND classification != 'ROUND_TRIP' ORDER BY ABS(net_value) DESC LIMIT 4",
                (prev_date,),
            )
        ]
        if prev_directional:
            items.append(
                f"After close, re-check T+1 prices on {prev_date}'s directional names: "
                + ", ".join(f"**{s}**" for s in prev_directional) + "."
            )
    failed = conn.execute(
        "SELECT COUNT(*) FROM fetch_log WHERE trade_date = ? AND status = 'failed' "
        "AND deal_type IN ('BULK','BLOCK')",
        (trade_date,),
    ).fetchone()[0]
    if failed:
        items.append("Data health: a bulk/block fetch failed — re-run "
                     f"`python main.py run-daily --date {trade_date}`.")
    if not items:
        items.append("Nothing actionable — churn-only day. Skim the liquidity table and move on.")
    return items[:6]
