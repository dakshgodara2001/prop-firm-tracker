"""Alert rules fire on the right conditions and stay idempotent."""
from proptracker import config
from proptracker.alerts import (
    CHURN_TO_CLEAN,
    CLEAN_NET,
    FIRST_TIME,
    HIGH_ATTENTION,
    LARGE_GROSS,
    MULTI_FIRM,
    REPEAT_MENTION,
    generate_alerts,
)
from proptracker.scoring import compute_attention

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


def _alert_types(conn, symbol, trade_date=DATE):
    return {
        r["alert_type"]
        for r in conn.execute(
            "SELECT alert_type FROM alerts WHERE symbol = ? AND trade_date = ?",
            (symbol, trade_date),
        )
    }


def _run(conn, trade_date=DATE):
    compute_attention(conn, trade_date)
    return generate_alerts(conn, trade_date)


def test_clean_net_first_time_and_multi_firm(conn):
    _seed_stock(conn, "PICKCO", "CLEAN_BUY",
                "Graviton Research Capital, Optiver", buy_value=5.8e7)
    _run(conn)
    types = _alert_types(conn, "PICKCO")
    assert CLEAN_NET in types
    assert FIRST_TIME in types
    assert MULTI_FIRM in types
    message = conn.execute(
        "SELECT message FROM alerts WHERE symbol='PICKCO' AND alert_type=?",
        (CLEAN_NET,),
    ).fetchone()["message"]
    assert "Clean buy of ₹5.80 Cr" in message


def test_clean_net_ignores_dust_and_mixed(conn):
    _seed_stock(conn, "DUSTCO", "CLEAN_BUY", "Optiver", buy_value=0.5e7)  # < 1 Cr
    _seed_stock(conn, "MIXCO", "MIXED", "Optiver, Jump Trading",
                buy_value=5e7, sell_value=4.8e7)
    _run(conn)
    assert CLEAN_NET not in _alert_types(conn, "DUSTCO")
    assert CLEAN_NET not in _alert_types(conn, "MIXCO")
    # anti-spam: a tiny first-timer stays out of the alert feed too
    assert FIRST_TIME not in _alert_types(conn, "DUSTCO")


def test_large_gross_fires_unless_high_attention_did(conn, monkeypatch):
    big = ("AlphaGrep Securities, Graviton Research Capital, Jump Trading, "
           "Optiver, iRage")
    _seed_stock(conn, "BIGCO", "ROUND_TRIP", big, buy_value=125e7, sell_value=125e7,
                churn_firms=5, churn_gross=250e7, first_time=0)
    _run(conn)
    # default HIGH_ATTENTION_MIN=60; this stock scores ~50 => LARGE_GROSS speaks
    types = _alert_types(conn, "BIGCO")
    assert LARGE_GROSS in types and HIGH_ATTENTION not in types
    message = conn.execute(
        "SELECT message FROM alerts WHERE symbol='BIGCO' AND alert_type=?",
        (LARGE_GROSS,),
    ).fetchone()["message"]
    assert "₹250.00 Cr gross" in message
    # lower the high-attention bar: HIGH fires and LARGE_GROSS is suppressed
    monkeypatch.setattr(config, "HIGH_ATTENTION_MIN", 40.0)
    _run(conn)
    types = _alert_types(conn, "BIGCO")
    assert HIGH_ATTENTION in types and LARGE_GROSS not in types


def test_high_attention_threshold(conn, monkeypatch):
    monkeypatch.setattr(config, "HIGH_ATTENTION_MIN", 40.0)
    _seed_stock(conn, "BIGCO", "ROUND_TRIP",
                "AlphaGrep Securities, Graviton Research Capital, Jump Trading, "
                "Optiver, iRage",
                buy_value=125e7, sell_value=125e7, churn_firms=5, churn_gross=250e7,
                first_time=0)
    _run(conn)
    assert HIGH_ATTENTION in _alert_types(conn, "BIGCO")
    # and a small single-firm stock does not trip it
    _seed_stock(conn, "TINYCO", "CLEAN_BUY", "Optiver", buy_value=1.5e7)
    _run(conn)
    assert HIGH_ATTENTION not in _alert_types(conn, "TINYCO")


def test_repeat_mention_counts_window_appearances(conn):
    for d in ("2026-06-02", "2026-06-05"):
        _seed_stock(conn, "AGAINCO", "ROUND_TRIP", "Optiver", buy_value=1e7,
                    sell_value=1e7, churn_firms=1, churn_gross=2e7,
                    trade_date=d, first_time=(d == "2026-06-02"))
    _seed_stock(conn, "AGAINCO", "ROUND_TRIP", "Optiver", buy_value=1e7,
                sell_value=1e7, churn_firms=1, churn_gross=2e7, first_time=0)
    _run(conn)
    types = _alert_types(conn, "AGAINCO")
    assert REPEAT_MENTION in types  # appearance #3 >= ALERT_REPEAT_MIN
    assert FIRST_TIME not in types
    message = conn.execute(
        "SELECT message FROM alerts WHERE symbol='AGAINCO' AND alert_type=?",
        (REPEAT_MENTION,),
    ).fetchone()["message"]
    assert "Appearance #3" in message


def test_churn_to_clean_shift(conn):
    for d in ("2026-06-03", "2026-06-05"):
        _seed_stock(conn, "SHIFTCO", "ROUND_TRIP", "Optiver", buy_value=5e7,
                    sell_value=5e7, churn_firms=1, churn_gross=10e7,
                    trade_date=d, first_time=(d == "2026-06-03"))
    _seed_stock(conn, "SHIFTCO", "NET_BUY", "Optiver", buy_value=6e7,
                sell_value=1.8e7, first_time=0)
    _run(conn)
    types = _alert_types(conn, "SHIFTCO")
    assert CHURN_TO_CLEAN in types
    message = conn.execute(
        "SELECT message FROM alerts WHERE symbol='SHIFTCO' AND alert_type=?",
        (CHURN_TO_CLEAN,),
    ).fetchone()["message"]
    assert "2 prior day(s) were pure round-trip" in message
    assert "Partial net buy" in message


def test_churn_to_clean_needs_pure_churn_history(conn):
    # one prior day only -> not enough history
    _seed_stock(conn, "THINCO", "ROUND_TRIP", "Optiver", buy_value=1e7,
                sell_value=1e7, trade_date="2026-06-05")
    _seed_stock(conn, "THINCO", "CLEAN_BUY", "Optiver", buy_value=5e7, first_time=0)
    # prior history contains a directional day -> no "shift"
    _seed_stock(conn, "ALREADYCO", "CLEAN_BUY", "Optiver", buy_value=5e7,
                trade_date="2026-06-03")
    _seed_stock(conn, "ALREADYCO", "ROUND_TRIP", "Optiver", buy_value=1e7,
                sell_value=1e7, trade_date="2026-06-05", first_time=0)
    _seed_stock(conn, "ALREADYCO", "CLEAN_BUY", "Optiver", buy_value=5e7, first_time=0)
    _run(conn)
    assert CHURN_TO_CLEAN not in _alert_types(conn, "THINCO")
    assert CHURN_TO_CLEAN not in _alert_types(conn, "ALREADYCO")


def test_alerts_idempotent(conn):
    _seed_stock(conn, "PICKCO", "CLEAN_BUY", "Optiver", buy_value=5e7)
    first = _run(conn)
    second = _run(conn)
    assert first == second
    count = conn.execute("SELECT COUNT(*) FROM alerts WHERE trade_date=?", (DATE,)).fetchone()[0]
    assert count == first
