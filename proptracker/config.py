"""Central configuration.

Three override layers, applied in order:
  1. Defaults in this file.
  2. Optional JSON file (``config.local.json`` in the project root, or the path
     in ``PFT_CONFIG_FILE``) — keys must match the UPPERCASE names below;
     dict values (e.g. ATTENTION_WEIGHTS) are merged key-by-key.
  3. PFT_* environment variables (highest precedence, where noted).

Modules read attributes via ``config.NAME`` at call time, so tests may also
monkeypatch them.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

log = logging.getLogger("proptracker.config")

PROJECT_ROOT = Path(__file__).resolve().parent.parent

DATA_DIR = Path(os.environ.get("PFT_DATA_DIR", PROJECT_ROOT / "data"))
RAW_DIR = DATA_DIR / "raw"
REPORTS_DIR = DATA_DIR / "reports"
DB_PATH = Path(os.environ.get("PFT_DB_PATH", DATA_DIR / "proptracker.db"))

# --- Sources -----------------------------------------------------------------
# Exchanges polled by run-daily/fetch, in order. NSE is primary; BSE secondary.
SOURCES = tuple(
    s.strip().lower()
    for s in os.environ.get("PFT_SOURCES", "nse,bse").split(",")
    if s.strip()
)

# --- Classification ----------------------------------------------------------
# When a firm both bought and sold the same stock on the same day, the trades
# are treated as round-trip churn (noise) if the residual position is small:
# |buy_qty - sell_qty| <= ROUND_TRIP_NET_RATIO * (buy_qty + sell_qty).
ROUND_TRIP_NET_RATIO = float(os.environ.get("PFT_ROUND_TRIP_NET_RATIO", "0.20"))

# --- Matching ----------------------------------------------------------------
# Single-word aliases shorter than this (e.g. "XTX", "HRT", "JUMP") only match
# when they equal the client's full normalized name; longer or multi-word
# aliases may also match as a prefix or as whole words inside the name.
MIN_FUZZY_ALIAS_LEN = int(os.environ.get("PFT_MIN_FUZZY_ALIAS_LEN", "5"))

# --- Market data / context -----------------------------------------------------
ADV_WINDOW = 20          # sessions used for the average-daily-volume baseline
MIN_ADV_DAYS = 5         # need at least this much history before ADV is trusted
FETCH_PRICES = os.environ.get("PFT_FETCH_PRICES", "1").lower() not in ("0", "false", "no")
# Series preference when a symbol has several rows in the bhavcopy on one day.
SERIES_PRIORITY = ("EQ", "BE", "BZ", "SM", "ST")

# --- Post-deal returns --------------------------------------------------------
# Horizons are fixed at T+1/3/5/10 sessions (schema columns). This is how many
# calendar days back run-daily re-checks deals whose returns may now be fillable.
RETURNS_LOOKBACK_DAYS = int(os.environ.get("PFT_RETURNS_LOOKBACK_DAYS", "21"))

# --- Multi-firm clusters --------------------------------------------------------
CLUSTER_MIN_FIRMS = 2        # >=N tracked firms, same side, directional
CHURN_CLUSTER_MIN_FIRMS = 3  # >=N tracked firms all round-tripping one stock

# --- Stock attention score (v1) ---------------------------------------------------
# Ranks which stocks deserve a look today by how much tracked-firm attention
# they received. It carries NO directional meaning — a huge churn day ranks
# high as a liquidity/event flag — and it does not try to predict returns.
# score = 100 * weighted_mean(components); components whose inputs are missing
# (e.g. no price history) are skipped and the remaining weights renormalized.
ATTENTION_WEIGHTS = {
    "firms": 0.15,    # how many tracked firms appeared
    "multi": 0.10,    # any multi-firm participation at all
    "gross": 0.20,    # total gross traded value
    "net": 0.15,      # |net| directional value
    "clean": 0.15,    # directional share of activity (vs round-trip churn)
    "context": 0.15,  # unusual volume / price move (needs price history)
    "repeat": 0.10,   # appearances in the trailing window
}
ATTENTION_FIRMS_CAP = 4           # this many firms scores the component 1.0
ATTENTION_GROSS_CAP_CR = 100.0    # gross ₹ Cr scoring 1.0
ATTENTION_NET_CAP_CR = 25.0       # |net| ₹ Cr scoring 1.0
ATTENTION_VOL_ADV_CAP = 10.0      # day volume at 10× ADV20 scores 1.0
ATTENTION_MOVE_CAP_PCT = 10.0     # |day move| of 10% scores 1.0
ATTENTION_REPEAT_CAP = 4          # prior appearances scoring 1.0
ATTENTION_REPEAT_WINDOW_DAYS = 30
# Report thresholds:
HIGH_ATTENTION_MIN = float(os.environ.get("PFT_HIGH_ATTENTION_MIN", "60"))
WATCHLIST_MIN_ATTENTION = float(os.environ.get("PFT_WATCHLIST_MIN_ATTENTION", "40"))

# --- Alerts ------------------------------------------------------------------------
# Alerts describe unusual tracked-firm attention; none of them are trade advice.
ALERT_MULTI_FIRM_MIN = int(os.environ.get("PFT_ALERT_MULTI_FIRM_MIN", "2"))
ALERT_MIN_NET_CR = float(os.environ.get("PFT_ALERT_MIN_NET_CR", "1.0"))  # directional dust filter
ALERT_REPEAT_MIN = int(os.environ.get("PFT_ALERT_REPEAT_MIN", "3"))  # total appearances in window
ALERT_CHURN_MIN_PRIOR = int(os.environ.get("PFT_ALERT_CHURN_MIN_PRIOR", "2"))  # churn days before a shift counts
ALERT_LARGE_GROSS_CR = float(os.environ.get("PFT_ALERT_LARGE_GROSS_CR", "100"))  # ₹ Cr gross
# Anti-spam: tiny first-timers don't page anyone (they still appear in the report).
ALERT_FIRST_TIME_MIN_ATTENTION = float(os.environ.get("PFT_ALERT_FIRST_TIME_MIN_ATTENTION", "25"))

# --- Securities masters ---------------------------------------------------------
MASTERS_MAX_AGE_DAYS = int(os.environ.get("PFT_MASTERS_MAX_AGE_DAYS", "7"))

# --- HTTP -----------------------------------------------------------------------
REQUEST_CONNECT_TIMEOUT = 10
REQUEST_READ_TIMEOUT = 30
MAX_RETRIES = int(os.environ.get("PFT_MAX_RETRIES", "3"))
RETRY_BACKOFF_SECONDS = float(os.environ.get("PFT_RETRY_BACKOFF", "2.0"))
USER_AGENT = os.environ.get(
    "PFT_USER_AGENT",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
)

# --- Misc -----------------------------------------------------------------------
IST_TZ = "Asia/Kolkata"
INCLUDE_SHORT_SELLING = os.environ.get("PFT_INCLUDE_SHORT_SELLING", "1").lower() not in (
    "0",
    "false",
    "no",
)


def _apply_config_file() -> None:
    """Overlay UPPERCASE keys from an optional JSON config file."""
    path = Path(os.environ.get("PFT_CONFIG_FILE", PROJECT_ROOT / "config.local.json"))
    if not path.is_file():
        return
    try:
        overrides = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.warning("ignoring unreadable config file %s: %s", path, exc)
        return
    if not isinstance(overrides, dict):
        log.warning("ignoring config file %s: top level must be an object", path)
        return
    g = globals()
    for key, value in overrides.items():
        if not (key.isupper() and key in g):
            log.warning("ignoring unknown config key %r from %s", key, path)
            continue
        current = g[key]
        if isinstance(current, dict) and isinstance(value, dict):
            current.update(value)
        elif isinstance(current, Path):
            g[key] = Path(value)
        elif isinstance(current, tuple) and isinstance(value, list):
            g[key] = tuple(value)
        else:
            g[key] = value


_apply_config_file()
