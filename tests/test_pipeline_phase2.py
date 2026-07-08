"""End-to-end Phase 2 pipeline: NSE + BSE fetchers, mapping, prices, scoring."""
from datetime import date

from proptracker import db, pipeline
from proptracker.fetchers.base import BaseFetcher
from proptracker.models import (
    DEAL_BLOCK,
    DEAL_BULK,
    DEAL_PRICES,
    DEAL_SHORT,
    FETCH_OK,
    FetchOutcome,
    RawDeal,
    SecurityRecord,
    StockDay,
)

TRADE_DATE = date(2026, 6, 10)
ISO = TRADE_DATE.isoformat()


def _deal(source, client, side, qty, price, symbol, code=""):
    return RawDeal(source, DEAL_BULK, ISO, symbol, f"{symbol} Ltd", client, side,
                   qty, price, exchange_code=code)


def _stock_day(trade_date, close, volume, prev=None, deliv=None):
    return StockDay("NSE", "ALPHACO", "EQ", trade_date, prev, close, close, close,
                    close, close, close, volume, close * volume, 10, deliv,
                    (deliv / volume * 100) if deliv else None)


class FakeNSE(BaseFetcher):
    source = "NSE"

    def fetch_bulk_deals(self, trade_date):
        deals = [
            _deal("NSE", "GRAVITON RESEARCH CAPITAL LLP", "BUY", 500_000, 50.0, "ALPHACO"),
            # churner: near-flat round trip in a second stock
            _deal("NSE", "NK SECURITIES RESEARCH PRIVATE LIMITED", "BUY", 200_000, 10.0, "BETACO"),
            _deal("NSE", "NK SECURITIES RESEARCH PRIVATE LIMITED", "SELL", 198_000, 10.1, "BETACO"),
        ]
        return FetchOutcome(DEAL_BULK, FETCH_OK, "api_csv", deals, [], f"{len(deals)} rows")

    def fetch_block_deals(self, trade_date):
        return FetchOutcome(DEAL_BLOCK, FETCH_OK, "api_csv", [], [], "0 rows")

    def fetch_short_selling(self, trade_date):
        return FetchOutcome(DEAL_SHORT, FETCH_OK, "api_csv", [], [], "0 rows")

    def fetch_price_history(self, trade_date):
        days = [
            _stock_day(f"2026-06-{d:02d}", 48.0, 1_000_000) for d in range(2, 9)
        ] + [_stock_day(ISO, 52.0, 3_000_000, prev=48.0, deliv=1_500_000)]
        return FetchOutcome(DEAL_PRICES, FETCH_OK, "bhav_full_csv", days, [],
                            f"{len(days)} rows")


class FakeBSE(BaseFetcher):
    source = "BSE"

    def fetch_bulk_deals(self, trade_date):
        deals = [_deal("BSE", "GRAVITON RESEARCH CAPITAL LLP", "BUY", 100_000, 50.5,
                       "ALPHACO_BSE", code="512345")]
        return FetchOutcome(DEAL_BULK, FETCH_OK, "api_json", deals, [], "1 rows")

    def fetch_block_deals(self, trade_date):
        return FetchOutcome(DEAL_BLOCK, FETCH_OK, "api_json", [], [], "0 rows")


def _seed_masters(db_path):
    conn = db.connect(db_path)
    db.init_db(conn)
    db.upsert_securities(
        conn,
        [
            SecurityRecord("NSE", "ALPHACO", "ALPHACO", "Alphaco Ltd", "INE0TEST0001"),
            SecurityRecord("BSE", "512345", "ALPHACO_BSE", "Alphaco Ltd", "INE0TEST0001"),
        ],
    )
    conn.close()


def test_phase2_end_to_end(tmp_path):
    db_path = tmp_path / "t.db"
    _seed_masters(db_path)  # fresh masters => no network refresh attempted

    summary = pipeline.run_daily(
        TRADE_DATE,
        fetchers=[FakeNSE(), FakeBSE()],
        db_path=db_path,
        raw_dir=tmp_path / "raw",
        reports_dir=tmp_path / "reports",
    )

    assert summary.raw_inserted == 4
    assert summary.bse_mapped == 1
    assert summary.prices_inserted == 8
    assert summary.matched_rows == 4 and summary.firms_matched == 2
    assert summary.agg_rows == 2  # ALPHACO (NSE+BSE merged) + BETACO churn
    assert summary.stock_rows == 2
    assert summary.new_stocks == 2
    assert summary.context_rows == 1  # BETACO has no price history
    assert summary.attention_rows == 2
    assert summary.returns_updated == 1
    # ALPHACO: clean-net + first-time. BETACO's first-time is suppressed by the
    # attention floor (tiny churn day) — anti-spam working as intended.
    assert summary.alert_rows == 2
    assert any("Clean buy" in m for m in summary.top_alerts)
    assert not summary.hard_fetch_failure

    conn = db.connect(db_path)
    agg = conn.execute(
        "SELECT * FROM daily_firm_stock_agg WHERE symbol = 'ALPHACO'"
    ).fetchone()
    assert agg["buy_qty"] == 600_000
    assert agg["classification"] == "CLEAN_BUY"
    stock = conn.execute(
        "SELECT * FROM daily_stock_agg WHERE symbol = 'BETACO'"
    ).fetchone()
    assert stock["classification"] == "ROUND_TRIP"
    assert stock["churn_firm_count"] == 1

    ctx = conn.execute("SELECT * FROM market_context WHERE trade_date = ?", (ISO,)).fetchone()
    assert ctx["adv20"] == 1_000_000.0 and ctx["volume_vs_adv"] == 3.0
    assert round(ctx["day_return_pct"], 4) == round(100 * 4 / 48, 4)

    scores = {
        r["symbol"]: r
        for r in conn.execute("SELECT * FROM attention_scores WHERE trade_date = ?", (ISO,))
    }
    assert set(scores) == {"ALPHACO", "BETACO"}
    assert scores["ALPHACO"]["score"] > 0
    assert "clean net buy" in scores["ALPHACO"]["explanation"]
    assert "worth tracking" in scores["ALPHACO"]["explanation"]
    assert "liquidity/churn" in scores["BETACO"]["explanation"]
    conn.close()

    text = summary.report_path.read_text(encoding="utf-8")
    # morning-brief structure
    assert "# Morning Brief" in text
    assert "## 1 · Daily Overview" in text
    assert "## 2 · Top Stocks to Watch" in text
    assert "**ALPHACO**" in text and "★" in text
    assert "not** as a bullish or bearish signal" in text
    assert "What happened, stock by stock:" in text
    # ALPHACO is directional + first-time, so it must reach the watchlist section
    watchlist_section = text.split("## 8 · Watchlist Additions")[1].split("## 9 ·")[0]
    assert "ALPHACO" in watchlist_section
    assert "rank attention, not expected returns" in watchlist_section
    # first stored session — nothing to compare
    assert "First stored session" in text
    # appendix keeps the audit trail
    assert "| BSE | BULK |" in text  # per-source overview
    assert "Vol vs ADV" in text


def test_phase2_report_has_follow_through(tmp_path):
    db_path = tmp_path / "t.db"
    _seed_masters(db_path)
    # Day 1 creates the signal + history; day 2's run should show follow-through.
    pipeline.run_daily(TRADE_DATE, fetchers=[FakeNSE(), FakeBSE()], db_path=db_path,
                       raw_dir=tmp_path / "raw", reports_dir=tmp_path / "reports")

    class QuietNSE(FakeNSE):
        def fetch_bulk_deals(self, trade_date):
            return FetchOutcome(DEAL_BULK, FETCH_OK, "api_csv", [], [], "0 rows")

        def fetch_price_history(self, trade_date):
            days = [_stock_day("2026-06-11", 55.0, 1_000_000, prev=52.0)]
            return FetchOutcome(DEAL_PRICES, FETCH_OK, "bhav_full_csv", days, [], "1 rows")

    summary = pipeline.run_daily(date(2026, 6, 11), fetchers=[QuietNSE()], db_path=db_path,
                                 raw_dir=tmp_path / "raw", reports_dir=tmp_path / "reports")
    text = summary.report_path.read_text(encoding="utf-8")
    assert "No tracked prop/HFT firms appeared" in text
    assert "Price follow-up on recent directional days" in text
    assert "Graviton Research Capital" in text  # yesterday's deal with its T+1
