"""Persistence behaviour — the part that makes a daily digest possible.

The behavioural cases live in ``tests/test_dynamodb_posting_store.py``, where one
parametrised fixture drives *both* implementations of ``PostingStorePort`` through
the same assertions. What is left here is what only the SQLite adapter can get
wrong: schema creation on an already-populated file, and the one place the two
stores deliberately differ — SQLite sweeps a superseded screening generation with
a DELETE, while DynamoDB leaves it to TTL because there a delete costs what a
write costs and there are ~84,000 of them a day.
"""
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from copilot.adapters.sqlite_posting_store import SqlitePostingStore
from copilot.domain.posting import Posting
from copilot.ports.postingstore import VIEW_KEPT, ScreenedRow, ScreenSummary

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


def kept_row(n: int) -> ScreenedRow:
    return ScreenedRow(
        posting_id=post(n).id,
        view=VIEW_KEPT,
        posted_at=DAY1,
        kept=True,
        level="entry",
        level_source="title",
        level_why="the title carries an entry marker",
        eligibility_checked=True,
        sponsorship="unstated",
    )


def summary(generation: str, *, kept: int) -> ScreenSummary:
    return ScreenSummary(
        generation=generation,
        screened_at=DAY2,
        corpus_size=kept,
        screened=kept,
        kept=kept,
        excluded=0,
        gates={},
        needs_level_check=0,
        eligible_total=kept,
        internship_total=0,
    )


class TestScreeningViewStorage:
    def test_a_superseded_generation_is_swept(self) -> None:
        """The one place the two stores differ on purpose. SQLite can afford the
        DELETE; DynamoDB uses TTL because there a delete costs what a write costs and
        a run writes ~84,000 rows. Left unswept, a laptop's file would grow by a
        whole screening pass a day for rows nothing can address.
        """
        s = store()
        s.save_screening([kept_row(1), kept_row(2)], summary=summary("gen-1", kept=2))
        s.save_screening([kept_row(3)], summary=summary("gen-2", kept=1))
        generations = {
            str(row["generation"])
            for row in s._conn.execute("SELECT DISTINCT generation FROM screen_rows")
        }
        assert generations == {"gen-2"}

    def test_the_sweep_happens_after_the_publish_not_before(self) -> None:
        """Deleting first would empty the live view for the duration of the write —
        a window in which the page is blank and honestly reports itself as complete.
        """
        s = store()
        s.save_screening([kept_row(1)], summary=summary("gen-1", kept=1))
        published = s.screening_summary()
        assert published is not None and published.generation == "gen-1"
        assert len(s.screened_page(VIEW_KEPT, generation="gen-1", limit=10).rows) == 1

    def test_only_one_summary_row_can_exist(self) -> None:
        """"Which generation is current" must not be a query with two answers."""
        s = store()
        s.save_screening([kept_row(1)], summary=summary("gen-1", kept=1))
        s.save_screening([kept_row(2)], summary=summary("gen-2", kept=1))
        [(count,)] = s._conn.execute("SELECT COUNT(*) FROM screen_summary").fetchall()
        assert count == 1
        with pytest.raises(sqlite3.IntegrityError):
            s._conn.execute(
                "INSERT INTO screen_summary (id, generation, payload) VALUES ('OTHER', 'g', '{}')"
            )

    def test_the_view_tables_appear_on_an_already_populated_file(self, tmp_path: Path) -> None:
        """``CREATE TABLE IF NOT EXISTS`` is a no-op on an existing file, which is the
        trap ``experience_level`` fell into: a new *column* needs a migration. New
        *tables* do not — but only if the schema script actually runs on every open,
        which is what this asserts against the developer's real 25k-row file shape.
        """
        path = tmp_path / "postings.db"
        first = SqlitePostingStore(path)
        first.sync([post(1)], now=DAY1)
        first.close()

        reopened = SqlitePostingStore(path)
        reopened.save_screening([kept_row(1)], summary=summary("gen-1", kept=1))
        stored = reopened.screening_summary()
        assert stored is not None and stored.kept == 1
        reopened.close()

        # ...and it survives another open, which is what a warm Lambda does.
        again = SqlitePostingStore(path)
        assert len(again.screened_page(VIEW_KEPT, generation="gen-1", limit=10).rows) == 1
        again.close()


class TestTheFirstSeenColumnOnAnOlderFile:
    """The column this file gained after a view was already published in it.

    ``screen_rows.first_seen`` is added by ``_migrate``, not by ``CREATE TABLE IF NOT
    EXISTS``, which is a no-op on an existing table — the trap ``experience_level`` fell
    into. What is different here is that the table already holds *rows*: the developer's
    25k-row ``data/postings.db`` and the deployed table both carry a published view that
    predates the field, and those rows must keep serving as ``firstSeen: null`` until
    tomorrow's cron republishes them. A schema bump instead of a nullable column is how
    400 LLM interpretation rows once ended up counted as cached and refused on read,
    permanently; this is the same shape and must not repeat it.
    """

    def _pre_change_file(self, path: Path) -> None:
        """The exact ``screen_rows`` this code shipped before ``first_seen`` existed."""
        with sqlite3.connect(path) as conn:
            conn.execute(
                """
                CREATE TABLE screen_rows (
                    generation TEXT NOT NULL, view TEXT NOT NULL, sort_key TEXT NOT NULL,
                    posting_id TEXT NOT NULL, kept INTEGER NOT NULL, level TEXT NOT NULL,
                    level_source TEXT NOT NULL, level_why TEXT NOT NULL DEFAULT '',
                    eligibility_checked INTEGER NOT NULL, sponsorship TEXT NOT NULL,
                    gate TEXT NOT NULL DEFAULT '', reason TEXT NOT NULL DEFAULT '',
                    quote TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY (generation, view, sort_key)
                ) WITHOUT ROWID
                """
            )
            conn.execute(
                "INSERT INTO screen_rows (generation, view, sort_key, posting_id, kept, "
                "level, level_source, eligibility_checked, sponsorship) "
                "VALUES ('gen-0', ?, ?, ?, 1, 'entry', 'title', 1, 'unstated')",
                (VIEW_KEPT, kept_row(1).sort_key, post(1).id),
            )

    def test_a_view_written_before_the_column_serves_as_null_instead_of_raising(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "postings.db"
        self._pre_change_file(path)

        store = SqlitePostingStore(path)
        [stored] = store.screened_page(VIEW_KEPT, generation="gen-0", limit=10).rows
        assert stored.posting_id == post(1).id
        assert stored.first_seen is None, "not recorded, and that is a fact not an error"
        store.close()

    def test_the_next_pass_republishes_the_same_view_with_the_stamp(
        self, tmp_path: Path
    ) -> None:
        """The whole compatibility story is "one stale day", so the second day matters
        as much as the first: nothing has to be backfilled because the cron rebuilds the
        view every morning from a corpus that has always had the column.
        """
        path = tmp_path / "postings.db"
        self._pre_change_file(path)

        store = SqlitePostingStore(path)
        store.sync([post(1)], now=DAY1)
        store.save_screening([kept_row(1)], summary=summary("gen-1", kept=1))
        [stored] = store.screened_page(VIEW_KEPT, generation="gen-1", limit=10).rows
        assert stored.first_seen == DAY1
        store.close()

    def test_opening_the_file_twice_does_not_add_the_column_again(
        self, tmp_path: Path
    ) -> None:
        """``ALTER TABLE ADD COLUMN`` is an error, not a no-op, on a column that is
        already there — so a migration that is not guarded takes every warm start down.
        """
        path = tmp_path / "postings.db"
        self._pre_change_file(path)

        SqlitePostingStore(path).close()
        store = SqlitePostingStore(path)
        columns = [
            str(row["name"]) for row in store._conn.execute("PRAGMA table_info(screen_rows)")
        ]
        assert columns.count("first_seen") == 1
        store.close()
