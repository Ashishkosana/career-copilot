"""Tests for pure email triage."""
from copilot.domain import triage
from copilot.domain.models import ApplicationStatus, Email


def _email(sender: str = "x@example.com", subject: str = "", snippet: str = "") -> Email:
    return Email(sender=sender, subject=subject, snippet=snippet)


def test_job_related_by_ats_sender() -> None:
    assert triage.is_job_related(_email(sender="no-reply@us.greenhouse-mail.io"))
    assert triage.is_job_related(_email(sender="jobs@ashbyhq.com"))
    assert triage.is_job_related(_email(sender="careers@acme.com"))


def test_job_related_by_subject() -> None:
    assert triage.is_job_related(_email(subject="Thank you for applying to Acme"))


def test_not_job_related() -> None:
    assert not triage.is_job_related(_email(sender="deals@nike.com", subject="50% off shoes"))


def test_status_offer_beats_others() -> None:
    e = _email(subject="We are pleased to offer you the role", snippet="offer letter attached")
    assert triage.classify_status(e) == ApplicationStatus.OFFER


def test_status_interview_requires_intent() -> None:
    real = _email(subject="Let's schedule an interview call this week")
    marketing = _email(subject="What's it like to interview at ICF?")
    assert triage.classify_status(real) == ApplicationStatus.INTERVIEW
    assert triage.classify_status(marketing) == ApplicationStatus.OTHER


def test_status_assessment_and_rejected() -> None:
    assessment = triage.classify_status(_email(subject="Your CodeSignal assessment"))
    rejected = triage.classify_status(_email(snippet="unfortunately we will not be moving forward"))
    assert assessment == ApplicationStatus.ASSESSMENT
    assert rejected == ApplicationStatus.REJECTED


def test_status_applied() -> None:
    assert triage.classify_status(
        _email(subject="We have received your application")
    ) == ApplicationStatus.APPLIED


def test_triage_sets_needs_action_only_for_action_statuses() -> None:
    interview = triage.triage(_email(sender="r@lever.co", subject="interview invitation"))
    applied = triage.triage(_email(sender="r@lever.co", subject="application received"))
    noise = triage.triage(_email(sender="deals@shop.com", subject="sale"))
    assert interview.needs_action is True
    assert applied.needs_action is False
    assert noise.needs_action is False and noise.status is ApplicationStatus.OTHER


def test_pipeline_counts_only_job_mail() -> None:
    triaged = triage.triage_all([
        _email(sender="r@lever.co", subject="interview invitation"),
        _email(sender="r@greenhouse.io", subject="application received"),
        _email(sender="deals@shop.com", subject="sale ends tonight"),
    ])
    counts = triage.pipeline_counts(triaged)
    assert counts[ApplicationStatus.INTERVIEW] == 1
    assert counts[ApplicationStatus.APPLIED] == 1
    assert sum(counts.values()) == 2  # noise excluded
