"""Participant-name normalization.

The same function is applied to disclosed client names and to watchlist
aliases, so both sides of a comparison are in the identical canonical form:
uppercase, punctuation collapsed to spaces, trailing legal-entity suffixes
removed. Examples:

    "NK Securities Research Private Limited" -> "NK SECURITIES RESEARCH"
    "N.K. Securities"                        -> "N K SECURITIES"
    "QUBE RESEARCH & TECHNOLOGIES PTE. LTD." -> "QUBE RESEARCH TECHNOLOGIES"
    "HRTI PRIVATE LIMITED"                   -> "HRTI"
"""
from __future__ import annotations

import re
from typing import Optional

_PUNCT_RE = re.compile(r"[.,\-/&()'\"*+:;]+")
_WS_RE = re.compile(r"\s+")

# Stripped only from the END of a name, repeatedly. Business words such as
# SECURITIES / RESEARCH / CAPITAL / TRADING are deliberately kept — they are
# what distinguishes one firm from another.
_LEGAL_SUFFIXES = {
    "PRIVATE",
    "PVT",
    "LIMITED",
    "LTD",
    "LLP",
    "LLC",
    "INC",
    "PLC",
    "PTE",
    "CORP",
    "CORPORATION",
    "COMPANY",
    "CO",
    "AND",  # left dangling after e.g. "X AND CO" loses "CO"
}


def normalize_name(name: Optional[str]) -> str:
    s = (name or "").upper()
    s = _PUNCT_RE.sub(" ", s)
    s = _WS_RE.sub(" ", s).strip()
    if not s:
        return ""
    tokens = s.split(" ")
    while tokens and tokens[-1] in _LEGAL_SUFFIXES:
        tokens.pop()
    return " ".join(tokens)
