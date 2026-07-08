"""Rule-based stock alerts.

Alerts flag unusual tracked-firm attention on a stock — they are discovery
prompts ("look at this"), never trade advice. Rebuilt idempotently per date
after attention scoring, stored in the ``alerts`` table, and surfaced on the
dashboard, in ``python main.py alerts`` and in the run-daily summary.

Rules (thresholds in config):

  HIGH_ATTENTION  attention score >= HIGH_ATTENTION_MIN
  MULTI_FIRM      >= ALERT_MULTI_FIRM_MIN tracked firms in one stock
  CLEAN_NET       clean/partial directional flow with |net| >= ALERT_MIN_NET_CR
  FIRST_TIME      first appearance in stored history (with an attention floor
                  so tiny first-timers don't spam the feed)
  REPEAT_MENTION  >= ALERT_REPEAT_MIN appearances within the trailing window
  CHURN_TO_CLEAN  stock was round-trip-only on its recent prior days, but
                  turned directional today — churn shifting into cleaner
                  net activity
  LARGE_GROSS     >= ALERT_LARGE_GROSS_CR gross in one day (suppressed when
                  HIGH_ATTENTION already fired for the same stock)
"""
from __future__ import annotations

import json
import sqlite3
from datetime import timedelta
from typing import List

from . import config
from .aggregate import CLASS_LABELS, ROUND_TRIP, STOCK_DIRECTIONAL_CLASSES
from .dates import ist_now, parse_iso

HIGH_ATTENTION = "HIGH_ATTENTION"
MULTI_FIRM = "MULTI_FIRM"
CLEAN_NET = "CLEAN_NET"
FIRST_TIME = "FIRST_TIME"
REPEAT_MENTION = "REPEAT_MENTION"
CHURN_TO_CLEAN = "CHURN_TO_CLEAN"
LARGE_GROSS = "LARGE_GROSS"

# 1 = look now, 2 = notable, 3 = FYI
PRIORITY = {
    CHURN_TO_CLEAN: 1,
    CLEAN_NET: 1,
    HIGH_ATTENTION: 1,
    MULTI_FIRM: 2,
    LARGE_GROSS: 2,
    FIRST_TIME: 2,
    REPEAT_MENTION: 3,
}

ALERT_LABELS = {
    HIGH_ATTENTION: "High attention",
    MULTI_FIRM: "Multi-firm",
    CLEAN_NET: "Clean net activity",
    FIRST_TIME: "First-time mention",
    REPEAT_MENTION: "Repeat mention",
    CHURN_TO_CLEAN: "Churn → directional",
    LARGE_GROSS: "Large gross activity",
}


def _cr(value: float) -> str:
    return f"₹{abs(value) / 1e7:,.2f} Cr"


def _prior_classifications(conn: sqlite3.Connection, symbol: str, trade_date: str,
                           window_days: int) -> List[str]:
    window_start = (parse_iso(trade_date) - timedelta(days=window_days)).isoformat()
    rows = conn.execute(
        """SELECT classification FROM daily_stock_agg
           WHERE symbol = ? AND trade_date < ? AND trade_date >= ?
           ORDER BY trade_date""",
        (symbol, trade_date, window_start),
    ).fetchall()
    return [r["classification"] for r in rows]


def generate_alerts(conn: sqlite3.Connection, trade_date: str) -> int:
    """(Re)build alerts for one date. Returns alerts written."""
    stocks = conn.execute(
        """SELECT s.*, a.score AS attention, mc.volume_vs_adv, mc.day_return_pct
           FROM daily_stock_agg s
           LEFT JOIN attention_scores a
             ON a.trade_date = s.trade_date AND a.symbol = s.symbol
           LEFT JOIN market_context mc
             ON mc.trade_date = s.trade_date AND mc.symbol = s.symbol
           WHERE s.trade_date = ?""",
        (trade_date,),
    ).fetchall()

    conn.execute("DELETE FROM alerts WHERE trade_date = ?", (trade_date,))
    now = ist_now().isoformat(timespec="seconds")
    written = 0

    for s in stocks:
        found = []  # (type, message, details)
        attention = s["attention"] or 0.0
        label = CLASS_LABELS.get(s["classification"], s["classification"])
        prior = _prior_classifications(
            conn, s["symbol"], trade_date, config.ATTENTION_REPEAT_WINDOW_DAYS
        )
        details = {
            "attention": attention,
            "classification": s["classification"],
            "firm_count": s["firm_count"],
            "firm_names": s["firm_names"],
            "gross_value": s["gross_value"],
            "net_value": s["net_value"],
            "first_time": bool(s["first_time"]),
            "prior_appearances_window": len(prior),
        }

        if attention >= config.HIGH_ATTENTION_MIN:
            volume_note = (
                f" on {s['volume_vs_adv']:,.1f}× normal volume"
                if s["volume_vs_adv"] is not None
                else ""
            )
            found.append((
                HIGH_ATTENTION,
                f"Attention {attention:.0f}/100 — {s['firm_count']} tracked firm(s), "
                f"{_cr(s['gross_value'])} gross{volume_note}. Flow: {label}.",
            ))

        if s["firm_count"] >= config.ALERT_MULTI_FIRM_MIN:
            found.append((
                MULTI_FIRM,
                f"{s['firm_count']} tracked firms in one stock "
                f"({s['firm_names']}) — {_cr(s['gross_value'])} combined gross.",
            ))

        # Large gross stands alone only when high-attention didn't already fire
        # for the same stock (anti-spam: one loud alert per reason).
        if (
            s["gross_value"] >= config.ALERT_LARGE_GROSS_CR * 1e7
            and attention < config.HIGH_ATTENTION_MIN
        ):
            found.append((
                LARGE_GROSS,
                f"{_cr(s['gross_value'])} gross tracked-firm turnover in one day "
                f"({label}) — large even without a directional read.",
            ))

        if (
            s["classification"] in STOCK_DIRECTIONAL_CLASSES
            and s["classification"] != "MIXED"
            and abs(s["net_value"]) >= config.ALERT_MIN_NET_CR * 1e7
        ):
            found.append((
                CLEAN_NET,
                f"{label} of {_cr(s['net_value'])} by {s['firm_names']}.",
            ))

        if s["first_time"] and attention >= config.ALERT_FIRST_TIME_MIN_ATTENTION:
            found.append((
                FIRST_TIME,
                f"First appearance in tracked-firm deals (attention {attention:.0f}, "
                f"flow: {label}).",
            ))

        total_appearances = len(prior) + 1
        if not s["first_time"] and total_appearances >= config.ALERT_REPEAT_MIN:
            found.append((
                REPEAT_MENTION,
                f"Appearance #{total_appearances} in the last "
                f"{config.ATTENTION_REPEAT_WINDOW_DAYS} days — recurring "
                "tracked-firm interest.",
            ))

        if (
            s["classification"] in STOCK_DIRECTIONAL_CLASSES
            and len(prior) >= config.ALERT_CHURN_MIN_PRIOR
            and all(c == ROUND_TRIP for c in prior)
        ):
            found.append((
                CHURN_TO_CLEAN,
                f"Shifted from churn to directional: {len(prior)} prior day(s) were "
                f"pure round-trip, today shows {label} of {_cr(s['net_value'])}.",
            ))

        for alert_type, message in found:
            conn.execute(
                """INSERT OR IGNORE INTO alerts
                   (trade_date, symbol, alert_type, priority, message, details, created_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (trade_date, s["symbol"], alert_type, PRIORITY[alert_type],
                 message, json.dumps(details), now),
            )
            written += 1
    conn.commit()
    return written


def alerts_for_date(conn: sqlite3.Connection, trade_date: str) -> list:
    return conn.execute(
        """SELECT a.*, s.score AS attention FROM alerts a
           LEFT JOIN attention_scores s
             ON s.trade_date = a.trade_date AND s.symbol = a.symbol
           WHERE a.trade_date = ?
           ORDER BY a.priority, COALESCE(s.score, 0) DESC, a.symbol""",
        (trade_date,),
    ).fetchall()


def latest_alert_date(conn: sqlite3.Connection):
    row = conn.execute("SELECT MAX(trade_date) AS d FROM alerts").fetchone()
    return row["d"] if row else None
