"""Server-rendered SVG charts: geometry/data, no browser needed."""
from proptracker.db import insert_raw_deals, insert_stock_history
from proptracker.models import RawDeal, StockDay
from proptracker.pipeline import process_date
from proptracker.aggregate import aggregate_date, aggregate_stocks_date
from proptracker.scoring import compute_attention
from proptracker.web import charts

DATE = "2026-06-10"


def _deal(client, side, qty, price, symbol):
    return RawDeal("NSE", "BULK", DATE, symbol, f"{symbol} Ltd", client, side, qty, price)


def _build(conn):
    insert_raw_deals(
        conn,
        [
            _deal("GRAVITON RESEARCH CAPITAL LLP", "BUY", 500_000, 50.0, "ALPHACO"),
            _deal("NK SECURITIES RESEARCH PRIVATE LIMITED", "BUY", 200_000, 100.0, "BETACO"),
            _deal("NK SECURITIES RESEARCH PRIVATE LIMITED", "SELL", 195_000, 101.0, "BETACO"),
            _deal("OPTIVER INDIA PRIVATE LIMITED", "SELL", 300_000, 20.0, "ALPHACO"),
        ],
    )
    process_date(conn, DATE)
    aggregate_date(conn, DATE)
    aggregate_stocks_date(conn, DATE)
    compute_attention(conn, DATE)


def test_matrix_has_dot_per_firm_stock_pair(conn):
    _build(conn)
    m = charts.firm_stock_matrix(conn, DATE)
    assert m is not None
    # 3 firms touched 2 stocks: Graviton+Optiver on ALPHACO, NK on BETACO = 3 dots
    assert m["svg"].count("<circle") == 3
    assert m["n_firms"] == 3 and m["n_stocks"] == 2
    assert "GRAVITON" in m["svg"].upper()
    assert 'viewBox="0 0' in m["svg"]
    # flow colours come through as CSS vars
    assert "var(--buy)" in m["svg"] or "var(--churn)" in m["svg"]


def test_matrix_none_when_empty(conn):
    assert charts.firm_stock_matrix(conn, DATE) is None


def test_matrix_truncates_to_max_stocks(conn):
    rows = []
    for i in range(20):
        rows.append(_deal("GRAVITON RESEARCH CAPITAL LLP", "BUY", 1000, 10.0, f"SYM{i:02d}"))
    insert_raw_deals(conn, rows)
    process_date(conn, DATE)
    aggregate_date(conn, DATE)
    aggregate_stocks_date(conn, DATE)
    compute_attention(conn, DATE)
    m = charts.firm_stock_matrix(conn, DATE, max_stocks=16)
    assert m["truncated"] is True
    assert m["n_stocks"] == 16 and m["total_stocks"] == 20


def test_price_chart_line_and_markers(conn):
    _build(conn)
    # price history: 5 prior sessions + the deal day
    days = [
        StockDay("NSE", "ALPHACO", "EQ", f"2026-06-0{d}", None, 48.0, 48.0, 48.0,
                 48.0 + d, 48.0, 48.0, 100000, 5e6, 10, None, None)
        for d in range(1, 6)
    ]
    days.append(StockDay("NSE", "ALPHACO", "EQ", DATE, 53.0, 55.0, 55.0, 55.0,
                         55.0, 55.0, 55.0, 300000, 1.6e7, 20, None, None))
    insert_stock_history(conn, days)
    c = charts.stock_price_svg(conn, "ALPHACO")
    assert c is not None
    assert "<polyline" in c["svg"]        # the price line
    assert c["sessions"] == 6
    assert c["markers"] == 1              # only the deal day had firm activity
    assert "<circle" in c["svg"]


def test_price_chart_none_without_history(conn):
    _build(conn)
    assert charts.stock_price_svg(conn, "BETACO") is None  # no stock_history rows
