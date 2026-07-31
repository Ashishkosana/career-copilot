"""Persistence behaviour, proven on an in-memory double of the boto3 Table API.

Two things are being tested here.

1. **Equivalence.** The SQLite suite's cases are re-run against *both* stores from
   one parametrised fixture. That is the whole point of a port: if the deployed
   store answers "what is new today" differently from the one on the laptop, the
   port is a lie. A behaviour change in either adapter fails these tests.
2. **The DynamoDB-only failure modes** that SQLite cannot have: a query that
   stops at 1 MB, duplicate keys inside one BatchWriteItem, an index projection
   that forgot an attribute, a conditional update that creates a phantom item,
   and an item over the 400 KB cap.

:class:`FakeTable` is deliberately strict rather than permissive — it emulates
sparse indexes, index projections, pagination, ``ExpressionAttributeNames``
resolution and the errors DynamoDB actually raises. A lenient fake would let a
projection bug or a missing pagination loop pass here and fail in Lambda. No moto,
no new dependency: the surface the adapter uses is five methods wide.
"""
from __future__ import annotations

import re
import sys
import types
from collections.abc import Iterator, Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest

from copilot.adapters import dynamodb_posting_store as adapter
from copilot.adapters.dynamodb_posting_store import (
    APPLIED_INDEX,
    CACHE_INDEX,
    MAX_ITEM_BYTES,
    OPEN_INDEX,
    OPEN_INDEX_PROJECTION,
    SEEN_INDEX,
    DynamoDbPostingStore,
    PostingTooLargeError,
    item_size_bytes,
)
from copilot.adapters.sqlite_posting_store import SqlitePostingStore
from copilot.domain.posting import Posting

DAY1 = datetime(2026, 7, 1, 9, 0, tzinfo=UTC)
DAY2 = DAY1 + timedelta(days=1)
DAY3 = DAY1 + timedelta(days=2)

#: DynamoDB's real ceiling. The adapter refuses below this; the fake enforces it
#: so a missing guard shows up as a failed write rather than a passing test.
_DYNAMO_ITEM_LIMIT = 400 * 1024
_BATCH_SIZE = 25


# --------------------------------------------------------------------------- #
# The double
# --------------------------------------------------------------------------- #


class ConditionalCheckFailedException(Exception):
    """Named to match botocore's dynamically built exception class."""


class ValidationException(Exception):
    """What DynamoDB raises for a malformed request or an oversized item."""


@dataclass(frozen=True)
class _IndexDef:
    pk: str
    sk: str
    #: ``None`` = project ALL; ``()`` = KEYS_ONLY; otherwise INCLUDE these.
    projection: tuple[str, ...] | None


#: Mirrors the table the CDK stack has to create. Sourced from the adapter's own
#: constants so a change there breaks these tests instead of production.
_INDEXES: Mapping[str, _IndexDef] = {
    OPEN_INDEX: _IndexDef("open_pk", "open_sk", OPEN_INDEX_PROJECTION),
    SEEN_INDEX: _IndexDef("seen_pk", "seen_sk", ()),
    CACHE_INDEX: _IndexDef("cache_pk", "cache_sk", ()),
    APPLIED_INDEX: _IndexDef("applied_pk", "applied_sk", ()),
}
_BASE = _IndexDef("pk", "sk", None)


def _split_top_level(text: str) -> list[str]:
    """Split on commas that are not inside ``if_not_exists(...)``."""
    parts: list[str] = []
    depth = 0
    current: list[str] = []
    for char in text:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        if char == "," and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(char)
    parts.append("".join(current))
    return [part.strip() for part in parts if part.strip()]


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
    """DynamoDB rejects unused names/values; so does this, to catch dead code."""
    text = " ".join(expressions)
    for token in (*names, *values):
        if not re.search(rf"{re.escape(token)}\b", text):
            raise ValidationException(f"unused expression attribute {token}")


def _eval_condition(condition: str, item: Mapping[str, Any], names: Mapping[str, str]) -> bool:
    for term in re.split(r"\s+AND\s+", condition.strip()):
        match = re.fullmatch(r"attribute_(not_)?exists\((#?\w+)\)", term)
        if match is None:
            raise ValidationException(f"unsupported condition: {term}")
        negated = bool(match.group(1))
        present = _resolve(match.group(2), names) in item
        if negated == present:
            return False
    return True


def _apply_update(
    item: dict[str, Any],
    expression: str,
    names: Mapping[str, str],
    values: Mapping[str, Any],
) -> None:
    clauses = re.split(r"\s+(?=SET\s|REMOVE\s)", expression.strip())
    for clause in clauses:
        verb, _, body = clause.strip().partition(" ")
        if verb == "SET":
            for assignment in _split_top_level(body):
                target, _, rhs = assignment.partition("=")
                item[_resolve(target.strip(), names)] = _eval_rhs(rhs.strip(), item, names, values)
        elif verb == "REMOVE":
            for target in _split_top_level(body):
                item.pop(_resolve(target.strip(), names), None)
        else:
            raise ValidationException(f"unsupported update clause: {clause}")


def _eval_rhs(
    rhs: str, item: Mapping[str, Any], names: Mapping[str, str], values: Mapping[str, Any]
) -> Any:
    match = re.fullmatch(r"if_not_exists\((.+)\)", rhs)
    if match is None:
        return _value(rhs, values)
    attr_token, value_token = _split_top_level(match.group(1))
    attr = _resolve(attr_token, names)
    return item[attr] if attr in item else _value(value_token, values)


class _FakeBatchWriter:
    """boto3's ``BatchWriter``, algorithm for algorithm.

    Copied from ``boto3/dynamodb/table.py`` rather than invented, because the
    adapter leans on two specifics of it: ``overwrite_by_pkeys`` de-duplicates
    *as items are added* (so a duplicate key never reaches the service), and
    ``__exit__`` loops ``while buffer: flush()`` — which is what makes unprocessed
    items eventually land. It also flushes on an exception, which is why the
    adapter size-checks everything before opening the block.
    """

    def __init__(self, table: FakeTable, overwrite_by_pkeys: list[str] | None) -> None:
        self._table = table
        self._overwrite = overwrite_by_pkeys
        self._buffer: list[dict[str, Any]] = []

    def __enter__(self) -> _FakeBatchWriter:
        return self

    def __exit__(self, *exc: object) -> None:
        while self._buffer:
            self._flush()

    def put_item(self, *, Item: dict[str, Any]) -> None:
        if self._overwrite is not None:
            keys = [Item[name] for name in self._overwrite]
            self._buffer = [i for i in self._buffer if [i[n] for n in self._overwrite] != keys]
        self._buffer.append(Item)
        if len(self._buffer) >= _BATCH_SIZE:
            self._flush()

    def _flush(self) -> None:
        batch = self._buffer[:_BATCH_SIZE]
        self._buffer = self._buffer[_BATCH_SIZE:]
        # Unprocessed items go back into the buffer, exactly as boto3 does.
        self._buffer.extend(self._table.batch_write(batch))


@dataclass
class FakeTable:
    """In-memory stand-in for ``boto3.resource('dynamodb').Table(...)``.

    ``page_size`` is in *items* rather than bytes; real DynamoDB cuts a query at
    1 MB, and any value here that is smaller than the result set exercises the
    same ``LastEvaluatedKey`` contract.

    ``throttle_every`` makes every nth item of a BatchWriteItem come back as
    unprocessed, which is how a table whose on-demand capacity is still ramping
    behaves — on an HTTP 200, not an error.
    """

    page_size: int = 100
    throttle_every: int = 0
    items: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    batch_flushes: list[int] = field(default_factory=list)
    batch_requests: int = 0
    puts: int = 0
    updates: int = 0
    queries: int = 0
    gets: int = 0
    _written: int = 0

    # --- writes ---

    def batch_write(self, batch: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """One BatchWriteItem request; returns the items it did not process."""
        self.batch_requests += 1
        self.batch_flushes.append(len(batch))
        keys = [(item["pk"], item["sk"]) for item in batch]
        if len(set(keys)) != len(keys):
            raise ValidationException("Provided list of item keys contains duplicates")
        unprocessed: list[dict[str, Any]] = []
        for item in batch:
            self._written += 1
            if self.throttle_every and self._written % self.throttle_every == 0:
                unprocessed.append(item)
            else:
                self.put_item(Item=item)
        return unprocessed

    def put_item(self, *, Item: dict[str, Any], ConditionExpression: str | None = None) -> None:
        self.puts += 1
        size = item_size_bytes(Item)
        if size > _DYNAMO_ITEM_LIMIT:
            raise ValidationException(f"Item size {size} has exceeded the maximum allowed size")
        key = (str(Item["pk"]), str(Item["sk"]))
        if ConditionExpression is not None and not _eval_condition(
            ConditionExpression, self.items.get(key, {}), {}
        ):
            raise ConditionalCheckFailedException("The conditional request failed")
        self.items[key] = deepcopy(Item)

    def update_item(
        self,
        *,
        Key: dict[str, Any],
        UpdateExpression: str,
        ExpressionAttributeNames: dict[str, str] | None = None,
        ExpressionAttributeValues: dict[str, Any] | None = None,
        ConditionExpression: str | None = None,
    ) -> dict[str, Any]:
        self.updates += 1
        names = ExpressionAttributeNames or {}
        values = ExpressionAttributeValues or {}
        _check_all_used([UpdateExpression, ConditionExpression or ""], names, values)
        key = (str(Key["pk"]), str(Key["sk"]))
        existing = self.items.get(key)
        if ConditionExpression is not None and not _eval_condition(
            ConditionExpression, existing or {}, names
        ):
            raise ConditionalCheckFailedException("The conditional request failed")
        item = deepcopy(existing) if existing is not None else dict(Key)
        _apply_update(item, UpdateExpression, names, values)
        size = item_size_bytes(item)
        if size > _DYNAMO_ITEM_LIMIT:
            raise ValidationException(f"Item size {size} has exceeded the maximum allowed size")
        self.items[key] = item
        return {}

    def batch_writer(self, overwrite_by_pkeys: list[str] | None = None) -> _FakeBatchWriter:
        return _FakeBatchWriter(self, overwrite_by_pkeys)

    # --- reads ---

    def get_item(
        self,
        *,
        Key: dict[str, Any],
        ProjectionExpression: str | None = None,
        ExpressionAttributeNames: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        self.gets += 1
        names = ExpressionAttributeNames or {}
        _check_all_used([ProjectionExpression or ""], names, {})
        item = self.items.get((str(Key["pk"]), str(Key["sk"])))
        if item is None:
            return {}
        if ProjectionExpression is None:
            return {"Item": deepcopy(item)}
        wanted = [_resolve(token.strip(), names) for token in ProjectionExpression.split(",")]
        return {"Item": {k: v for k, v in deepcopy(item).items() if k in wanted}}

    def query(self, **kwargs: Any) -> dict[str, Any]:
        self.queries += 1
        index = _INDEXES[kwargs["IndexName"]] if kwargs.get("IndexName") else _BASE
        names: dict[str, str] = kwargs.get("ExpressionAttributeNames", {})
        values: dict[str, Any] = kwargs.get("ExpressionAttributeValues", {})
        condition = kwargs["KeyConditionExpression"]
        if not isinstance(condition, str):
            raise ValidationException("this double only accepts string key conditions")
        _check_all_used([condition], names, values)
        rows = self._matching(condition, index, names, values)
        rows.sort(key=lambda item: str(item[index.sk]))
        if not kwargs.get("ScanIndexForward", True):
            rows.reverse()
        start = int(kwargs.get("ExclusiveStartKey", {}).get("_offset", 0))
        page = rows[start : start + self.page_size]
        response: dict[str, Any] = {"Count": len(page)}
        if kwargs.get("Select") != "COUNT":
            response["Items"] = [_project(item, index) for item in page]
        if start + self.page_size < len(rows):
            response["LastEvaluatedKey"] = {"_offset": start + self.page_size}
        return response

    def _matching(
        self,
        condition: str,
        index: _IndexDef,
        names: Mapping[str, str],
        values: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        terms = re.split(r"\s+AND\s+", condition.strip())
        pk_match = re.fullmatch(r"(#?\w+)\s*=\s*(:\w+)", terms[0].strip())
        if pk_match is None:
            raise ValidationException(f"unsupported key condition: {terms[0]}")
        if _resolve(pk_match.group(1), names) != index.pk:
            raise ValidationException("key condition does not start with the partition key")
        pk_value = _value(pk_match.group(2), values)
        # Sparse index: an item is only in it if it carries both key attributes.
        rows = [
            deepcopy(item)
            for item in self.items.values()
            if index.pk in item and index.sk in item and item[index.pk] == pk_value
        ]
        if len(terms) == 1:
            return rows
        return [row for row in rows if _sk_matches(terms[1], row, index, names, values)]


def _sk_matches(
    term: str,
    item: Mapping[str, Any],
    index: _IndexDef,
    names: Mapping[str, str],
    values: Mapping[str, Any],
) -> bool:
    begins = re.fullmatch(r"begins_with\((#?\w+),\s*(:\w+)\)", term.strip())
    if begins is not None:
        attr, expected = _resolve(begins.group(1), names), _value(begins.group(2), values)
        _require_sort_key(attr, index)
        return str(item[attr]).startswith(str(expected))
    compare = re.fullmatch(r"(#?\w+)\s*(=|<|<=|>|>=)\s*(:\w+)", term.strip())
    if compare is None:
        raise ValidationException(f"unsupported sort key condition: {term}")
    attr = _resolve(compare.group(1), names)
    _require_sort_key(attr, index)
    actual, expected = str(item[attr]), str(_value(compare.group(3), values))
    operator = compare.group(2)
    return {
        "=": actual == expected,
        "<": actual < expected,
        "<=": actual <= expected,
        ">": actual > expected,
        ">=": actual >= expected,
    }[operator]


def _require_sort_key(attr: str, index: _IndexDef) -> None:
    if attr != index.sk:
        raise ValidationException(f"{attr} is not the sort key of this index")


def _project(item: dict[str, Any], index: _IndexDef) -> dict[str, Any]:
    """Return only what the index actually projects.

    This is the strictness that matters most: with ALL projections everywhere, a
    schema that forgets to project ``description`` would pass every test and hand
    the scorer empty descriptions in production.
    """
    if index.projection is None:
        return item
    keys = {_BASE.pk, _BASE.sk, index.pk, index.sk, *index.projection}
    return {k: v for k, v in item.items() if k in keys}


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


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


def dynamo(*, page_size: int = 100) -> tuple[DynamoDbPostingStore, FakeTable]:
    table = FakeTable(page_size=page_size)
    return DynamoDbPostingStore("career-copilot-postings", table=table), table


@pytest.fixture(params=["dynamodb", "sqlite"])
def store(request: pytest.FixtureRequest) -> Iterator[Any]:
    """Both implementations of the port, driven by the same assertions.

    The DynamoDB variant runs with a deliberately tiny page size so that every
    shared case also crosses a query page boundary — pagination is not a special
    case in production, it is the normal state of a 25k-item table.
    """
    built: Any = (
        dynamo(page_size=3)[0] if request.param == "dynamodb" else SqlitePostingStore(":memory:")
    )
    yield built
    built.close()


# --------------------------------------------------------------------------- #
# Ported from the SQLite suite: both stores must answer identically
# --------------------------------------------------------------------------- #


class TestSync:
    def test_first_fetch_is_all_new(self, store: Any) -> None:
        new, seen = store.sync([post(1), post(2)], now=DAY1)
        assert len(new) == 2
        assert seen == []

    def test_second_fetch_of_the_same_roles_is_not_new(self, store: Any) -> None:
        """This is the whole point: day 2 must not look like day 1."""
        store.sync([post(1), post(2)], now=DAY1)
        new, seen = store.sync([post(1), post(2)], now=DAY2)
        assert new == []
        assert len(seen) == 2

    def test_only_genuinely_new_roles_are_reported(self, store: Any) -> None:
        store.sync([post(1)], now=DAY1)
        new, seen = store.sync([post(1), post(2)], now=DAY2)
        assert len(new) == 1
        assert len(seen) == 1

    def test_first_seen_is_never_overwritten(self, store: Any) -> None:
        store.sync([post(1)], now=DAY1)
        store.sync([post(1)], now=DAY3)
        assert store.new_since(DAY2) == []  # still dated to day 1, so not "new"

    def test_new_since_is_the_diff_feed(self, store: Any) -> None:
        store.sync([post(1)], now=DAY1)
        store.sync([post(1), post(2)], now=DAY2)
        fresh = store.new_since(DAY1 + timedelta(hours=1))
        assert [p.title for p in fresh] == ["Software Engineer 2"]

    def test_an_empty_fetch_writes_nothing(self, store: Any) -> None:
        assert store.sync([], now=DAY1) == ([], [])
        assert store.open_postings() == []


class TestDescriptionPreservation:
    def test_an_empty_description_never_overwrites_a_real_one(self, store: Any) -> None:
        """Workday returns no description; the same role from Greenhouse does."""
        store.sync([post(1, desc="the full text")], now=DAY1)
        store.sync([post(1, desc="")], now=DAY2)
        [stored] = store.open_postings()
        assert stored.description == "the full text"
        assert stored.desc_available is True

    def test_a_real_description_upgrades_an_empty_one(self, store: Any) -> None:
        store.sync([post(1, desc="")], now=DAY1)
        store.sync([post(1, desc="now we have it")], now=DAY2)
        [stored] = store.open_postings()
        assert stored.description == "now we have it"
        assert stored.desc_available is True

    def test_a_role_that_never_had_a_description_stays_flagged(self, store: Any) -> None:
        """desc_available=False must survive, or title-only gating is skipped."""
        store.sync([post(1, desc="")], now=DAY1)
        store.sync([post(1, desc="")], now=DAY2)
        [stored] = store.open_postings()
        assert stored.description == ""
        assert stored.desc_available is False


class TestClosing:
    def test_absent_postings_are_closed(self, store: Any) -> None:
        store.sync([post(1), post(2)], now=DAY1)
        store.sync([post(1)], now=DAY2)
        closed = store.close_missing(now=DAY2, seen_ids={post(1).id})
        assert closed == 1
        assert [p.title for p in store.open_postings()] == ["Software Engineer 1"]

    def test_an_empty_fetch_does_not_mass_close(self, store: Any) -> None:
        """An empty fetch is a broken run, not a market where every job vanished."""
        store.sync([post(1), post(2)], now=DAY1)
        assert store.close_missing(now=DAY2, seen_ids=set()) == 0
        assert len(store.open_postings()) == 2

    def test_a_reappearing_posting_is_reopened(self, store: Any) -> None:
        store.sync([post(1), post(2)], now=DAY1)
        store.close_missing(now=DAY2, seen_ids={post(1).id})
        store.sync([post(2)], now=DAY3)
        assert len(store.open_postings()) == 2

    def test_a_reappearing_posting_is_not_new_again(self, store: Any) -> None:
        """A role that vanishes for a day and returns is not a fresh opening."""
        store.sync([post(1), post(2)], now=DAY1)
        store.close_missing(now=DAY2, seen_ids={post(1).id})
        new, seen = store.sync([post(1), post(2)], now=DAY3)
        assert new == []
        assert len(seen) == 2
        assert store.new_since(DAY2) == []

    def test_closing_twice_counts_once(self, store: Any) -> None:
        """The count is 'how many closed now', so a rerun must report zero."""
        store.sync([post(1), post(2)], now=DAY1)
        assert store.close_missing(now=DAY2, seen_ids={post(1).id}) == 1
        assert store.close_missing(now=DAY3, seen_ids={post(1).id}) == 0


class TestInterpretationCache:
    def test_round_trip(self, store: Any) -> None:
        store.sync([post(1)], now=DAY1)
        assert store.cached_interpretation(post(1).id) is None
        store.save_interpretation(post(1).id, {"band": "entry", "min_years": 0})
        assert store.cached_interpretation(post(1).id) == {"band": "entry", "min_years": 0}

    def test_uncached_ids_drives_the_batch(self, store: Any) -> None:
        """The cost lever: a posting's description is read once, not once per day."""
        store.sync([post(1), post(2), post(3)], now=DAY1)
        ids = [post(n).id for n in (1, 2, 3)]
        assert len(store.uncached_ids(ids)) == 3
        store.save_interpretation(post(2).id, {"band": "mid"})
        assert store.uncached_ids(ids) == [post(1).id, post(3).id]

    def test_uncached_ids_of_nothing_is_nothing(self, store: Any) -> None:
        assert store.uncached_ids([]) == []

    def test_cache_survives_a_refetch(self, store: Any) -> None:
        store.sync([post(1)], now=DAY1)
        store.save_interpretation(post(1).id, {"band": "entry"})
        store.sync([post(1)], now=DAY2)
        assert store.cached_interpretation(post(1).id) == {"band": "entry"}

    def test_cache_survives_a_close_and_reopen(self, store: Any) -> None:
        """Paying the LLM again because a role blinked out for a day is waste."""
        store.sync([post(1), post(2)], now=DAY1)
        store.save_interpretation(post(1).id, {"band": "entry"})
        store.close_missing(now=DAY2, seen_ids={post(2).id})
        store.sync([post(1), post(2)], now=DAY3)
        assert store.cached_interpretation(post(1).id) == {"band": "entry"}

    def test_large_id_lists_are_handled(self, store: Any) -> None:
        many = [post(n) for n in range(1200)]
        store.sync(many, now=DAY1)
        assert len(store.uncached_ids([p.id for p in many])) == 1200


class TestApplied:
    def test_mark_applied_is_idempotent(self, store: Any) -> None:
        store.sync([post(1)], now=DAY1)
        store.mark_applied(post(1).id, now=DAY1)
        store.mark_applied(post(1).id, now=DAY3)
        assert store.stats()["applied"] == 1

    def test_marking_an_unknown_posting_is_a_no_op(self, store: Any) -> None:
        """A stale id from the CLI must not invent a posting."""
        store.sync([post(1)], now=DAY1)
        store.mark_applied("deadbeefdeadbeef", now=DAY1)
        assert store.stats() == {
            "total": 1, "open": 1, "closed": 0, "interpreted": 0, "applied": 0
        }

    def test_interpreting_an_unknown_posting_is_a_no_op(self, store: Any) -> None:
        store.save_interpretation("deadbeefdeadbeef", {"band": "entry"})
        assert store.cached_interpretation("deadbeefdeadbeef") is None
        assert store.stats()["total"] == 0


class TestStats:
    def test_counts(self, store: Any) -> None:
        store.sync([post(1), post(2), post(3)], now=DAY1)
        store.close_missing(now=DAY2, seen_ids={post(1).id, post(2).id})
        store.save_interpretation(post(1).id, {"band": "entry"})
        store.mark_applied(post(1).id, now=DAY2)
        assert store.stats() == {
            "total": 3, "open": 2, "closed": 1, "interpreted": 1, "applied": 1
        }


class TestOrdering:
    def test_open_postings_are_newest_posted_first(self, store: Any) -> None:
        """The digest reads top-down, so order is product behaviour, not a detail.
        A posting whose source gave no date sorts last rather than first."""
        old = post(1).model_copy(update={"posted_at": DAY1})
        recent = post(2).model_copy(update={"posted_at": DAY3})
        undated = post(3).model_copy(update={"posted_at": None})
        store.sync([old, recent, undated], now=DAY1)
        assert [p.title for p in store.open_postings()] == [
            recent.title,
            old.title,
            undated.title,
        ]

    def test_new_since_is_newest_first_seen_first(self, store: Any) -> None:
        """Enough postings that the shard iteration order cannot fake this."""
        arrivals = [post(n) for n in range(8)]
        for day in range(len(arrivals)):
            # one more posting arrives each day, so first_seen is strictly ordered
            store.sync(arrivals[: day + 1], now=DAY1 + timedelta(days=day))
        fresh = store.new_since(DAY1 - timedelta(days=1))
        assert [p.title for p in fresh] == [p.title for p in reversed(arrivals)]


class TestRoundTrip:
    def test_every_field_survives(self, store: Any) -> None:
        """A dropped field is invisible until a gate silently changes its mind.

        Every declared field is set to a non-default value on purpose: an
        adapter that forgets one still passes an equality check if the value it
        forgot happens to equal the model default. ``experience_level`` was
        exactly that — DynamoDB round-tripped it, SQLite had no column, and the
        divergence was invisible because no parser populates the field yet.
        """
        posting = Posting(
            title="Backend Engineer",
            company="Globex",
            url="https://jobs.globex.example/42",
            ats="workday",
            tenant="globex",
            location="Tempe, AZ",
            description="python, aws",
            desc_available=True,
            req_id="R-42",
            posted_at=DAY1,
            remote=False,
            employment_type="FULL_TIME",
            experience_level="Entry Level",
        )
        store.sync([posting], now=DAY1)
        [stored] = store.open_postings()
        assert stored == posting

    def test_no_declared_field_is_left_at_its_default_by_this_suite(self) -> None:
        """Guards the guard: the case above only proves what it varies.

        If someone adds a field to ``Posting`` and does not persist it, the
        equality assertion above passes silently unless the fixture sets that
        field to something other than its default. This fails instead.
        """
        posting = Posting(
            title="Backend Engineer",
            company="Globex",
            url="https://jobs.globex.example/42",
            ats="workday",
            tenant="globex",
            location="Tempe, AZ",
            description="python, aws",
            desc_available=True,
            req_id="R-42",
            posted_at=DAY1,
            remote=False,
            employment_type="FULL_TIME",
            experience_level="Entry Level",
        )
        #: ``desc_available=False`` cannot be varied here without contradicting
        #: ``description="python, aws"``. It has its own both-stores case in
        #: ``TestDescriptionPreservation``, which is the stronger test anyway: it
        #: asserts the flag survives a *second* fetch, not just one write.
        covered_elsewhere = {"desc_available"}
        defaulted = {
            name
            for name, field in Posting.model_fields.items()
            if getattr(posting, name) == field.default
        } - covered_elsewhere
        assert defaulted == set(), (
            f"test_every_field_survives leaves {sorted(defaulted)} at the model "
            "default, so it cannot detect an adapter that drops them"
        )


# --------------------------------------------------------------------------- #
# DynamoDB-only failure modes
# --------------------------------------------------------------------------- #


class TestPagination:
    def test_open_postings_follows_every_page(self) -> None:
        """A query stops at 1 MB whether you handle it or not."""
        store, _ = dynamo(page_size=3)
        postings = [post(n) for n in range(60)]
        store.sync(postings, now=DAY1)
        assert len(store.open_postings()) == 60

    def test_new_since_follows_every_page(self) -> None:
        store, _ = dynamo(page_size=2)
        store.sync([post(n) for n in range(40)], now=DAY2)
        assert len(store.new_since(DAY1)) == 40

    def test_the_known_id_probe_follows_every_page(self) -> None:
        """A truncated probe would re-put known postings and erase first_seen."""
        store, _ = dynamo(page_size=2)
        postings = [post(n) for n in range(40)]
        store.sync(postings, now=DAY1)
        store.save_interpretation(postings[0].id, {"band": "entry"})
        new, seen = store.sync(postings, now=DAY2)
        assert new == []
        assert len(seen) == 40
        assert store.cached_interpretation(postings[0].id) == {"band": "entry"}

    def test_stats_and_uncached_ids_follow_every_page(self) -> None:
        store, _ = dynamo(page_size=2)
        postings = [post(n) for n in range(40)]
        store.sync(postings, now=DAY1)
        for posting in postings:
            store.save_interpretation(posting.id, {"band": "entry"})
        assert store.uncached_ids([p.id for p in postings]) == []
        assert store.stats() == {
            "total": 40, "open": 40, "closed": 0, "interpreted": 40, "applied": 0
        }


class TestBatching:
    def test_new_postings_are_written_25_at_a_time(self) -> None:
        """25k single PutItems is the difference between seconds and minutes."""
        store, table = dynamo()
        store.sync([post(n) for n in range(60)], now=DAY1)
        assert table.batch_flushes == [25, 25, 10]

    def test_known_postings_never_go_through_a_blind_put(self) -> None:
        """A put would erase first_seen, the LLM cache and applied_at."""
        store, table = dynamo()
        store.sync([post(n) for n in range(10)], now=DAY1)
        table.batch_flushes.clear()
        table.updates = 0
        store.sync([post(n) for n in range(10)], now=DAY2)
        assert table.batch_flushes == []
        assert table.updates == 10

    def test_unprocessed_items_are_retried_until_every_posting_lands(self) -> None:
        """Partial throttling arrives as UnprocessedItems on an HTTP 200. Code
        that treats a 200 as "written" loses postings and never notices."""
        store, table = dynamo()
        table.throttle_every = 3
        store.sync([post(n) for n in range(30)], now=DAY1)
        assert len(store.open_postings()) == 30
        # More requests than the two a clean run needs, i.e. retries happened.
        assert table.batch_requests > 2

    def test_two_postings_sharing_a_url_do_not_fail_the_batch(self) -> None:
        """Duplicate keys in one BatchWriteItem are a ValidationException; a
        company re-listing the same requisition is routine."""
        store, table = dynamo()
        new, seen = store.sync([post(1), post(1)], now=DAY1)
        assert new == [post(1).id, post(1).id]  # matches SQLite's return
        assert seen == []
        assert len(store.open_postings()) == 1
        assert table.batch_flushes == [1]


class TestItemSize:
    def test_a_normal_description_is_nowhere_near_the_cap(self) -> None:
        """Measured worst case is ~25 KB against a 400 KB ceiling."""
        item = dynamo()[0]._item(
            post(1, desc="x" * 25_000), first_seen="2026-07-01", last_seen="2026-07-01"
        )
        assert item_size_bytes(item) < MAX_ITEM_BYTES // 10

    def test_an_oversized_posting_fails_loudly_and_writes_nothing(self) -> None:
        """Better a raised error than a fetch that silently lost a posting."""
        store, table = dynamo()
        with pytest.raises(PostingTooLargeError, match="over the"):
            store.sync([post(1), post(2, desc="x" * (MAX_ITEM_BYTES + 1))], now=DAY1)
        assert table.items == {}
        assert table.batch_flushes == []

    def test_the_size_check_counts_attribute_names_too(self) -> None:
        assert item_size_bytes({"pk": "a"}) == 3
        assert item_size_bytes({"desc_available": True}) == len("desc_available") + 1


class TestIndexHygiene:
    def test_a_closed_posting_leaves_the_open_index(self) -> None:
        """'Still open' is index membership, not a filtered attribute."""
        store, table = dynamo()
        store.sync([post(1), post(2)], now=DAY1)
        store.close_missing(now=DAY2, seen_ids={post(1).id})
        indexed = [i for i in table.items.values() if "open_pk" in i]
        assert [i["id"] for i in indexed] == [post(1).id]
        # ...but it is still known, so it can never be counted as new again.
        closed = table.items[(f"POSTING#{post(2).id}", "META")]
        assert closed["seen_sk"].startswith("CLOSED#")
        assert closed["closed_at"] == DAY2.isoformat()

    def test_ids_spread_across_shards(self) -> None:
        """A single hot partition is the failure this schema is shaped to avoid."""
        store, table = dynamo()
        store.sync([post(n) for n in range(200)], now=DAY1)
        shards = {str(item["open_pk"]) for item in table.items.values()}
        assert len(shards) > 8

    def test_open_index_projects_everything_a_posting_needs(self) -> None:
        """The fake honours the projection, so a missing attribute raises here."""
        store, _ = dynamo()
        store.sync([post(1)], now=DAY1)
        assert store.open_postings()[0].title == "Software Engineer 1"
        assert set(OPEN_INDEX_PROJECTION) >= {"description", "desc_available", "first_seen"}

    def test_the_llm_cache_is_not_copied_into_the_read_index(self) -> None:
        """Projecting it would double the write cost of every interpretation."""
        assert "interpretation" not in OPEN_INDEX_PROJECTION


class TestTimestampNormalisation:
    def test_offsets_do_not_break_the_new_since_boundary(self) -> None:
        """Sort keys compare as bytes, so a +05:00 stamp would sort as if later."""
        store, _ = dynamo()
        early = datetime(2026, 7, 1, 9, 0, tzinfo=timezone(timedelta(hours=5)))  # 04:00Z
        store.sync([post(1)], now=early)
        store.sync([post(1), post(2)], now=datetime(2026, 7, 1, 6, 0, tzinfo=UTC))
        fresh = store.new_since(datetime(2026, 7, 1, 5, 0, tzinfo=UTC))
        assert [p.title for p in fresh] == ["Software Engineer 2"]

    def test_the_since_boundary_is_exclusive(self) -> None:
        """first_seen == since must not count as new; the sort key alone would."""
        store, _ = dynamo()
        store.sync([post(1)], now=DAY1)
        assert store.new_since(DAY1) == []
        assert len(store.new_since(DAY1 - timedelta(seconds=1))) == 1


class TestRaces:
    def test_mark_applied_keeps_the_first_timestamp(self) -> None:
        """The application *date* is the record; a second call must not move it."""
        store, table = dynamo()
        store.sync([post(1)], now=DAY1)
        store.mark_applied(post(1).id, now=DAY1)
        store.mark_applied(post(1).id, now=DAY3)
        item = table.items[(f"POSTING#{post(1).id}", "META")]
        assert item["applied_at"] == DAY1.isoformat()
        assert item["applied_sk"] == f"{DAY1.isoformat()}#{post(1).id}"

    def test_a_second_invocation_cannot_re_close_a_posting(self) -> None:
        """Two crons overlapping would otherwise both count the same close and
        push closed_at forward, so 'closed on' would drift by a day per run."""
        store, table = dynamo()
        store.sync([post(1)], now=DAY1)
        first_seen = DAY1.isoformat()
        assert store._close_one(
            post(1).id, first_seen=first_seen, now=DAY2.isoformat()
        )
        assert not store._close_one(
            post(1).id, first_seen=first_seen, now=DAY3.isoformat()
        )
        assert table.items[(f"POSTING#{post(1).id}", "META")]["closed_at"] == DAY2.isoformat()

    def test_a_posting_that_vanishes_between_probe_and_write_is_rewritten_whole(self) -> None:
        """The probe is a read. A partial update would leave an item with no url,
        which every reader would then choke on."""
        store, table = dynamo()
        store._update_seen(
            post(1), first_seen=DAY1.isoformat(), now=DAY2.isoformat()
        )
        [stored] = store.open_postings()
        assert stored == post(1)
        assert table.puts == 1


class TestLazyImport:
    def test_importing_the_module_does_not_bind_boto3(self) -> None:
        """House rule: the SDK is imported inside the method that needs it, so
        importing this package never requires boto3 or AWS credentials."""
        assert not hasattr(adapter, "boto3")
        # Constructing the store must also stay credential-free.
        assert adapter.DynamoDbPostingStore("career-copilot-postings") is not None

    def test_the_table_is_built_with_the_region_and_adaptive_retries(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Untested wiring is where "why is it hitting us-east-1?" comes from —
        and adaptive retry is the only brake on boto3's no-delay re-send of
        unprocessed items, so a default Config here is a throttling incident.

        boto3 is stubbed rather than imported: this suite must run without it.
        """
        captured: dict[str, Any] = {}

        class _Resource:
            def Table(self, name: str) -> str:
                captured["table_name"] = name
                return "table-resource"

        def _resource(service: str, **kwargs: Any) -> _Resource:
            captured["service"] = service
            captured.update(kwargs)
            return _Resource()

        config_module = types.ModuleType("botocore.config")
        config_module.Config = lambda **kwargs: kwargs  # type: ignore[attr-defined]
        botocore_module = types.ModuleType("botocore")
        botocore_module.config = config_module  # type: ignore[attr-defined]
        boto3_module = types.ModuleType("boto3")
        boto3_module.resource = _resource  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "boto3", boto3_module)
        monkeypatch.setitem(sys.modules, "botocore", botocore_module)
        monkeypatch.setitem(sys.modules, "botocore.config", config_module)

        store = DynamoDbPostingStore("career-copilot-postings", region="us-west-2")
        assert store.table == "table-resource"
        assert captured["service"] == "dynamodb"
        assert captured["region_name"] == "us-west-2"
        assert captured["table_name"] == "career-copilot-postings"
        assert captured["config"]["retries"]["mode"] == "adaptive"
