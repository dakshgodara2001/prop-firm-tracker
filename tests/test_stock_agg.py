"""Stock-first aggregation: flow labels, churn separation, first-time flags."""
from proptracker.aggregate import (
    CLEAN_BUY,
    CLEAN_SELL,
    MIXED,
    NET_BUY,
    NET_SELL,
    ROUND_TRIP,
    aggregate_stocks_date,
    classify_stock,
)

D1 = "2026-06-10"
D2 = "2026-06-11"


def test_classify_stock_matrix():
    assert classify_stock(0, 0, True, True) == ROUND_TRIP  # churn only
    assert classify_stock(100, 0, True, True) == CLEAN_BUY
    assert classify_stock(100, 0, False, True) == NET_BUY  # a buyer churned some
    assert classify_stock(0, 100, True, True) == CLEAN_SELL
    assert classify_stock(0, 100, True, False) == NET_SELL
    # both sides, largely offsetting -> firms disagree, not churn
    assert classify_stock(100, 90, True, True) == MIXED
    # both sides, one clearly dominates
    assert classify_stock(100, 20, True, True) == NET_BUY
    assert classify_stock(20, 100, True, True) == NET_SELL


def _seed_firm_agg(conn, firm_name, symbol, classification, buy_qty, sell_qty,
                   buy_value, sell_value, trade_date=D1):
    firm_id = conn.execute("SELECT id FROM firms WHERE name = ?", (firm_name,)).fetchone()[0]
    conn.execute(
        """INSERT INTO daily_firm_stock_agg
           (trade_date, firm_id, symbol, deal_type, buy_qty, sell_qty, buy_value,
            sell_value, buy_trades, sell_trades, net_qty, gross_qty, net_value,
            vwap_buy, vwap_sell, classification)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (trade_date, firm_id, symbol, "BULK", buy_qty, sell_qty, buy_value,
         sell_value, 1, 1, buy_qty - sell_qty, buy_qty + sell_qty,
         buy_value - sell_value, None, None, classification),
    )
    conn.commit()


def test_stock_rollup_with_churn_backdrop(conn):
    # Graviton cleanly buys; Optiver churns the same stock.
    _seed_firm_agg(conn, "Graviton Research Capital", "HOTSTOCK", CLEAN_BUY,
                   10_000, 0, 1e7, 0)
    _seed_firm_agg(conn, "Optiver", "HOTSTOCK", ROUND_TRIP,
                   50_000, 50_000, 5e7, 5.05e7)
    assert aggregate_stocks_date(conn, D1) == 1

    s = conn.execute("SELECT * FROM daily_stock_agg WHERE symbol = 'HOTSTOCK'").fetchone()
    assert s["classification"] == CLEAN_BUY  # directional flow label
    assert s["firm_count"] == 2
    assert "Graviton Research Capital" in s["firm_names"] and "Optiver" in s["firm_names"]
    assert s["churn_firm_count"] == 1
    assert s["churn_gross_value"] == 5e7 + 5.05e7
    # totals cover ALL tracked activity, churn included
    assert s["buy_qty"] == 60_000 and s["sell_qty"] == 50_000
    assert s["gross_value"] == 1e7 + 5e7 + 5.05e7
    assert s["net_value"] == 1e7 + 5e7 - 5.05e7
    assert s["dir_buy_qty"] == 10_000 and s["dir_sell_qty"] == 0
    assert s["first_time"] == 1


def test_stock_mixed_when_firms_disagree(conn):
    _seed_firm_agg(conn, "Graviton Research Capital", "MIXCO", CLEAN_BUY,
                   10_000, 0, 1e7, 0)
    _seed_firm_agg(conn, "Optiver", "MIXCO", CLEAN_SELL, 0, 9_000, 0, 0.9e7)
    aggregate_stocks_date(conn, D1)
    s = conn.execute("SELECT * FROM daily_stock_agg WHERE symbol = 'MIXCO'").fetchone()
    assert s["classification"] == MIXED
    assert s["dir_buy_qty"] == 10_000 and s["dir_sell_qty"] == 9_000


def test_stock_partial_net_when_buyer_churned(conn):
    _seed_firm_agg(conn, "Jump Trading", "PARTCO", NET_BUY, 100_000, 40_000, 1e7, 0.4e7)
    aggregate_stocks_date(conn, D1)
    s = conn.execute("SELECT * FROM daily_stock_agg WHERE symbol = 'PARTCO'").fetchone()
    assert s["classification"] == NET_BUY


def test_first_time_flag_clears_on_second_appearance(conn):
    _seed_firm_agg(conn, "Optiver", "REPEATCO", CLEAN_BUY, 1_000, 0, 1e6, 0, trade_date=D1)
    aggregate_stocks_date(conn, D1)
    _seed_firm_agg(conn, "Optiver", "REPEATCO", CLEAN_SELL, 0, 1_000, 0, 1e6, trade_date=D2)
    _seed_firm_agg(conn, "Optiver", "FRESHCO", ROUND_TRIP, 500, 500, 5e5, 5e5, trade_date=D2)
    aggregate_stocks_date(conn, D2)

    rows = {
        r["symbol"]: r
        for r in conn.execute("SELECT * FROM daily_stock_agg WHERE trade_date = ?", (D2,))
    }
    assert rows["REPEATCO"]["first_time"] == 0
    assert rows["FRESHCO"]["first_time"] == 1
    assert rows["FRESHCO"]["classification"] == ROUND_TRIP


def test_rollup_is_idempotent(conn):
    _seed_firm_agg(conn, "Optiver", "IDEMCO", CLEAN_BUY, 1_000, 0, 1e6, 0)
    assert aggregate_stocks_date(conn, D1) == 1
    assert aggregate_stocks_date(conn, D1) == 1
    n = conn.execute("SELECT COUNT(*) FROM daily_stock_agg").fetchone()[0]
    assert n == 1
