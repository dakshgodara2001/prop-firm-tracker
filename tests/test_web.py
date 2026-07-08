"""Dashboard routes render stock-first content from a seeded database."""
from datetime import date

import pytest

from proptracker import pipeline
from proptracker.web import create_app
from tests.test_pipeline_phase2 import FakeBSE, FakeNSE, _seed_masters

TRADE_DATE = date(2026, 6, 10)


@pytest.fixture
def client(tmp_path):
    db_path = tmp_path / "t.db"
    _seed_masters(db_path)
    pipeline.run_daily(
        TRADE_DATE,
        fetchers=[FakeNSE(), FakeBSE()],
        db_path=db_path,
        raw_dir=tmp_path / "raw",
        reports_dir=tmp_path / "reports",
    )
    app = create_app(db_path=db_path)
    app.config["TESTING"] = True
    with app.test_client() as test_client:
        yield test_client


def test_dashboard_shows_command_center_sections(client):
    html = client.get("/").get_data(as_text=True)
    assert "Daily overview" in html
    assert "Top stocks to watch" in html
    assert "Follow-up checklist" in html
    assert "High attention" in html
    assert "Stocks picked by tracked firms" in html
    assert "Multi-firm activity" in html
    assert "Clean net buy / sell" in html
    assert "Round-trip / liquidity churn" in html
    assert "New stock mentions" in html
    assert "Repeat mentions" in html
    assert "Watchlist additions" in html
    assert "Raw deals / audit trail" in html
    assert "ALPHACO" in html and "BETACO" in html
    assert "GRAVITON RESEARCH CAPITAL LLP" in html  # raw audit rows on dashboard
    assert "alert(s)" in html
    assert "not buy/sell advice" in html


def test_dashboard_has_coverage_universe_wall(client):
    html = client.get("/").get_data(as_text=True)
    assert "Coverage universe" in html
    assert "tracked firms active this session" in html
    assert 'class="ftile' in html          # monogram tiles
    assert "ftile live" in html            # at least one firm active today
    assert "not the firms' trademarks" in html   # honest labelling, no real logos


def test_dashboard_has_firm_stock_matrix(client):
    html = client.get("/").get_data(as_text=True)
    assert "Firm × stock activity map" in html
    assert "<svg" in html and "Firm by stock activity matrix" in html
    assert "<circle" in html          # at least one firm-stock dot
    assert "Round-trip / churn" in html  # legend present (multi-series)


def test_stock_page_has_price_chart(client):
    html = client.get("/stock/ALPHACO").get_data(as_text=True)
    assert "Price &amp; tracked-firm entries" in html
    assert "close price with tracked-firm activity markers" in html
    assert "<polyline" in html         # the price line


def test_healthz(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["status"] == "ok"
    assert payload["last_data_date"] == "2026-06-10"


def test_stock_page_is_complete(client):
    html = client.get("/stock/ALPHACO").get_data(as_text=True)
    assert "Firm-wise activity" in html
    assert "Graviton Research Capital" in html
    assert "GRAVITON RESEARCH CAPITAL LLP" in html  # raw disclosed name
    assert "Appearance history" in html
    assert "Raw deal rows" in html
    assert "Price / volume context" in html
    assert "Clean buy" in html
    assert "first-time" in html
    # NSE + BSE legs both visible in raw rows
    assert ">NSE<" in html and ">BSE<" in html


def test_stock_page_case_insensitive_and_404(client):
    assert client.get("/stock/alphaco").status_code == 200
    response = client.get("/stock/NOSUCH")
    assert response.status_code == 404
    assert "No data available" in response.get_data(as_text=True)


def test_firm_pages(client):
    html = client.get("/firms").get_data(as_text=True)
    assert "Graviton Research Capital" in html
    assert "Directional rows" in html and "Round-trip rows" in html

    # follow the first firm link that has activity
    import re

    firm_ids = re.findall(r'href="/firm/(\d+)"', html)
    assert firm_ids
    page = client.get(f"/firm/{firm_ids[0]}").get_data(as_text=True)
    assert "Recent stocks touched" in page
    assert "directional vs round-trip rows" in page
    assert client.get("/firm/99999").status_code == 404


def test_stocks_index_and_alerts_feed(client):
    html = client.get("/stocks").get_data(as_text=True)
    assert "ALPHACO" in html and "Appearances" in html

    feed = client.get("/alerts").get_data(as_text=True)
    assert "First-time mention" in feed
    assert "Clean net activity" in feed
    assert "ALPHACO" in feed
