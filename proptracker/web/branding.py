"""Firm display metadata for the coverage-universe wall.

Generated monograms + accent colours — NOT the firms' real logos. Using actual
trademarks on a public product that tracks these firms would imply endorsement
/ affiliation, so each firm gets a neutral typographic tile instead. To use
licensed logo images later, drop files in a static dir and swap the tile body.
"""
from __future__ import annotations

import sqlite3
from typing import Optional, Tuple

# canonical watchlist name -> (monogram, short label)
_DISPLAY = {
    "NK Securities Research": ("NK", "NK Securities"),
    "Graviton Research Capital": ("GRV", "Graviton"),
    "AlphaGrep Securities": ("AG", "AlphaGrep"),
    "QE Securities / Quadeye": ("QE", "Quadeye"),
    "Tower Research Capital India": ("TRC", "Tower Research"),
    "iRage": ("iR", "iRage"),
    "Estee Advisors": ("EA", "Estee"),
    "Dolat / Dolat Algotech": ("DA", "Dolat"),
    "IMC India Securities": ("IMC", "IMC"),
    "Citadel Securities India": ("CS", "Citadel"),
    "Hudson River Trading / HRT India": ("HRT", "Hudson River"),
    "Two Roads Trading": ("2R", "Two Roads"),
    "Jane Street related entities": ("JS", "Jane Street"),
    "Optiver": ("OPT", "Optiver"),
    "Jump Trading": ("JT", "Jump"),
    "XTX Markets": ("XTX", "XTX"),
    "Qube Research & Technologies": ("QRT", "Qube"),
    "Quantbox Research": ("QBX", "Quantbox"),
    "Millennium": ("MLM", "Millennium"),
    "Da Vinci Derivatives": ("DV", "Da Vinci"),
    "Maverick Derivatives": ("MAV", "Maverick"),
    "Optimus Prime Securities & Research": ("OP", "Optimus Prime"),
    "Mathisys Quantcap": ("MQ", "Mathisys"),
    "Maxizo Trading": ("MX", "Maxizo"),
    "Microcurves Trading": ("MC", "Microcurves"),
}

# Decorative accent hues (dark-surface friendly). These brand the tiles; they
# do not encode data, so they are not held to the categorical-palette checks.
_ACCENTS = [
    "#3987e5", "#2aa4bc", "#e8b339", "#9085e9", "#199e70",
    "#e07b3c", "#d55181", "#5aa9d6", "#c98500", "#7bb356",
]


def _fallback(name: str) -> Tuple[str, str]:
    core = name.split("/")[0].split("(")[0]
    words = [w for w in core.replace("&", " ").split() if w[:1].isalnum()]
    mono = "".join(w[0] for w in words[:3]).upper() or name[:2].upper()
    short = core.strip()
    return mono, (short if len(short) <= 16 else short[:15] + "…")


def display(name: str) -> Tuple[str, str]:
    return _DISPLAY.get(name) or _fallback(name)


def firm_tiles(conn: sqlite3.Connection, trade_date: Optional[str]) -> dict:
    firms = conn.execute("SELECT id, name FROM firms WHERE active = 1").fetchall()
    active = set()
    if trade_date:
        active = {
            r["name"]
            for r in conn.execute(
                """SELECT DISTINCT f.name FROM daily_firm_stock_agg a
                   JOIN firms f ON f.id = a.firm_id WHERE a.trade_date = ?""",
                (trade_date,),
            )
        }
    tiles = []
    for i, f in enumerate(sorted(firms, key=lambda r: r["name"])):
        mono, short = display(f["name"])
        tiles.append({
            "id": f["id"],
            "name": f["name"],
            "monogram": mono,
            "short": short,
            "accent": _ACCENTS[i % len(_ACCENTS)],
            "active": f["name"] in active,
        })
    # active firms first (today's players lead the wall), then alphabetical
    tiles.sort(key=lambda t: (not t["active"], t["short"].lower()))
    return {"tiles": tiles, "total": len(tiles), "active": sum(t["active"] for t in tiles)}
