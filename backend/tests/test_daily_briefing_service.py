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
from datetime import UTC, datetime

from copilot.adapters.sqlite_posting_store import SqlitePostingStore
from copilot.domain.models import Briefing, Email, Job
from copilot.domain.posting import Posting
from copilot.services.daily_briefing import DailyBriefingService

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
