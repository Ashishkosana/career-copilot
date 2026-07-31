"""Worklist read API, driven with an in-memory store — no cloud, no network.

Every test below names the failure it prevents. The ones that matter most:

* an offset page 2 silently *skips* a posting when a row closes between requests;
* a missing description serialised as ``""`` reads as "a role with nothing to say"
  and passes every description-based gate;
* ``limit=880`` clamped instead of refused looks like it worked;
* a cursor from a different filter set continues someone else's pagination;
* ``POST /applied`` must record and never submit — asserted structurally, not by
  reading the code and hoping;
* an internship leaking back into the worklist, or into the exact-match tier, which
  is what ``Exclusion.INTERNSHIP`` was added to stop after 5 of them ranked *exact
  match* in a list whose whole job is to be short and correct.
"""
from __future__ import annotations

import ast
import base64
import json
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from copilot.adapters.sqlite_posting_store import SqlitePostingStore
from copilot.config import Settings
from copilot.domain.posting import Posting

# Imported from its home module rather than reached for through the handler's
# re-export: under ``--strict`` that is ``no_implicit_reexport``, and the gate set
# is a domain fact, not part of the API module's surface.
from copilot.domain.screening import Exclusion
from copilot.handlers import worklist_api as api
from copilot.ports.postingstore import PostingStorePort

NOW = datetime(2026, 7, 30, 12, 0, tzinfo=UTC)
SUB = "user-123"

#: A description that names enough technologies for the scorer to have real
#: evidence (>= gap.MIN_EVIDENCE_FOR_EXACT) under a "Requirements" heading.
JD_BACKEND = """
About the role
We build payment infrastructure.

Requirements
- Python and PostgreSQL in production
- AWS (Lambda, DynamoDB)
- Docker

Nice to have
- Kubernetes
- Go
"""

#: A description whose every required technology is on the résumé, so a posting
#: carrying it scores *exact match* once its level is confirmed. Used to prove that
#: an internship good enough to top the list still does not enter the list.
JD_FULLY_COVERED = """
About the role
Requirements
- Python
- PostgreSQL
- AWS
"""

RESUME = "Python, PostgreSQL, AWS, Lambda, DynamoDB, Flutter, pytest"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakePostingStore:
    """In-memory PostingStorePort. ``raises`` reproduces a store outage.

    ``calls`` is the audit trail the no-submit test reads: the applied path must
    touch exactly one store method and nothing else.
    """

    def __init__(self, postings: Iterable[Posting] = (), *, raises: bool = False) -> None:
        self.postings = list(postings)
        self.raises = raises
        self.applied: dict[str, datetime] = {}
        self.calls: list[str] = []
        self.reads = 0

    def _boom(self, what: str) -> None:
        self.calls.append(what)
        if self.raises:
            raise RuntimeError("sqlite file is gone")

    def open_postings(self) -> list[Posting]:
        self._boom("open_postings")
        self.reads += 1
        return list(self.postings)

    def mark_applied(self, posting_id: str, *, now: datetime) -> None:
        self._boom("mark_applied")
        # Mirrors the SQL guard: `applied_at IS NULL`, so a repeat is a no-op.
        self.applied.setdefault(posting_id, now)

    # --- rest of the port, unused by the read API but part of the contract ---
    def sync(self, postings: list[Posting], *, now: datetime) -> tuple[list[str], list[str]]:
        self.postings.extend(postings)
        return [p.id for p in postings], []

    def close_missing(self, *, now: datetime, seen_ids: set[str]) -> int:
        return 0

    def new_since(self, since: datetime) -> list[Posting]:
        return []

    def cached_interpretation(self, posting_id: str) -> dict[str, Any] | None:
        return None

    def save_interpretation(self, posting_id: str, payload: dict[str, Any]) -> None:
        return None


def test_fake_satisfies_the_port() -> None:
    """If the port grows a method, this fake must grow with it."""
    store: PostingStorePort = FakePostingStore()
    assert store.open_postings() == []


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------

def make(
    title: str = "Software Engineer",
    *,
    company: str = "Acme",
    ats: str = "greenhouse",
    desc: str = JD_BACKEND,
    has_desc: bool | None = None,
    day: int = 1,
    posted_at: datetime | None | str = "auto",
    url: str | None = None,
    employment_type: str = "",
) -> Posting:
    when: datetime | None
    if posted_at == "auto":
        when = datetime(2026, 7, day, 9, 0, tzinfo=UTC)
    else:
        assert not isinstance(posted_at, str)
        when = posted_at
    return Posting(
        title=title,
        company=company,
        url=url or f"https://boards.example/{company}/{title}/{day}".replace(" ", "-"),
        ats=ats,
        location="Remote",
        description=desc,
        desc_available=bool(desc) if has_desc is None else has_desc,
        posted_at=when,
        employment_type=employment_type,
    )


def event(
    *,
    sub: str | None = SUB,
    query: dict[str, str] | None = None,
    path_params: dict[str, str] | None = None,
    body: str | None = None,
    method: str = "GET",
    path: str = "/worklist",
    rest_shape: bool = False,
) -> dict[str, Any]:
    claims = {"sub": sub}
    authorizer: dict[str, Any] = (
        {"claims": claims} if rest_shape else {"jwt": {"claims": claims}}
    )
    if sub is None:
        authorizer = {}
    built: dict[str, Any] = {
        "routeKey": f"{method} {path}",
        "rawPath": path,
        "requestContext": {"authorizer": authorizer, "http": {"method": method}},
        "queryStringParameters": query,
        "pathParameters": path_params,
    }
    if body is not None:
        built["body"] = body
    return built


def body_of(response: dict[str, Any]) -> dict[str, Any]:
    parsed: dict[str, Any] = json.loads(response["body"])
    return parsed


def worklist(store: FakePostingStore, **kwargs: Any) -> dict[str, Any]:
    resp = api.list_worklist(store, event(**kwargs), resume_text=RESUME, now=NOW)
    assert resp["statusCode"] == 200, resp["body"]
    return body_of(resp)


def ids_of(page: dict[str, Any]) -> list[str]:
    return [item["id"] for item in page["items"]]


# ---------------------------------------------------------------------------
# GET /worklist — shape and ordering
# ---------------------------------------------------------------------------

class TestWorklistShape:
    def test_returns_a_page_not_the_whole_corpus(self) -> None:
        """880 rows in one response is the thing this endpoint exists to avoid."""
        store = FakePostingStore(make(day=d, url=f"https://x/{d}") for d in range(1, 29))
        page = worklist(store)
        assert page["page"]["count"] == api.DEFAULT_LIMIT
        assert page["matched"] == 28
        assert page["eligibleTotal"] == 28
        assert page["page"]["hasMore"] is True
        assert page["page"]["nextCursor"]

    def test_newest_first_with_undated_last(self) -> None:
        recent = make(title="Junior Software Engineer", day=20, url="https://x/recent")
        older = make(title="Software Engineer I", day=2, url="https://x/older")
        undated = make(
            title="Graduate Software Engineer", posted_at=None, url="https://x/undated"
        )
        page = worklist(FakePostingStore([older, undated, recent]))
        assert ids_of(page) == [recent.id, older.id, undated.id]

    def test_naive_posted_at_does_not_crash_the_sort(self) -> None:
        """Nothing in Posting forces a tz; comparing naive with aware raises TypeError."""
        naive = make(title="Junior Software Engineer", posted_at=datetime(2026, 7, 15, 9, 0))
        aware = make(title="Software Engineer I", day=2, url="https://x/aware")
        page = worklist(FakePostingStore([aware, naive]))
        assert ids_of(page) == [naive.id, aware.id]

    def test_score_is_never_a_bare_number(self) -> None:
        page = worklist(FakePostingStore([make(title="Junior Software Engineer")]))
        score = page["items"][0]["score"]
        assert score["total"] == 65
        assert score["tier"] == "strong"
        assert score["required"] == {
            "covered": 5,
            "total": 6,
            "have": ["AWS", "DynamoDB", "Lambda", "PostgreSQL", "Python"],
            "missing": ["Docker"],
        }
        assert score["preferred"] == {
            "covered": 0, "total": 1, "have": [], "missing": ["Kubernetes"]
        }
        assert score["levelConfirmed"] is True
        assert score["resumeVariant"] == "backend"
        assert score["unscoredReason"] is None
        # The denominator is the posting's, which is the only reason the number is
        # defensible — so it travels with the number.
        assert "covers 5/6 required" in score["explain"]

    def test_level_unconfirmed_when_nothing_states_a_band(self) -> None:
        """652 of 880 eligible postings carry no level marker; they must not read as verified."""
        page = worklist(FakePostingStore([make(title="Software Engineer", desc="Python.")]))
        item = page["items"][0]
        assert item["level"] == "unknown"
        assert item["levelSource"] == "none"
        assert item["score"]["levelConfirmed"] is False

    def test_funnel_flags_that_gate_counts_overcount(self) -> None:
        """A UI rendering gate counts as a subtraction chain produces nonsense."""
        both = make(
            title="Senior Software Engineer",
            desc="Requires an active TS/SCI clearance. Must be a US citizen.",
        )
        page = worklist(FakePostingStore([both, make(title="Junior Software Engineer")]))
        funnel = page["funnel"]
        assert funnel["screened"] == 2
        assert funnel["kept"] == 1
        assert funnel["excluded"] == 1
        assert funnel["gateCountTotal"] == 3
        assert funnel["gateCountsOvercount"] is True
        assert set(funnel["gates"]) == {gate.value for gate in Exclusion}

    def test_list_rows_carry_no_description_prose(self) -> None:
        """Bulk republication is a different act from quoting evidence."""
        page = worklist(FakePostingStore([make()]))
        assert "description" not in page["items"][0]


class TestScoringDegradation:
    def test_no_resume_reports_unavailable_instead_of_scoring_zero(self) -> None:
        """Scoring against an empty résumé marks every requirement missing — a lie."""
        resp = api.list_worklist(FakePostingStore([make()]), event(), resume_text="", now=NOW)
        page = body_of(resp)
        assert page["scoring"] == {"available": False, "reason": "no_resume_configured"}
        assert page["items"][0]["score"] is None

    def test_tier_filter_is_refused_when_scoring_is_unavailable(self) -> None:
        resp = api.list_worklist(
            FakePostingStore([make()]), event(query={"tier": "strong"}), resume_text="", now=NOW
        )
        assert resp["statusCode"] == 400
        assert body_of(resp) == {"error": "scoring_unavailable"}


# ---------------------------------------------------------------------------
# The description distinction
# ---------------------------------------------------------------------------

class TestDescriptionStatus:
    def test_a_source_with_no_description_says_so(self) -> None:
        """Workday's list endpoint returns none; "" would pass every gate silently."""
        posting = make(title="Software Engineer", desc="", has_desc=False, ats="workday")
        store = FakePostingStore([posting])
        detail = body_of(
            api.get_posting(store, event(path_params={"id": posting.id}), now=NOW)
        )["posting"]
        assert detail["descAvailable"] is False
        assert detail["descriptionStatus"] == api.DESC_NOT_PROVIDED
        assert detail["description"] is None
        assert detail["descriptionChars"] is None
        assert detail["screening"]["eligibility"]["checked"] is False
        assert "could not run" in detail["screening"]["eligibility"]["note"]

    def test_an_empty_description_from_a_claiming_source_is_its_own_state(self) -> None:
        posting = make(title="Software Engineer", desc="", has_desc=True)
        store = FakePostingStore([posting])
        detail = body_of(
            api.get_posting(store, event(path_params={"id": posting.id}), now=NOW)
        )["posting"]
        assert detail["descAvailable"] is True
        assert detail["descriptionStatus"] == api.DESC_EMPTY
        assert detail["description"] is None  # never ""

    def test_a_real_description_is_returned_in_full(self) -> None:
        posting = make(title="Junior Software Engineer")
        store = FakePostingStore([posting])
        detail = body_of(
            api.get_posting(
                store, event(path_params={"id": posting.id}), resume_text=RESUME, now=NOW
            )
        )["posting"]
        assert detail["description"] == JD_BACKEND
        assert detail["descriptionChars"] == len(JD_BACKEND)
        assert detail["score"]["required"]["missing"] == ["Docker"]

    def test_unscored_reason_distinguishes_no_description_from_no_technologies(self) -> None:
        missing = make(title="Junior Software Engineer", desc="", has_desc=False, url="https://x/1")
        silent = make(title="Junior Software Engineer", desc="Join our team.", url="https://x/2")
        page = worklist(FakePostingStore([missing, silent]))
        reasons = {item["id"]: item["score"]["unscoredReason"] for item in page["items"]}
        assert reasons[missing.id] == "description_not_provided"
        assert reasons[silent.id] == "no_technologies_named"


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------

class TestFilters:
    def test_level_and_ats_filters_narrow_the_list(self) -> None:
        """Filters over the two bands that actually reach a worklist.

        This used to filter on ``intern`` versus ``entry``, which stopped working
        when internships became a gate rather than a band: no intern-band posting
        survives the funnel any more, so the fixture could only ever return nothing.
        ``unknown`` is the right counterpart — it is 652 of the 880 real postings and
        the largest slice the filter has to handle.
        """
        unknown = make(title="Software Engineer", ats="lever", url="https://x/u")
        entry = make(title="Junior Software Engineer", ats="greenhouse", url="https://x/e")
        store = FakePostingStore([unknown, entry])
        assert ids_of(worklist(store, query={"level": "unknown"})) == [unknown.id]
        assert ids_of(worklist(store, query={"ats": "GREENHOUSE"})) == [entry.id]
        assert len(ids_of(worklist(store, query={"level": "unknown,entry"}))) == 2

    def test_repeated_parameters_are_not_lost(self) -> None:
        """REST APIs put repeats in multiValueQueryStringParameters and nowhere else."""
        unknown = make(title="Software Engineer", url="https://x/u")
        entry = make(title="Junior Software Engineer", url="https://x/e")
        raw = event()
        raw["queryStringParameters"] = {"level": "entry"}
        raw["multiValueQueryStringParameters"] = {"level": ["unknown", "entry"]}
        resp = api.list_worklist(
            FakePostingStore([unknown, entry]), raw, resume_text=RESUME, now=NOW
        )
        assert body_of(resp)["matched"] == 2

    def test_a_filter_that_matches_nothing_is_an_empty_page_not_an_error(self) -> None:
        """Senior roles never survive the funnel, so this filter is legitimately empty."""
        page = worklist(FakePostingStore([make(title="Junior Software Engineer")]), query={
            "level": "senior"
        })
        assert page["items"] == []
        assert page["matched"] == 0
        assert page["eligibleTotal"] == 1  # the funnel is still reported honestly
        assert page["page"]["hasMore"] is False
        assert page["page"]["nextCursor"] is None

    def test_unknown_ats_is_empty_rather_than_rejected(self) -> None:
        """The supported-board set grows with the adapters; a 400 would age badly."""
        page = worklist(FakePostingStore([make()]), query={"ats": "smartrecruiters"})
        assert page["matched"] == 0

    def test_tier_filter_accepts_the_spaceless_alias(self) -> None:
        """Tier.EXACT is spelled "exact match" — a query string should not need the space."""
        store = FakePostingStore([make(title="Junior Software Engineer")])
        page = worklist(store, query={"tier": "strong,exact_match"})
        assert page["filters"]["tier"] == ["exact match", "strong"]
        assert page["matched"] == 1
        assert worklist(store, query={"tier": "weak"})["matched"] == 0

    @pytest.mark.parametrize(
        ("param", "value", "code"),
        [
            ("tier", "amazing", "invalid_tier"),
            ("level", "wizard", "invalid_level"),
            ("postedAfter", "last tuesday", "invalid_date"),
            ("includeUndated", "maybe", "invalid_boolean"),
            ("limit", "many", "invalid_limit"),
        ],
    )
    def test_a_bad_parameter_is_a_400_not_a_default(
        self, param: str, value: str, code: str
    ) -> None:
        resp = api.list_worklist(
            FakePostingStore([make()]), event(query={param: value}), resume_text=RESUME, now=NOW
        )
        assert resp["statusCode"] == 400
        assert body_of(resp) == {"error": code}

    @pytest.mark.parametrize("value", ["0", "-3", str(api.MAX_LIMIT + 1), "10000"])
    def test_an_out_of_range_limit_is_refused_not_clamped(self, value: str) -> None:
        """A clamped limit=880 looks like it worked, which is how a 25-row cap gets missed."""
        store = FakePostingStore(make(day=d, url=f"https://x/{d}") for d in range(1, 5))
        resp = api.list_worklist(
            store, event(query={"limit": value}), resume_text=RESUME, now=NOW
        )
        assert resp["statusCode"] == 400
        assert body_of(resp) == {"error": "invalid_limit"}


class TestAgeWindow:
    """Old postings must stay findable — a role open since February is still open."""

    def test_there_is_no_default_age_cutoff(self) -> None:
        ancient = make(
            title="Junior Software Engineer", posted_at=datetime(2024, 2, 1, tzinfo=UTC),
            url="https://x/old",
        )
        page = worklist(FakePostingStore([ancient]))
        assert ids_of(page) == [ancient.id]
        assert page["filters"]["postedAfter"] is None

    def test_the_window_selects_months_old_postings(self) -> None:
        ancient = make(
            title="Junior Software Engineer", posted_at=datetime(2024, 2, 1, tzinfo=UTC),
            url="https://x/old",
        )
        fresh = make(title="Junior Software Engineer", day=29, url="https://x/new")
        store = FakePostingStore([ancient, fresh])
        old_only = worklist(store, query={"postedBefore": "2025-01-01"})
        assert ids_of(old_only) == [ancient.id]
        new_only = worklist(store, query={"postedAfter": "2026-07-01T00:00:00Z"})
        assert ids_of(new_only) == [fresh.id]

    def test_undated_postings_are_in_by_default_and_out_of_an_explicit_window(self) -> None:
        """An undated posting cannot honestly be claimed to fall inside a date range."""
        undated = make(title="Junior Software Engineer", posted_at=None, url="https://x/u")
        store = FakePostingStore([undated])
        assert ids_of(worklist(store)) == [undated.id]
        assert worklist(store, query={"postedAfter": "2020-01-01"})["matched"] == 0
        asked = worklist(store, query={"postedAfter": "2020-01-01", "includeUndated": "true"})
        assert ids_of(asked) == [undated.id]


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------

def paginate(store: FakePostingStore, *, limit: int) -> list[str]:
    seen: list[str] = []
    cursor: str | None = None
    for _ in range(50):  # guards against a cursor that never advances
        query = {"limit": str(limit)}
        if cursor:
            query["cursor"] = cursor
        page = worklist(store, query=query)
        seen.extend(ids_of(page))
        cursor = page["page"]["nextCursor"]
        if cursor is None:
            return seen
    raise AssertionError("pagination did not terminate")


class TestPagination:
    def test_every_row_is_returned_exactly_once(self) -> None:
        store = FakePostingStore(make(day=d, url=f"https://x/{d}") for d in range(1, 24))
        seen = paginate(store, limit=7)
        assert len(seen) == 23
        assert len(set(seen)) == 23

    def test_rows_sharing_a_timestamp_still_page_cleanly(self) -> None:
        """Without the id tiebreaker a cursor cannot separate same-second postings."""
        same = datetime(2026, 7, 20, 9, 0, tzinfo=UTC)
        store = FakePostingStore(
            make(posted_at=same, url=f"https://x/{n}") for n in range(9)
        )
        seen = paginate(store, limit=3)
        assert len(set(seen)) == 9

    def test_a_posting_closing_mid_scroll_does_not_skip_the_next_row(self) -> None:
        """The offset bug: page 2 taken at offset 3 of a now-shorter list drops a row."""
        postings = [make(day=d, url=f"https://x/{d}") for d in range(1, 8)]
        store = FakePostingStore(postings)
        first = worklist(store, query={"limit": "3"})
        # Newest first, so page 1 is days 7,6,5. Day 7 then closes.
        store.postings = [p for p in postings if p.url != "https://x/7"]
        second = worklist(store, query={"limit": "3", "cursor": first["page"]["nextCursor"]})
        # Anchored to day 5, so day 4 is next. An offset of 3 into the now-shorter
        # list [6,5,4,3,2,1] would have started at day 3 and lost day 4 entirely.
        wanted = {"https://x/4", "https://x/3", "https://x/2"}
        assert ids_of(second) == [p.id for p in reversed(postings) if p.url in wanted]

    def test_a_cursor_past_the_end_is_an_empty_page_not_an_error(self) -> None:
        store = FakePostingStore([make(day=5, url="https://x/5")])
        page = worklist(store, query={"limit": "1"})
        assert page["page"]["nextCursor"] is None  # single row, nothing after it
        # A well-formed cursor positioned after every row: the epoch sorts last.
        beyond = worklist(store, query={"limit": "1", "cursor": _cursor_after_everything()})
        assert beyond["items"] == []
        assert beyond["page"]["hasMore"] is False
        assert beyond["page"]["nextCursor"] is None

    @pytest.mark.parametrize(
        "raw",
        [
            "not-base64-!!",
            base64.urlsafe_b64encode(b"not json").decode(),
            base64.urlsafe_b64encode(b'{"v":99,"id":"a","f":"x"}').decode(),
            base64.urlsafe_b64encode(b'{"v":1,"id":"","f":"x"}').decode(),
            base64.urlsafe_b64encode(b'{"v":1,"id":"a","ts":7,"f":"x"}').decode(),
            base64.urlsafe_b64encode(b'{"v":1,"id":"a","ts":"soon","f":"x"}').decode(),
        ],
    )
    def test_a_malformed_cursor_is_a_400(self, raw: str) -> None:
        resp = api.list_worklist(
            FakePostingStore([make()]),
            event(query={"cursor": raw}),
            resume_text=RESUME,
            now=NOW,
        )
        assert resp["statusCode"] == 400
        # The fingerprint check only runs once the payload itself is well formed.
        assert body_of(resp)["error"] in {"invalid_cursor", "cursor_filter_mismatch"}

    def test_a_cursor_from_a_different_filter_set_is_refused(self) -> None:
        """Continuing a foreign cursor returns a page that repeats or skips rows."""
        store = FakePostingStore(
            make(title="Junior Software Engineer", day=d, url=f"https://x/{d}") for d in range(1, 6)
        )
        page = worklist(store, query={"limit": "2"})
        resp = api.list_worklist(
            store,
            event(query={"limit": "2", "cursor": page["page"]["nextCursor"],
                         "level": "entry"}),
            resume_text=RESUME,
            now=NOW,
        )
        assert resp["statusCode"] == 400
        assert body_of(resp) == {"error": "cursor_filter_mismatch"}


def _fingerprint() -> str:
    return api.WorklistFilters().fingerprint


def _cursor_after_everything() -> str:
    """A well-formed cursor whose key sorts after every real row (the epoch is last)."""
    payload = json.dumps(
        {"v": api.CURSOR_VERSION, "ts": None, "id": "z" * 32, "f": _fingerprint()},
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


# ---------------------------------------------------------------------------
# GET /worklist/{id}
# ---------------------------------------------------------------------------

class TestGetPosting:
    def test_unknown_id_is_404(self) -> None:
        resp = api.get_posting(
            FakePostingStore([make()]), event(path_params={"id": "deadbeefdeadbeef"}), now=NOW
        )
        assert resp["statusCode"] == 404
        assert body_of(resp) == {"error": "posting_not_found"}

    def test_a_missing_path_parameter_is_400(self) -> None:
        resp = api.get_posting(FakePostingStore([make()]), event(path="/worklist/"), now=NOW)
        assert resp["statusCode"] == 400
        assert body_of(resp) == {"error": "missing_posting_id"}

    def test_an_unsubstituted_route_template_is_400_not_404(self) -> None:
        """`/worklist/{id}` with no pathParameters is a wiring fault, not a missing row."""
        resp = api.get_posting(FakePostingStore([make()]), event(path="/worklist/{id}"), now=NOW)
        assert resp["statusCode"] == 400

    def test_id_falls_back_to_the_raw_path(self) -> None:
        posting = make()
        resp = api.get_posting(
            FakePostingStore([posting]), event(path=f"/worklist/{posting.id}"), now=NOW
        )
        assert resp["statusCode"] == 200

    def test_an_excluded_posting_is_still_readable(self) -> None:
        """/excluded is only a trust surface if you can click through to the evidence."""
        posting = make(
            title="Software Engineer",
            desc="Requirements\n- Must be a US citizen\n- Python",
        )
        resp = api.get_posting(
            FakePostingStore([posting]), event(path_params={"id": posting.id}), now=NOW
        )
        assert resp["statusCode"] == 200
        screening = body_of(resp)["posting"]["screening"]
        assert screening["kept"] is False
        assert screening["eligibility"]["citizenshipRequired"] is True
        [exclusion] = screening["exclusions"]
        assert exclusion["gate"] == "citizenship_or_itar_restricted"
        assert "citizen" in exclusion["quote"]


# ---------------------------------------------------------------------------
# GET /excluded
# ---------------------------------------------------------------------------

def excluded(store: FakePostingStore, **kwargs: Any) -> dict[str, Any]:
    resp = api.list_excluded(store, event(path="/excluded", **kwargs), now=NOW)
    assert resp["statusCode"] == 200, resp["body"]
    return body_of(resp)


def group_for(page: dict[str, Any], gate: str) -> dict[str, Any]:
    found: dict[str, Any] = next(g for g in page["groups"] if g["gate"] == gate)
    return found


class TestExcluded:
    def test_every_gate_is_present_even_when_empty(self) -> None:
        """A stable set of groups keeps the UI's chart from reordering day to day."""
        page = excluded(FakePostingStore([make(title="Product Manager")]))
        assert [g["gate"] for g in page["groups"]] == [gate.value for gate in Exclusion]

    def test_each_exclusion_carries_the_sentence_that_caused_it(self) -> None:
        store = FakePostingStore(
            [
                make(
                    title="Software Engineer",
                    desc="Requirements\nActive TS/SCI clearance required.\n",
                    url="https://x/c",
                ),
                make(title="Senior Software Engineer", url="https://x/s"),
                make(title="Account Executive", url="https://x/a"),
            ]
        )
        page = excluded(store)
        clearance = group_for(page, "security_clearance_required")["items"][0]
        assert "TS/SCI clearance" in clearance["quote"]
        assert "clearance required" in clearance["reason"]
        level = group_for(page, "wrong_seniority_band")["items"][0]
        assert level["quote"] == "Senior Software Engineer"
        role = group_for(page, "not_a_software_role")["items"][0]
        assert role["quote"] == "Account Executive"

    def test_a_posting_failing_two_gates_appears_in_both_groups(self) -> None:
        """Which is exactly why the per-gate counts overcount, and why we say so."""
        posting = make(
            title="Senior Software Engineer",
            desc="Requirements\nUS citizenship is required.\n",
        )
        page = excluded(FakePostingStore([posting]))
        assert group_for(page, "wrong_seniority_band")["count"] == 1
        assert group_for(page, "citizenship_or_itar_restricted")["count"] == 1
        assert page["excludedTotal"] == 1  # one posting, two gate hits
        assert page["gateCountTotal"] == 2
        assert page["gateCountsOvercount"] is True

    def test_quotes_are_excerpts_not_the_description(self) -> None:
        """Short excerpts explain a decision; full prose would be bulk republication."""
        filler = "We are a wonderful company with many opportunities. " * 12
        description = f"{filler}\nActive security clearance required.\n{filler}"
        page = excluded(
            FakePostingStore([make(title="Software Engineer", desc=description)])
        )
        quote = group_for(page, "security_clearance_required")["items"][0]["quote"]
        assert "clearance required" in quote
        assert len(quote) <= api.QUOTE_MAX_CHARS
        assert len(quote) < len(description) / 4

    def test_an_absurdly_long_title_is_still_capped(self) -> None:
        """Real Workday titles run to hundreds of characters; the quote is an excerpt."""
        page = excluded(FakePostingStore([make(title="Program Manager " * 40)]))
        quote = group_for(page, "not_a_software_role")["items"][0]["quote"]
        assert len(quote) == api.QUOTE_MAX_CHARS
        assert quote.endswith("…")

    def test_groups_are_paged(self) -> None:
        store = FakePostingStore(
            make(title="Program Manager", day=d, url=f"https://x/{d}") for d in range(1, 15)
        )
        page = excluded(store, query={"limit": "5"})
        group = group_for(page, "not_a_software_role")
        assert group["count"] == 14
        assert group["page"]["count"] == 5
        assert group["page"]["hasMore"] is True

        second = excluded(
            store,
            query={
                "gate": "not_a_software_role",
                "limit": "5",
                "cursor": group["page"]["nextCursor"],
            },
        )
        assert len(second["groups"]) == 1
        first_ids = {item["id"] for item in group["items"]}
        assert not first_ids & {item["id"] for item in second["groups"][0]["items"]}

    def test_a_cursor_without_a_gate_is_refused(self) -> None:
        """A boundary in "all gates" is ambiguous — a posting sits in several groups."""
        store = FakePostingStore([make(title="Program Manager")])
        page = excluded(store, query={"limit": "1"})
        cursor = group_for(page, "not_a_software_role")["page"]["nextCursor"] or "x"
        resp = api.list_excluded(
            store, event(path="/excluded", query={"cursor": cursor}), now=NOW
        )
        assert resp["statusCode"] == 400
        assert body_of(resp) == {"error": "cursor_requires_gate"}

    @pytest.mark.parametrize("value", ["nonsense", "not_a_software_role,wrong_seniority_band"])
    def test_a_bad_gate_is_400(self, value: str) -> None:
        resp = api.list_excluded(
            FakePostingStore([make()]), event(path="/excluded", query={"gate": value}), now=NOW
        )
        assert resp["statusCode"] == 400
        assert body_of(resp) == {"error": "invalid_gate"}


# ---------------------------------------------------------------------------
# GET /internships
# ---------------------------------------------------------------------------

def internships(store: FakePostingStore, **kwargs: Any) -> dict[str, Any]:
    resp = api.list_internships(
        store, event(path="/internships", **kwargs), resume_text=RESUME, now=NOW
    )
    assert resp["statusCode"] == 200, resp["body"]
    return body_of(resp)


def a_full_time_role() -> Posting:
    return make(title="Junior Software Engineer", url="https://x/ft")


def an_internship(**kwargs: Any) -> Posting:
    return make(
        title="Software Engineer Intern",
        desc=JD_FULLY_COVERED,
        url="https://x/intern",
        **kwargs,
    )


class TestInternshipsCollection:
    """The internships section: addressable, scored, and *not* in the worklist.

    Real numbers these fixtures stand in for: 813 full-time roles kept, 20 of them
    exact match, 318 postings caught by the internship gate, and **48** in this
    section — the other 270 fail a gate that has nothing to do with being an
    internship (264 are not software roles, 12 are vendor demo boards). The
    fixtures are small on purpose, but every assertion below is one that fails if
    those numbers start leaking into each other.
    """

    def test_an_internship_stays_out_of_the_worklist_and_the_exact_tier(self) -> None:
        """The regression that matters: 5 internships once ranked *exact match*.

        The internship here is engineered to be the single best-scoring row in the
        store, so a leak cannot hide behind a low score — if it reaches the worklist
        it reaches the top of it.
        """
        full_time, intern = a_full_time_role(), an_internship()
        store = FakePostingStore([full_time, intern])

        worklist_page = worklist(store)
        assert ids_of(worklist_page) == [full_time.id]
        assert worklist_page["matched"] == 1
        assert worklist_page["eligibleTotal"] == 1

        exact = worklist(store, query={"tier": "exact_match"})
        assert exact["matched"] == 0

        section = internships(store)
        assert ids_of(section) == [intern.id]
        assert section["items"][0]["score"]["tier"] == "exact match"

    def test_both_collections_report_both_totals(self) -> None:
        """One read is enough to render "1 full-time · 1 internship" as a heading."""
        store = FakePostingStore([a_full_time_role(), an_internship()])
        for page in (worklist(store), internships(store)):
            assert page["eligibleTotal"] == 1
            assert page["internshipTotal"] == 1
        assert worklist(store)["collection"] == "worklist"
        assert internships(store)["collection"] == "internships"

    def test_the_internship_is_still_browsable_as_an_exclusion(self) -> None:
        """Nothing disappears silently — the funnel still accounts for it as dropped."""
        intern = an_internship()
        store = FakePostingStore([a_full_time_role(), intern])
        page = excluded(store)
        group = group_for(page, "internship_not_full_time")
        assert [item["id"] for item in group["items"]] == [intern.id]
        # Quoted, like every other gate: this was the one exclusion with no evidence.
        assert group["items"][0]["quote"] == "Software Engineer Intern"
        assert "'Intern' in the title" in group["items"][0]["reason"]
        assert worklist(store)["funnel"]["gates"]["internship_not_full_time"] == 1

    def test_an_employment_type_internship_quotes_the_type_not_the_title(self) -> None:
        """The title is clean, so quoting it would show evidence that is not there."""
        typed = make(
            title="Software Engineer, Product",
            employment_type="Internship",
            url="https://x/typed",
        )
        group = group_for(excluded(FakePostingStore([typed])), "internship_not_full_time")
        assert group["items"][0]["quote"] == "Internship"

    def test_an_employment_type_only_internship_is_in_the_section(self) -> None:
        """22 real postings declare type Internship under a clean SWE title."""
        typed = make(
            title="Software Engineer, Product",
            desc=JD_FULLY_COVERED,
            employment_type="Internship",
            url="https://x/typed",
        )
        store = FakePostingStore([typed])
        assert worklist(store)["matched"] == 0
        assert ids_of(internships(store)) == [typed.id]

    def test_a_vendor_demo_internship_is_in_neither_collection(self) -> None:
        """The gate that removed 317 demo fixtures applies to every collection.

        Publishing a vendor's invented roles under a new heading would be the
        original failure — "invented companies served as real matches" — wearing a
        different hat.
        """
        store = FakePostingStore([an_internship(company="leverdemo")])
        assert worklist(store)["matched"] == 0
        assert internships(store)["matched"] == 0
        assert internships(store)["internshipTotal"] == 0

    @pytest.mark.parametrize(
        ("title", "desc", "gate"),
        [
            ("Marketing Intern", JD_FULLY_COVERED, "not_a_software_role"),
            (
                "Software Engineer Intern",
                "Requirements\nActive TS/SCI clearance required.\nPython\n",
                "security_clearance_required",
            ),
            (
                "Software Engineer Intern",
                "Requirements\nMust be a US citizen.\nPython\n",
                "citizenship_or_itar_restricted",
            ),
        ],
    )
    def test_every_other_gate_still_applies(self, title: str, desc: str, gate: str) -> None:
        """The section re-screens with one flag flipped, not with the funnel skipped."""
        posting = make(title=title, desc=desc, url="https://x/gated")
        store = FakePostingStore([posting])
        section = internships(store)
        assert section["matched"] == 0
        assert section["internshipTotal"] == 0
        # And the two numbers reconcile in the same payload rather than looking like
        # an off-by-270: the gate fired, the section is still empty, and both say so.
        assert section["funnel"]["gates"]["internship_not_full_time"] == 1
        assert group_for(excluded(store), gate)["count"] == 1

    def test_cards_are_the_same_shape_so_the_page_reuses_one_component(self) -> None:
        store = FakePostingStore([a_full_time_role(), an_internship()])
        assert set(internships(store)["items"][0]) == set(worklist(store)["items"][0])

    def test_the_section_pages_with_its_own_cursor(self) -> None:
        store = FakePostingStore(
            make(
                title="Software Engineer Intern", desc=JD_FULLY_COVERED,
                day=d, url=f"https://x/i{d}",
            )
            for d in range(1, 8)
        )
        first = internships(store, query={"limit": "3"})
        assert first["matched"] == 7
        second = internships(
            store, query={"limit": "3", "cursor": first["page"]["nextCursor"]}
        )
        assert not set(ids_of(first)) & set(ids_of(second))

    def test_a_worklist_cursor_is_refused_by_the_internships_section(self) -> None:
        """Same filters, different population: continuing would skip or repeat rows."""
        store = FakePostingStore(
            [make(title="Junior Software Engineer", day=d, url=f"https://x/{d}") for d in
             range(1, 6)]
            + [an_internship()]
        )
        page = worklist(store, query={"limit": "2"})
        resp = api.list_internships(
            store,
            event(path="/internships", query={"limit": "2",
                                             "cursor": page["page"]["nextCursor"]}),
            resume_text=RESUME,
            now=NOW,
        )
        assert resp["statusCode"] == 400
        assert body_of(resp) == {"error": "cursor_filter_mismatch"}

    def test_filters_narrow_the_section_like_the_worklist(self) -> None:
        store = FakePostingStore([an_internship(), a_full_time_role()])
        assert internships(store, query={"level": "intern"})["matched"] == 1
        assert internships(store, query={"level": "entry"})["matched"] == 0
        assert internships(store, query={"ats": "workday"})["matched"] == 0

    def test_the_detail_read_still_names_the_internship_gate(self) -> None:
        """The honest answer to "why is this not in my worklist" is the gate itself."""
        intern = an_internship()
        resp = api.get_posting(
            FakePostingStore([intern]), event(path_params={"id": intern.id}), now=NOW
        )
        screening = body_of(resp)["posting"]["screening"]
        assert screening["kept"] is False
        assert [e["gate"] for e in screening["exclusions"]] == ["internship_not_full_time"]

    def test_screening_the_corpus_twice_is_not_how_this_works(self) -> None:
        """The second pass covers the internship-gated rows, not all 25,294 again."""
        store = FakePostingStore([a_full_time_role(), an_internship()])
        cache = api.IndexCache(ttl_seconds=300)
        api.list_worklist(store, event(), resume_text=RESUME, now=NOW, cache=cache)
        api.list_internships(
            store, event(path="/internships"), resume_text=RESUME, now=NOW, cache=cache
        )
        assert store.reads == 1


# ---------------------------------------------------------------------------
# POST /applied
# ---------------------------------------------------------------------------

def applied_event(posting_id: str | None, **kwargs: Any) -> dict[str, Any]:
    payload = json.dumps({"postingId": posting_id} if posting_id else {})
    return event(method="POST", path="/applied", body=payload, **kwargs)


class TestApplied:
    def test_records_once_and_is_idempotent(self) -> None:
        posting = make()
        store = FakePostingStore([posting])
        first = api.record_applied(store, applied_event(posting.id), now=NOW)
        later = api.record_applied(
            store, applied_event(posting.id), now=NOW + timedelta(hours=3)
        )
        assert first["statusCode"] == later["statusCode"] == 200
        # Byte-identical: no timestamp is echoed that the port cannot read back.
        assert first["body"] == later["body"]
        assert store.applied == {posting.id: NOW}
        assert body_of(first)["recorded"] is True
        assert body_of(first)["submitted"] is False

    def test_unknown_id_is_404_and_writes_nothing(self) -> None:
        store = FakePostingStore([make()])
        resp = api.record_applied(store, applied_event("0000000000000000"), now=NOW)
        assert resp["statusCode"] == 404
        assert body_of(resp) == {"error": "posting_not_found"}
        assert store.applied == {}

    @pytest.mark.parametrize(
        ("body", "code"),
        [
            (None, "missing_body"),
            ("", "missing_body"),
            ("{not json}", "invalid_json"),
            ("[1,2]", "invalid_json"),
            ("{}", "missing_posting_id"),
            ('{"postingId": ""}', "missing_posting_id"),
            ('{"postingId": 7}', "missing_posting_id"),
        ],
    )
    def test_a_bad_body_is_a_400(self, body: str | None, code: str) -> None:
        raw = event(method="POST", path="/applied")
        if body is not None:
            raw["body"] = body
        resp = api.record_applied(FakePostingStore([make()]), raw, now=NOW)
        assert resp["statusCode"] == 400
        assert body_of(resp) == {"error": code}

    def test_a_base64_body_is_decoded(self) -> None:
        posting = make()
        raw = applied_event(posting.id)
        raw["body"] = base64.b64encode(raw["body"].encode()).decode()
        raw["isBase64Encoded"] = True
        resp = api.record_applied(FakePostingStore([posting]), raw, now=NOW)
        assert resp["statusCode"] == 200

    def test_recording_touches_only_the_store(self) -> None:
        """The product promise: this endpoint records, it never submits."""
        posting = make()
        store = FakePostingStore([posting])
        api.record_applied(store, applied_event(posting.id), now=NOW)
        assert store.calls == ["open_postings", "mark_applied"]


def test_the_module_cannot_submit_an_application() -> None:
    """No network client is importable from here, so no future edit can quietly add one.

    Asserted structurally rather than by review: "nothing may auto-apply" is a
    product invariant, and a comment does not enforce it.
    """
    source = Path(api.__file__).read_text()
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    forbidden = {
        "urllib", "http", "requests", "httpx", "socket", "smtplib", "ftplib",
        "webbrowser", "selenium", "playwright", "boto3", "botocore", "subprocess",
    }
    assert not imported & forbidden, f"network-capable import: {imported & forbidden}"
    assert "urlopen" not in source
    assert "webdriver" not in source


# ---------------------------------------------------------------------------
# Auth, store failure, caching, routing
# ---------------------------------------------------------------------------

def all_handlers() -> Sequence[tuple[str, Any, dict[str, Any]]]:
    return [
        ("worklist", api.list_worklist, event()),
        ("detail", api.get_posting, event(path_params={"id": "abc"})),
        ("excluded", api.list_excluded, event(path="/excluded")),
        ("internships", api.list_internships, event(path="/internships")),
        ("applied", api.record_applied, applied_event("abc")),
    ]


@pytest.mark.parametrize(("name", "handler", "raw"), all_handlers())
def test_no_jwt_subject_is_401(name: str, handler: Any, raw: dict[str, Any]) -> None:
    stripped = dict(raw)
    stripped["requestContext"] = {"authorizer": {}}
    resp = handler(FakePostingStore([make()]), stripped, now=NOW)
    assert resp["statusCode"] == 401
    assert body_of(resp) == {"error": "unauthorized"}


@pytest.mark.parametrize(("name", "handler", "raw"), all_handlers())
def test_a_store_that_raises_is_503_not_500(
    name: str, handler: Any, raw: dict[str, Any]
) -> None:
    """A dead store is retryable; a 500 tells the UI nothing and looks like a bug."""
    resp = handler(FakePostingStore([make()], raises=True), raw, now=NOW)
    assert resp["statusCode"] == 503
    assert body_of(resp) == {"error": "store_unavailable"}


def test_mark_applied_failure_is_also_503() -> None:
    """The read succeeded and the write failed — the human's click must not be lost silently."""
    posting = make()
    store = FakePostingStore([posting])
    resp = api.record_applied(store, applied_event(posting.id), now=NOW)
    assert resp["statusCode"] == 200
    store.raises = True
    resp = api.record_applied(store, applied_event(posting.id), now=NOW)
    assert resp["statusCode"] == 503


def test_rest_authorizer_shape_is_accepted() -> None:
    store = FakePostingStore([make()])
    resp = api.list_worklist(store, event(rest_shape=True), resume_text=RESUME, now=NOW)
    assert resp["statusCode"] == 200


class TestIndexCache:
    def test_a_warm_container_screens_once(self) -> None:
        """Screening 25k postings per request makes the UI unusable."""
        store = FakePostingStore([make()])
        cache = api.IndexCache(ttl_seconds=300)
        for _ in range(3):
            api.list_worklist(store, event(), resume_text=RESUME, now=NOW, cache=cache)
        assert store.reads == 1

    def test_the_index_expires(self) -> None:
        store = FakePostingStore([make()])
        cache = api.IndexCache(ttl_seconds=60)
        api.list_worklist(store, event(), resume_text=RESUME, now=NOW, cache=cache)
        api.list_worklist(
            store, event(), resume_text=RESUME, now=NOW + timedelta(seconds=61), cache=cache
        )
        assert store.reads == 2

    def test_a_backwards_clock_rebuilds_rather_than_pinning_a_stale_index(self) -> None:
        store = FakePostingStore([make()])
        cache = api.IndexCache(ttl_seconds=300)
        api.list_worklist(store, event(), resume_text=RESUME, now=NOW, cache=cache)
        api.list_worklist(
            store, event(), resume_text=RESUME, now=NOW - timedelta(hours=1), cache=cache
        )
        assert store.reads == 2

    def test_without_a_cache_every_request_is_fresh(self) -> None:
        store = FakePostingStore([make()])
        api.list_worklist(store, event(), resume_text=RESUME, now=NOW)
        api.list_worklist(store, event(), resume_text=RESUME, now=NOW)
        assert store.reads == 2


class TestRouting:
    def test_each_endpoint_is_reachable(self) -> None:
        posting = make()
        store = FakePostingStore([posting])
        assert api.route(store, event(), now=NOW)["statusCode"] == 200
        detail = event(path=f"/worklist/{posting.id}", path_params={"id": posting.id})
        assert api.route(store, detail, now=NOW)["statusCode"] == 200
        assert api.route(store, event(path="/excluded"), now=NOW)["statusCode"] == 200
        assert api.route(store, event(path="/internships"), now=NOW)["statusCode"] == 200
        assert api.route(store, applied_event(posting.id), now=NOW)["statusCode"] == 200

    def test_preflight_needs_no_store(self) -> None:
        resp = api.route(
            FakePostingStore(raises=True), event(method="OPTIONS", path="/worklist"), now=NOW
        )
        assert resp["statusCode"] == 204
        assert resp["body"] == ""  # 204 carries no body
        assert resp["headers"]["Access-Control-Allow-Methods"] == "GET,POST,OPTIONS"

    def test_wrong_method_is_405_and_unknown_path_is_404(self) -> None:
        store = FakePostingStore([make()])
        wrong = api.route(store, event(method="POST", path="/worklist"), now=NOW)
        assert wrong["statusCode"] == 405
        assert body_of(wrong) == {"error": "method_not_allowed"}
        missing = api.route(store, event(path="/nope"), now=NOW)
        assert missing["statusCode"] == 404
        assert body_of(missing) == {"error": "not_found"}

    def test_rest_api_event_shape_routes(self) -> None:
        """REST APIs send httpMethod + resource; the HTTP API sends routeKey."""
        store = FakePostingStore([make()])
        raw = {
            "httpMethod": "GET",
            "resource": "/excluded",
            "requestContext": {"authorizer": {"claims": {"sub": SUB}}},
        }
        assert api.route(store, raw, now=NOW)["statusCode"] == 200


class TestWiring:
    def test_build_store_is_the_v2_posting_store(self) -> None:
        """Not DynamoDbStore: that table is the v1 briefing model and must stay untouched."""
        store = api.build_store(Settings(postings_db_path=Path(":memory:")))
        assert isinstance(store, SqlitePostingStore)

    def test_resume_text_is_empty_when_nothing_is_configured(self, tmp_path: Path) -> None:
        assert api.load_resume_text(Settings(private_dir=tmp_path)) == ""

    def test_resume_html_is_reduced_to_its_text_layer(self, tmp_path: Path) -> None:
        """An ATS reads the text layer, and so does the technology vocabulary."""
        html = tmp_path / "resume" / "html"
        html.mkdir(parents=True)
        (html / "software-engineering.html").write_text("<p>Python</p><span>AWS</span>")
        text = api.load_resume_text(Settings(private_dir=tmp_path))
        assert "<p>" not in text
        assert "Python" in text
