"""Fan out across a watchlist and return one deduplicated posting list.

Isolation is the point: a single unreachable or rate-limited board must never
sink the run, so every source is wrapped and failures are logged and counted
rather than raised. Companies re-post identical requisitions (Affirm and Samsara
both do it), so results are collapsed on ``Posting.dedupe_key`` with the
best-described copy winning.
"""
from __future__ import annotations

from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from copilot.adapters.ats._http import AtsFetchError
from copilot.adapters.ats.ashby import AshbySource
from copilot.adapters.ats.greenhouse import GreenhouseSource
from copilot.adapters.ats.lever import LeverSource
from copilot.adapters.ats.watchlist import WatchlistEntry
from copilot.adapters.ats.workable import WorkableWidgetSource
from copilot.adapters.ats.workday import WorkdaySource
from copilot.domain.posting import Posting
from copilot.logging import get_logger
from copilot.ports.postingsource import PostingSourcePort

_LOG = get_logger("copilot.adapters.ats.aggregate")


def source_for(
    entry: WatchlistEntry, *, search_text: str = "software engineer"
) -> PostingSourcePort:
    """Build the right adapter for a watchlist entry."""
    if entry.ats == "greenhouse":
        return GreenhouseSource(entry.tenant)
    if entry.ats == "ashby":
        return AshbySource(entry.tenant)
    if entry.ats == "lever":
        return LeverSource(entry.tenant)
    if entry.ats == "workable":
        return WorkableWidgetSource(entry.tenant)
    if entry.ats == "workday":
        return WorkdaySource(entry.tenant, entry.wd, entry.site, search_text=search_text)
    raise ValueError(f"no adapter for ats {entry.ats!r}")


def dedupe(postings: Sequence[Posting]) -> list[Posting]:
    """Collapse duplicate requisitions, preferring the copy with a description."""
    best: dict[tuple[str, str], Posting] = {}
    for posting in postings:
        key = posting.dedupe_key
        incumbent = best.get(key)
        if incumbent is None:
            best[key] = posting
            continue
        if len(posting.description) > len(incumbent.description):
            best[key] = posting
    return list(best.values())


@dataclass
class FetchReport:
    """What happened during a fan-out. Surfaced so silent shrinkage is visible."""

    raw: int = 0
    unique: int = 0
    ok_sources: int = 0
    failed_sources: list[tuple[str, str]] = field(default_factory=list)
    no_description: int = 0

    @property
    def dropped_as_duplicate(self) -> int:
        return self.raw - self.unique


class WatchlistPostingSource:
    """PostingSourcePort fanning out over every entry in a watchlist.

    A real watchlist is several hundred boards, so fetching is concurrent —
    but deliberately *bounded*. These are other people's public job boards; a
    handful of workers keeps a full sweep to a couple of minutes without looking
    like an attack. ``max_workers=1`` restores fully serial behaviour.
    """

    name = "watchlist"

    def __init__(
        self,
        entries: Sequence[WatchlistEntry],
        *,
        extra_sources: Sequence[PostingSourcePort] = (),
        search_text: str = "software engineer",
        max_workers: int = 6,
    ) -> None:
        self._entries = list(entries)
        self._extra = list(extra_sources)
        self._search_text = search_text
        self._max_workers = max(1, max_workers)
        self.report = FetchReport()

    def _fetch_one(self, label: str, source: PostingSourcePort) -> tuple[str, list[Posting], str]:
        """Never raises — failure is returned as a message so one board can't sink a run."""
        try:
            return label, source.fetch(), ""
        except AtsFetchError as exc:
            return label, [], str(exc)
        except Exception as exc:
            _LOG.warning("source_error", extra={"extra_fields": {"source": label}}, exc_info=True)
            return label, [], f"{type(exc).__name__}: {exc}"

    def fetch(self) -> list[Posting]:
        report = FetchReport()
        collected: list[Posting] = []

        sources: list[tuple[str, PostingSourcePort]] = [
            (f"{entry.ats}:{entry.tenant}", source_for(entry, search_text=self._search_text))
            for entry in self._entries
        ]
        sources.extend((f"extra:{src.name}", src) for src in self._extra)

        if self._max_workers == 1 or len(sources) == 1:
            results = [self._fetch_one(label, src) for label, src in sources]
        else:
            with ThreadPoolExecutor(max_workers=self._max_workers) as pool:
                results = list(pool.map(lambda pair: self._fetch_one(*pair), sources))

        for label, found, error in results:
            if error:
                report.failed_sources.append((label, error))
                _LOG.warning("source_failed", extra={"extra_fields": {"source": label}})
                continue
            report.ok_sources += 1
            collected.extend(found)

        report.raw = len(collected)
        unique = dedupe(collected)
        report.unique = len(unique)
        report.no_description = sum(1 for p in unique if not p.desc_available)
        self.report = report
        _LOG.info(
            "watchlist_fetch_complete",
            extra={
                "extra_fields": {
                    "raw": report.raw,
                    "unique": report.unique,
                    "ok_sources": report.ok_sources,
                    "failed_sources": len(report.failed_sources),
                    "no_description": report.no_description,
                }
            },
        )
        return unique
