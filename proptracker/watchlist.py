"""Seed watchlist of Indian prop / HFT / quant trading firms.

Edit this file to add firms or aliases, then run ``python main.py seed-firms``
(seeding is additive and idempotent; it never deletes). Aliases are stored in
normalized form (see normalize.py), so they can be written naturally here.

Prefix an alias with ``=`` to make it exact-match-only: it then matches a
client only when it equals the client's full normalized name, never as a
prefix or inner word. Use this for generic English words — live data showed
plain "MILLENNIUM" catching "MILLENNIUM STOCK BROKING PVT LTD", an unrelated
retail broker.
"""
from __future__ import annotations

import logging
import sqlite3
from typing import Tuple

from .normalize import normalize_name

log = logging.getLogger("proptracker.watchlist")

DEFAULT_CATEGORY = "PROP_HFT_QUANT"

SEED_FIRMS = [
    {
        "name": "NK Securities Research",
        "aliases": ["NK SECURITIES", "NK SECURITIES RESEARCH", "N K SECURITIES"],
    },
    {
        "name": "Graviton Research Capital",
        "aliases": ["GRAVITON", "GRAVITON RESEARCH", "GRAVITON RESEARCH CAPITAL"],
    },
    {
        "name": "AlphaGrep Securities",
        "aliases": ["ALPHAGREP", "ALPHAGREP SECURITIES"],
    },
    {
        "name": "QE Securities / Quadeye",
        "aliases": ["QE SECURITIES", "QUADEYE", "QUADEYE SECURITIES"],
    },
    {
        "name": "Tower Research Capital India",
        "aliases": [
            "TOWER RESEARCH",
            "TOWER RESEARCH CAPITAL",
            "TOWER RESEARCH CAPITAL MARKETS INDIA",
        ],
    },
    {
        "name": "iRage",
        "aliases": ["IRAGE", "IRAGE BROKING", "IRAGE CAPITAL"],
    },
    {
        "name": "Estee Advisors",
        "aliases": ["ESTEE", "ESTEE ADVISORS"],
    },
    {
        "name": "Dolat / Dolat Algotech",
        "aliases": ["DOLAT", "DOLAT ALGOTECH", "DOLAT CAPITAL"],
    },
    {
        "name": "IMC India Securities",
        "aliases": ["IMC INDIA", "IMC INDIA SECURITIES", "IMC TRADING"],
    },
    {
        "name": "Citadel Securities India",
        "aliases": ["CITADEL", "CITADEL SECURITIES", "CITADEL SECURITIES INDIA"],
    },
    {
        "name": "Hudson River Trading / HRT India",
        "aliases": ["HRTI", "HRT", "HUDSON RIVER", "HUDSON RIVER TRADING"],
    },
    {
        "name": "Two Roads Trading",
        "aliases": ["TWO ROADS", "TWO ROADS TRADING"],
    },
    {
        "name": "Jane Street related entities",
        "aliases": [
            "JANE STREET",
            "JSI INVESTMENTS",
            "JSI2 INVESTMENTS",
            "JANE STREET SINGAPORE",
            "JANE STREET ASIA",
        ],
    },
    {
        "name": "Optiver",
        "aliases": ["OPTIVER", "OPTIVER INDIA"],
    },
    {
        "name": "Jump Trading",
        "aliases": ["JUMP", "JUMP TRADING"],
    },
    {
        "name": "XTX Markets",
        "aliases": ["XTX", "XTX MARKETS"],
    },
    {
        "name": "Qube Research & Technologies",
        "aliases": ["QUBE RESEARCH", "QRT", "QUBE RESEARCH TECHNOLOGIES"],
    },
    {
        "name": "Quantbox Research",
        "aliases": ["QUANTBOX", "QUANTBOX RESEARCH"],
    },
    {
        "name": "Millennium",
        "aliases": ["=MILLENNIUM", "MILLENNIUM MANAGEMENT"],
    },
    {
        "name": "Da Vinci Derivatives",
        "aliases": ["DA VINCI", "DA VINCI DERIVATIVES", "DAVINCI DERIVATIVES"],
    },
    {
        "name": "Maverick Derivatives",
        "aliases": ["=MAVERICK", "MAVERICK DERIVATIVES"],
    },
    {
        "name": "Optimus Prime Securities & Research",
        "aliases": ["OPTIMUS PRIME", "OPTIMUSPRIME", "OPTIMUS PRIME SECURITIES"],
    },
    {
        "name": "Mathisys Quantcap",
        "aliases": ["MATHISYS", "MATHISYS QUANTCAP"],
    },
    {
        "name": "Maxizo Trading",
        "aliases": ["MAXIZO", "MAXIZO TRADING"],
    },
    {
        "name": "Microcurves Trading",
        "aliases": ["MICROCURVES", "MICROCURVES TRADING"],
    },
]


def seed_firms(conn: sqlite3.Connection) -> Tuple[int, int]:
    """Idempotently insert watchlist firms and aliases. Returns (new_firms, new_aliases)."""
    new_firms = 0
    new_aliases = 0
    seen_aliases = {}
    for entry in SEED_FIRMS:
        cur = conn.execute(
            "INSERT OR IGNORE INTO firms(name, category) VALUES (?, ?)",
            (entry["name"], entry.get("category", DEFAULT_CATEGORY)),
        )
        new_firms += cur.rowcount
        firm_id = conn.execute(
            "SELECT id FROM firms WHERE name = ?", (entry["name"],)
        ).fetchone()[0]
        for alias in entry["aliases"]:
            mode = "fuzzy"
            if alias.startswith("="):
                mode = "exact"
                alias = alias[1:]
            norm = normalize_name(alias)
            if not norm:
                log.warning("alias %r for %s normalizes to empty; skipped", alias, entry["name"])
                continue
            if norm in seen_aliases and seen_aliases[norm] != entry["name"]:
                log.warning(
                    "alias %r is claimed by both %s and %s; matches resolve to the longest alias first",
                    norm,
                    seen_aliases[norm],
                    entry["name"],
                )
            seen_aliases.setdefault(norm, entry["name"])
            cur = conn.execute(
                "INSERT OR IGNORE INTO firm_aliases(firm_id, alias, match_mode) VALUES (?, ?, ?)",
                (firm_id, norm, mode),
            )
            new_aliases += cur.rowcount
            if cur.rowcount == 0:  # alias existed; keep its match_mode current
                conn.execute(
                    "UPDATE firm_aliases SET match_mode = ? "
                    "WHERE firm_id = ? AND alias = ? AND match_mode != ?",
                    (mode, firm_id, norm, mode),
                )
    conn.commit()
    return new_firms, new_aliases
