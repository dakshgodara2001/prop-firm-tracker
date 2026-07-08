"""Stock attention scoring, explanations, and cluster detection."""
import json

from proptracker.scoring import (
    attention_from_components,
    compute_attention,
    detect_clusters,
)

DATE = "2026-06-10"
D_PREV = "2026-06-09"


def _seed_stock(conn, symbol, classification, firm_names, buy_value=0.0,
                sell_value=0.0, churn_firms=0, churn_gross=0.0, first_time=1,
                trade_date=DATE, buy_qty=0, sell_qty=0):
    firm_count = len(firm_names.split(", "))
    conn.execute(
        """INSERT INTO daily_stock_agg
           (trade_date, symbol, security_name, firm_count, firm_names,
            buy_qty, sell_qty, buy_value, sell_value, gross_value, net_value,
            net_qty, dir_buy_qty, dir_sell_qty, churn_firm_count,
            churn_gross_value, classification, first_time)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (trade_date, symbol, "", firm_count, firm_names, buy_qty, sell_qty,
         buy_value, sell_value, buy_value + sell_value, buy_value - sell_value,
         buy_qty - sell_qty, max(buy_qty - sell_qty, 0), max(sell_qty - buy_qty, 0),
         churn_firms, churn_gross, classification, first_time),
    )
    conn.commit()


def _seed_context(conn, symbol, day_return_pct=2.5, volume_vs_adv=3.2, trade_date=DATE):
    conn.execute(
        """INSERT INTO market_context
           (trade_date, symbol, series, close, prev_close, day_return_pct, volume,
            turnover, deliv_qty, deliv_per, adv20, adv_days, volume_vs_adv)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (trade_date, symbol, "EQ", 102.0, 100.0, day_return_pct, 500_000, 5.1e7,
         200_000, 40.0, 100_000.0, 20, volume_vs_adv),
    )
    conn.commit()


def _score_row(conn, symbol):
    return conn.execute(
        "SELECT * FROM attention_scores WHERE symbol = ? AND trade_date = ?",
        (symbol, DATE),
    ).fetchone()


def test_attention_weighting_and_renormalization():
    full = {"firms": 1.0, "multi": 1.0, "gross": 1.0, "net": 1.0, "clean": 1.0,
            "context": 1.0, "repeat": 1.0}
    assert attention_from_components(full) == 100.0
    # missing context renormalizes instead of dragging the score down
    no_ctx = dict(full, context=None)
    assert attention_from_components(no_ctx) == 100.0
    assert attention_from_components({k: 0.0 for k in full}) == 0.0


def test_churn_still_earns_attention(conn):
    # A five-firm ₹250 Cr churn frenzy is exactly the kind of event to notice —
    # attention carries no direction, so no conviction gate zeroes it out.
    _seed_stock(conn, "EVENTCO", "ROUND_TRIP",
                "AlphaGrep Securities, Graviton Research Capital, Jump Trading, "
                "Optiver, iRage",
                buy_value=125e7, sell_value=125e7, churn_firms=5, churn_gross=250e7,
                first_time=0)
    assert compute_attention(conn, DATE) == 1
    row = _score_row(conn, "EVENTCO")
    assert row["score"] > 40
    components = json.loads(row["components"])
    assert components["clean"] == 0.0 and components["gross"] == 1.0
    assert components["context"] is None  # no price history seeded


def test_clean_buy_explanation_and_context(conn):
    _seed_stock(conn, "XYZCO", "CLEAN_BUY", "Graviton Research Capital",
                buy_value=5.8e7, buy_qty=100_000)
    _seed_context(conn, "XYZCO")
    compute_attention(conn, DATE)
    row = _score_row(conn, "XYZCO")
    text = row["explanation"]
    assert "picked by Graviton Research Capital" in text
    assert "clean net buy of ₹5.80 Cr" in text
    assert "No same-day selling" in text
    assert "worth tracking" in text
    assert "Volume ran 3.2× the 20-day average" in text
    assert "+2.5% on the day" in text
    assert "First appearance in stored history." in text
    assert json.loads(row["components"])["context"] is not None


def test_churn_explanation_wording(conn):
    _seed_stock(conn, "ABCCO", "ROUND_TRIP",
                "AlphaGrep Securities, QE Securities / Quadeye",
                buy_value=6.3e7, sell_value=6.1e7, churn_firms=2, churn_gross=12.4e7)
    compute_attention(conn, DATE)
    text = _score_row(conn, "ABCCO")["explanation"]
    assert "touched by AlphaGrep Securities and QE Securities / Quadeye" in text
    assert "₹12.40 Cr" in text
    assert "liquidity/churn rather than directional accumulation" in text
    # no directional advice language
    assert "worth tracking" not in text


def test_mixed_and_partial_explanations(conn):
    _seed_stock(conn, "MIXCO", "MIXED", "Graviton Research Capital, Optiver",
                buy_value=5e7, sell_value=4.8e7, buy_qty=50_000, sell_qty=48_000)
    _seed_stock(conn, "PARTCO", "NET_SELL", "Jump Trading",
                buy_value=2e7, sell_value=6e7, buy_qty=20_000, sell_qty=60_000)
    compute_attention(conn, DATE)
    assert "both sides" in _score_row(conn, "MIXCO")["explanation"]
    part = _score_row(conn, "PARTCO")["explanation"]
    assert "net sellers of ₹4.00 Cr" in part and "partial" in part


def test_repeat_appearance_component_and_suffix(conn):
    _seed_stock(conn, "REPEATCO", "ROUND_TRIP", "Optiver", buy_value=1e7,
                sell_value=1e7, churn_firms=1, churn_gross=2e7,
                trade_date=D_PREV, first_time=1)
    _seed_stock(conn, "REPEATCO", "ROUND_TRIP", "Optiver", buy_value=1e7,
                sell_value=1e7, churn_firms=1, churn_gross=2e7, first_time=0)
    compute_attention(conn, DATE)
    row = _score_row(conn, "REPEATCO")
    assert json.loads(row["components"])["repeat"] == 0.25  # 1 of cap 4
    assert "Also appeared on 1 day(s) in the last 30." in row["explanation"]


def test_multi_firm_lifts_attention(conn):
    _seed_stock(conn, "SOLO", "CLEAN_BUY", "Optiver", buy_value=5e7, buy_qty=1000)
    _seed_stock(conn, "CROWD", "CLEAN_BUY",
                "Graviton Research Capital, Jump Trading, Optiver",
                buy_value=5e7, buy_qty=1000)
    compute_attention(conn, DATE)
    assert _score_row(conn, "CROWD")["score"] > _score_row(conn, "SOLO")["score"]


# --- clusters (unchanged behavior) -------------------------------------------


def _seed_firm_agg(conn, firm_name, symbol, classification, net_qty, net_value,
                   buy_value=0.0, sell_value=0.0):
    firm_id = conn.execute("SELECT id FROM firms WHERE name = ?", (firm_name,)).fetchone()[0]
    conn.execute(
        """INSERT INTO daily_firm_stock_agg
           (trade_date, firm_id, symbol, deal_type, buy_qty, sell_qty, buy_value,
            sell_value, buy_trades, sell_trades, net_qty, gross_qty, net_value,
            vwap_buy, vwap_sell, classification)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (DATE, firm_id, symbol, "BULK", max(net_qty, 0), max(-net_qty, 0),
         buy_value, sell_value, 1, 1, net_qty, abs(net_qty), net_value,
         None, None, classification),
    )
    conn.commit()


def test_cluster_detection(conn):
    for firm in ("Graviton Research Capital", "Optiver", "Jump Trading"):
        _seed_firm_agg(conn, firm, "HOTSTOCK", "CLEAN_BUY", 10_000, 5e7)
    _seed_firm_agg(conn, "XTX Markets", "HOTSTOCK", "CLEAN_SELL", -2_000, -1e7)
    assert detect_clusters(conn, DATE) == 1  # 3 buyers cluster; 1 seller doesn't
    c = conn.execute("SELECT * FROM clusters WHERE trade_date = ?", (DATE,)).fetchone()
    assert c["symbol"] == "HOTSTOCK" and c["side"] == "BUY" and c["firm_count"] == 3
    assert "Graviton Research Capital" in c["firm_names"]
    assert "|" not in c["firm_names"]  # pipes would break markdown tables
    assert c["combined_net_value"] == 15e7


def test_churn_cluster_needs_three_firms(conn):
    for firm in ("Graviton Research Capital", "Optiver"):
        _seed_firm_agg(conn, firm, "EVENTCO", "ROUND_TRIP", 0, 0.0,
                       buy_value=1e7, sell_value=1e7)
    assert detect_clusters(conn, DATE) == 0
    _seed_firm_agg(conn, "Jump Trading", "EVENTCO", "ROUND_TRIP", 0, 0.0,
                   buy_value=1e7, sell_value=1e7)
    assert detect_clusters(conn, DATE) == 1
    c = conn.execute("SELECT * FROM clusters WHERE symbol = 'EVENTCO'").fetchone()
    assert c["side"] == "CHURN" and c["firm_count"] == 3
    assert c["combined_net_value"] == 6e7  # gross turnover for churn clusters
