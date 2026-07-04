"""Tests for daily-briefing Markdown rendering."""
from career_copilot import briefing


def _summary(**kw):
    base = {"total_scanned": 0, "job_mail": 0, "by_status": {}, "needs_action": [], "noise": 0}
    base.update(kw)
    return base


def test_needs_action_section_lists_items():
    s = _summary(needs_action=[{"from": "Recruiter <r@x.com>", "subject": "Interview?", "status": "interview"}])
    md = briefing.render(s)
    assert "## 🔴 Needs you" in md
    assert "Interview" in md
    assert "Recruiter" in md  # display name, not the raw <email>


def test_empty_inbox_shows_nothing_needed():
    md = briefing.render(_summary())
    assert "Nothing needs you" in md


def test_jobs_section_renders_matches():
    jobs = [{"title": "SWE", "company": "Acme", "location": "Remote",
             "url": "https://x/1", "score": 92}]
    md = briefing.render(_summary(), jobs=jobs)
    assert "## 🎯 Today's matches" in md
    assert "92%" in md and "SWE @ Acme" in md and "https://x/1" in md


def test_no_jobs_shows_placeholder():
    md = briefing.render(_summary(), jobs=[])
    assert "No new roles today" in md


def test_plan_counts_jobs():
    jobs = [{"title": "A", "company": "B", "location": "", "url": "u", "score": 80}]
    md = briefing.render(_summary(), jobs=jobs)
    assert "Apply to the **1** matches" in md


def test_pipeline_snapshot_summarizes_statuses():
    s = _summary(by_status={"interview": 1, "applied": 2}, job_mail=3, noise=5, total_scanned=8)
    md = briefing.render(s)
    assert "1 interview" in md and "2 applied" in md
    assert "3 job emails" in md and "5 noise" in md
