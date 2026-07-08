"""Shared datatypes and constants."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

DEAL_BULK = "BULK"
DEAL_BLOCK = "BLOCK"
DEAL_SHORT = "SHORT_SELL"
DEAL_PRICES = "PRICES"   # daily bhavcopy (price/volume/delivery)
DEAL_MASTER = "MASTER"   # securities master (symbol <-> ISIN)

SIDE_BUY = "BUY"
SIDE_SELL = "SELL"

FETCH_OK = "ok"
FETCH_FAILED = "failed"
FETCH_SKIPPED = "skipped"


@dataclass
class RawDeal:
    """One bulk/block deal row exactly as disclosed by the exchange."""

    source: str
    deal_type: str  # DEAL_BULK | DEAL_BLOCK
    trade_date: str  # ISO YYYY-MM-DD
    symbol: str
    security_name: str
    client_name: str  # as published, un-normalized
    side: str  # BUY | SELL
    quantity: int
    price: Optional[float]  # trade price / weighted avg price
    remarks: str = ""
    exchange_code: str = ""  # exchange-native id (BSE scrip code); "" for NSE


@dataclass
class ShortSellRecord:
    """Security-level short-selling disclosure (NSE publishes no client names)."""

    source: str
    trade_date: str
    symbol: str
    security_name: str
    quantity: int


@dataclass
class StockDay:
    """One symbol-day of price/volume/delivery data from the bhavcopy."""

    source: str
    symbol: str
    series: str
    trade_date: str  # ISO
    prev_close: Optional[float]
    open: Optional[float]
    high: Optional[float]
    low: Optional[float]
    close: Optional[float]
    last: Optional[float]
    avg_price: Optional[float]
    volume: int
    turnover: Optional[float]  # rupees
    trades: Optional[int]
    deliv_qty: Optional[int]
    deliv_per: Optional[float]


@dataclass
class SecurityRecord:
    """One row of an exchange's securities master."""

    source: str  # 'NSE' | 'BSE'
    code: str    # exchange-native id: NSE symbol / BSE scrip code
    symbol: str  # trading symbol (NSE symbol / BSE scrip_id)
    name: str
    isin: str


@dataclass
class FetchOutcome:
    """Result of fetching one report type for one date."""

    deal_type: str
    status: str  # FETCH_OK | FETCH_FAILED | FETCH_SKIPPED
    strategy: str = ""  # which endpoint produced the data
    deals: List = field(default_factory=list)  # RawDeal/ShortSellRecord/StockDay/SecurityRecord
    raw_paths: List[Path] = field(default_factory=list)
    message: str = ""
