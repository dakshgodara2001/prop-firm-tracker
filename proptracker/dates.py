"""Date helpers. NSE publishes everything in IST."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from . import config

try:
    from zoneinfo import ZoneInfo

    _IST = ZoneInfo(config.IST_TZ)
except Exception:  # pragma: no cover - tzdata missing
    _IST = timezone(timedelta(hours=5, minutes=30), name="IST")

# Formats seen across NSE/BSE payloads, e.g. "06-JUL-2026", "06 Jul 2026",
# "06/07/2026".
_DEAL_DATE_FORMATS = (
    "%d-%b-%Y",
    "%d-%m-%Y",
    "%Y-%m-%d",
    "%d-%B-%Y",
    "%d %b %Y",
    "%d/%m/%Y",
)


def ist_now() -> datetime:
    return datetime.now(_IST)


def ist_today() -> date:
    return ist_now().date()


def parse_iso(s: str) -> date:
    return datetime.strptime(s.strip(), "%Y-%m-%d").date()


def parse_deal_date(s: str) -> str:
    """Parse any NSE-style date string to ISO YYYY-MM-DD."""
    raw = (s or "").strip()
    for fmt in _DEAL_DATE_FORMATS:
        try:
            return datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            continue
    raise ValueError(f"unrecognized deal date: {s!r}")


def to_nse_api_date(d: date) -> str:
    """Format used by the historicalOR API's from/to params."""
    return d.strftime("%d-%m-%Y")
