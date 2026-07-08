"""Fetcher registry.

Sources are addressed by name so secondary sources (BSE, Screener, ...) can be
added later: implement BaseFetcher in a new module and register it here.
Imports are lazy so commands that never touch the network (process/report)
don't require the ``requests`` dependency.
"""
from __future__ import annotations

from importlib import import_module

_REGISTRY = {
    "nse": ("proptracker.fetchers.nse", "NSEFetcher"),
    "bse": ("proptracker.fetchers.bse", "BSEFetcher"),
    # "screener": ("proptracker.fetchers.screener", "ScreenerFetcher"),  # Phase 3
}


def get_fetcher_class(name: str = "nse"):
    try:
        module_name, class_name = _REGISTRY[name.lower()]
    except KeyError:
        raise KeyError(f"unknown source {name!r}; available: {sorted(_REGISTRY)}") from None
    return getattr(import_module(module_name), class_name)
