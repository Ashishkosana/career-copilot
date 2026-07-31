"""DynamoDB-backed :class:`~copilot.ports.store.StorePort` — the v1 briefing table.

Single-table design, one partition per user (tenant isolation)::

    PK                SK                                 type
    USER#<user_id>    BRIEFING#<generated_at, UTC ISO>    briefing
    USER#<user_id>    JOB#<job_id>                        job

**The key attributes are ``PK`` and ``SK``, uppercase.** That is not a style
choice. The ``career-copilot`` table is declared with
``partitionKey: { name: "PK" }`` in ``infra/lib/career-copilot-stack.ts`` and
created with ``RemovalPolicy.RETAIN``, so its schema predates this adapter and
cannot be renamed without migrating the table. This module used to build items
and key conditions with lowercase ``pk``/``sk`` — copied from
:mod:`copilot.adapters.dynamodb_posting_store`, which is a *different table* that
genuinely does use lowercase — so **every** call in here failed against the real
service::

    ClientError: ValidationException ...
    BatchWriteItem: The provided key element does not match the schema

It surfaced as a crash in :meth:`save_jobs` only because that is the first of the
four to run in the daily pipeline; the writes, the reads and the queries were all
wrong together. Nothing had ever been written by this module, and
``GET /briefing`` had never returned a briefing. The whole class of bug is now
held down by three things: the two constants below are the single source of the
names, every attribute is referenced through an ``#alias`` (so the names are data
a test double can inspect), and the test suite checks those names against the CDK
declaration instead of against this file.

Why the ``USER#`` prefix, when the table already holds items keyed on a bare
Cognito ``sub``. The v1 prototype (``src/career_copilot/storage.py``, outside this
package) wrote 8 items as ``PK=<sub>``, ``SK=BRIEFING#<date>`` with a
``markdown``/``summary`` body and no ``payload`` — a shape :func:`item_to_briefing`
cannot parse. Dropping the prefix to "match the data" would put those rows inside
the range :meth:`latest_briefing` reads, and on any day with no v2 briefing yet
the read API would fetch one and raise ``KeyError: 'payload'``. The prefix is the
cheap version of a migration: the prototype's rows stay addressable and inert.

Briefings and jobs are stored as their pydantic ``model_dump(mode="json")``
payloads, so the mapping stays schema-driven and round-trips exactly. DynamoDB
represents every number as :class:`decimal.Decimal`; the ``_to_dynamo`` /
``_from_dynamo`` helpers make writes and reads Decimal-safe and are pure, so the
mapping is unit-tested without any AWS calls.
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from copilot.domain.models import Briefing, Job
from copilot.logging import get_logger

_LOG = get_logger("copilot.adapters.dynamodb_store")

#: Key attribute names of the deployed ``career-copilot`` table, confirmed with
#: ``aws dynamodb describe-table --table-name career-copilot``. Uppercase, unlike
#: the v2 postings table. Every item, key and key condition in this module is
#: built from these two names so there is exactly one place to be right, and the
#: tests assert them against ``infra/lib/career-copilot-stack.ts``.
PARTITION_KEY = "PK"
SORT_KEY = "SK"

BRIEFING_PREFIX = "BRIEFING#"
JOB_PREFIX = "JOB#"

#: ``#pk``/``#sk`` aliases for the two key attributes. Aliasing is not
#: decoration: DynamoDB's reserved-word list is long and a collision surfaces as
#: a ValidationException at runtime, in production, once — and the alias map is
#: also the only place a test double can *read* which attribute names a query
#: used, which is what turned "wrong key name" from a production incident into a
#: test failure.
_KEY_NAMES = {"#pk": PARTITION_KEY, "#sk": SORT_KEY}
#: The one key condition every read here needs: this user's partition, one item
#: kind (``BRIEFING#`` or ``JOB#``) out of it.
_KEY_CONDITION = "#pk = :pk AND begins_with(#sk, :sk)"


def _pk(user_id: str) -> str:
    return f"USER#{user_id}"


def _stamp(value: datetime) -> str:
    """UTC-normalised ISO-8601, for a timestamp that ends up inside a sort key.

    DynamoDB compares sort keys as bytes, so ``2026-07-06T23:00:00+05:30`` sorts
    *after* ``2026-07-06T18:00:00+00:00`` while being the earlier instant. Since
    the sort key is the entire definition of "latest briefing", a mixed offset
    would make :meth:`DynamoDbStore.latest_briefing` return the wrong day's
    briefing. Naive input is read as UTC: the pipeline always passes tz-aware
    UTC, this is a guard, not a feature. Identical output to a plain
    ``isoformat()`` for anything already tz-aware UTC, so it changes no key that
    the pipeline actually writes.
    """
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


def _to_dynamo(value: Any) -> Any:
    """Recursively convert a JSON-safe value into a DynamoDB-safe one.

    ``float`` is unsupported by DynamoDB and must be ``Decimal``; everything
    else (str/int/bool/None/list/dict) passes through unchanged.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, list):
        return [_to_dynamo(v) for v in value]
    if isinstance(value, dict):
        return {k: _to_dynamo(v) for k, v in value.items()}
    return value


def _from_dynamo(value: Any) -> Any:
    """Inverse of :func:`_to_dynamo`: turn ``Decimal`` back into int/float."""
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, list):
        return [_from_dynamo(v) for v in value]
    if isinstance(value, dict):
        return {k: _from_dynamo(v) for k, v in value.items()}
    return value


def briefing_to_item(user_id: str, briefing: Briefing) -> dict[str, Any]:
    """Map a :class:`Briefing` to a DynamoDB item (pure)."""
    return {
        PARTITION_KEY: _pk(user_id),
        SORT_KEY: f"{BRIEFING_PREFIX}{_stamp(briefing.generated_at)}",
        "type": "briefing",
        "user_id": user_id,
        "day": briefing.day.isoformat(),
        "payload": _to_dynamo(briefing.model_dump(mode="json")),
    }


def item_to_briefing(item: dict[str, Any]) -> Briefing:
    """Rebuild a :class:`Briefing` from a stored item (pure).

    Raises rather than returning ``None`` on an item it cannot parse: the read
    API turns ``None`` into a 404 "no briefing yet", which is a lie a user cannot
    distinguish from an unwritten briefing, while an exception is a 500 with a
    stack trace pointing at the row.
    """
    return Briefing.model_validate(_from_dynamo(item["payload"]))


def job_to_item(user_id: str, job: Job) -> dict[str, Any]:
    """Map a :class:`Job` to a DynamoDB item (pure)."""
    return {
        PARTITION_KEY: _pk(user_id),
        SORT_KEY: f"{JOB_PREFIX}{job.id}",
        "type": "job",
        "user_id": user_id,
        "job_id": job.id,
        "payload": _to_dynamo(job.model_dump(mode="json")),
    }


def item_to_job(item: dict[str, Any]) -> Job:
    """Rebuild a :class:`Job` from a stored item (pure)."""
    return Job.model_validate(_from_dynamo(item["payload"]))


class DynamoDbStore:
    """StorePort backed by a DynamoDB table (boto3 imported lazily).

    A ``table`` resource may be injected (tests / reuse); otherwise it is
    created on first use so importing this module needs no AWS credentials.
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
        #: Writes this instance swallowed, newest last — see :meth:`_contained`.
        #: The seam a caller uses to report "the corpus is fine but the digest did
        #: not persist" without the port having to grow a return value.
        self.write_errors: list[str] = []

    @property
    def table(self) -> Any:
        if self._table is None:
            import boto3

            self._table = boto3.resource("dynamodb", region_name=self._region).Table(
                self._table_name
            )
        return self._table

    @contextmanager
    def _contained(self, operation: str, *, items: int) -> Iterator[None]:
        """Log a failed write as ``briefing_store_write_failed`` instead of raising.

        Both writes here are the *last two statements* of the daily run, after the
        posting corpus has already been committed, and the EventBridge rule is
        ``retryAttempts: 0`` because the run is not idempotent in the mailbox — a
        retry re-sends the digest. So an exception from either write used to
        destroy an otherwise successful sweep and, worse, skip the briefing email
        and the drafts that come after it. Losing the day's mail over the day's
        *record of* the mail is the wrong trade, and it is the same trade the
        inbox half already makes.

        Contained does not mean quiet, because silence is precisely how the
        lowercase-key bug survived: this logs at ERROR with the traceback and a
        stable ``message`` field, which is the shape ``MonitoringStack`` already
        builds CloudWatch metric filters on (``{ $.message = "..." }``), so the
        infra owner can alarm on it without a code change. The failure is also
        recorded on :attr:`write_errors` for the caller.

        Two deliberate limits. Only the service call is wrapped — every item is
        built *before* the ``with`` block, so a mapping bug still raises and still
        fails a test. And the reads are not wrapped at all: a contained
        :meth:`latest_briefing` would answer "no briefing yet" for what is really
        an outage, and the read API would serve that 404 to the user.
        """
        try:
            yield
        except Exception as exc:
            self.write_errors.append(f"{operation}: {type(exc).__name__}: {exc}")
            _LOG.error(
                "briefing_store_write_failed",
                exc_info=True,
                extra={
                    "extra_fields": {
                        "operation": operation,
                        "table": self._table_name,
                        "items": items,
                        # The key names this adapter used. A ValidationException
                        # about the schema is unreadable without them.
                        "key_attributes": [PARTITION_KEY, SORT_KEY],
                        "error": type(exc).__name__,
                    }
                },
            )

    def save_briefing(self, user_id: str, briefing: Briefing) -> None:
        item = briefing_to_item(user_id, briefing)
        with self._contained("save_briefing", items=1):
            self.table.put_item(Item=item)

    def latest_briefing(self, user_id: str) -> Briefing | None:
        resp = self.table.query(
            KeyConditionExpression=_KEY_CONDITION,
            ExpressionAttributeNames=dict(_KEY_NAMES),
            ExpressionAttributeValues={":pk": _pk(user_id), ":sk": BRIEFING_PREFIX},
            ScanIndexForward=False,  # newest sort key first
            Limit=1,
        )
        items = resp.get("Items", [])
        if not items:
            return None
        return item_to_briefing(items[0])

    def seen_job_ids(self, user_id: str) -> frozenset[str]:
        """Every job id already surfaced to this user, for dedup.

        The id is sliced out of the sort key rather than read from the ``job_id``
        attribute. The sort key *is* the identity, it is always present, and it is
        already projected for free — reading a non-key attribute instead means a
        row written without it (the v1 prototype wrote none) comes back as an
        empty projection and raises ``KeyError`` mid-page.
        """
        ids: set[str] = set()
        start_key: dict[str, Any] | None = None
        while True:
            kwargs: dict[str, Any] = {
                "KeyConditionExpression": _KEY_CONDITION,
                "ExpressionAttributeNames": dict(_KEY_NAMES),
                "ExpressionAttributeValues": {":pk": _pk(user_id), ":sk": JOB_PREFIX},
                "ProjectionExpression": "#sk",
            }
            if start_key is not None:
                kwargs["ExclusiveStartKey"] = start_key
            resp = self.table.query(**kwargs)
            ids.update(str(i[SORT_KEY]).removeprefix(JOB_PREFIX) for i in resp.get("Items", []))
            # A query stops at 1 MB whether or not you asked it to; without this
            # loop a long-running user's dedup set silently truncates and old jobs
            # start reappearing as "new today".
            start_key = resp.get("LastEvaluatedKey")
            if not start_key:
                break
        return frozenset(ids)

    def save_jobs(self, user_id: str, jobs: list[Job]) -> None:
        items = [job_to_item(user_id, job) for job in jobs]
        with (
            self._contained("save_jobs", items=len(items)),
            # overwrite_by_pkeys is not an optimisation: duplicate keys inside one
            # BatchWriteItem request are a ValidationException that fails the whole
            # request, and two entries in one briefing can share a job id (the same
            # posting reached the shortlist twice). De-duplicating in the writer
            # keeps that a no-op instead of a lost day of jobs.
            self.table.batch_writer(overwrite_by_pkeys=[PARTITION_KEY, SORT_KEY]) as batch,
        ):
            for item in items:
                batch.put_item(Item=item)
