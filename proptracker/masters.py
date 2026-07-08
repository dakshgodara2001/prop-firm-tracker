"""Securities masters: refresh + ISIN-based BSE→NSE symbol mapping.

Dual-listed stocks appear in BSE deals under a scrip code/id; mapping them to
the NSE symbol lets NSE and BSE flow aggregate together and join NSE price
history. BSE-only listings keep their BSE scrip id as the symbol (no price
context, flagged in the report footer).
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import timedelta
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from . import config, db
from .dates import ist_now
from .models import FETCH_OK, RawDeal

log = logging.getLogger("proptracker.masters")


def master_age_days(conn: sqlite3.Connection, source: str) -> Optional[float]:
    row = conn.execute(
        "SELECT MAX(updated_at) AS ts FROM securities_master WHERE source = ?", (source,)
    ).fetchone()
    if not row or not row["ts"]:
        return None
    try:
        from datetime import datetime

        then = datetime.fromisoformat(row["ts"])
    except ValueError:
        return None
    now = ist_now()
    if then.tzinfo is None:
        now = now.replace(tzinfo=None)
    return (now - then) / timedelta(days=1)


def refresh_masters(
    conn: sqlite3.Connection,
    raw_dir: Optional[Path] = None,
    force: bool = False,
    max_age_days: Optional[int] = None,
) -> Dict[str, int]:
    """Fetch NSE + BSE masters if missing/stale. Returns {source: rows_upserted}."""
    limit = config.MASTERS_MAX_AGE_DAYS if max_age_days is None else max_age_days
    results: Dict[str, int] = {}

    from .fetchers import get_fetcher_class  # lazy: needs `requests`

    for source, make_outcome in (
        ("NSE", lambda: get_fetcher_class("nse")(raw_dir=raw_dir).fetch_equity_master()),
        ("BSE", lambda: get_fetcher_class("bse")(raw_dir=raw_dir).fetch_scrip_master()),
    ):
        age = master_age_days(conn, source)
        if not force and age is not None and age <= limit:
            log.debug("%s master fresh (%.1f d old); skipping", source, age)
            continue
        outcome = make_outcome()
        db.log_fetch(conn, ist_now().date().isoformat(), source, outcome)
        if outcome.status == FETCH_OK:
            results[source] = db.upsert_securities(conn, outcome.deals)
            log.info("%s master refreshed: %d rows", source, results[source])
        else:
            log.warning("%s master refresh failed: %s", source, outcome.message)
    return results


def build_bse_to_nse_map(conn: sqlite3.Connection) -> Dict[str, str]:
    """BSE scrip code -> NSE symbol, joined on ISIN."""
    rows = conn.execute(
        """SELECT b.code AS bse_code, n.symbol AS nse_symbol
           FROM securities_master b
           JOIN securities_master n
             ON n.source = 'NSE' AND n.isin = b.isin AND b.isin != ''
           WHERE b.source = 'BSE'"""
    ).fetchall()
    return {r["bse_code"]: r["nse_symbol"] for r in rows}


def map_bse_deals(conn: sqlite3.Connection, deals: Iterable[RawDeal]) -> int:
    """Rewrite BSE deal symbols to NSE symbols where the ISIN maps. Returns count mapped."""
    deals = list(deals)
    if not deals:
        return 0
    mapping = build_bse_to_nse_map(conn)
    mapped = 0
    for d in deals:
        nse_symbol = mapping.get(d.exchange_code)
        if nse_symbol:
            if d.symbol != nse_symbol:
                d.security_name = f"{d.security_name} (BSE {d.exchange_code})"
                d.symbol = nse_symbol
            mapped += 1
    return mapped


def unmapped_symbols(conn: sqlite3.Connection, trade_date: str) -> List[str]:
    """BSE-only symbols for the date (no NSE mapping => no price context)."""
    rows = conn.execute(
        """SELECT DISTINCT symbol FROM raw_deals
           WHERE trade_date = ? AND source = 'BSE'
             AND symbol NOT IN (SELECT symbol FROM securities_master WHERE source='NSE')
           ORDER BY symbol""",
        (trade_date,),
    ).fetchall()
    return [r["symbol"] for r in rows]
