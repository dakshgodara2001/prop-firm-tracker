"""BSE fetcher — secondary source, official BSE API only.

Bulk/block deals come from the JSON API that powers
https://www.bseindia.com/markets/equity/EQReports/bulk_deals:

    GET https://api.bseindia.com/BseIndiaAPI/api/BulkblockDeal/w
        ?type=1&scripcode=&fromdt=DD/MM/YYYY&todt=DD/MM/YYYY   (1=bulk, 2=block)

The API needs browser-ish headers plus a bseindia.com Referer. A valid request
returns {"Table":[...]}; a *missing* "Table" key means the request was wrong
or blocked (treated as failure), while an empty Table is a legitimate
no-deals day. The official CSV download endpoint (BulkblockDownload/w) is not
used because it omits the scrip code/name columns.

Deals identify stocks by BSE scrip code + scrip id; the pipeline remaps them
to NSE symbols via ISIN using the securities masters (see masters.py), so
dual-listed stocks aggregate together and join price history. The scrip
master comes from the official ListofScripData API.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import date
from pathlib import Path
from typing import List, Optional, Tuple

import requests

from .. import config
from ..dates import parse_deal_date
from ..models import (
    DEAL_BLOCK,
    DEAL_BULK,
    DEAL_MASTER,
    FETCH_FAILED,
    FETCH_OK,
    FetchOutcome,
    RawDeal,
    SecurityRecord,
)
from .base import BaseFetcher, FetchError

log = logging.getLogger("proptracker.fetchers.bse")

API_BASE = "https://api.bseindia.com/BseIndiaAPI/api"
DEALS_URL = API_BASE + "/BulkblockDeal/w"
MASTER_URL = API_BASE + "/ListofScripData/w"
REFERER = "https://www.bseindia.com/"
DEAL_TYPE_PARAM = {DEAL_BULK: "1", DEAL_BLOCK: "2"}
SIDE_MAP = {"B": "BUY", "S": "SELL", "P": "BUY"}


def parse_bse_deals_json(payload: dict, deal_type: str, source: str = "BSE") -> List[RawDeal]:
    """Parse {"Table":[{DEAL_DATE, SCRIP_CODE, scripname, CLIENT_NAME,
    TRANSACTION_TYPE, QUANTITY, PRICE}]}. Raises if "Table" is absent
    (bad params / blocked); an empty or null Table is a valid empty day."""
    if not isinstance(payload, dict) or "Table" not in payload:
        raise FetchError("unexpected BSE payload (no 'Table' key — bad params or blocked)")
    deals = []
    for r in payload.get("Table") or []:
        try:
            side_raw = str(r.get("TRANSACTION_TYPE") or "").strip().upper()
            scrip_id = str(r.get("scripname") or "").strip()
            deals.append(
                RawDeal(
                    source=source,
                    deal_type=deal_type,
                    trade_date=parse_deal_date(str(r.get("DEAL_DATE") or "")),
                    symbol=scrip_id,  # remapped to the NSE symbol via ISIN later
                    security_name=scrip_id,
                    client_name=str(r.get("CLIENT_NAME") or "").strip(),
                    side=SIDE_MAP.get(side_raw, side_raw),
                    quantity=int(float(r.get("QUANTITY") or 0)),
                    price=float(r["PRICE"]) if r.get("PRICE") is not None else None,
                    exchange_code=str(r.get("SCRIP_CODE") or "").strip(),
                )
            )
        except (ValueError, TypeError) as exc:
            log.warning("skipping malformed BSE deal row %r: %s", r, exc)
    return deals


def parse_bse_master_json(payload, source: str = "BSE") -> List[SecurityRecord]:
    """Parse ListofScripData: [{SCRIP_CD, Scrip_Name, scrip_id, ISIN_NUMBER, ...}]."""
    if not isinstance(payload, list):
        raise FetchError("unexpected BSE master payload (expected a JSON array)")
    records = []
    for r in payload:
        code = str(r.get("SCRIP_CD") or "").strip()
        if not code:
            continue
        records.append(
            SecurityRecord(
                source=source,
                code=code,
                symbol=str(r.get("scrip_id") or "").strip(),
                name=str(r.get("Scrip_Name") or r.get("Issuer_Name") or "").strip(),
                isin=str(r.get("ISIN_NUMBER") or "").strip(),
            )
        )
    return records


class BSEFetcher(BaseFetcher):
    source = "BSE"

    def __init__(self, raw_dir: Optional[Path] = None, session: Optional[requests.Session] = None):
        self.raw_dir = Path(raw_dir or config.RAW_DIR)
        self.session = session or self._build_session()

    @staticmethod
    def _build_session() -> requests.Session:
        s = requests.Session()
        s.headers.update(
            {
                "User-Agent": config.USER_AGENT,
                "Referer": REFERER,
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "en-US,en;q=0.9",
            }
        )
        return s

    def _get_json(self, url: str, params: Optional[dict] = None):
        last_error = None
        for attempt in range(1, config.MAX_RETRIES + 1):
            try:
                resp = self.session.get(
                    url,
                    params=params,
                    timeout=(config.REQUEST_CONNECT_TIMEOUT, config.REQUEST_READ_TIMEOUT),
                )
                resp.raise_for_status()
                return resp.json(), resp.content
            except (requests.RequestException, ValueError) as exc:
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

    def _fetch_deals(self, deal_type: str, trade_date: date) -> FetchOutcome:
        params = {
            "type": DEAL_TYPE_PARAM[deal_type],
            "scripcode": "",
            "fromdt": trade_date.strftime("%d/%m/%Y"),
            "todt": trade_date.strftime("%d/%m/%Y"),
        }
        try:
            payload, content = self._get_json(DEALS_URL, params)
            deals = parse_bse_deals_json(payload, deal_type, self.source)
        except Exception as exc:  # noqa: BLE001
            log.warning("%s %s fetch failed: %s", self.source, deal_type, exc)
            return FetchOutcome(deal_type=deal_type, status=FETCH_FAILED,
                                message=f"api_json: {exc}")
        target = trade_date.isoformat()
        deals = [d for d in deals if d.trade_date == target]
        raw = self._save_raw(target, f"bse_{deal_type.lower()}_api.json", content)
        log.info("%s %s %s: %d rows via api_json", self.source, deal_type, target, len(deals))
        return FetchOutcome(
            deal_type=deal_type,
            status=FETCH_OK,
            strategy="api_json",
            deals=deals,
            raw_paths=[raw],
            message=f"{len(deals)} rows",
        )

    def fetch_bulk_deals(self, trade_date: date) -> FetchOutcome:
        return self._fetch_deals(DEAL_BULK, trade_date)

    def fetch_block_deals(self, trade_date: date) -> FetchOutcome:
        return self._fetch_deals(DEAL_BLOCK, trade_date)

    # fetch_short_selling: BaseFetcher default ("skipped") — BSE publishes none.

    def fetch_scrip_master(self) -> FetchOutcome:
        params = {
            "Group": "",
            "Scripcode": "",
            "industry": "",
            "segment": "Equity",
            "status": "Active",
        }
        try:
            payload, content = self._get_json(MASTER_URL, params)
            records = parse_bse_master_json(payload, self.source)
            raw = self._save_raw("masters", "bse_scrip_master.json", content)
        except Exception as exc:  # noqa: BLE001
            log.warning("BSE scrip master fetch failed: %s", exc)
            return FetchOutcome(deal_type=DEAL_MASTER, status=FETCH_FAILED, message=str(exc))
        return FetchOutcome(
            deal_type=DEAL_MASTER,
            status=FETCH_OK,
            strategy="listofscrip_json",
            deals=records,
            raw_paths=[raw],
            message=f"{len(records)} securities",
        )
