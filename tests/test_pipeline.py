"""End-to-end pipeline tests with a fake fetcher (no network)."""
from datetime import date

from proptracker import pipeline
from proptracker.fetchers.base import BaseFetcher
from proptracker.models import (
    DEAL_BLOCK,
    DEAL_BULK,
    DEAL_SHORT,
    FETCH_FAILED,
    FETCH_OK,
    FetchOutcome,
    RawDeal,
    ShortSellRecord,
)

TRADE_DATE = date(2026, 7, 6)
ISO = TRADE_DATE.isoformat()


def _deal(client, side, qty, price, symbol, deal_type=DEAL_BULK):
    return RawDeal(
        source="NSE",
        deal_type=deal_type,
        trade_date=ISO,
        symbol=symbol,
        security_name=f"{symbol} Ltd",
        client_name=client,
        side=side,
        quantity=qty,
        price=price,
    )


class FakeFetcher(BaseFetcher):
    source = "NSE"

    def fetch_bulk_deals(self, trade_date):
        deals = [
            # clean directional buy by a tracked firm
            _deal("GRAVITON RESEARCH CAPITAL LLP", "BUY", 500_000, 50.0, "ALPHACO"),
            # round-trip churn by a tracked firm
            _deal("NK SECURITIES RESEARCH PRIVATE LIMITED", "BUY", 200_000, 100.0, "BETACO"),
            _deal("NK SECURITIES RESEARCH PRIVATE LIMITED", "SELL", 195_000, 101.0, "BETACO"),
            # untracked market participant
            _deal("RANDOM TRADER LLP", "BUY", 50_000, 10.0, "GAMMACO"),
        ]
        return FetchOutcome(DEAL_BULK, FETCH_OK, "api_csv", deals, [], f"{len(deals)} rows")

    def fetch_block_deals(self, trade_date):
        deals = [
            _deal("TOWER RESEARCH CAPITAL MARKETS INDIA PRIVATE LIMITED", "SELL",
                  80_000, 200.0, "DELTACO", DEAL_BLOCK),
        ]
        return FetchOutcome(DEAL_BLOCK, FETCH_OK, "api_csv", deals, [], "1 rows")

    def fetch_short_selling(self, trade_date):
        records = [ShortSellRecord("NSE", ISO, "BETACO", "Betaco Ltd", 12_345)]
        return FetchOutcome(DEAL_SHORT, FETCH_OK, "api_json", records, [], "1 rows")


class EmptyFetcher(BaseFetcher):
    source = "NSE"

    def _empty(self, deal_type):
        return FetchOutcome(deal_type, FETCH_OK, "api_csv", [], [], "0 rows")

    def fetch_bulk_deals(self, trade_date):
        return self._empty(DEAL_BULK)

    def fetch_block_deals(self, trade_date):
        return self._empty(DEAL_BLOCK)

    def fetch_short_selling(self, trade_date):
        return self._empty(DEAL_SHORT)


class FailingFetcher(EmptyFetcher):
    def fetch_bulk_deals(self, trade_date):
        return FetchOutcome(DEAL_BULK, FETCH_FAILED, message="api_csv: boom | api_json: boom")


def _run(tmp_path, fetcher, trade_date=TRADE_DATE):
    return pipeline.run_daily(
        trade_date,
        fetcher=fetcher,
        db_path=tmp_path / "t.db",
        raw_dir=tmp_path / "raw",
        reports_dir=tmp_path / "reports",
    )


def test_run_daily_end_to_end(tmp_path):
    summary = _run(tmp_path, FakeFetcher())

    assert summary.raw_inserted == 5
    assert summary.short_inserted == 1
    assert summary.processed_rows == 5
    assert summary.matched_rows == 4  # all but RANDOM TRADER LLP
    assert summary.firms_matched == 3
    assert summary.agg_rows == 3
    assert not summary.hard_fetch_failure

    text = summary.report_path.read_text(encoding="utf-8")
    # the morning-brief structure renders
    assert "# Morning Brief" in text
    for section in (
        "## 1 · Daily Overview",
        "## 2 · Top Stocks to Watch",
        "## 3 · What Changed",
        "## 4 · Clean Net Buy / Sell Activity",
        "## 5 · Multi-Firm Activity",
        "## 6 · New & Repeat Mentions",
        "## 7 · Round-Trip / Liquidity Churn",
        "## 8 · Watchlist Additions",
        "## 9 · Follow-Up Checklist",
        "## 10 · Appendix",
    ):
        assert section in text
    # directional vs churn separation
    assert "Graviton Research Capital" in text
    assert "Clean buy" in text
    assert "Tower Research Capital India" in text
    assert "Clean sell" in text
    assert "NK Securities Research" in text
    # the churn trade sits in the liquidity section, not presented as directional
    churn_section = text.split("## 7 · Round-Trip / Liquidity Churn")[1].split("## 8 ·")[0]
    assert "BETACO" in churn_section
    assert "not** as a bullish or bearish signal" in churn_section
    directional_section = text.split("## 4 · Clean Net Buy / Sell Activity")[1].split("## 5 ·")[0]
    assert "BETACO" not in directional_section
    # every stock gets a plain-English explanation (appendix)
    assert "What happened, stock by stock:" in text
    assert "liquidity/churn rather than directional accumulation" in text
    assert "worth tracking" in text
    # checklist has actionable items
    assert "- [ ]" in text
    # attention framed as attention, not advice
    assert "not** buy/sell advice" in text
    # disclosed names remain auditable in the appendix
    assert "NK SECURITIES RESEARCH PRIVATE LIMITED" in text


def test_run_daily_is_idempotent(tmp_path):
    first = _run(tmp_path, FakeFetcher())
    second = _run(tmp_path, FakeFetcher())

    assert second.raw_inserted == 0
    assert second.raw_duplicates == 5
    assert second.agg_rows == first.agg_rows == 3

    import sqlite3

    conn = sqlite3.connect(tmp_path / "t.db")
    assert conn.execute("SELECT COUNT(*) FROM raw_deals").fetchone()[0] == 5
    assert conn.execute("SELECT COUNT(*) FROM matched_deals").fetchone()[0] == 5
    assert conn.execute("SELECT COUNT(*) FROM daily_firm_stock_agg").fetchone()[0] == 3
    conn.close()


def test_run_daily_with_no_deals_at_all(tmp_path):
    sunday = date(2026, 7, 5)
    summary = _run(tmp_path, EmptyFetcher(), trade_date=sunday)

    assert summary.raw_inserted == 0
    assert summary.agg_rows == 0
    assert any("Sunday" in w for w in summary.warnings)
    text = summary.report_path.read_text(encoding="utf-8")
    assert "No tracked prop/HFT firms appeared" in text
    assert "No bulk/block deals stored" in text
    # the fixed structure still renders on an empty day
    assert "## 8 · Watchlist Additions" in text
    assert "## 9 · Follow-Up Checklist" in text


def test_run_daily_with_deals_but_no_tracked_firms(tmp_path):
    class UntrackedOnlyFetcher(EmptyFetcher):
        def fetch_bulk_deals(self, trade_date):
            deals = [_deal("PLAIN RETAIL INVESTOR", "BUY", 1_000, 5.0, "XYZ")]
            return FetchOutcome(DEAL_BULK, FETCH_OK, "api_csv", deals, [], "1 rows")

    summary = _run(tmp_path, UntrackedOnlyFetcher())
    assert summary.processed_rows == 1
    assert summary.matched_rows == 0
    assert summary.agg_rows == 0
    text = summary.report_path.read_text(encoding="utf-8")
    assert "No tracked prop/HFT firms appeared" in text
    assert "PLAIN RETAIL INVESTOR" in text  # still visible in market context


def test_run_daily_flags_fetch_failure(tmp_path):
    summary = _run(tmp_path, FailingFetcher())
    assert summary.hard_fetch_failure
    assert any("BULK fetch failed" in w for w in summary.warnings)
    # report still generated
    assert summary.report_path.exists()
