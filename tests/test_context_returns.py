"""Market context and post-deal return computation on synthetic history."""
from proptracker import db
from proptracker.context import compute_market_context, trailing_adv
from proptracker.db import insert_raw_deals, insert_stock_history
from proptracker.models import RawDeal, StockDay
from proptracker.returns import update_deal_returns

DEAL_DATE = "2026-06-10"


def _day(symbol, trade_date, close, volume, series="EQ", prev_close=None,
         deliv_qty=None, deliv_per=None):
    return StockDay(
        source="NSE", symbol=symbol, series=series, trade_date=trade_date,
        prev_close=prev_close, open=close, high=close, low=close, close=close,
        last=close, avg_price=close, volume=volume, turnover=close * volume,
        trades=100, deliv_qty=deliv_qty, deliv_per=deliv_per,
    )


def _seed_history(conn, symbol="TESTCO", sessions_before=21, base_vol=100_000):
    days = []
    for i in range(sessions_before):
        d = f"2026-05-{i + 1:02d}"  # synthetic consecutive sessions
        days.append(_day(symbol, d, close=100.0, volume=base_vol))
    days.append(
        _day(symbol, DEAL_DATE, close=110.0, volume=500_000, prev_close=100.0,
             deliv_qty=200_000, deliv_per=40.0)
    )
    insert_stock_history(conn, days)


def _seed_deal(conn, client="GRAVITON RESEARCH CAPITAL LLP", symbol="TESTCO"):
    insert_raw_deals(
        conn,
        [RawDeal("NSE", "BULK", DEAL_DATE, symbol, "Test Co", client, "BUY", 50_000, 105.0)],
    )


def test_market_context_math(conn):
    _seed_history(conn)
    _seed_deal(conn)
    assert compute_market_context(conn, DEAL_DATE) == 1
    ctx = conn.execute("SELECT * FROM market_context WHERE trade_date = ?", (DEAL_DATE,)).fetchone()
    assert ctx["symbol"] == "TESTCO" and ctx["series"] == "EQ"
    assert ctx["day_return_pct"] == 10.0
    assert ctx["adv20"] == 100_000.0
    assert ctx["adv_days"] == 20  # window capped even though 21 sessions exist
    assert ctx["volume_vs_adv"] == 5.0
    assert ctx["deliv_per"] == 40.0


def test_market_context_skips_symbols_without_history(conn):
    _seed_deal(conn, symbol="NOHISTORY")
    assert compute_market_context(conn, DEAL_DATE) == 0


def test_trailing_adv_requires_minimum_days(conn):
    insert_stock_history(
        conn, [_day("THIN", f"2026-06-0{i}", 50.0, 1000) for i in range(1, 4)]
    )
    adv, n = trailing_adv(conn, "THIN", DEAL_DATE, "EQ")
    assert adv is None and n == 3


def _seed_agg(conn, classification, vwap_buy=None, vwap_sell=None, net_qty=1000,
              symbol="TESTCO", trade_date=DEAL_DATE):
    firm_id = conn.execute("SELECT id FROM firms LIMIT 1").fetchone()[0]
    cur = conn.execute(
        """INSERT INTO daily_firm_stock_agg
           (trade_date, firm_id, symbol, deal_type, buy_qty, sell_qty, buy_value,
            sell_value, buy_trades, sell_trades, net_qty, gross_qty, net_value,
            vwap_buy, vwap_sell, classification)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (trade_date, firm_id, symbol, "BULK", max(net_qty, 0), max(-net_qty, 0),
         0, 0, 1, 1, net_qty, abs(net_qty), 0, vwap_buy, vwap_sell, classification),
    )
    conn.commit()
    return cur.lastrowid


def test_returns_use_vwap_base_and_fill_progressively(conn):
    _seed_history(conn)  # deal day close 110
    # six sessions after the deal: closes 112, 114, 116, 118, 120, 122
    insert_stock_history(
        conn,
        [_day("TESTCO", f"2026-06-{11 + i:02d}", 112.0 + 2 * i, 100_000) for i in range(6)],
    )
    agg_id = _seed_agg(conn, "CLEAN_BUY", vwap_buy=100.0)
    assert update_deal_returns(conn, as_of=DEAL_DATE) == 1
    r = conn.execute("SELECT * FROM deal_returns WHERE agg_id = ?", (agg_id,)).fetchone()
    assert r["base_kind"] == "vwap_buy" and r["base_price"] == 100.0
    assert r["r1"] == 12.0   # 112 vs 100
    assert r["r3"] == 16.0   # 116
    assert r["r5"] == 20.0   # 120
    assert r["r10"] is None  # only 6 sessions elapsed


def test_returns_round_trip_uses_close_base(conn):
    _seed_history(conn)
    insert_stock_history(conn, [_day("TESTCO", "2026-06-11", 99.0, 100_000)])
    agg_id = _seed_agg(conn, "ROUND_TRIP", vwap_buy=105.0, vwap_sell=105.5, net_qty=0)
    update_deal_returns(conn, as_of=DEAL_DATE)
    r = conn.execute("SELECT * FROM deal_returns WHERE agg_id = ?", (agg_id,)).fetchone()
    assert r["base_kind"] == "close" and r["base_price"] == 110.0
    assert r["r1"] == (99.0 - 110.0) / 110.0 * 100.0


def test_returns_skipped_without_deal_day_history(conn):
    _seed_agg(conn, "CLEAN_BUY", vwap_buy=100.0, symbol="NOHISTORY")
    assert update_deal_returns(conn, as_of=DEAL_DATE) == 0
