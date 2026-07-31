"""Tests for domain model invariants."""
import pytest
from pydantic import ValidationError

from copilot.domain.models import ApplicationStatus, Email, Job, TriagedEmail


def test_email_is_frozen() -> None:
    e = Email(sender="a@b.com", subject="hi")
    with pytest.raises(ValidationError):
        e.subject = "changed"  # type: ignore[misc]


def test_job_score_bounds() -> None:
    Job(id="1", title="t", company="c", url="u", score=0)
    Job(id="1", title="t", company="c", url="u", score=100)
    with pytest.raises(ValidationError):
        Job(id="1", title="t", company="c", url="u", score=101)
    with pytest.raises(ValidationError):
        Job(id="1", title="t", company="c", url="u", score=-1)


def test_needs_action_derived_from_status() -> None:
    def te(status: ApplicationStatus, *, job: bool = True) -> TriagedEmail:
        return TriagedEmail(email=Email(sender="a", subject="b"), is_job_related=job, status=status)

    assert te(ApplicationStatus.INTERVIEW).needs_action
    assert te(ApplicationStatus.OFFER).needs_action
    assert te(ApplicationStatus.ASSESSMENT).needs_action
    assert not te(ApplicationStatus.APPLIED).needs_action
    assert not te(ApplicationStatus.INTERVIEW, job=False).needs_action  # non-job never acts
