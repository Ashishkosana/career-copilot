"""Worklist read API, driven with an in-memory store — no cloud, no network.

Every test below names the failure it prevents. The ones that matter most:

* **a read that screens the corpus**, which is what took every request to 39 s
  locally and ~72 s at the deployed size and 504'd against API Gateway's 29 s
  ceiling. Asserted twice: no route may touch ``open_postings``, and the module may
  not even name it;
* an unscreened corpus answered with a hang instead of a state — a 29-second timeout
  is indistinguishable from an outage, and "nothing has been screened yet" is a
  different fact from "the service is broken";
* rows published without their summary read as an authoritative empty view, which is
  exactly the shape the first live cron crash left behind;
* a total recounted from the page instead of taken from the summary, because
  recounting is how a read starts touching the corpus again;
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
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
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
from copilot.ports.postingstore import (
    SCREEN_VIEWS,
    PostingStorePort,
    ScreenedPage,
    ScreenedRow,
    ScreenSummary,
)

# The view is built by the cron's own builder, not by a fixture that mimics it — see
# ``FakePostingStore``. This is the one import in this file that is deliberately not
# a fake.
from copilot.services.daily_briefing import build_screening_view

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

#: A non-UTC offset a real ATS actually sends, for the one test that needs the
#: displayed date to differ from the UTC-normalised sort key.
FIVE_HOURS_AHEAD = timezone(timedelta(hours=5))


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakePostingStore:
    """In-memory ``PostingStorePort`` that also plays the cron.

    Construction screens the postings and publishes the view through the *real*
    builder, ``services.daily_briefing.build_screening_view``. That coupling is
    deliberate. These tests are about a reader that consumes what a writer produced,
    and a hand-rolled view here would only ever prove that the reader agrees with
    itself — which is precisely how the DynamoDB key-schema bug survived: a double
    derived from the code under test.

    ``rescreen`` is a second cron run, ``publish=False`` is a cron that died between
    the rows and the summary, and ``doctor_summary`` publishes counts the rows cannot
    support, which is how "the totals come from the summary" is tested as a property
    rather than by reading the handler.

    ``calls`` is the audit trail two guarantees read: that a read never touches the
    whole corpus, and that the applied path touches exactly one write.
    """

    def __init__(
        self,
        postings: Iterable[Posting] = (),
        *,
        raises: bool = False,
        screened: bool = True,
        now: datetime = NOW,
        publish: bool = True,
    ) -> None:
        self.postings = list(postings)
        self.raises = False
        self.applied: dict[str, datetime] = {}
        self.calls: list[str] = []
        self.summary_reads = 0
        self._views: dict[str, tuple[ScreenedRow, ...]] = {}
        self._summary: ScreenSummary | None = None
        if screened:
            self.rescreen(now=now, publish=publish)
        # Setting up the view is the cron's work, not a request's: the audit trail
        # starts empty, and ``raises`` only breaks the reads actually under test.
        self.calls.clear()
        self.raises = raises

    # --- playing the cron ----------------------------------------------------

    def rescreen(self, *, now: datetime = NOW, publish: bool = True) -> None:
        """Screen the corpus and publish the view, exactly as the cron does.

        ``publish=False`` stops after the rows. That is not a hypothetical: the first
        live cron run crashed after the corpus landed and before the run finished, and
        a reader must call the result "not screened" rather than "screened, 0 kept".
        """
        view = build_screening_view(self.postings, now=now)
        if publish:
            self.save_screening(view.rows, summary=view.summary)
        else:
            self._views[view.summary.generation] = tuple(view.rows)

    def doctor_summary(self, **overrides: Any) -> None:
        """Republish the current summary with different numbers on it."""
        assert self._summary is not None
        self._summary = replace(self._summary, **overrides)

    # --- the corpus ----------------------------------------------------------

    def _note(self, what: str) -> None:
        self.calls.append(what)
        if self.raises:
            raise RuntimeError("sqlite file is gone")

    def open_postings(self) -> list[Posting]:
        self._note("open_postings")
        return list(self.postings)

    def postings_by_id(self, posting_ids: Sequence[str]) -> dict[str, Posting]:
        self._note("postings_by_id")
        wanted = set(posting_ids)
        return {p.id: p for p in self.postings if p.id in wanted}

    def mark_applied(self, posting_id: str, *, now: datetime) -> None:
        self._note("mark_applied")
        # Mirrors the SQL guard: `applied_at IS NULL`, so a repeat is a no-op.
        self.applied.setdefault(posting_id, now)

    # --- the materialised screening view -------------------------------------

    def save_screening(self, rows: Iterable[ScreenedRow], *, summary: ScreenSummary) -> None:
        self._note("save_screening")
        # Rows first, summary second — the order *is* the publish, so the fake has to
        # honour it or the ``publish=False`` case above would be untestable.
        self._views[summary.generation] = tuple(rows)
        self._summary = summary

    def screening_summary(self) -> ScreenSummary | None:
        self._note("screening_summary")
        self.summary_reads += 1
        return self._summary

    def screened_page(
        self, view: str, *, generation: str, limit: int, after: str | None = None
    ) -> ScreenedPage:
        self._note("screened_page")
        if view not in SCREEN_VIEWS:
            raise ValueError(f"unknown screening view {view!r}")
        rows = sorted(
            (row for row in self._views.get(generation, ()) if row.view == view),
            key=lambda row: row.sort_key,
            reverse=True,
        )
        if after is not None:
            rows = [row for row in rows if row.sort_key < after]
        window = tuple(rows[:limit])
        next_token = window[-1].sort_key if len(rows) > limit and window else None
        return ScreenedPage(rows=window, next_token=next_token)

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
    assert store.screening_summary() is not None


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
    posted_at: datetime | str | None = "auto",
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


def worklist(store: FakePostingStore, *, at: datetime = NOW, **kwargs: Any) -> dict[str, Any]:
    resp = api.list_worklist(store, event(**kwargs), resume_text=RESUME, now=at)
    assert resp["statusCode"] == 200, resp["body"]
    return body_of(resp)


def ids_of(page: dict[str, Any]) -> list[str]:
    return [item["id"] for item in page["items"]]


# ---------------------------------------------------------------------------
# The read consumes the view. This section is the fix.
# ---------------------------------------------------------------------------

def every_read(store: FakePostingStore, posting: Posting) -> None:
    """Drive all five routes once, the way the page and the UI build do."""
    api.list_worklist(store, event(), resume_text=RESUME, now=NOW)
    api.list_internships(store, event(path="/internships"), resume_text=RESUME, now=NOW)
    api.list_excluded(store, event(path="/excluded"), now=NOW)
    api.get_posting(store, event(path_params={"id": posting.id}), now=NOW)
    api.record_applied(store, applied_event(posting.id), now=NOW)


class TestTheReadNeverScreensTheCorpus:
    """The production 504, and the two assertions that keep it dead.

    Measured before this change: ``open_postings`` returned 25,294 rows in 1.7 s and
    screening them took 37.8 s — 1.506 ms each, so ~72 s at the deployed 47,538. API
    Gateway's REST integration ceiling is 29 s, hard. ``?limit=1`` did not help,
    because ``limit`` is applied after screening: ``eligibleTotal`` and the funnel
    describe the whole set.
    """

    def test_no_route_reads_the_whole_corpus(self) -> None:
        posting = make()
        store = FakePostingStore([posting, an_internship()])
        every_read(store, posting)
        assert "open_postings" not in store.calls
        assert set(store.calls) == {
            "screening_summary", "screened_page", "postings_by_id", "mark_applied"
        }

    def test_the_module_does_not_even_name_open_postings(self) -> None:
        """Structural, so a future edit cannot reintroduce it behind a flag or a retry.

        The call-log test above only proves nothing reached it *on these paths*; this
        one proves there is no path at all.
        """
        tree = ast.parse(Path(api.__file__).read_text())
        reached = {
            node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
        }
        assert "open_postings" not in reached

    def test_a_page_reads_one_page_and_not_one_row_per_posting(self) -> None:
        """O(page), not O(corpus): the property the whole materialisation buys."""
        store = FakePostingStore(make(day=d, url=f"https://x/{d}") for d in range(1, 29))
        page = worklist(store, query={"limit": "25"})
        assert page["page"]["count"] == 25
        # One summary read, one view query, one batched hydrate. Nothing per posting.
        assert store.calls == ["screening_summary", "screened_page", "postings_by_id"]

    def test_the_totals_are_the_summarys_and_are_not_recounted(self) -> None:
        """A recount is a read that touches the corpus, which is the bug.

        The summary here claims a 47,538-posting pass over a store holding one row. A
        handler deriving its funnel from the page would answer 1 and look correct in
        every other test in this file.
        """
        store = FakePostingStore([make(title="Junior Software Engineer")])
        store.doctor_summary(
            screened=47538, kept=811, excluded=46727, eligible_total=811,
            internship_total=48, needs_level_check=631,
            gates={Exclusion.INTERNSHIP.value: 318},
        )
        page = worklist(store)
        assert page["funnel"]["screened"] == 47538
        assert page["funnel"]["excluded"] == 46727
        assert page["funnel"]["needsLevelCheck"] == 631
        assert page["eligibleTotal"] == 811
        assert page["internshipTotal"] == 48
        # Reconciliation the published page displays: the gate fires 318 times while
        # the collection is 48 postings, and both numbers travel in one payload.
        assert page["funnel"]["gates"][Exclusion.INTERNSHIP.value] == 318
        assert page["page"]["count"] == 1  # the page is still just the page

    def test_generated_at_is_when_the_corpus_was_screened(self) -> None:
        """Not "now": claiming freshness for a twenty-hour-old pass is a lie in a field."""
        screened = NOW - timedelta(hours=20)
        store = FakePostingStore([make()], now=screened)
        page = worklist(store)
        assert page["generatedAt"] == screened.isoformat()

    def test_a_republished_view_is_served_and_the_old_pass_is_not(self) -> None:
        """A page must never mix two screening passes; the generation is what prevents it."""
        first, second = make(day=1, url="https://x/1"), make(day=2, url="https://x/2")
        store = FakePostingStore([first])
        later = NOW + timedelta(hours=1)
        store.postings.append(second)
        store.rescreen(now=later)
        page = worklist(store, at=later)
        assert set(ids_of(page)) == {first.id, second.id}
        assert page["generatedAt"] == later.isoformat()


class TestNotReadyIsAnAnswer:
    """"Nothing has been screened yet" must be distinguishable from "we are broken".

    Today both were a 29-second hang, which is the worst of the three outcomes: a
    timeout tells a visitor nothing and tells a monitor the wrong thing.
    """

    @pytest.mark.parametrize(
        ("name", "handler", "raw"),
        [
            ("worklist", api.list_worklist, event()),
            ("internships", api.list_internships, event(path="/internships")),
            ("excluded", api.list_excluded, event(path="/excluded")),
        ],
    )
    def test_an_unscreened_corpus_is_a_named_state(
        self, name: str, handler: Any, raw: dict[str, Any]
    ) -> None:
        store = FakePostingStore([make()], screened=False)
        resp = handler(store, raw, now=NOW)
        assert resp["statusCode"] == 503
        assert body_of(resp) == {"error": api.NOT_SCREENED}
        # And emphatically not by screening live, which is what 504s.
        assert "open_postings" not in store.calls

    def test_rows_without_their_summary_are_not_an_empty_view(self) -> None:
        """The first live cron crashed mid-run. Its half-written view must not publish.

        The summary is written last precisely so its absence means "incomplete", and a
        reader that paged the rows anyway would serve a partial screen as authoritative
        — or, worse, report "0 eligible of 0 screened", which reads as a working search
        that found nothing.
        """
        store = FakePostingStore([make(title="Junior Software Engineer")], publish=False)
        resp = api.list_worklist(store, event(), resume_text=RESUME, now=NOW)
        assert resp["statusCode"] == 503
        assert body_of(resp) == {"error": api.NOT_SCREENED}

    def test_one_missed_cron_run_still_serves(self) -> None:
        """The grace period is a day and a half, so a single failed run is not an outage."""
        store = FakePostingStore([make()], now=NOW - timedelta(hours=30))
        assert worklist(store)["page"]["count"] == 1

    def test_two_missed_runs_are_refused_rather_than_served_quietly(self) -> None:
        """There is no field on the wire for "shown, but three days old".

        Serving it silently is how a dead cron goes unnoticed — the failure mode this
        whole project has already been bitten by twice.
        """
        store = FakePostingStore([make()], now=NOW - timedelta(hours=60))
        resp = api.list_worklist(store, event(), resume_text=RESUME, now=NOW)
        assert resp["statusCode"] == 503
        assert body_of(resp) == {"error": api.VIEW_STALE}

    def test_a_view_stamped_in_the_future_is_refused(self) -> None:
        """Writer and reader clocks disagree; serving it reports a date nothing explains."""
        store = FakePostingStore([make()], now=NOW + timedelta(hours=2))
        resp = api.list_worklist(store, event(), resume_text=RESUME, now=NOW)
        assert resp["statusCode"] == 503
        assert body_of(resp) == {"error": api.VIEW_STALE}

    def test_a_detail_read_works_with_no_view_at_all(self) -> None:
        """It re-screens one posting (1.5 ms), so it needs no pass to have happened.

        Worth keeping: the trust surface's click-through is the one read that still
        answers while the corpus is unscreened.
        """
        posting = make(title="Junior Software Engineer")
        store = FakePostingStore([posting], screened=False)
        resp = api.get_posting(
            store, event(path_params={"id": posting.id}), resume_text=RESUME, now=NOW
        )
        assert resp["statusCode"] == 200
        assert body_of(resp)["posting"]["screening"]["kept"] is True
        assert "screening_summary" not in store.calls

    def test_applied_records_with_no_view_at_all(self) -> None:
        """A human's click must not be lost because the cron has not run."""
        posting = make()
        store = FakePostingStore([posting], screened=False)
        resp = api.record_applied(store, applied_event(posting.id), now=NOW)
        assert resp["statusCode"] == 200
        assert store.applied == {posting.id: NOW}


class TestHydrationGaps:
    """The corpus moves under a reader: a row can outlive the posting it points at."""

    def test_a_reaped_posting_costs_its_row_not_the_request(self) -> None:
        """The port's contract, and the alternative is a 500 for one missing row."""
        rows = [make(day=d, url=f"https://x/{d}") for d in range(1, 4)]
        store = FakePostingStore(rows)
        store.postings = [p for p in rows if p.url != "https://x/2"]  # reaped, not rescreened
        page = worklist(store)
        assert len(page["items"]) == 2
        assert page["matched"] == 3  # the summary still describes the pass that ran

    def test_a_reaped_posting_does_not_stall_pagination(self) -> None:
        """The gap must still advance the cursor, or page 2 starts before it forever."""
        rows = [make(day=d, url=f"https://x/{d}") for d in range(1, 6)]
        store = FakePostingStore(rows)
        store.postings = [p for p in rows if p.url != "https://x/4"]
        seen = paginate(store, limit=2)
        assert len(seen) == len(set(seen)) == 4


def test_a_card_from_the_view_matches_a_card_from_a_fresh_screen() -> None:
    """The list card and the detail card must not disagree about the same posting.

    A card's verdict comes from a stored row; a detail read re-screens. The mapping
    from a verdict to those fields therefore exists twice — here in
    ``Screened.from_decision`` and in ``services/daily_briefing._row`` — because the
    writer cannot import the reader. Drift would show one seniority band on a card and
    a different one on that card's own page, and nothing else in either suite would
    notice.
    """
    posting = make(title="Junior Software Engineer")
    store = FakePostingStore([posting])
    card = worklist(store)["items"][0]
    found = body_of(
        api.get_posting(
            store, event(path_params={"id": posting.id}), resume_text=RESUME, now=NOW
        )
    )["posting"]
    assert {key: found[key] for key in card if key != "score"} == {
        key: value for key, value in card.items() if key != "score"
    }
    assert card["score"] == found["score"]


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
        """Nothing in Posting forces a tz; a naive value used to raise TypeError.

        The order now lives in the stored sort key, which ``sort_stamp`` normalises to
        UTC — so this asserts the reader agrees with that normalisation rather than
        re-deriving an ordering of its own.
        """
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

    def test_the_score_is_not_stored_so_a_new_resume_reranks_immediately(self) -> None:
        """A baked score would misrank silently: the résumé changes, the corpus does not.

        Same store, same view, two résumés — the tiers must differ, which they can only
        do if the score is computed per request.
        """
        store = FakePostingStore([make(title="Junior Software Engineer")])
        rich = body_of(api.list_worklist(store, event(), resume_text=RESUME, now=NOW))
        thin = body_of(api.list_worklist(store, event(), resume_text="Excel", now=NOW))
        assert rich["items"][0]["score"]["tier"] == "strong"
        assert thin["items"][0]["score"]["tier"] != "strong"


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

    def test_a_band_filter_is_answered_from_the_view_without_hydrating_everything(
        self,
    ) -> None:
        """The band is *on* the row, so narrowing by it costs the walk and nothing else.

        Worth pinning because the alternative — hydrating the whole view to read a
        field the view already stores — is how an O(page) read quietly becomes O(view).
        """
        store = FakePostingStore(
            make(title="Junior Software Engineer", day=d, url=f"https://x/{d}")
            for d in range(1, 20)
        )
        page = worklist(store, query={"level": "entry", "limit": "5"})
        assert page["matched"] == 19
        assert page["page"]["count"] == 5
        # One hydrate, for the page — not one per store page of the walk.
        assert store.calls.count("postings_by_id") == 1

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

    def test_a_view_too_large_to_filter_exactly_is_refused_before_any_work(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The one branch where a read could start growing with the corpus again.

        ``ats`` and ``tier`` are not in the view, so an exact ``matched`` costs a
        hydrate — and for ``tier`` a score, measured at 5.1 ms a posting — per candidate
        row. Affordable over 811 kept postings and not over 47,538, so the bound is
        enforced rather than assumed. Refused against the summary's own total, so it
        costs one read rather than the scan it is refusing to do.
        """
        store = FakePostingStore([make(day=d, url=f"https://x/{d}") for d in range(1, 4)])
        monkeypatch.setattr(api, "FILTER_SCAN_MAX_ROWS", 2)
        resp = api.list_worklist(
            store, event(query={"ats": "greenhouse"}), resume_text=RESUME, now=NOW
        )
        assert resp["statusCode"] == 400
        assert body_of(resp) == {"error": api.TOO_MANY_ROWS}
        assert "screened_page" not in store.calls  # refused before the first row
        # Unfiltered paging is unaffected: that path never scans.
        assert worklist(store)["page"]["count"] == 3

    def test_a_walk_that_runs_out_of_time_gives_up_with_the_ceiling_in_hand(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The row bound is a proxy for time; this is the invariant itself.

        Scoring is 5.1 ms a posting against the real corpus and it scales with the
        résumé, so the rows-to-seconds constant is not stable — the day the résumé grows,
        a row bound protects nothing. A walk that has spent its budget answers 503 with
        23 s of the gateway's 29 s still unspent, rather than being killed at 29 s and
        looking like an outage.
        """
        store = FakePostingStore(
            make(title="Junior Software Engineer", day=d, url=f"https://x/{d}")
            for d in range(1, 12)
        )
        monkeypatch.setattr(api, "SCAN_PAGE_ROWS", 2)  # force several store pages
        monkeypatch.setattr(api, "FILTER_SCAN_BUDGET_SECONDS", -1.0)  # already spent
        resp = api.list_worklist(
            store, event(query={"level": "entry"}), resume_text=RESUME, now=NOW
        )
        # 503 and its own code: unlike the size bound this one really can come out
        # differently next time, so a retry is honest advice rather than a loop.
        assert resp["statusCode"] == 503
        assert body_of(resp) == {"error": api.SCAN_TOO_SLOW}

    def test_a_walk_that_never_finishes_refuses_rather_than_reporting_a_short_count(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The page bound used to end the loop and return what it had.

        ``_MAX_SCAN_PAGES`` is a belt on the two real bounds and it cannot be reached
        while a store returns full pages. The reason it must not simply fall out of the
        loop is what happens if one ever does not: ``matched`` is reported as the *exact*
        number of rows matching across the whole collection, so a truncated walk
        publishes a wrong total on the trust surface with nothing anywhere saying it is
        short. A wrong count that looks right is the failure mode this rewrite exists to
        remove; a 503 is the lesser answer.

        Driven by a store that promises another page forever, which is the only shape
        that gets here.
        """
        store = FakePostingStore([make(title="Junior Software Engineer")])
        real_page = store.screened_page

        def never_ends(view: str, **kwargs: Any) -> ScreenedPage:
            page = real_page(view, **kwargs)
            # Same rows, but always "there is more" — a short page plus a next token.
            return ScreenedPage(rows=page.rows, next_token=page.next_token or "9999")

        monkeypatch.setattr(store, "screened_page", never_ends)
        resp = api.list_worklist(
            store, event(query={"level": "entry"}), resume_text=RESUME, now=NOW
        )
        assert resp["statusCode"] == 503
        assert body_of(resp) == {"error": api.SCAN_TOO_SLOW}

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

    def test_an_offset_posted_at_comes_back_as_the_instant_it_names(self) -> None:
        """The stored sort key is UTC-normalised; the *displayed* date is the posting's.

        Both stores keep the ordering byte-comparable by normalising, which is why the
        card's ``postedAt`` is taken from the hydrated posting rather than from the row
        — the row would report ``04:00+00:00`` for a posting whose ATS said
        ``09:00+05:00``.
        """
        offset = datetime(2026, 7, 15, 9, 0, tzinfo=UTC).astimezone(FIVE_HOURS_AHEAD)
        posting = make(title="Junior Software Engineer", posted_at=offset)
        page = worklist(FakePostingStore([posting]))
        assert page["items"][0]["postedAt"] == offset.isoformat()


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
        # Newest first, so page 1 is days 7,6,5. Day 7 then closes and the cron
        # republishes the view without it.
        store.postings = [p for p in postings if p.url != "https://x/7"]
        store.rescreen()
        second = worklist(store, query={"limit": "3", "cursor": first["page"]["nextCursor"]})
        # Anchored to day 5, so day 4 is next. An offset of 3 into the now-shorter
        # list [6,5,4,3,2,1] would have started at day 3 and lost day 4 entirely.
        wanted = {"https://x/4", "https://x/3", "https://x/2"}
        assert ids_of(second) == [p.id for p in reversed(postings) if p.url in wanted]

    def test_a_cursor_survives_the_nightly_republish(self) -> None:
        """A cursor anchors to a posting, not to a pass — so the generation stays out of it.

        Folding the generation into the fingerprint would be the "safe" choice and
        would refuse every in-flight "load more" once a day, for no benefit: keyset
        paging is already correct across a corpus that changed underneath it.
        """
        postings = [make(day=d, url=f"https://x/{d}") for d in range(1, 7)]
        store = FakePostingStore(postings)
        first = worklist(store, query={"limit": "2"})
        store.rescreen(now=NOW + timedelta(hours=1))
        second = worklist(
            store,
            at=NOW + timedelta(hours=1),
            query={"limit": "2", "cursor": first["page"]["nextCursor"]},
        )
        assert not set(ids_of(first)) & set(ids_of(second))

    def test_a_cursor_past_the_end_is_an_empty_page_not_an_error(self) -> None:
        store = FakePostingStore([make(day=5, url="https://x/5")])
        page = worklist(store, query={"limit": "1"})
        assert page["page"]["nextCursor"] is None  # single row, nothing after it
        # A well-formed cursor positioned after every row: undated sorts last.
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

    def test_a_filtered_list_pages_without_repeating_a_row(self) -> None:
        """The filtered branch does its own window arithmetic, so it gets its own walk."""
        store = FakePostingStore(
            make(title="Junior Software Engineer", day=d, url=f"https://x/{d}")
            for d in range(1, 12)
        )
        seen: list[str] = []
        cursor: str | None = None
        for _ in range(10):
            query = {"limit": "4", "level": "entry"}
            if cursor:
                query["cursor"] = cursor
            page = worklist(store, query=query)
            assert page["matched"] == 11  # the whole view, every page, not the page
            seen.extend(ids_of(page))
            cursor = page["page"]["nextCursor"]
            if cursor is None:
                break
        assert len(seen) == len(set(seen)) == 11


def _fingerprint() -> str:
    return api.WorklistFilters().fingerprint


def _cursor_after_everything() -> str:
    """A well-formed cursor whose key sorts after every real row (undated is last)."""
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

    def test_a_detail_read_costs_one_lookup_and_one_screen(self) -> None:
        """No view, no page, no walk — that is why it can be exact about all seven gates."""
        posting = make()
        store = FakePostingStore([posting])
        api.get_posting(store, event(path_params={"id": posting.id}), now=NOW)
        assert store.calls == ["postings_by_id"]


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

    def test_the_evidence_is_the_pass_s_own_and_is_not_re_derived(self) -> None:
        """A quote read off the row provably belongs to the pass whose counts sit beside it.

        Re-screening to fill this in would be the read path screening again, one gate at
        a time — and would let a gate's evidence describe a different pass than its
        count.
        """
        store = FakePostingStore([make(title="Account Executive")])
        page = excluded(store)
        assert group_for(page, "not_a_software_role")["items"][0]["quote"] == "Account Executive"
        # Seven gate views plus one hydrate each; no screen, no corpus read.
        assert store.calls.count("screened_page") == len(Exclusion)
        assert "open_postings" not in store.calls

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
        """Real Workday titles run to hundreds of characters; the quote is an excerpt.

        Capped at the point the row is *written* now, which is the stronger place for
        it: the 180-character promise no longer depends on a serialiser remembering to
        trim what the store handed it.
        """
        page = excluded(FakePostingStore([make(title="Program Manager " * 40)]))
        quote = group_for(page, "not_a_software_role")["items"][0]["quote"]
        assert len(quote) == api.QUOTE_MAX_CHARS
        assert quote.endswith("…")

    def test_a_gate_with_no_quotable_phrase_reports_null_not_an_empty_string(self) -> None:
        """The demo-board gate's evidence is the tenant slug, which ``reason`` names."""
        page = excluded(FakePostingStore([make(company="leverdemo")]))
        [row] = group_for(page, "ats_vendor_demo_board")["items"]
        assert row["quote"] is None
        assert "demo board" in row["reason"]

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

    def test_a_group_reads_a_page_and_not_its_whole_gate(self) -> None:
        """The largest real gate holds ~41,500 rows; a page of ten must read ten."""
        store = FakePostingStore(
            make(title="Program Manager", day=d, url=f"https://x/{d}") for d in range(1, 20)
        )
        page = excluded(store, query={"gate": "not_a_software_role", "limit": "3"})
        group = page["groups"][0]
        assert group["count"] == 19  # from the summary
        assert group["page"]["count"] == 3
        assert store.calls.count("screened_page") == 1

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

    def test_the_two_collections_are_separate_views_not_one_filtered_set(self) -> None:
        """Disjoint populations with different denominators, materialised separately."""
        store = FakePostingStore([a_full_time_role(), an_internship()])
        assert not set(ids_of(worklist(store))) & set(ids_of(internships(store)))

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

    def test_serving_both_collections_costs_one_summary_read(self) -> None:
        """Both are views over one pass, so one summary answers both — with a cache."""
        store = FakePostingStore([a_full_time_role(), an_internship()])
        cache = api.SummaryCache(ttl_seconds=300)
        api.list_worklist(store, event(), resume_text=RESUME, now=NOW, cache=cache)
        api.list_internships(
            store, event(path="/internships"), resume_text=RESUME, now=NOW, cache=cache
        )
        assert store.summary_reads == 1


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

    def test_a_posting_that_has_since_closed_can_still_be_recorded(self) -> None:
        """Closing is exactly what happens to a role you applied to.

        The old implementation validated the id against the *open* screened index and
        documented the 404 as a known limit. A single-posting read closes it, and the
        store's hydrate deliberately does not filter on ``closed_at``.
        """
        posting = make()
        store = FakePostingStore([posting])
        store.rescreen()  # a pass in which nothing is open is not the question
        resp = api.record_applied(store, applied_event(posting.id), now=NOW)
        assert resp["statusCode"] == 200
        assert store.applied == {posting.id: NOW}

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
        assert store.calls == ["postings_by_id", "mark_applied"]


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
    """A dead store is retryable; a 500 tells the UI nothing and looks like a bug.

    Distinct from the not-ready states on purpose: ``store_unavailable`` means the
    store could not be read, ``corpus_not_screened`` means it was read and there is
    nothing in it yet. A page renders those as different sentences.
    """
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


class TestSummaryCache:
    """What is left of the old ``IndexCache``, and why it is only the summary now."""

    def test_a_warm_container_reads_the_summary_once(self) -> None:
        store = FakePostingStore([make()])
        cache = api.SummaryCache(ttl_seconds=300)
        for _ in range(3):
            api.list_worklist(store, event(), resume_text=RESUME, now=NOW, cache=cache)
        assert store.summary_reads == 1
        # The pages themselves are still read per request — a cached page would be a
        # cached answer to a different question.
        assert store.calls.count("screened_page") == 3

    def test_the_summary_expires_so_a_republish_is_picked_up(self) -> None:
        store = FakePostingStore([make()])
        cache = api.SummaryCache(ttl_seconds=60)
        api.list_worklist(store, event(), resume_text=RESUME, now=NOW, cache=cache)
        api.list_worklist(
            store, event(), resume_text=RESUME, now=NOW + timedelta(seconds=61), cache=cache
        )
        assert store.summary_reads == 2

    def test_a_backwards_clock_re_reads_rather_than_pinning_a_stale_summary(self) -> None:
        store = FakePostingStore([make()], now=NOW - timedelta(hours=2))
        cache = api.SummaryCache(ttl_seconds=300)
        api.list_worklist(store, event(), resume_text=RESUME, now=NOW, cache=cache)
        api.list_worklist(
            store, event(), resume_text=RESUME, now=NOW - timedelta(hours=1), cache=cache
        )
        assert store.summary_reads == 2

    def test_an_absent_summary_is_not_cached(self) -> None:
        """"Not screened yet" is the state that most wants to end promptly.

        Caching it would keep a container answering 503 for the whole TTL after the
        cron finally published, which is the same "a cached error outlives its cause"
        argument the public route's ``no-store`` header makes.
        """
        store = FakePostingStore([make()], screened=False)
        cache = api.SummaryCache(ttl_seconds=300)
        for _ in range(2):
            resp = api.list_worklist(store, event(), resume_text=RESUME, now=NOW, cache=cache)
            assert body_of(resp) == {"error": api.NOT_SCREENED}
        assert store.summary_reads == 2
        store.rescreen()
        assert worklist(store)["page"]["count"] == 1

    def test_without_a_cache_every_request_re_reads(self) -> None:
        store = FakePostingStore([make()])
        api.list_worklist(store, event(), resume_text=RESUME, now=NOW)
        api.list_worklist(store, event(), resume_text=RESUME, now=NOW)
        assert store.summary_reads == 2

    def test_the_old_index_cache_name_is_gone(self) -> None:
        """``IndexCache`` was kept for one commit as an alias while
        ``tools/ui/build_ui.py`` still constructed it. That call site now builds a
        ``SummaryCache``, so the alias is deleted — and asserted deleted, because a
        compatibility shim nobody removes is how two names for one thing become two
        things: the next reader who finds ``IndexCache`` would reasonably expect it to
        cache an index."""
        assert not hasattr(api, "IndexCache")


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


def test_a_real_sqlite_store_serves_the_view_it_was_given(tmp_path: Path) -> None:
    """One end-to-end pass through a real adapter, because the fake is still a fake.

    Everything above drives an in-memory double. This drives the same read path over
    ``SqlitePostingStore``: sync the corpus, publish a view built by the cron's
    builder, then serve a page. It is the smallest test that would fail if the port's
    two halves — what the writer stores and what the reader queries — disagreed.
    """
    store = SqlitePostingStore(tmp_path / "postings.db")
    postings = [make(title="Junior Software Engineer", day=d, url=f"https://x/{d}")
                for d in range(1, 6)]
    postings.append(make(title="Senior Software Engineer", url="https://x/senior"))
    store.sync(postings, now=NOW)
    view = build_screening_view(postings, now=NOW)
    store.save_screening(view.rows, summary=view.summary)

    page = body_of(api.list_worklist(store, event(query={"limit": "2"}), resume_text=RESUME,
                                     now=NOW))
    assert page["matched"] == 5
    assert page["page"]["count"] == 2
    assert page["funnel"]["gates"]["wrong_seniority_band"] == 1
    dropped = body_of(api.list_excluded(store, event(path="/excluded"), now=NOW))
    assert group_for(dropped, "wrong_seniority_band")["items"][0]["quote"] == (
        "Senior Software Engineer"
    )
