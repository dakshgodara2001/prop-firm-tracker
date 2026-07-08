"""Fetcher interface every data source must implement."""
from __future__ import annotations

import abc
from datetime import date

from ..models import DEAL_SHORT, FETCH_SKIPPED, FetchOutcome


class FetchError(RuntimeError):
    """A fetch strategy failed (network, HTTP, or unparseable payload)."""


class BaseFetcher(abc.ABC):
    source = "BASE"

    @abc.abstractmethod
    def fetch_bulk_deals(self, trade_date: date) -> FetchOutcome:
        """Return the exchange's bulk-deal disclosures for one date."""

    @abc.abstractmethod
    def fetch_block_deals(self, trade_date: date) -> FetchOutcome:
        """Return the exchange's block-deal disclosures for one date."""

    def fetch_short_selling(self, trade_date: date) -> FetchOutcome:
        """Security-level short-selling data; optional per source."""
        return FetchOutcome(
            deal_type=DEAL_SHORT,
            status=FETCH_SKIPPED,
            message=f"{self.source} does not provide short-selling data",
        )
