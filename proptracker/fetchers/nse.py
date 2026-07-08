"""NSE fetcher — official sources only.

For each report type, strategies are tried in order until one succeeds:

  1. api_csv           GET /api/historicalOR/bulk-block-short-deals?...&csv=true
                       The official CSV download behind the website's
                       "Download (.csv)" button; works for any historical date.
  2. api_json          Same endpoint without csv=true (keys BD_* / SS_*).
  3. daily_archive_csv https://nsearchives.nseindia.com/content/equities/{bulk,block}.csv
                       Static file holding only the latest trading day; a
                       resilient fallback for the evening cron run.

A zero-row response from the API is a *success* (holiday / nothing disclosed);
the archive, by contrast, can only vouch for the day it contains, so it raises
when the requested date is absent. Every successful response body is saved
verbatim under data/raw/<date>/ before parsing.

NSE fronts www.nseindia.com with a bot filter that wants browser-like headers
and cookies, so the session "warms up" against the homepage first and re-warms
after a 401/403.
"""
from __future__ import annotations

import csv
import io
import logging
import re
import time
import warnings
from datetime import date
from pathlib import Path
from typing import List, Optional, Tuple

# macOS system Python links LibreSSL, which makes urllib3 emit a loud but (for
# our purposes) harmless warning at import time; TLS to NSE works regardless.
# The filter must be installed before requests/urllib3 are imported.
warnings.filterwarnings("ignore", message="urllib3 v2 only supports OpenSSL")

import requests  # noqa: E402

from .. import config
from ..dates import parse_deal_date, to_nse_api_date
from ..models import (
    DEAL_BLOCK,
    DEAL_BULK,
    DEAL_MASTER,
    DEAL_PRICES,
    DEAL_SHORT,
    FETCH_FAILED,
    FETCH_OK,
    FetchOutcome,
    RawDeal,
    SecurityRecord,
    ShortSellRecord,
    StockDay,
)
from .base import BaseFetcher, FetchError

log = logging.getLogger("proptracker.fetchers.nse")

BASE_URL = "https://www.nseindia.com"
API_URL = BASE_URL + "/api/historicalOR/bulk-block-short-deals"
REFERER = BASE_URL + "/report-detail/display-bulk-and-block-deals"
ARCHIVE_URLS = {
    DEAL_BULK: "https://nsearchives.nseindia.com/content/equities/bulk.csv",
    DEAL_BLOCK: "https://nsearchives.nseindia.com/content/equities/block.csv",
}
OPTION_TYPES = {DEAL_BULK: "bulk_deals", DEAL_BLOCK: "block_deals", DEAL_SHORT: "short_selling"}
# "Full Bhavcopy and Security Deliverable data" — OHLC, volume, trades and
# delivery quantity/percentage for every symbol-series, one file per day.
BHAV_FULL_URL = "https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_{ddmmyyyy}.csv"
# Listed-equities master: SYMBOL -> ISIN (used to map BSE scrips to NSE symbols).
EQUITY_MASTER_URL = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"

# ---------------------------------------------------------------------------
# Parsing (module-level so tests can exercise it without a network)
# ---------------------------------------------------------------------------

# Column-header candidates after squashing to lowercase alphanumerics, e.g.
# '"Trade Price / Wght. Avg. Price "' -> "tradepricewghtavgprice".
_HEADER_CANDIDATES = {
    "date": ("date",),
    "symbol": ("symbol",),
    "security": ("securityname", "scripname", "security"),
    "client": ("clientname",),
    "side": ("buysell",),
    "qty": ("quantitytraded", "qtytraded", "quantity", "qty"),
    "price": ("tradepricewghtavgprice", "tradeprice", "watp", "price"),
    "remarks": ("remarks",),
}


def _squash_header(h: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (h or "").lower())


def _column_index(headers: List[str], field: str) -> Optional[int]:
    for candidate in _HEADER_CANDIDATES[field]:
        if candidate in headers:
            return headers.index(candidate)
    return None


def _parse_qty(value) -> int:
    if isinstance(value, (int, float)):
        return int(value)
    return int(float(str(value).replace(",", "").strip()))


def _parse_price(value) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).replace(",", "").strip()
    if s in ("", "-", "NA", "NIL"):
        return None
    return float(s)


def _parse_opt_int(value) -> Optional[int]:
    f = _parse_price(value)
    return None if f is None else int(f)


def _cell(row: List[str], idx: Optional[int]) -> str:
    if idx is None or idx >= len(row):
        return ""
    return (row[idx] or "").strip()


def _csv_rows(text: str) -> List[List[str]]:
    reader = csv.reader(io.StringIO(text.lstrip("\ufeff")))
    return [row for row in reader if any(c.strip() for c in row)]


def parse_deals_csv(text: str, deal_type: str, source: str = "NSE") -> List[RawDeal]:
    """Parse a bulk/block deals CSV (archive file or API csv=true download)."""
    rows = _csv_rows(text)
    if not rows:
        return []
    headers = [_squash_header(h) for h in rows[0]]
    if "symbol" not in headers:
        raise FetchError(f"unrecognized CSV header: {rows[0]!r}")
    idx = {field: _column_index(headers, field) for field in _HEADER_CANDIDATES}
    deals = []
    for row in rows[1:]:
        first = _cell(row, 0).upper()
        if not first or first.startswith("NO RECORD"):
            continue
        try:
            deals.append(
                RawDeal(
                    source=source,
                    deal_type=deal_type,
                    trade_date=parse_deal_date(_cell(row, idx["date"])),
                    symbol=_cell(row, idx["symbol"]),
                    security_name=_cell(row, idx["security"]),
                    client_name=_cell(row, idx["client"]),
                    side=_cell(row, idx["side"]).upper(),
                    quantity=_parse_qty(_cell(row, idx["qty"])),
                    price=_parse_price(_cell(row, idx["price"])),
                    remarks=_cell(row, idx["remarks"]),
                )
            )
        except (ValueError, TypeError) as exc:
            log.warning("skipping malformed CSV row %r: %s", row, exc)
    return deals


def parse_deals_json(payload: dict, deal_type: str, source: str = "NSE") -> List[RawDeal]:
    """Parse the historicalOR JSON body for bulk_deals / block_deals."""
    deals = []
    for r in payload.get("data") or []:
        try:
            deals.append(
                RawDeal(
                    source=source,
                    deal_type=deal_type,
                    trade_date=parse_deal_date(str(r.get("BD_DT_DATE") or "")),
                    symbol=str(r.get("BD_SYMBOL") or "").strip(),
                    security_name=str(r.get("BD_SCRIP_NAME") or "").strip(),
                    client_name=str(r.get("BD_CLIENT_NAME") or "").strip(),
                    side=str(r.get("BD_BUY_SELL") or "").strip().upper(),
                    quantity=_parse_qty(r.get("BD_QTY_TRD") or 0),
                    price=_parse_price(r.get("BD_TP_WATP")),
                    remarks=str(r.get("BD_REMARKS") or "").strip(),
                )
            )
        except (ValueError, TypeError) as exc:
            log.warning("skipping malformed JSON row %r: %s", r, exc)
    return deals


def parse_short_csv(text: str, source: str = "NSE") -> List[ShortSellRecord]:
    rows = _csv_rows(text)
    if not rows:
        return []
    headers = [_squash_header(h) for h in rows[0]]
    if "symbol" not in headers:
        raise FetchError(f"unrecognized short-selling CSV header: {rows[0]!r}")
    idx = {field: _column_index(headers, field) for field in ("date", "symbol", "security", "qty")}
    records = []
    for row in rows[1:]:
        first = _cell(row, 0).upper()
        if not first or first.startswith("NO RECORD"):
            continue
        try:
            records.append(
                ShortSellRecord(
                    source=source,
                    trade_date=parse_deal_date(_cell(row, idx["date"])),
                    symbol=_cell(row, idx["symbol"]),
                    security_name=_cell(row, idx["security"]),
                    quantity=_parse_qty(_cell(row, idx["qty"])),
                )
            )
        except (ValueError, TypeError) as exc:
            log.warning("skipping malformed short-selling row %r: %s", row, exc)
    return records


def parse_bhavdata_csv(text: str, source: str = "NSE") -> List[StockDay]:
    """Parse sec_bhavdata_full (price/volume/delivery; values space-padded,
    delivery columns may be '-')."""
    rows = _csv_rows(text)
    if not rows:
        return []
    headers = [_squash_header(h) for h in rows[0]]
    required = ("symbol", "series", "date1", "closeprice", "ttltrdqnty")
    if any(h not in headers for h in required):
        raise FetchError(f"unrecognized bhavcopy header: {rows[0]!r}")
    col = {name: headers.index(name) for name in headers}
    days = []
    for row in rows[1:]:
        try:
            turnover_lacs = _parse_price(_cell(row, col.get("turnoverlacs")))
            days.append(
                StockDay(
                    source=source,
                    symbol=_cell(row, col.get("symbol")),
                    series=_cell(row, col.get("series")).upper(),
                    trade_date=parse_deal_date(_cell(row, col.get("date1"))),
                    prev_close=_parse_price(_cell(row, col.get("prevclose"))),
                    open=_parse_price(_cell(row, col.get("openprice"))),
                    high=_parse_price(_cell(row, col.get("highprice"))),
                    low=_parse_price(_cell(row, col.get("lowprice"))),
                    close=_parse_price(_cell(row, col.get("closeprice"))),
                    last=_parse_price(_cell(row, col.get("lastprice"))),
                    avg_price=_parse_price(_cell(row, col.get("avgprice"))),
                    volume=_parse_qty(_cell(row, col.get("ttltrdqnty")) or 0),
                    turnover=turnover_lacs * 1e5 if turnover_lacs is not None else None,
                    trades=_parse_opt_int(_cell(row, col.get("nooftrades"))),
                    deliv_qty=_parse_opt_int(_cell(row, col.get("delivqty"))),
                    deliv_per=_parse_price(_cell(row, col.get("delivper"))),
                )
            )
        except (ValueError, TypeError) as exc:
            log.warning("skipping malformed bhavcopy row %r: %s", row, exc)
    return days


def parse_equity_master_csv(text: str, source: str = "NSE") -> List[SecurityRecord]:
    """Parse EQUITY_L.csv into SYMBOL -> ISIN records."""
    rows = _csv_rows(text)
    if not rows:
        return []
    headers = [_squash_header(h) for h in rows[0]]
    if "symbol" not in headers or "isinnumber" not in headers:
        raise FetchError(f"unrecognized equity master header: {rows[0]!r}")
    i_sym = headers.index("symbol")
    i_isin = headers.index("isinnumber")
    i_name = headers.index("nameofcompany") if "nameofcompany" in headers else None
    records = []
    for row in rows[1:]:
        symbol = _cell(row, i_sym)
        isin = _cell(row, i_isin)
        if not symbol:
            continue
        records.append(
            SecurityRecord(
                source=source,
                code=symbol,
                symbol=symbol,
                name=_cell(row, i_name) if i_name is not None else "",
                isin=isin,
            )
        )
    return records


def parse_short_json(payload: dict, source: str = "NSE") -> List[ShortSellRecord]:
    records = []
    for r in payload.get("data") or []:
        try:
            records.append(
                ShortSellRecord(
                    source=source,
                    trade_date=parse_deal_date(str(r.get("SS_DATE") or "")),
                    symbol=str(r.get("SS_SYMBOL") or "").strip(),
                    security_name=str(r.get("SS_NAME") or "").strip(),
                    quantity=_parse_qty(r.get("SS_QTY") or 0),
                )
            )
        except (ValueError, TypeError) as exc:
            log.warning("skipping malformed short-selling JSON row %r: %s", r, exc)
    return records


# ---------------------------------------------------------------------------
# Fetcher
# ---------------------------------------------------------------------------


class NSEFetcher(BaseFetcher):
    source = "NSE"

    def __init__(self, raw_dir: Optional[Path] = None, session: Optional[requests.Session] = None):
        self.raw_dir = Path(raw_dir or config.RAW_DIR)
        self.session = session or self._build_session()
        self._warmed = False

    # -- plumbing -----------------------------------------------------------

    @staticmethod
    def _build_session() -> requests.Session:
        s = requests.Session()
        s.headers.update(
            {
                "User-Agent": config.USER_AGENT,
                "Accept-Language": "en-US,en;q=0.9",
                "Accept-Encoding": "gzip, deflate",
            }
        )
        return s

    def _warm_up(self) -> None:
        try:
            self.session.get(
                BASE_URL,
                timeout=(config.REQUEST_CONNECT_TIMEOUT, config.REQUEST_READ_TIMEOUT),
                headers={"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"},
            )
        except requests.RequestException as exc:
            # Cookies sometimes arrive even on error pages; the API call
            # itself is the real test, so warm-up failures are not fatal.
            log.debug("NSE warm-up request failed (continuing): %s", exc)
        time.sleep(0.4)
        self._warmed = True

    def _get(self, url: str, params: Optional[dict] = None, accept: str = "*/*",
             referer: Optional[str] = None) -> requests.Response:
        last_error = None
        for attempt in range(1, config.MAX_RETRIES + 1):
            if url.startswith(BASE_URL) and not self._warmed:
                self._warm_up()
            headers = {"Accept": accept}
            if referer:
                headers["Referer"] = referer
            try:
                resp = self.session.get(
                    url,
                    params=params,
                    headers=headers,
                    timeout=(config.REQUEST_CONNECT_TIMEOUT, config.REQUEST_READ_TIMEOUT),
                )
                if resp.status_code in (401, 403):
                    self._warmed = False  # stale session; re-warm before retrying
                resp.raise_for_status()
                return resp
            except requests.RequestException as exc:
                last_error = exc
                log.warning("GET %s attempt %d/%d failed: %s", url, attempt, config.MAX_RETRIES, exc)
                if attempt < config.MAX_RETRIES:
                    time.sleep(config.RETRY_BACKOFF_SECONDS * attempt)
        raise FetchError(f"GET {url} failed after {config.MAX_RETRIES} attempts: {last_error}")

    def _save_raw(self, subdir: str, filename: str, content: bytes) -> Path:
        out_dir = self.raw_dir / subdir
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / filename
        path.write_bytes(content)
        return path

    def _api_params(self, deal_type: str, trade_date: date, as_csv: bool) -> dict:
        params = {
            "optionType": OPTION_TYPES[deal_type],
            "from": to_nse_api_date(trade_date),
            "to": to_nse_api_date(trade_date),
        }
        if as_csv:
            params["csv"] = "true"
        return params

    @staticmethod
    def _decode(resp: requests.Response) -> str:
        text = resp.content.decode("utf-8-sig", errors="replace")
        if text.lstrip()[:1] == "<":
            raise FetchError("received HTML instead of data (blocked or wrong URL)")
        return text

    # -- bulk / block strategies --------------------------------------------

    def _via_api_csv(self, deal_type: str, trade_date: date) -> Tuple[list, Path]:
        resp = self._get(API_URL, self._api_params(deal_type, trade_date, True), referer=REFERER)
        deals = parse_deals_csv(self._decode(resp), deal_type, self.source)
        raw = self._save_raw(trade_date.isoformat(), f"nse_{deal_type.lower()}_api.csv", resp.content)
        return [d for d in deals if d.trade_date == trade_date.isoformat()], raw

    def _via_api_json(self, deal_type: str, trade_date: date) -> Tuple[list, Path]:
        resp = self._get(API_URL, self._api_params(deal_type, trade_date, False),
                         accept="application/json", referer=REFERER)
        try:
            payload = resp.json()
        except ValueError as exc:
            raise FetchError(f"invalid JSON from API: {exc}") from exc
        deals = parse_deals_json(payload, deal_type, self.source)
        raw = self._save_raw(trade_date.isoformat(), f"nse_{deal_type.lower()}_api.json", resp.content)
        return [d for d in deals if d.trade_date == trade_date.isoformat()], raw

    def _via_archive_csv(self, deal_type: str, trade_date: date) -> Tuple[list, Path]:
        url = ARCHIVE_URLS.get(deal_type)
        if not url:
            raise FetchError(f"no daily-archive URL for {deal_type}")
        resp = self._get(url)
        all_deals = parse_deals_csv(self._decode(resp), deal_type, self.source)
        target = trade_date.isoformat()
        deals = [d for d in all_deals if d.trade_date == target]
        if not deals:
            covered = sorted({d.trade_date for d in all_deals}) or ["no records"]
            raise FetchError(f"daily archive does not cover {target} (contains: {covered})")
        raw = self._save_raw(trade_date.isoformat(), f"nse_{deal_type.lower()}_archive.csv", resp.content)
        return deals, raw

    def _fetch_with_strategies(self, deal_type: str, trade_date: date, strategies) -> FetchOutcome:
        errors = []
        for name, fn in strategies:
            try:
                deals, raw_path = fn(deal_type, trade_date)
            except Exception as exc:  # noqa: BLE001 - any strategy failure falls through
                log.warning("%s %s via %s failed: %s", self.source, deal_type, name, exc)
                errors.append(f"{name}: {exc}")
                continue
            log.info("%s %s %s: %d rows via %s", self.source, deal_type,
                     trade_date.isoformat(), len(deals), name)
            return FetchOutcome(
                deal_type=deal_type,
                status=FETCH_OK,
                strategy=name,
                deals=deals,
                raw_paths=[raw_path],
                message=f"{len(deals)} rows",
            )
        return FetchOutcome(deal_type=deal_type, status=FETCH_FAILED, message=" | ".join(errors))

    def _fetch_deals(self, deal_type: str, trade_date: date) -> FetchOutcome:
        return self._fetch_with_strategies(
            deal_type,
            trade_date,
            [
                ("api_csv", self._via_api_csv),
                ("api_json", self._via_api_json),
                ("daily_archive_csv", self._via_archive_csv),
            ],
        )

    def fetch_bulk_deals(self, trade_date: date) -> FetchOutcome:
        return self._fetch_deals(DEAL_BULK, trade_date)

    def fetch_block_deals(self, trade_date: date) -> FetchOutcome:
        return self._fetch_deals(DEAL_BLOCK, trade_date)

    # -- short selling -------------------------------------------------------

    def _short_via_api_csv(self, deal_type: str, trade_date: date) -> Tuple[list, Path]:
        resp = self._get(API_URL, self._api_params(deal_type, trade_date, True), referer=REFERER)
        records = parse_short_csv(self._decode(resp), self.source)
        raw = self._save_raw(trade_date.isoformat(), "nse_short_sell_api.csv", resp.content)
        return [r for r in records if r.trade_date == trade_date.isoformat()], raw

    def _short_via_api_json(self, deal_type: str, trade_date: date) -> Tuple[list, Path]:
        resp = self._get(API_URL, self._api_params(deal_type, trade_date, False),
                         accept="application/json", referer=REFERER)
        try:
            payload = resp.json()
        except ValueError as exc:
            raise FetchError(f"invalid JSON from API: {exc}") from exc
        records = parse_short_json(payload, self.source)
        raw = self._save_raw(trade_date.isoformat(), "nse_short_sell_api.json", resp.content)
        return [r for r in records if r.trade_date == trade_date.isoformat()], raw

    def fetch_short_selling(self, trade_date: date) -> FetchOutcome:
        return self._fetch_with_strategies(
            DEAL_SHORT,
            trade_date,
            [
                ("api_csv", self._short_via_api_csv),
                ("api_json", self._short_via_api_json),
            ],
        )

    # -- price / volume / delivery history ------------------------------------

    def _prices_via_bhav_full(self, deal_type: str, trade_date: date) -> Tuple[list, Path]:
        url = BHAV_FULL_URL.format(ddmmyyyy=trade_date.strftime("%d%m%Y"))
        resp = self._get(url)
        days = parse_bhavdata_csv(self._decode(resp), self.source)
        target = trade_date.isoformat()
        days = [d for d in days if d.trade_date == target]
        if not days:
            raise FetchError(f"bhavcopy for {target} parsed to zero rows")
        raw = self._save_raw(trade_date.isoformat(), "nse_prices_bhav.csv", resp.content)
        return days, raw

    def fetch_price_history(self, trade_date: date) -> FetchOutcome:
        """Daily bhavcopy with delivery data. 404 on holidays -> 'failed' with
        a 404 message, which the pipeline treats as a soft warning."""
        return self._fetch_with_strategies(
            DEAL_PRICES,
            trade_date,
            [("bhav_full_csv", self._prices_via_bhav_full)],
        )

    # -- securities master ------------------------------------------------------

    def fetch_equity_master(self) -> FetchOutcome:
        try:
            resp = self._get(EQUITY_MASTER_URL)
            records = parse_equity_master_csv(self._decode(resp), self.source)
            raw = self._save_raw("masters", "nse_equity_master.csv", resp.content)
        except Exception as exc:  # noqa: BLE001
            log.warning("NSE equity master fetch failed: %s", exc)
            return FetchOutcome(deal_type=DEAL_MASTER, status=FETCH_FAILED, message=str(exc))
        return FetchOutcome(
            deal_type=DEAL_MASTER,
            status=FETCH_OK,
            strategy="equity_l_csv",
            deals=records,
            raw_paths=[raw],
            message=f"{len(records)} securities",
        )
