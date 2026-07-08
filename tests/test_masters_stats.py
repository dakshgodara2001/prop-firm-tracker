"""ISIN symbol mapping and firm performance stats."""
from proptracker import db, masters
from proptracker.models import RawDeal, SecurityRecord
from proptracker.stats import firm_performance

DATE = "2026-06-10"


def _seed_masters(conn):
    db.upsert_securities(
        conn,
        [
            SecurityRecord("NSE", "ABB", "ABB", "ABB India Limited", "INE117A01022"),
            SecurityRecord("BSE", "500002", "ABB", "ABB India Ltd", "INE117A01022"),
            SecurityRecord("BSE", "999999", "BSEONLY", "BSE Only Ltd", "INE000TEST99"),
        ],
    )


def test_bse_to_nse_mapping(conn):
    _seed_masters(conn)
    mapping = masters.build_bse_to_nse_map(conn)
    assert mapping == {"500002": "ABB"}

    deals = [
        RawDeal("BSE", "BULK", DATE, "ABB LTD", "ABB Ltd", "SOME CLIENT",
                "BUY", 100, 50.0, exchange_code="500002"),
        RawDeal("BSE", "BULK", DATE, "BSEONLY", "BSE Only Ltd", "OTHER CLIENT",
                "SELL", 200, 10.0, exchange_code="999999"),
    ]
    assert masters.map_bse_deals(conn, deals) == 1
    assert deals[0].symbol == "ABB"
    assert "BSE 500002" in deals[0].security_name
    assert deals[1].symbol == "BSEONLY"  # unmapped keeps scrip id


def test_unmapped_symbols_listed(conn):
    _seed_masters(conn)
    db.insert_raw_deals(
        conn,
        [RawDeal("BSE", "BULK", DATE, "BSEONLY", "BSE Only", "X", "BUY", 1, 1.0,
                 exchange_code="999999")],
    )
    assert masters.unmapped_symbols(conn, DATE) == ["BSEONLY"]


def test_master_age(conn):
    assert masters.master_age_days(conn, "NSE") is None
    _seed_masters(conn)
    age = masters.master_age_days(conn, "NSE")
    assert age is not None and age < 1


def _seed_agg(conn, firm_name, symbol, classification, net_qty, trade_date=DATE,
              buy_value=0.0, sell_value=0.0, r5=None):
    firm_id = conn.execute("SELECT id FROM firms WHERE name = ?", (firm_name,)).fetchone()[0]
    cur = conn.execute(
        """INSERT INTO daily_firm_stock_agg
           (trade_date, firm_id, symbol, deal_type, buy_qty, sell_qty, buy_value,
            sell_value, buy_trades, sell_trades, net_qty, gross_qty, net_value,
            vwap_buy, vwap_sell, classification)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (trade_date, firm_id, symbol, "BULK", max(net_qty, 0), max(-net_qty, 0),
         buy_value, sell_value, 1, 1, net_qty, abs(net_qty),
         buy_value - sell_value, None, None, classification),
    )
    if r5 is not None:
        conn.execute(
            """INSERT INTO deal_returns (agg_id, trade_date, symbol, base_price,
                                         base_kind, r1, r3, r5, r10, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (cur.lastrowid, trade_date, symbol, 100.0, "close", None, None, r5, None, "t"),
        )
    conn.commit()


def test_firm_performance_direction_adjusted(conn):
    # Graviton: a winning buy (+5% after) and a winning sell (-4% after)
    _seed_agg(conn, "Graviton Research Capital", "AAA", "CLEAN_BUY", 1000,
              buy_value=2e7, r5=5.0)
    _seed_agg(conn, "Graviton Research Capital", "BBB", "CLEAN_SELL", -1000,
              trade_date="2026-06-11", sell_value=1e7, r5=-4.0)
    # and one churn day
    _seed_agg(conn, "Graviton Research Capital", "CCC", "ROUND_TRIP", 0,
              trade_date="2026-06-12", buy_value=5e7, sell_value=5e7)

    rows = firm_performance(conn)
    assert len(rows) == 1
    s = rows[0]
    assert s["firm"] == "Graviton Research Capital"
    assert s["days"] == 3 and s["symbols"] == 3 and s["rows"] == 3
    assert s["signal_rows"] == 2 and s["churn_rows"] == 1
    assert abs(s["churn_share"] - 1 / 3) < 1e-9
    # buy +5% -> +5 adjusted; sell -4% -> +4 adjusted
    assert s["avg_adj_r5"] == 4.5
    assert s["hit_rate_r5"] == 1.0 and s["n_r5"] == 2


def test_firm_performance_since_filter(conn):
    _seed_agg(conn, "Optiver", "AAA", "CLEAN_BUY", 100, trade_date="2026-01-05")
    _seed_agg(conn, "Optiver", "BBB", "CLEAN_BUY", 100, trade_date="2026-06-15")
    rows = firm_performance(conn, since="2026-06-01")
    assert len(rows) == 1 and rows[0]["rows"] == 1
