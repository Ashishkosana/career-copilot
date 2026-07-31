"""Tests for briefing assembly + Markdown rendering."""
from __future__ import annotations

from datetime import datetime

from copilot.domain import briefing
from copilot.domain.models import ApplicationStatus, Briefing, Email, Job, TriagedEmail

NOW = datetime(2026, 7, 6, 14, 0, 0)


def _triaged(
    sender: str, subject: str, *, job: bool, status: ApplicationStatus
) -> TriagedEmail:
    return TriagedEmail(
        email=Email(sender=sender, subject=subject), is_job_related=job, status=status
    )


def _sample() -> Briefing:
    triaged = [
        _triaged("Ashby <no-reply@ashbyhq.com>", "Interview with Cribl",
                 job=True, status=ApplicationStatus.INTERVIEW),
        _triaged("greenhouse <x@greenhouse.io>", "Application received",
                 job=True, status=ApplicationStatus.APPLIED),
        _triaged("deals@shop.com", "50% off", job=False, status=ApplicationStatus.OTHER),
    ]
    jobs = [
        Job(id="a", title="SWE", company="Acme", url="https://x/1", location="Remote", score=92)
    ]
    return briefing.build_briefing(triaged, jobs, now=NOW)


def test_build_briefing_aggregates() -> None:
    b = _sample()
    assert b.scanned == 3 and b.noise == 1
    assert b.pipeline[ApplicationStatus.INTERVIEW] == 1
    assert b.pipeline[ApplicationStatus.APPLIED] == 1
    assert len(b.needs_action) == 1  # only the interview
    assert b.day == NOW.date()


def test_render_has_all_sections_and_content() -> None:
    md = briefing.render_markdown(_sample())
    assert "## 🔴 Needs you" in md
    assert "Interview" in md and "Ashby" in md
    assert "## 🎯 Today's matches" in md and "92%" in md and "SWE @ Acme" in md
    assert "## 📊 Pipeline" in md and "1 interview" in md and "1 applied" in md
    assert "1 job emails" not in md  # phrasing check: it's "2 job emails" (2 job mail, 1 noise)
    assert "2 job emails · 1 noise (of 3 scanned)" in md
    assert "Apply to the **1** matches" in md


def test_render_empty_states() -> None:
    empty = briefing.build_briefing([], [], now=NOW)
    md = briefing.render_markdown(empty)
    assert "Nothing needs you" in md
    assert "No new roles today" in md
    assert "no application mail yet" in md
