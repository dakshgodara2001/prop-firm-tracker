"""Parser tests against payload shapes captured from live NSE endpoints (2026-07)."""
import pytest

from proptracker.fetchers.base import FetchError
from proptracker.fetchers.nse import (
    parse_deals_csv,
    parse_deals_json,
    parse_short_csv,
    parse_short_json,
)

ARCHIVE_CSV = """Date,Symbol,Security Name,Client Name,Buy/Sell,Quantity Traded,Trade Price / Wght. Avg. Price,Remarks
06-JUL-2026,AARTECH,Aartech Solonics Limited,AMJAY IMPEX PRIVATE LIMITED,BUY,200000,57.05,-
06-JUL-2026,TARSONS,Tarsons Products Limited,NK SECURITIES RESEARCH PRIVATE LIMITED,SELL,384134,300.42,-
"""

# The API csv=true download: BOM, quoted cells, headers with trailing spaces,
# "Buy / Sell" with spaces, Indian-style digit grouping.
API_CSV = (
    '﻿"Date ","Symbol ","Security Name ","Client Name ","Buy / Sell ",'
    '"Quantity Traded ","Trade Price / Wght. Avg. Price ","Remarks "\n'
    '"03-JUL-2026","ATALREAL","Atal Realtech Limited","ALTIZEN VENTURES LLP","BUY",'
    '"6,81,464","28.07","-"\n'
)

BLOCK_NO_RECORDS_CSV = """Date,Symbol,Security Name,Client Name,Buy/Sell,Quantity Traded,Trade Price / Wght. Avg. Price
NO RECORDS,,,,,,
"""

API_JSON = {
    "data": [
        {
            "BD_DT_DATE": "23-JUN-2026",
            "BD_DT_ORDER": "2026-06-22T18:30:00.000Z",
            "BD_SYMBOL": "CRAFTSMAN",
            "BD_SCRIP_NAME": "Craftsman Automation Ltd",
            "BD_CLIENT_NAME": "ABU DHABI INVESTMENT",
            "BD_BUY_SELL": "BUY",
            "BD_QTY_TRD": 57298,
            "BD_TP_WATP": 9250,  # integers happen
            "BD_REMARKS": None,  # nulls happen
        }
    ]
}

SHORT_JSON = {
    "data": [
        {
            "SS_DATE": "03-JUL-2026",
            "SS_DATE_ORDER": "2026-07-02T18:30:00.000Z",
            "SS_SYMBOL": "ABFRL",
            "SS_NAME": "ADITYA BIRLA FASHION & RT",
            "SS_QTY": 25,
        }
    ]
}

SHORT_CSV = (
    '﻿"Date ","Symbol ","Security Name ","Quantity "\n'
    '"03-JUL-2026","ACC","ACC LIMITED","1,010"\n'
)


def test_parse_archive_csv():
    deals = parse_deals_csv(ARCHIVE_CSV, "BULK")
    assert len(deals) == 2
    nk = deals[1]
    assert nk.trade_date == "2026-07-06"
    assert nk.symbol == "TARSONS"
    assert nk.client_name == "NK SECURITIES RESEARCH PRIVATE LIMITED"
    assert nk.side == "SELL"
    assert nk.quantity == 384134
    assert nk.price == 300.42
    assert nk.deal_type == "BULK"


def test_parse_api_csv_with_bom_and_indian_grouping():
    deals = parse_deals_csv(API_CSV, "BULK")
    assert len(deals) == 1
    d = deals[0]
    assert d.trade_date == "2026-07-03"
    assert d.quantity == 681464
    assert d.price == 28.07
    assert d.side == "BUY"


def test_parse_block_no_records():
    assert parse_deals_csv(BLOCK_NO_RECORDS_CSV, "BLOCK") == []


def test_parse_rejects_html():
    with pytest.raises(FetchError):
        parse_deals_csv("<!DOCTYPE html><html>blocked</html>", "BULK")


def test_parse_api_json():
    deals = parse_deals_json(API_JSON, "BLOCK")
    assert len(deals) == 1
    d = deals[0]
    assert d.trade_date == "2026-06-23"
    assert d.price == 9250.0
    assert d.remarks == ""
    assert d.deal_type == "BLOCK"


def test_parse_short_json():
    records = parse_short_json(SHORT_JSON)
    assert len(records) == 1
    assert records[0].symbol == "ABFRL"
    assert records[0].quantity == 25
    assert records[0].trade_date == "2026-07-03"


def test_parse_short_csv():
    records = parse_short_csv(SHORT_CSV)
    assert len(records) == 1
    assert records[0].symbol == "ACC"
    assert records[0].quantity == 1010
