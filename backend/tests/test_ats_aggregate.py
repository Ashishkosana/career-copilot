"""Watchlist validation, dedupe, and fan-out isolation."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from copilot.adapters.ats import (
    AtsFetchError,
    WatchlistEntry,
    WatchlistPostingSource,
    classify_apply_url,
    dedupe,
    load_watchlist,
    parse_watchlist,
    source_for,
    sponsorship_hints,
    watchlist_from_apply_urls,
    watchlist_from_simplify,
)
from copilot.domain.posting import Posting


def posting(company: str, title: str, *, url: str = "", desc: str = "") -> Posting:
    return Posting(
        title=title,
        company=company,
        url=url or f"https://x/{company}/{title}".replace(" ", "-"),
        ats="greenhouse",
        description=desc,
        desc_available=bool(desc),
    )


class TestWatchlist:
    def test_rejects_unknown_ats(self) -> None:
        assert parse_watchlist([{"company": "A", "ats": "taleo", "tenant": "a"}]) == []

    def test_normalizes_ats_case(self) -> None:
        [entry] = parse_watchlist([{"company": "A", "ats": "GreenHouse", "tenant": "a"}])
        assert entry.ats == "greenhouse"

    def test_workday_needs_shard_and_site(self) -> None:
        incomplete = {"company": "N", "ats": "workday", "tenant": "nvidia"}
        assert parse_watchlist([incomplete]) == []
        complete = {**incomplete, "wd": "wd5", "site": "ExternalSite"}
        assert len(parse_watchlist([complete])) == 1

    def test_accepts_both_document_shapes(self) -> None:
        row = {"company": "A", "ats": "lever", "tenant": "a"}
        assert len(parse_watchlist([row])) == 1
        assert len(parse_watchlist({"companies": [row]})) == 1
        assert parse_watchlist("nope") == []

    def test_load_missing_file_is_empty_not_an_error(self) -> None:
        assert load_watchlist("/nonexistent/watchlist.json") == []

    def test_load_roundtrip(self, tmp_path: Path) -> None:
        target = tmp_path / "watchlist.json"
        target.write_text(
            json.dumps({"companies": [{"company": "A", "ats": "ashby", "tenant": "a"}]})
        )
        [entry] = load_watchlist(target)
        assert entry.tenant == "a"

    def test_source_for_each_supported_ats(self) -> None:
        cases = [
            WatchlistEntry(company="A", ats="greenhouse", tenant="a"),
            WatchlistEntry(company="A", ats="ashby", tenant="a"),
            WatchlistEntry(company="A", ats="lever", tenant="a"),
            WatchlistEntry(company="A", ats="workable", tenant="a"),
            WatchlistEntry(company="A", ats="workday", tenant="a", wd="wd5", site="s"),
        ]
        for entry in cases:
            assert source_for(entry) is not None


class TestDedupe:
    def test_collapses_reposted_requisitions(self) -> None:
        """Affirm and Samsara both post identical reqs twice under different ids."""
        rows = [
            posting("Affirm", "Software Engineer I", url="https://x/1"),
            posting("Affirm", "Software Engineer I", url="https://x/2"),
        ]
        assert len(dedupe(rows)) == 1

    def test_prefers_the_copy_with_a_description(self) -> None:
        rows = [
            posting("Acme", "SWE", url="https://x/1"),
            posting("Acme", "SWE", url="https://x/2", desc="a much longer description"),
        ]
        [kept] = dedupe(rows)
        assert kept.desc_available is True

    def test_case_and_whitespace_insensitive(self) -> None:
        rows = [
            posting("Acme", "SWE", url="https://x/1"),
            posting(" acme ", " swe ", url="https://x/2"),
        ]
        assert len(dedupe(rows)) == 1

    def test_keeps_genuinely_distinct_roles(self) -> None:
        rows = [posting("Acme", "Backend Engineer"), posting("Acme", "Frontend Engineer")]
        assert len(dedupe(rows)) == 2


class FakeSource:
    def __init__(self, name: str, result: list[Posting] | Exception) -> None:
        self.name = name
        self._result = result

    def fetch(self) -> list[Posting]:
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


class TestFanOutIsolation:
    def test_one_dead_board_does_not_sink_the_run(self) -> None:
        good = FakeSource("good", [posting("Acme", "SWE", desc="d")])
        dead = FakeSource("dead", AtsFetchError("https://x", "HTTP 429", 429))
        source = WatchlistPostingSource([], extra_sources=[good, dead])
        assert len(source.fetch()) == 1
        assert source.report.ok_sources == 1
        assert len(source.report.failed_sources) == 1

    def test_unexpected_exceptions_are_also_contained(self) -> None:
        boom = FakeSource("boom", ValueError("malformed json"))
        source = WatchlistPostingSource([], extra_sources=[boom])
        assert source.fetch() == []
        assert source.report.failed_sources[0][0] == "extra:boom"

    def test_report_surfaces_silent_shrinkage(self) -> None:
        dupes = FakeSource(
            "dupes",
            [
                posting("Acme", "SWE", url="https://x/1", desc="d"),
                posting("Acme", "SWE", url="https://x/2"),
                posting("Acme", "Other", url="https://x/3"),
            ],
        )
        source = WatchlistPostingSource([], extra_sources=[dupes])
        source.fetch()
        assert source.report.raw == 3
        assert source.report.unique == 2
        assert source.report.dropped_as_duplicate == 1
        assert source.report.no_description == 1

    def test_unknown_ats_raises_at_construction_not_silently(self) -> None:
        with pytest.raises(ValueError, match="unsupported ats"):
            WatchlistEntry(company="A", ats="taleo", tenant="a")


class TestPostingIdentity:
    def test_id_is_stable_and_url_keyed(self) -> None:
        a = posting("Acme", "SWE", url="https://x/1")
        b = posting("Other", "Different", url="https://x/1")
        assert a.id == b.id

    def test_frozen(self) -> None:
        with pytest.raises(ValueError, match="frozen"):
            posting("Acme", "SWE").title = "changed"  # type: ignore[misc]


class TestDiscovery:
    def test_classifies_each_supported_host(self) -> None:
        cases = {
            "https://job-boards.greenhouse.io/flexport/jobs/7978127": ("greenhouse", "flexport"),
            "https://boards.greenhouse.io/acme/jobs/1": ("greenhouse", "acme"),
            "https://jobs.ashbyhq.com/notion/abc-uuid": ("ashby", "notion"),
            "https://jobs.lever.co/palantir/uuid": ("lever", "palantir"),
            "https://apply.workable.com/hotjar/j/ABC": ("workable", "hotjar"),
        }
        for url, (ats, tenant) in cases.items():
            entry = classify_apply_url(url)
            assert entry is not None
            assert (entry.ats, entry.tenant) == (ats, tenant)

    def test_workday_extracts_tenant_shard_and_site(self) -> None:
        entry = classify_apply_url(
            "https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite/job/X_JR1"
        )
        assert entry is not None
        assert (entry.ats, entry.tenant, entry.wd, entry.site) == (
            "workday",
            "nvidia",
            "wd5",
            "NVIDIAExternalCareerSite",
        )

    def test_workday_skips_locale_segment(self) -> None:
        entry = classify_apply_url("https://kla.wd1.myworkdayjobs.com/en-US/Search/job/Y_JR2")
        assert entry is not None
        assert entry.site == "Search"

    def test_greenhouse_embed_route_reads_tenant_from_query(self) -> None:
        """boards.greenhouse.io/embed/job_board?for=<tenant> — 'embed' is a route."""
        entry = classify_apply_url("https://boards.greenhouse.io/embed/job_board?for=acme")
        assert entry is not None
        assert entry.tenant == "acme"

    def test_unsupported_ats_is_reported_not_silently_dropped(self) -> None:
        rows = [
            ("A", "https://jobs.smartrecruiters.com/Acme/123"),
            ("B", "https://jobs.lever.co/b/uuid"),
            ("C", "https://careers.example.org/jobs/1"),
        ]
        entries, report = watchlist_from_apply_urls(rows)
        assert [e.ats for e in entries] == ["lever"]
        assert report.unsupported_ats["smartrecruiters"] == 1
        assert "careers.example.org" in report.unclassified_hosts

    def test_simplify_shape_and_active_filter(self) -> None:
        listings = [
            {
                "company_name": "Acme",
                "url": "https://jobs.lever.co/acme/uuid",
                "active": True,
                "sponsorship": "Offers Sponsorship",
            },
            {"company_name": "Old", "url": "https://jobs.lever.co/old/uuid", "active": False},
        ]
        entries, _ = watchlist_from_simplify(listings)
        assert [e.tenant for e in entries] == ["acme"]
        assert sponsorship_hints(listings)["acme"] == "offers sponsorship"

    def test_dedupes_boards_across_many_postings(self) -> None:
        rows = [("Acme", f"https://jobs.lever.co/acme/uuid-{n}") for n in range(50)]
        entries, report = watchlist_from_apply_urls(rows)
        assert len(entries) == 1
        assert report.supported == 50
