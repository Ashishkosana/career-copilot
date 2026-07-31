"""The v1 briefing store, proven on a key-schema-validating double of the Table API.

The bug this file exists to prevent: the adapter built every item and every key
condition with lowercase ``pk``/``sk`` while the deployed ``career-copilot`` table
is keyed on ``PK``/``SK``, so all four methods failed against the real service
with ``ValidationException: The provided key element does not match the schema``.
The daily cron died on it at the last two statements of the run, and
``GET /briefing`` had never once returned a briefing.

**Why the old suite was green anyway.** Its fake table read ``Item["pk"]`` — it
had been written from the adapter, so the two agreed with each other and both
disagreed with AWS. A double that derives the schema from the code under test can
only ever prove self-consistency. So this file pins the schema in three places
that cannot all be wrong at once:

1. :data:`LIVE_KEY_SCHEMA` is a **literal**, transcribed from
   ``aws dynamodb describe-table``, never imported from the adapter.
2. :class:`FakeTable` validates every write, key and key condition against it and
   raises the message DynamoDB actually raises, so a rename in the adapter fails
   here instead of in Lambda.
3. :func:`test_key_attributes_match_the_deployed_table_declaration` reads the CDK
   stack, so the literal in (1) cannot drift from the table either.

The double also refuses ``boto3.dynamodb.conditions.Key`` objects. That is load
bearing rather than lazy: a ``ConditionBase`` hides its attribute names behind
boto3's builder, so an adapter using them cannot be checked against a schema at
all. String conditions with ``#alias`` names are what make (2) possible.
"""
from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from conftest import make_briefing

from copilot.adapters.dynamodb_store import (
    BRIEFING_PREFIX,
    JOB_PREFIX,
    PARTITION_KEY,
    SORT_KEY,
    DynamoDbStore,
    _from_dynamo,
    _to_dynamo,
    briefing_to_item,
    item_to_briefing,
    item_to_job,
    job_to_item,
)
from copilot.domain.models import Briefing, Job

#: The deployed table's key schema, from::
#:
#:     aws dynamodb describe-table --table-name career-copilot \
#:       --query 'Table.KeySchema'
#:
#: A literal on purpose. Importing ``PARTITION_KEY``/``SORT_KEY`` here would make
#: the fake agree with the adapter by construction, which is exactly the mistake
#: that let the lowercase-key bug ship. Uppercase, unlike the v2 postings table —
#: that difference is the whole bug.
LIVE_KEY_SCHEMA = ("PK", "SK")

#: The CDK stack that creates the table (``RemovalPolicy.RETAIN``, so its schema
#: outlives any deploy). Single source of truth for what AWS holds.
_STACK_FILE = Path(__file__).resolve().parents[2] / "infra" / "lib" / "career-copilot-stack.ts"


# --------------------------------------------------------------------------- #
# The double
# --------------------------------------------------------------------------- #


class ValidationException(Exception):
    """What DynamoDB raises for a request that does not fit the table's schema."""


class _FakeBatchWriter:
    """boto3's ``BatchWriter``, in the two respects the adapter leans on.

    ``overwrite_by_pkeys`` de-duplicates as items are added (so a duplicate key
    never reaches the service), and the flush happens on ``__exit__`` — which is
    why a schema error shows up when the ``with`` block *closes*, and why the
    adapter has to keep the whole block inside its containment.
    """

    def __init__(self, table: FakeTable, overwrite_by_pkeys: list[str] | None) -> None:
        self._table = table
        self._overwrite = overwrite_by_pkeys
        self._buffer: list[dict[str, Any]] = []

    def __enter__(self) -> _FakeBatchWriter:
        return self

    def __exit__(self, *exc: object) -> None:
        self._table.batch_write(self._buffer)
        self._buffer = []

    def put_item(self, *, Item: dict[str, Any]) -> None:
        if self._overwrite is not None:
            keys = [Item[name] for name in self._overwrite]
            self._buffer = [i for i in self._buffer if [i[n] for n in self._overwrite] != keys]
        self._buffer.append(Item)


@dataclass
class FakeTable:
    """In-memory ``boto3.resource('dynamodb').Table(...)`` that enforces a key schema.

    ``page_size`` is in *items* rather than bytes; real DynamoDB cuts a query at
    1 MB, and any value smaller than the result set exercises the same
    ``LastEvaluatedKey`` contract the adapter has to follow.
    """

    key_schema: tuple[str, str] = LIVE_KEY_SCHEMA
    page_size: int = 100
    items: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    queries: list[dict[str, Any]] = field(default_factory=list)
    batch_requests: int = 0

    @property
    def _pk_name(self) -> str:
        return self.key_schema[0]

    @property
    def _sk_name(self) -> str:
        return self.key_schema[1]

    def _key_of(self, item: Mapping[str, Any], *, message: str) -> tuple[str, str]:
        """The item's primary key, or the error DynamoDB gives for the wrong names.

        Extra non-key attributes are fine (a stray lowercase ``pk`` is simply
        stored), which is what makes the real failure a *missing key* rather than
        an unknown attribute.
        """
        for name in self.key_schema:
            value = item.get(name)
            if not isinstance(value, str) or not value:
                raise ValidationException(message)
        return str(item[self._pk_name]), str(item[self._sk_name])

    # --- writes ---

    def put_item(self, *, Item: dict[str, Any]) -> None:
        key = self._key_of(
            Item,
            message="One or more parameter values were invalid: "
            f"Missing the key {self._pk_name} in the item",
        )
        self.items[key] = deepcopy(Item)

    def batch_write(self, batch: list[dict[str, Any]]) -> None:
        if not batch:
            return
        self.batch_requests += 1
        keys = [
            self._key_of(
                item,
                message="BatchWriteItem: The provided key element does not match the schema",
            )
            for item in batch
        ]
        if len(set(keys)) != len(keys):
            raise ValidationException("Provided list of item keys contains duplicates")
        for key, item in zip(keys, batch, strict=True):
            self.items[key] = deepcopy(item)

    def batch_writer(self, overwrite_by_pkeys: list[str] | None = None) -> _FakeBatchWriter:
        return _FakeBatchWriter(self, overwrite_by_pkeys)

    # --- reads ---

    def query(self, **kwargs: Any) -> dict[str, Any]:
        self.queries.append(kwargs)
        condition = kwargs["KeyConditionExpression"]
        if not isinstance(condition, str):
            raise ValidationException(
                "this double only accepts string key conditions: a ConditionBase "
                "hides its attribute names, so its key schema cannot be checked"
            )
        names: dict[str, str] = kwargs.get("ExpressionAttributeNames", {})
        values: dict[str, Any] = kwargs.get("ExpressionAttributeValues", {})
        _check_all_used([condition, kwargs.get("ProjectionExpression", "")], names, values)
        rows = self._matching(condition, names, values)
        rows.sort(key=lambda item: str(item[self._sk_name]))
        if not kwargs.get("ScanIndexForward", True):
            rows.reverse()
        start = int(kwargs.get("ExclusiveStartKey", {}).get("_offset", 0))
        page = rows[start : start + self.page_size]
        limit = kwargs.get("Limit")
        truncated = limit is not None and limit < len(page)
        if limit is not None:
            page = page[:limit]
        response: dict[str, Any] = {
            "Items": [_project(item, kwargs.get("ProjectionExpression"), names) for item in page]
        }
        if not truncated and start + self.page_size < len(rows):
            response["LastEvaluatedKey"] = {"_offset": start + self.page_size}
        return response

    def _matching(
        self, condition: str, names: Mapping[str, str], values: Mapping[str, Any]
    ) -> list[dict[str, Any]]:
        terms = re.split(r"\s+AND\s+", condition.strip())
        equality = re.fullmatch(r"(#?\w+)\s*=\s*(:\w+)", terms[0].strip())
        if equality is None:
            raise ValidationException(f"unsupported key condition: {terms[0]}")
        if _resolve(equality.group(1), names) != self._pk_name:
            raise ValidationException(
                f"Query condition missed key schema element: {self._pk_name}"
            )
        pk_value = _value(equality.group(2), values)
        rows = [
            deepcopy(item) for item in self.items.values() if item[self._pk_name] == pk_value
        ]
        if len(terms) == 1:
            return rows
        begins = re.fullmatch(r"begins_with\((#?\w+),\s*(:\w+)\)", terms[1].strip())
        if begins is None:
            raise ValidationException(f"unsupported sort key condition: {terms[1]}")
        attr = _resolve(begins.group(1), names)
        if attr != self._sk_name:
            raise ValidationException(f"{attr} is not the sort key of this table")
        prefix = str(_value(begins.group(2), values))
        return [row for row in rows if str(row[attr]).startswith(prefix)]


def _resolve(token: str, names: Mapping[str, str]) -> str:
    if token.startswith("#"):
        if token not in names:
            raise ValidationException(f"undefined expression attribute name {token}")
        return names[token]
    return token


def _value(token: str, values: Mapping[str, Any]) -> Any:
    if token not in values:
        raise ValidationException(f"undefined expression attribute value {token}")
    return values[token]


def _check_all_used(
    expressions: list[str], names: Mapping[str, str], values: Mapping[str, Any]
) -> None:
    """DynamoDB rejects unused names/values; so does this, to catch dead aliases."""
    text = " ".join(expressions)
    for token in (*names, *values):
        if not re.search(rf"{re.escape(token)}\b", text):
            raise ValidationException(f"unused expression attribute {token}")


def _project(
    item: dict[str, Any], projection: str | None, names: Mapping[str, str]
) -> dict[str, Any]:
    if projection is None:
        return item
    wanted = [_resolve(token.strip(), names) for token in projection.split(",")]
    return {k: v for k, v in item.items() if k in wanted}


def _store(**kwargs: Any) -> tuple[DynamoDbStore, FakeTable]:
    table = FakeTable(**kwargs)
    return DynamoDbStore("career-copilot", table=table), table


def _job(n: int) -> Job:
    return Job(id=f"j{n}", title="SWE", company="Acme", url=f"https://x/{n}", score=50 + n)


# --------------------------------------------------------------------------- #
# The key schema — the bug
# --------------------------------------------------------------------------- #


def test_items_are_keyed_on_the_live_tables_attribute_names() -> None:
    """``PK``/``SK``, uppercase. Lowercase was a ValidationException on every write."""
    pk_name, sk_name = LIVE_KEY_SCHEMA
    for item in (briefing_to_item("u1", make_briefing()), job_to_item("u1", _job(1))):
        assert item[pk_name] == "USER#u1"
        assert isinstance(item[sk_name], str)
        # The v2 postings table's names must not reappear here by copy-paste.
        assert "pk" not in item
        assert "sk" not in item


def test_key_attributes_match_the_deployed_table_declaration() -> None:
    """Tie the constants to the CDK stack, not to this file's opinion.

    Without this, ``LIVE_KEY_SCHEMA`` is just a second guess: it would stay
    ``PK``/``SK`` even if the table were replaced with a lowercase-keyed one, and
    the suite would go green against a schema that no longer exists.
    """
    source = _STACK_FILE.read_text()
    start = source.find("const briefingTable = new dynamodb.Table(")
    assert start != -1, f"briefing table declaration not found in {_STACK_FILE}"
    block = source[start : source.index("});", start)]

    declared: list[str] = []
    for key in ("partitionKey", "sortKey"):
        match = re.search(rf'{key}:\s*{{\s*name:\s*"([^"]+)"', block)
        assert match is not None, f"no {key} name in the briefing table declaration"
        declared.append(match.group(1))

    assert tuple(declared) == LIVE_KEY_SCHEMA
    assert (PARTITION_KEY, SORT_KEY) == LIVE_KEY_SCHEMA


def test_the_double_rejects_the_key_names_the_adapter_used_to_write() -> None:
    """Prove the fake can catch the *class* of bug, not just today's instance.

    The old fake indexed ``Item["pk"]``, so lowercase keys round-tripped happily
    through the whole suite. This asserts the replacement fails the way
    BatchWriteItem failed in production, with the same message — otherwise every
    other test in this file is only proving the adapter agrees with itself.
    """
    table = FakeTable()
    lowercased = {k.lower(): v for k, v in job_to_item("u1", _job(1)).items()}

    with pytest.raises(ValidationException, match="does not match the schema"):
        table.batch_write([lowercased])
    with pytest.raises(ValidationException, match="Missing the key PK"):
        table.put_item(Item=lowercased)
    with pytest.raises(ValidationException, match="missed key schema element: PK"):
        table.query(
            KeyConditionExpression="#pk = :pk",
            ExpressionAttributeNames={"#pk": "pk"},
            ExpressionAttributeValues={":pk": "USER#u1"},
        )


def test_the_double_rejects_boto3_condition_objects() -> None:
    """A ``Key(...)`` condition is unverifiable, so it is not allowed back in.

    ``Key("pk").eq(...)`` is how the broken version spelled its query: the
    attribute name is buried in a ``ConditionBase`` the double cannot inspect, so
    switching back to one would silently disable the schema check above.
    """
    table = FakeTable()
    with pytest.raises(ValidationException, match="string key conditions"):
        table.query(KeyConditionExpression=object())


# --------------------------------------------------------------------------- #
# Mapping
# --------------------------------------------------------------------------- #


def test_briefing_item_round_trips() -> None:
    briefing = make_briefing()
    item = briefing_to_item("u1", briefing)

    assert item[SORT_KEY].startswith(BRIEFING_PREFIX)
    assert item["type"] == "briefing"
    assert item_to_briefing(item) == briefing


def test_job_item_round_trips() -> None:
    job = _job(1)
    item = job_to_item("u1", job)

    assert item[SORT_KEY] == f"{JOB_PREFIX}j1"
    assert item["job_id"] == "j1"
    assert item_to_job(item) == job


def test_to_dynamo_converts_floats_and_from_dynamo_inverts() -> None:
    converted = _to_dynamo({"a": 1.5, "b": [2.0, 3], "c": {"d": True}})
    assert converted == {"a": Decimal("1.5"), "b": [Decimal("2.0"), 3], "c": {"d": True}}

    restored = _from_dynamo(converted)
    assert restored == {"a": 1.5, "b": [2, 3], "c": {"d": True}}
    # integral Decimals come back as int, not float
    assert isinstance(restored["b"][0], int)
    assert isinstance(restored["a"], float)


def test_to_dynamo_keeps_bool_as_bool() -> None:
    assert _to_dynamo(True) is True
    assert _to_dynamo(False) is False


# --------------------------------------------------------------------------- #
# Reads and writes against the strict double
# --------------------------------------------------------------------------- #


def test_store_save_and_read_latest_briefing() -> None:
    store, _ = _store()
    briefing = make_briefing()
    assert store.latest_briefing("u1") is None

    store.save_briefing("u1", briefing)
    read = store.latest_briefing("u1")
    assert isinstance(read, Briefing)
    assert read == briefing
    assert store.write_errors == []


def test_store_latest_returns_newest_of_many_and_reads_one_item() -> None:
    store, table = _store()
    older = make_briefing()
    newer = older.model_copy(update={"generated_at": older.generated_at + timedelta(hours=9)})
    store.save_briefing("u1", older)
    store.save_briefing("u1", newer)

    assert store.latest_briefing("u1") == newer
    # Limit=1 + descending, not "read the partition and sort in Python": this
    # partition also holds every job ever surfaced to the user.
    assert table.queries[-1]["Limit"] == 1


def test_latest_briefing_normalises_offsets_before_comparing() -> None:
    """The sort key is the only definition of "latest", and it sorts as bytes.

    ``2026-07-06T23:00:00+05:30`` is 17:30 UTC — *earlier* than 18:00 UTC — but
    sorts after it as a string. Without the UTC normalisation in ``_stamp`` the
    read API would serve the stale briefing, and only for users who ran the cron
    from a non-UTC clock, which is the kind of bug that never reproduces.
    """
    store, _ = _store()
    base = make_briefing()
    latest = base.model_copy(update={"generated_at": datetime(2026, 7, 6, 18, tzinfo=UTC)})
    earlier_but_sorts_later = base.model_copy(
        update={
            "generated_at": datetime(
                2026, 7, 6, 23, tzinfo=timezone(timedelta(hours=5, minutes=30))
            ),
            "scanned": 99,
        }
    )
    store.save_briefing("u1", latest)
    store.save_briefing("u1", earlier_but_sorts_later)

    assert store.latest_briefing("u1") == latest


def test_latest_briefing_ignores_the_v1_prototypes_rows() -> None:
    """The live table already holds 8 rows the v2 payload parser cannot read.

    ``src/career_copilot/storage.py`` (the prototype, outside this package) wrote
    ``PK=<cognito sub>``, ``SK=BRIEFING#<date>`` with a ``markdown`` body and no
    ``payload``. The ``USER#`` prefix is what keeps them out of this query; drop
    it to "match the data" and the read API raises ``KeyError: 'payload'`` on any
    day before the first v2 briefing lands.
    """
    store, table = _store()
    sub = "84d81458-2011-705f-eec4-ccda7dcd1e35"
    table.put_item(
        Item={
            PARTITION_KEY: sub,
            SORT_KEY: f"{BRIEFING_PREFIX}2026-07-04",
            "markdown": "# yesterday",
            "summary": {"scanned": 3},
        }
    )

    assert store.latest_briefing(sub) is None


def test_store_save_jobs_and_seen_ids() -> None:
    store, table = _store()
    store.save_jobs("u1", [_job(1), _job(2)])

    assert store.seen_job_ids("u1") == {"j1", "j2"}
    assert table.batch_requests == 1
    assert store.write_errors == []


def test_seen_job_ids_follows_every_page() -> None:
    """A query stops at 1 MB whether or not you asked it to.

    Without the ``LastEvaluatedKey`` loop the dedup set truncates at the first
    page and jobs the user has already been shown come back as "new today" — the
    quietest possible failure, since the briefing still looks full.
    """
    store, table = _store(page_size=2)
    store.save_jobs("u1", [_job(n) for n in range(5)])

    assert store.seen_job_ids("u1") == {"j0", "j1", "j2", "j3", "j4"}
    assert len(table.queries) == 3


def test_seen_job_ids_needs_no_attribute_beyond_the_sort_key() -> None:
    """Rows written without ``job_id`` (the prototype wrote none) must still count."""
    store, table = _store()
    table.put_item(Item={PARTITION_KEY: "USER#u1", SORT_KEY: f"{JOB_PREFIX}legacy"})

    assert store.seen_job_ids("u1") == {"legacy"}


def test_seen_job_ids_excludes_the_briefing_rows() -> None:
    """One partition holds both kinds; the ``begins_with`` is the only separator."""
    store, _ = _store()
    store.save_briefing("u1", make_briefing())
    store.save_jobs("u1", [_job(1)])

    assert store.seen_job_ids("u1") == {"j1"}


def test_jobs_and_briefings_are_scoped_to_one_user() -> None:
    store, _ = _store()
    store.save_jobs("u1", [_job(1)])
    store.save_briefing("u1", make_briefing())

    assert store.seen_job_ids("u2") == frozenset()
    assert store.latest_briefing("u2") is None


def test_save_jobs_tolerates_a_duplicate_id_in_one_call() -> None:
    """Duplicate keys in one BatchWriteItem request fail the *whole* request.

    Two entries of one briefing can carry the same job id (the same posting
    reached the shortlist twice). Without ``overwrite_by_pkeys`` that is a
    ValidationException and the day's jobs are all lost, for a cosmetic reason.
    """
    store, table = _store()
    store.save_jobs("u1", [_job(1), _job(1)])

    assert store.seen_job_ids("u1") == {"j1"}
    assert store.write_errors == []
    assert len(table.items) == 1


# --------------------------------------------------------------------------- #
# Containment
# --------------------------------------------------------------------------- #


class _BrokenTable(FakeTable):
    """A table that fails the way a schema mismatch or an AccessDenied fails."""

    def put_item(self, *, Item: dict[str, Any]) -> None:
        raise ValidationException("The provided key element does not match the schema")

    def batch_write(self, batch: list[dict[str, Any]]) -> None:
        raise ValidationException("The provided key element does not match the schema")

    def query(self, **kwargs: Any) -> dict[str, Any]:
        raise ValidationException("no")


def test_a_failed_write_is_contained_and_logged_loudly(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The corpus is already committed by the time these two writes run.

    EventBridge is set to ``retryAttempts: 0`` because the run is not idempotent
    in the mailbox, so raising here used to throw away a successful 47k-posting
    sweep *and* skip the briefing email and drafts that come after it. Contained,
    but at ERROR with the traceback and a stable ``message`` — the monitoring
    stack builds metric filters on exactly that field, and a silently swallowed
    write is how the key bug survived a full test suite in the first place.
    """
    store = DynamoDbStore("career-copilot", table=_BrokenTable())

    with caplog.at_level(logging.ERROR, logger="copilot.adapters.dynamodb_store"):
        store.save_jobs("u1", [_job(1)])
        store.save_briefing("u1", make_briefing())

    assert [r.message for r in caplog.records] == ["briefing_store_write_failed"] * 2
    assert all(r.exc_info is not None for r in caplog.records)
    fields = caplog.records[0].extra_fields  # type: ignore[attr-defined]
    assert fields["operation"] == "save_jobs"
    assert fields["key_attributes"] == list(LIVE_KEY_SCHEMA)
    assert [e.split(":")[0] for e in store.write_errors] == ["save_jobs", "save_briefing"]


def test_a_failed_read_is_not_contained() -> None:
    """Containment stops at the writes, on purpose.

    A swallowed read would return ``None``, which ``handlers/api.py`` turns into a
    404 "no briefing yet" — indistinguishable, to the user, from a briefing that
    was never written. An exception is a 500 with a stack trace.
    """
    store = DynamoDbStore("career-copilot", table=_BrokenTable())

    with pytest.raises(ValidationException):
        store.latest_briefing("u1")
    with pytest.raises(ValidationException):
        store.seen_job_ids("u1")
    assert store.write_errors == []


def test_a_mapping_bug_still_raises() -> None:
    """Only the service call is contained; item building sits outside the block.

    Otherwise the containment would swallow the next ``KeyError`` in the mapping
    layer too, and this file would go quiet about it the way the old fake went
    quiet about the key names.
    """
    store, _ = _store()
    with pytest.raises(AttributeError):
        store.save_jobs("u1", [object()])  # type: ignore[list-item]
