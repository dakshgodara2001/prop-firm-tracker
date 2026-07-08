"""SQLite storage: schema and small helpers.

Layers:
  raw_deals            — deals exactly as disclosed (append-only, deduped)
  short_selling        — security-level short-selling context (no client names)
  stock_history        — daily price/volume/delivery per symbol (bhavcopy)
  securities_master    — exchange masters for ISIN-based symbol mapping
  matched_deals        — one row per raw deal: normalized name + firm match
  daily_firm_stock_agg — per (date, firm, symbol, deal type) aggregates
  market_context       — per (date, symbol) liquidity/price context
  deal_returns         — post-deal returns (T+1/3/5/10) per aggregate row
  attention_scores     — 0-100 stock attention score + explanation per (date, symbol)
  clusters             — multi-firm same-stock clusters per day
  alerts               — rule-based stock alerts per day (dashboard/CLI feed)
  fetch_log            — audit trail of every fetch attempt (cron monitoring)

Schema management is additive-only: CREATE IF NOT EXISTS plus _ensure_column()
for columns added after Phase 1, so existing databases upgrade in place.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterable, Optional, Tuple

from . import config
from .dates import ist_now
from .models import FetchOutcome, RawDeal, SecurityRecord, ShortSellRecord, StockDay

SCHEMA = """
CREATE TABLE IF NOT EXISTS firms (
    id       INTEGER PRIMARY KEY,
    name     TEXT NOT NULL UNIQUE,
    category TEXT NOT NULL DEFAULT 'PROP_HFT_QUANT',
    active   INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS firm_aliases (
    id      INTEGER PRIMARY KEY,
    firm_id INTEGER NOT NULL REFERENCES firms(id) ON DELETE CASCADE,
    alias   TEXT NOT NULL,              -- stored normalized
    UNIQUE (firm_id, alias)
);

CREATE TABLE IF NOT EXISTS raw_deals (
    id            INTEGER PRIMARY KEY,
    source        TEXT NOT NULL,        -- 'NSE' | 'BSE'
    deal_type     TEXT NOT NULL,        -- 'BULK' | 'BLOCK'
    trade_date    TEXT NOT NULL,        -- ISO YYYY-MM-DD
    symbol        TEXT NOT NULL,        -- NSE symbol (BSE deals mapped via ISIN)
    security_name TEXT,
    client_name   TEXT NOT NULL,        -- as disclosed
    side          TEXT NOT NULL,        -- 'BUY' | 'SELL'
    quantity      INTEGER NOT NULL,
    price         REAL,
    remarks       TEXT,
    fetched_at    TEXT NOT NULL,
    UNIQUE (source, deal_type, trade_date, symbol, client_name, side, quantity, price)
);
CREATE INDEX IF NOT EXISTS idx_raw_deals_date ON raw_deals(trade_date);

CREATE TABLE IF NOT EXISTS short_selling (
    id            INTEGER PRIMARY KEY,
    source        TEXT NOT NULL,
    trade_date    TEXT NOT NULL,
    symbol        TEXT NOT NULL,
    security_name TEXT,
    quantity      INTEGER NOT NULL,
    fetched_at    TEXT NOT NULL,
    UNIQUE (source, trade_date, symbol)
);

CREATE TABLE IF NOT EXISTS stock_history (
    id         INTEGER PRIMARY KEY,
    source     TEXT NOT NULL DEFAULT 'NSE',
    symbol     TEXT NOT NULL,
    series     TEXT NOT NULL DEFAULT '',
    trade_date TEXT NOT NULL,
    prev_close REAL,
    open       REAL,
    high       REAL,
    low        REAL,
    close      REAL,
    last       REAL,
    avg_price  REAL,
    volume     INTEGER NOT NULL DEFAULT 0,
    turnover   REAL,                    -- rupees
    trades     INTEGER,
    deliv_qty  INTEGER,
    deliv_per  REAL,
    UNIQUE (source, symbol, series, trade_date)
);
CREATE INDEX IF NOT EXISTS idx_hist_symbol_date ON stock_history(symbol, trade_date);

CREATE TABLE IF NOT EXISTS securities_master (
    source     TEXT NOT NULL,           -- 'NSE' | 'BSE'
    code       TEXT NOT NULL,           -- NSE symbol / BSE scrip code
    symbol     TEXT,                    -- trading symbol (BSE scrip_id)
    name       TEXT,
    isin       TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (source, code)
);
CREATE INDEX IF NOT EXISTS idx_master_isin ON securities_master(isin);

CREATE TABLE IF NOT EXISTS matched_deals (
    id                INTEGER PRIMARY KEY,
    raw_deal_id       INTEGER NOT NULL UNIQUE REFERENCES raw_deals(id) ON DELETE CASCADE,
    normalized_client TEXT NOT NULL,
    firm_id           INTEGER REFERENCES firms(id),   -- NULL = not a tracked firm
    matched_alias     TEXT,
    match_method      TEXT,                           -- 'exact' | 'prefix' | 'contains'
    match_confidence  REAL
);
CREATE INDEX IF NOT EXISTS idx_matched_firm ON matched_deals(firm_id);

CREATE TABLE IF NOT EXISTS daily_firm_stock_agg (
    id             INTEGER PRIMARY KEY,
    trade_date     TEXT NOT NULL,
    firm_id        INTEGER NOT NULL REFERENCES firms(id),
    symbol         TEXT NOT NULL,
    deal_type      TEXT NOT NULL,
    buy_qty        INTEGER NOT NULL DEFAULT 0,
    sell_qty       INTEGER NOT NULL DEFAULT 0,
    buy_value      REAL NOT NULL DEFAULT 0,
    sell_value     REAL NOT NULL DEFAULT 0,
    buy_trades     INTEGER NOT NULL DEFAULT 0,
    sell_trades    INTEGER NOT NULL DEFAULT 0,
    net_qty        INTEGER NOT NULL,
    gross_qty      INTEGER NOT NULL,
    net_value      REAL NOT NULL,
    vwap_buy       REAL,
    vwap_sell      REAL,
    classification TEXT NOT NULL,
    UNIQUE (trade_date, firm_id, symbol, deal_type)
);

CREATE TABLE IF NOT EXISTS daily_stock_agg (
    id                INTEGER PRIMARY KEY,
    trade_date        TEXT NOT NULL,
    symbol            TEXT NOT NULL,
    security_name     TEXT,
    firm_count        INTEGER NOT NULL,
    firm_names        TEXT NOT NULL,    -- comma-joined canonical names
    buy_qty           INTEGER NOT NULL,
    sell_qty          INTEGER NOT NULL,
    buy_value         REAL NOT NULL,
    sell_value        REAL NOT NULL,
    gross_value       REAL NOT NULL,    -- buy + sell (all tracked activity)
    net_value         REAL NOT NULL,    -- buy - sell
    net_qty           INTEGER NOT NULL,
    dir_buy_qty       INTEGER NOT NULL DEFAULT 0,  -- directional (non-churn) net buys
    dir_sell_qty      INTEGER NOT NULL DEFAULT 0,  -- directional net sells (abs)
    churn_firm_count  INTEGER NOT NULL DEFAULT 0,
    churn_gross_value REAL NOT NULL DEFAULT 0,
    classification    TEXT NOT NULL,    -- CLEAN_BUY/CLEAN_SELL/NET_BUY/NET_SELL/MIXED/ROUND_TRIP
    first_time        INTEGER NOT NULL DEFAULT 0,  -- first appearance in stored history
    UNIQUE (trade_date, symbol)
);

CREATE TABLE IF NOT EXISTS market_context (
    id             INTEGER PRIMARY KEY,
    trade_date     TEXT NOT NULL,
    symbol         TEXT NOT NULL,
    series         TEXT,
    close          REAL,
    prev_close     REAL,
    day_return_pct REAL,
    volume         INTEGER,
    turnover       REAL,
    deliv_qty      INTEGER,
    deliv_per      REAL,
    adv20          REAL,                -- mean volume over prior ADV_WINDOW sessions
    adv_days       INTEGER,             -- sessions actually available for the mean
    volume_vs_adv  REAL,                -- day volume / adv20
    UNIQUE (trade_date, symbol)
);

CREATE TABLE IF NOT EXISTS deal_returns (
    agg_id     INTEGER PRIMARY KEY REFERENCES daily_firm_stock_agg(id) ON DELETE CASCADE,
    trade_date TEXT NOT NULL,
    symbol     TEXT NOT NULL,
    base_price REAL NOT NULL,
    base_kind  TEXT NOT NULL,           -- 'vwap_buy' | 'vwap_sell' | 'close'
    r1         REAL,                    -- % return to close of T+1 session
    r3         REAL,
    r5         REAL,
    r10        REAL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS attention_scores (
    id          INTEGER PRIMARY KEY,
    trade_date  TEXT NOT NULL,
    symbol      TEXT NOT NULL,
    score       REAL NOT NULL,          -- 0..100; attention rank, NOT buy/sell advice
    components  TEXT NOT NULL,          -- JSON breakdown for auditability
    explanation TEXT NOT NULL,          -- plain-English one/two-liner for the report
    computed_at TEXT NOT NULL,
    UNIQUE (trade_date, symbol)
);

CREATE TABLE IF NOT EXISTS clusters (
    id                 INTEGER PRIMARY KEY,
    trade_date         TEXT NOT NULL,
    symbol             TEXT NOT NULL,
    side               TEXT NOT NULL,   -- 'BUY' | 'SELL' | 'CHURN'
    firm_count         INTEGER NOT NULL,
    firm_names         TEXT NOT NULL,   -- comma-joined canonical names
    combined_net_value REAL NOT NULL,   -- for CHURN: combined gross value
    UNIQUE (trade_date, symbol, side)
);

CREATE TABLE IF NOT EXISTS alerts (
    id         INTEGER PRIMARY KEY,
    trade_date TEXT NOT NULL,
    symbol     TEXT NOT NULL,
    alert_type TEXT NOT NULL,    -- HIGH_ATTENTION | MULTI_FIRM | CLEAN_NET |
                                 -- FIRST_TIME | REPEAT_MENTION | CHURN_TO_CLEAN
    priority   INTEGER NOT NULL, -- 1 = look now, 2 = notable, 3 = FYI
    message    TEXT NOT NULL,    -- plain English, stock-first
    details    TEXT,             -- JSON payload for downstream use
    created_at TEXT NOT NULL,
    UNIQUE (trade_date, symbol, alert_type)
);
CREATE INDEX IF NOT EXISTS idx_alerts_date ON alerts(trade_date);

CREATE TABLE IF NOT EXISTS fetch_log (
    id         INTEGER PRIMARY KEY,
    run_at     TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    source     TEXT NOT NULL,
    deal_type  TEXT NOT NULL,
    status     TEXT NOT NULL,
    strategy   TEXT,
    row_count  INTEGER NOT NULL DEFAULT 0,
    message    TEXT
);
"""


def connect(db_path: Optional[Path] = None) -> sqlite3.Connection:
    path = Path(db_path or config.DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    """Additive migration for columns introduced after Phase 1."""
    existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    _ensure_column(conn, "raw_deals", "exchange_code", "exchange_code TEXT NOT NULL DEFAULT ''")
    # 'exact' = alias only matches the full normalized name (for generic
    # English-word aliases like MILLENNIUM that would otherwise catch
    # unrelated brokers); 'fuzzy' = prefix/contains also allowed.
    _ensure_column(conn, "firm_aliases", "match_mode",
                   "match_mode TEXT NOT NULL DEFAULT 'fuzzy'")
    conn.commit()


def insert_raw_deals(conn: sqlite3.Connection, deals: Iterable[RawDeal]) -> Tuple[int, int]:
    """Insert deals, ignoring duplicates. Returns (inserted, ignored)."""
    now = ist_now().isoformat(timespec="seconds")
    inserted = 0
    total = 0
    for d in deals:
        total += 1
        cur = conn.execute(
            """INSERT OR IGNORE INTO raw_deals
               (source, deal_type, trade_date, symbol, security_name, client_name,
                side, quantity, price, remarks, exchange_code, fetched_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                d.source,
                d.deal_type,
                d.trade_date,
                d.symbol,
                d.security_name,
                d.client_name,
                d.side,
                d.quantity,
                d.price,
                d.remarks,
                d.exchange_code,
                now,
            ),
        )
        inserted += cur.rowcount
    conn.commit()
    return inserted, total - inserted


def insert_short_selling(conn: sqlite3.Connection, records: Iterable[ShortSellRecord]) -> int:
    now = ist_now().isoformat(timespec="seconds")
    inserted = 0
    for r in records:
        cur = conn.execute(
            """INSERT OR IGNORE INTO short_selling
               (source, trade_date, symbol, security_name, quantity, fetched_at)
               VALUES (?,?,?,?,?,?)""",
            (r.source, r.trade_date, r.symbol, r.security_name, r.quantity, now),
        )
        inserted += cur.rowcount
    conn.commit()
    return inserted


def insert_stock_history(conn: sqlite3.Connection, days: Iterable[StockDay]) -> Tuple[int, int]:
    """Insert bhavcopy rows, ignoring duplicates. Returns (inserted, ignored)."""
    inserted = 0
    total = 0
    for s in days:
        total += 1
        cur = conn.execute(
            """INSERT OR IGNORE INTO stock_history
               (source, symbol, series, trade_date, prev_close, open, high, low,
                close, last, avg_price, volume, turnover, trades, deliv_qty, deliv_per)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                s.source,
                s.symbol,
                s.series,
                s.trade_date,
                s.prev_close,
                s.open,
                s.high,
                s.low,
                s.close,
                s.last,
                s.avg_price,
                s.volume,
                s.turnover,
                s.trades,
                s.deliv_qty,
                s.deliv_per,
            ),
        )
        inserted += cur.rowcount
    conn.commit()
    return inserted, total - inserted


def upsert_securities(conn: sqlite3.Connection, records: Iterable[SecurityRecord]) -> int:
    now = ist_now().isoformat(timespec="seconds")
    count = 0
    for r in records:
        if not r.code:
            continue
        conn.execute(
            """INSERT INTO securities_master (source, code, symbol, name, isin, updated_at)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(source, code) DO UPDATE SET
                 symbol=excluded.symbol, name=excluded.name,
                 isin=excluded.isin, updated_at=excluded.updated_at""",
            (r.source, r.code, r.symbol, r.name, r.isin, now),
        )
        count += 1
    conn.commit()
    return count


def log_fetch(conn: sqlite3.Connection, trade_date: str, source: str, outcome: FetchOutcome) -> None:
    conn.execute(
        """INSERT INTO fetch_log (run_at, trade_date, source, deal_type, status, strategy, row_count, message)
           VALUES (?,?,?,?,?,?,?,?)""",
        (
            ist_now().isoformat(timespec="seconds"),
            trade_date,
            source,
            outcome.deal_type,
            outcome.status,
            outcome.strategy,
            len(outcome.deals),
            outcome.message,
        ),
    )
    conn.commit()


def clear_processed(conn: sqlite3.Connection, trade_date: str) -> None:
    """Remove derived rows for a date so processing can be re-run idempotently.

    deal_returns cascade away with daily_firm_stock_agg.
    """
    conn.execute(
        "DELETE FROM matched_deals WHERE raw_deal_id IN "
        "(SELECT id FROM raw_deals WHERE trade_date = ?)",
        (trade_date,),
    )
    conn.execute("DELETE FROM daily_firm_stock_agg WHERE trade_date = ?", (trade_date,))
    conn.execute("DELETE FROM daily_stock_agg WHERE trade_date = ?", (trade_date,))
    conn.execute("DELETE FROM market_context WHERE trade_date = ?", (trade_date,))
    conn.execute("DELETE FROM clusters WHERE trade_date = ?", (trade_date,))
    conn.execute("DELETE FROM attention_scores WHERE trade_date = ?", (trade_date,))
    conn.execute("DELETE FROM alerts WHERE trade_date = ?", (trade_date,))
    conn.commit()
