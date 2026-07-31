"""Open ATS job-board adapters.

These five are the ones worth having for a US entry-level SWE search: Greenhouse,
Ashby and Lever give full descriptions with real apply URLs; Workable adds a
cross-company meta-search that needs no company slug; Workday carries the largest
share of the inventory but exposes titles only.

Aggregators (LinkedIn, Wellfound, Built In, Jobright, Dice, Peerlist) are
deliberately absent — roughly 84% of their inventory resolves to one of these ATS
hosts, and they strip the apply URL, the timestamps and the description on the way
in. They belong in watchlist discovery, not in fetching.
"""
from __future__ import annotations

from copilot.adapters.ats._http import AtsFetchError
from copilot.adapters.ats.aggregate import (
    FetchReport,
    WatchlistPostingSource,
    dedupe,
    source_for,
)
from copilot.adapters.ats.ashby import AshbySource
from copilot.adapters.ats.discover import (
    DiscoveryReport,
    classify_apply_url,
    sponsorship_hints,
    watchlist_from_apply_urls,
    watchlist_from_simplify,
)
from copilot.adapters.ats.greenhouse import GreenhouseSource
from copilot.adapters.ats.lever import LeverSource
from copilot.adapters.ats.watchlist import WatchlistEntry, load_watchlist, parse_watchlist
from copilot.adapters.ats.workable import WorkableSearchSource, WorkableWidgetSource
from copilot.adapters.ats.workday import WorkdaySource

__all__ = [
    "AshbySource",
    "AtsFetchError",
    "DiscoveryReport",
    "FetchReport",
    "GreenhouseSource",
    "LeverSource",
    "WatchlistEntry",
    "WatchlistPostingSource",
    "WorkableSearchSource",
    "WorkableWidgetSource",
    "WorkdaySource",
    "classify_apply_url",
    "dedupe",
    "load_watchlist",
    "parse_watchlist",
    "source_for",
    "sponsorship_hints",
    "watchlist_from_apply_urls",
    "watchlist_from_simplify",
]
