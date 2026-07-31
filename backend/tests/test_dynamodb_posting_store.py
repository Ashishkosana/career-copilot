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
from collections.abc import Iterator, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, field, replace
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
from copilot.domain.screening import Exclusion
from copilot.ports.postingstore import (
    QUOTE_MAX_CHARS,
    UNDATED_SORT_STAMP,
    VIEW_INTERNSHIPS,
    VIEW_KEPT,
    ScreenedRow,
    ScreenSummary,
    cap_quote,
    summary_from_payload,
    summary_to_payload,
)

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
    #: Every item key in the order it was written. The screening view's whole
    #: crash-safety argument is "the summary is written last", and nothing else
    #: here can observe write *order*.
    write_log: list[tuple[str, str]] = field(default_factory=list)
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
        self.write_log.append(key)

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
        # ``Limit`` caps a page; ``page_size`` stands in for the 1 MB cut, which
        # DynamoDB applies *first*. So the smaller of the two wins, and a caller
        # asking for 26 rows from a table paging at 3 really does get 3 — which is
        # exactly the under-fill an implementation using ``Limit`` alone would hide.
        size = min(self.page_size, int(kwargs["Limit"])) if "Limit" in kwargs else self.page_size
        page = rows[start : start + size]
        response: dict[str, Any] = {"Count": len(page)}
        if kwargs.get("Select") != "COUNT":
            response["Items"] = [_project(item, index) for item in page]
        if start + size < len(rows):
            response["LastEvaluatedKey"] = {"_offset": start + size}
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


def kept_row(
    n: int, *, posted_at: datetime | None = DAY1, first_seen: datetime | None = None
) -> ScreenedRow:
    """One kept row. ``first_seen`` defaults to unset, as the real writer leaves it.

    It is settable only so :class:`TestFirstSeen` can prove the store ignores it: the
    screening pass that builds these rows has no access to storage history, and the
    store stamps the column it owns.
    """
    return ScreenedRow(
        posting_id=post(n).id,
        view=VIEW_KEPT,
        posted_at=posted_at,
        kept=True,
        level="entry",
        level_source="title",
        level_why="the title carries an entry marker",
        eligibility_checked=True,
        sponsorship="unstated",
        first_seen=first_seen,
    )


def gate_row(
    n: int,
    gate: Exclusion,
    *,
    posted_at: datetime | None = DAY1,
    quote: str = "US citizens only",
) -> ScreenedRow:
    return ScreenedRow(
        posting_id=post(n).id,
        view=gate.value,
        posted_at=posted_at,
        kept=False,
        level="senior",
        level_source="title",
        level_why="the title carries a senior marker",
        eligibility_checked=True,
        sponsorship="unstated",
        gate=gate.value,
        reason=f"failed {gate.value}",
        quote=quote,
    )


def intern_row(n: int, *, posted_at: datetime | None = DAY1) -> ScreenedRow:
    return ScreenedRow(
        posting_id=post(n).id,
        view=VIEW_INTERNSHIPS,
        posted_at=posted_at,
        kept=True,
        level="intern",
        level_source="title",
        level_why="the title carries an intern marker",
        eligibility_checked=True,
        sponsorship="unstated",
    )


def view_summary(
    rows: Sequence[ScreenedRow],
    *,
    generation: str = "gen-1",
    screened_at: datetime = DAY2,
) -> ScreenSummary:
    """A summary derived from the rows, so a test cannot state numbers that disagree."""
    kept = [row for row in rows if row.view == VIEW_KEPT]
    interns = [row for row in rows if row.view == VIEW_INTERNSHIPS]
    gates: dict[str, int] = {}
    for row in rows:
        if row.gate:
            gates[row.gate] = gates.get(row.gate, 0) + 1
    postings = {row.posting_id for row in rows}
    return ScreenSummary(
        generation=generation,
        screened_at=screened_at,
        corpus_size=len(postings),
        screened=len(postings),
        kept=len(kept),
        excluded=len(postings) - len(kept),
        gates=gates,
        needs_level_check=0,
        eligible_total=len(kept),
        internship_total=len(interns),
    )


def dies_after(rows: Sequence[ScreenedRow], n: int) -> Iterator[ScreenedRow]:
    """A row producer that fails part-way, the way a Lambda timeout does."""
    for index, row in enumerate(rows):
        if index >= n:
            raise RuntimeError("screen died mid-write")
        yield row


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
# The materialised screening view — both stores, one set of assertions
#
# The bug every case below prevents is the same one: the read API screened the
# whole corpus per request (1.7 s to read 25,294 rows, 37.8 s to screen them;
# ~70 s at the deployed 47,538) against a 29 s API Gateway ceiling, so every
# request 504'd including ``?limit=1``. These prove the view that replaces it is
# O(page), that it is never half-published, and that both adapters agree.
# --------------------------------------------------------------------------- #


class TestScreeningViewLifecycle:
    def test_an_unscreened_corpus_says_so_instead_of_pretending(self, store: Any) -> None:
        """The "not ready" answer. Without it a reader has to screen live to find
        out there is nothing to read, which is the 504."""
        store.sync([post(1)], now=DAY1)
        assert store.screening_summary() is None
        assert store.screened_page(VIEW_KEPT, generation="gen-1", limit=25).rows == ()

    def test_publishing_makes_the_funnel_and_the_page_readable(self, store: Any) -> None:
        rows = [kept_row(1), kept_row(2), gate_row(3, Exclusion.LEVEL)]
        summary = view_summary(rows)
        store.save_screening(rows, summary=summary)

        stored = store.screening_summary()
        assert stored == summary
        page = store.screened_page(VIEW_KEPT, generation=summary.generation, limit=25)
        assert {row.posting_id for row in page.rows} == {post(1).id, post(2).id}
        assert page.next_token is None

    def test_the_summary_survives_the_round_trip_field_for_field(self, store: Any) -> None:
        """A count that decodes wrongly is worse than one that is missing: it is
        published on the page as fact."""
        rows = [kept_row(1), gate_row(2, Exclusion.NOT_SWE), gate_row(2, Exclusion.LEVEL)]
        summary = view_summary(rows, generation="gen-round-trip", screened_at=DAY3)
        store.save_screening(rows, summary=summary)

        stored = store.screening_summary()
        assert stored is not None
        assert stored.generation == "gen-round-trip"
        assert stored.screened_at == DAY3
        assert stored.gates == {"not_a_software_role": 1, "wrong_seniority_band": 1}
        assert stored.gate_count_total == 2
        assert stored.excluded == 1, "one posting removed, two gate fires"

    def test_a_screen_that_dies_mid_write_leaves_the_previous_view_current(
        self, store: Any
    ) -> None:
        """The exact production shape: the first live cron crashed after the corpus
        landed and before the run finished. A half-written view that reads as
        authoritative would publish a page with nothing on it and no way to tell
        that apart from a genuinely empty market."""
        good = [kept_row(n) for n in range(4)]
        first = view_summary(good, generation="gen-good")
        store.save_screening(good, summary=first)

        doomed = [kept_row(n) for n in range(20, 40)]
        with pytest.raises(RuntimeError, match="died mid-write"):
            store.save_screening(
                dies_after(doomed, 5), summary=view_summary(doomed, generation="gen-doomed")
            )

        assert store.screening_summary() == first
        page = store.screened_page(VIEW_KEPT, generation="gen-good", limit=25)
        assert len(page.rows) == 4

    def test_rows_under_an_unpublished_generation_are_unreachable(self, store: Any) -> None:
        """The generation is the publish mechanism, not a label. Nothing can read a
        pass the summary does not name — which is why writing the summary last is
        the whole crash-safety argument."""
        rows = [kept_row(1)]
        store.save_screening(rows, summary=view_summary(rows, generation="gen-1"))
        assert store.screened_page(VIEW_KEPT, generation="gen-2", limit=25).rows == ()

    def test_republishing_serves_the_new_pass_and_not_the_old_one(self, store: Any) -> None:
        yesterday = [kept_row(1), kept_row(2)]
        store.save_screening(yesterday, summary=view_summary(yesterday, generation="gen-1"))
        today = [kept_row(3)]
        latest = view_summary(today, generation="gen-2", screened_at=DAY3)
        store.save_screening(today, summary=latest)

        assert store.screening_summary() == latest
        page = store.screened_page(VIEW_KEPT, generation="gen-2", limit=25)
        assert [row.posting_id for row in page.rows] == [post(3).id]

    def test_an_unknown_view_is_refused_rather_than_answered_emptily(self, store: Any) -> None:
        """A typo'd view name returning no rows is indistinguishable from a screen
        that produced nothing, and telling those apart is why this view exists."""
        with pytest.raises(ValueError, match="unknown screening view"):
            store.screened_page("kepts", generation="gen-1", limit=25)


class TestScreeningViewOrderingAndPaging:
    def test_a_page_is_newest_posted_first_with_undated_last(self, store: Any) -> None:
        """The worklist's only ordering is recency, and an undated posting must sort
        last rather than vanish or lead."""
        rows = [
            kept_row(1, posted_at=DAY1),
            kept_row(2, posted_at=DAY3),
            kept_row(3, posted_at=None),
            kept_row(4, posted_at=DAY2),
        ]
        store.save_screening(rows, summary=view_summary(rows))
        page = store.screened_page(VIEW_KEPT, generation="gen-1", limit=25)
        assert [row.posted_at for row in page.rows] == [DAY3, DAY2, DAY1, None]

    def test_keyset_paging_walks_every_row_exactly_once(self, store: Any) -> None:
        """40 rows in pages of 7 — the boundary case that catches an off-by-one in
        the cursor, which shows up as a skipped or duplicated posting."""
        rows = [kept_row(n, posted_at=DAY1 + timedelta(minutes=n)) for n in range(40)]
        store.save_screening(rows, summary=view_summary(rows))

        seen: list[str] = []
        token: str | None = None
        for _ in range(20):  # generous bound; a runaway loop is a failure too
            page = store.screened_page(VIEW_KEPT, generation="gen-1", limit=7, after=token)
            seen.extend(row.posting_id for row in page.rows)
            token = page.next_token
            if token is None:
                break
        assert token is None, "paging never terminated"
        assert len(seen) == 40
        assert len(set(seen)) == 40, "a posting was served on two pages"
        assert seen == [row.posting_id for row in reversed(rows)]

    def test_the_last_full_page_reports_no_next_token(self, store: Any) -> None:
        """``hasMore`` is derived from the token, so a token handed out on an exact
        multiple would render a 'next page' button onto an empty page."""
        rows = [kept_row(n, posted_at=DAY1 + timedelta(minutes=n)) for n in range(10)]
        store.save_screening(rows, summary=view_summary(rows))
        first = store.screened_page(VIEW_KEPT, generation="gen-1", limit=5)
        assert first.next_token is not None
        second = store.screened_page(
            VIEW_KEPT, generation="gen-1", limit=5, after=first.next_token
        )
        assert len(second.rows) == 5
        assert second.next_token is None

    def test_postings_sharing_a_posted_at_still_page_without_repeats(self, store: Any) -> None:
        """~4,700 postings in this corpus share a ``posted_at`` to the second. A sort
        key without the id would make every page boundary between them lossy."""
        rows = [kept_row(n, posted_at=DAY1) for n in range(9)]
        store.save_screening(rows, summary=view_summary(rows))
        first = store.screened_page(VIEW_KEPT, generation="gen-1", limit=4)
        second = store.screened_page(
            VIEW_KEPT, generation="gen-1", limit=4, after=first.next_token
        )
        third = store.screened_page(
            VIEW_KEPT, generation="gen-1", limit=4, after=second.next_token
        )
        served = [r.posting_id for r in (*first.rows, *second.rows, *third.rows)]
        assert len(set(served)) == 9


class TestScreeningViewMembership:
    def test_a_posting_is_filed_under_every_gate_it_failed(self, store: Any) -> None:
        """A posting is routinely senior *and* clearance-restricted, and /excluded
        groups by gate. One row per posting could not serve both groups."""
        rows = [
            gate_row(1, Exclusion.LEVEL),
            gate_row(1, Exclusion.CLEARANCE, quote="active TS/SCI clearance"),
            gate_row(2, Exclusion.LEVEL),
        ]
        store.save_screening(rows, summary=view_summary(rows))
        level = store.screened_page("wrong_seniority_band", generation="gen-1", limit=25)
        clearance = store.screened_page(
            "security_clearance_required", generation="gen-1", limit=25
        )
        assert {row.posting_id for row in level.rows} == {post(1).id, post(2).id}
        assert [row.posting_id for row in clearance.rows] == [post(1).id]
        assert clearance.rows[0].quote == "active TS/SCI clearance"

    def test_each_gate_row_carries_its_own_evidence(self, store: Any) -> None:
        """A grouped view must quote the phrase that tripped *that* gate, not
        whichever exclusion happened to fire first."""
        rows = [
            gate_row(1, Exclusion.CITIZENSHIP, quote="must be a US citizen"),
            gate_row(1, Exclusion.NO_SPONSORSHIP, quote="we do not sponsor visas"),
        ]
        store.save_screening(rows, summary=view_summary(rows))
        quotes = {
            gate: store.screened_page(gate, generation="gen-1", limit=5).rows[0].quote
            for gate in ("citizenship_or_itar_restricted", "employer_will_not_sponsor")
        }
        assert quotes == {
            "citizenship_or_itar_restricted": "must be a US citizen",
            "employer_will_not_sponsor": "we do not sponsor visas",
        }

    def test_the_internships_collection_is_its_own_population(self, store: Any) -> None:
        """318 postings hit the internship gate and 48 are software internships.
        Both numbers are stored, so the difference is legible rather than an
        off-by-270 bug."""
        rows = [
            gate_row(1, Exclusion.INTERNSHIP, quote="Intern"),
            gate_row(2, Exclusion.INTERNSHIP, quote="Marketing Intern"),
            gate_row(2, Exclusion.NOT_SWE, quote="Marketing Intern"),
            intern_row(1),
        ]
        summary = view_summary(rows)
        store.save_screening(rows, summary=summary)

        gated = store.screened_page("internship_not_full_time", generation="gen-1", limit=25)
        collection = store.screened_page(VIEW_INTERNSHIPS, generation="gen-1", limit=25)
        assert len(gated.rows) == 2
        assert [row.posting_id for row in collection.rows] == [post(1).id]
        # The reconciliation, asserted on the stored funnel and not just the pages.
        assert summary.gates["internship_not_full_time"] == 2
        assert summary.internship_total == 1

    def test_the_kept_view_holds_only_kept_postings(self, store: Any) -> None:
        rows = [kept_row(1), gate_row(2, Exclusion.LEVEL)]
        store.save_screening(rows, summary=view_summary(rows))
        page = store.screened_page(VIEW_KEPT, generation="gen-1", limit=25)
        assert all(row.kept for row in page.rows)
        assert all(row.gate == "" for row in page.rows)

    def test_a_posting_listed_twice_lands_in_the_view_once(self, store: Any) -> None:
        """A company re-listing a requisition gives two postings one id, and so one
        row key. SQLite would raise an IntegrityError and DynamoDB a
        ValidationException on the duplicate key — either way the whole publish fails
        and the site serves yesterday's view for a routine data shape.
        """
        rows = [kept_row(1), kept_row(1), kept_row(2)]
        store.save_screening(rows, summary=view_summary(rows))
        page = store.screened_page(VIEW_KEPT, generation="gen-1", limit=25)
        assert len(page.rows) == 2

    def test_a_non_utc_posted_at_comes_back_identically_from_both_stores(
        self, store: Any
    ) -> None:
        """``posted_at`` is derived from the sort key, not stored beside it.

        SQLite used to keep it in its own column, which preserved whatever offset the
        ATS sent, while DynamoDB read it out of the (UTC-normalised) sort key. The two
        answers compare **equal as instants**, which is exactly why the divergence
        would survive an equality assertion and then surface as ``09:00+05:00`` on the
        laptop and ``04:00+00:00`` in Lambda for the same posting.
        """
        tehran = datetime(2026, 7, 1, 9, 0, tzinfo=timezone(timedelta(hours=5)))
        row = kept_row(1, posted_at=tehran)
        store.save_screening([row], summary=view_summary([row]))
        [stored] = store.screened_page(VIEW_KEPT, generation="gen-1", limit=5).rows
        assert stored.posted_at == tehran
        assert stored.posted_at is not None
        assert stored.posted_at.utcoffset() == timedelta(0), "normalised, not as sent"
        assert stored.sort_key == row.sort_key

    def test_a_row_round_trips_every_field_it_declares(self, store: Any) -> None:
        """A dropped field is invisible until a card renders "unknown" for a level
        the screen decided, or an exclusion loses its quote."""
        row = ScreenedRow(
            posting_id=post(7).id,
            view="wrong_seniority_band",
            posted_at=DAY2,
            kept=False,
            level="mid",
            level_source="years",
            level_why="the description asks for 4+ years",
            eligibility_checked=False,
            sponsorship="will_not_sponsor",
            gate="wrong_seniority_band",
            reason="seniority band is mid, not entry-level",
            quote="4+ years of experience",
        )
        store.save_screening([row], summary=view_summary([row]))
        [stored] = store.screened_page(
            "wrong_seniority_band", generation="gen-1", limit=5
        ).rows
        assert stored == row

    def test_first_seen_is_not_declarable_by_the_caller_so_it_is_covered_below(
        self, store: Any
    ) -> None:
        """Guards the guard above, which cannot cover every field any more.

        ``first_seen`` is the one field the store writes rather than round-trips: no
        posting was synced in that case, so the row above comes back with ``None`` and an
        adapter that dropped the column entirely would still pass. :class:`TestFirstSeen`
        is what actually holds both stores to it, and this is the breadcrumb that says so
        — a field with no owner is how ``experience_level`` stayed broken in SQLite while
        an equality assertion passed.
        """
        row = kept_row(1, first_seen=DAY3)
        store.save_screening([row], summary=view_summary([row]))
        [stored] = store.screened_page(VIEW_KEPT, generation="gen-1", limit=5).rows
        assert stored.first_seen is None, "the corpus is empty, so the store knows nothing"
        assert stored == replace(row, first_seen=None), "every *other* field round-trips"


class TestFirstSeen:
    """When *this system* first saw a posting, published on every card.

    The bug: the page's "posted in the last 24 hours" chip reads ``posted_at``, the
    employer's own date, and showed **5** on a morning the cron reported **358 new**.
    ``posted_at`` spans 1 to 4,572 days on the live corpus — a company added to the
    watchlist today delivers 200 months-old-but-still-open roles, all of them new *to
    us* — so the two dates answer different questions and the page needs both.

    The field rides the materialised row so a read stays O(page): no lookup is added to
    a request, and the stamp on a card provably comes from the pass whose counts sit
    beside it. Every case here runs against both stores, because a stamp that exists on
    the laptop and not in Lambda is worse than no stamp at all.
    """

    def test_a_row_carries_the_moment_the_store_first_saw_the_posting(
        self, store: Any
    ) -> None:
        store.sync([post(1)], now=DAY1)
        rows = [kept_row(1)]
        store.save_screening(rows, summary=view_summary(rows))
        [stored] = store.screened_page(VIEW_KEPT, generation="gen-1", limit=5).rows
        assert stored.first_seen == DAY1

    def test_first_seen_is_ours_and_posted_at_is_the_employers(self, store: Any) -> None:
        """The gap this field exists for, in its production shape: an old-but-open role
        that we only met today. Collapsing the two would either report 47,550 roles as
        new on day one or, as the live page did, report 5 when 358 were.
        """
        old = post(1).model_copy(update={"posted_at": DAY1 - timedelta(days=4572)})
        store.sync([old], now=DAY2)
        rows = [ScreenedRow(
            posting_id=old.id,
            view=VIEW_KEPT,
            posted_at=old.posted_at,
            kept=True,
            level="entry",
            level_source="title",
            level_why="the title carries an entry marker",
            eligibility_checked=True,
            sponsorship="unstated",
        )]
        store.save_screening(rows, summary=view_summary(rows))
        [stored] = store.screened_page(VIEW_KEPT, generation="gen-1", limit=5).rows
        assert stored.posted_at == DAY1 - timedelta(days=4572)
        assert stored.first_seen == DAY2

    def test_a_role_seen_again_today_keeps_its_original_stamp(self, store: Any) -> None:
        """The failure mode that would make the whole field useless: every republish
        reporting the corpus as new. ``first_seen`` is set once on the posting, and the
        view copies *that*, not the run's clock — so a standing role stays old news
        through every pass that reprints it.
        """
        store.sync([post(1)], now=DAY1)
        store.sync([post(1)], now=DAY3)
        rows = [kept_row(1)]
        store.save_screening(rows, summary=view_summary(rows, generation="gen-2"))
        [stored] = store.screened_page(VIEW_KEPT, generation="gen-2", limit=5).rows
        assert stored.first_seen == DAY1

    def test_the_store_overrides_whatever_the_caller_put_on_the_row(
        self, store: Any
    ) -> None:
        """The store owns this column, like a database default owns its own.

        A caller that could set it could invent history — and the caller here is a pure
        screening pass whose only available timestamp is "now", which is exactly the
        wrong answer for all 2,569 kept roles.
        """
        store.sync([post(1)], now=DAY1)
        rows = [kept_row(1, first_seen=DAY3)]
        store.save_screening(rows, summary=view_summary(rows))
        [stored] = store.screened_page(VIEW_KEPT, generation="gen-1", limit=5).rows
        assert stored.first_seen == DAY1

    def test_a_posting_the_store_no_longer_has_is_stamped_null_not_refused(
        self, store: Any
    ) -> None:
        """The corpus moves under the publish: a posting can be closed and reaped
        between the screen reading it and the view being written. One missing stamp must
        cost that card's stamp — the port already says a missing row costs the row — and
        never the publish, which costs the whole page for a day.
        """
        store.sync([post(1)], now=DAY1)
        rows = [kept_row(1), kept_row(2)]  # 2 was never synced
        store.save_screening(rows, summary=view_summary(rows))
        stamps = {
            row.posting_id: row.first_seen
            for row in store.screened_page(VIEW_KEPT, generation="gen-1", limit=5).rows
        }
        assert stamps == {post(1).id: DAY1, post(2).id: None}

    def test_a_closed_posting_keeps_its_stamp_in_the_gate_views(self, store: Any) -> None:
        """``/excluded`` pages postings the worklist no longer lists, and its cards go
        through the same shaper. A stamp that only the kept view carried would make the
        page's "new to this list" section right and the excluded section quietly wrong.
        """
        store.sync([post(1)], now=DAY1)
        rows = [gate_row(1, Exclusion.CITIZENSHIP)]
        store.save_screening(rows, summary=view_summary(rows))
        [stored] = store.screened_page(
            Exclusion.CITIZENSHIP.value, generation="gen-1", limit=5
        ).rows
        assert stored.first_seen == DAY1

    def test_a_non_utc_clock_comes_back_identically_from_both_stores(
        self, store: Any
    ) -> None:
        """The divergence this field could have inherited from ``posted_at``.

        SQLite stores the corpus timestamp with whatever offset it was given while
        DynamoDB normalises every stamp to UTC, because its keys are compared as bytes.
        Those two compare **equal as instants**, which is exactly why the difference
        would survive an equality assertion and then surface as ``14:00+05:00`` on the
        laptop and ``09:00+00:00`` in Lambda for one posting. The view's copy is
        normalised on the way in, so the string on the wire is the same from both.
        """
        tehran = datetime(2026, 7, 1, 14, 0, tzinfo=timezone(timedelta(hours=5)))
        store.sync([post(1)], now=tehran)
        rows = [kept_row(1)]
        store.save_screening(rows, summary=view_summary(rows))
        [stored] = store.screened_page(VIEW_KEPT, generation="gen-1", limit=5).rows
        assert stored.first_seen == tehran
        assert stored.first_seen is not None
        assert stored.first_seen.isoformat() == "2026-07-01T09:00:00+00:00"


class TestHydratingAPage:
    def test_a_page_of_ids_becomes_postings(self, store: Any) -> None:
        store.sync([post(1), post(2), post(3)], now=DAY1)
        found = store.postings_by_id([post(2).id, post(1).id])
        assert set(found) == {post(1).id, post(2).id}
        assert found[post(2).id] == post(2)

    def test_an_unknown_id_is_absent_rather_than_fatal(self, store: Any) -> None:
        """The corpus moves under a reader: a posting can be reaped between the view
        being written and a page being served, and that must cost one row."""
        store.sync([post(1)], now=DAY1)
        found = store.postings_by_id([post(1).id, "deadbeefdeadbeef"])
        assert list(found) == [post(1).id]

    def test_a_closed_posting_still_hydrates(self, store: Any) -> None:
        """/excluded and POST /applied both address postings the worklist no longer
        lists. Filtering them here turns "this role closed" into "no such posting"."""
        store.sync([post(1), post(2)], now=DAY1)
        store.close_missing(now=DAY2, seen_ids={post(1).id})
        assert set(store.postings_by_id([post(1).id, post(2).id])) == {
            post(1).id,
            post(2).id,
        }

    def test_hydrating_nothing_reads_nothing(self, store: Any) -> None:
        assert store.postings_by_id([]) == {}


class TestSummaryEncoding:
    """Pure encoding rules — no store, because both stores share this code."""

    def test_a_summary_from_an_older_view_version_reads_as_absent(self) -> None:
        """A shape change must cost one stale day, not a page of plausible nonsense
        decoded out of fields that no longer mean what they did."""
        payload = summary_to_payload(view_summary([kept_row(1)]))
        payload["view_version"] = 0
        assert summary_from_payload(payload) is None

    def test_a_summary_missing_a_field_reads_as_absent(self) -> None:
        payload = summary_to_payload(view_summary([kept_row(1)]))
        del payload["kept"]
        assert summary_from_payload(payload) is None

    def test_a_healthy_summary_round_trips(self) -> None:
        summary = view_summary([kept_row(1), gate_row(2, Exclusion.LEVEL)])
        assert summary_from_payload(summary_to_payload(summary)) == summary

    def test_staleness_is_a_property_of_the_summary_not_of_the_reader(self) -> None:
        """A reader must be able to answer "is this current" without touching the
        corpus — counting the corpus per request is the cost this view removes."""
        summary = view_summary([kept_row(1)], screened_at=DAY1)
        assert summary.is_stale(DAY1 + timedelta(hours=12)) is False
        assert summary.is_stale(DAY1 + timedelta(hours=47)) is False
        assert summary.is_stale(DAY1 + timedelta(hours=49)) is True

    def test_a_view_stamped_in_the_future_is_stale(self) -> None:
        """Writer and reader clocks disagreeing must not publish a ``screenedAt``
        a reader cannot reconcile with anything."""
        summary = view_summary([kept_row(1)], screened_at=DAY3)
        assert summary.is_stale(DAY1) is True

    def test_the_overcount_flag_is_always_on_the_wire(self) -> None:
        """A UI that renders the funnel as a subtraction chain of gate counts lies:
        43,602 gate fires against 24,414 postings removed on a real run."""
        summary = view_summary(
            [gate_row(1, Exclusion.LEVEL), gate_row(1, Exclusion.CLEARANCE)]
        )
        assert summary.gate_counts_overcount is True
        assert summary.gate_count_total == 2
        assert summary.excluded == 1

    def test_an_evidence_quote_is_capped_and_whitespace_normalised(self) -> None:
        """The cap belongs where the row is written, not only where it is
        serialised: the public route publishes no description prose, and an
        uncapped quote is copied once per view the posting sits in."""
        assert cap_quote("  US citizens\n  only  ") == "US citizens only"
        long = cap_quote("x" * 500)
        assert len(long) == QUOTE_MAX_CHARS
        assert long.endswith("…")

    def test_the_undated_sentinel_sorts_below_every_real_stamp(self) -> None:
        """Sort keys compare as bytes, so the sentinel has to be lexicographically
        under 1970 as well as under 2026."""
        assert datetime(1970, 1, 1, tzinfo=UTC).isoformat() > UNDATED_SORT_STAMP
        assert DAY1.isoformat() > UNDATED_SORT_STAMP


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


class TestScreeningViewOnDynamo:
    """The parts of the view that only DynamoDB can get wrong."""

    def test_the_summary_is_the_last_write_of_the_publish(self) -> None:
        """The whole crash-safety argument is an ordering claim, and this is the only
        assertion that can observe ordering. If the summary went first, a run that
        died half-way would publish a generation whose rows were not all there."""
        store, table = dynamo()
        rows = [kept_row(n) for n in range(30)]
        store.save_screening(rows, summary=view_summary(rows))
        assert table.write_log[-1] == ("SCREEN#SUMMARY", "CURRENT")
        assert ("SCREEN#SUMMARY", "CURRENT") not in table.write_log[:-1]

    def test_rows_are_written_25_at_a_time(self) -> None:
        """84k single PutItems is the difference between seconds and an hour."""
        store, table = dynamo()
        rows = [kept_row(n) for n in range(60)]
        store.save_screening(rows, summary=view_summary(rows))
        assert table.batch_flushes == [25, 25, 10]

    def test_rows_spread_across_shards_instead_of_one_hot_partition(self) -> None:
        """``not_a_software_role`` holds ~45,000 of 47,538 postings. A single
        partition key takes at most 1,000 WCU/s no matter the table's capacity —
        DynamoDB splits a hot partition by key range, never a single key — so one
        unsharded view would spend ~45 s of the cron's 900 s budget on its own."""
        store, table = dynamo()
        rows = [gate_row(n, Exclusion.NOT_SWE) for n in range(200)]
        store.save_screening(rows, summary=view_summary(rows))
        partitions = {
            str(key[0]) for key in table.items if str(key[0]).startswith("SCREENVIEW#")
        }
        assert len(partitions) > 8

    def test_a_page_read_is_bounded_by_the_page_not_by_the_view(self) -> None:
        """The measurement that this change exists for. A page of 25 out of 500 rows
        must cost a fixed 16 shard queries — one per shard — not a walk of the view.
        An implementation that paged to exhaustion would pass every other test here
        and 504 in production."""
        store, table = dynamo()
        rows = [kept_row(n, posted_at=DAY1 + timedelta(minutes=n)) for n in range(500)]
        store.save_screening(rows, summary=view_summary(rows))
        table.queries = 0
        page = store.screened_page(VIEW_KEPT, generation="gen-1", limit=25)
        assert len(page.rows) == 25
        assert table.queries == 16, "one query per shard, regardless of view size"

    def test_reading_the_summary_is_one_get_not_a_query(self) -> None:
        """It is on the critical path of every request, so it is a primary-key read
        at a fixed key — not a query, and certainly not a count over the corpus."""
        store, table = dynamo()
        rows = [kept_row(1)]
        store.save_screening(rows, summary=view_summary(rows))
        table.gets = 0
        table.queries = 0
        assert store.screening_summary() is not None
        assert (table.gets, table.queries) == (1, 0)

    def test_a_page_still_fills_when_the_1mb_cut_lands_first(self) -> None:
        """DynamoDB applies the 1 MB cut *before* ``Limit``, so a single query is
        only guaranteed to return ``Limit`` items while rows stay small. Rows are
        ~540 bytes today; a future field that pushed them to 40 KB would silently
        start under-filling pages, and a short page reads as "that is all there is"."""
        store, _ = dynamo(page_size=2)
        rows = [kept_row(n, posted_at=DAY1 + timedelta(minutes=n)) for n in range(60)]
        store.save_screening(rows, summary=view_summary(rows))
        page = store.screened_page(VIEW_KEPT, generation="gen-1", limit=25)
        assert len(page.rows) == 25

    def test_every_row_carries_an_epoch_ttl(self) -> None:
        """Stale generations are reaped by TTL because DeleteItem costs what a write
        costs, and 84k deletes a day is real money for rows no reader can name. TTL
        only reads **epoch seconds** — an ISO string here is silently ignored and the
        rows live forever."""
        store, table = dynamo()
        rows = [kept_row(1)]
        summary = view_summary(rows, screened_at=DAY2)
        store.save_screening(rows, summary=summary)
        stored = [i for k, i in table.items.items() if str(k[0]).startswith("SCREENVIEW#")]
        assert len(stored) == 1
        expires = stored[0]["expires_at"]
        assert isinstance(expires, int)
        assert expires == int(DAY2.timestamp()) + adapter.VIEW_TTL_SECONDS

    def test_first_seen_is_read_from_the_index_not_with_a_get_per_row(self) -> None:
        """~84,900 rows a day. A GetItem per row to fetch one timestamp would be ~84,900
        round trips at ~5 ms — over an hour, inside a 900 s cron — and it would bill the
        whole 5.6 KB posting item each time to read 25 characters. The stamps come from
        one sweep of the KEYS_ONLY ``seen-index``, whose sort key already carries
        ``first_seen``: 16 queries flat, the same read ``close_missing`` performs earlier
        in the same run.
        """
        store, table = dynamo()
        postings = [post(n) for n in range(40)]
        store.sync(postings, now=DAY1)
        table.gets = 0
        table.queries = 0
        rows = [kept_row(n) for n in range(40)]
        store.save_screening(rows, summary=view_summary(rows))
        assert table.gets == 0, "a publish must not read one posting item per row"
        assert table.queries == 16, "one query per shard, and no more than one sweep"
        assert all(row.first_seen == DAY1 for row in
                   store.screened_page(VIEW_KEPT, generation="gen-1", limit=40).rows)

    def test_an_unknown_stamp_is_an_absent_attribute_and_reads_as_null(self) -> None:
        """This is also what every row published *before* the field existed looks like:
        DynamoDB stores nothing for an absent attribute, so yesterday's view decodes to
        ``firstSeen: null`` and the next pass fills it in. No migration, no backfill, and
        crucially no refusal — a version bump would have blanked the page for a day, which
        is how 400 interpretation rows once became permanently unreadable.
        """
        store, table = dynamo()
        rows = [kept_row(1)]  # nothing synced, so the store has no stamp to give
        store.save_screening(rows, summary=view_summary(rows))
        [item] = [i for k, i in table.items.items() if str(k[0]).startswith("SCREENVIEW#")]
        assert "first_seen" not in item
        [stored] = store.screened_page(VIEW_KEPT, generation="gen-1", limit=5).rows
        assert stored.first_seen is None

    def test_a_row_with_a_stamp_still_bills_one_wcu(self) -> None:
        """424 bytes on the mean measured, and a WCU is 1 KB: the field had to be free.
        Had it pushed a row over, the publish would have gone from ~84,900 WCU to
        ~169,800 — $0.106 to $0.212 a run — to carry one timestamp.
        """
        store, table = dynamo()
        store.sync([post(1)], now=DAY1)
        rows = [gate_row(1, Exclusion.CITIZENSHIP, quote="x" * QUOTE_MAX_CHARS)]
        store.save_screening(rows, summary=view_summary(rows))
        [item] = [i for k, i in table.items.items() if str(k[0]).startswith("SCREENVIEW#")]
        assert item["first_seen"] == DAY1.isoformat()
        assert item_size_bytes(item) < 1024

    def test_the_view_never_stores_a_description(self) -> None:
        """~2.5 KB a posting and ~118 MB across the corpus. Copying it into a
        structure that is written once per view a posting sits in would take each row
        from 1 WCU to 3 for data the page hydrates anyway."""
        store, table = dynamo()
        rows = [kept_row(1), gate_row(2, Exclusion.LEVEL)]
        store.save_screening(rows, summary=view_summary(rows))
        for key, item in table.items.items():
            if str(key[0]).startswith("SCREENVIEW#"):
                assert "description" not in item

    def test_two_postings_sharing_a_url_do_not_fail_the_publish(self) -> None:
        """A company re-listing a requisition gives two postings one id, so one view
        row key. Duplicate keys inside a BatchWriteItem are a ValidationException
        that fails the whole run."""
        store, _ = dynamo()
        rows = [kept_row(1), kept_row(1)]
        store.save_screening(rows, summary=view_summary(rows))
        page = store.screened_page(VIEW_KEPT, generation="gen-1", limit=25)
        assert len(page.rows) == 1

    def test_throttled_rows_are_retried_until_the_view_is_whole(self) -> None:
        """Partial throttling arrives as UnprocessedItems on an HTTP 200; code that
        reads a 200 as "written" publishes a summary over an incomplete view."""
        store, table = dynamo()
        table.throttle_every = 3
        rows = [kept_row(n, posted_at=DAY1 + timedelta(minutes=n)) for n in range(30)]
        store.save_screening(rows, summary=view_summary(rows))
        assert len(store.screened_page(VIEW_KEPT, generation="gen-1", limit=100).rows) == 30

    def test_the_view_is_not_visible_to_the_corpus_indexes(self) -> None:
        """View rows live in the base table beside the postings. If they leaked into
        seen-index or the open-index they would be counted as postings and screened
        as postings — and ``stats`` is what the run summary reports."""
        store, _ = dynamo()
        store.sync([post(1)], now=DAY1)
        rows = [kept_row(1), gate_row(1, Exclusion.LEVEL)]
        store.save_screening(rows, summary=view_summary(rows))
        assert store.stats() == {
            "total": 1, "open": 1, "closed": 0, "interpreted": 0, "applied": 0
        }
        assert len(store.open_postings()) == 1

    def test_hydrating_a_page_reads_one_item_per_posting_once(self) -> None:
        """Capped at the API's page size, so the round trips are bounded — and a
        duplicated id in a page must not be paid for twice."""
        store, table = dynamo()
        store.sync([post(1), post(2)], now=DAY1)
        table.gets = 0
        found = store.postings_by_id([post(1).id, post(2).id, post(1).id])
        assert len(found) == 2
        assert table.gets == 2

    def test_hydrating_is_one_get_item_per_posting_and_that_is_the_cost_to_beat(
        self,
    ) -> None:
        """The number this pins is not a page — it is what a *filtered* read costs.

        A page read is capped at ``MAX_LIMIT`` and so is bounded by construction. The
        ``ats`` and ``tier`` filters are not: ``matched`` means "across the whole
        collection", so the read walks the entire kept view and hydrates every row that
        survives the cheap filters. One GetItem per row, at a measured ~5 ms in-region,
        means the deployed kept view (~1,524 rows at 47,538 postings) costs ~7.6 s of
        sequential round trips *before* any scoring — which is why ``?tier=`` is
        expected to hit ``FILTER_SCAN_BUDGET_SECONDS`` and answer 503
        ``filter_scan_too_slow`` there while measuring only 4.2 s against local SQLite,
        where a hydrate is an in-process index lookup and costs nothing.

        Asserted rather than left as a comment so the arithmetic is checkable, and so
        the day someone replaces this with a 100-key BatchGetItem — the fix that turns
        1,524 round trips into 16 — this test is what tells them the win was real.
        """
        store, table = dynamo()
        postings = [post(n) for n in range(40)]
        store.sync(postings, now=DAY1)
        table.gets = 0
        found = store.postings_by_id([p.id for p in postings])
        assert len(found) == len(postings)
        assert table.gets == len(postings), (
            "one GetItem per posting; a batched read would make this smaller and "
            "would make the tier filter viable at the deployed corpus size"
        )


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
