"""SQLite implementation of :class:`PostingStorePort`.

SQLite because it is the right tool for this shape of problem: one file, no
server, no setup, trivially inspectable with any SQL client, and ~25k rows is
nothing for it. The same port has a DynamoDB implementation for the deployed
cron; the domain never learns which one it is talking to.

Three columns carry the product logic:

* ``first_seen`` — this is what "new today" means. Set once, never updated.
* ``last_seen`` — bumped on every fetch that still contains the posting.
* ``closed_at`` — set when a posting stops appearing. A role vanishing from the
  feed is a real signal, and it is invisible without history.

``interpretation`` is the LLM cache, keyed by posting id (a hash of the URL). A
posting sits in a feed for weeks; reading its description once instead of once
per day is a larger cost saving than any model choice.

Two further tables hold the **materialised screening view** (see
:mod:`copilot.ports.postingstore`): ``screen_rows`` is one row per
(posting, view) pair and ``screen_summary`` is the single published funnel. The
generation column is what makes publishing atomic — rows land under a new
generation that nothing reads until the summary names it — and it is why a screen
that dies half-way cannot leave a half-written view that reads as authoritative.

``screen_rows`` carries a copy of ``first_seen`` for the same reason it carries the
level verdict: a read is one page of one view plus a hydrate, and a card that had to
ask the corpus "when did we first see this" would add a lookup per page for a fact the
publish already had in hand.
"""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Iterator, Sequence
from contextlib import closing, contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from copilot.domain.posting import Posting
from copilot.logging import get_logger
from copilot.ports.postingstore import (
    SCREEN_VIEWS,
    ScreenedPage,
    ScreenedRow,
    ScreenSummary,
    first_seen_from_stamp,
    first_seen_stamp,
    posted_at_from_sort_key,
    summary_from_json,
    summary_to_json,
)

_LOG = get_logger("copilot.adapters.sqlite_posting_store")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS postings (
    id              TEXT PRIMARY KEY,
    url             TEXT NOT NULL,
    title           TEXT NOT NULL,
    company         TEXT NOT NULL,
    ats             TEXT NOT NULL,
    tenant          TEXT NOT NULL DEFAULT '',
    location        TEXT NOT NULL DEFAULT '',
    description     TEXT NOT NULL DEFAULT '',
    desc_available  INTEGER NOT NULL DEFAULT 1,
    req_id          TEXT NOT NULL DEFAULT '',
    posted_at       TEXT,
    remote          INTEGER,
    employment_type TEXT NOT NULL DEFAULT '',
    experience_level TEXT NOT NULL DEFAULT '',
    first_seen      TEXT NOT NULL,
    last_seen       TEXT NOT NULL,
    closed_at       TEXT,
    interpretation  TEXT,
    applied_at      TEXT
);
CREATE INDEX IF NOT EXISTS idx_postings_first_seen ON postings(first_seen);
CREATE INDEX IF NOT EXISTS idx_postings_open ON postings(closed_at) WHERE closed_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_postings_company ON postings(company);

-- The materialised screening view. One row per (posting, view): a posting that
-- fails three gates is filed under all three, because /excluded pages per gate.
CREATE TABLE IF NOT EXISTS screen_rows (
    generation          TEXT NOT NULL,
    view                TEXT NOT NULL,
    sort_key            TEXT NOT NULL,
    posting_id          TEXT NOT NULL,
    kept                INTEGER NOT NULL,
    level               TEXT NOT NULL,
    level_source        TEXT NOT NULL,
    level_why           TEXT NOT NULL DEFAULT '',
    eligibility_checked INTEGER NOT NULL,
    sponsorship         TEXT NOT NULL,
    gate                TEXT NOT NULL DEFAULT '',
    reason              TEXT NOT NULL DEFAULT '',
    quote               TEXT NOT NULL DEFAULT '',
    -- When *we* first saw the posting, copied off the corpus row at publish time.
    -- Nullable, and that is the whole compatibility story: rows written before this
    -- column existed read as NULL, which is the truth about them.
    first_seen          TEXT,
    -- (generation, view, sort_key) rather than a rowid: this *is* the index the
    -- read path pages on, so the primary key does the work and no second B-tree
    -- is written 45k times a day for nothing.
    PRIMARY KEY (generation, view, sort_key)
) WITHOUT ROWID;

-- Exactly one published view. The CHECK is the schema saying so: a second summary
-- row would make "which generation is current" a query with two answers.
CREATE TABLE IF NOT EXISTS screen_summary (
    id          TEXT PRIMARY KEY CHECK (id = 'CURRENT'),
    generation  TEXT NOT NULL,
    payload     TEXT NOT NULL
);
"""

_COLUMNS = (
    "id, url, title, company, ats, tenant, location, description, desc_available, "
    "req_id, posted_at, remote, employment_type, experience_level"
)

#: ``(table, column, ddl)`` for every column added after the first release.
#: ``CREATE TABLE IF NOT EXISTS`` is a no-op on an existing file, so a new column in
#: :data:`_SCHEMA` alone would leave every already-populated database (the developer's
#: ~25k-row ``data/postings.db``) one column short and every read raising ``KeyError``.
#: Applied idempotently at open.
#:
#: ``screen_rows.first_seen`` is nullable on purpose: the ALTER adds it to a table that
#: already holds a published view, and those rows genuinely do not know the answer. They
#: read as ``firstSeen: null`` until the next cron pass republishes — no backfill, and
#: nothing refuses a page it can still serve.
_ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("postings", "experience_level", "TEXT NOT NULL DEFAULT ''"),
    ("screen_rows", "first_seen", "TEXT"),
)


def _iso(value: datetime) -> str:
    return value.isoformat()


class SqlitePostingStore:
    """PostingStorePort backed by a single SQLite file."""

    def __init__(self, path: str | Path = "postings.db") -> None:
        self._path = str(path)
        if self._path != ":memory:":
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        # A single shared connection: SQLite handles this fine for our access
        # pattern, and it keeps :memory: databases alive between calls.
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._migrate()
        self._conn.commit()

    def _migrate(self) -> None:
        """Add any column this code needs that an older file does not have.

        Deliberately additive-only and idempotent — no version table, no
        down-migration. The corpus is *derived* data: it re-fetches from public
        boards in under a minute, so the cost of a wrong migration is far higher
        than the cost of rebuilding, and the only thing worth preserving across a
        schema change is ``first_seen`` and the LLM cache.

        The view tables are migrated the same way and for a sharper reason: they are
        rebuilt daily by the cron, so an added column needs no backfill at all — but the
        *existing* rows must keep being readable until then, which an ALTER to a nullable
        column gives and a ``DROP TABLE`` would not.
        """
        for table, name, ddl in _ADDED_COLUMNS:
            have = {
                str(row["name"])
                for row in self._conn.execute(f"PRAGMA table_info({table})")
            }
            if name not in have:
                self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self._conn
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    # --- writes ---------------------------------------------------------------

    def sync(self, postings: list[Posting], *, now: datetime) -> tuple[list[str], list[str]]:
        """Upsert a fetch, preserving ``first_seen`` and clearing any prior close."""
        stamp = _iso(now)
        known = self._existing_ids([p.id for p in postings])
        new_ids = [p.id for p in postings if p.id not in known]
        seen_ids = [p.id for p in postings if p.id in known]

        rows = [
            (
                p.id, p.url, p.title, p.company, p.ats, p.tenant, p.location,
                p.description, int(p.desc_available), p.req_id,
                _iso(p.posted_at) if p.posted_at else None,
                None if p.remote is None else int(p.remote),
                p.employment_type, p.experience_level, stamp, stamp,
            )
            for p in postings
        ]
        with self._tx() as conn:
            conn.executemany(
                f"""
                INSERT INTO postings ({_COLUMNS}, first_seen, last_seen)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                    last_seen = excluded.last_seen,
                    closed_at = NULL,
                    title = excluded.title,
                    location = excluded.location,
                    -- never overwrite a real description with an empty one
                    description = CASE
                        WHEN excluded.desc_available = 1 THEN excluded.description
                        ELSE postings.description END,
                    desc_available = MAX(postings.desc_available, excluded.desc_available)
                """,
                rows,
            )
        _LOG.info(
            "posting_sync",
            extra={"extra_fields": {"new": len(new_ids), "seen_again": len(seen_ids)}},
        )
        return new_ids, seen_ids

    def close_missing(self, *, now: datetime, seen_ids: set[str]) -> int:
        """Mark open postings that were not in this fetch as closed."""
        with self._tx() as conn:
            if seen_ids:
                placeholders = ",".join("?" * len(seen_ids))
                cursor = conn.execute(
                    f"UPDATE postings SET closed_at = ? "
                    f"WHERE closed_at IS NULL AND id NOT IN ({placeholders})",
                    (_iso(now), *seen_ids),
                )
            else:
                # An empty fetch is far more likely to be a broken run than a
                # market where every job closed at once. Refuse to mass-close.
                _LOG.warning("close_missing_skipped_empty_fetch")
                return 0
            return cursor.rowcount

    def save_interpretation(self, posting_id: str, payload: dict[str, Any]) -> None:
        with self._tx() as conn:
            conn.execute(
                "UPDATE postings SET interpretation = ? WHERE id = ?",
                (json.dumps(payload, sort_keys=True), posting_id),
            )

    def mark_applied(self, posting_id: str, *, now: datetime) -> None:
        with self._tx() as conn:
            conn.execute(
                "UPDATE postings SET applied_at = ? WHERE id = ? AND applied_at IS NULL",
                (_iso(now), posting_id),
            )

    # --- the materialised screening view -------------------------------------

    def save_screening(self, rows: Iterable[ScreenedRow], *, summary: ScreenSummary) -> None:
        """Write every row under the new generation, then publish the summary.

        Two transactions on purpose, in this order:

        1. the rows, which no reader can find yet because nothing names their
           generation;
        2. the summary, which is the publish.

        A crash between them leaves orphan rows and the *previous* view still
        current — aging but complete — which is the only failure shape a reader can
        answer honestly. Doing it in one transaction would be tidier here and
        impossible on DynamoDB, and a port whose two implementations have different
        crash semantics is a port that proves nothing.

        Old generations are deleted **after** publishing, for the same reason:
        deleting first would empty the live view for the duration of the write.
        SQLite can afford that sweep — 45,158 rows land in 0.48 s on the real
        corpus — while the DynamoDB adapter uses a TTL instead, because there deletes
        cost as much as writes and ~85k of them a day is real money for rows nothing
        can read.

        ``first_seen`` is stamped here from the corpus rather than taken from the row,
        because the store owns that column and the pure screening pass upstream has no
        access to it. One query for the whole map, ahead of the loop, deliberately: the
        alternative — a correlated subquery per row — is 45,158 index probes for a join
        that costs 20 ms done once, and the same shape as the DynamoDB adapter, which is
        what keeps the two answering identically.
        """
        stamps = self._open_first_seen()
        payload = [
            (
                summary.generation,
                row.view,
                row.sort_key,
                row.posting_id,
                int(row.kept),
                row.level,
                row.level_source,
                row.level_why,
                int(row.eligibility_checked),
                row.sponsorship,
                row.gate,
                row.reason,
                row.quote,
                first_seen_stamp(stamps.get(row.posting_id)),
            )
            for row in rows
        ]
        with self._tx() as conn:
            conn.executemany(
                """
                INSERT INTO screen_rows (
                    generation, view, sort_key, posting_id, kept, level,
                    level_source, level_why, eligibility_checked, sponsorship,
                    gate, reason, quote, first_seen
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(generation, view, sort_key) DO NOTHING
                """,
                payload,
            )
        with self._tx() as conn:
            conn.execute(
                """
                INSERT INTO screen_summary (id, generation, payload) VALUES ('CURRENT', ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    generation = excluded.generation, payload = excluded.payload
                """,
                (summary.generation, summary_to_json(summary)),
            )
        with self._tx() as conn:
            conn.execute("DELETE FROM screen_rows WHERE generation <> ?", (summary.generation,))
        _LOG.info(
            "screening_view_published",
            extra={"extra_fields": {
                "generation": summary.generation,
                "rows": len(payload),
                "kept": summary.kept,
                "screened": summary.screened,
            }},
        )

    def screening_summary(self) -> ScreenSummary | None:
        row = self._conn.execute(
            "SELECT payload FROM screen_summary WHERE id = 'CURRENT'"
        ).fetchone()
        if row is None:
            return None
        return summary_from_json(str(row["payload"]))

    def screened_page(
        self, view: str, *, generation: str, limit: int, after: str | None = None
    ) -> ScreenedPage:
        """One recency page of one view, served by the primary key alone.

        ``limit + 1`` rows are read so "is there a next page" is answered without a
        COUNT: a ``hasMore`` derived from a separate count can disagree with the
        page it describes when the view is republished between the two queries.
        """
        if view not in SCREEN_VIEWS:
            raise ValueError(f"unknown screening view {view!r}")
        sql = "SELECT * FROM screen_rows WHERE generation = ? AND view = ?"
        params: list[object] = [generation, view]
        if after is not None:
            sql += " AND sort_key < ?"
            params.append(after)
        sql += " ORDER BY sort_key DESC LIMIT ?"
        params.append(limit + 1)
        found = self._conn.execute(sql, params).fetchall()
        rows = tuple(self._to_screened_row(row) for row in found[:limit])
        next_token = rows[-1].sort_key if len(found) > limit and rows else None
        return ScreenedPage(rows=rows, next_token=next_token)

    def _open_first_seen(self) -> dict[str, str]:
        """``id -> first_seen`` for every open posting. The view's own copy comes from here.

        Open only, matching the corpus the screen covers, and named to match the
        DynamoDB adapter's method of the same name so the two publish paths read alike.
        Two columns of ~25k rows is ~1.5 MB and ~20 ms; it is a cron cost, and no
        handler may call it — that is the ``O(corpus)`` read the materialised view
        exists to keep off the request path.
        """
        return {
            str(row["id"]): str(row["first_seen"])
            for row in self._conn.execute(
                "SELECT id, first_seen FROM postings WHERE closed_at IS NULL"
            )
        }

    @staticmethod
    def _to_screened_row(row: sqlite3.Row) -> ScreenedRow:
        return ScreenedRow(
            posting_id=row["posting_id"],
            view=row["view"],
            # Out of the sort key, not a column of its own: the key holds it
            # UTC-normalised, and a column would keep whatever offset the ATS sent —
            # so the two stores would answer with two different strings.
            posted_at=posted_at_from_sort_key(str(row["sort_key"])),
            kept=bool(row["kept"]),
            level=row["level"],
            level_source=row["level_source"],
            level_why=row["level_why"],
            eligibility_checked=bool(row["eligibility_checked"]),
            sponsorship=row["sponsorship"],
            gate=row["gate"],
            reason=row["reason"],
            quote=row["quote"],
            # NULL for a row written before the column existed. Decoded, not raised on:
            # the next cron pass fills it in and until then the card says "not recorded".
            first_seen=first_seen_from_stamp(row["first_seen"]),
        )

    # --- reads ---------------------------------------------------------------

    def _existing_ids(self, ids: Sequence[str]) -> set[str]:
        found: set[str] = set()
        with closing(self._conn.cursor()) as cursor:
            for start in range(0, len(ids), 500):  # stay under SQLite's variable cap
                chunk = ids[start : start + 500]
                placeholders = ",".join("?" * len(chunk))
                cursor.execute(f"SELECT id FROM postings WHERE id IN ({placeholders})", chunk)
                found.update(row["id"] for row in cursor.fetchall())
        return found

    @staticmethod
    def _to_posting(row: sqlite3.Row) -> Posting:
        return Posting(
            title=row["title"],
            company=row["company"],
            url=row["url"],
            ats=row["ats"],
            tenant=row["tenant"],
            location=row["location"],
            description=row["description"],
            desc_available=bool(row["desc_available"]),
            req_id=row["req_id"],
            posted_at=datetime.fromisoformat(row["posted_at"]) if row["posted_at"] else None,
            remote=None if row["remote"] is None else bool(row["remote"]),
            employment_type=row["employment_type"],
            # Read here as well as written because the DynamoDB adapter round-trips
            # it, and a field one implementation of a port keeps while the other
            # silently drops is the exact class of divergence the port exists to
            # rule out — it would surface as the same posting gating differently
            # on the laptop and in Lambda.
            experience_level=row["experience_level"],
        )

    def new_since(self, since: datetime) -> list[Posting]:
        rows = self._conn.execute(
            "SELECT * FROM postings WHERE first_seen > ? AND closed_at IS NULL "
            "ORDER BY first_seen DESC",
            (_iso(since),),
        ).fetchall()
        return [self._to_posting(row) for row in rows]

    def open_postings(self) -> list[Posting]:
        rows = self._conn.execute(
            "SELECT * FROM postings WHERE closed_at IS NULL ORDER BY posted_at DESC"
        ).fetchall()
        return [self._to_posting(row) for row in rows]

    def postings_by_id(self, posting_ids: Sequence[str]) -> dict[str, Posting]:
        """Hydrate one page. Closed postings are included on purpose.

        ``/excluded`` and ``POST /applied`` both address postings the worklist no
        longer lists, and a hydrate that quietly filtered on ``closed_at`` would
        turn "this role closed" into "no such posting" — a 404 that reads as a bug
        in the page rather than as news about the job.
        """
        if not posting_ids:
            return {}
        found: dict[str, Posting] = {}
        with closing(self._conn.cursor()) as cursor:
            for start in range(0, len(posting_ids), 500):  # SQLite's variable cap
                chunk = posting_ids[start : start + 500]
                placeholders = ",".join("?" * len(chunk))
                cursor.execute(
                    f"SELECT * FROM postings WHERE id IN ({placeholders})", chunk
                )
                for row in cursor.fetchall():
                    found[row["id"]] = self._to_posting(row)
        return found

    def cached_interpretation(self, posting_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT interpretation FROM postings WHERE id = ?", (posting_id,)
        ).fetchone()
        if row is None or row["interpretation"] is None:
            return None
        loaded: dict[str, Any] = json.loads(row["interpretation"])
        return loaded

    def uncached_ids(self, posting_ids: Sequence[str]) -> list[str]:
        """Which of these still need an LLM call. Drives the batch."""
        if not posting_ids:
            return []
        cached: set[str] = set()
        with closing(self._conn.cursor()) as cursor:
            for start in range(0, len(posting_ids), 500):
                chunk = posting_ids[start : start + 500]
                placeholders = ",".join("?" * len(chunk))
                cursor.execute(
                    f"SELECT id FROM postings WHERE interpretation IS NOT NULL "
                    f"AND id IN ({placeholders})",
                    chunk,
                )
                cached.update(row["id"] for row in cursor.fetchall())
        return [pid for pid in posting_ids if pid not in cached]

    def stats(self) -> dict[str, int]:
        row = self._conn.execute(
            """
            SELECT COUNT(*) AS total,
                   SUM(closed_at IS NULL) AS open,
                   SUM(closed_at IS NOT NULL) AS closed,
                   SUM(interpretation IS NOT NULL) AS interpreted,
                   SUM(applied_at IS NOT NULL) AS applied
            FROM postings
            """
        ).fetchone()
        # `.keys()` is required here: iterating a sqlite3.Row yields *values*,
        # not keys, so SIM118's suggestion would silently change behaviour.
        return {key: int(row[key] or 0) for key in row.keys()}  # noqa: SIM118
