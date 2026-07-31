"""Shared test fixtures: a Briefing factory and in-memory fake adapters.

The fakes satisfy the port Protocols structurally, so handlers/services run
against them with no cloud or network.
"""
from __future__ import annotations

from datetime import UTC, datetime

from copilot.domain.models import (
    ApplicationStatus,
    Briefing,
    Email,
    Job,
    TriagedEmail,
)
from copilot.domain.posting import Posting

FIXED_NOW = datetime(2026, 7, 6, 14, 0, 0, tzinfo=UTC)


def make_briefing() -> Briefing:
    """A representative briefing with a needs-action email and a ranked job."""
    triaged = TriagedEmail(
        email=Email(
            sender="Ashby <no-reply@ashbyhq.com>",
            subject="Interview invitation from Cribl",
            snippet="can we schedule a call?",
        ),
        is_job_related=True,
        status=ApplicationStatus.INTERVIEW,
    )
    job = Job(
        id="abc123",
        title="Full Stack Engineer",
        company="Acme",
        url="https://jobs.example.com/acme/fse",
        location="Remote",
        score=72,
    )
    return Briefing(
        generated_at=FIXED_NOW,
        day=FIXED_NOW.date(),
        scanned=3,
        noise=1,
        pipeline={ApplicationStatus.INTERVIEW: 1, ApplicationStatus.APPLIED: 1},
        needs_action=[triaged],
        jobs=[job],
    )


class FakeMailbox:
    def __init__(self, emails: list[Email] | None = None) -> None:
        self._emails = emails or []
        self.drafts: list[tuple[str, str, str]] = []
        self.sent: list[tuple[str, str, str]] = []

    def fetch_recent(self, *, query: str, max_results: int) -> list[Email]:
        return self._emails[:max_results]

    def create_draft(self, *, to: str, subject: str, body: str) -> None:
        self.drafts.append((to, subject, body))

    def send(self, *, to: str, subject: str, body: str) -> None:
        self.sent.append((to, subject, body))


class FakePostings:
    """A PostingSourcePort over a fixed list.

    Replaces the old ``FakeJobs``, which returned bare mappings because the source
    it doubled read a ``ja`` database that existed nowhere and fell back to a
    fixture of invented companies. The port now speaks ``Posting``, so the fake
    does too — a fake that models a shape the real adapter no longer produces is
    worse than no fake at all.
    """

    name = "fake"

    def __init__(self, postings: list[Posting] | None = None) -> None:
        self._postings = postings or []

    def fetch(self) -> list[Posting]:
        return list(self._postings)


def make_posting(
    n: int = 1,
    title: str = "New Grad Software Engineer",
    *,
    company: str = "Acme",
    desc: str = "Build REST APIs in Python on AWS. 1+ years of experience.",
) -> Posting:
    return Posting(
        title=title,
        company=company,
        url=f"https://boards.example/{n}",
        ats="greenhouse",
        location="Remote (US)",
        description=desc,
        desc_available=bool(desc),
        posted_at=datetime(2026, 7, 6, 14, 0, 0, tzinfo=UTC),
    )


class FakeLLM:
    def draft_reply(self, email: Email) -> str:
        return f"Thanks for reaching out about: {email.subject}"


class FakeStore:
    def __init__(self) -> None:
        self.briefings: dict[str, Briefing] = {}
        self.jobs: dict[str, list[Job]] = {}
        self._seen: set[str] = set()

    def save_briefing(self, user_id: str, briefing: Briefing) -> None:
        self.briefings[user_id] = briefing

    def latest_briefing(self, user_id: str) -> Briefing | None:
        return self.briefings.get(user_id)

    def seen_job_ids(self, user_id: str) -> frozenset[str]:
        return frozenset(self._seen)

    def save_jobs(self, user_id: str, jobs: list[Job]) -> None:
        self.jobs.setdefault(user_id, []).extend(jobs)
        self._seen.update(j.id for j in jobs)
