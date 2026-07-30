"""Persistence behaviour — the part that makes a daily digest possible."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from copilot.adapters.sqlite_posting_store import SqlitePostingStore
from copilot.domain.posting import Posting

DAY1 = datetime(2026, 7, 1, 9, 0, tzinfo=UTC)
DAY2 = DAY1 + timedelta(days=1)
DAY3 = DAY1 + timedelta(days=2)


def post(n: int, *, desc: str = "a description", company: str = "Acme") -> Posting:
    return Posting(
        title=f"Software Engineer {n}",
        company=company,
        url=f"https://boards.example/{n}",
        ats="greenhouse",
        description=desc,
        desc_available=bool(desc),
        posted_at=DAY1,
    )


def store() -> SqlitePostingStore:
    return SqlitePostingStore(":memory:")


class TestSync:
    def test_first_fetch_is_all_new(self) -> None:
        s = store()
        new, seen = s.sync([post(1), post(2)], now=DAY1)
        assert len(new) == 2
        assert seen == []

    def test_second_fetch_of_the_same_roles_is_not_new(self) -> None:
        """This is the whole point: day 2 must not look like day 1."""
        s = store()
        s.sync([post(1), post(2)], now=DAY1)
        new, seen = s.sync([post(1), post(2)], now=DAY2)
        assert new == []
        assert len(seen) == 2

    def test_only_genuinely_new_roles_are_reported(self) -> None:
        s = store()
        s.sync([post(1)], now=DAY1)
        new, seen = s.sync([post(1), post(2)], now=DAY2)
        assert len(new) == 1
        assert len(seen) == 1

    def test_first_seen_is_never_overwritten(self) -> None:
        s = store()
        s.sync([post(1)], now=DAY1)
        s.sync([post(1)], now=DAY3)
        assert s.new_since(DAY2) == []  # still dated to day 1, so not "new"

    def test_new_since_is_the_diff_feed(self) -> None:
        s = store()
        s.sync([post(1)], now=DAY1)
        s.sync([post(1), post(2)], now=DAY2)
        fresh = s.new_since(DAY1 + timedelta(hours=1))
        assert [p.title for p in fresh] == ["Software Engineer 2"]


class TestDescriptionPreservation:
    def test_an_empty_description_never_overwrites_a_real_one(self) -> None:
        """Workday returns no description; the same role from Greenhouse does."""
        s = store()
        s.sync([post(1, desc="the full text")], now=DAY1)
        s.sync([post(1, desc="")], now=DAY2)
        [stored] = s.open_postings()
        assert stored.description == "the full text"
        assert stored.desc_available is True

    def test_a_real_description_upgrades_an_empty_one(self) -> None:
        s = store()
        s.sync([post(1, desc="")], now=DAY1)
        s.sync([post(1, desc="now we have it")], now=DAY2)
        [stored] = s.open_postings()
        assert stored.description == "now we have it"
        assert stored.desc_available is True


class TestClosing:
    def test_absent_postings_are_closed(self) -> None:
        s = store()
        s.sync([post(1), post(2)], now=DAY1)
        s.sync([post(1)], now=DAY2)
        closed = s.close_missing(now=DAY2, seen_ids={post(1).id})
        assert closed == 1
        assert [p.title for p in s.open_postings()] == ["Software Engineer 1"]

    def test_an_empty_fetch_does_not_mass_close(self) -> None:
        """An empty fetch is a broken run, not a market where every job vanished."""
        s = store()
        s.sync([post(1), post(2)], now=DAY1)
        assert s.close_missing(now=DAY2, seen_ids=set()) == 0
        assert len(s.open_postings()) == 2

    def test_a_reappearing_posting_is_reopened(self) -> None:
        s = store()
        s.sync([post(1), post(2)], now=DAY1)
        s.close_missing(now=DAY2, seen_ids={post(1).id})
        s.sync([post(2)], now=DAY3)
        assert len(s.open_postings()) == 2


class TestInterpretationCache:
    def test_round_trip(self) -> None:
        s = store()
        s.sync([post(1)], now=DAY1)
        assert s.cached_interpretation(post(1).id) is None
        s.save_interpretation(post(1).id, {"band": "entry", "min_years": 0})
        assert s.cached_interpretation(post(1).id) == {"band": "entry", "min_years": 0}

    def test_uncached_ids_drives_the_batch(self) -> None:
        """The cost lever: a posting's description is read once, not once per day."""
        s = store()
        s.sync([post(1), post(2), post(3)], now=DAY1)
        ids = [post(n).id for n in (1, 2, 3)]
        assert len(s.uncached_ids(ids)) == 3
        s.save_interpretation(post(2).id, {"band": "mid"})
        assert s.uncached_ids(ids) == [post(1).id, post(3).id]

    def test_cache_survives_a_refetch(self) -> None:
        s = store()
        s.sync([post(1)], now=DAY1)
        s.save_interpretation(post(1).id, {"band": "entry"})
        s.sync([post(1)], now=DAY2)
        assert s.cached_interpretation(post(1).id) == {"band": "entry"}

    def test_large_id_lists_are_chunked_under_the_variable_cap(self) -> None:
        s = store()
        many = [post(n) for n in range(1200)]
        s.sync(many, now=DAY1)
        assert len(s.uncached_ids([p.id for p in many])) == 1200


class TestApplied:
    def test_mark_applied_is_idempotent(self) -> None:
        s = store()
        s.sync([post(1)], now=DAY1)
        s.mark_applied(post(1).id, now=DAY1)
        s.mark_applied(post(1).id, now=DAY3)
        assert s.stats()["applied"] == 1


class TestStats:
    def test_counts(self) -> None:
        s = store()
        s.sync([post(1), post(2), post(3)], now=DAY1)
        s.close_missing(now=DAY2, seen_ids={post(1).id, post(2).id})
        s.save_interpretation(post(1).id, {"band": "entry"})
        s.mark_applied(post(1).id, now=DAY2)
        assert s.stats() == {
            "total": 3, "open": 2, "closed": 1, "interpreted": 1, "applied": 1
        }
