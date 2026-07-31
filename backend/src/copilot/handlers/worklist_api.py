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

Five decisions here are load-bearing:

**Pagination is keyset, not offset.** The worklist is 880 rows and the postings
underneath it close while a human reads. An offset page 2 taken after a row closes
silently *skips* a posting — the one that slid into the boundary. The opaque cursor
carries the sort key of the last row returned, so a page boundary is anchored to a
posting rather than to a count. The cursor also carries a fingerprint of the filter
set: a cursor is only meaningful inside one ordering, and honouring a foreign one
would return a page that repeats or skips rows without saying so.

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
their own addressable population — derived by re-screening the postings the
internship gate removed with ``include_internships=True``, so every *other* gate
(vendor demo boards, seniority, clearance, citizenship, sponsorship) still applies.
The primary pass is left alone on purpose: it is what produces the funnel counts
and the ``/excluded`` evidence, and both are part of the trust surface.

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
from bisect import bisect_right
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

from copilot.adapters.dynamodb_posting_store import DynamoDbPostingStore
from copilot.adapters.sqlite_posting_store import SqlitePostingStore
from copilot.config import Settings, load_settings
from copilot.domain.gap import GapReport, Score, Tier, build_report, score_report
from copilot.domain.posting import Posting
from copilot.domain.screening import (
    Exclusion,
    ScreenDecision,
    ScreenReport,
    is_internship,
    screen,
)
from copilot.domain.seniority import Level, LevelSource
from copilot.handlers.api import user_id_from_event
from copilot.logging import get_logger
from copilot.ports.postingstore import PostingStorePort

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

#: How long a warm Lambda container may reuse a screened index.
INDEX_TTL_SECONDS: Final = 300.0

DESC_AVAILABLE: Final = "available"
#: The source returned no description at all (Workday's list endpoint, some Lever).
DESC_NOT_PROVIDED: Final = "not_provided_by_source"
#: The source claimed a description and returned nothing in it — a data fault worth
#: seeing, and not the same thing as never having offered one.
DESC_EMPTY: Final = "empty_from_source"

_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)
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


# ---------------------------------------------------------------------------
# Ordering
# ---------------------------------------------------------------------------

def _as_utc(value: datetime | None) -> datetime:
    """Normalise a posting timestamp to an aware UTC datetime.

    Two hazards, both real: ``posted_at`` is optional, and nothing in ``Posting``
    forces a timezone, so a single naive row in the store makes ``sorted()`` raise
    ``TypeError`` mid-request. Undated postings collapse to the epoch, which sorts
    them last instead of dropping them.
    """
    if value is None:
        return _EPOCH
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _order_key(posted_at: datetime | None, posting_id: str) -> tuple[float, str]:
    """Ascending key that yields *newest first*, with the id as a tiebreaker.

    Negating the timestamp rather than reversing the sort keeps the list ascending
    in this key, which is what lets ``bisect`` find a cursor position in log time
    instead of scanning 24k rows per page.
    """
    return (-_as_utc(posted_at).timestamp(), posting_id)


def _decision_key(decision: ScreenDecision) -> tuple[float, str]:
    return _order_key(decision.posting.posted_at, decision.posting.id)


# ---------------------------------------------------------------------------
# Cursor
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Cursor:
    """An opaque page boundary: the sort key of the last row already returned."""

    posted_at: datetime | None
    posting_id: str

    @property
    def key(self) -> tuple[float, str]:
        return _order_key(self.posted_at, self.posting_id)


def _encode_cursor(decision: ScreenDecision, *, fingerprint: str) -> str:
    payload = {
        "v": CURSOR_VERSION,
        "ts": decision.posting.posted_at.isoformat() if decision.posting.posted_at else None,
        "id": decision.posting.id,
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


def _page(
    ordered: Sequence[ScreenDecision],
    *,
    limit: int,
    cursor: Cursor | None,
    fingerprint: str,
) -> tuple[list[ScreenDecision], str | None]:
    """Take one keyset page. ``ordered`` must already be sorted by ``_decision_key``."""
    start = 0
    if cursor is not None:
        keys = [_decision_key(decision) for decision in ordered]
        start = bisect_right(keys, cursor.key)
    window = list(ordered[start : start + limit])
    has_more = start + limit < len(ordered)
    next_cursor = (
        _encode_cursor(window[-1], fingerprint=fingerprint) if window and has_more else None
    )
    return window, next_cursor


def _page_wire(
    window: Sequence[ScreenDecision], *, limit: int, next_cursor: str | None
) -> dict[str, Any]:
    return {
        "limit": limit,
        "count": len(window),
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
    def fingerprint(self) -> str:
        """Identifies the question a cursor belongs to (not a secret, just a tag).

        The collection is folded in, not just the filters: without it a cursor from
        ``/worklist`` would be accepted by ``/internships`` under the same filters
        and page into the middle of a list it was never taken from.
        """
        raw = json.dumps(
            {"collection": self.collection, "filters": self.as_wire()},
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha1(raw.encode()).hexdigest()[:12]


def _matches(filters: WorklistFilters, decision: ScreenDecision) -> bool:
    """Every filter except tier, which needs a score and so is applied separately."""
    posting = decision.posting
    if filters.levels and decision.level not in filters.levels:
        return False
    if filters.sources and posting.ats.lower() not in filters.sources:
        return False
    if posting.posted_at is None:
        return filters.include_undated
    when = _as_utc(posting.posted_at)
    if filters.posted_after is not None and when < filters.posted_after:
        return False
    return not (filters.posted_before is not None and when > filters.posted_before)


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
# The screened index
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class WorklistIndex:
    """One screening pass over the open postings, in wire order.

    Built as a whole rather than queried per request because the funnel counts are
    part of the trust surface: "880 kept of 25,294" is only true if every posting
    went through the same pass that produced the page being shown.
    """

    generated_at: datetime
    kept: tuple[ScreenDecision, ...]
    excluded: tuple[ScreenDecision, ...]
    report: ScreenReport
    by_gate: Mapping[str, tuple[ScreenDecision, ...]]
    by_id: Mapping[str, ScreenDecision]
    #: The internships collection: postings the internship gate removed that pass
    #: every *other* gate. Disjoint from :attr:`kept` by construction, and a subset
    #: of the postings counted in :attr:`excluded` — see :func:`_internship_collection`.
    internships: tuple[ScreenDecision, ...] = ()


def _open_postings(store: PostingStorePort) -> list[Posting]:
    try:
        return list(store.open_postings())
    except Exception as exc:  # the store is I/O; any failure is a 503, never a 500
        log.exception("worklist_store_read_failed")
        raise StoreUnavailable() from exc


def build_index(store: PostingStorePort, *, now: datetime) -> WorklistIndex:
    """Screen every open posting and index it for reads.

    ``screening.screen_all`` is deliberately not used: it sorts with a key that
    mixes the aware epoch with whatever tzinfo a posting carries, so one naive
    ``posted_at`` in the store raises ``TypeError``. Sorting through ``_as_utc``
    here keeps a bad row from taking the whole endpoint down.
    """
    kept: list[ScreenDecision] = []
    excluded: list[ScreenDecision] = []
    report = ScreenReport()
    for posting in _open_postings(store):
        decision = screen(posting)
        report.note(decision)
        (kept if decision.kept else excluded).append(decision)

    kept.sort(key=_decision_key)
    excluded.sort(key=_decision_key)
    by_gate = {
        gate.value: tuple(d for d in excluded if gate in d.exclusions) for gate in Exclusion
    }
    by_id = {d.posting.id: d for d in (*kept, *excluded)}
    return WorklistIndex(
        generated_at=now,
        kept=tuple(kept),
        excluded=tuple(excluded),
        report=report,
        by_gate=by_gate,
        by_id=by_id,
        internships=_internship_collection(excluded),
    )


def _internship_collection(excluded: Sequence[ScreenDecision]) -> tuple[ScreenDecision, ...]:
    """The internships collection, re-screened with the preference flipped.

    Measured on the live corpus: 318 postings hit the internship gate and **48**
    come back out of here. The other 270 fail a gate that has nothing to do with
    being an internship — 264 are not software roles, 12 are ATS vendor demo
    fixtures, the rest want a clearance, citizenship, or say they will not sponsor.
    Publishing all 318 under a "internships" heading would have put "Marketing
    Intern" and a vendor's invented roles on the page.

    Built as a second pass over the 318 postings the internship gate removed
    rather than by running the whole funnel with ``include_internships=True``,
    because the primary pass is the one whose numbers are published: flipping the
    flag there would move 318 postings out of ``/excluded`` and quietly restate
    "813 kept of 25,294" as "1,131 kept". So the primary pass stays exactly as it
    was, and this derives a second population from its output.

    ``screen(..., include_internships=True)`` is the mechanism, deliberately — not
    a bespoke "is this an internship" check here. That is what guarantees the rest
    of the funnel still applies: an internship on an ATS vendor demo board, with a
    senior title, or requiring a clearance is *not* kept, because every other gate
    runs unchanged. A second mechanism would have to re-implement all of them.
    """
    kept: list[ScreenDecision] = []
    for decision in excluded:
        if Exclusion.INTERNSHIP not in decision.exclusions:
            continue
        allowed = screen(decision.posting, include_internships=True)
        if allowed.kept:
            kept.append(allowed)
    kept.sort(key=_decision_key)
    return tuple(kept)


class IndexCache:
    """Screen once per warm container, not once per keystroke.

    A build reads every open posting and runs the funnel over ~25k rows. Paying
    that per request makes the UI feel broken and the Lambda expensive, and every
    request in a session sees the same daily corpus anyway. The TTL is measured
    against the caller's ``now`` rather than the wall clock so expiry is
    deterministic and testable; a clock that jumps backwards forces a rebuild
    rather than pinning a stale index forever.
    """

    def __init__(self, *, ttl_seconds: float = INDEX_TTL_SECONDS) -> None:
        self._ttl = ttl_seconds
        self._index: WorklistIndex | None = None

    def load(self, store: PostingStorePort, *, now: datetime) -> WorklistIndex:
        cached = self._index
        if cached is not None:
            age = (now - cached.generated_at).total_seconds()
            if 0.0 <= age < self._ttl:
                return cached
        fresh = build_index(store, now=now)
        self._index = fresh
        return fresh


def _load_index(
    store: PostingStorePort, *, now: datetime, cache: IndexCache | None
) -> WorklistIndex:
    return cache.load(store, now=now) if cache is not None else build_index(store, now=now)


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

class Scorer:
    """Scores postings against the résumé, memoised for the life of one request.

    Kept out of :class:`IndexCache` on purpose: a memo keyed only by posting id
    would keep serving numbers computed against a résumé that has since changed.

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

    def _scored(self, decision: ScreenDecision) -> tuple[GapReport, Score]:
        posting = decision.posting
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
        verdict = decision.level_verdict
        confirmed = verdict is not None and verdict.source is not LevelSource.NONE
        scored = (report, score_report(report, level_confirmed=confirmed))
        self._memo[posting.id] = scored
        return scored

    def tier_of(self, decision: ScreenDecision) -> Tier:
        return self._scored(decision)[1].tier

    def wire(self, decision: ScreenDecision) -> dict[str, Any] | None:
        """The score with its components, or ``None`` when scoring is unavailable."""
        if not self.available:
            return None
        report, score = self._scored(decision)
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
            "unscoredReason": _unscored_reason(decision, score),
        }


def _unscored_reason(decision: ScreenDecision, score: Score) -> str | None:
    """Why a posting has no meaningful score — the two causes are not the same bug."""
    if score.tier is not Tier.UNSCORED:
        return None
    if not decision.posting.desc_available:
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
    """The text that tripped one specific gate.

    Addressed per gate rather than taken from ``reasons[0]``: a posting routinely
    fails several gates at once, and a grouped view must show the evidence for the
    gate it is displaying, not for whichever one happened to fire first.
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


def _card(decision: ScreenDecision) -> dict[str, Any]:
    """The list-row shape. No description prose: that is a detail read."""
    posting = decision.posting
    verdict = decision.level_verdict
    return {
        "id": posting.id,
        "title": posting.title,
        "company": posting.company,
        "location": posting.location,
        "url": posting.url,
        "ats": posting.ats,
        "level": decision.level.value,
        "levelSource": verdict.source.value if verdict is not None else LevelSource.NONE.value,
        "levelWhy": verdict.explain() if verdict is not None else "",
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


def _funnel_wire(report: ScreenReport) -> dict[str, Any]:
    return {
        # "screened", not "fetched": the store holds open postings, and a closed
        # posting was fetched once but is not in this pass.
        "screened": report.total,
        "kept": report.kept,
        "excluded": report.excluded,
        "gates": {gate.value: report.by_exclusion.get(gate.value, 0) for gate in Exclusion},
        "gateCountTotal": report.gate_count_total,
        # A posting fails several gates at once, so the per-gate counts sum to far
        # more than the number of postings removed (43,602 vs 24,414 on a real
        # run). Flagged because a UI that renders them as a subtraction chain lies.
        "gateCountsOvercount": True,
        "needsLevelCheck": report.needs_llm,
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
    cache: IndexCache | None,
) -> dict[str, Any]:
    """One page of one collection. Shared by ``/worklist`` and ``/internships``.

    Both routes take the same filters, the same keyset paging and the same card and
    score shape, so they are one implementation with a different population — a
    second copy would be where the two drift, and the page renders them with the
    same component precisely because they are the same rows in a different set.

    Both totals travel in both responses, so a UI can render "813 full-time · 48
    internships" from either read without a second request.
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
        index = _load_index(store, now=_now(now), cache=cache)
        population = (
            index.internships if collection == COLLECTION_INTERNSHIPS else index.kept
        )
        matched = [d for d in population if _matches(filters, d)]
        if filters.tiers:
            matched = [d for d in matched if scorer.tier_of(d) in filters.tiers]
        window, next_cursor = _page(
            matched, limit=limit, cursor=cursor, fingerprint=fingerprint
        )
    except ApiError as exc:
        return exc.response()

    return _response(
        200,
        {
            "generatedAt": index.generated_at.isoformat(),
            "collection": collection,
            "items": [{**_card(d), "score": scorer.wire(d)} for d in window],
            "page": _page_wire(window, limit=limit, next_cursor=next_cursor),
            "matched": len(matched),
            "eligibleTotal": len(index.kept),
            "internshipTotal": len(index.internships),
            "filters": filters.as_wire(),
            "funnel": _funnel_wire(index.report),
            "scoring": scorer.scoring_wire(),
        },
    )


def list_worklist(
    store: PostingStorePort,
    event: dict[str, Any],
    *,
    resume_text: str = "",
    now: datetime | None = None,
    cache: IndexCache | None = None,
) -> dict[str, Any]:
    """``GET /worklist`` — eligible **full-time** postings, newest first, scored.

    Query: ``limit``, ``cursor``, ``tier``, ``level``, ``ats``, ``postedAfter``,
    ``postedBefore``, ``includeUndated``. ``tier`` needs a résumé, so it is refused
    rather than ignored when none is configured.

    Internships are not in here and must not leak back in: they crowd the one list
    whose whole job is to be short and correct — 50 of them survived once, and 5
    ranked *exact match*. ``/internships`` is where they live.
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
    cache: IndexCache | None = None,
) -> dict[str, Any]:
    """``GET /internships`` — the internships collection. Same query, same cards.

    **Why a path and not ``GET /worklist?collection=internships``.** For the same
    reason ``/excluded`` is a path rather than ``?kept=false``: this is a different
    *population*, not a narrowing of the worklist, and the two have different
    denominators — ``matched`` out of 318 means something else than ``matched`` out
    of 48. An addressable route is also what lets the page link to the section, and
    lets a cache or a per-route throttle treat it separately from the main list.

    48 is the honest size of this collection, against 318 postings that hit the
    internship gate — see :func:`_internship_collection`. ``funnel`` travels in this
    response too, so both numbers are visible in the same payload.

    A detail read for one of these rows (``GET /worklist/{id}``) still reports the
    primary pass's verdict, which names the internship gate. That is deliberate: it
    is the honest answer to "why is this not in my worklist", and it is the same
    fact this section is built from.
    """
    return _list_collection(
        store,
        event,
        collection=COLLECTION_INTERNSHIPS,
        resume_text=resume_text,
        now=now,
        cache=cache,
    )


def get_posting(
    store: PostingStorePort,
    event: dict[str, Any],
    *,
    resume_text: str = "",
    now: datetime | None = None,
    cache: IndexCache | None = None,
) -> dict[str, Any]:
    """``GET /worklist/{id}`` — one posting, its description, and its verdict.

    Serves excluded postings too: the whole point of /excluded is that a human can
    click through to the posting that was dropped and read the evidence in context.
    """
    if user_id_from_event(event) is None:
        return _error(401, "unauthorized")
    try:
        posting_id = _posting_id_from_path(event)
        index = _load_index(store, now=_now(now), cache=cache)
        decision = index.by_id.get(posting_id)
        if decision is None:
            raise ApiError(404, "posting_not_found")
    except ApiError as exc:
        return exc.response()

    scorer = Scorer(resume_text)
    posting = decision.posting
    description = _description(posting)
    return _response(
        200,
        {
            "generatedAt": index.generated_at.isoformat(),
            "posting": {
                **_card(decision),
                "tenant": posting.tenant,
                "reqId": posting.req_id,
                "description": description,
                "descriptionChars": len(description) if description is not None else None,
                "screening": _screening_wire(decision),
                "score": scorer.wire(decision),
            },
            "scoring": scorer.scoring_wire(),
        },
    )


def list_excluded(
    store: PostingStorePort,
    event: dict[str, Any],
    *,
    now: datetime | None = None,
    cache: IndexCache | None = None,
) -> dict[str, Any]:
    """``GET /excluded`` — what was dropped, grouped by gate, with the sentence quoted.

    Query: ``gate``, ``limit``, ``cursor``. A cursor is only meaningful inside one
    gate's ordering, so paging requires ``gate``; asking for page 2 of "all gates"
    is refused rather than silently answered for one of them.
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
        index = _load_index(store, now=_now(now), cache=cache)
        groups = [
            _excluded_group(index, g, limit=limit, raw_cursor=raw_cursor) for g in wanted
        ]
    except ApiError as exc:
        return exc.response()

    return _response(
        200,
        {
            "generatedAt": index.generated_at.isoformat(),
            "excludedTotal": index.report.excluded,
            "counts": {
                g.value: index.report.by_exclusion.get(g.value, 0) for g in Exclusion
            },
            "gateCountTotal": index.report.gate_count_total,
            "gateCountsOvercount": True,
            "groups": groups,
            "funnel": _funnel_wire(index.report),
        },
    )


def _excluded_group(
    index: WorklistIndex, gate: Exclusion, *, limit: int, raw_cursor: str | None
) -> dict[str, Any]:
    members = index.by_gate.get(gate.value, ())
    fingerprint = _gate_fingerprint(gate)
    cursor = _decode_cursor(raw_cursor, fingerprint=fingerprint) if raw_cursor else None
    window, next_cursor = _page(members, limit=limit, cursor=cursor, fingerprint=fingerprint)
    return {
        "gate": gate.value,
        "count": len(members),
        "items": [
            {
                **_card(decision),
                "reason": decision.reason_for(gate),
                "quote": _quote_for(decision, gate),
            }
            for decision in window
        ],
        "page": _page_wire(window, limit=limit, next_cursor=next_cursor),
    }


def _gate_fingerprint(gate: Exclusion) -> str:
    return hashlib.sha1(f"excluded:{gate.value}".encode()).hexdigest()[:12]


def record_applied(
    store: PostingStorePort,
    event: dict[str, Any],
    *,
    now: datetime | None = None,
    cache: IndexCache | None = None,
) -> dict[str, Any]:
    """``POST /applied`` — record that a human applied. Body: ``{"postingId": "..."}``.

    **Records only.** Nothing in this function, or anything it calls, can submit an
    application: the single write is ``PostingStorePort.mark_applied``.

    Idempotent by construction. ``mark_applied`` writes only where ``applied_at IS
    NULL``, so a repeat is a no-op, and the response body is byte-identical on
    every call — no ``recordedAt`` is echoed, because the port cannot read the
    stored timestamp back and inventing the current one would misreport the first
    call's time.

    Known limit: an id is validated against the *open* index, so recording an
    application for a posting that has since closed 404s. Fixing that needs a
    single-posting read on ``PostingStorePort``.
    """
    if user_id_from_event(event) is None:
        return _error(401, "unauthorized")
    try:
        posting_id = _posting_id_from_body(event)
        index = _load_index(store, now=_now(now), cache=cache)
        decision = index.by_id.get(posting_id)
        if decision is None:
            raise ApiError(404, "posting_not_found")
        _mark_applied(store, posting_id, now=_now(now))
    except ApiError as exc:
        return exc.response()

    posting = decision.posting
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
    cache: IndexCache | None = None,
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
    cache: IndexCache | None,
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


#: Container-scoped so a warm Lambda screens the corpus once, not per request.
_CACHE: Final = IndexCache()


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Lambda entrypoint for the worklist read API."""
    settings = load_settings()
    return route(
        build_store(settings),
        event,
        resume_text=load_resume_text(settings),
        cache=_CACHE,
    )
