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
"""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import closing, contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from copilot.domain.posting import Posting
from copilot.logging import get_logger

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
"""

_COLUMNS = (
    "id, url, title, company, ats, tenant, location, description, desc_available, "
    "req_id, posted_at, remote, employment_type, experience_level"
)

#: Columns added after the first release. ``CREATE TABLE IF NOT EXISTS`` is a no-op
#: on an existing file, so a new column in :data:`_SCHEMA` alone would leave every
#: already-populated database (the developer's ~25k-row ``data/postings.db``) one
#: column short and every read raising ``KeyError``. Applied idempotently at open.
_ADDED_COLUMNS: tuple[tuple[str, str], ...] = (
    ("experience_level", "TEXT NOT NULL DEFAULT ''"),
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
        """
        have = {str(row["name"]) for row in self._conn.execute("PRAGMA table_info(postings)")}
        for name, ddl in _ADDED_COLUMNS:
            if name not in have:
                self._conn.execute(f"ALTER TABLE postings ADD COLUMN {name} {ddl}")

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
