"""DynamoDB implementation of :class:`~copilot.ports.postingstore.PostingStorePort`.

The SQLite adapter is the right tool on a laptop; it is the wrong tool in Lambda,
where the filesystem is ephemeral and two concurrent invocations would fight over
one file. This adapter is the deployed half of the same port, and it must behave
*identically* — the equivalence is tested, not assumed.

Key schema (single table, one item per posting)::

    pk                  sk      the posting itself
    POSTING#<id>        META    <id> = Posting.id = sha1(url)[:16]

``sk`` is a constant rather than part of the identity so a posting can later grow
sub-items (say ``INTERPRETATION#<model>``) without a migration. The attribute
names match the v1 briefing table (``pk``/``sk``), so this can share that table —
but a separate table is recommended: 25k posting items with a projected index
have nothing in common with a handful of briefings, and separating them keeps
capacity, backups and index rebuilds independent.

Three attributes carry the product logic, exactly as in SQLite:

* ``first_seen`` — what "new today" means. Written once, never updated.
* ``last_seen``  — bumped on every fetch that still contains the posting.
* ``closed_at``  — set when a posting stops appearing.

Four indexes, one named access pattern each. No query in this module is a Scan.

======================  ====================================  ==============================
index                   key                                   serves
======================  ====================================  ==============================
``open-index``          ``open_pk`` = ``OPEN#<shard>``         :meth:`new_since` (range query
(INCLUDE, see           ``open_sk`` = ``<first_seen>#<id>``    on ``open_sk``) and
:data:`OPEN_INDEX_                                            :meth:`open_postings` (whole
PROJECTION`)                                                  partition, full items)
``seen-index``          ``seen_pk`` = ``SEEN#<shard>``         :meth:`sync`'s known/new probe,
(KEYS_ONLY)             ``seen_sk`` =                          :meth:`close_missing`'s open-id
                        ``<OPEN|CLOSED>#<first_seen>#<id>``    enumeration, ``stats`` totals
``cache-index``         ``cache_pk`` = ``CACHE#<shard>``       :meth:`uncached_ids` and the
(KEYS_ONLY, sparse)     ``cache_sk`` = ``<id>``                ``interpreted`` count
``applied-index``       ``applied_pk`` = ``APPLIED``           ``applied`` count, and "what
(KEYS_ONLY, sparse)     ``applied_sk`` = ``<applied_at>#<id>`` did I apply to, by date"
======================  ====================================  ==============================

**Why shard on the id instead of bucketing by date.** The warning against a
single hot partition is real: a lone ``OPEN`` partition would take all 25,294
writes of a sync, and at ~3 WCU per item that is ~75k WCU landing on one
partition inside ~30s — far past the 1,000 WCU/s per-partition ceiling, so the
run throttles no matter how much on-demand capacity the table has. Date bucketing
(``OPEN#<YYYY-MM-DD>``) fixes the *read* fan-out but not that write concentration,
because on a first run every item shares today's date, and it forces the reader to
either enumerate every date since launch (``open_postings`` spans all history) or
keep a registry of which buckets exist. Sharding on the first hex character of the
id is a better fit for this data: ``Posting.id`` is already a sha1, so 16 shards
are uniform by construction, the fan-out is a fixed 16 queries (no registry, no
dependence on the wall clock), and both ``new_since`` and ``open_postings`` are
served by one index instead of two. ``applied-index`` deliberately keeps a single
partition — it is written only when a human applies, so it cannot get hot.

Both "is it open" indexes are **sparse**: closing a posting removes ``open_pk``/
``open_sk`` instead of setting a flag, so "still open" costs nothing to filter —
the closed rows are simply not in the index.

The materialised screening view — and why it needs **no fifth index**
---------------------------------------------------------------------

The read path needs four access patterns (see :mod:`copilot.ports.postingstore`):
a recency page of the kept set, a recency page of the internships set, a per-gate
page of the excluded set, and the summary. None of the four indexes above can
serve them: they are all keyed on ``first_seen`` or on presence, and none knows a
screening verdict.

A fifth GSI would be the obvious move and is the wrong one. A GSI gives exactly
**one** entry per item, and a posting belongs to several views at once — it fails
the seniority gate *and* the citizenship gate — so one index over the posting
items cannot express the membership at all. The view is therefore stored as its
own item collection in the **base table**, which also means these 44k daily writes
carry no index write amplification::

    pk                                     sk           holds
    SCREENVIEW#<gen>#<view>#<shard>        <sort_key>   one (posting, view) row
    SCREEN#SUMMARY                         CURRENT      the published funnel

``<sort_key>`` is ``<posted_at>#<id>``, so a recency page is a plain descending
range query. ``<gen>`` is in the partition key, which is what makes publishing
atomic: a new pass writes into partitions nothing reads until the summary names
that generation. There are no deletes and no diffing — a posting that moves from
"excluded" to "kept" simply does not exist in the new generation's old view.

``<shard>`` is the leading hex digit of the posting id, for the same reason the
posting items are sharded and with a sharper edge here: the ``not_a_software_role``
view holds 87% of the corpus (22,074 of 25,294 measured locally, so ~41,500 of the
deployed 47,538), and a single partition key accepts at most 1,000 WCU/s no matter
how much on-demand capacity the table has — DynamoDB splits a hot partition by key
*range*, never a single key. Unsharded, that one view would spend ~42 s of wall
clock on its own. 16 shards bring it under 3 s, and cost the reader a fixed
16-query scatter-gather per page (a page of 25 reads ≤ 416 rows and returns 25).

**Stale generations are reaped by TTL, not by DeleteItem.** Deletes cost the same
as writes, and ~85k of them a day is real money to remove rows no reader can name.
Every row carries ``expires_at`` (epoch seconds), and the table must have TTL
enabled on that attribute. If it is not enabled nothing breaks — old generations
are unreadable either way — it just accrues ~36 MB of storage per stale day, which
is under a cent a month.

**What a run costs, measured.** Screening the real 25,294-posting corpus produces
45,158 rows (1.79 per posting) whose items are **424 bytes on the mean, 679 at the
maximum** — so every row is comfortably inside the 1 KB WCU boundary and bills as
exactly 1 WCU. Extrapolated to the deployed 47,538: ~84,900 rows, ~84,900 WCU,
**$0.106 per run** on-demand at $1.25/million. That is the dominant *new* cost in
this system, and it sits alongside the corpus sync's own measured ~286,000 WCU
(items average 5,643 bytes, so ~6 WCU each; $0.358/run) — so the view adds ~30% to
the daily write bill and removes an endpoint that was failing 100% of requests.

The rows deliberately do not carry ``description``. It is the bulk of a posting
item — mean 5.6 KB, 25 KB worst case, 268 MB across the deployed corpus — so
copying it here would take each row from 1 WCU to ~6 and multiply that by the 1.79
views a posting sits in, for data the page hydrates from the posting item anyway.

They *do* carry ``first_seen``, at ~36 bytes: a 25-character stamp plus its name, which
leaves a 424-byte row inside the same 1 KB WCU and so adds nothing to the $0.106 a
publish costs. The alternative was reading it per page at request time, which on this
adapter means re-fetching the very items the hydrate is already fetching.
"""
from __future__ import annotations

import json
from collections.abc import Iterable, Iterator, Mapping, Sequence
from datetime import UTC, datetime
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
    summary_from_payload,
    summary_to_payload,
)

_LOG = get_logger("copilot.adapters.dynamodb_posting_store")

_SK = "META"
#: 16 shards, one per leading hex digit of the (sha1) posting id.
_SHARDS = tuple("0123456789abcdef")

#: Partition key of the single published summary. A fixed key so reading it is one
#: GetItem — the cheapest possible "is there a view, and which one".
_SUMMARY_PK = "SCREEN#SUMMARY"
_SUMMARY_SK = "CURRENT"
_VIEW_PK_PREFIX = "SCREENVIEW"

#: How long a screening row survives its generation. Three days, so two failed
#: crons in a row still leave the last good view readable, and long enough that
#: DynamoDB's TTL sweep (best-effort, typically within 48 h) is never the thing
#: that removes a *current* row.
VIEW_TTL_SECONDS = 3 * 24 * 3600

OPEN_INDEX = "open-index"
SEEN_INDEX = "seen-index"
CACHE_INDEX = "cache-index"
APPLIED_INDEX = "applied-index"

#: Attributes ``open-index`` must project, i.e. exactly what :func:`_to_posting`
#: reads. INCLUDE rather than ALL on purpose: ``interpretation`` is an LLM JSON
#: blob and ``open_postings`` never needs it, so projecting it would double the
#: write cost of :meth:`save_interpretation` for nothing.
OPEN_INDEX_PROJECTION: tuple[str, ...] = (
    "id",
    "url",
    "title",
    "company",
    "ats",
    "tenant",
    "location",
    "description",
    "desc_available",
    "req_id",
    "posted_at",
    "remote",
    "employment_type",
    "experience_level",
    "first_seen",
)

#: DynamoDB rejects any item over 400 KB. We refuse a little earlier because
#: :func:`item_size_bytes` is an estimate, and because the GSI copy of an item is
#: measured separately against the same ceiling.
MAX_ITEM_BYTES = 380 * 1024


class PostingTooLargeError(ValueError):
    """A posting will not fit in one DynamoDB item.

    Raised *before* anything is written, so a fetch either lands whole or not at
    all. The largest real description measured across Greenhouse, Ashby, Lever,
    Workable and Workday is ~25 KB, so tripping a 380 KB ceiling almost certainly
    means a parser captured a whole rendered page (nav, footer, embedded JSON)
    instead of a description — fix the parser first. If a source genuinely does
    ship a book, put the description in S3 and store an ``description_s3_key``
    pointer on the item; the rest of this schema does not care where the text
    lives.
    """


def _stamp(value: datetime) -> str:
    """UTC-normalised ISO-8601.

    Timestamps end up inside sort keys, and DynamoDB compares sort keys as bytes.
    ``2026-07-01T09:00:00+05:00`` sorts *after* ``2026-07-01T10:00:00+00:00`` as a
    string while being the earlier instant, so mixing offsets would quietly
    corrupt the ``new_since`` range query. Naive input is read as UTC: the
    pipeline always passes tz-aware UTC, this is a guard rather than a feature.
    """
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


def _value_size(value: Any) -> int:
    """Rough DynamoDB-attribute byte count (see :func:`item_size_bytes`)."""
    if isinstance(value, bool) or value is None:
        return 1
    if isinstance(value, str):
        return len(value.encode())
    if isinstance(value, int | float):
        return len(str(value)) // 2 + 1
    if isinstance(value, list):
        return 3 + sum(_value_size(v) + 1 for v in value)
    if isinstance(value, Mapping):
        return 3 + sum(len(str(k).encode()) + _value_size(v) + 2 for k, v in value.items())
    return len(str(value).encode())


def item_size_bytes(item: Mapping[str, Any]) -> int:
    """Estimate the stored size of an item the way DynamoDB bills it.

    Attribute *names* count towards the limit too, which is easy to forget and is
    why the index key attributes are short. This is an estimate (the wire format
    adds a little per type), so it is compared against a ceiling below 400 KB.
    """
    return sum(len(name.encode()) + _value_size(value) for name, value in item.items())


def _epoch_seconds(value: datetime) -> int:
    """Epoch seconds, reading a naive input as UTC.

    TTL is the one field where a wrong timezone is silently destructive: a naive
    stamp read as local time can put ``expires_at`` hours off, and DynamoDB will not
    complain about either an early reap or a row that outlives its generation.
    """
    aware = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return int(aware.timestamp())


def _shard_of(posting_id: str) -> str:
    """Which of the 16 write shards a posting belongs to.

    The leading hex digit of a sha1 id, so the shards are uniform by construction —
    no registry, no dependence on the wall clock, and identical for the posting
    items and their screening rows.
    """
    return posting_id[0] if posting_id else "0"


def _to_screened_row(item: Mapping[str, Any]) -> ScreenedRow:
    """Rebuild a :class:`ScreenedRow` from a stored item.

    ``posted_at`` is read back out of the **sort key** rather than stored twice.
    The key already has to hold it byte-comparably, and a second copy is a second
    thing to disagree with the ordering the reader is paging on — see
    :func:`~copilot.ports.postingstore.posted_at_from_sort_key`, which the SQLite
    adapter uses for exactly the same reason.
    """
    return ScreenedRow(
        posting_id=str(item["posting_id"]),
        view=str(item["view"]),
        posted_at=posted_at_from_sort_key(str(item["sk"])),
        kept=bool(item["kept"]),
        level=str(item["level"]),
        level_source=str(item["level_source"]),
        level_why=str(item.get("level_why", "")),
        eligibility_checked=bool(item["eligibility_checked"]),
        sponsorship=str(item["sponsorship"]),
        gate=str(item.get("gate", "")),
        reason=str(item.get("reason", "")),
        quote=str(item.get("quote", "")),
        # ``.get`` and not ``[]``: rows published before this attribute existed simply
        # do not have it, and DynamoDB stores no nulls for absent attributes. Missing
        # decodes to ``None`` — "we did not record it" — which is the honest answer for
        # yesterday's view and is replaced by the next cron pass.
        first_seen=first_seen_from_stamp(item.get("first_seen")),
    )


def _to_posting(item: Mapping[str, Any]) -> Posting:
    """Rebuild a :class:`Posting` from a stored item.

    Required fields are read with ``[]`` on purpose: if an index projection is
    missing one of them this raises immediately instead of silently handing the
    scorer a posting with an empty description.
    """
    posted_at = item.get("posted_at")
    remote = item.get("remote")
    return Posting(
        title=str(item["title"]),
        company=str(item["company"]),
        url=str(item["url"]),
        ats=str(item["ats"]),
        tenant=str(item.get("tenant", "")),
        location=str(item.get("location", "")),
        description=str(item["description"]),
        desc_available=bool(item["desc_available"]),
        req_id=str(item.get("req_id", "")),
        posted_at=datetime.fromisoformat(str(posted_at)) if posted_at else None,
        remote=None if remote is None else bool(remote),
        employment_type=str(item.get("employment_type", "")),
        experience_level=str(item.get("experience_level", "")),
    )


class DynamoDbPostingStore:
    """PostingStorePort backed by one DynamoDB table (boto3 imported lazily).

    A ``table`` resource may be injected (tests, or reuse of an existing session);
    otherwise it is built on first use, so importing this module never needs
    boto3 or AWS credentials.
    """

    def __init__(
        self,
        table_name: str,
        *,
        region: str = "us-east-1",
        table: Any | None = None,
    ) -> None:
        self._table_name = table_name
        self._region = region
        self._table: Any | None = table

    @property
    def table(self) -> Any:
        if self._table is None:
            import boto3
            from botocore.config import Config

            # Adaptive retries are the brake on a 25k first run, and they are not
            # the default. BatchWriteItem reports partial throttling as
            # ``UnprocessedItems`` on an HTTP *200*, and boto3's BatchWriter puts
            # those straight back in the buffer and re-sends them with no delay of
            # its own (see ``boto3/dynamodb/table.py``). Adaptive mode adds the
            # client-side rate limiter that turns that into backoff instead of a
            # tight loop against a table whose on-demand capacity is still ramping.
            config = Config(retries={"mode": "adaptive", "max_attempts": 10})
            self._table = boto3.resource(
                "dynamodb", region_name=self._region, config=config
            ).Table(self._table_name)
        return self._table

    def close(self) -> None:
        """No-op, present so callers can treat both stores the same way.

        The SQLite adapter has a file handle to release; boto3 pools its HTTPS
        connections on the session and Lambda wants them kept warm.
        """

    # --- item mapping ---------------------------------------------------------

    def _item(self, posting: Posting, *, first_seen: str, last_seen: str) -> dict[str, Any]:
        shard = _shard_of(posting.id)
        item: dict[str, Any] = {
            "pk": f"POSTING#{posting.id}",
            "sk": _SK,
            "id": posting.id,
            "url": posting.url,
            "title": posting.title,
            "company": posting.company,
            "ats": posting.ats,
            "tenant": posting.tenant,
            "location": posting.location,
            "description": posting.description,
            "desc_available": posting.desc_available,
            "req_id": posting.req_id,
            "employment_type": posting.employment_type,
            "experience_level": posting.experience_level,
            "first_seen": first_seen,
            "last_seen": last_seen,
            "open_pk": f"OPEN#{shard}",
            "open_sk": f"{first_seen}#{posting.id}",
            "seen_pk": f"SEEN#{shard}",
            "seen_sk": f"OPEN#{first_seen}#{posting.id}",
        }
        if posting.posted_at is not None:
            item["posted_at"] = _stamp(posting.posted_at)
        if posting.remote is not None:
            item["remote"] = posting.remote
        return item

    # --- query plumbing -------------------------------------------------------

    def _pages(self, **kwargs: Any) -> Iterator[dict[str, Any]]:
        """Query, following ``LastEvaluatedKey``.

        A query stops at 1 MB whether or not you asked it to, so anything that
        forgets to page silently truncates. 25k open postings are ~100 MB; this
        loop is the difference between "all open roles" and "the first few
        hundred".
        """
        start_key: dict[str, Any] | None = None
        while True:
            if start_key is not None:
                kwargs["ExclusiveStartKey"] = start_key
            response = self.table.query(**kwargs)
            yield from response.get("Items", [])
            start_key = response.get("LastEvaluatedKey")
            if not start_key:
                return

    def _count(self, **kwargs: Any) -> int:
        kwargs["Select"] = "COUNT"
        start_key: dict[str, Any] | None = None
        total = 0
        while True:
            if start_key is not None:
                kwargs["ExclusiveStartKey"] = start_key
            response = self.table.query(**kwargs)
            total += int(response.get("Count", 0))
            start_key = response.get("LastEvaluatedKey")
            if not start_key:
                return total

    @staticmethod
    def _shard_query(
        index: str | None,
        pk_attr: str,
        pk_value: str,
        *,
        sk_attr: str | None = None,
        sk_condition: str | None = None,
        sk_value: str | None = None,
        scan_forward: bool = True,
    ) -> dict[str, Any]:
        """Build query kwargs. ``index=None`` queries the base table.

        Every attribute is referenced through an ``#alias``: DynamoDB's reserved
        word list is long and undocumented at the call site, and a name collision
        surfaces as a ValidationException at runtime, in production, once.
        ``pk``/``sk`` are aliased for the same reason even though they are not
        reserved words — one code path, so the screening view cannot be the one
        query that forgot.
        """
        names = {"#pk": pk_attr}
        values: dict[str, Any] = {":pk": pk_value}
        expression = "#pk = :pk"
        if sk_attr is not None and sk_condition is not None:
            names["#sk"] = sk_attr
            values[":sk"] = sk_value
            expression += f" AND {sk_condition}"
        kwargs: dict[str, Any] = {
            "KeyConditionExpression": expression,
            "ExpressionAttributeNames": names,
            "ExpressionAttributeValues": values,
        }
        if index is not None:
            kwargs["IndexName"] = index
        if not scan_forward:
            kwargs["ScanIndexForward"] = False
        return kwargs

    # --- writes ---------------------------------------------------------------

    def sync(self, postings: list[Posting], *, now: datetime) -> tuple[list[str], list[str]]:
        """Upsert a fetch, preserving ``first_seen`` and clearing any prior close.

        Two write paths, because DynamoDB has no ``INSERT ... ON CONFLICT``:

        * ids we have never seen are **put** through ``batch_writer`` (25 items
          per request, which is what makes a 25k first run finish in seconds);
        * ids we already hold are **updated** conditionally, because a blind put
          would erase ``first_seen``, the cached interpretation, ``applied_at``,
          and — the bug that motivated this — a real description, whenever the
          source that won the race returns none.

        Which ids we already hold comes from one pass over ``seen-index``
        (16 KEYS_ONLY queries, ~5 MB for 25k postings) rather than 25k GetItems.
        That index's sort key carries ``first_seen``, so the update path can
        rebuild the open-index key for a reopened posting without reading the
        item first.
        """
        if not postings:
            _LOG.info("posting_sync", extra={"extra_fields": {"new": 0, "seen_again": 0}})
            return [], []

        stamp = _stamp(now)
        known = self._known_first_seen()
        new_ids = [p.id for p in postings if p.id not in known]
        seen_ids = [p.id for p in postings if p.id in known]

        # Size-check everything *before* writing anything: batch_writer flushes
        # buffered items even when the block exits by exception, so validating
        # inline would leave a half-applied fetch behind.
        for posting in postings:
            self._check_size(posting, first_seen=known.get(posting.id, stamp), last_seen=stamp)

        fresh = [p for p in postings if p.id not in known]
        if fresh:
            # overwrite_by_pkeys is not an optimisation: two postings in one
            # fetch can share a URL (a company re-lists the same requisition),
            # and duplicate keys inside a single BatchWriteItem request are a
            # ValidationException that fails the whole run.
            #
            # Unprocessed items are handled by the writer itself: partial
            # throttling comes back as ``UnprocessedItems`` on a 200, BatchWriter
            # re-queues them, and leaving the ``with`` block drains the buffer
            # until it is empty. Nothing here may report success before that exit.
            with self.table.batch_writer(overwrite_by_pkeys=["pk", "sk"]) as batch:
                for posting in fresh:
                    batch.put_item(Item=self._item(posting, first_seen=stamp, last_seen=stamp))

        for posting in postings:
            first_seen = known.get(posting.id)
            if first_seen is not None:
                self._update_seen(posting, first_seen=first_seen, now=stamp)

        _LOG.info(
            "posting_sync",
            extra={"extra_fields": {"new": len(new_ids), "seen_again": len(seen_ids)}},
        )
        return new_ids, seen_ids

    def _check_size(self, posting: Posting, *, first_seen: str, last_seen: str) -> None:
        item = self._item(posting, first_seen=first_seen, last_seen=last_seen)
        size = item_size_bytes(item)
        if size > MAX_ITEM_BYTES:
            raise PostingTooLargeError(
                f"{posting.id} ({posting.company}: {posting.title}) is ~{size} bytes, "
                f"over the {MAX_ITEM_BYTES} byte ceiling; description is "
                f"{len(posting.description)} chars. See PostingTooLargeError."
            )

    def _update_seen(self, posting: Posting, *, first_seen: str, now: str) -> None:
        """Update a posting we already hold, mirroring the SQLite upsert exactly.

        SQLite's ``ON CONFLICT`` touches only ``last_seen``, ``closed_at``,
        ``title``, ``location`` and the description pair; url/company/ats/tenant/
        req_id/posted_at are left at their first-seen values. Same here, so the
        two stores cannot drift.
        """
        names = {
            "#pk": "pk",
            "#last_seen": "last_seen",
            "#title": "title",
            "#location": "location",
            "#description": "description",
            "#desc_available": "desc_available",
            "#open_pk": "open_pk",
            "#open_sk": "open_sk",
            "#seen_sk": "seen_sk",
            "#closed_at": "closed_at",
        }
        values: dict[str, Any] = {
            ":last_seen": now,
            ":title": posting.title,
            ":location": posting.location,
            ":open_pk": f"OPEN#{_shard_of(posting.id)}",
            ":open_sk": f"{first_seen}#{posting.id}",
            ":seen_sk": f"OPEN#{first_seen}#{posting.id}",
        }
        sets = [
            "#last_seen = :last_seen",
            "#title = :title",
            "#location = :location",
            # Re-asserting the index keys is what reopens a posting that had been
            # closed: the sparse open-index entry comes back with its *original*
            # first_seen, so a returning role is not reported as new today.
            "#open_pk = :open_pk",
            "#open_sk = :open_sk",
            "#seen_sk = :seen_sk",
        ]
        if posting.desc_available:
            sets += ["#description = :description", "#desc_available = :desc_available"]
            values[":description"] = posting.description
            values[":desc_available"] = True
        else:
            # Workday's list endpoint returns no description for roles Greenhouse
            # describes in full. if_not_exists is the whole guard: an empty
            # description may fill a gap, never overwrite text we already have.
            sets += [
                "#description = if_not_exists(#description, :empty)",
                "#desc_available = if_not_exists(#desc_available, :false)",
            ]
            values[":empty"] = ""
            values[":false"] = False

        try:
            self.table.update_item(
                Key={"pk": f"POSTING#{posting.id}", "sk": _SK},
                UpdateExpression="SET " + ", ".join(sets) + " REMOVE #closed_at",
                ConditionExpression="attribute_exists(#pk)",
                ExpressionAttributeNames=names,
                ExpressionAttributeValues=values,
            )
        except Exception as exc:
            if not _is_conditional_failure(exc):
                raise
            # The probe is a read; the item can be gone by the time we write (a
            # TTL, a manual delete, a concurrent run). A partial update would
            # create an item with no url or company, which every reader would
            # then choke on, so rewrite it whole instead.
            _LOG.warning(
                "posting_missing_at_update", extra={"extra_fields": {"id": posting.id}}
            )
            self.table.put_item(
                Item=self._item(posting, first_seen=first_seen, last_seen=now)
            )

    def close_missing(self, *, now: datetime, seen_ids: set[str]) -> int:
        """Mark open postings that were not in this fetch as closed.

        DynamoDB cannot express ``WHERE id NOT IN (...)``, so the open set is
        enumerated from ``seen-index`` (KEYS_ONLY, ~150 bytes per posting) and
        diffed in memory. Its sort key already carries ``first_seen``, so no item
        has to be read to rewrite the index keys.
        """
        if not seen_ids:
            # An empty fetch is far more likely to be a broken run than a market
            # where every job closed at once. Refuse to mass-close.
            _LOG.warning("close_missing_skipped_empty_fetch")
            return 0

        stamp = _stamp(now)
        closed = 0
        for posting_id, first_seen in self._open_first_seen().items():
            if posting_id in seen_ids:
                continue
            if self._close_one(posting_id, first_seen=first_seen, now=stamp):
                closed += 1
        return closed

    def _close_one(self, posting_id: str, *, first_seen: str, now: str) -> bool:
        """Close one posting. Returns whether *this* call did it.

        The condition makes the count mean what SQLite's ``rowcount`` means: a
        posting closed by an earlier run is not counted again.
        """
        try:
            self.table.update_item(
                Key={"pk": f"POSTING#{posting_id}", "sk": _SK},
                UpdateExpression="SET #closed_at = :now, #seen_sk = :seen_sk "
                "REMOVE #open_pk, #open_sk",
                ConditionExpression="attribute_exists(#pk) AND attribute_not_exists(#closed_at)",
                ExpressionAttributeNames={
                    "#pk": "pk",
                    "#closed_at": "closed_at",
                    "#seen_sk": "seen_sk",
                    "#open_pk": "open_pk",
                    "#open_sk": "open_sk",
                },
                ExpressionAttributeValues={
                    ":now": now,
                    ":seen_sk": f"CLOSED#{first_seen}#{posting_id}",
                },
            )
        except Exception as exc:
            if not _is_conditional_failure(exc):
                raise
            return False
        return True

    def save_interpretation(self, posting_id: str, payload: dict[str, Any]) -> None:
        """Cache an LLM result, and index the posting as interpreted.

        Stored as a JSON string rather than a DynamoDB map, for two reasons: it
        is byte-identical to what SQLite stores (so the cache round-trips the same
        in both), and it sidesteps DynamoDB's refusal of ``float`` and its
        Decimal-on-read surprise for scores like ``0.85``.
        """
        self._update_or_ignore_missing(
            posting_id,
            update="SET #interpretation = :payload, #cache_pk = :cache_pk, "
            "#cache_sk = :cache_sk",
            names={
                "#pk": "pk",
                "#interpretation": "interpretation",
                "#cache_pk": "cache_pk",
                "#cache_sk": "cache_sk",
            },
            values={
                ":payload": json.dumps(payload, sort_keys=True),
                ":cache_pk": f"CACHE#{_shard_of(posting_id)}",
                ":cache_sk": posting_id,
            },
            condition="attribute_exists(#pk)",
        )

    def mark_applied(self, posting_id: str, *, now: datetime) -> None:
        """Record that a human applied. Idempotent: the first timestamp wins.

        Nothing in this system applies on its own; this only records that the
        human did, which is why re-recording must never move the date.
        """
        stamp = _stamp(now)
        self._update_or_ignore_missing(
            posting_id,
            update="SET #applied_at = :now, #applied_pk = :applied_pk, "
            "#applied_sk = :applied_sk",
            names={
                "#pk": "pk",
                "#applied_at": "applied_at",
                "#applied_pk": "applied_pk",
                "#applied_sk": "applied_sk",
            },
            values={
                ":now": stamp,
                ":applied_pk": "APPLIED",
                ":applied_sk": f"{stamp}#{posting_id}",
            },
            condition="attribute_exists(#pk) AND attribute_not_exists(#applied_at)",
        )

    # --- the materialised screening view --------------------------------------

    def save_screening(self, rows: Iterable[ScreenedRow], *, summary: ScreenSummary) -> None:
        """Write every row under the new generation, then publish the summary.

        The order is the whole guarantee. Rows live under
        ``SCREENVIEW#<generation>#…`` partitions, so until the summary names that
        generation no reader can address them; the final ``put_item`` is the atomic
        swap. A pass that dies part-way therefore leaves orphan rows that TTL will
        reap and the *previous* complete view still current — never a half-written
        view that reads as authoritative. The first live cron crashed after the
        corpus landed and before the run finished, which is exactly this shape.

        ``overwrite_by_pkeys`` is not an optimisation: two postings in one fetch can
        share a URL (a company re-lists a requisition), so they share an id, a
        sort key and therefore an item key — and duplicate keys inside one
        BatchWriteItem request are a ValidationException that fails the whole run.

        Nothing reports success before the ``with`` block exits: partial throttling
        comes back as ``UnprocessedItems`` on an HTTP 200 and BatchWriter re-queues
        it, so leaving the block is what drains the buffer.

        ``first_seen`` is stamped onto each row from :meth:`_open_first_seen`, read once
        before the loop. That is 16 KEYS_ONLY queries — the same read
        :meth:`close_missing` already performs earlier in the same run, ~150 bytes a
        posting — and it is deliberately *not* wrapped in a fallback: if the table cannot
        answer that here it did not answer it minutes ago either, and this run has a
        larger problem than a null on a card. The failure is contained one level up, by
        the caller that keeps yesterday's view current.
        """
        expires_at = _epoch_seconds(summary.screened_at) + VIEW_TTL_SECONDS
        stamps = self._open_first_seen()
        written = 0
        with self.table.batch_writer(overwrite_by_pkeys=["pk", "sk"]) as batch:
            for row in rows:
                batch.put_item(
                    Item=self._row_item(
                        row,
                        generation=summary.generation,
                        expires=expires_at,
                        first_seen=first_seen_stamp(stamps.get(row.posting_id)),
                    )
                )
                written += 1
        self.table.put_item(
            Item={
                "pk": _SUMMARY_PK,
                "sk": _SUMMARY_SK,
                "generation": summary.generation,
                "screened_at": _stamp(summary.screened_at),
                # One JSON string rather than 11 attributes, byte-identical to what
                # SQLite stores. Same reasoning as the interpretation cache: the two
                # stores round-trip the summary the same way, and a JSON string
                # cannot hand a reader a Decimal where it wrote an int.
                "summary": json.dumps(summary_to_payload(summary), sort_keys=True),
            }
        )
        _LOG.info(
            "screening_view_published",
            extra={"extra_fields": {
                "generation": summary.generation,
                "rows": written,
                "kept": summary.kept,
                "screened": summary.screened,
            }},
        )

    def _row_item(
        self,
        row: ScreenedRow,
        *,
        generation: str,
        expires: int,
        first_seen: str | None = None,
    ) -> dict[str, Any]:
        """One view row as an item. ``first_seen`` comes from the store, not from ``row``.

        Omitted rather than written as ``None`` when it is unknown, the way ``posted_at``
        and ``remote`` already are: DynamoDB bills attribute *names* too, and an absent
        attribute and a null one decode identically here while only one of them costs
        ~10 bytes on ~84,900 rows a day. With the stamp present a row measures ~460 bytes
        against a mean of 424, so it stays inside the 1 KB WCU boundary and the publish
        still bills exactly one WCU per row.
        """
        item: dict[str, Any] = {
            "pk": f"{_VIEW_PK_PREFIX}#{generation}#{row.view}#{_shard_of(row.posting_id)}",
            "sk": row.sort_key,
            "posting_id": row.posting_id,
            "view": row.view,
            "kept": row.kept,
            "level": row.level,
            "level_source": row.level_source,
            "level_why": row.level_why,
            "eligibility_checked": row.eligibility_checked,
            "sponsorship": row.sponsorship,
            "gate": row.gate,
            "reason": row.reason,
            "quote": row.quote,
            # Epoch seconds, which is the only format DynamoDB TTL reads. An ISO
            # string here would be silently ignored and the rows would live forever.
            "expires_at": expires,
        }
        if first_seen is not None:
            item["first_seen"] = first_seen
        return item

    def screening_summary(self) -> ScreenSummary | None:
        """The published funnel, or ``None``. One GetItem, ~5 ms, always.

        A missing item, an unreadable payload and a payload from an older
        ``VIEW_VERSION`` all answer ``None``: a reader's only two honest answers are
        "here is the view" and "the corpus has not been screened yet". Raising here
        would turn a stale deploy into a 500 on the public page, which is as opaque
        as the 504 this replaced.
        """
        response = self.table.get_item(Key={"pk": _SUMMARY_PK, "sk": _SUMMARY_SK})
        raw = response.get("Item", {}).get("summary")
        if raw is None:
            return None
        try:
            payload = json.loads(str(raw))
        except ValueError:
            _LOG.warning("screening_summary_unparsable")
            return None
        if not isinstance(payload, Mapping):
            return None
        return summary_from_payload(payload)

    def screened_page(
        self, view: str, *, generation: str, limit: int, after: str | None = None
    ) -> ScreenedPage:
        """One recency page of one view: 16 shard queries, merged, top ``limit``.

        Each shard is asked for ``limit + 1`` rows below the cursor, so the merged
        pool holds more than ``limit`` rows exactly when a next page exists. That
        equivalence is why ``hasMore`` needs no COUNT query — and a count taken
        separately could describe a different generation than the page beside it.

        Asking each shard for the page size is what keeps this O(page): the
        ``not_a_software_role`` view holds ~41,500 rows and a page of 25 reads at
        most 416 of them, measured at 16 queries flat regardless of view size.
        """
        if view not in SCREEN_VIEWS:
            raise ValueError(f"unknown screening view {view!r}")
        pool: list[dict[str, Any]] = []
        for shard in _SHARDS:
            kwargs = self._shard_query(
                None,
                "pk",
                f"{_VIEW_PK_PREFIX}#{generation}#{view}#{shard}",
                sk_attr="sk" if after is not None else None,
                sk_condition="#sk < :sk" if after is not None else None,
                sk_value=after,
                scan_forward=False,
            )
            pool.extend(self._limited_pages(want=limit + 1, **kwargs))
        pool.sort(key=lambda item: str(item["sk"]), reverse=True)
        rows = tuple(_to_screened_row(item) for item in pool[:limit])
        next_token = rows[-1].sort_key if len(pool) > limit and rows else None
        return ScreenedPage(rows=rows, next_token=next_token)

    def _limited_pages(self, *, want: int, **kwargs: Any) -> list[dict[str, Any]]:
        """Query until ``want`` items are in hand, then stop.

        The other query helper in this module pages to exhaustion, because
        truncating it would silently lose postings. This one must *not*: the
        ``not_a_software_role`` view holds ~41,500 rows and paging it to serve a page
        of 25 is the cost this whole change removes.

        ``Limit`` alone would be the obvious implementation and would be subtly
        wrong. DynamoDB cuts a query at 1 MB **before** applying ``Limit``, so a
        single query is only guaranteed to return ``Limit`` items while the rows stay
        small. They are 424 bytes on the mean and 679 at the measured maximum, so one
        query always suffices today — but a future field that pushed a row to 40 KB
        would silently start under-filling pages, and a short page reads as "that is
        all there is". Looping until the count is met costs nothing when one page
        suffices and stays correct when it does not.
        """
        kwargs["Limit"] = want
        items: list[dict[str, Any]] = []
        start_key: dict[str, Any] | None = None
        while len(items) < want:
            if start_key is not None:
                kwargs["ExclusiveStartKey"] = start_key
            response = self.table.query(**kwargs)
            items.extend(response.get("Items", []))
            start_key = response.get("LastEvaluatedKey")
            if not start_key:
                break
        return items[:want]

    def _update_or_ignore_missing(
        self,
        posting_id: str,
        *,
        update: str,
        names: dict[str, str],
        values: dict[str, Any],
        condition: str,
    ) -> None:
        """Conditional update whose failure is a no-op, not an error.

        ``attribute_exists(pk)`` is doing real work: an unconditional
        ``update_item`` on an unknown id *creates* an item, so a stale id from a
        caller would leave a phantom row with an ``applied_at`` and nothing else.
        SQLite's ``UPDATE ... WHERE id = ?`` simply matches no rows; this is how
        that is spelled here.
        """
        try:
            self.table.update_item(
                Key={"pk": f"POSTING#{posting_id}", "sk": _SK},
                UpdateExpression=update,
                ConditionExpression=condition,
                ExpressionAttributeNames=names,
                ExpressionAttributeValues=values,
            )
        except Exception as exc:
            if not _is_conditional_failure(exc):
                raise

    # --- reads ----------------------------------------------------------------

    def _known_first_seen(self) -> dict[str, str]:
        """``id -> first_seen`` for every posting, open or closed.

        Closed postings must be in here: a role that disappears for a week and
        comes back is *not* new, and must keep its original ``first_seen`` and its
        cached interpretation.
        """
        return self._seen_index_map()

    def _open_first_seen(self) -> dict[str, str]:
        """``id -> first_seen`` for the open corpus. 16 KEYS_ONLY queries, no item reads.

        Two callers, both in the cron: :meth:`close_missing` diffs it against the fetch,
        and :meth:`save_screening` stamps it onto the view rows it publishes. It is
        ``O(corpus)`` and must stay out of every handler — the point of the view is that a
        request never pays a corpus-sized read.
        """
        return self._seen_index_map(state="OPEN")

    def _seen_index_map(self, *, state: str | None = None) -> dict[str, str]:
        found: dict[str, str] = {}
        for shard in _SHARDS:
            kwargs = self._shard_query(
                SEEN_INDEX,
                "seen_pk",
                f"SEEN#{shard}",
                sk_attr="seen_sk" if state else None,
                sk_condition="begins_with(#sk, :sk)" if state else None,
                sk_value=f"{state}#" if state else None,
            )
            for item in self._pages(**kwargs):
                try:
                    _state, first_seen, posting_id = str(item["seen_sk"]).split("#")
                except ValueError:  # pragma: no cover - defensive
                    _LOG.warning(
                        "unparsable_seen_sk",
                        extra={"extra_fields": {"seen_sk": str(item.get("seen_sk"))}},
                    )
                    continue
                found[posting_id] = first_seen
        return found

    def new_since(self, since: datetime) -> list[Posting]:
        """Open postings first seen after ``since`` — the "what changed" feed.

        A range query on the open-index sort key, so the cost is proportional to
        the ~3 postings a day that are actually new, not to the 25k that are not.
        The sort key is ``<first_seen>#<id>``, so ``open_sk > since`` also matches
        an item whose ``first_seen`` *equals* ``since`` (its key continues with
        ``#<id>``). SQLite's ``first_seen > ?`` is strict, so the boundary is
        re-checked on ``first_seen`` itself; that comparison is exact because
        :func:`_stamp` writes every timestamp in one UTC format.
        """
        cutoff = _stamp(since)
        items: list[dict[str, Any]] = []
        for shard in _SHARDS:
            kwargs = self._shard_query(
                OPEN_INDEX,
                "open_pk",
                f"OPEN#{shard}",
                sk_attr="open_sk",
                sk_condition="#sk > :sk",
                sk_value=cutoff,
                scan_forward=False,
            )
            items.extend(i for i in self._pages(**kwargs) if str(i["first_seen"]) > cutoff)
        items.sort(key=lambda i: str(i["first_seen"]), reverse=True)
        return [_to_posting(i) for i in items]

    def open_postings(self) -> list[Posting]:
        """Everything not yet marked closed, newest posting date first.

        Ordering is done here rather than by the index: the product order is
        ``posted_at`` (what the company published) while the index is ordered by
        ``first_seen`` (when we noticed). Sorting 25k rows in memory is cheap;
        a second full-projection index for it would not be. Postings with no
        ``posted_at`` sort last, matching SQLite's NULL ordering in ``DESC``.
        """
        items = [
            item
            for shard in _SHARDS
            for item in self._pages(**self._shard_query(OPEN_INDEX, "open_pk", f"OPEN#{shard}"))
        ]
        items.sort(key=lambda i: str(i.get("posted_at") or ""), reverse=True)
        return [_to_posting(i) for i in items]

    def postings_by_id(self, posting_ids: Sequence[str]) -> dict[str, Posting]:
        """Hydrate one page of postings by primary key. Unknown ids are absent.

        A GetItem per id rather than BatchGetItem, deliberately. BatchGetItem lives
        on the *service resource*, not on a Table, so reaching it would mean holding
        a second boto3 object purely to save round trips on a call whose input is
        capped at the API's ``MAX_LIMIT`` of 100 — and the in-memory double that
        proves both stores behave alike would have to grow a second write path. At
        ~5 ms each a default page of 25 is ~125 ms; the thing this replaced was 70 s
        of screening, so the round trips are not where the budget goes.

        Closed postings are returned on purpose: ``/excluded`` and ``POST /applied``
        both address postings the worklist no longer lists, and filtering them here
        would turn "this role closed" into "no such posting".
        """
        found: dict[str, Posting] = {}
        for posting_id in dict.fromkeys(posting_ids):  # de-duped, order preserved
            response = self.table.get_item(Key={"pk": f"POSTING#{posting_id}", "sk": _SK})
            item = response.get("Item")
            if item:
                found[posting_id] = _to_posting(item)
        return found

    def cached_interpretation(self, posting_id: str) -> dict[str, Any] | None:
        """A previously stored LLM result, or ``None``. The main cost lever."""
        response = self.table.get_item(
            Key={"pk": f"POSTING#{posting_id}", "sk": _SK},
            # Projected: this is called once per candidate and the item carries a
            # description we do not want on the wire 880 times.
            ProjectionExpression="#interpretation",
            ExpressionAttributeNames={"#interpretation": "interpretation"},
        )
        raw = response.get("Item", {}).get("interpretation")
        if raw is None:
            return None
        loaded: dict[str, Any] = json.loads(str(raw))
        return loaded

    def uncached_ids(self, posting_ids: Sequence[str]) -> list[str]:
        """Which of these still need an LLM call. Drives the batch.

        Queries only the shards the requested ids fall into, against a sparse
        KEYS_ONLY index — cheaper and far fewer round trips than a GetItem per
        candidate, each of which would bill for the whole description.
        """
        if not posting_ids:
            return []
        cached: set[str] = set()
        for shard in sorted({pid[0] for pid in posting_ids if pid}):
            kwargs = self._shard_query(CACHE_INDEX, "cache_pk", f"CACHE#{shard}")
            cached.update(str(item["cache_sk"]) for item in self._pages(**kwargs))
        return [pid for pid in posting_ids if pid not in cached]

    def stats(self) -> dict[str, int]:
        """Counts for the CLI and the daily log line.

        Every count is an index ``Select=COUNT`` query; there is no Scan here.
        This is a diagnostic, not a hot path — it touches every shard of three
        indexes, which is fine once a run and wrong in a loop.
        """
        total = 0
        open_count = 0
        interpreted = 0
        for shard in _SHARDS:
            total += self._count(**self._shard_query(SEEN_INDEX, "seen_pk", f"SEEN#{shard}"))
            open_count += self._count(
                **self._shard_query(
                    SEEN_INDEX,
                    "seen_pk",
                    f"SEEN#{shard}",
                    sk_attr="seen_sk",
                    sk_condition="begins_with(#sk, :sk)",
                    sk_value="OPEN#",
                )
            )
            interpreted += self._count(
                **self._shard_query(CACHE_INDEX, "cache_pk", f"CACHE#{shard}")
            )
        applied = self._count(**self._shard_query(APPLIED_INDEX, "applied_pk", "APPLIED"))
        return {
            "total": total,
            "open": open_count,
            "closed": total - open_count,
            "interpreted": interpreted,
            "applied": applied,
        }


def _is_conditional_failure(exc: Exception) -> bool:
    """Is this DynamoDB saying "your condition did not hold"?

    Matched by name rather than by ``except ConditionalCheckFailedException``:
    botocore builds that class dynamically per client, so naming it would mean
    importing botocore at module scope and reaching through
    ``table.meta.client.exceptions`` — which defeats the lazy import that keeps
    this package importable without boto3, and makes the adapter untestable
    against an in-memory double.
    """
    if type(exc).__name__ == "ConditionalCheckFailedException":
        return True
    response = getattr(exc, "response", None)
    if isinstance(response, Mapping):
        error = response.get("Error")
        if isinstance(error, Mapping):
            return bool(error.get("Code") == "ConditionalCheckFailedException")
    return False
