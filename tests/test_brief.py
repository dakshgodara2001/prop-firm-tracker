"""Morning-brief intelligence: KPIs, stance, changes vs yesterday, checklist."""
from proptracker import brief
from proptracker.scoring import compute_attention

D_PREV = "2026-06-09"
DATE = "2026-06-10"


def _seed_stock(conn, symbol, classification, firm_names, buy_value=0.0,
                sell_value=0.0, churn_firms=0, churn_gross=0.0, first_time=1,
                trade_date=DATE):
    firm_count = len(firm_names.split(", "))
    conn.execute(
        """INSERT INTO daily_stock_agg
           (trade_date, symbol, security_name, firm_count, firm_names,
            buy_qty, sell_qty, buy_value, sell_value, gross_value, net_value,
            net_qty, dir_buy_qty, dir_sell_qty, churn_firm_count,
            churn_gross_value, classification, first_time)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (trade_date, symbol, "", firm_count, firm_names, 0, 0, buy_value,
         sell_value, buy_value + sell_value, buy_value - sell_value, 0, 0, 0,
         churn_firms, churn_gross, classification, first_time),
    )
    conn.commit()


def _seed_firm_agg(conn, symbol, deal_type="BULK", trade_date=DATE):
    firm_id = conn.execute("SELECT id FROM firms LIMIT 1").fetchone()[0]
    conn.execute(
        """INSERT OR IGNORE INTO daily_firm_stock_agg
           (trade_date, firm_id, symbol, deal_type, buy_qty, sell_qty, buy_value,
            sell_value, buy_trades, sell_trades, net_qty, gross_qty, net_value,
            vwap_buy, vwap_sell, classification)
           VALUES (?,?,?,?,1,0,1,0,1,0,1,1,1,NULL,NULL,'CLEAN_BUY')""",
        (trade_date, firm_id, symbol, deal_type),
    )
    conn.commit()


def test_enrich_includes_deal_types_and_recent_count(conn):
    _seed_stock(conn, "ENRICHCO", "ROUND_TRIP", "Optiver", buy_value=1e7,
                sell_value=1e7, trade_date=D_PREV)
    _seed_stock(conn, "ENRICHCO", "CLEAN_BUY", "Optiver", buy_value=5e7, first_time=0)
    _seed_firm_agg(conn, "ENRICHCO", "BULK")
    _seed_firm_agg(conn, "ENRICHCO", "BLOCK")
    stocks = brief.enrich_stocks(conn, DATE)
    s = stocks[0]
    assert set((s["deal_types"] or "").split(",")) == {"BULK", "BLOCK"}
    assert s["recent_count"] == 1


def test_kpis_and_stance_churn_heavy(conn):
    _seed_stock(conn, "A", "ROUND_TRIP", "Optiver, Jump Trading", buy_value=50e7,
                sell_value=50e7, churn_firms=2, churn_gross=100e7)
    _seed_stock(conn, "B", "ROUND_TRIP", "Optiver", buy_value=10e7,
                sell_value=10e7, churn_firms=1, churn_gross=20e7)
    stocks = brief.enrich_stocks(conn, DATE)
    kpis = brief.day_kpis(conn, DATE, stocks)
    assert kpis["stocks"] == 2 and kpis["firms"] == 2
    assert kpis["gross"] == 120e7
    assert kpis["churn_share"] == 1.0
    assert kpis["multi_firm"] == 1
    stance = brief.stance_line(stocks, kpis)
    assert "no clean directional flow" in stance
    assert "churn-heavy" in stance


def test_changes_vs_previous_detects_shift_and_dropouts(conn):
    _seed_stock(conn, "SHIFTCO", "ROUND_TRIP", "Optiver", buy_value=5e7,
                sell_value=5e7, churn_firms=1, churn_gross=10e7, trade_date=D_PREV)
    _seed_stock(conn, "GONECO", "ROUND_TRIP", "Optiver", buy_value=9e7,
                sell_value=9e7, churn_firms=1, churn_gross=18e7, trade_date=D_PREV)
    _seed_stock(conn, "SHIFTCO", "CLEAN_BUY", "Optiver", buy_value=6e7, first_time=0)
    stocks = brief.enrich_stocks(conn, DATE)
    changes = brief.changes_vs_previous(conn, DATE, stocks)
    assert changes["prev_date"] == D_PREV
    text = " ".join(changes["bullets"])
    assert "SHIFTCO" in text and "turned directional" in text
    assert "GONECO" in text and "Gone quiet" in text


def test_checklist_watchlist_and_fallback(conn):
    stocks = brief.enrich_stocks(conn, DATE)
    items = brief.follow_up_checklist(conn, DATE, stocks, [])
    assert items == ["Nothing actionable — churn-only day. Skim the liquidity table and move on."]

    _seed_stock(conn, "WATCHCO", "CLEAN_BUY", "Graviton Research Capital",
                buy_value=6e7)
    compute_attention(conn, DATE)
    stocks = brief.enrich_stocks(conn, DATE)
    items = brief.follow_up_checklist(conn, DATE, stocks, stocks)
    assert any("WATCHCO" in i and "Check news/results" in i for i in items)
