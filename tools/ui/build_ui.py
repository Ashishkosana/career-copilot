#!/usr/bin/env python3
"""Render the worklist site from one template into two builds.

    local   -> build/ui.html   (full prose, the authenticated API, can record applied)
    public  -> docs/index.html (no prose, the unauthenticated /public read API)

Both builds ship the *same* JavaScript. The differences are three values in the boot
object: where the API lives, whether that API is the unauthenticated ``/public``
prefix, and whether description prose is present.

**The published page is live now, and the snapshot is its fallback.** It used to be a
snapshot and nothing else, because every route in ``handlers/worklist_api.py`` sits
behind the Cognito authorizer and a static page has no JWT. ``handlers/public_api.py``
is what changed that: four read-only routes under :data:`PUBLIC_API_PREFIX` serving
exactly the sanitized payload this build already bakes. So the public build now points
at :data:`PUBLIC_API_BASE` and reads it first.

**What the fallback is for, and why it must keep working.** A visitor behind a network
that blocks the API — a corporate proxy, an offline laptop, a throttled endpoint, or
simply a Lambda that is down — must still see the page. So the snapshot is still baked
into every build, the page decides *once* at boot which source it will read (never
mid-session: page 2 of a different dataset repeats or skips rows), and the banner says
which one it got, why, and how old the data is. That is why the boot object carries
both ``apiBase`` and a complete ``snapshot``, and why ``--no-public-api`` still
produces a page that works with the network unplugged.

**Why the snapshot is produced by calling the handlers.** Nothing here re-implements
serialisation. ``collect_from_api`` drives ``list_worklist`` / ``list_internships`` /
``get_posting`` / ``list_excluded`` through the same synthetic API Gateway events
Lambda would send, so every field name in the baked JSON is the field name the wire
uses by construction rather than by review. :func:`check_fields` then asserts the key
sets it expected, so a rename in the exporter fails this build instead of silently
blanking a column in the browser.

**Why internships are a second collection and not a filter.** ``screen`` gates
internships out of the worklist, and that stays: 813 full-time roles is the list whose
whole job is to be short and correct, and internships crowded it once — 50 got in and 5
ranked *exact match*. But some new-grad pipelines at large employers run through
Fall/Summer internship postings, so they are collected here from ``GET /internships``
into their own array, rendered under their own heading with the same card component,
and never merged into ``items``. :func:`assert_collections_disjoint` enforces that,
because "the internships leaked back into the worklist" is the exact regression this
section is one edit away from re-introducing.

**Why the public build strips prose.** 234 companies' description text republished
in bulk is a different act from quoting a sentence to explain a decision. So the
public build drops ``description`` entirely and keeps the short evidence excerpts,
capped at :data:`QUOTE_CAP` characters — the same cap the API applies, re-asserted
here because this is the copy that gets published.

**Why there is a path list and not just a review.** :data:`BOOT_PATHS` spells out every
field the template's JavaScript reads out of the boot object, and :func:`check_boot`
walks all of them before either file is written. That is the mechanical version of
"check the field names match the exporter": rename ``eligibleTotal`` in the API and
this build stops, instead of the page rendering "undefined of undefined". ``--check-js``
runs ``node --check`` over the rendered script for the other half — a syntax error in a
single-file page is a blank screen with no other warning.

Usage
-----
    # both builds, reading the local postings store (needs the backend venv)
    backend/.venv/bin/python tools/ui/build_ui.py

    # one build only, or a different API origin baked into either page
    python build_ui.py --only public
    python build_ui.py --api-base https://api.example.com
    python build_ui.py --public-api-base https://staging.example.com/prod

    # publish a page with no live path at all (renders from the snapshot alone)
    python build_ui.py --only public --no-public-api

    # no store on this machine: adapt the legacy 60-role export instead
    python build_ui.py --data ui-data.json

    # keep the snapshot it produced, so a later run can skip the 50s screen pass
    python build_ui.py --snapshot-out ui-data.full.json
    python build_ui.py --snapshot-in ui-data.full.json

    # also syntax-check the rendered JavaScript (needs node on PATH)
    python build_ui.py --check-js
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
# tools/ui/build_ui.py -> tools/ -> repo root. Derived rather than hardcoded: this
# file is tracked in a public repo, and an absolute /Users/<name>/ path in it both
# leaks a username and breaks for anyone who clones it.
DEFAULT_REPO = HERE.parents[1]

#: Evidence excerpts are capped at the same width the API caps them at. Kept as a
#: local constant *and* checked against the API's value when the backend imports,
#: so the two cannot drift apart unnoticed.
QUOTE_CAP = 180

#: How much description prose the local build bakes in per role. The API's detail
#: read returns the whole thing; the baked copy is a fallback, and 880 full
#: descriptions in one HTML file is tens of megabytes for a page that can only be
#: read one role at a time.
DEFAULT_DESC_CHARS = 900

#: Rows per gate in the baked evidence view. The API defaults to 10; the snapshot
#: takes more because it cannot page.
DEFAULT_GROUP_LIMIT = 25

#: The synthetic caller. Every handler 401s without a JWT subject, and this build
#: reads the store directly, so the subject is a label rather than a credential.
BUILD_SUBJECT = "build_ui"

#: Where the published page reads from. This is the deployed REST API in the personal
#: account — the same origin ``app/lib/config.dart`` already ships to the phone, which
#: is why it is a literal here and not a secret: it is a public, unauthenticated,
#: read-only endpoint by design, and the whole point of ``handlers/public_api.py`` is
#: that everything behind it is data this page already published.
#:
#: Baked at build time rather than discovered at run time because the page has no
#: config channel of its own — GitHub Pages serves one static file. If a CDK cutover
#: ever *replaces* the REST API (a new api id, or a custom domain in front of it), this
#: constant is the one line to change, and until it is changed the page falls back to
#: the snapshot and says so rather than showing nothing.
PUBLIC_API_BASE = "https://9iidni6dml.execute-api.us-east-1.amazonaws.com/prod"

#: The prefix the unauthenticated routes live under: ``/public/worklist``,
#: ``/public/internships``, ``/public/worklist/{id}``, ``/public/excluded``. Carried in
#: the boot object rather than hardcoded in the JavaScript so that one page can talk to
#: the authenticated routes locally and the public ones when published, with the same
#: request code and no branch per call site.
PUBLIC_API_PREFIX = "/public"

#: ``https`` is not a style preference. ``docs/index.html`` is served over TLS from
#: GitHub Pages, and a browser silently blocks an ``http://`` fetch from an ``https://``
#: page as mixed content — the page would fall back to the snapshot on every load with
#: no error anyone could see.
_HTTPS = "https://"

#: The gate whose count the internships section reconciles itself against.
INTERNSHIP_GATE = "internship_not_full_time"

#: ATS hosts whose *first path segment* is the employer's board slug. Used by
#: :func:`assert_no_demo_boards`; see that function for why the check is this narrow.
_TENANT_IN_PATH = ("greenhouse.io", "ashbyhq.com", "lever.co", "workable.com")

PLACEHOLDER = "__BOOT__"

#: The only status a handler response may have for this build to bake it into a page.
HTTP_OK = 200

# --- the shapes this build expects from the exporter --------------------------
# A missing key is a build failure. An *extra* key is not: the wire is allowed to
# grow without breaking a page that ignores what it does not render.
CARD_FIELDS = frozenset(
    {
        "id", "title", "company", "location", "url", "ats", "level", "levelSource",
        "levelWhy", "postedAt", "remote", "employmentType", "descAvailable",
        "descriptionStatus",
    }
)
SCORE_FIELDS = frozenset(
    {
        "total", "tier", "explain", "required", "preferred", "levelConfirmed",
        "resumeVariant", "unscoredReason",
    }
)
COMPONENT_FIELDS = frozenset({"covered", "total", "have", "missing"})
FUNNEL_FIELDS = frozenset(
    {
        "screened", "kept", "excluded", "gates", "gateCountTotal",
        "gateCountsOvercount", "needsLevelCheck",
    }
)
PAGE_FIELDS = frozenset({"limit", "count", "nextCursor", "hasMore"})
#: Both list routes answer this shape. ``collection`` and ``internshipTotal`` are
#: asserted rather than merely tolerated: the page renders the internships section from
#: ``internshipTotal`` and labels each collection from ``collection``, so a rename
#: upstream has to fail here instead of rendering a heading that reads "undefined".
WORKLIST_FIELDS = frozenset(
    {
        "generatedAt", "collection", "items", "page", "matched", "eligibleTotal",
        "internshipTotal", "filters", "funnel", "scoring",
    }
)
DETAIL_FIELDS = frozenset({"generatedAt", "posting", "scoring"})
EXCLUDED_FIELDS = frozenset(
    {
        "generatedAt", "excludedTotal", "counts", "gateCountTotal",
        "gateCountsOvercount", "groups", "funnel",
    }
)
GROUP_FIELDS = frozenset({"gate", "count", "items", "page"})

#: Every path the template's JavaScript reads out of the boot object, in its own
#: notation: ``[]`` descends into the first element of a list, and ``?`` marks a
#: value the page handles as null (so the walk stops there rather than failing).
#:
#: This is the contract the page codes against. It is asserted rather than reviewed
#: because the failure it prevents is silent: a renamed field renders as "undefined"
#: in a browser and raises nothing, and this build is the last place that can catch
#: it. ``CARD_FIELDS`` and friends above check what the *API* sends; this checks what
#: the *page* reads, and the two are not the same list — the page ignores
#: ``employmentType`` and ``levelSource`` on cards, and cares about ``facets``, which
#: the API does not send at all.
BOOT_PATHS: tuple[str, ...] = (
    "mode",
    "apiBase?",
    "apiPrefix",
    "apiPublic",
    "builtAt",
    "snapshot.generatedAt?",
    "snapshot.prosePublished",
    "snapshot.itemCount",
    "snapshot.eligibleTotal",
    "snapshot.internshipCount",
    "snapshot.internshipTotal",
    "snapshot.internshipGateCount",
    "snapshot.funnel.screened",
    "snapshot.funnel.kept",
    "snapshot.funnel.excluded",
    "snapshot.funnel.needsLevelCheck",
    "snapshot.scoring.available",
    "snapshot.scoring.reason?",
    "snapshot.facets.levels",
    "snapshot.facets.sources",
    "snapshot.facets.tiers",
    "snapshot.items[].id",
    "snapshot.items[].title",
    "snapshot.items[].company",
    "snapshot.items[].location?",
    "snapshot.items[].url",
    "snapshot.items[].ats",
    "snapshot.items[].level",
    "snapshot.items[].levelWhy",
    "snapshot.items[].postedAt?",
    "snapshot.items[].remote?",
    "snapshot.items[].descriptionStatus",
    "snapshot.items[].description?",
    "snapshot.items[].descriptionChars?",
    "snapshot.items[].descriptionTruncated",
    "snapshot.items[].score?.tier",
    "snapshot.items[].score?.explain",
    "snapshot.items[].score?.levelConfirmed",
    "snapshot.items[].score?.resumeVariant",
    "snapshot.items[].score?.unscoredReason?",
    "snapshot.items[].score?.required.covered",
    "snapshot.items[].score?.required.total",
    "snapshot.items[].score?.required.have",
    "snapshot.items[].score?.required.missing",
    "snapshot.items[].score?.preferred.covered",
    "snapshot.items[].score?.preferred.total",
    "snapshot.items[].score?.preferred.have",
    "snapshot.items[].score?.preferred.missing",
    # The internships collection. Same card shape as ``items`` — deliberately, since
    # the page renders both through one component — so only the fields the section
    # itself reads are listed here rather than the whole card again.
    "snapshot.internships[].id",
    "snapshot.internships[].title",
    "snapshot.internships[].company",
    "snapshot.internships[].url",
    "snapshot.internships[].level",
    "snapshot.internships[].levelWhy",
    "snapshot.internships[].postedAt?",
    "snapshot.internships[].employmentType?",
    "snapshot.internships[].descriptionStatus",
    "snapshot.internships[].score?.tier",
    "snapshot.internships[].score?.explain",
    "snapshot.excluded.excludedTotal",
    "snapshot.excluded.gateCountTotal",
    "snapshot.excluded.gateCountsOvercount",
    "snapshot.excluded.funnel.screened",
    "snapshot.excluded.groups[].gate",
    "snapshot.excluded.groups[].count",
    "snapshot.excluded.groups[].page.hasMore",
    "snapshot.excluded.groups[].page.nextCursor?",
    "snapshot.excluded.groups[].items[].id",
    "snapshot.excluded.groups[].items[].title",
    "snapshot.excluded.groups[].items[].company",
    "snapshot.excluded.groups[].items[].level",
    "snapshot.excluded.groups[].items[].levelSource",
    "snapshot.excluded.groups[].items[].reason?",
    "snapshot.excluded.groups[].items[].quote?",
)

#: ``score.total`` is a 0-100 weighted number. It is on the wire because the API
#: reports it, and it must never reach the page: labelled or not, a 0-100 number in a
#: job UI reads as a match percentage, and ``domain/gap.py`` exists to argue those are
#: incomparable. ``required.total`` and ``preferred.total`` are denominators and are
#: fine — hence the anchor on ``score``.
#:
#: The check is textual and does not parse the JavaScript, so it fires on a *comment*
#: that spells the property access too. That is deliberate: a grep cannot be fooled,
#: and the cost is one paraphrase in the template's header.
_BARE_SCORE = re.compile(r"""score\s*(?:\.\s*total\b|\[\s*['"]total['"]\s*\])""")

#: Description states, mirrored from the API so the template can be checked against
#: a closed set rather than string literals scattered through the JS.
DESC_AVAILABLE = "available"
DESC_NOT_PROVIDED = "not_provided_by_source"
DESC_EMPTY = "empty_from_source"


class BuildError(RuntimeError):
    """Something is wrong with the inputs; say what and stop, never half-write."""


# ---------------------------------------------------------------------------
# Backend access
# ---------------------------------------------------------------------------

def load_backend(repo: Path) -> tuple[Any, Any, Any]:
    """Import both read APIs and the settings, or explain what is missing.

    The public API comes back too, because the published page now reads it: the snapshot
    is checked against ``worklist_api``'s field names, but the *live* page reads
    ``public_api``'s projection, and those are two different wires. Checking only the one
    this build bakes from would leave the live path's field names asserted by nobody —
    exactly the "renders undefined in a browser and raises nothing" failure the rest of
    this file exists to prevent.
    """
    src = repo / "backend" / "src"
    if not src.is_dir():
        raise BuildError(f"no backend source at {src} — pass --repo or use --data")
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))
    try:
        # ``copilot`` ships no py.typed marker, so mypy treats it as untyped from
        # outside the backend. Ignored rather than stubbed: this function is already
        # declared to return ``Any``, so the ignore hides nothing — every field this
        # script depends on is asserted at runtime by ``check_fields``/``check_boot``.
        from copilot.config import load_settings  # type: ignore[import-untyped] # noqa: PLC0415
        from copilot.handlers import (  # type: ignore[import-untyped] # noqa: PLC0415
            public_api,
            worklist_api,
        )
    except ImportError as exc:  # pydantic et al. live in the backend venv
        raise BuildError(
            f"cannot import the backend ({exc}). Run this with the backend venv:\n"
            f"  {repo / 'backend' / '.venv' / 'bin' / 'python'} {Path(__file__).name}\n"
            "or build from the legacy export with --data ui-data.json"
        ) from exc
    api = worklist_api
    if api.QUOTE_MAX_CHARS != QUOTE_CAP:
        raise BuildError(
            f"quote cap drifted: api={api.QUOTE_MAX_CHARS} build={QUOTE_CAP}. "
            "Update QUOTE_CAP here deliberately — this cap is a publishing decision."
        )
    if public_api.PUBLIC_PREFIX != PUBLIC_API_PREFIX:
        raise BuildError(
            f"public route prefix drifted: api={public_api.PUBLIC_PREFIX} "
            f"build={PUBLIC_API_PREFIX}. The published page would read the "
            "authenticated routes, 401 on every request, and silently fall back."
        )
    return api, public_api, load_settings()


def load_demo_check(repo: Path) -> Any | None:
    """The domain's demo-board rule, or ``None`` if the source tree is not here.

    Separate from :func:`load_backend` because it has to work on the builds that have
    no backend venv: ``domain/demo_boards.py`` is pure ``re`` and stdlib, so a
    ``--snapshot-in`` or ``--data`` build can still run :func:`assert_no_demo_boards`.
    ``None`` is returned rather than raised only when the checkout itself is absent, and
    the caller prints that the check was skipped — a silently skipped safety check is
    worse than a loud one that could not run.
    """
    src = repo / "backend" / "src"
    if not src.is_dir():
        return None
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))
    try:
        from copilot.domain.demo_boards import (  # type: ignore[import-untyped] # noqa: PLC0415
            is_demo_tenant,
        )
    except ImportError:  # pragma: no cover - stdlib-only module
        return None
    return is_demo_tenant


def _event(path: str, *, query: dict[str, str] | None = None,
           path_params: dict[str, str] | None = None) -> dict[str, Any]:
    """One API Gateway HTTP-API event, shaped exactly as the handlers read it."""
    return {
        "routeKey": f"GET {path}",
        "rawPath": path,
        "requestContext": {
            "authorizer": {"jwt": {"claims": {"sub": BUILD_SUBJECT}}},
            "http": {"method": "GET"},
        },
        "queryStringParameters": dict(query or {}),
        "pathParameters": dict(path_params or {}),
    }


def _ok(response: dict[str, Any], what: str) -> dict[str, Any]:
    """Unwrap a handler response, refusing to bake a page out of an error body."""
    body: dict[str, Any] = json.loads(response["body"] or "{}")
    if response["statusCode"] != HTTP_OK:
        raise BuildError(f"{what} returned {response['statusCode']}: {body.get('error')}")
    return body


# ---------------------------------------------------------------------------
# Collecting the snapshot
# ---------------------------------------------------------------------------

def _pages(read: Any, store: Any, path: str, *, resume: str, now: datetime, cache: Any,
           limit: int) -> Iterator[dict[str, Any]]:
    """Walk one list route to the end with its own keyset cursor.

    Paging rather than one big read on purpose: the endpoint refuses a limit above
    100, and a build that quietly needed a "give me everything" mode would be
    testing a code path the browser never uses.

    Parameterised over the handler rather than copied for ``/internships``, because a
    second copy is where the two would drift — and the cursor rules are exactly the
    thing that must not drift: the API folds the collection name into the cursor
    fingerprint, so a ``/worklist`` cursor handed to ``/internships`` is refused with
    ``cursor_filter_mismatch``. One walker cannot make that mistake.
    """
    cursor: str | None = None
    seen = 0
    for _ in range(1000):  # 100k rows at limit=100; a cursor that stalls must not hang
        query = {"limit": str(limit)}
        if cursor:
            query["cursor"] = cursor
        page = _ok(
            read(store, _event(path, query=query), resume_text=resume, now=now, cache=cache),
            f"GET {path}",
        )
        yield page
        seen += page["page"]["count"]
        cursor = page["page"]["nextCursor"]
        if cursor is None:
            return
    raise BuildError(f"pagination did not terminate after {seen} rows")


def _collection(read: Any, store: Any, path: str, *, collection: str, resume: str,
                now: datetime, cache: Any, limit: int,
                verbose: bool) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Every row of one collection, plus the first page's header.

    The header is kept because it carries the totals and the funnel, and returning it
    rather than re-reading page 1 later is what keeps the numbers on the page from
    coming from a different pass than the rows.
    """
    items: list[dict[str, Any]] = []
    head: dict[str, Any] | None = None
    for page in _pages(read, store, path, resume=resume, now=now, cache=cache, limit=limit):
        check_fields(page, WORKLIST_FIELDS, f"GET {path}")
        check_fields(page["page"], PAGE_FIELDS, f"GET {path} .page")
        if page["collection"] != collection:
            raise BuildError(
                f"GET {path} answered collection {page['collection']!r}, expected "
                f"{collection!r} — the two collections must not be served from one population"
            )
        head = head or page
        items.extend(page["items"])
        if verbose:
            print(f"  {collection}: {len(items)}/{page['matched']}", file=sys.stderr)
    if head is None:  # pragma: no cover - _pages always yields at least once
        raise BuildError(f"GET {path} yielded no pages")
    if len(items) != head["matched"]:
        raise BuildError(
            f"GET {path} paged {len(items)} rows but matched {head['matched']}"
        )
    for item in items:
        check_fields(item, CARD_FIELDS, f"{collection} item {item.get('id')}")
        _check_score(item.get("score"), item.get("id", "?"))
    return items, head


def _truncate(text: str, limit: int) -> tuple[str, bool]:
    """Cut prose at a word boundary and say that it was cut."""
    if len(text) <= limit:
        return text, False
    cut = text[:limit]
    space = cut.rfind(" ")
    if space > limit * 0.6:
        cut = cut[:space]
    return cut.rstrip() + "…", True


def collect_from_api(api: Any, reader: tuple[Any, str, Any], *, desc_chars: int,
                     group_limit: int, now: datetime,
                     verbose: bool = True) -> dict[str, Any]:
    """Build the snapshot by driving the read API over the local store."""
    store, resume, cache = reader

    walk = {
        "resume": resume, "now": now, "cache": cache, "limit": api.MAX_LIMIT,
        "verbose": verbose,
    }
    items, head = _collection(
        api.list_worklist, store, "/worklist", collection="worklist", **walk
    )
    check_fields(head["funnel"], FUNNEL_FIELDS, "funnel")
    interns, intern_head = _collection(
        api.list_internships, store, "/internships", collection="internships", **walk
    )
    # The two routes report the same population size from different sides — the
    # worklist's ``internshipTotal`` and the internships route's own ``matched``. They
    # are compared rather than trusted because a mismatch would mean the section header
    # is counting one population and the rows underneath it are another.
    if intern_head["matched"] != head["internshipTotal"]:
        raise BuildError(
            f"GET /internships matched {intern_head['matched']} but GET /worklist "
            f"reported internshipTotal={head['internshipTotal']}"
        )

    if verbose:
        print(f"  details: 0/{len(items) + len(interns)}", file=sys.stderr)
    for index, item in enumerate([*items, *interns], 1):
        detail = _ok(
            api.get_posting(
                store, _event("/worklist/{id}", path_params={"id": item["id"]}),
                resume_text=resume, now=now, cache=cache,
            ),
            f"GET /worklist/{item['id']}",
        )
        check_fields(detail, DETAIL_FIELDS, "GET /worklist/{id}")
        posting = detail["posting"]
        item["screening"] = posting["screening"]
        item["descriptionChars"] = posting["descriptionChars"]
        prose = posting["description"]
        if prose is None:
            item["description"] = None
            item["descriptionTruncated"] = False
        else:
            cut, truncated = _truncate(prose, desc_chars)
            item["description"] = cut
            item["descriptionTruncated"] = truncated
        if verbose and index % 200 == 0:
            print(f"  details: {index}/{len(items) + len(interns)}", file=sys.stderr)

    excluded = _ok(
        api.list_excluded(
            store, _event("/excluded", query={"limit": str(group_limit)}),
            now=now, cache=cache,
        ),
        "GET /excluded",
    )
    check_fields(excluded, EXCLUDED_FIELDS, "GET /excluded")
    for group in excluded["groups"]:
        check_fields(group, GROUP_FIELDS, f"excluded group {group.get('gate')}")
        for row in group["items"]:
            check_fields(row, CARD_FIELDS, f"excluded item {row.get('id')}")

    return _assemble(
        source="api",
        generated_at=head["generatedAt"],
        items=items,
        internships=interns,
        internship_total=head["internshipTotal"],
        eligible_total=head["eligibleTotal"],
        funnel=head["funnel"],
        scoring=head["scoring"],
        excluded=excluded,
        desc_chars=desc_chars,
    )


#: The two facts every public payload stamps on itself, and which the page relies on.
PUBLISHED_FIELDS = frozenset({"prosePublished", "quoteMaxChars"})


def build_reader(api: Any, settings: Any) -> tuple[Any, str, Any]:
    """The store, the résumé text, and one shared screened-index cache.

    Shared rather than built per caller: a cold :class:`IndexCache` screens all 25,294
    open postings, which is most of this build's runtime. The snapshot collection and the
    public-route contract check therefore run against the *same* index — which also
    means the contract check compares like with like instead of two passes that could
    have seen different corpora.
    """
    return (
        api.build_store(settings),
        api.load_resume_text(settings),
        api.IndexCache(ttl_seconds=3600.0),
    )


def check_public_contract(public: Any, reader: tuple[Any, str, Any], *, now: datetime) -> None:
    """Drive the four *unauthenticated* routes and assert what the live page reads.

    The rest of this file checks the snapshot, which is built from ``worklist_api``. The
    published page's primary path is ``public_api``, whose response is a separate
    allowlist projection — so a field renamed *there* would show up as "undefined" on a
    live page while every check in this build stayed green. This closes that: same
    method as :func:`check_fields`, applied to the wire the page actually reads.

    Three things beyond field names, each a failure this page would otherwise ship:

    * ``prosePublished`` false and ``quoteMaxChars`` equal to :data:`QUOTE_CAP` — the
      page prints "the description is not republished here" based on the first, and the
      second is the publishing decision this build re-asserts everywhere else.
    * no ``description`` key anywhere in any of the four bodies, at any depth. Not "no
      prose in the fields we remembered to look at": a recursive scan, because the whole
      point of the public route is that this text is not on the wire.
    * the internships route answers a different ``collection`` than the worklist route,
      since serving one population under both names is precisely the leak the split
      exists to prevent.
    """
    store, resume, cache = reader

    def read(path: str, **kwargs: Any) -> dict[str, Any]:
        response = public.route(
            store, _event(path, **kwargs), resume_text=resume, now=now, cache=cache
        )
        body: dict[str, Any] = json.loads(response["body"] or "{}")
        if response["statusCode"] != HTTP_OK:
            raise BuildError(f"GET {path} returned {response['statusCode']}: {body.get('error')}")
        for key, want in (("Cache-Control", "public"), ("Access-Control-Allow-Origin", "*")):
            got = str(response["headers"].get(key, ""))
            if want not in got:
                raise BuildError(f"GET {path}: {key} is {got!r}, expected to contain {want!r}")
        check_fields(body, PUBLISHED_FIELDS, f"GET {path}")
        if body["prosePublished"] is not False or body["quoteMaxChars"] != QUOTE_CAP:
            raise BuildError(
                f"GET {path}: prosePublished={body['prosePublished']!r} "
                f"quoteMaxChars={body['quoteMaxChars']!r} — expected False and {QUOTE_CAP}"
            )
        _assert_no_description_key(body, where=f"GET {path}")
        return body

    prefix = PUBLIC_API_PREFIX
    worklist = read(f"{prefix}/worklist", query={"limit": "1"})
    interns = read(f"{prefix}/internships", query={"limit": "1"})
    for body, collection in ((worklist, "worklist"), (interns, "internships")):
        what = f"GET {prefix}/{collection}"
        check_fields(body, WORKLIST_FIELDS, what)
        check_fields(body["page"], PAGE_FIELDS, f"{what} .page")
        check_fields(body["funnel"], FUNNEL_FIELDS, f"{what} .funnel")
        if body["collection"] != collection:
            raise BuildError(f"{what} answered collection {body['collection']!r}")
        for item in body["items"]:
            check_fields(item, CARD_FIELDS | {"descriptionWithheld"}, f"{what} item")
            _check_score(item.get("score"), item.get("id", "?"))
    if INTERNSHIP_GATE not in worklist["funnel"]["gates"]:
        raise BuildError(
            f"GET {prefix}/worklist: funnel.gates has no {INTERNSHIP_GATE!r} — the "
            "internships section reconciles its count against that number"
        )

    rows = worklist["items"]
    if rows:
        detail = read(f"{prefix}/worklist/{rows[0]['id']}", path_params={"id": rows[0]["id"]})
        check_fields(detail, {"generatedAt", "posting", "scoring"}, f"GET {prefix}/worklist/{{id}}")
        check_fields(
            detail["posting"],
            CARD_FIELDS | {"descriptionChars", "descriptionWithheld", "screening"},
            f"GET {prefix}/worklist/{{id}} .posting",
        )

    excluded = read(f"{prefix}/excluded", query={"limit": "1"})
    check_fields(excluded, EXCLUDED_FIELDS, f"GET {prefix}/excluded")
    for group in excluded["groups"]:
        check_fields(group, GROUP_FIELDS, f"GET {prefix}/excluded group")


def _assert_no_description_key(node: Any, *, where: str) -> None:
    """No ``description`` field anywhere in a published body, at any depth."""
    if isinstance(node, dict):
        if "description" in node:
            raise BuildError(f"{where}: a published body carries a 'description' field")
        for key, value in node.items():
            _assert_no_description_key(value, where=f"{where}.{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            _assert_no_description_key(value, where=f"{where}[{index}]")


def _check_score(score: dict[str, Any] | None, posting_id: str) -> None:
    """A score is either absent (scoring unavailable) or complete with components."""
    if score is None:
        return
    check_fields(score, SCORE_FIELDS, f"score for {posting_id}")
    for part in ("required", "preferred"):
        check_fields(score[part], COMPONENT_FIELDS, f"score.{part} for {posting_id}")


def check_fields(obj: Any, expected: frozenset[str], what: str) -> None:
    """Fail the build when the exporter no longer carries a field the page renders."""
    if not isinstance(obj, dict):
        raise BuildError(f"{what}: expected an object, got {type(obj).__name__}")
    missing = expected - set(obj)
    if missing:
        raise BuildError(f"{what}: missing field(s) {sorted(missing)}")


def _walk_path(node: Any, steps: list[str], *, path: str) -> None:
    """Follow one :data:`BOOT_PATHS` entry, raising :class:`BuildError` if it breaks."""
    if not steps:
        return
    step, rest = steps[0], steps[1:]
    optional = step.endswith("?")
    name = step.removesuffix("?").removesuffix("[]")
    is_list = step.removesuffix("?").endswith("[]")

    if not isinstance(node, dict):
        raise BuildError(f"{path}: expected an object at {name!r}, got {type(node).__name__}")
    if name not in node:
        raise BuildError(f"{path}: no field {name!r} (have {sorted(node)})")
    value = node[name]

    if value is None:
        if optional:
            return  # the page handles null here
        raise BuildError(f"{path}: {name!r} is null, and the page does not expect that")
    if is_list:
        if not isinstance(value, list):
            raise BuildError(f"{path}: {name!r} is {type(value).__name__}, not a list")
        if not value:
            return  # an empty list is legitimate; there is nothing to descend into
        _walk_path(value[0], rest, path=path)
        return
    _walk_path(value, rest, path=path)


def check_boot(boot: dict[str, Any], *, what: str) -> None:
    """Assert every field the page reads is present in the boot object it will get."""
    for path in BOOT_PATHS:
        _walk_path(boot, path.split("."), path=f"{what}: {path}")


def assert_no_bare_score(template: str) -> None:
    """The page must never render ``score.total``. See :data:`_BARE_SCORE`."""
    found = _BARE_SCORE.search(template)
    if found:
        raise BuildError(
            f"template reads {found.group(0)!r}. The score is reported as a tier plus its "
            "components, never as a number — see domain/gap.py."
        )


def assert_collections_disjoint(snapshot: dict[str, Any], *, what: str) -> None:
    """No posting may appear in both the worklist and the internships section.

    This is the regression the internships section is one edit away from: internships
    used to reach the worklist — 50 of them did, and 5 ranked *exact match*, which is
    how the leak was found in the first place. The two arrives here from two different
    routes over two populations the API builds separately, so the only way they can
    overlap is a real defect in the screening pass, and it would show on the page as a
    "Software Engineering Intern" sitting in a list of full-time openings.
    """
    worklist = {item["id"] for item in snapshot["items"]}
    both = sorted(worklist & {item["id"] for item in snapshot["internships"]})
    if both:
        raise BuildError(
            f"{what}: {len(both)} posting(s) are in both the worklist and the "
            f"internships section, e.g. {both[:3]} — they must be disjoint"
        )


def assert_internships_reconcile(snapshot: dict[str, Any], *, what: str) -> None:
    """The section's own count must match its total and sit inside the gate count.

    Three numbers describe internships and a visitor can see two of them next to each
    other, so they have to agree: the rows held, the collection's size, and the 318
    postings the internship gate removed. The section is a *subset* of what the gate
    caught — never larger — because it is that set re-screened with every other gate
    still applied. A count larger than the gate's would mean the section had stopped
    being derived from the exclusion it claims to explain.
    """
    held, total = snapshot["internshipCount"], snapshot["internshipTotal"]
    gated = snapshot["internshipGateCount"]
    if held != total:
        raise BuildError(
            f"{what}: the snapshot holds {held} internships but reports {total} — "
            "the section header would contradict the rows underneath it"
        )
    if total > gated:
        raise BuildError(
            f"{what}: {total} internships from a gate that fired {gated} times; the "
            "collection is a subset of the postings that gate removed, so this is a bug"
        )


def _board_slugs(url: str) -> tuple[str, ...]:
    """The board slugs in one ATS posting URL: the tenant, and nothing else.

    Deliberately narrow. The card does not publish ``tenant`` (the public projection
    drops it), so the slug has to be recovered from the URL — but ``is_demo_tenant``
    fires on ``…-demo`` as a fused suffix, and a real posting titled "Sales Demo
    Engineer" has a *job* slug ending in ``-demo``. Checking every path segment would
    fail the build on a legitimate role. So: the first path segment for the four boards
    that put the tenant there, plus the leading host label, which is where Workday puts
    it (``<tenant>.wd1.myworkdayjobs.com``).
    """
    match = re.match(r"https?://([^/?#]+)(/[^?#]*)?", url.strip())
    if match is None:
        return ()
    host = match.group(1).lower()
    slugs = [host.split(".")[0]]
    segments = [part for part in (match.group(2) or "").split("/") if part]
    if segments and any(host.endswith(vendor) for vendor in _TENANT_IN_PATH):
        slugs.append(segments[0])
    return tuple(slugs)


def assert_no_demo_boards(snapshot: dict[str, Any], is_demo: Any, *, what: str) -> None:
    """An ATS vendor's demo fixtures must not reach the published page.

    ``leverdemo`` is Lever's public sandbox: it answers with invented roles dated as far
    back as 2013, and they screen exactly like real ones because structurally they *are*
    real postings. Publishing them is the original sin this whole project was a reaction
    to — a page that served invented companies as real matches — and the internships
    collection is a fresh way to re-introduce it, since 12 of the 318 postings the
    internship gate caught are from those boards.

    The rule itself is not re-implemented here: ``domain.demo_boards.is_demo_tenant`` is
    imported and called, because a second copy of that regex is how the two would
    disagree, and the domain module's own docstring says as much.
    """
    for collection in ("items", "internships"):
        for item in snapshot[collection]:
            for slug in _board_slugs(str(item.get("url") or "")):
                if is_demo(slug):
                    raise BuildError(
                        f"{what}: {collection} carries {item['id']} from demo board "
                        f"{slug!r} ({item.get('company')!r}) — vendor fixtures are not jobs"
                    )


def check_js(html: str) -> None:
    """Syntax-check the rendered page's single script block with ``node --check``.

    A one-file page with a syntax error is a blank screen: no console anyone will see,
    no failing test, nothing. Opt-in because it is the only step that needs node.
    """
    blocks = re.findall(r"<script>(.*?)</script>", html, re.S)
    if len(blocks) != 1:
        raise BuildError(f"expected exactly one inline script block, found {len(blocks)}")
    node = shutil.which("node")
    if node is None:
        raise BuildError("--check-js needs node on PATH; drop the flag to skip it")
    with tempfile.TemporaryDirectory() as tmp:
        script = Path(tmp) / "page.js"
        script.write_text(blocks[0], encoding="utf-8")
        done = subprocess.run(
            [node, "--check", str(script)], capture_output=True, text=True, check=False
        )
    if done.returncode != 0:
        raise BuildError(f"rendered JavaScript does not parse:\n{done.stderr.strip()}")


# ---------------------------------------------------------------------------
# The legacy export, adapted to the same shape
# ---------------------------------------------------------------------------

def collect_from_legacy(path: Path) -> dict[str, Any]:
    """Adapt ``ui-data.json`` (the pre-API export) into the wire shape.

    Kept so the site builds on a machine with no postings store. It is a lesser
    build and says so: it holds 60 of 880 roles and carries no scores, which the
    page reports rather than papering over.
    """
    raw = json.loads(path.read_text(encoding="utf-8"))
    funnel_in = raw.get("funnel", {})
    funnel = {
        # "fetched" was the old name for the same count.
        "screened": funnel_in.get("screened", funnel_in.get("fetched", 0)),
        "kept": funnel_in.get("kept", 0),
        "excluded": funnel_in.get("excluded", 0),
        "gates": funnel_in.get("gates", raw.get("excludedCounts", {})),
        "gateCountTotal": funnel_in.get("gateCountTotal", 0),
        "gateCountsOvercount": True,
        "needsLevelCheck": funnel_in.get("needsLevelCheck", 0),
    }

    items: list[dict[str, Any]] = []
    for row in raw.get("worklist", []):
        items.append(_legacy_card(row) | {
            "score": None,
            "screening": None,
            "description": row.get("descPreview"),
            "descriptionChars": None,
            "descriptionTruncated": bool(row.get("descPreview")),
        })

    counts = raw.get("excludedCounts", {})
    groups = [
        {
            "gate": gate,
            "count": counts.get(gate, len(rows)),
            "items": [
                _legacy_card(row) | {"reason": row.get("reason"), "quote": None}
                for row in rows
            ],
            "page": {
                "limit": len(rows), "count": len(rows),
                "nextCursor": None, "hasMore": counts.get(gate, 0) > len(rows),
            },
        }
        for gate, rows in raw.get("excludedSamples", {}).items()
    ]
    excluded = {
        "generatedAt": raw.get("generatedAt"),
        "excludedTotal": funnel["excluded"],
        "counts": counts,
        "gateCountTotal": funnel["gateCountTotal"],
        "gateCountsOvercount": True,
        "groups": groups,
        "funnel": funnel,
    }
    return _assemble(
        source="legacy-snapshot",
        generated_at=raw.get("generatedAt"),
        items=items,
        # The legacy export predates the internships collection entirely. Empty with a
        # ``0`` total is the truthful answer: the section renders its "nothing in this
        # data" state rather than borrowing a count from a population it does not hold.
        internships=[],
        internship_total=0,
        eligible_total=raw.get("worklistTotal", len(items)),
        funnel=funnel,
        # Not "no résumé configured": the export simply predates the scorer, and
        # claiming a missing résumé would misreport why there are no numbers.
        scoring={"available": False, "reason": "not_in_snapshot"},
        excluded=excluded,
        desc_chars=0,
    )


def _legacy_card(row: dict[str, Any]) -> dict[str, Any]:
    """One legacy row as a card, with ``descriptionStatus`` reconstructed."""
    if not row.get("descAvailable"):
        status = DESC_NOT_PROVIDED
    elif row.get("descPreview"):
        status = DESC_AVAILABLE
    else:
        status = DESC_EMPTY
    card = {key: row.get(key) for key in CARD_FIELDS if key != "descriptionStatus"}
    card["descriptionStatus"] = status
    return card


# ---------------------------------------------------------------------------
# Assembly, facets, stripping
# ---------------------------------------------------------------------------

def _assemble(*, source: str, generated_at: str | None, items: list[dict[str, Any]],
              internships: list[dict[str, Any]], internship_total: int,
              eligible_total: int, funnel: dict[str, Any], scoring: dict[str, Any],
              excluded: dict[str, Any], desc_chars: int) -> dict[str, Any]:
    """The boot payload. ``items`` and ``internships`` stay separate all the way out.

    ``internshipGateCount`` travels beside ``internshipTotal`` because the two numbers
    disagree and the difference is the honest part: the gate fires on 318 postings and
    48 of them are software internships that pass every *other* gate. Shipping only the
    48 makes the section look like it lost 270 rows; shipping only the 318 would put
    "Marketing Intern" on the page. Both, labelled, is the only version that is true.
    """
    return {
        "source": source,
        "generatedAt": generated_at,
        "prosePublished": True,
        "descChars": desc_chars,
        "items": items,
        "itemCount": len(items),
        "eligibleTotal": eligible_total,
        "internships": internships,
        "internshipCount": len(internships),
        "internshipTotal": internship_total,
        "internshipGateCount": funnel.get("gates", {}).get(INTERNSHIP_GATE, 0),
        "funnel": funnel,
        "scoring": scoring,
        "excluded": excluded,
        # Tallied over ``items`` only, and read only by the worklist's filter chips.
        # The internships section deliberately has no chips of its own: 48 rows do not
        # need filtering, and a second facet block would be the thing that eventually
        # printed 813-row counts above a 48-row list.
        "facets": _facets(items),
    }


def _facets(items: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Counts the page shows on its filter chips, tallied from the rows themselves.

    Deliberately not carried over from the old export's ``levels``/``sources``
    blocks: those were tallied over a different population than the rows shipped,
    so a chip could claim 183 entry-level roles over a list that held 12.
    """
    levels: dict[str, int] = {}
    sources: dict[str, int] = {}
    tiers: dict[str, int] = {}
    companies: set[str] = set()
    for item in items:
        levels[item["level"]] = levels.get(item["level"], 0) + 1
        ats = (item.get("ats") or "unknown").lower()
        sources[ats] = sources.get(ats, 0) + 1
        score = item.get("score")
        if score is not None:
            tiers[score["tier"]] = tiers.get(score["tier"], 0) + 1
        if item.get("company"):
            companies.add(str(item["company"]).strip().lower())
    return {
        "levels": levels,
        "sources": sources,
        "tiers": tiers,
        "companies": len(companies),
    }


def _cap(text: Any) -> Any:
    """Re-apply the excerpt cap to any string leaving in the published build."""
    if not isinstance(text, str):
        return text
    cleaned = " ".join(text.split())
    if len(cleaned) <= QUOTE_CAP:
        return cleaned
    return cleaned[: QUOTE_CAP - 1] + "…"


def strip_prose(snapshot: dict[str, Any]) -> dict[str, Any]:
    """A publishable copy: no description prose, evidence excerpts capped.

    ``descriptionStatus`` and ``descriptionChars`` survive, because "this posting
    has 4,812 characters we are not reprinting" and "this source publishes no
    description at all" are different facts and the page says different things
    about them.
    """
    public: dict[str, Any] = json.loads(json.dumps(snapshot))  # deep copy; plain JSON
    public["prosePublished"] = False
    public["descChars"] = 0
    # Both collections, from one list. An internship's description is a company's
    # description: the collection it is filed under changes nothing about who owns the
    # text, and "we stripped the worklist and forgot the new array" is precisely the
    # shape of regression a second loop invites.
    for item in (*public["items"], *public["internships"]):
        item["descriptionWithheld"] = item["descriptionStatus"] == DESC_AVAILABLE
        item["description"] = None
        item["descriptionTruncated"] = False
        screening = item.get("screening")
        if screening:
            _cap_screening(screening)
    for group in public["excluded"]["groups"]:
        for row in group["items"]:
            row["reason"] = _cap(row.get("reason"))
            row["quote"] = _cap(row.get("quote"))
    return public


def _cap_screening(screening: dict[str, Any]) -> None:
    eligibility = screening.get("eligibility") or {}
    for evidence in eligibility.get("evidence", []):
        evidence["quote"] = _cap(evidence.get("quote"))
    for exclusion in screening.get("exclusions", []):
        exclusion["reason"] = _cap(exclusion.get("reason"))
        exclusion["quote"] = _cap(exclusion.get("quote"))


# ---------------------------------------------------------------------------
# Rendering + output checks
# ---------------------------------------------------------------------------

_ASSET_PATTERNS = (
    re.compile(r"<script[^>]+\ssrc\s*=", re.I),
    re.compile(r"<link[^>]+\shref\s*=", re.I),
    re.compile(r"@import", re.I),
    re.compile(r"url\(\s*['\"]?https?:", re.I),
    re.compile(r"<iframe|<img[^>]+\ssrc\s*=\s*['\"]https?:", re.I),
)


def render(template: str, boot: dict[str, Any]) -> str:
    if template.count(PLACEHOLDER) != 1:
        raise BuildError(
            f"template must contain {PLACEHOLDER} exactly once "
            f"(found {template.count(PLACEHOLDER)})"
        )
    payload = json.dumps(boot, ensure_ascii=False, separators=(",", ":"))
    # ``</script>`` and U+2028/9 inside a string literal end the script block or
    # break the parse. Escaping the ``<`` keeps the JSON valid and the HTML intact.
    payload = (
        payload.replace("</", "<\\/")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )
    return template.replace(PLACEHOLDER, payload)


def assert_self_contained(html: str, *, what: str) -> None:
    """No CDN, no webfont, no remote stylesheet — the fallback must render offline."""
    for pattern in _ASSET_PATTERNS:
        found = pattern.search(html)
        if found:
            raise BuildError(f"{what}: external asset reference {found.group(0)!r}")


def assert_no_prose(boot: dict[str, Any], *, what: str) -> None:
    """The published build carries no description text at all."""
    snapshot = boot["snapshot"]
    if snapshot["prosePublished"] is not False or snapshot["descChars"] != 0:
        raise BuildError(
            f"{what}: prosePublished={snapshot['prosePublished']!r} "
            f"descChars={snapshot['descChars']!r} — the published build declares neither"
        )
    for item in (*snapshot["items"], *snapshot["internships"]):
        if item["description"] is not None:
            raise BuildError(f"{what}: {item['id']} still carries description prose")
    for group in boot["snapshot"]["excluded"]["groups"]:
        for row in group["items"]:
            for key in ("reason", "quote"):
                value = row.get(key)
                if isinstance(value, str) and len(value) > QUOTE_CAP:
                    raise BuildError(
                        f"{what}: {row['id']} {key} is {len(value)} chars, "
                        f"over the {QUOTE_CAP}-char excerpt cap"
                    )


#: The one write the page can make, and the guard that must sit in front of it.
_POST = "method: 'POST'"
_WRITE_GUARD = "if (BOOT.apiPublic) throw new SourceError("


def assert_public_cannot_write(html: str) -> None:
    """The published page must be structurally unable to POST anything.

    ``POST /applied`` records that a human applied. It is behind the Cognito authorizer
    and is not part of the public read API at all, so a click on the published page
    could at best 401 — but "at best" is not a guarantee, and *which roles he applied to
    is private*. Both builds ship the same JavaScript, so the protection cannot be
    "the public build omits the code": it has to be a runtime guard, at the write, in
    the shipped file.

    Asserted textually because the failure is silent in exactly the way a browser
    hides: nobody clicks "Record that I applied" on a page they are reading anonymously,
    so a missing guard would not surface until it mattered. Three things are checked —
    there is only one writer, the guard exists, and the guard is inside that writer's
    body rather than somewhere harmlessly above it.
    """
    posts = [found.start() for found in re.finditer(re.escape(_POST), html)]
    if len(posts) != 1:
        raise BuildError(
            f"expected exactly one {_POST!r} in the page, found {len(posts)}. Every "
            "write must go through the one guarded helper — see assert_public_cannot_write"
        )
    guards = [found.start() for found in re.finditer(re.escape(_WRITE_GUARD), html)]
    if len(guards) != 1:
        raise BuildError(
            f"the write guard {_WRITE_GUARD!r} appears {len(guards)} times, expected once"
        )
    post_at, guard_at = posts[0], guards[0]
    opens = html.rfind("\nfunction ", 0, post_at)
    if not opens < guard_at < post_at:
        raise BuildError(
            "the write guard is not inside the function that performs the POST — "
            "the published page could reach a write"
        )


def assert_api_wiring(boot: dict[str, Any], *, what: str, public: bool) -> None:
    """The three boot values that decide what the page may talk to.

    ``apiPublic`` picks the route prefix *and* arms the write guard, so a public build
    with it unset would read the authenticated paths (401 on every request, silent
    fallback to the snapshot, and a live page that is quietly never live) while also
    disarming the guard above. Checked here rather than trusted, because both mistakes
    look like a working page.
    """
    base, prefix, is_public = boot["apiBase"], boot["apiPrefix"], boot["apiPublic"]
    if is_public is not public:
        raise BuildError(f"{what}: apiPublic is {is_public!r}, expected {public!r}")
    if base is None:
        if prefix:
            raise BuildError(f"{what}: no apiBase, but apiPrefix is {prefix!r}")
        return
    if public:
        if not base.startswith(_HTTPS):
            raise BuildError(
                f"{what}: apiBase {base!r} is not https — a browser blocks a plain-http "
                "fetch from an https page as mixed content, with no error the visitor sees"
            )
        if prefix != PUBLIC_API_PREFIX:
            raise BuildError(
                f"{what}: apiPrefix is {prefix!r}, but the unauthenticated routes live "
                f"under {PUBLIC_API_PREFIX!r}"
            )


def write(path: Path, html: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html, encoding="utf-8")
    print(f"wrote {path}  ({len(html.encode()) / 1024:.0f} KiB)")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--repo", type=Path, default=DEFAULT_REPO,
                        help="career-copilot checkout (default: %(default)s)")
    parser.add_argument("--template", type=Path, default=HERE / "index.template.html")
    # Written into build/, which is gitignored: the local build embeds full
    # description prose for 813 postings and must never reach a public commit.
    parser.add_argument("--local-out", type=Path, default=DEFAULT_REPO / "build" / "ui.html")
    parser.add_argument("--public-out", type=Path, default=None,
                        help="default: <repo>/docs/index.html")
    parser.add_argument("--only", choices=("local", "public", "both"), default="both")
    parser.add_argument("--data", type=Path, default=None,
                        help="build from a legacy ui-data.json export instead of the store")
    parser.add_argument("--snapshot-in", type=Path, default=None,
                        help="reuse a snapshot written by --snapshot-out, skipping the "
                             "50s screening pass. No store or backend venv needed.")
    parser.add_argument("--snapshot-out", type=Path, default=None,
                        help="also write the collected snapshot as JSON")
    parser.add_argument("--api-base", default="",
                        help="authenticated API origin baked into the local build; "
                             "empty means same origin")
    parser.add_argument("--public-api-base", default=PUBLIC_API_BASE,
                        help="origin of the unauthenticated read API for the published "
                             "page (default: %(default)s)")
    parser.add_argument("--no-public-api", action="store_true",
                        help="publish a page with no live path at all: it renders from "
                             "the baked snapshot and says so")
    parser.add_argument("--desc-chars", type=int, default=DEFAULT_DESC_CHARS)
    parser.add_argument("--group-limit", type=int, default=DEFAULT_GROUP_LIMIT)
    parser.add_argument("--check-js", action="store_true",
                        help="syntax-check the rendered script with node --check")
    parser.add_argument("--quiet", action="store_true")
    return parser.parse_args(argv)


def load_snapshot(path: Path) -> dict[str, Any]:
    """Read back a ``--snapshot-out`` file, checked rather than trusted.

    ``internships`` is required, so a snapshot written before that collection existed
    is refused with an instruction instead of building a page whose internships section
    reads "0 of 0" over a corpus that has 48 of them.
    """
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or raw.get("source") is None:
        raise BuildError(f"{path} is not a snapshot written by --snapshot-out")
    missing = {"internships", "internshipTotal", "internshipGateCount"} - set(raw)
    if missing:
        raise BuildError(
            f"{path} predates the internships collection (no {sorted(missing)}). "
            "Re-collect it: drop --snapshot-in and add --snapshot-out."
        )
    snapshot: dict[str, Any] = raw
    return snapshot


def collect(args: argparse.Namespace, *, now: datetime) -> dict[str, Any]:
    """The snapshot, from whichever of the three inputs was asked for, checked."""
    if args.snapshot_in is not None:
        snapshot = load_snapshot(args.snapshot_in)
        print(f"snapshot: {args.snapshot_in} (reused, collected {snapshot['generatedAt']})")
    elif args.data is not None:
        snapshot = collect_from_legacy(args.data)
        print(f"snapshot: {args.data} (legacy export, no scores)")
    else:
        api, public, settings = load_backend(args.repo)
        reader = build_reader(api, settings)
        snapshot = collect_from_api(
            api, reader, desc_chars=args.desc_chars,
            group_limit=args.group_limit, now=now, verbose=not args.quiet,
        )
        # The live path the published page reads. Checked here rather than on the page,
        # where a renamed field is a silent "undefined" and not an error. Runs on the
        # index the snapshot was collected from, so it costs four handler calls.
        check_public_contract(public, reader, now=now)
        print(f"public read API contract: 4 routes OK under {PUBLIC_API_PREFIX}")
    partial = snapshot["itemCount"] < snapshot["eligibleTotal"]
    print(
        f"snapshot holds {snapshot['itemCount']} of {snapshot['eligibleTotal']} "
        f"eligible roles"
        + ("  ← partial" if partial else "")
    )
    print(
        f"internships: {snapshot['internshipCount']} software internships, from "
        f"{snapshot['internshipGateCount']} postings the internship gate removed"
    )

    # Invariants over the collected data, before either page is written. Ordered
    # cheapest-first only incidentally; each one is independent.
    assert_collections_disjoint(snapshot, what="snapshot")
    assert_internships_reconcile(snapshot, what="snapshot")
    is_demo = load_demo_check(args.repo)
    if is_demo is None:
        print("demo-board check SKIPPED: no backend source at "
              f"{args.repo / 'backend' / 'src'}", file=sys.stderr)
    else:
        assert_no_demo_boards(snapshot, is_demo, what="snapshot")

    if args.snapshot_out is not None:
        args.snapshot_out.write_text(
            json.dumps(snapshot, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        print(f"wrote {args.snapshot_out}")
    return snapshot


def build_local(args: argparse.Namespace, template: str, snapshot: dict[str, Any],
                built_at: str) -> None:
    """The unpublished build: full prose, the authenticated routes, writes allowed."""
    boot = {
        "mode": "local",
        # "" means same origin: the local page is normally opened through a dev proxy
        # that fronts both the static file and the API.
        "apiBase": args.api_base,
        # The local build reads the *authenticated* routes: it has a bearer token in
        # sessionStorage, it gets description prose, and it is the only build that may
        # record that a human applied.
        "apiPrefix": "",
        "apiPublic": False,
        "builtAt": built_at,
        "snapshot": snapshot,
    }
    check_boot(boot, what="local build")
    assert_api_wiring(boot, what="local build", public=False)
    html = render(template, boot)
    assert_self_contained(html, what="local build")
    assert_public_cannot_write(html)
    if args.check_js:
        check_js(html)
    write(args.local_out, html)


def build_public(args: argparse.Namespace, template: str, snapshot: dict[str, Any],
                 built_at: str) -> None:
    """The published build: no prose, the unauthenticated routes, no write possible."""
    # The origin of the unauthenticated read API, which is what makes the published page
    # live. ``--no-public-api`` puts it back to null, and the page then renders from the
    # baked snapshot without calling fetch at all.
    public_base = None if args.no_public_api else args.public_api_base
    boot = {
        "mode": "public",
        "apiBase": public_base,
        "apiPrefix": PUBLIC_API_PREFIX if public_base else "",
        # Two jobs: route every read under /public, and arm the write guard.
        "apiPublic": True,
        "builtAt": built_at,
        "snapshot": strip_prose(snapshot),
    }
    check_boot(boot, what="public build")
    assert_no_prose(boot, what="public build")
    assert_api_wiring(boot, what="public build", public=True)
    html = render(template, boot)
    # Still self-contained: the API is a fetch the page can do without, not an asset it
    # needs in order to render. A blocked network shows the snapshot instead.
    assert_self_contained(html, what="public build")
    assert_public_cannot_write(html)
    if args.check_js:
        check_js(html)
    write(args.public_out or (args.repo / "docs" / "index.html"), html)
    print(
        f"public build reads {public_base}{PUBLIC_API_PREFIX}/worklist"
        if public_base
        else "public build has no live path: snapshot only"
    )


def build(args: argparse.Namespace) -> int:
    template = args.template.read_text(encoding="utf-8")
    assert_no_bare_score(template)
    now = datetime.now(UTC)
    snapshot = collect(args, now=now)
    built_at = now.isoformat()
    if args.only in ("local", "both"):
        build_local(args, template, snapshot, built_at)
    if args.only in ("public", "both"):
        build_public(args, template, snapshot, built_at)
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        return build(parse_args(argv))
    except BuildError as exc:
        print(f"build failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
