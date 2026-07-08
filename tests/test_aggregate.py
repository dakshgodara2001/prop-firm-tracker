from proptracker.aggregate import (
    CLEAN_BUY,
    CLEAN_SELL,
    NET_BUY,
    NET_SELL,
    ROUND_TRIP,
    aggregate_date,
    classify,
)
from proptracker.db import insert_raw_deals
from proptracker.models import RawDeal
from proptracker.pipeline import process_date

DATE = "2026-07-06"


def test_classify_one_sided():
    assert classify(100, 0) == CLEAN_BUY
    assert classify(0, 50) == CLEAN_SELL


def test_classify_round_trip():
    assert classify(100, 100) == ROUND_TRIP  # perfect flat
    assert classify(110, 90) == ROUND_TRIP  # |net|=20 of gross 200 -> 10%
    assert classify(120, 80) == ROUND_TRIP  # exactly at the 20% boundary


def test_classify_mixed_with_residual():
    assert classify(150, 50) == NET_BUY  # 50% of gross
    assert classify(50, 150) == NET_SELL


def test_classify_respects_custom_ratio():
    assert classify(110, 90, round_trip_ratio=0.05) == NET_BUY


def _deal(client, side, qty, price, symbol="TESTCO", deal_type="BULK"):
    return RawDeal(
        source="NSE",
        deal_type=deal_type,
        trade_date=DATE,
        symbol=symbol,
        security_name="Test Co Ltd",
        client_name=client,
        side=side,
        quantity=qty,
        price=price,
    )


def test_aggregate_math_and_classification(conn):
    insert_raw_deals(
        conn,
        [
            # Graviton churns TESTCO: two buy prints, one sell, tiny residual
            _deal("GRAVITON RESEARCH CAPITAL LLP", "BUY", 60_000, 100.0),
            _deal("GRAVITON RESEARCH CAPITAL LLP", "BUY", 40_000, 110.0),
            _deal("GRAVITON RESEARCH CAPITAL LLP", "SELL", 95_000, 105.0),
            # Tower cleanly buys OTHERCO in the block window
            _deal("TOWER RESEARCH CAPITAL MARKETS INDIA PRIVATE LIMITED", "BUY",
                  50_000, 200.0, symbol="OTHERCO", deal_type="BLOCK"),
            # Untracked participant is ignored by aggregation
            _deal("SOME RANDOM INVESTOR", "BUY", 10_000, 5.0),
        ],
    )
    process_date(conn, DATE)
    assert aggregate_date(conn, DATE) == 2

    rows = {
        (r["symbol"], r["deal_type"]): r
        for r in conn.execute("SELECT * FROM daily_firm_stock_agg WHERE trade_date = ?", (DATE,))
    }

    graviton = rows[("TESTCO", "BULK")]
    assert graviton["buy_qty"] == 100_000 and graviton["sell_qty"] == 95_000
    assert graviton["gross_qty"] == 195_000 and graviton["net_qty"] == 5_000
    assert graviton["buy_trades"] == 2 and graviton["sell_trades"] == 1
    assert graviton["vwap_buy"] == 104.0  # (60k*100 + 40k*110) / 100k
    assert graviton["vwap_sell"] == 105.0
    assert graviton["classification"] == ROUND_TRIP

    tower = rows[("OTHERCO", "BLOCK")]
    assert tower["classification"] == CLEAN_BUY
    assert tower["net_value"] == 50_000 * 200.0


def test_aggregate_is_idempotent(conn):
    insert_raw_deals(conn, [_deal("GRAVITON RESEARCH CAPITAL LLP", "BUY", 1_000, 10.0)])
    process_date(conn, DATE)
    assert aggregate_date(conn, DATE) == 1
    assert aggregate_date(conn, DATE) == 1  # rebuild, not duplicate
    count = conn.execute(
        "SELECT COUNT(*) FROM daily_firm_stock_agg WHERE trade_date = ?", (DATE,)
    ).fetchone()[0]
    assert count == 1
