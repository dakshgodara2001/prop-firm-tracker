"""Orchestration: fetch → store raw → match → aggregate → analytics → report.

run-daily steps for one date:

  1. fetch bulk/block deals from every enabled source (NSE primary, BSE
     secondary), short selling and the price/delivery bhavcopy from NSE
  2. map BSE deals to NSE symbols via the ISIN masters, store everything raw
  3. normalize + match client names against the watchlist
  4. aggregate per (firm, symbol, deal type) and classify signal vs churn
  5. compute market context, clusters, and stock attention scores for the date
  6. update post-deal returns over the trailing window (new bhavcopies fill
     in T+1/3/5/10 for earlier deals)
  7. write the markdown report

Every step is idempotent, so re-running a date (or a cron retry) is safe:
raw rows are deduped on insert, and derived rows for the date are rebuilt
from scratch each run.
"""
from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import List, Optional, Tuple

from . import config, db, masters, watchlist
from .aggregate import aggregate_date, aggregate_stocks_date
from .alerts import generate_alerts
from .context import compute_market_context
from .fetchers import get_fetcher_class
from .matching import FirmMatcher
from .models import DEAL_PRICES, DEAL_SHORT, FETCH_FAILED, FETCH_OK, FetchOutcome
from .report import write_report
from .returns import update_deal_returns
from .scoring import compute_attention, detect_clusters

log = logging.getLogger("proptracker.pipeline")


@dataclass
class RunSummary:
    trade_date: str
    fetch_outcomes: List[FetchOutcome] = field(default_factory=list)
    raw_inserted: int = 0
    raw_duplicates: int = 0
    short_inserted: int = 0
    prices_inserted: int = 0
    bse_mapped: int = 0
    processed_rows: int = 0
    matched_rows: int = 0
    firms_matched: int = 0
    agg_rows: int = 0
    stock_rows: int = 0
    new_stocks: int = 0
    context_rows: int = 0
    cluster_rows: int = 0
    attention_rows: int = 0
    returns_updated: int = 0
    alert_rows: int = 0
    top_alerts: List[str] = field(default_factory=list)
    report_path: Optional[Path] = None
    warnings: List[str] = field(default_factory=list)

    @property
    def hard_fetch_failure(self) -> bool:
        """True when a bulk/block fetch failed outright (short selling and
        prices are soft failures)."""
        return any(
            o.status == FETCH_FAILED and o.deal_type not in (DEAL_SHORT, DEAL_PRICES)
            for o in self.fetch_outcomes
        )


def prepare_db(conn: sqlite3.Connection) -> None:
    db.init_db(conn)
    watchlist.seed_firms(conn)


def build_fetchers(sources=None, raw_dir: Optional[Path] = None) -> list:
    fetchers = []
    for name in sources or config.SOURCES:
        try:
            fetchers.append(get_fetcher_class(name)(raw_dir=raw_dir))
        except KeyError as exc:
            log.warning("skipping unknown source: %s", exc)
    return fetchers


def fetch_and_store(
    conn: sqlite3.Connection,
    trade_date: date,
    include_short: bool = True,
    include_prices: bool = True,
    fetchers: Optional[list] = None,
    raw_dir: Optional[Path] = None,
) -> Tuple[List[FetchOutcome], dict]:
    """Fetch every report type from every source and persist them.

    Returns (outcomes, counters) where counters has keys raw_inserted,
    raw_duplicates, short_inserted, prices_inserted, bse_mapped.
    """
    if fetchers is None:
        fetchers = build_fetchers(raw_dir=raw_dir)

    counters = dict.fromkeys(
        ("raw_inserted", "raw_duplicates", "short_inserted", "prices_inserted", "bse_mapped"), 0
    )
    outcomes: List[FetchOutcome] = []
    masters_refreshed = False
    iso = trade_date.isoformat()

    for fetcher in fetchers:
        for step in (fetcher.fetch_bulk_deals, fetcher.fetch_block_deals):
            outcome = step(trade_date)
            if outcome.status == FETCH_OK and outcome.deals and fetcher.source == "BSE":
                if not masters_refreshed:
                    try:
                        masters.refresh_masters(conn, raw_dir=raw_dir)
                    except Exception as exc:  # noqa: BLE001 - mapping is best-effort
                        log.warning("securities master refresh failed: %s", exc)
                    masters_refreshed = True
                mapped = masters.map_bse_deals(conn, outcome.deals)
                counters["bse_mapped"] += mapped
                outcome.message += f" ({mapped} mapped to NSE symbols)"
            db.log_fetch(conn, iso, fetcher.source, outcome)
            outcomes.append(outcome)
            if outcome.status == FETCH_OK:
                ins, dup = db.insert_raw_deals(conn, outcome.deals)
                counters["raw_inserted"] += ins
                counters["raw_duplicates"] += dup
            elif outcome.status == FETCH_FAILED:
                log.error("%s %s fetch failed: %s", fetcher.source, outcome.deal_type, outcome.message)

        if include_short and fetcher.source == "NSE":
            outcome = fetcher.fetch_short_selling(trade_date)
            db.log_fetch(conn, iso, fetcher.source, outcome)
            outcomes.append(outcome)
            if outcome.status == FETCH_OK:
                counters["short_inserted"] += db.insert_short_selling(conn, outcome.deals)

        if include_prices and hasattr(fetcher, "fetch_price_history"):
            outcome = fetcher.fetch_price_history(trade_date)
            db.log_fetch(conn, iso, fetcher.source, outcome)
            outcomes.append(outcome)
            if outcome.status == FETCH_OK:
                ins, _ = db.insert_stock_history(conn, outcome.deals)
                counters["prices_inserted"] += ins
            else:
                log.warning("%s price history unavailable: %s", fetcher.source, outcome.message)

    return outcomes, counters


def process_date(conn: sqlite3.Connection, trade_date: str) -> Tuple[int, int, int]:
    """Normalize + match every raw deal of the date. Returns (rows, matched, firms)."""
    matcher = FirmMatcher.from_db(conn)
    db.clear_processed(conn, trade_date)
    rows = conn.execute(
        "SELECT id, client_name FROM raw_deals WHERE trade_date = ?", (trade_date,)
    ).fetchall()

    matched = 0
    firms = set()
    from .normalize import normalize_name  # local import to keep module deps flat

    for row in rows:
        result = matcher.match(row["client_name"])
        if result:
            matched += 1
            firms.add(result.firm_id)
        conn.execute(
            """INSERT INTO matched_deals
               (raw_deal_id, normalized_client, firm_id, matched_alias, match_method, match_confidence)
               VALUES (?,?,?,?,?,?)""",
            (
                row["id"],
                normalize_name(row["client_name"]),
                result.firm_id if result else None,
                result.alias if result else None,
                result.method if result else None,
                result.confidence if result else None,
            ),
        )
    conn.commit()
    return len(rows), matched, len(firms)


def analyze_date(conn: sqlite3.Connection, trade_date: str) -> dict:
    """Post-aggregation analytics: context, clusters, scores, returns."""
    results = {
        "context_rows": compute_market_context(conn, trade_date),
        "cluster_rows": detect_clusters(conn, trade_date),
        "attention_rows": compute_attention(conn, trade_date),
        "returns_updated": update_deal_returns(conn, as_of=trade_date),
    }
    return results


def run_daily(
    trade_date: date,
    *,
    skip_fetch: bool = False,
    include_short: Optional[bool] = None,
    include_prices: Optional[bool] = None,
    sources=None,
    fetcher=None,
    fetchers: Optional[list] = None,
    db_path: Optional[Path] = None,
    raw_dir: Optional[Path] = None,
    reports_dir: Optional[Path] = None,
) -> RunSummary:
    """The full daily pipeline for one date. Never raises on a data-less day.

    ``fetcher`` (single) is accepted for backward compatibility with Phase 1
    callers/tests; ``fetchers`` (list) or ``sources`` (names) is preferred.
    """
    if include_short is None:
        include_short = config.INCLUDE_SHORT_SELLING
    if include_prices is None:
        include_prices = config.FETCH_PRICES
    if fetchers is None and fetcher is not None:
        fetchers = [fetcher]
    iso = trade_date.isoformat()
    summary = RunSummary(trade_date=iso)

    conn = db.connect(db_path)
    try:
        prepare_db(conn)

        if trade_date.weekday() >= 5:
            summary.warnings.append(f"{iso} is a {trade_date.strftime('%A')} — exchanges are closed.")

        if skip_fetch:
            log.info("skip-fetch: processing existing raw data for %s", iso)
        else:
            if fetchers is None:
                fetchers = build_fetchers(sources=sources, raw_dir=raw_dir)
            summary.fetch_outcomes, counters = fetch_and_store(
                conn,
                trade_date,
                include_short=include_short,
                include_prices=include_prices,
                fetchers=fetchers,
                raw_dir=raw_dir,
            )
            summary.raw_inserted = counters["raw_inserted"]
            summary.raw_duplicates = counters["raw_duplicates"]
            summary.short_inserted = counters["short_inserted"]
            summary.prices_inserted = counters["prices_inserted"]
            summary.bse_mapped = counters["bse_mapped"]
            for outcome in summary.fetch_outcomes:
                if outcome.status == FETCH_FAILED:
                    summary.warnings.append(
                        f"{outcome.deal_type} fetch failed: {outcome.message}"
                    )

        summary.processed_rows, summary.matched_rows, summary.firms_matched = process_date(conn, iso)
        summary.agg_rows = aggregate_date(conn, iso)
        summary.stock_rows = aggregate_stocks_date(conn, iso)
        summary.new_stocks = conn.execute(
            "SELECT COUNT(*) FROM daily_stock_agg WHERE trade_date = ? AND first_time = 1",
            (iso,),
        ).fetchone()[0]
        analytics = analyze_date(conn, iso)
        summary.context_rows = analytics["context_rows"]
        summary.cluster_rows = analytics["cluster_rows"]
        summary.attention_rows = analytics["attention_rows"]
        summary.returns_updated = analytics["returns_updated"]

        summary.alert_rows = generate_alerts(conn, iso)
        summary.top_alerts = [
            f"[{r['symbol']}] {r['message']}"
            for r in conn.execute(
                """SELECT symbol, message FROM alerts WHERE trade_date = ?
                   ORDER BY priority, id LIMIT 5""",
                (iso,),
            )
        ]

        unmapped = masters.unmapped_symbols(conn, iso)
        if unmapped:
            summary.warnings.append(
                f"{len(unmapped)} BSE-only symbol(s) without NSE mapping "
                f"(no price context): {', '.join(unmapped[:8])}"
                + ("…" if len(unmapped) > 8 else "")
            )

        summary.report_path = write_report(conn, iso, reports_dir)
    finally:
        conn.close()
    return summary


def backfill_prices(
    from_date: date,
    to_date: date,
    *,
    db_path: Optional[Path] = None,
    raw_dir: Optional[Path] = None,
    sleep_seconds: float = 1.0,
) -> Tuple[int, int]:
    """Fetch bhavcopies over a date range (weekends skipped).

    Returns (days_loaded, rows_inserted). Used to seed ADV/returns history.
    """
    import time as _time
    from datetime import timedelta

    conn = db.connect(db_path)
    try:
        db.init_db(conn)
        fetcher = get_fetcher_class("nse")(raw_dir=raw_dir)
        days_loaded = 0
        rows = 0
        current = from_date
        while current <= to_date:
            if current.weekday() >= 5:
                current += timedelta(days=1)
                continue
            outcome = fetcher.fetch_price_history(current)
            db.log_fetch(conn, current.isoformat(), fetcher.source, outcome)
            if outcome.status == FETCH_OK:
                ins, _ = db.insert_stock_history(conn, outcome.deals)
                rows += ins
                days_loaded += 1
                log.info("%s: %d price rows (+%d new)", current, len(outcome.deals), ins)
            else:
                log.warning("%s: no bhavcopy (%s)", current, outcome.message)
            current += timedelta(days=1)
            if current <= to_date:
                _time.sleep(sleep_seconds)
        return days_loaded, rows
    finally:
        conn.close()
