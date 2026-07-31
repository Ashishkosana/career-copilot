"""Read API for the v2 worklist — what to apply to, and what was filtered out and why.

The website currently ships a baked-in snapshot of one pipeline run. These five
endpoints replace it with live reads:

* ``GET /worklist``      every eligible full-time posting, newest first, scored,
                         paginated.
* ``GET /worklist/{id}`` one posting with its description and screening verdict.
* ``GET /internships``   the internships collection: same cards, same scores, its
                         own heading, deliberately *not* mixed into the worklist.
* ``GET /excluded``      the trust surface: what was dropped, grouped by gate, with
                         the sentence that caused each drop quoted back.
* ``POST /applied``      record that a human applied. **Records only.**

Every route here is behind the Cognito authorizer. ``handlers/public_api.py`` wraps
the four read routes in a sanitising projection for the unauthenticated page; it
calls the functions below rather than re-serialising anything, because two
serialisers for the same data is how a private field reaches a public page.

Six decisions here are load-bearing:

**A read consumes the materialised screening view. It never screens the corpus.**
This module used to call ``store.open_postings()`` and run the whole funnel on every
request, and in production that meant:

    read 25,294 rows: 1.7 s    screen: 37.8 s    total 39.4 s

At the deployed size — 47,538 open postings, 1.506 ms of screening each — that is
~72 s of CPU against an API Gateway REST integration ceiling of **29 s, hard and
non-negotiable**. Every single request 504'd, including ``?limit=1``, because
``limit`` is applied *after* screening: ``eligibleTotal`` and the funnel describe the
whole set. Memory peaked at 496 MB of 3008, so no amount of memory could have fixed
it. The cron already screens the corpus once a day, so it now writes the verdict down
(``ports/postingstore.py``) and a read is: one summary item → one keyset page of one
view → hydrate that page → score that page. Every total comes from the summary, so no
count touches the corpus. Measured on the same 25,294-posting corpus, driven through
synthetic gateway events:

    GET /excluded (all 7 gates)   1.7 ms       GET /worklist?limit=25   134 ms
    GET /excluded?gate=<22,074>   1.9 ms       GET /worklist?limit=100  514 ms
    GET /worklist/{id}            8.2 ms       not screened yet        0.01 ms

The two numbers worth reading together are 1.9 ms and 134 ms. The 1.9 ms is the whole
cost of the store work — a page of 100 out of the largest view, 22,074 rows, and it is
the *same* 1.9 ms for a view of 48. Nothing here scales with the corpus. The 134 ms is
almost entirely **scoring**: 5.1 ms per posting against a 5 KB résumé, times the page
size, which is why the read scales with ``limit`` (capped at 100) and with nothing else.
Worst case is 514 ms, 56x under the ceiling; before this change the best case was 39 s,
1.4x over it.

Screening is materialised and **scoring is not**, because they depend on different
things. Whether a posting is a software role, an internship, a vendor demo board, the
wrong band or barred by clearance is a fact about the posting. A score is a fact about
the posting *and the résumé*, the résumé changes independently of the corpus, and a
baked score would silently misrank the day it changed. So a score is computed here,
over one page of ~25 rather than 47,538.

**"Not screened yet" is an answer, not an outage.** When there is no usable summary,
or it is too old to present as current, these routes answer immediately with 503 and a named
state — :data:`NOT_SCREENED` or :data:`VIEW_STALE` — and they do **not** fall back to
screening live. That fallback is exactly what 504s, and a request that hangs for 29 s
and dies is worse than one that says "not ready", because a timeout is
indistinguishable from an outage. ``GET /worklist/{id}`` is the exception and needs no
view at all: it re-screens that one posting, which is 1.5 ms.

**Pagination is keyset, not offset.** The worklist is 880 rows and the postings
underneath it close while a human reads. An offset page 2 taken after a row closes
silently *skips* a posting — the one that slid into the boundary. The opaque cursor
carries the sort key of the last row returned, so a page boundary is anchored to a
posting rather than to a count, and it stays meaningful across the nightly republish
for the same reason (the generation is deliberately **not** in the cursor: folding it
in would refuse every in-flight cursor once a day). The cursor does carry a
fingerprint of the filter set, because a cursor is only meaningful inside one
ordering and honouring a foreign one would return a page that repeats or skips rows
without saying so.

**The score is always sent with its components.** ``gap.score_report`` reports a
set and a named tier because competitor match percentages were found to be mutually
incomparable; serialising ``total`` alone would throw away exactly the part that
makes the number defensible (the denominator comes from the posting).

**A missing description is never an empty string.** Workday's list endpoint returns
no description at all, and ``""`` matches no exclusion pattern — which is why
``Posting.desc_available`` and ``Eligibility.checked`` exist. The wire keeps that
distinction: ``description`` is ``null`` with a ``descriptionStatus`` saying why, and
a verdict whose gates could not run says so instead of reading as "clean".

**Internships are a separate collection, not a filter.** ``screen`` gates them out
of the worklist because this search exists to obtain full-time work authorisation,
but Fall/Summer internship pipelines at large employers do convert, so they get
their own addressable population — materialised as its own view by re-screening the
postings the internship gate removed with ``include_internships=True``, so every
*other* gate (vendor demo boards, seniority, clearance, citizenship, sponsorship)
still applies. The primary pass is left alone on purpose: it is what produces the
funnel counts and the ``/excluded`` evidence, and both are part of the trust surface.

That is also why the section is **48 rows and not 318**: the internship gate fires
318 times, but 264 of those postings are not software roles at all ("Marketing
Intern", "Finance Co-op"), 12 sit on ATS vendor demo boards, and a handful require a
clearance or a citizenship this search cannot satisfy. Both numbers are on the wire
in every response — ``internshipTotal`` and ``funnel.gates.internship_not_full_time``
— so the difference is visible rather than looking like an off-by-270 bug.

**Nothing here submits anything.** ``POST /applied`` writes one timestamp through
``PostingStorePort.mark_applied`` and returns. There is no HTTP client in this module
and no code path that could acquire one; ``test_worklist_api`` asserts that
structurally, because "it never auto-applies" is a product promise, not a comment.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

from copilot.adapters.dynamodb_posting_store import DynamoDbPostingStore
from copilot.adapters.sqlite_posting_store import SqlitePostingStore
from copilot.config import Settings, load_settings
from copilot.domain.gap import GapReport, Score, Tier, build_report, score_report
from copilot.domain.posting import Posting
from copilot.domain.screening import Exclusion, ScreenDecision, is_internship, screen
from copilot.domain.seniority import Level, LevelSource
from copilot.handlers.api import user_id_from_event
from copilot.logging import get_logger
from copilot.ports.postingstore import (
    VIEW_INTERNSHIPS,
    VIEW_KEPT,
    PostingStorePort,
    ScreenedPage,
    ScreenedRow,
    ScreenSummary,
    sort_stamp,
)

log = get_logger("copilot.handlers.worklist_api")

CORS_HEADERS: Final[dict[str, str]] = {
    "Content-Type": "application/json",
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type,Authorization",
}

#: Page sizes. There is no "give me everything" option: the eligible worklist is
#: 880 rows today and the excluded list is 24,414, and a UI that asks for all of it
#: gets a 400 rather than a response that works until the corpus grows.
DEFAULT_LIMIT: Final = 25
MAX_LIMIT: Final = 100
#: /excluded returns every gate at once, so its default per-group page is smaller.
DEFAULT_GROUP_LIMIT: Final = 10

#: Evidence quotes are excerpts shown to explain a filtering decision. Capped
#: because republishing description prose in bulk is a different thing entirely —
#: and Workday's terms forbid it.
QUOTE_MAX_CHARS: Final = 180

#: Bumped if the cursor payload ever changes shape, so an in-flight cursor from an
#: older deploy is rejected instead of being misread.
CURSOR_VERSION: Final = 1

#: The addressable collections of postings. A collection is a *population*, not a
#: filter over one: ``/worklist`` and ``/internships`` answer different questions
#: over disjoint sets of rows, which is why each is a path (like ``/excluded``)
#: rather than a query parameter. The name also travels in the cursor fingerprint,
#: so a cursor taken from one collection cannot continue paging through the other.
COLLECTION_WORKLIST: Final = "worklist"
COLLECTION_INTERNSHIPS: Final = "internships"

#: Which materialised view backs which collection.
_VIEW_FOR_COLLECTION: Final[dict[str, str]] = {
    COLLECTION_WORKLIST: VIEW_KEPT,
    COLLECTION_INTERNSHIPS: VIEW_INTERNSHIPS,
}

#: How long a warm Lambda container may reuse the view's summary.
SUMMARY_TTL_SECONDS: Final = 300.0

#: Rows per store page while walking a view to apply filters. Larger than a response
#: page because these reads are not what a caller sees: on DynamoDB one call is 16
#: bounded shard queries whatever the size, so fewer, fatter calls is strictly less
#: round-tripping — and 200 rows is 85 KB per shard, an order of magnitude under the
#: 1 MB query cut-off that would silently under-fill a page.
SCAN_PAGE_ROWS: Final = 200

#: The most rows a *filtered* read may walk. Only the two collection views are ever
#: filterable — the kept set (811 measured, ~1,520 at the deployed corpus size) and the
#: internships set (48) — so this is a guard rather than a limit, and it is checked
#: against the summary's own total *before* any work, so an oversized view is refused
#: in microseconds. It exists because the whole point of this rewrite is that no read
#: grows with the corpus, and "the filtered branch quietly still does" is exactly the
#: shape of the bug that 504'd. Measured cost of a walk that does not score: 0.017 ms
#: per row (the range query plus a batched hydrate), so 20,000 rows is ~0.34 s.
FILTER_SCAN_MAX_ROWS: Final = 20_000

#: Wall-clock ceiling on a filtered walk, checked after every store page.
#:
#: A row bound alone would be tidier and deterministic, and it is not enough: it is a
#: *proxy* for time, and the proxy's constant is not stable. A ``tier`` filter has to
#: score every candidate, and scoring is **5.1 ms per posting** measured against the
#: real 25,294-posting corpus (2.2 ms against a short description; it scales with both
#: the description and the 5 KB résumé). So the same 20,000 rows cost 0.34 s for an
#: ``ats`` filter and would cost 100 s for a ``tier`` one — and the day the résumé grows
#: or ``domain/gap`` gets slower, a row bound silently stops protecting anything.
#:
#: This is the invariant itself rather than a proxy for it: the gateway cuts the
#: integration at 29 s, and a walk that has spent this long gives up with a named,
#: retryable answer. It is checked between pages, so the overshoot is at most one page
#: (measured: a 0.2 s budget refuses after 1.12 s, being 200 rows of scoring), which
#: leaves ~16 s in hand.
#:
#: **The local measurement is not the deployed one, and the gap is hydration.** Against
#: the local SQLite corpus ``?tier=strong`` over 811 kept postings measures 4.2 s, and
#: essentially all of it is scoring, because a hydrate there is an in-process index
#: lookup. On DynamoDB every candidate row costs its own GetItem —
#: ``postings_by_id`` is one GetItem per id, deliberately (see that method) — so the
#: deployed kept view of ~1,524 rows adds ~7.6 s of sequential round trips at ~5 ms
#: each to ~7.8 s of scoring: ~16 s, which this budget **refuses**. So ``?tier=`` is
#: expected to answer 503 :data:`SCAN_TOO_SLOW` in production until hydration is
#: batched (100 keys per BatchGetItem turns 1,524 round trips into 16). That is the
#: intended failure: one optional filter degrades to a named, retryable, *bounded*
#: answer instead of the whole endpoint spending 29 s and dying. ``?ats=`` pays the
#: same round trips without the scoring, ~8 s, and passes.
FILTER_SCAN_BUDGET_SECONDS: Final = 12.0

#: Belt to the two bounds above: a store whose ``next_token`` never went ``None`` would
#: otherwise loop forever inside one request.
_MAX_SCAN_PAGES: Final = FILTER_SCAN_MAX_ROWS // SCAN_PAGE_ROWS + 2

#: A filtered read over a view too large to count exactly. 400 and not 503: a retry
#: cannot help, the caller has to drop the filter and page instead.
TOO_MANY_ROWS: Final = "too_many_rows_to_filter"
#: A filtered walk that ran out of :data:`FILTER_SCAN_BUDGET_SECONDS`. 503 and not 400,
#: because unlike the bound above this one *can* come out differently next time — a
#: warm page cache or an unthrottled table is the difference — so a retry is honest
#: advice rather than a loop.
SCAN_TOO_SLOW: Final = "filter_scan_too_slow"

#: The corpus has never been screened, or the only view on it was written by a deploy
#: with a different record shape. Either way there is nothing to serve and saying so
#: costs one cheap read.
NOT_SCREENED: Final = "corpus_not_screened"
#: A view exists but is too old to present as current (or is stamped in the future,
#: which means the writer's clock and ours disagree). Refused rather than served
#: quietly: the transport has no channel for "shown, but three days old", and a stale
#: view being served silently is how a dead cron goes unnoticed.
VIEW_STALE: Final = "screening_view_stale"

DESC_AVAILABLE: Final = "available"
#: The source returned no description at all (Workday's list endpoint, some Lever).
DESC_NOT_PROVIDED: Final = "not_provided_by_source"
#: The source claimed a description and returned nothing in it — a data fault worth
#: seeing, and not the same thing as never having offered one.
DESC_EMPTY: Final = "empty_from_source"

#: ``/worklist/{id}`` needs both segments before the tail can be read as an id.
_COLLECTION_AND_ID: Final = 2
_TRUE_WORDS: Final = frozenset({"true", "1", "yes"})
_FALSE_WORDS: Final = frozenset({"false", "0", "no"})
_MARKUP = re.compile(r"<[^>]+>")

_TIERS_BY_NAME: Final[dict[str, Tier]] = {
    **{tier.value: tier for tier in Tier},
    # "exact match" has a space in it; a query string should not have to.
    "exact": Tier.EXACT,
    "exact_match": Tier.EXACT,
    "exact-match": Tier.EXACT,
}
_LEVELS_BY_NAME: Final[dict[str, Level]] = {level.value: level for level in Level}
_GATES_BY_NAME: Final[dict[str, Exclusion]] = {gate.value: gate for gate in Exclusion}
_ELIGIBILITY_EVIDENCE_KEY: Final[dict[Exclusion, str]] = {
    Exclusion.CLEARANCE: "clearance",
    Exclusion.CITIZENSHIP: "citizenship",
    Exclusion.NO_SPONSORSHIP: "no_sponsorship",
}


# ---------------------------------------------------------------------------
# Transport: envelope + errors. Shapes match handlers/api.py exactly — success is
# a bare JSON object, failure is ``{"error": "<code>"}`` and nothing else.
# ---------------------------------------------------------------------------

def _response(status: int, body: dict[str, Any]) -> dict[str, Any]:
    return {"statusCode": status, "headers": dict(CORS_HEADERS), "body": json.dumps(body)}


def _error(status: int, code: str) -> dict[str, Any]:
    return _response(status, {"error": code})


def _preflight() -> dict[str, Any]:
    """CORS preflight. 204 carries no body, so it does not go through ``_response``."""
    return {"statusCode": 204, "headers": dict(CORS_HEADERS), "body": ""}


class ApiError(Exception):
    """A request that cannot be served, carrying the status and error code.

    Raised by the parsers so a bad query parameter fails at the point it is read
    rather than being coerced into a default — a silently clamped ``limit=1000``
    looks like it worked, which is worse than a 400.
    """

    def __init__(self, status: int, code: str) -> None:
        super().__init__(code)
        self.status = status
        self.code = code

    def response(self) -> dict[str, Any]:
        return _error(self.status, self.code)


class StoreUnavailable(ApiError):
    """The store raised. Surfaced as 503 so a caller can retry the same request."""

    def __init__(self) -> None:
        super().__init__(503, "store_unavailable")


class ViewNotReady(ApiError):
    """There is no materialised screening view a read can honestly serve.

    A first-class answer, not a bug report. It is 503 because it is *retryable* — the
    cron runs daily and the correct client behaviour is to come back — and because a
    200 carrying zeroes would be the one genuinely dangerous response: "0 eligible of
    0 screened" reads as "the search found nothing today", which is a different fact
    and looks fine on a page.

    The code is the machine-readable state the page renders as a sentence, and it is
    the only thing that crosses the public projection (an error body is
    ``{"error": code}`` and nothing else), so the state has to live *in* the code
    rather than in a field beside it. Errors are ``no-store``, so a "not ready" cannot
    outlive the cron run that fixes it.
    """

    def __init__(self, code: str) -> None:
        super().__init__(503, code)


# ---------------------------------------------------------------------------
# Ordering
# ---------------------------------------------------------------------------

def _as_utc(value: datetime) -> datetime:
    """Normalise a posting timestamp to an aware UTC datetime.

    Nothing in ``Posting`` forces a timezone, and the date filters compare a stored
    stamp against a parsed query parameter — so one naive value on either side raises
    ``TypeError`` mid-request. Naive input is read as UTC, matching
    ``ports.postingstore.sort_stamp``, which is what put the value in the sort key.
    """
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


# ---------------------------------------------------------------------------
# Cursor
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Cursor:
    """An opaque page boundary: the sort key of the last row already returned."""

    posted_at: datetime | None
    posting_id: str

    @property
    def store_token(self) -> str:
        """The same boundary in the store's own terms.

        Built with :func:`~copilot.ports.postingstore.sort_stamp` rather than by
        formatting ``posted_at`` here, because that function is what the *writer* used
        to build the key this token is compared against. Sort keys compare as bytes, so
        a second formatter — one that left ``+05:00`` where the key holds ``+00:00`` —
        would page from the wrong place rather than fail.
        """
        return f"{sort_stamp(self.posted_at)}#{self.posting_id}"


def _encode_cursor(row: ScreenedRow, *, fingerprint: str) -> str:
    payload = {
        "v": CURSOR_VERSION,
        "ts": row.posted_at.isoformat() if row.posted_at else None,
        "id": row.posting_id,
        "f": fingerprint,
    }
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(raw: str, *, fingerprint: str) -> Cursor:
    """Decode a cursor, or fail with a 400.

    ``binascii.Error``, ``UnicodeDecodeError`` and ``JSONDecodeError`` are all
    ``ValueError`` subclasses, so one except clause covers every way a hand-edited
    cursor can be malformed.
    """
    padded = raw + "=" * (-len(raw) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(padded.encode()))
    except ValueError as exc:
        raise ApiError(400, "invalid_cursor") from exc

    if not isinstance(payload, dict) or payload.get("v") != CURSOR_VERSION:
        raise ApiError(400, "invalid_cursor")
    posting_id = payload.get("id")
    if not isinstance(posting_id, str) or not posting_id:
        raise ApiError(400, "invalid_cursor")
    if payload.get("f") != fingerprint:
        # Page 2 of a *different* question. Continuing would skip or repeat rows.
        raise ApiError(400, "cursor_filter_mismatch")

    stamp = payload.get("ts")
    if stamp is None:
        return Cursor(None, posting_id)
    if not isinstance(stamp, str):
        raise ApiError(400, "invalid_cursor")
    return Cursor(_parse_moment(stamp, code="invalid_cursor"), posting_id)


def _page_wire(count: int, *, limit: int, next_cursor: str | None) -> dict[str, Any]:
    return {
        "limit": limit,
        "count": count,
        "nextCursor": next_cursor,
        # One source of truth: there is a next page exactly when there is a cursor.
        "hasMore": next_cursor is not None,
    }


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class WorklistFilters:
    """The query the caller asked. Echoed back so a UI can render its own state.

    There is deliberately **no default age window**. The user wants postings that
    are months or years old to stay findable — a role that has been open since
    February is still open — so age is only ever narrowed on request.
    """

    tiers: frozenset[Tier] = frozenset()
    levels: frozenset[Level] = frozenset()
    sources: frozenset[str] = frozenset()
    posted_after: datetime | None = None
    posted_before: datetime | None = None
    include_undated: bool = True
    #: Which population the filters were applied to. Not part of :meth:`as_wire`,
    #: because it is not something the caller narrowed — but it *is* part of the
    #: fingerprint, since the same filters over a different collection are a
    #: different ordering and so a different pagination.
    collection: str = COLLECTION_WORKLIST

    def as_wire(self) -> dict[str, Any]:
        return {
            "tier": sorted(tier.value for tier in self.tiers) or None,
            "level": sorted(level.value for level in self.levels) or None,
            "ats": sorted(self.sources) or None,
            "postedAfter": self.posted_after.isoformat() if self.posted_after else None,
            "postedBefore": self.posted_before.isoformat() if self.posted_before else None,
            "includeUndated": self.include_undated,
        }

    @property
    def narrows_rows(self) -> bool:
        """Whether anything here can be decided from a stored row alone.

        The seniority band and the posting date are *on* the row, so these filters
        cost nothing beyond the walk. Split from :attr:`narrows_postings` because that
        is the difference between hydrating one page and hydrating a whole view.
        """
        return bool(self.levels) or self.posted_after is not None or (
            self.posted_before is not None
        ) or not self.include_undated

    @property
    def narrows_postings(self) -> bool:
        """Whether answering this needs the posting, not just its screening row.

        ``ats`` is a field of the posting and a tier is a fact about the posting *and*
        the résumé; neither is in the view, deliberately — see
        :class:`~copilot.ports.postingstore.ScreenedRow` for why a row carries only
        what its own view renders.
        """
        return bool(self.sources) or bool(self.tiers)

    @property
    def narrows(self) -> bool:
        return self.narrows_rows or self.narrows_postings

    @property
    def fingerprint(self) -> str:
        """Identifies the question a cursor belongs to (not a secret, just a tag).

        The collection is folded in, not just the filters: without it a cursor from
        ``/worklist`` would be accepted by ``/internships`` under the same filters
        and page into the middle of a list it was never taken from.

        The view's *generation* is deliberately **not** folded in. A cursor is an
        anchor to a posting, not to a pass, and it stays correct across the nightly
        republish for the same reason keyset paging is used at all — whereas including
        it would refuse every in-flight "load more" once a day.
        """
        raw = json.dumps(
            {"collection": self.collection, "filters": self.as_wire()},
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha1(raw.encode()).hexdigest()[:12]


def _row_matches(filters: WorklistFilters, row: ScreenedRow) -> bool:
    """The filters a stored row can answer: seniority band and posting date."""
    if filters.levels and row.level not in {level.value for level in filters.levels}:
        return False
    if row.posted_at is None:
        return filters.include_undated
    when = _as_utc(row.posted_at)
    if filters.posted_after is not None and when < filters.posted_after:
        return False
    return not (filters.posted_before is not None and when > filters.posted_before)


def _posting_matches(filters: WorklistFilters, item: Screened) -> bool:
    """The filters that need the hydrated posting. Tier is applied separately."""
    return not (filters.sources and item.posting.ats.lower() not in filters.sources)


# ---------------------------------------------------------------------------
# Query / body parsing
# ---------------------------------------------------------------------------

def _query_values(event: Mapping[str, Any], name: str) -> list[str]:
    """All values for one parameter, accepting repeats *and* comma-separated lists.

    API Gateway exposes repeats only in ``multiValueQueryStringParameters`` (REST)
    and collapses them in the HTTP API, so both shapes have to be read or a
    ``?level=entry&level=intern`` filter silently loses a value.
    """
    multi = event.get("multiValueQueryStringParameters")
    raw: list[str] = []
    if isinstance(multi, dict) and multi.get(name) is not None:
        raw = [str(item) for item in multi[name]]
    else:
        single = event.get("queryStringParameters")
        if isinstance(single, dict) and single.get(name) is not None:
            raw = [str(single[name])]
    values: list[str] = []
    for chunk in raw:
        values.extend(part.strip() for part in chunk.split(",") if part.strip())
    return values


def _query_one(event: Mapping[str, Any], name: str) -> str | None:
    """Last value wins, matching how browsers and gateways treat duplicates."""
    values = _query_values(event, name)
    return values[-1] if values else None


def _parse_limit(event: Mapping[str, Any], *, default: int) -> int:
    raw = _query_one(event, "limit")
    if raw is None:
        return default
    try:
        limit = int(raw)
    except ValueError as exc:
        raise ApiError(400, "invalid_limit") from exc
    if not 1 <= limit <= MAX_LIMIT:
        # Refused, not clamped: "limit=880" must not appear to have worked.
        raise ApiError(400, "invalid_limit")
    return limit


def _parse_moment(raw: str, *, code: str = "invalid_date") -> datetime:
    text = raw.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ApiError(400, code) from exc
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _parse_bool(raw: str) -> bool:
    lowered = raw.strip().lower()
    if lowered in _TRUE_WORDS:
        return True
    if lowered in _FALSE_WORDS:
        return False
    raise ApiError(400, "invalid_boolean")


def _parse_named[T](
    event: Mapping[str, Any], name: str, table: Mapping[str, T], code: str
) -> frozenset[T]:
    """Map query values onto a closed vocabulary; an unknown value is a 400, not a silent drop."""
    chosen: set[T] = set()
    for raw in _query_values(event, name):
        found = table.get(raw.strip().lower())
        if found is None:
            raise ApiError(400, code)
        chosen.add(found)
    return frozenset(chosen)


def _parse_filters(
    event: Mapping[str, Any], *, collection: str = COLLECTION_WORKLIST
) -> WorklistFilters:
    posted_after = _query_one(event, "postedAfter")
    posted_before = _query_one(event, "postedBefore")
    after = _parse_moment(posted_after) if posted_after else None
    before = _parse_moment(posted_before) if posted_before else None

    raw_undated = _query_one(event, "includeUndated")
    if raw_undated is not None:
        include_undated = _parse_bool(raw_undated)
    else:
        # Default depends on the question. With no window nothing should vanish, so
        # undated postings are in. With a window they are out, because an undated
        # posting cannot honestly be claimed to fall inside a date range.
        include_undated = after is None and before is None

    # ``ats`` is intentionally not validated against a closed list: the set of
    # supported boards grows with the adapter layer, and rejecting a source that
    # exists would be worse than returning an empty page for a typo.
    return WorklistFilters(
        tiers=_parse_named(event, "tier", _TIERS_BY_NAME, "invalid_tier"),
        levels=_parse_named(event, "level", _LEVELS_BY_NAME, "invalid_level"),
        sources=frozenset(value.lower() for value in _query_values(event, "ats")),
        posted_after=after,
        posted_before=before,
        include_undated=include_undated,
        collection=collection,
    )


def _parse_cursor(event: Mapping[str, Any], *, fingerprint: str) -> Cursor | None:
    raw = _query_one(event, "cursor")
    return _decode_cursor(raw, fingerprint=fingerprint) if raw else None


def _json_body(event: Mapping[str, Any]) -> dict[str, Any]:
    raw = event.get("body")
    if not isinstance(raw, str) or not raw.strip():
        raise ApiError(400, "missing_body")
    text = raw
    if event.get("isBase64Encoded"):
        try:
            text = base64.b64decode(raw).decode()
        except ValueError as exc:
            raise ApiError(400, "invalid_json") from exc
    try:
        parsed = json.loads(text)
    except ValueError as exc:
        raise ApiError(400, "invalid_json") from exc
    if not isinstance(parsed, dict):
        raise ApiError(400, "invalid_json")
    body: dict[str, Any] = parsed
    return body


def _posting_id_from_path(event: Mapping[str, Any]) -> str:
    params = event.get("pathParameters")
    if isinstance(params, dict):
        found = params.get("id")
        if isinstance(found, str) and found.strip():
            return found.strip()
    # Fall back to the raw path, but only accept a segment that actually sits under
    # /worklist/. Without that check ``GET /worklist/`` reads its own collection name
    # back as an id and 404s, which hides the real fault (no id was sent at all).
    raw_path = str(event.get("rawPath") or event.get("path") or "")
    segments = [part.strip() for part in raw_path.split("/") if part.strip()]
    under_worklist = len(segments) >= _COLLECTION_AND_ID and segments[-2] == "worklist"
    tail = segments[-1] if under_worklist else ""
    if not tail or tail.startswith("{"):
        # "{id}" leaks through when a route template arrives with no path
        # parameters attached — that is a wiring fault, not a missing posting.
        raise ApiError(400, "missing_posting_id")
    return tail


def _posting_id_from_body(event: Mapping[str, Any]) -> str:
    found = _json_body(event).get("postingId")
    if not isinstance(found, str) or not found.strip():
        raise ApiError(400, "missing_posting_id")
    return found.strip()


# ---------------------------------------------------------------------------
# Reading the materialised view
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Screened:
    """One posting with the screening verdict a card renders.

    Four fields, because that is everything a card and a score need beyond the
    posting itself. There are two constructors because there are two honest sources
    for those four facts, and both are load-bearing:

    * :meth:`from_row` — the list routes, reading back what the cron materialised.
    * :meth:`from_decision` — ``GET /worklist/{id}``, which re-screens that **one**
      posting rather than making the store keep all seven gates' evidence for every
      posting on the chance that someone clicks through. One screen is 1.5 ms; the
      storage alternative is the same 180-character excerpt written up to seven times
      per posting.

    The two must agree field for field for the same posting. The mapping from a
    verdict to these four fields therefore exists here *and* in
    ``services/daily_briefing._row`` — the writer cannot import the reader — so
    ``test_a_card_from_the_view_matches_a_card_from_a_fresh_screen`` pins them
    together. Drift would show one seniority band on a card and a different one on
    that card's own detail page.
    """

    posting: Posting
    #: ``Level`` value as a string, not the enum: it crosses a storage boundary, and
    #: a band written by a future deploy must render rather than raise here.
    level: str
    level_source: str
    level_why: str

    @classmethod
    def from_row(cls, row: ScreenedRow, posting: Posting) -> Screened:
        return cls(
            posting=posting,
            level=row.level,
            level_source=row.level_source,
            level_why=row.level_why,
        )

    @classmethod
    def from_decision(cls, decision: ScreenDecision) -> Screened:
        verdict = decision.level_verdict
        return cls(
            posting=decision.posting,
            level=decision.level.value,
            level_source=verdict.source.value if verdict is not None else LevelSource.NONE.value,
            level_why=verdict.explain() if verdict is not None else "",
        )

    @property
    def level_confirmed(self) -> bool:
        """Whether anything actually *stated* the band, rather than it being unknown."""
        return self.level_source != LevelSource.NONE.value


@dataclass(frozen=True)
class Window:
    """One page of one view, plus the two numbers the wire needs around it."""

    items: tuple[Screened, ...]
    #: Rows matching the filters across the **whole** view, not just this page.
    matched: int
    #: The last row of this page when another page follows; ``None`` at the end. A
    #: *row* and not an item, because a row whose posting has since been reaped is
    #: dropped from ``items`` but must still advance the boundary — otherwise page 2
    #: starts before it and re-serves the gap forever.
    boundary: ScreenedRow | None = None


def _guarded[T](what: str, call: Callable[[], T]) -> T:
    """Run one store call; any failure is a 503, never a 500.

    The store is I/O — a dead SQLite file, a throttled table, expired credentials —
    and every one of those is retryable by the caller. A 500 tells a UI nothing and
    reads as a bug in this code.
    """
    try:
        return call()
    except Exception as exc:
        log.exception(what)
        raise StoreUnavailable() from exc


def _read_summary(store: PostingStorePort) -> ScreenSummary | None:
    return _guarded("worklist_summary_read_failed", store.screening_summary)


def _screened_page(
    store: PostingStorePort, view: str, *, generation: str, limit: int, after: str | None
) -> ScreenedPage:
    """One page of one view. A ``ValueError`` for an unknown view lands here as a 503.

    That is the right answer even though it would be our bug and not the store's: every
    view name this module passes comes from ``Exclusion`` or from the two collection
    constants, so the only way to reach it is a code change, and a 503 keeps a page
    that is already broken from also being a 500 with a stack trace.
    """
    return _guarded(
        "worklist_view_read_failed",
        lambda: store.screened_page(view, generation=generation, limit=limit, after=after),
    )


def _postings_by_id(store: PostingStorePort, ids: Sequence[str]) -> dict[str, Posting]:
    """Fetch postings by id. Missing ids are simply absent.

    A posting can close and be reaped between the view being written and a page being
    served, and the port's contract is that one missing row costs that row rather than
    the request. Logged at WARNING with a count: a handful is the corpus moving, a
    whole page is the view pointing at a corpus that no longer exists.
    """
    if not ids:
        return {}
    found = _guarded("worklist_hydrate_failed", lambda: store.postings_by_id(ids))
    if len(found) < len(set(ids)):
        log.warning(
            "worklist_hydrate_gap",
            extra={"extra_fields": {"asked": len(set(ids)), "found": len(found)}},
        )
    return found


def _hydrate(store: PostingStorePort, rows: Sequence[ScreenedRow]) -> dict[str, Posting]:
    """Hydrate one page of screening rows into the postings they point at."""
    return _postings_by_id(store, [row.posting_id for row in rows])


def _items(rows: Sequence[ScreenedRow], postings: Mapping[str, Posting]) -> tuple[Screened, ...]:
    return tuple(
        Screened.from_row(row, postings[row.posting_id])
        for row in rows
        if row.posting_id in postings
    )


class SummaryCache:
    """Hold the view's summary for the life of a warm container.

    All that is left of the old ``IndexCache``, and the shrinkage is the point: that
    class cached a *screening pass over the whole corpus* because a read performed one,
    at 39 s a go. A read no longer screens anything, so the only thing worth keeping
    between requests is the single summary item every route starts from.

    The TTL is what bounds how long a container may keep answering out of a generation
    the cron has already replaced, and it is measured against the caller's ``now``
    rather than the wall clock so expiry is deterministic and testable. A clock that
    jumps backwards forces a re-read rather than pinning one summary forever.

    A *missing* summary is deliberately not cached. "Not screened yet" is the state
    that most wants to end promptly, and re-reading one absent item costs a GetItem.
    """

    def __init__(self, *, ttl_seconds: float = SUMMARY_TTL_SECONDS) -> None:
        self._ttl = ttl_seconds
        self._summary: ScreenSummary | None = None
        self._read_at: datetime | None = None

    def load(self, store: PostingStorePort, *, now: datetime) -> ScreenSummary | None:
        cached, read_at = self._summary, self._read_at
        if cached is not None and read_at is not None:
            age = (now - read_at).total_seconds()
            if 0.0 <= age < self._ttl:
                return cached
        fresh = _read_summary(store)
        if fresh is not None:
            self._summary = fresh
            self._read_at = now
        return fresh


def _load_summary(
    store: PostingStorePort, *, now: datetime, cache: SummaryCache | None
) -> ScreenSummary:
    """The current view's funnel, or a named not-ready state.

    There is deliberately **no fallback to screening the corpus**. That fallback is
    the bug: it is what took 39 s locally, ~72 s at the deployed size, and 504'd every
    request against a 29 s ceiling. A read that cannot find a view says so in
    milliseconds instead.

    Staleness is an age check rather than a comparison against the live corpus, and
    that is a cost decision the port documents: counting the corpus is ``O(corpus)``
    (33 paged COUNT queries over 47k rows on DynamoDB) and paying it per request would
    reintroduce exactly what this view removes. The 48-hour bound survives one missed
    cron run and not two.
    """
    summary = cache.load(store, now=now) if cache is not None else _read_summary(store)
    if summary is None:
        raise ViewNotReady(NOT_SCREENED)
    if summary.is_stale(now):
        log.warning(
            "worklist_view_stale",
            extra={"extra_fields": {"age_hours": round(summary.age_hours(now), 2)}},
        )
        raise ViewNotReady(VIEW_STALE)
    return summary


def _collect(
    store: PostingStorePort,
    view: str,
    *,
    summary: ScreenSummary,
    view_total: int,
    limit: int,
    cursor: Cursor | None,
    filters: WorklistFilters,
    scorer: Scorer,
) -> Window:
    """One page of one view, plus an exact ``matched``.

    Two branches, because they cost different things and the common one has to be the
    cheap one:

    * **No filters** — one store page, one hydrate, done. ``matched`` is the view's
      size from the summary, so nothing counts anything. This is what the published
      page and ``tools/ui/build_ui.py`` walk, and it is O(page) at any corpus size.
    * **Filters** — the view is walked from the top, because ``matched`` means "rows
      matching across the whole collection" and the rows before the cursor count
      towards it too. Rows that the view itself can judge (band, date) are filtered
      for free; ``ats`` and ``tier`` need the posting, so those hydrate every row that
      survives the cheap filters. Bounded twice — by :data:`FILTER_SCAN_MAX_ROWS`
      up front and by :data:`FILTER_SCAN_BUDGET_SECONDS` as it goes — and only the two
      small collection views are ever filterable.

    The walk keeps *rows*, not postings, and the window is hydrated afterwards. That
    re-fetches at most ``limit`` postings the expensive branch had already read, which
    is the cheap half of the trade: holding them all would mean up to 20,000 postings at
    a measured 5.6 KB each in one Lambda's memory, for the sake of ≤100 GetItems.
    """
    generation = summary.generation
    token = cursor.store_token if cursor is not None else None

    if not filters.narrows:
        page = _screened_page(store, view, generation=generation, limit=limit, after=token)
        items = _items(page.rows, _hydrate(store, page.rows))
        boundary = page.rows[-1] if page.rows and page.next_token is not None else None
        return Window(items=items, matched=view_total, boundary=boundary)

    if view_total > FILTER_SCAN_MAX_ROWS:
        # Refused before a single row is read, using the summary's own total. This is
        # the one place a read could quietly start growing with the corpus again.
        raise ApiError(400, TOO_MANY_ROWS)

    matching = _walk_view(
        store,
        view,
        generation=generation,
        filters=filters,
        scorer=scorer,
        deadline=time.monotonic() + FILTER_SCAN_BUDGET_SECONDS,
    )
    start = _first_after(matching, token)
    rows = matching[start : start + limit]
    boundary = rows[-1] if rows and start + limit < len(matching) else None
    return Window(
        items=_items(rows, _hydrate(store, rows)),
        matched=len(matching),
        boundary=boundary,
    )


def _walk_view(
    store: PostingStorePort,
    view: str,
    *,
    generation: str,
    filters: WorklistFilters,
    scorer: Scorer,
    deadline: float,
) -> list[ScreenedRow]:
    """Every row of one view that matches the filters, still in recency order.

    The deadline is checked *after* each page rather than before, so the walk gives up
    having overshot by at most one page — and so a budget that has already been spent
    cannot be reported as a success on the strength of a page that was never read.
    """
    matching: list[ScreenedRow] = []
    after: str | None = None
    for _ in range(_MAX_SCAN_PAGES):
        page = _screened_page(
            store, view, generation=generation, limit=SCAN_PAGE_ROWS, after=after
        )
        candidates = [row for row in page.rows if _row_matches(filters, row)]
        if filters.narrows_postings and candidates:
            candidates = _keep_by_posting(
                candidates, _hydrate(store, candidates), filters=filters, scorer=scorer
            )
        matching.extend(candidates)
        if page.next_token is None:
            return matching
        if time.monotonic() > deadline:
            log.warning(
                "worklist_filter_scan_too_slow",
                extra={"extra_fields": {"view": view, "rows_seen": len(matching)}},
            )
            raise ApiError(503, SCAN_TOO_SLOW)
        after = page.next_token
    # Unreachable while a store returns full pages: the view is refused above unless it
    # holds at most FILTER_SCAN_MAX_ROWS, and _MAX_SCAN_PAGES covers that many rows with
    # two pages to spare. It is a 503 and not a `return matching` because the only way
    # here is a store handing back short pages while still promising more, and then
    # `matching` is *incomplete* — which would be reported as an exact `matched`, i.e. a
    # silently wrong count. A wrong number on the trust surface is the failure this
    # whole rewrite exists to remove; refusing is the lesser answer.
    log.error(
        "worklist_filter_scan_did_not_terminate",
        extra={"extra_fields": {"view": view, "pages": _MAX_SCAN_PAGES,
                                "rows_seen": len(matching)}},
    )
    raise ApiError(503, SCAN_TOO_SLOW)


def _keep_by_posting(
    rows: Sequence[ScreenedRow],
    postings: Mapping[str, Posting],
    *,
    filters: WorklistFilters,
    scorer: Scorer,
) -> list[ScreenedRow]:
    """Apply the filters that need a hydrated posting, keeping the rows in order."""
    kept: list[ScreenedRow] = []
    for row in rows:
        posting = postings.get(row.posting_id)
        if posting is None:
            continue
        item = Screened.from_row(row, posting)
        if not _posting_matches(filters, item):
            continue
        if filters.tiers and scorer.tier_of(item) not in filters.tiers:
            continue
        kept.append(row)
    return kept


def _first_after(rows: Sequence[ScreenedRow], token: str | None) -> int:
    """Where the page after ``token`` starts. Linear, over rows already in hand.

    A scan rather than a bisect because the list is *descending* by sort key, and the
    walk that produced it already cost more than this does.
    """
    if token is None:
        return 0
    for index, row in enumerate(rows):
        if row.sort_key < token:
            return index
    return len(rows)


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

class Scorer:
    """Scores postings against the résumé, memoised for the life of one request.

    Deliberately **not** cached across requests, and deliberately not stored in the
    view: a score memo keyed only by posting id would keep serving numbers computed
    against a résumé that has since changed, and a baked one would misrank silently
    while the corpus looked fresh. It is affordable here precisely because it runs over
    one page of ~25 rows rather than over 47,538 — and it is nonetheless the dominant
    cost of a read: **5.1 ms per posting** measured against the real corpus and a 5 KB
    résumé, so a page of 25 is 128 ms of the 134 ms the whole request takes. That
    constant is why the ``tier`` filter, alone among the filters, needs a wall-clock
    bound (:data:`FILTER_SCAN_BUDGET_SECONDS`).

    With no résumé configured this reports *unavailable* rather than scoring
    against an empty document — that would mark every requirement as "missing" and
    render a confident 0 for 880 postings. Same degradation shape as
    ``adapters/llm_reply.py`` with no API key.
    """

    def __init__(self, resume_text: str = "") -> None:
        self._resume_text = resume_text
        self._memo: dict[str, tuple[GapReport, Score]] = {}

    @property
    def available(self) -> bool:
        return bool(self._resume_text.strip())

    def scoring_wire(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "reason": None if self.available else "no_resume_configured",
        }

    def _scored(self, item: Screened) -> tuple[GapReport, Score]:
        posting = item.posting
        cached = self._memo.get(posting.id)
        if cached is not None:
            return cached
        report = build_report(
            title=posting.title,
            company=posting.company,
            url=posting.url,
            # An unavailable description must not be passed off as prose we read.
            description=posting.description if posting.desc_available else "",
            resume_text=self._resume_text,
        )
        scored = (report, score_report(report, level_confirmed=item.level_confirmed))
        self._memo[posting.id] = scored
        return scored

    def tier_of(self, item: Screened) -> Tier:
        return self._scored(item)[1].tier

    def wire(self, item: Screened) -> dict[str, Any] | None:
        """The score with its components, or ``None`` when scoring is unavailable."""
        if not self.available:
            return None
        report, score = self._scored(item)
        return {
            "total": score.total,
            "tier": score.tier.value,
            "explain": score.explain(),
            "required": {
                "covered": score.required_covered,
                "total": score.required_total,
                "have": list(report.have_required),
                "missing": list(report.missing_required),
            },
            "preferred": {
                "covered": score.preferred_covered,
                "total": score.preferred_total,
                "have": list(report.have_preferred),
                "missing": list(report.missing_preferred),
            },
            "levelConfirmed": score.level_confirmed,
            "resumeVariant": report.variant.value,
            "unscoredReason": _unscored_reason(item, score),
        }


def _unscored_reason(item: Screened, score: Score) -> str | None:
    """Why a posting has no meaningful score — the two causes are not the same bug."""
    if score.tier is not Tier.UNSCORED:
        return None
    if not item.posting.desc_available:
        return "description_not_provided"
    return "no_technologies_named"


# ---------------------------------------------------------------------------
# Serialisation. camelCase on the wire, snake_case in Python.
# ---------------------------------------------------------------------------

def _quote(text: str) -> str | None:
    """Normalise and cap one evidence excerpt; ``None`` when there is nothing to show."""
    cleaned = " ".join(text.split())
    if not cleaned:
        return None
    if len(cleaned) <= QUOTE_MAX_CHARS:
        return cleaned
    return cleaned[: QUOTE_MAX_CHARS - 1] + "…"


def _quote_for(decision: ScreenDecision, exclusion: Exclusion) -> str | None:
    """The text that tripped one specific gate, for a **freshly screened** posting.

    Only the detail read needs this: a list row's evidence was quoted and capped when
    the view was written, per gate, and is read back off the row. Kept addressed per
    gate for the same reason it is stored that way — a posting routinely fails several
    gates at once, and a grouped view must show the evidence for the gate it is
    displaying, not for whichever one happened to fire first.
    """
    if exclusion is Exclusion.NOT_SWE:
        return _quote(decision.posting.title)
    if exclusion is Exclusion.INTERNSHIP:
        # The two signals disagree often, so quote whichever one fired: 27 postings
        # carry it only in the title and 22 only in the ATS employment type. Without
        # this the internships group was the one gate with no evidence at all, which
        # is precisely the shape "nothing disappears silently" rules out.
        posting = decision.posting
        return _quote(
            posting.title if is_internship(posting.title) else posting.employment_type
        )
    if exclusion is Exclusion.LEVEL:
        verdict = decision.level_verdict
        return _quote(verdict.evidence) if verdict is not None else None
    key = _ELIGIBILITY_EVIDENCE_KEY.get(exclusion)
    if key is None:
        return None
    return _quote(dict(decision.eligibility.evidence).get(key, ""))


def _description_status(posting: Posting) -> str:
    if not posting.desc_available:
        return DESC_NOT_PROVIDED
    return DESC_AVAILABLE if posting.description.strip() else DESC_EMPTY


def _description(posting: Posting) -> str | None:
    """The description, or ``None`` — **never** ``""``.

    The empty string is what made this a bug in the first place: it passes every
    description-based gate and reads, in a UI, as a role that simply has nothing to
    say. ``null`` plus ``descriptionStatus`` forces the caller to handle the case.
    """
    return posting.description if _description_status(posting) == DESC_AVAILABLE else None


def _card(item: Screened) -> dict[str, Any]:
    """The list-row shape. No description prose: that is a detail read."""
    posting = item.posting
    return {
        "id": posting.id,
        "title": posting.title,
        "company": posting.company,
        "location": posting.location,
        "url": posting.url,
        "ats": posting.ats,
        "level": item.level,
        "levelSource": item.level_source,
        "levelWhy": item.level_why,
        "postedAt": posting.posted_at.isoformat() if posting.posted_at else None,
        "remote": posting.remote,
        "employmentType": posting.employment_type,
        "descAvailable": posting.desc_available,
        "descriptionStatus": _description_status(posting),
    }


def _screening_wire(decision: ScreenDecision) -> dict[str, Any]:
    eligibility = decision.eligibility
    verdict = decision.level_verdict
    return {
        "kept": decision.kept,
        "level": decision.level.value,
        "levelSource": verdict.source.value if verdict is not None else LevelSource.NONE.value,
        "levelWhy": verdict.explain() if verdict is not None else "",
        "eligibility": {
            "checked": eligibility.checked,
            "clearanceRequired": eligibility.clearance_required,
            "citizenshipRequired": eligibility.citizenship_required,
            "sponsorship": eligibility.sponsorship.value,
            "evidence": [
                {"gate": gate, "quote": _quote(quoted)}
                for gate, quoted in eligibility.evidence
            ],
            # Unchecked is not "eligible". Saying so on the wire is the whole point
            # of Eligibility.checked existing.
            "note": None if eligibility.checked
            else "no description from this source — the eligibility gates could not run",
        },
        "exclusions": [
            {
                "gate": exclusion.value,
                "reason": decision.reason_for(exclusion),
                "quote": _quote_for(decision, exclusion),
            }
            for exclusion in decision.exclusions
        ],
    }


def _funnel_wire(summary: ScreenSummary) -> dict[str, Any]:
    """The funnel, entirely from the summary — no count touches the corpus.

    Keyed over ``Exclusion`` rather than over ``summary.gates`` so the gate set on the
    wire is stable: the summary only carries gates that actually fired, and a UI whose
    chart columns appeared and disappeared day to day would be unreadable.
    """
    return {
        # "screened", not "fetched": the pass covered the open corpus, and a closed
        # posting was fetched once but is not in it.
        "screened": summary.screened,
        "kept": summary.kept,
        "excluded": summary.excluded,
        "gates": {gate.value: summary.gates.get(gate.value, 0) for gate in Exclusion},
        "gateCountTotal": summary.gate_count_total,
        # A posting fails several gates at once, so the per-gate counts sum to far
        # more than the number of postings removed (43,602 vs 24,414 on a real
        # run). Flagged because a UI that renders them as a subtraction chain lies.
        "gateCountsOvercount": summary.gate_counts_overcount,
        "needsLevelCheck": summary.needs_level_check,
    }


# ---------------------------------------------------------------------------
# Handlers. Each takes an injected store; the Lambda entry point does the wiring.
# ---------------------------------------------------------------------------

def _now(now: datetime | None) -> datetime:
    return now if now is not None else datetime.now(UTC)


def _list_collection(
    store: PostingStorePort,
    event: dict[str, Any],
    *,
    collection: str,
    resume_text: str,
    now: datetime | None,
    cache: SummaryCache | None,
) -> dict[str, Any]:
    """One page of one collection. Shared by ``/worklist`` and ``/internships``.

    Both routes take the same filters, the same keyset paging and the same card and
    score shape, so they are one implementation with a different population — a
    second copy would be where the two drift, and the page renders them with the
    same component precisely because they are the same rows in a different set.

    Both totals travel in both responses, so a UI can render "813 full-time · 48
    internships" from either read without a second request.

    ``generatedAt`` is the summary's ``screenedAt``: the moment the verdicts and the
    counts in this body were computed. It is emphatically not "now" — that would claim
    freshness for a page that is showing a pass which may be twenty hours old.
    """
    if user_id_from_event(event) is None:
        return _error(401, "unauthorized")
    try:
        filters = _parse_filters(event, collection=collection)
        limit = _parse_limit(event, default=DEFAULT_LIMIT)
        scorer = Scorer(resume_text)
        if filters.tiers and not scorer.available:
            raise ApiError(400, "scoring_unavailable")
        fingerprint = filters.fingerprint
        cursor = _parse_cursor(event, fingerprint=fingerprint)
        summary = _load_summary(store, now=_now(now), cache=cache)
        internships = collection == COLLECTION_INTERNSHIPS
        window = _collect(
            store,
            _VIEW_FOR_COLLECTION[collection],
            summary=summary,
            view_total=summary.internship_total if internships else summary.eligible_total,
            limit=limit,
            cursor=cursor,
            filters=filters,
            scorer=scorer,
        )
    except ApiError as exc:
        return exc.response()

    next_cursor = (
        _encode_cursor(window.boundary, fingerprint=fingerprint)
        if window.boundary is not None
        else None
    )
    return _response(
        200,
        {
            "generatedAt": summary.screened_at.isoformat(),
            "collection": collection,
            "items": [{**_card(item), "score": scorer.wire(item)} for item in window.items],
            "page": _page_wire(len(window.items), limit=limit, next_cursor=next_cursor),
            "matched": window.matched,
            "eligibleTotal": summary.eligible_total,
            "internshipTotal": summary.internship_total,
            "filters": filters.as_wire(),
            "funnel": _funnel_wire(summary),
            "scoring": scorer.scoring_wire(),
        },
    )


def list_worklist(
    store: PostingStorePort,
    event: dict[str, Any],
    *,
    resume_text: str = "",
    now: datetime | None = None,
    cache: SummaryCache | None = None,
) -> dict[str, Any]:
    """``GET /worklist`` — eligible **full-time** postings, newest first, scored.

    Query: ``limit``, ``cursor``, ``tier``, ``level``, ``ats``, ``postedAfter``,
    ``postedBefore``, ``includeUndated``. ``tier`` needs a résumé, so it is refused
    rather than ignored when none is configured.

    Internships are not in here and must not leak back in: they crowd the one list
    whose whole job is to be short and correct — 50 of them survived once, and 5
    ranked *exact match*. ``/internships`` is where they live, as its own view.
    """
    return _list_collection(
        store,
        event,
        collection=COLLECTION_WORKLIST,
        resume_text=resume_text,
        now=now,
        cache=cache,
    )


def list_internships(
    store: PostingStorePort,
    event: dict[str, Any],
    *,
    resume_text: str = "",
    now: datetime | None = None,
    cache: SummaryCache | None = None,
) -> dict[str, Any]:
    """``GET /internships`` — the internships collection. Same query, same cards.

    **Why a path and not ``GET /worklist?collection=internships``.** For the same
    reason ``/excluded`` is a path rather than ``?kept=false``: this is a different
    *population*, not a narrowing of the worklist, and the two have different
    denominators — ``matched`` out of 318 means something else than ``matched`` out
    of 48. An addressable route is also what lets the page link to the section, and
    lets a cache or a per-route throttle treat it separately from the main list.

    48 is the honest size of this collection, against 318 postings that hit the
    internship gate. ``funnel`` travels in this response too, so both numbers are
    visible in the same payload.

    A detail read for one of these rows (``GET /worklist/{id}``) still reports the
    primary pass's verdict, which names the internship gate. That is deliberate: it
    is the honest answer to "why is this not in my worklist", and it is the same
    fact this collection is derived from.
    """
    return _list_collection(
        store,
        event,
        collection=COLLECTION_INTERNSHIPS,
        resume_text=resume_text,
        now=now,
        cache=cache,
    )


def _one_posting(store: PostingStorePort, posting_id: str) -> Posting:
    """One posting by id, or a 404. Never a view lookup.

    Addressed against the corpus rather than against the screening view on purpose: the
    view holds only *open* postings as of the last pass, and both callers — the detail
    read and ``POST /applied`` — are legitimately about postings that have closed.
    """
    posting = _postings_by_id(store, [posting_id]).get(posting_id)
    if posting is None:
        raise ApiError(404, "posting_not_found")
    return posting


def get_posting(
    store: PostingStorePort,
    event: dict[str, Any],
    *,
    resume_text: str = "",
    now: datetime | None = None,
    cache: SummaryCache | None = None,
) -> dict[str, Any]:
    """``GET /worklist/{id}`` — one posting, its description, and its verdict.

    Serves excluded postings too: the whole point of /excluded is that a human can
    click through to the posting that was dropped and read the evidence in context.
    It also serves *closed* postings, because "this role closed" is news about the job
    and a 404 there would read as a bug in the page.

    The only route that does not consult the materialised view, and the only one that
    still calls ``screen``. It re-screens this **one** posting — 1.5 ms — which is what
    lets the store keep one gate's evidence per row instead of all seven gates' quotes
    for every posting. Two consequences worth stating: this read keeps working while
    the corpus is unscreened, and ``generatedAt`` here is *now*, because the verdict
    below was computed now. On the list routes it is the pass's timestamp. Both mean
    the same thing — when the screening you are looking at happened.

    ``cache`` is accepted and unused. All four read handlers keep one call signature
    because ``public_api`` and ``tools/ui/build_ui.py`` pass the same keywords to every
    one of them; a signature that differed per route would move that decision into
    those callers.
    """
    if user_id_from_event(event) is None:
        return _error(401, "unauthorized")
    try:
        posting_id = _posting_id_from_path(event)
        posting = _one_posting(store, posting_id)
    except ApiError as exc:
        return exc.response()

    decision = screen(posting)
    item = Screened.from_decision(decision)
    scorer = Scorer(resume_text)
    description = _description(posting)
    return _response(
        200,
        {
            "generatedAt": _now(now).isoformat(),
            "posting": {
                **_card(item),
                "tenant": posting.tenant,
                "reqId": posting.req_id,
                "description": description,
                "descriptionChars": len(description) if description is not None else None,
                "screening": _screening_wire(decision),
                "score": scorer.wire(item),
            },
            "scoring": scorer.scoring_wire(),
        },
    )


def list_excluded(
    store: PostingStorePort,
    event: dict[str, Any],
    *,
    now: datetime | None = None,
    cache: SummaryCache | None = None,
) -> dict[str, Any]:
    """``GET /excluded`` — what was dropped, grouped by gate, with the sentence quoted.

    Query: ``gate``, ``limit``, ``cursor``. A cursor is only meaningful inside one
    gate's ordering, so paging requires ``gate``; asking for page 2 of "all gates"
    is refused rather than silently answered for one of them.

    Every gate is its own materialised view, which is what makes this route cheap:
    the largest group holds ~41,500 rows and a page of ten reads ten of them. The
    counts come from the summary, so nothing here is derived by scanning — the old
    implementation built all seven groups by screening the whole corpus, which is
    why this endpoint 504'd along with the rest.
    """
    if user_id_from_event(event) is None:
        return _error(401, "unauthorized")
    try:
        gate = _parse_named(event, "gate", _GATES_BY_NAME, "invalid_gate")
        if len(gate) > 1:
            raise ApiError(400, "invalid_gate")
        limit = _parse_limit(event, default=DEFAULT_GROUP_LIMIT)
        raw_cursor = _query_one(event, "cursor")
        if raw_cursor is not None and not gate:
            raise ApiError(400, "cursor_requires_gate")
        wanted = sorted(gate, key=lambda g: g.value) if gate else list(Exclusion)
        summary = _load_summary(store, now=_now(now), cache=cache)
        groups = [
            _excluded_group(store, summary, g, limit=limit, raw_cursor=raw_cursor)
            for g in wanted
        ]
    except ApiError as exc:
        return exc.response()

    return _response(
        200,
        {
            "generatedAt": summary.screened_at.isoformat(),
            "excludedTotal": summary.excluded,
            "counts": {
                g.value: summary.gates.get(g.value, 0) for g in Exclusion
            },
            "gateCountTotal": summary.gate_count_total,
            "gateCountsOvercount": summary.gate_counts_overcount,
            "groups": groups,
            "funnel": _funnel_wire(summary),
        },
    )


def _excluded_group(
    store: PostingStorePort,
    summary: ScreenSummary,
    gate: Exclusion,
    *,
    limit: int,
    raw_cursor: str | None,
) -> dict[str, Any]:
    """One gate's page. ``reason`` and ``quote`` are read back off the row.

    Quoted and capped when the view was written rather than re-derived here, so the
    evidence on a card is provably the evidence from the pass whose counts sit beside
    it. ``count`` is the gate's own count from the summary — the number of postings the
    gate fired on, which for the internship gate is 318 while the internships
    *collection* is 48. Both numbers ship, in this body and in ``funnel``, because the
    gap is real and looks like an off-by-270 bug when only one of them is visible.
    """
    fingerprint = _gate_fingerprint(gate)
    cursor = _decode_cursor(raw_cursor, fingerprint=fingerprint) if raw_cursor else None
    page = _screened_page(
        store,
        gate.value,
        generation=summary.generation,
        limit=limit,
        after=cursor.store_token if cursor is not None else None,
    )
    postings = _hydrate(store, page.rows)
    boundary = page.rows[-1] if page.rows and page.next_token is not None else None
    items = [
        {
            **_card(Screened.from_row(row, postings[row.posting_id])),
            "reason": row.reason,
            "quote": _quote(row.quote),
        }
        for row in page.rows
        if row.posting_id in postings
    ]
    return {
        "gate": gate.value,
        "count": summary.gates.get(gate.value, 0),
        "items": items,
        "page": _page_wire(
            len(items),
            limit=limit,
            next_cursor=(
                _encode_cursor(boundary, fingerprint=fingerprint)
                if boundary is not None
                else None
            ),
        ),
    }


def _gate_fingerprint(gate: Exclusion) -> str:
    return hashlib.sha1(f"excluded:{gate.value}".encode()).hexdigest()[:12]


def record_applied(
    store: PostingStorePort,
    event: dict[str, Any],
    *,
    now: datetime | None = None,
    cache: SummaryCache | None = None,
) -> dict[str, Any]:
    """``POST /applied`` — record that a human applied. Body: ``{"postingId": "..."}``.

    **Records only.** Nothing in this function, or anything it calls, can submit an
    application: the single write is ``PostingStorePort.mark_applied``.

    Idempotent by construction. ``mark_applied`` writes only where ``applied_at IS
    NULL``, so a repeat is a no-op, and the response body is byte-identical on
    every call — no ``recordedAt`` is echoed, because the port cannot read the
    stored timestamp back and inventing the current one would misreport the first
    call's time.

    The id is validated with a single-posting read rather than against a screened
    index, which closes the limit the index version documented: recording an
    application for a role that has since closed used to 404, and closing is exactly
    what happens to a posting you applied to. ``cache`` is accepted and unused, for the
    signature reason :func:`get_posting` gives.
    """
    if user_id_from_event(event) is None:
        return _error(401, "unauthorized")
    try:
        posting_id = _posting_id_from_body(event)
        posting = _one_posting(store, posting_id)
        _mark_applied(store, posting_id, now=_now(now))
    except ApiError as exc:
        return exc.response()

    return _response(
        200,
        {
            "postingId": posting.id,
            "title": posting.title,
            "company": posting.company,
            "url": posting.url,
            "recorded": True,
            "submitted": False,
            "note": "recorded only — this API never submits an application anywhere",
        },
    )


def _mark_applied(store: PostingStorePort, posting_id: str, *, now: datetime) -> None:
    try:
        store.mark_applied(posting_id, now=now)
    except Exception as exc:
        log.exception("worklist_mark_applied_failed", extra={"extra_fields": {"id": posting_id}})
        raise StoreUnavailable() from exc


# ---------------------------------------------------------------------------
# Routing + wiring
# ---------------------------------------------------------------------------

_KNOWN_PATHS: Final[frozenset[str]] = frozenset(
    {"/worklist", "/worklist/{id}", "/internships", "/excluded", "/applied"}
)


def method_and_path(event: Mapping[str, Any]) -> tuple[str, str]:
    """Read method and path from any of the three shapes API Gateway emits.

    Public because ``handlers/public_api.py`` routes on the same three shapes, and a
    second reader would be a second set of bugs about which key wins.
    """
    route_key = event.get("routeKey")
    if isinstance(route_key, str) and " " in route_key:
        method, _, path = route_key.partition(" ")
        return method.upper(), path
    http = event.get("requestContext", {}).get("http", {})
    method = str(event.get("httpMethod") or http.get("method") or "GET").upper()
    path = str(event.get("resource") or event.get("rawPath") or event.get("path") or "/")
    return method, path


def _template(path: str) -> str:
    trimmed = "/" + path.strip("/")
    if trimmed.startswith("/worklist/"):
        return "/worklist/{id}"
    return trimmed


def route(
    store: PostingStorePort,
    event: dict[str, Any],
    *,
    resume_text: str = "",
    now: datetime | None = None,
    cache: SummaryCache | None = None,
) -> dict[str, Any]:
    """Dispatch one API Gateway event. Split from :func:`handler` so tests drive it."""
    method, path = method_and_path(event)
    template = _template(path)
    if method == "OPTIONS":
        return _preflight()
    handled = _dispatch(
        store, event, method=method, template=template,
        resume_text=resume_text, now=now, cache=cache,
    )
    if handled is not None:
        return handled
    # A known path with the wrong verb is a caller bug worth naming; anything else
    # is simply not part of this API.
    known = template in _KNOWN_PATHS
    return _error(405, "method_not_allowed") if known else _error(404, "not_found")


def _dispatch(
    store: PostingStorePort,
    event: dict[str, Any],
    *,
    method: str,
    template: str,
    resume_text: str,
    now: datetime | None,
    cache: SummaryCache | None,
) -> dict[str, Any] | None:
    """The route table. ``None`` means nothing matched, which the caller names."""
    if (method, template) == ("GET", "/worklist"):
        return list_worklist(store, event, resume_text=resume_text, now=now, cache=cache)
    if (method, template) == ("GET", "/worklist/{id}"):
        return get_posting(store, event, resume_text=resume_text, now=now, cache=cache)
    if (method, template) == ("GET", "/internships"):
        return list_internships(store, event, resume_text=resume_text, now=now, cache=cache)
    if (method, template) == ("GET", "/excluded"):
        return list_excluded(store, event, now=now, cache=cache)
    if (method, template) == ("POST", "/applied"):
        return record_applied(store, event, now=now, cache=cache)
    return None


def build_store(settings: Settings) -> PostingStorePort:
    """The v2 posting store: DynamoDB when a table is named, SQLite otherwise.

    Deliberately **not** ``adapters.dynamodb_store.DynamoDbStore``: that is the v1
    briefing table with a different data model.

    The backend is chosen by whether ``postings_table_name`` is set rather than by
    an explicit ``backend="sqlite"|"dynamodb"`` flag, because the two settings can
    then never disagree — there is no state where the flag says DynamoDB and no
    table name exists, which in Lambda would surface as a boto3 error on the first
    request instead of at wiring time. An unset table name is the local default on
    purpose: a clone of this public repo runs against a file with no AWS account.

    Both branches are the same ``PostingStorePort``, and the shared parametrised
    suite in ``tests/test_dynamodb_posting_store.py`` drives both implementations
    through one set of assertions, so this swap cannot change behaviour.
    """
    if settings.postings_table_name:
        return DynamoDbPostingStore(settings.postings_table_name, region=settings.aws_region)
    return SqlitePostingStore(settings.postings_db_path)


def load_resume_text(settings: Settings) -> str:
    """Résumé text for scoring, or ``""`` when none is configured.

    Everything personal lives under the gitignored ``private_dir``, so a clone of
    this public repo has no résumé. Returning ``""`` makes the worklist serve with
    scoring reported unavailable, which is the same graceful degradation
    ``llm_reply`` uses when the API key is absent.
    """
    variant = settings.resume_variant
    candidates = (
        settings.resume_dir / f"{variant}.txt",
        settings.resume_dir / f"{variant}.md",
        settings.resume_dir / "html" / f"{variant}.html",
    )
    for path in candidates:
        if path.is_file():
            raw = path.read_text(encoding="utf-8", errors="replace")
            # An ATS reads the text layer, not the markup; so does the vocabulary.
            return _MARKUP.sub(" ", raw) if path.suffix in {".html", ".htm"} else raw
    return ""


#: Container-scoped so a warm Lambda reads the view's summary once, not per request.
_CACHE: Final = SummaryCache()


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Lambda entrypoint for the worklist read API."""
    settings = load_settings()
    return route(
        build_store(settings),
        event,
        resume_text=load_resume_text(settings),
        cache=_CACHE,
    )
