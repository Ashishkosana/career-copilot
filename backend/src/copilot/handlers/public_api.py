"""Unauthenticated read API — exactly what the published page already shows.

``docs/index.html`` is a snapshot, because GitHub Pages cannot query anything and
every route in ``handlers/worklist_api.py`` sits behind the Cognito authorizer. So a
static page cannot be *live*. These four routes are what make it live:

* ``GET /public/worklist``      the full-time worklist, paged, filtered, scored.
* ``GET /public/worklist/{id}`` one posting: card, screening verdict, score.
* ``GET /public/internships``   the internships collection.
* ``GET /public/excluded``      the trust surface, grouped by gate, with quotes.

This is a **narrow, deliberate exposure of data that is already public on that page
today** — same fields, same tiers, same evidence excerpts. It is not a new
disclosure. What keeps it that way is that the boundary is one mechanism, in one
place, and tested:

**Deny by default, in both directions.** The response is an *allowlist projection*:
:data:`WORKLIST_FIELDS` and friends name every field that may be published and
:func:`project` drops everything else. A blocklist would publish whatever the model
grows next — the failure mode is silent and six months late. An allowlist fails
closed: a new field is invisible until someone adds it here on purpose, and a
*renamed* field is a 500 rather than a card with blank columns. The request is an
allowlist too (:data:`ALLOWED_QUERY`): a fresh ``GET`` event is constructed with the
documented query parameters copied across and nothing else — no body, ever.

**Read-only, structurally.** Anything that is not a ``GET`` on one of the four paths
is refused before a handler runs, and this module never names ``record_applied`` or
``mark_applied``. ``tests/test_public_api.py`` asserts both, plus that a full sweep
of every route touches exactly one store method: ``open_postings``.

**No description prose.** Republishing 234 companies' description text in bulk is a
different act from quoting one sentence to justify a decision — and Workday's terms
forbid it. ``description`` is simply not in the allowlist; ``descriptionStatus``,
``descriptionChars`` and ``descriptionWithheld`` are, because "this posting has
4,812 characters we are not reprinting" and "this source publishes none at all" are
different facts.

**Every published string is bounded, and there are two bounds because there are two
kinds of string.** An *evidence excerpt* — a span lifted out of someone's
description to justify a verdict — is marked :data:`EXCERPT` and capped at
``QUOTE_MAX_CHARS`` (180): that is the publishing decision, and marking it in the
spec rather than trimming in a follow-up pass means a newly allowed evidence field
is capped the moment it is allowed. *Card identity text* — title, company,
location, url — is marked :data:`BOUNDED` and capped at
:data:`CARD_TEXT_MAX_CHARS`, which is a payload bound rather than a disclosure one:
these fields are the smallest thing that identifies a role and 180 would truncate
26 real postings whose ``location`` is a legitimate 24-city list (the longest in
this corpus runs 347 characters). What is *not* acceptable is what this module did
until the boundary was audited: leave them as plain scalars and republish whatever
length an upstream feed happens to contain, on an open metered endpoint.

``url`` is bounded the same way but **fails closed instead of truncating**, because
a shortened sentence is still true and a shortened URL is a lie — it would render as
a live "Apply" link that goes nowhere. No URL in this corpus is within a factor of
four of the bound, so that branch is a "this cannot happen" guard, not a formatter.

**Nothing personal.** No résumé text, no email address, no Cognito subject, no owner
id, and no applied state — which roles he applied to is private. The score stays,
because the snapshot already publishes it and ``required.have`` is canonical
vocabulary tokens (``domain/gap.VOCAB``), never résumé prose. Note that this Lambda
*does* load the résumé, because scoring needs it; the projection is therefore the
only thing between that text and the wire, which is the whole reason it is an
allowlist and the whole reason the test suite salts a payload with ``ownerId`` /
``appliedAt`` and asserts they cannot come out.

**There is no caller identity, and nothing needs one.** The authenticated handlers
use the JWT ``sub`` only as an authentication gate — no read below selects data by
subject, because the posting corpus is per-installation, not per-user (asserted in
``test_public_api.py`` by serving two different subjects and comparing bodies). So
this module synthesises the constant :data:`PUBLIC_PRINCIPAL` to satisfy that gate.
It is a label, not a credential, and it appears nowhere in a response.

**Cost.** An open endpoint must not be able to run up a bill. Three brakes, two of
them here: a page is capped at ``MAX_LIMIT`` rows, the screened index is built once
per warm container (:class:`~copilot.handlers.worklist_api.IndexCache`), and every
successful read carries ``Cache-Control: public, max-age=300`` so browsers and any CDN
in front absorb repeats — errors are ``no-store``, because a cached 503 outlives the
outage that caused it. The third brake is the request-rate throttle on the API Gateway
stage, which is infrastructure, not code — this module cannot enforce it and does
not pretend to.
"""
from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any, Final

from copilot.config import load_settings
from copilot.handlers import worklist_api
from copilot.handlers.worklist_api import (
    DESC_AVAILABLE,
    QUOTE_MAX_CHARS,
    IndexCache,
    build_store,
    load_resume_text,
)
from copilot.logging import get_logger
from copilot.ports.postingstore import PostingStorePort

log = get_logger("copilot.handlers.public_api")

#: Every public path is a read, so only ``GET`` is advertised — and no
#: ``Authorization`` header, because this route has no notion of a caller. Origin is
#: ``*`` on purpose: the payload is published data and the page may be served from
#: ``jobs.ashishkosana.com`` or from a local file during development.
CORS_HEADERS: Final[dict[str, str]] = {
    "Content-Type": "application/json",
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET,OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
}

#: Matches ``IndexCache``'s TTL: the response is served from an index that may
#: already be five minutes old, so promising fresher data than that would be a lie,
#: and promising less would spend money re-screening for no one's benefit.
CACHE_MAX_AGE_SECONDS: Final = 300

#: The synthetic principal handed to the authenticated read path to satisfy its
#: ``sub`` check. Prefixed so it can never be mistaken for a Cognito subject (those
#: are UUIDs) if it ever shows up in a log line.
PUBLIC_PRINCIPAL: Final = "public-unauthenticated"

PUBLIC_PREFIX: Final = "/public"

#: The only upstream statuses this module treats specially.
_OK: Final = 200
_UNAUTHORIZED: Final = 401

#: Query parameters carried across to the authenticated read. Anything else is
#: dropped: an unrecognised parameter cannot change a read it never reaches.
ALLOWED_QUERY: Final[frozenset[str]] = frozenset(
    {
        "limit", "cursor", "tier", "level", "ats",
        "postedAfter", "postedBefore", "includeUndated", "gate",
    }
)


class Excerpt:
    """Spec marker: a string leaf that is capped at :data:`QUOTE_MAX_CHARS`.

    Used for every value that can contain text taken from a posting. Marking it in
    the spec rather than capping in a follow-up pass means a newly published
    evidence field is capped the moment it is allowed, not the moment someone
    remembers to add it to a list of paths to trim.
    """


class ScalarMap:
    """Spec marker: an object whose *keys* are open but whose values must be scalars.

    Exactly two things need this — ``counts`` and ``funnel.gates``, both gate name →
    integer. Values are checked, so an object cannot smuggle itself in as a "count".
    """


class Bounded:
    """Spec marker: card identity text, capped at :data:`CARD_TEXT_MAX_CHARS`.

    Separate from :class:`Excerpt` because the two caps answer different questions.
    180 is a *disclosure* limit on other people's prose; this is a *payload* limit on
    the four fields that name a role, sized so that no real posting is truncated
    while no upstream feed can put an unbounded string on an open endpoint.

    ``exact`` inverts the failure: instead of truncating, exceeding the bound raises.
    Used for ``url``, where a truncated value is not a shorter truth but a broken
    link.
    """

    def __init__(self, *, exact: bool = False) -> None:
        self.exact = exact


EXCERPT: Final = Excerpt()
SCALAR_MAP: Final = ScalarMap()
BOUNDED: Final = Bounded()
BOUNDED_EXACT: Final = Bounded(exact=True)

#: Bound on ``title``/``company``/``location``/``url``. 400 rather than 180 because
#: the longest legitimate ``location`` in this corpus is a 347-character list of 24
#: cities, and rather than "whatever fits" because 100 rows of 4 unbounded strings is
#: how an open route's response size stops being a number anyone has thought about.
CARD_TEXT_MAX_CHARS: Final = 400

#: A field spec: field name -> ``None`` (publish this scalar as-is), ``EXCERPT``,
#: ``BOUNDED``, ``SCALAR_MAP``, or a nested spec (applied to an object, or to every
#: element of a list of objects).
type Fields = Mapping[str, "Fields | Excerpt | Bounded | ScalarMap | None"]

#: The list row. Compare ``worklist_api._card``: ``descAvailable`` and
#: ``descriptionStatus`` are published, ``description`` is not — and ``levelWhy`` is
#: an excerpt because it is derived from the posting, so the cap holds even if a
#: future edit widens it from "the description asks for 3+ years" into a quote.
#:
#: The four fields carrying free text off an upstream feed are ``BOUNDED``; ``id``,
#: ``ats``, ``level`` and ``levelSource`` are not, because each is either a hash or a
#: value from a closed enum this repo defines, so there is no length to bound.
CARD_FIELDS: Final[Fields] = {
    "id": None,
    "title": BOUNDED,
    "company": BOUNDED,
    "location": BOUNDED,
    "url": BOUNDED_EXACT,
    "ats": None,
    "level": None,
    "levelSource": None,
    "levelWhy": EXCERPT,
    "postedAt": None,
    "remote": None,
    "employmentType": None,
    "descAvailable": None,
    "descriptionStatus": None,
}

#: ``have``/``missing`` are canonical vocabulary tokens ("Python", "Kubernetes"),
#: not text from either document, which is why they can be published at all.
SCORE_COMPONENT_FIELDS: Final[Fields] = {
    "covered": None,
    "total": None,
    "have": None,
    "missing": None,
}

SCORE_FIELDS: Final[Fields] = {
    "total": None,
    "tier": None,
    "explain": EXCERPT,
    "required": SCORE_COMPONENT_FIELDS,
    "preferred": SCORE_COMPONENT_FIELDS,
    "levelConfirmed": None,
    "resumeVariant": None,
    "unscoredReason": None,
}

PAGE_FIELDS: Final[Fields] = {
    "limit": None,
    "count": None,
    "nextCursor": None,
    "hasMore": None,
}

FILTER_FIELDS: Final[Fields] = {
    "tier": None,
    "level": None,
    "ats": None,
    "postedAfter": None,
    "postedBefore": None,
    "includeUndated": None,
}

FUNNEL_FIELDS: Final[Fields] = {
    "screened": None,
    "kept": None,
    "excluded": None,
    "gates": SCALAR_MAP,
    "gateCountTotal": None,
    "gateCountsOvercount": None,
    "needsLevelCheck": None,
}

SCORING_FIELDS: Final[Fields] = {"available": None, "reason": None}

#: ``GET /public/worklist`` and ``GET /public/internships``.
COLLECTION_FIELDS: Final[Fields] = {
    "generatedAt": None,
    "collection": None,
    "items": {**CARD_FIELDS, "score": SCORE_FIELDS},
    "page": PAGE_FIELDS,
    "matched": None,
    "eligibleTotal": None,
    "internshipTotal": None,
    "filters": FILTER_FIELDS,
    "funnel": FUNNEL_FIELDS,
    "scoring": SCORING_FIELDS,
}

ELIGIBILITY_FIELDS: Final[Fields] = {
    "checked": None,
    "clearanceRequired": None,
    "citizenshipRequired": None,
    "sponsorship": None,
    "evidence": {"gate": None, "quote": EXCERPT},
    "note": None,
}

SCREENING_FIELDS: Final[Fields] = {
    "kept": None,
    "level": None,
    "levelSource": None,
    "levelWhy": EXCERPT,
    "eligibility": ELIGIBILITY_FIELDS,
    "exclusions": {"gate": None, "reason": EXCERPT, "quote": EXCERPT},
}

#: ``GET /public/worklist/{id}``. ``description`` is dropped, and so are ``tenant``
#: and ``reqId``: they are ATS board and requisition identifiers, they are not on
#: the published page, and nothing the page renders needs them.
DETAIL_FIELDS: Final[Fields] = {
    "generatedAt": None,
    "posting": {
        **CARD_FIELDS,
        "descriptionChars": None,
        "screening": SCREENING_FIELDS,
        "score": SCORE_FIELDS,
    },
    "scoring": SCORING_FIELDS,
}

#: ``GET /public/excluded``.
EXCLUDED_FIELDS: Final[Fields] = {
    "generatedAt": None,
    "excludedTotal": None,
    "counts": SCALAR_MAP,
    "gateCountTotal": None,
    "gateCountsOvercount": None,
    "groups": {
        "gate": None,
        "count": None,
        "items": {**CARD_FIELDS, "reason": EXCERPT, "quote": EXCERPT},
        "page": PAGE_FIELDS,
    },
    "funnel": FUNNEL_FIELDS,
}


class ProjectionError(RuntimeError):
    """The upstream payload does not match the allowlist, so nothing is published.

    Raised when an allowlisted field is missing (a rename upstream) or when a value
    marked as a leaf turns out to contain an object (a scalar that grew a body).
    Both are fail-closed: the route answers 500 rather than publishing a card with
    blank columns, or an object whose keys nobody reviewed.
    """


# ---------------------------------------------------------------------------
# The projection. This is the whole guarantee.
# ---------------------------------------------------------------------------

def project(source: Mapping[str, Any], fields: Fields, *, where: str = "body") -> dict[str, Any]:
    """Copy the allowlisted fields out of ``source``. Everything else is dropped.

    Deliberately built by iterating over the *spec*, never over the source: an
    iteration over the source would need a rule for the unknown key it finds, and
    the only safe rule — drop it — is what iterating the spec does for free.
    """
    published: dict[str, Any] = {}
    for name, spec in fields.items():
        path = f"{where}.{name}"
        if name not in source:
            raise ProjectionError(f"{path} is missing — the upstream field was renamed or removed")
        value = source[name]
        if isinstance(spec, Excerpt):
            published[name] = _excerpt(value, where=path)
        elif isinstance(spec, Bounded):
            published[name] = _bounded(value, spec, where=path)
        elif isinstance(spec, ScalarMap):
            published[name] = _scalar_map(value, where=path)
        elif spec is None:
            published[name] = _scalar(value, where=path)
        elif value is None:
            # A nested object may legitimately be absent — ``score`` is null when
            # no résumé is configured. Null is a value; a *missing* key is not.
            published[name] = None
        elif isinstance(value, list):
            published[name] = [
                project(_object(item, where=f"{path}[{n}]"), spec, where=f"{path}[{n}]")
                for n, item in enumerate(value)
            ]
        else:
            published[name] = project(_object(value, where=path), spec, where=path)
    return published


def _object(value: Any, *, where: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ProjectionError(f"{where} is {type(value).__name__}, not an object")
    mapping: Mapping[str, Any] = value
    return mapping


def _scalar(value: Any, *, where: str) -> Any:
    """A leaf: a scalar, or a list of scalars. Never an object, at any depth.

    This is the half of deny-by-default that is easy to miss. Allowlisting a field
    name only protects the level it is on — if the value later becomes an object,
    every key inside it is published without anyone naming one. So a leaf that grows
    a body is a :class:`ProjectionError`, not a pass-through.
    """
    if isinstance(value, Mapping):
        raise ProjectionError(
            f"{where} is an object but is allowlisted as a scalar; "
            "name its fields explicitly before publishing them"
        )
    if isinstance(value, list):
        return [_scalar(item, where=f"{where}[{n}]") for n, item in enumerate(value)]
    return value


def _scalar_map(value: Any, *, where: str) -> dict[str, Any]:
    """An open-keyed object of scalars: gate name -> count."""
    mapping = _object(value, where=where)
    return {str(key): _scalar(item, where=f"{where}.{key}") for key, item in mapping.items()}


def _excerpt(value: Any, *, where: str) -> Any:
    """Cap one published string at :data:`QUOTE_MAX_CHARS`.

    ``worklist_api`` already caps the ``quote`` fields, but not ``reason`` — which
    interpolates a posting title that in this corpus really does run to hundreds of
    characters. Capping here as well is not redundancy for its own sake: this is the
    copy that gets published, and the cap is a publishing decision.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise ProjectionError(f"{where} is {type(value).__name__}, not a string excerpt")
    cleaned = " ".join(value.split())
    if len(cleaned) <= QUOTE_MAX_CHARS:
        return cleaned
    return cleaned[: QUOTE_MAX_CHARS - 1] + "…"


def _bounded(value: Any, spec: Bounded, *, where: str) -> Any:
    """Cap one card identity field at :data:`CARD_TEXT_MAX_CHARS`.

    Whitespace is *not* collapsed the way :func:`_excerpt` collapses it: an excerpt is
    a sentence re-flowed for display, while a title is a value, and rewriting a value
    on the way out makes the published card differ from the posting for no reason.

    ``spec.exact`` raises instead of truncating. That is the right failure for a URL —
    a truncated link renders as a working "Apply" button that 404s, which is worse
    than a 500 that says the projection refused. It fails the whole page rather than
    the row on purpose: one pathological URL in a corpus whose longest is 85
    characters means the upstream shape changed, and that is worth noticing loudly.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise ProjectionError(f"{where} is {type(value).__name__}, not a string")
    if len(value) <= CARD_TEXT_MAX_CHARS:
        return value
    if spec.exact:
        raise ProjectionError(
            f"{where} is {len(value)} characters, over the {CARD_TEXT_MAX_CHARS} bound, "
            "and truncating it would publish a broken link"
        )
    return value[: CARD_TEXT_MAX_CHARS - 1] + "…"


def _withheld(card: dict[str, Any]) -> dict[str, Any]:
    """Say that prose exists and is not being published, rather than staying silent.

    Same flag the published snapshot carries. Without it, a role whose source gave
    no description and a role whose description we decline to reprint look identical
    on the page, and only one of those is a data gap.
    """
    return {**card, "descriptionWithheld": card["descriptionStatus"] == DESC_AVAILABLE}


def sanitise_collection(body: Mapping[str, Any]) -> dict[str, Any]:
    """Project ``GET /worklist`` or ``GET /internships`` for publication."""
    public = project(body, COLLECTION_FIELDS)
    public["items"] = [_withheld(item) for item in public["items"]]
    return _published(public)


def sanitise_detail(body: Mapping[str, Any]) -> dict[str, Any]:
    """Project ``GET /worklist/{id}`` for publication — card and verdict, no prose."""
    public = project(body, DETAIL_FIELDS)
    public["posting"] = _withheld(public["posting"])
    return _published(public)


def sanitise_excluded(body: Mapping[str, Any]) -> dict[str, Any]:
    """Project ``GET /excluded`` for publication."""
    public = project(body, EXCLUDED_FIELDS)
    for group in public["groups"]:
        group["items"] = [_withheld(item) for item in group["items"]]
    return _published(public)


def _published(body: dict[str, Any]) -> dict[str, Any]:
    """Stamp the two facts a consumer needs to know about this payload."""
    return {
        **body,
        # The same flag ``tools/ui/build_ui.py`` bakes into the public page: no
        # description prose is present, and the page must not claim otherwise.
        "prosePublished": False,
        "quoteMaxChars": QUOTE_MAX_CHARS,
    }


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------

def _headers(*, cacheable: bool) -> dict[str, str]:
    """CORS plus a cache policy. **Only successful reads are cacheable.**

    A cached error outlives the thing that caused it: a 503 from a store outage,
    handed to a CDN with ``max-age=300``, keeps the page broken for five minutes
    after the store comes back, and a cached 400 makes a fixed query look still
    broken. Errors are therefore ``no-store`` — which costs nothing, because an
    endpoint that is erroring is not the one running up a bill.
    """
    policy = f"public, max-age={CACHE_MAX_AGE_SECONDS}" if cacheable else "no-store"
    return {**CORS_HEADERS, "Cache-Control": policy}


def _response(status: int, body: dict[str, Any]) -> dict[str, Any]:
    return {
        "statusCode": status,
        "headers": _headers(cacheable=status == _OK),
        "body": json.dumps(body),
    }


def _error(status: int, code: str) -> dict[str, Any]:
    return _response(status, {"error": code})


def _preflight() -> dict[str, Any]:
    """CORS preflight. Cacheable: the answer is a constant, and it is not data."""
    return {"statusCode": 204, "headers": _headers(cacheable=True), "body": ""}


# ---------------------------------------------------------------------------
# The request projection
# ---------------------------------------------------------------------------

def _copy_query(event: Mapping[str, Any], key: str) -> dict[str, Any]:
    """One query-string container with only the documented parameters in it.

    Both containers are filtered by the same allowlist, because API Gateway puts
    repeats in ``multiValueQueryStringParameters`` (REST) and collapses them in the
    HTTP API — filtering one and forwarding the other wholesale would leave the
    other shape unfiltered on exactly the deployment that uses it.
    """
    raw = event.get(key)
    if not isinstance(raw, dict):
        return {}
    return {name: value for name, value in raw.items() if name in ALLOWED_QUERY}


def read_event(event: Mapping[str, Any], inner_path: str) -> dict[str, Any]:
    """Build the ``GET`` event the authenticated read handler will see.

    A fresh dict, not a copy of the caller's: the caller's event carries a body, a
    method, headers and (in a real deployment) an authorizer context, and none of
    those may influence what the read path does. The method is hardcoded, the body
    is never carried, and ``requestContext.authorizer`` is replaced outright — which
    is what makes ``POST /applied`` unreachable from here structurally rather than
    by inspection of the router.
    """
    inner: dict[str, Any] = {
        "routeKey": f"GET {inner_path}",
        "rawPath": inner_path,
        "requestContext": {
            "authorizer": {"jwt": {"claims": {"sub": PUBLIC_PRINCIPAL}}},
            "http": {"method": "GET"},
        },
        "queryStringParameters": _copy_query(event, "queryStringParameters"),
        "pathParameters": _path_params(event),
    }
    # Attached only when the caller actually repeated a parameter: ``_query_values``
    # prefers this container whenever the key is present, so sending an empty one is
    # noise on the common path and a foot-gun if it is ever built from anything but
    # the allowlist.
    multi = _copy_query(event, "multiValueQueryStringParameters")
    if multi:
        inner["multiValueQueryStringParameters"] = multi
    return inner


def _path_params(event: Mapping[str, Any]) -> dict[str, str]:
    """Carry ``{id}`` through, if the gateway substituted one.

    Nothing is invented when it did not: ``inner_path`` still ends in the raw tail,
    so ``worklist_api`` applies its own rule — including answering 400 rather than
    404 for an unsubstituted ``/worklist/{id}`` template, which is a wiring fault
    and should read as one on the public route too.
    """
    params = event.get("pathParameters")
    if not isinstance(params, dict):
        return {}
    found = params.get("id")
    return {"id": found} if isinstance(found, str) and found.strip() else {}


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------

WORKLIST_PATH: Final = f"{PUBLIC_PREFIX}/worklist"
DETAIL_PATH: Final = f"{PUBLIC_PREFIX}/worklist/{{id}}"
INTERNSHIPS_PATH: Final = f"{PUBLIC_PREFIX}/internships"
EXCLUDED_PATH: Final = f"{PUBLIC_PREFIX}/excluded"

KNOWN_PATHS: Final[frozenset[str]] = frozenset(
    {WORKLIST_PATH, DETAIL_PATH, INTERNSHIPS_PATH, EXCLUDED_PATH}
)


def _template(path: str) -> str:
    trimmed = "/" + path.strip("/")
    if trimmed.startswith(f"{WORKLIST_PATH}/"):
        return DETAIL_PATH
    return trimmed


def route(
    store: PostingStorePort,
    event: dict[str, Any],
    *,
    resume_text: str = "",
    now: datetime | None = None,
    cache: IndexCache | None = None,
) -> dict[str, Any]:
    """Dispatch one unauthenticated request. Split from :func:`handler` for tests.

    The method check comes first and is exhaustive: only ``GET`` (and the CORS
    preflight) reaches a handler, so no verb that could mutate anything gets as far
    as the store. A known path with a write verb is a 405 — named, so a caller that
    tries it learns the route is read-only rather than that it does not exist.
    """
    method, path = worklist_api.method_and_path(event)
    template = _template(path)
    if method == "OPTIONS":
        return _preflight()
    if method != "GET":
        return (
            _error(405, "method_not_allowed")
            if template in KNOWN_PATHS
            else _error(404, "not_found")
        )
    try:
        handled = _dispatch(
            store, event,
            template=template,
            inner_path=("/" + path.strip("/")).removeprefix(PUBLIC_PREFIX),
            resume_text=resume_text,
            now=now,
            cache=cache,
        )
    except ProjectionError:
        # Fail closed and loudly. Publishing a half-projected body would be the one
        # outcome worse than an outage on a public page.
        log.exception("public_projection_failed", extra={"extra_fields": {"path": template}})
        return _error(500, "public_projection_failed")
    return handled if handled is not None else _error(404, "not_found")


def _dispatch(
    store: PostingStorePort,
    event: Mapping[str, Any],
    *,
    template: str,
    inner_path: str,
    resume_text: str,
    now: datetime | None,
    cache: IndexCache | None,
) -> dict[str, Any] | None:
    """The read table: four paths, four projections. ``None`` means no route matched.

    Every branch is ``authenticated read -> sanitise``. There is no branch that
    serialises a posting itself, which is the point: one serialiser, one allowlist.
    """
    inner = read_event(event, inner_path)
    if template == WORKLIST_PATH:
        return _sanitised(
            worklist_api.list_worklist(
                store, inner, resume_text=resume_text, now=now, cache=cache
            ),
            sanitise_collection,
        )
    if template == INTERNSHIPS_PATH:
        return _sanitised(
            worklist_api.list_internships(
                store, inner, resume_text=resume_text, now=now, cache=cache
            ),
            sanitise_collection,
        )
    if template == DETAIL_PATH:
        return _sanitised(
            worklist_api.get_posting(
                store, inner, resume_text=resume_text, now=now, cache=cache
            ),
            sanitise_detail,
        )
    if template == EXCLUDED_PATH:
        return _sanitised(
            worklist_api.list_excluded(store, inner, now=now, cache=cache),
            sanitise_excluded,
        )
    return None


def _sanitised(
    upstream: Mapping[str, Any],
    sanitise: Callable[[Mapping[str, Any]], dict[str, Any]],
) -> dict[str, Any]:
    """Re-emit one authenticated response through the projection.

    An error body is re-stated rather than forwarded: the authenticated handlers'
    error shape is ``{"error": "<code>"}`` and nothing else, and re-stating it means
    even a future handler that started attaching detail to an error could not
    publish it here. A 401 would mean the synthetic principal stopped satisfying the
    ``sub`` check, which is a wiring fault on our side, not the caller's — so it is
    reported as a 500 rather than asking an anonymous visitor to authenticate.
    """
    status = int(upstream["statusCode"])
    body: dict[str, Any] = json.loads(str(upstream["body"]) or "{}")
    if status == _UNAUTHORIZED:
        log.error("public_route_lost_its_principal")
        return _error(500, "public_route_misconfigured")
    if status != _OK:
        code = body.get("error")
        return _error(status, str(code) if code else "error")
    return _response(_OK, sanitise(body))


#: Container-scoped, like the authenticated reader's: an open endpoint that
#: re-screened 25,294 postings per request would be a bill waiting to happen.
_CACHE: Final = IndexCache()


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Lambda entrypoint for the unauthenticated read API."""
    settings = load_settings()
    return route(
        build_store(settings),
        event,
        resume_text=load_resume_text(settings),
        cache=_CACHE,
    )
