"""Match normalized client names against the firm watchlist.

Three tiers, tried in order, each on normalized strings:

  exact    (1.0) — client name equals an alias. Any alias qualifies.
  prefix   (0.9) — client name starts with "<alias> " on a word boundary,
                   e.g. alias "GRAVITON RESEARCH CAPITAL" matches client
                   "GRAVITON RESEARCH CAPITAL MARKETS TRADING".
  contains (0.8) — alias appears as whole word(s) inside the client name.

To limit false positives, prefix/contains are only attempted for aliases that
are multi-word or at least MIN_FUZZY_ALIAS_LEN characters long; ultra-short
tickers like "XTX", "HRT", "JUMP" match on exact equality only ("HRTI PRIVATE
LIMITED" still matches alias "HRTI" exactly because normalization strips the
legal suffix). Aliases seeded with match_mode='exact' (generic English words
like MILLENNIUM) are likewise excluded from prefix/contains regardless of
length. Longer aliases are tried first, and every match records the alias +
method so results are auditable in the report and database.
"""
from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from typing import Iterable, Optional, Tuple

from . import config
from .normalize import normalize_name

log = logging.getLogger("proptracker.matching")

METHOD_CONFIDENCE = {"exact": 1.0, "prefix": 0.9, "contains": 0.8}


@dataclass(frozen=True)
class MatchResult:
    firm_id: int
    firm_name: str
    alias: str
    method: str
    confidence: float


class FirmMatcher:
    def __init__(self, alias_rows: Iterable[tuple], min_fuzzy_len: Optional[int] = None):
        """alias_rows: (firm_id, firm_name, alias[, match_mode]) with aliases
        already normalized; match_mode defaults to 'fuzzy'."""
        min_len = config.MIN_FUZZY_ALIAS_LEN if min_fuzzy_len is None else min_fuzzy_len
        self._exact = {}
        fuzzy = []
        for row in alias_rows:
            firm_id, firm_name, alias = row[0], row[1], row[2]
            mode = row[3] if len(row) > 3 else "fuzzy"
            alias = normalize_name(alias)  # no-op for already-normalized aliases
            if not alias:
                continue
            if alias in self._exact and self._exact[alias][0] != firm_id:
                log.warning(
                    "alias %r maps to multiple firms (%s, %s); keeping the first",
                    alias,
                    self._exact[alias][1],
                    firm_name,
                )
                continue
            self._exact[alias] = (firm_id, firm_name)
            if mode != "exact" and (" " in alias or len(alias) >= min_len):
                fuzzy.append((alias, firm_id, firm_name))
        # Longest alias first so the most specific match wins deterministically.
        self._fuzzy = sorted(fuzzy, key=lambda t: (-len(t[0]), t[0]))

    @classmethod
    def from_db(cls, conn: sqlite3.Connection) -> "FirmMatcher":
        rows = conn.execute(
            """SELECT f.id, f.name, a.alias, a.match_mode
               FROM firm_aliases a JOIN firms f ON f.id = a.firm_id
               WHERE f.active = 1"""
        ).fetchall()
        return cls([(r["id"], r["name"], r["alias"], r["match_mode"]) for r in rows])

    def match(self, client_name: str) -> Optional[MatchResult]:
        name = normalize_name(client_name)
        if not name:
            return None
        hit = self._exact.get(name)
        if hit:
            return MatchResult(hit[0], hit[1], name, "exact", METHOD_CONFIDENCE["exact"])
        for alias, firm_id, firm_name in self._fuzzy:
            if name.startswith(alias + " "):
                return MatchResult(firm_id, firm_name, alias, "prefix", METHOD_CONFIDENCE["prefix"])
        padded = f" {name} "
        for alias, firm_id, firm_name in self._fuzzy:
            if f" {alias} " in padded:
                return MatchResult(firm_id, firm_name, alias, "contains", METHOD_CONFIDENCE["contains"])
        return None
