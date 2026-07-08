"""Parsers for Phase 2 payloads, captured from live NSE/BSE endpoints (2026-07)."""
import pytest

from proptracker.fetchers.base import FetchError
from proptracker.fetchers.bse import parse_bse_deals_json, parse_bse_master_json
from proptracker.fetchers.nse import parse_bhavdata_csv, parse_equity_master_csv

BHAV_FULL_CSV = """SYMBOL, SERIES, DATE1, PREV_CLOSE, OPEN_PRICE, HIGH_PRICE, LOW_PRICE, LAST_PRICE, CLOSE_PRICE, AVG_PRICE, TTL_TRD_QNTY, TURNOVER_LACS, NO_OF_TRADES, DELIV_QTY, DELIV_PER
1018GS2026, GS, 06-Jul-2026, 103.85, 101.25, 104.50, 101.25, 104.05, 104.05, 103.85, 465, 0.48, 7, 282, 60.65
20MICRONS, EQ, 06-Jul-2026, 204.36, 204.56, 209.95, 195.00, 195.02, 196.07, 200.51, 391809, 785.61, 7781, 166603, 42.52
NODELIV, BE, 06-Jul-2026, 10.00, 10.10, 10.50, 9.90, 10.20, 10.25, 10.15, 5000, 0.51, 12,  -,  -
"""

EQUITY_L_CSV = """SYMBOL,NAME OF COMPANY, SERIES, DATE OF LISTING, PAID UP VALUE, MARKET LOT, ISIN NUMBER, FACE VALUE
20MICRONS,20 Microns Limited,EQ,06-OCT-2008,5,1,INE144J01027,5
ABB,ABB India Limited,EQ,13-FEB-1995,2,1,INE117A01022,2
"""

BSE_DEALS_JSON = {
    "Table": [
        {
            "DEAL_DATE": "06 Jul 2026",
            "SCRIP_CODE": 539217,
            "scripname": "SRESTHA",
            "CLIENT_NAME": "SAROJ KUMAR KUNDU",
            "TRANSACTION_TYPE": "S",
            "QUANTITY": 8913537.0,
            "PRICE": 0.31,
        },
        {
            "DEAL_DATE": "06 Jul 2026",
            "SCRIP_CODE": 512441,
            "scripname": "ENBETRD",
            "CLIENT_NAME": "SATISH KUMAR ANANTA SATYANARAYANA ANAPU",
            "TRANSACTION_TYPE": "B",
            "QUANTITY": 7902399.0,
            "PRICE": 0.30,
        },
    ]
}

BSE_MASTER_JSON = [
    {
        "SCRIP_CD": "500002",
        "Scrip_Name": "ABB India Ltd",
        "Status": "Active",
        "GROUP": "A",
        "FACE_VALUE": "2.00",
        "ISIN_NUMBER": "INE117A01022",
        "INDUSTRY": None,
        "scrip_id": "ABB",
        "Segment": "Equity",
        "Issuer_Name": "ABB India Limited",
        "Mktcap": "146664.96",
    }
]


def test_parse_bhavdata_full():
    days = parse_bhavdata_csv(BHAV_FULL_CSV)
    assert len(days) == 3
    eq = days[1]
    assert eq.symbol == "20MICRONS" and eq.series == "EQ"
    assert eq.trade_date == "2026-07-06"
    assert eq.prev_close == 204.36 and eq.close == 196.07
    assert eq.volume == 391809
    assert eq.turnover == pytest.approx(785.61 * 1e5)  # lakh -> rupees
    assert eq.trades == 7781
    assert eq.deliv_qty == 166603 and eq.deliv_per == 42.52


def test_parse_bhavdata_missing_delivery_is_none():
    days = parse_bhavdata_csv(BHAV_FULL_CSV)
    be = days[2]
    assert be.deliv_qty is None and be.deliv_per is None
    assert be.volume == 5000


def test_parse_bhavdata_rejects_garbage():
    with pytest.raises(FetchError):
        parse_bhavdata_csv("Date,Nope\n1,2\n")


def test_parse_equity_master():
    records = parse_equity_master_csv(EQUITY_L_CSV)
    assert len(records) == 2
    assert records[1].code == "ABB" and records[1].isin == "INE117A01022"
    assert records[1].name == "ABB India Limited"


def test_parse_bse_deals():
    deals = parse_bse_deals_json(BSE_DEALS_JSON, "BULK")
    assert len(deals) == 2
    d = deals[0]
    assert d.source == "BSE" and d.deal_type == "BULK"
    assert d.trade_date == "2026-07-06"
    assert d.symbol == "SRESTHA"  # scrip id until ISIN-mapped
    assert d.exchange_code == "539217"
    assert d.side == "SELL"
    assert d.quantity == 8913537
    assert d.price == 0.31
    assert deals[1].side == "BUY"


def test_parse_bse_deals_empty_table_is_valid():
    assert parse_bse_deals_json({"Table": []}, "BLOCK") == []
    assert parse_bse_deals_json({"Table": None}, "BLOCK") == []


def test_parse_bse_deals_missing_table_is_error():
    # The API answers {} to malformed/blocked requests — must not read as "no deals".
    with pytest.raises(FetchError):
        parse_bse_deals_json({}, "BULK")


def test_parse_bse_master():
    records = parse_bse_master_json(BSE_MASTER_JSON)
    assert len(records) == 1
    r = records[0]
    assert r.code == "500002" and r.symbol == "ABB" and r.isin == "INE117A01022"
    with pytest.raises(FetchError):
        parse_bse_master_json({"not": "a list"})
