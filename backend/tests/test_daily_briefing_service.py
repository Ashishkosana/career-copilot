"""Integration test for the daily-briefing orchestration using in-memory fakes.

Proves the ports/DI design end-to-end with zero cloud: the same service that runs
in Lambda runs here against fakes and an in-memory SQLite store.

The supply half is the part worth testing hard. It replaced a job source that read
a ``ja`` database existing on no machine, and therefore served a bundled 4-row
fixture of invented companies as real matches. So these tests assert the counts
that make that failure impossible to hide: ``fetched`` alongside ``kept``
distinguishes "no roles today" from "no source answered", and ``kept`` is the whole
applicable list rather than the ``max_jobs`` slice that reached the briefing.
"""
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from copilot.adapters.sqlite_posting_store import SqlitePostingStore
from copilot.domain.models import Briefing, Email, Job
from copilot.domain.posting import Posting
from copilot.ports.postingstore import (
    QUOTE_MAX_CHARS,
    VIEW_INTERNSHIPS,
    VIEW_KEPT,
    ScreenedRow,
    ScreenSummary,
)
from copilot.services.daily_briefing import DailyBriefingService, build_screening_view

FIXED_NOW = datetime(2026, 7, 6, 14, 0, 0, tzinfo=UTC)

RESUME = """
Python, TypeScript, JavaScript, SQL, Linux
REST, FastAPI, OAuth 2.0, microservices, distributed systems
AWS Lambda, API Gateway, Cognito, CDK, DynamoDB, PostgreSQL, Docker
Next.js, React, Node.js, HTML, CSS, Flutter
pytest, unit testing, integration testing, CI/CD
"""


class FakeMailbox:
    """MailboxPort double whose three methods can each be made to fail.

    Modelled on the real failure: ``GmailMailbox`` with no credentials raises
    ``RuntimeError`` from *every* method, including ``fetch_recent``, which the
    service used to call as its first statement.
    """

    def __init__(
        self,
        emails: list[Email],
        *,
        fetch_error: Exception | None = None,
        draft_error: Exception | None = None,
        send_error: Exception | None = None,
    ) -> None:
        self._emails = emails
        self._fetch_error = fetch_error
        self._draft_error = draft_error
        self._send_error = send_error
        self.drafts: list[tuple[str, str, str]] = []
        self.sent: list[tuple[str, str, str]] = []

    def fetch_recent(self, *, query: str, max_results: int) -> list[Email]:
        if self._fetch_error is not None:
            raise self._fetch_error
        return self._emails[:max_results]

    def create_draft(self, *, to: str, subject: str, body: str) -> None:
        if self._draft_error is not None:
            raise self._draft_error
        self.drafts.append((to, subject, body))

    def send(self, *, to: str, subject: str, body: str) -> None:
        if self._send_error is not None:
            raise self._send_error
        self.sent.append((to, subject, body))


class FakePostings:
    """A PostingSourcePort over a fixed list."""

    name = "fake"

    def __init__(self, postings: list[Posting]) -> None:
        self._postings = postings

    def fetch(self) -> list[Posting]:
        return list(self._postings)


class FakeLLM:
    def __init__(self, reply: str = "Happy to chat — Tuesday works.") -> None:
        self._reply = reply

    def draft_reply(self, email: Email) -> str:
        return self._reply


class FakeStore:
    """The v1 briefing StorePort."""

    def __init__(self) -> None:
        self.briefings: list[Briefing] = []
        self.jobs: list[Job] = []
        self.seen: frozenset[str] = frozenset()

    def save_briefing(self, user_id: str, briefing: Briefing) -> None:
        self.briefings.append(briefing)

    def save_jobs(self, user_id: str, jobs: list[Job]) -> None:
        self.jobs.extend(jobs)

    def seen_job_ids(self, user_id: str) -> frozenset[str]:
        return self.seen

    def latest_briefing(self, user_id: str) -> Briefing | None:
        """Unused by the service, present because ``StorePort`` requires it.

        A double that is missing a protocol member does not satisfy the port, and
        under ``mypy --strict`` over ``tests/`` that is an error rather than a
        detail — the point of a structural port is that a fake which passes here
        could be swapped for the real adapter.
        """
        return self.briefings[-1] if self.briefings else None


def posting(n: int, title: str, *, desc: str = "", company: str = "Acme") -> Posting:
    return Posting(
        title=title,
        company=company,
        url=f"https://boards.example/{n}",
        ats="greenhouse",
        location="Remote (US)",
        description=desc or "Build REST APIs in Python on AWS. 1+ years of experience.",
        desc_available=True,
        posted_at=FIXED_NOW,
    )


def build(
    *,
    emails: list[Email] | None = None,
    postings: list[Posting] | None = None,
    resume: str = RESUME,
    llm: FakeLLM | None = None,
    mailbox: FakeMailbox | None = None,
) -> tuple[DailyBriefingService, FakeMailbox, FakeStore, SqlitePostingStore]:
    mailbox = mailbox if mailbox is not None else FakeMailbox(emails or [])
    store = FakeStore()
    posting_store = SqlitePostingStore(":memory:")
    service = DailyBriefingService(
        mailbox=mailbox,
        store=store,
        postings=FakePostings(postings or []),
        posting_store=posting_store,
        llm=llm if llm is not None else FakeLLM(),
        resume_text=resume,
        now=lambda: FIXED_NOW,
    )
    return service, mailbox, store, posting_store


class TestInboxHalf:
    def test_triages_and_drafts_only(self) -> None:
        emails = [
            Email(
                sender="Recruiter <r@acme.com>",
                subject="Interview request",
                snippet="We would like to schedule an interview with you.",
            ),
            Email(sender="noreply@newsletter.com", subject="Weekly digest", snippet="news"),
        ]
        service, mailbox, store, _ = build(emails=emails)
        run = service.run(user_id="u1")

        assert run.briefing.scanned == 2
        assert len(run.briefing.needs_action) == 1
        assert run.drafts == 1
        # Drafts only — never sent. The guardrail, asserted rather than assumed.
        assert len(mailbox.drafts) == 1
        assert mailbox.sent == []
        assert len(store.briefings) == 1

    def test_emails_the_briefing_only_when_an_address_is_configured(self) -> None:
        service, mailbox, _, _ = build()
        service.run(user_id="u1")
        assert mailbox.sent == []

        service, mailbox, _, _ = build()
        service.run(user_id="u1", my_email="me@example.com")
        assert len(mailbox.sent) == 1

    def test_draft_replies_can_be_turned_off(self) -> None:
        emails = [
            Email(sender="r@acme.com", subject="Interview request",
                  snippet="Can we schedule an interview?")
        ]
        service, mailbox, _, _ = build(emails=emails)
        run = service.run(user_id="u1", draft_replies=False)
        assert run.drafts == 0
        assert mailbox.drafts == []


class TestSupplyHalf:
    def test_counts_make_shrinkage_visible(self) -> None:
        postings = [
            posting(1, "New Grad Software Engineer"),
            posting(2, "Senior Staff Engineer", desc="10+ years. Kubernetes, Kafka."),
            posting(3, "Sales Engineer"),
        ]
        service, _, _, _ = build(postings=postings)
        run = service.run(user_id="u1")

        assert run.supply.fetched == 3
        assert run.supply.new == 3
        assert run.supply.kept == 1, "only the new-grad role is applicable"
        # excluded is total - kept, NOT the sum of per-gate counts, which overcount.
        assert run.supply.excluded == 2

    def test_an_empty_fetch_is_reported_not_silently_zero(self) -> None:
        """The failure that produced the fixture bug: no supply reading as no roles."""
        service, _, _, _ = build(postings=[])
        run = service.run(user_id="u1")
        assert run.supply.fetched == 0
        assert run.supply.kept == 0
        assert run.briefing.jobs == []

    def test_second_run_of_the_same_postings_reports_them_as_known(self) -> None:
        postings = [posting(1, "New Grad Software Engineer")]
        service, _, _, _ = build(postings=postings)
        first = service.run(user_id="u1")
        second = service.run(user_id="u1")
        assert first.supply.new == 1
        assert second.supply.new == 0
        assert second.supply.known == 1

    def test_postings_are_persisted_so_tomorrow_can_diff(self) -> None:
        service, _, _, store = build(postings=[posting(1, "Junior Backend Engineer")])
        service.run(user_id="u1")
        assert store.stats()["total"] == 1

    def test_max_jobs_caps_the_digest_not_the_corpus(self) -> None:
        many = [posting(n, f"New Grad Software Engineer {n}") for n in range(12)]
        service, _, _, _ = build(postings=many)
        service.max_jobs = 3
        run = service.run(user_id="u1")
        assert len(run.briefing.jobs) <= 3
        assert run.supply.kept == 12, "the cap must never read as 'that was all there was'"


class TestInboxFailureDoesNotSinkSupply:
    """The bug this class exists to prevent, in its exact production shape.

    ``handlers.cron.build_service`` constructs ``GmailMailbox()`` with no
    credentials — nothing in the codebase fetches the Gmail secret — and
    ``fetch_recent`` was the first statement of ``run``. Every scheduled run
    therefore failed at 100% before fetching a single board, so the keyless half
    of the product that demonstrably works (25,294 postings from public ATS APIs)
    produced nothing, daily, and the corpus never gained a day of history.
    """

    UNCONFIGURED = RuntimeError("Gmail credentials are not configured.")

    def test_the_supply_half_still_runs_when_the_mailbox_cannot_be_read(self) -> None:
        service, _, _, posting_store = build(
            postings=[posting(1, "New Grad Software Engineer"), posting(2, "Sales Engineer")],
            mailbox=FakeMailbox([], fetch_error=self.UNCONFIGURED),
        )
        run = service.run(user_id="u1")

        assert run.supply.fetched == 2, "an unreachable mailbox must not cost the fetch"
        assert run.supply.kept == 1
        assert run.supply.new == 2
        assert posting_store.stats()["total"] == 2, "the corpus gained the day's history"

    def test_an_unreachable_mailbox_is_reported_not_read_as_a_quiet_morning(self) -> None:
        """``scanned: 0`` is ambiguous; ``inbox_ok: False`` is not."""
        service, _, _, _ = build(mailbox=FakeMailbox([], fetch_error=self.UNCONFIGURED))
        run = service.run(user_id="u1")

        assert run.briefing.scanned == 0
        assert run.inbox_ok is False
        assert run.inbox_error.startswith("RuntimeError: Gmail credentials")

    def test_a_healthy_run_says_the_inbox_was_healthy(self) -> None:
        service, _, _, _ = build(emails=[Email(sender="a@b.c", subject="hi", snippet="hi")])
        run = service.run(user_id="u1")
        assert run.inbox_ok is True
        assert run.inbox_error == ""

    def test_a_failed_send_does_not_lose_a_completed_run(self) -> None:
        """By send time the corpus is synced and the briefing stored. Raising
        would report total failure for a run that did all of its work — and an
        EventBridge retry would refetch 25k postings to re-attempt one email."""
        service, _, store, _ = build(
            postings=[posting(1, "New Grad Software Engineer")],
            mailbox=FakeMailbox([], send_error=OSError("connection reset")),
        )
        run = service.run(user_id="u1", my_email="me@example.com")

        assert run.supply.kept == 1
        assert len(store.briefings) == 1
        assert run.inbox_ok is False
        assert run.inbox_error == "OSError: connection reset"

    def test_a_draft_the_mailbox_rejected_is_not_counted(self) -> None:
        """Reporting "1 draft" for a draft Gmail refused is a lie the user only
        finds by opening Gmail and seeing nothing."""
        emails = [
            Email(sender="r@acme.com", subject="Interview request",
                  snippet="Can we schedule an interview?")
        ]
        service, _, _, _ = build(
            mailbox=FakeMailbox(emails, draft_error=RuntimeError("insufficient scope"))
        )
        run = service.run(user_id="u1")

        assert len(run.briefing.needs_action) == 1
        assert run.drafts == 0
        assert run.inbox_ok is False
        assert run.inbox_error == "RuntimeError: insufficient scope"

    def test_an_error_with_no_message_still_names_its_type(self) -> None:
        """``inbox_error: ""`` alongside ``inbox_ok: False`` would be unreadable."""
        service, _, _, _ = build(mailbox=FakeMailbox([], fetch_error=ConnectionResetError()))
        run = service.run(user_id="u1")
        assert run.inbox_error == "ConnectionResetError"


class TestTheScreeningViewIsMaterialised:
    """The bug: the read API screened the whole corpus on every request.

    Measured on the real 25,294-row corpus — 1.7 s to read, 37.8 s to screen — which
    is ~70 s at the deployed 47,538, against a hard 29 s API Gateway ceiling. Every
    read 504'd, ``?limit=1`` included, because ``eligibleTotal`` and the funnel counts
    describe the whole set and so the whole set had to be screened. The cron has
    900 s and already screens once, so it writes the answer down instead.
    """

    def test_the_run_publishes_a_view_the_read_path_can_serve(self) -> None:
        postings = [
            posting(1, "New Grad Software Engineer"),
            posting(2, "Senior Staff Engineer", desc="10+ years. Kubernetes, Kafka."),
            posting(3, "Software Engineering Intern"),
        ]
        service, _, _, store = build(postings=postings)
        run = service.run(user_id="u1")

        published = store.screening_summary()
        assert published is not None
        assert published.screened == 3
        assert published.kept == 1
        assert published.eligible_total == 1
        assert run.supply.screened == 3
        assert run.supply.eligible == 1
        page = store.screened_page(VIEW_KEPT, generation=published.generation, limit=25)
        assert [row.posting_id for row in page.rows] == [postings[0].id]

    def test_the_screen_covers_the_stored_corpus_not_the_fetch(self) -> None:
        """A posting that is still open but absent from today's fetch is on the page,
        so it has to be in the view. Screening the fetch would drop it — and this is
        the normal state whenever closing is skipped for a degraded sweep.
        """
        first = posting(1, "New Grad Software Engineer")
        second = posting(2, "Junior Backend Engineer")
        service, _, _, store = build(postings=[first, second])
        service.run(user_id="u1")

        # Day two the sweep comes back empty, so nothing is closed — an empty fetch
        # is a broken run, not a market where every job vanished. Both postings are
        # therefore still open, still on the page, and still have to be screened.
        service.postings = FakePostings([])
        run = service.run(user_id="u1")

        assert (run.supply.fetched, run.supply.close_skipped) == (0, "empty_fetch")
        assert run.supply.screened == 2, "the view must cover every open posting"
        published = store.screening_summary()
        assert published is not None
        page = store.screened_page(VIEW_KEPT, generation=published.generation, limit=25)
        assert {row.posting_id for row in page.rows} == {first.id, second.id}

    def test_the_screen_judges_the_merged_description_not_the_fetched_one(self) -> None:
        """Workday's list endpoint ships no description and the store keeps the text
        Greenhouse gave it yesterday. Screening the fetch would publish a verdict the
        read path could never reproduce from the posting it actually serves.
        """
        described = posting(1, "Software Engineer", desc="US citizens only. Python.")
        blank = described.model_copy(update={"description": "", "desc_available": False})
        service, _, _, store = build(postings=[described])
        service.run(user_id="u1")

        service.postings = FakePostings([blank])
        run = service.run(user_id="u1")

        assert run.supply.kept == 0, "the stored citizenship clause still excludes it"
        published = store.screening_summary()
        assert published is not None
        assert published.gates["citizenship_or_itar_restricted"] == 1

    def test_the_gate_counts_and_the_internships_collection_reconcile(self) -> None:
        """The page shows both numbers because they disagree by design: on the live
        corpus the internship gate fires 318 times while the collection is 48 rows —
        264 of those are not software roles, 12 are vendor demo fixtures. Publishing
        the gate count as the collection size would be an off-by-270 lie.
        """
        postings = [
            posting(1, "Software Engineer Intern"),
            posting(2, "Marketing Intern"),
            posting(3, "Finance Co-op"),
        ]
        service, _, _, store = build(postings=postings)
        run = service.run(user_id="u1")

        published = store.screening_summary()
        assert published is not None
        assert published.gates["internship_not_full_time"] == 3
        assert published.internship_total == 1
        assert run.supply.internships == 1
        page = store.screened_page(
            VIEW_INTERNSHIPS, generation=published.generation, limit=25
        )
        assert [row.posting_id for row in page.rows] == [postings[0].id]

    def test_an_excluded_posting_is_filed_under_every_gate_with_its_evidence(self) -> None:
        """/excluded groups by gate and quotes the phrase that tripped *that* gate."""
        service, _, _, store = build(
            postings=[
                posting(
                    1,
                    "Senior Software Engineer",
                    desc="10+ years of experience. Active TS/SCI clearance required.",
                )
            ]
        )
        service.run(user_id="u1")
        published = store.screening_summary()
        assert published is not None

        level = store.screened_page(
            "wrong_seniority_band", generation=published.generation, limit=5
        )
        clearance = store.screened_page(
            "security_clearance_required", generation=published.generation, limit=5
        )
        assert len(level.rows) == 1
        assert len(clearance.rows) == 1
        assert "clearance" in clearance.rows[0].quote.lower()
        assert clearance.rows[0].reason.startswith("clearance required")
        assert published.excluded == 1, "one posting removed"
        assert published.gate_count_total == 2, "two gates fired on it"

    def test_a_failed_publish_never_costs_the_corpus(self) -> None:
        """By this point the fetch is synced. Raising would report total failure for a
        run that did almost all its work, and an EventBridge retry would refetch
        several hundred boards to re-attempt a CPU-bound pass.
        """
        service, _, _, store = build(postings=[posting(1, "New Grad Software Engineer")])
        service.posting_store = _RefusesToPublish(store)
        run = service.run(user_id="u1")

        assert store.stats()["total"] == 1, "the corpus still gained the day"
        assert run.supply.screen_skipped.startswith("RuntimeError: no capacity")
        assert run.supply.view_rows == 0
        assert run.supply.kept == 0, "nothing was published, so nothing is claimed"
        assert run.briefing.jobs == []

    def test_a_failed_publish_leaves_yesterdays_view_current(self) -> None:
        """The read path must keep serving the last complete pass, aging, rather than
        fall back to screening live — which is precisely what 504s.
        """
        service, _, _, store = build(postings=[posting(1, "New Grad Software Engineer")])
        service.run(user_id="u1")
        yesterday = store.screening_summary()

        service.posting_store = _RefusesToPublish(store)
        service.run(user_id="u1")
        assert store.screening_summary() == yesterday

    def test_the_digest_is_drawn_from_the_published_pass(self) -> None:
        """One screening pass, not two. The morning email and the website must not be
        able to disagree about what was applicable today.
        """
        service, _, _, store = build(
            postings=[posting(1, "New Grad Software Engineer"), posting(2, "Sales Engineer")]
        )
        run = service.run(user_id="u1")
        published = store.screening_summary()
        assert published is not None
        assert len(run.briefing.jobs) == published.eligible_total == 1


class TestBuildScreeningView:
    """The materialisation itself — pure, so it is asserted without a store."""

    def test_a_posting_lands_in_one_row_per_view_it_belongs_to(self) -> None:
        view = build_screening_view(
            [
                posting(1, "New Grad Software Engineer"),
                posting(2, "Senior Marketing Manager"),
            ],
            now=FIXED_NOW,
        )
        by_view: dict[str, list[str]] = {}
        for row in view.rows:
            by_view.setdefault(row.view, []).append(row.posting_id)
        assert by_view[VIEW_KEPT] == [posting(1, "x").id]
        # The marketing role fails the software-role gate *and* the seniority gate,
        # so it is filed under both — which is why len(rows) > len(postings).
        assert sorted(by_view) == [
            VIEW_KEPT,
            "not_a_software_role",
            "wrong_seniority_band",
        ]

    def test_the_row_count_exceeds_the_posting_count_by_the_overcount(self) -> None:
        """~1.76 rows per posting on the live corpus. A reader that assumed one row
        per posting would silently drop the second gate a posting failed."""
        postings = [
            posting(1, "New Grad Software Engineer"),
            posting(2, "Senior Sales Engineer", desc="US citizens only."),
        ]
        view = build_screening_view(postings, now=FIXED_NOW)
        assert len(view.rows) == view.summary.kept + view.summary.gate_count_total
        assert len(view.rows) > len(postings)

    def test_an_empty_corpus_produces_an_honest_empty_view(self) -> None:
        """A published-but-empty view is a real state (a broken sweep), and it must be
        distinguishable from "never screened" — which is what the summary's presence
        answers."""
        view = build_screening_view([], now=FIXED_NOW)
        assert view.rows == ()
        assert view.summary.screened == 0
        assert view.summary.corpus_size == 0

    def test_a_stored_quote_is_capped(self) -> None:
        """The row is copied once per view a posting sits in, and the public route
        publishes no description prose."""
        long_clause = "We are unable to provide visa sponsorship " + "blah " * 100
        view = build_screening_view(
            [posting(1, "Software Engineer", desc=long_clause)], now=FIXED_NOW
        )
        quotes = [row.quote for row in view.rows if row.gate == "employer_will_not_sponsor"]
        assert quotes and all(len(q) <= QUOTE_MAX_CHARS for q in quotes)

    def test_two_runs_in_one_second_get_different_generations(self) -> None:
        """Same-second runs writing into the same partitions would mix two passes
        into one view — the half-written state the generation exists to rule out."""
        first = build_screening_view([], now=FIXED_NOW)
        second = build_screening_view([], now=FIXED_NOW + timedelta(microseconds=1))
        assert first.summary.generation != second.summary.generation

    def test_a_generation_never_contains_the_key_delimiter(self) -> None:
        """It is spliced into a DynamoDB partition key around ``#``; a ``#`` inside it
        would make two different generations parse as the same partition."""
        view = build_screening_view([], now=FIXED_NOW)
        assert "#" not in view.summary.generation

    def test_a_naive_posted_at_does_not_take_the_run_down(self) -> None:
        """Comparing an aware epoch against a naive timestamp raises ``TypeError``,
        and one odd row must not cost the whole corpus."""
        naive = posting(1, "New Grad Software Engineer").model_copy(
            update={"posted_at": datetime(2026, 7, 6, 14, 0, 0)}
        )
        view = build_screening_view([naive], now=FIXED_NOW)
        assert len(view.rows) == 1
        assert view.rows[0].sort_key.endswith(naive.id)


class _RefusesToPublish:
    """A posting store whose screening publish always fails.

    Wraps a real store rather than replacing it, so the assertion that the corpus
    survived is made against the same rows the run actually wrote.
    """

    def __init__(self, inner: SqlitePostingStore) -> None:
        self._inner = inner

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def save_screening(
        self, rows: Iterable[ScreenedRow], *, summary: ScreenSummary
    ) -> None:
        raise RuntimeError("no capacity")

    def sync(self, postings: list[Posting], *, now: datetime) -> tuple[list[str], list[str]]:
        return self._inner.sync(postings, now=now)

    def close_missing(self, *, now: datetime, seen_ids: set[str]) -> int:
        return self._inner.close_missing(now=now, seen_ids=seen_ids)

    def open_postings(self) -> list[Posting]:
        return self._inner.open_postings()

    def new_since(self, since: datetime) -> list[Posting]:
        return self._inner.new_since(since)

    def postings_by_id(self, posting_ids: Sequence[str]) -> dict[str, Posting]:
        return self._inner.postings_by_id(posting_ids)

    def cached_interpretation(self, posting_id: str) -> dict[str, Any] | None:
        return self._inner.cached_interpretation(posting_id)

    def save_interpretation(self, posting_id: str, payload: dict[str, Any]) -> None:
        self._inner.save_interpretation(posting_id, payload)

    def mark_applied(self, posting_id: str, *, now: datetime) -> None:
        self._inner.mark_applied(posting_id, now=now)

    def screening_summary(self) -> ScreenSummary | None:
        return self._inner.screening_summary()

    def screened_page(
        self, view: str, *, generation: str, limit: int, after: str | None = None
    ) -> Any:
        return self._inner.screened_page(view, generation=generation, limit=limit, after=after)


class TestScoringHonesty:
    def test_no_resume_means_not_scored_rather_than_scored_zero(self) -> None:
        service, _, _, _ = build(postings=[posting(1, "New Grad Software Engineer")], resume="")
        run = service.run(user_id="u1")
        assert run.scored is False
        assert service.scoring_available is False

    def test_a_resume_enables_scoring(self) -> None:
        service, _, _, _ = build(postings=[posting(1, "New Grad Software Engineer")])
        run = service.run(user_id="u1")
        assert run.scored is True
