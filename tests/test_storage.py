"""Tests for the DynamoDB store's local-JSON fallback and tenant isolation."""
import pytest

from career_copilot import storage


@pytest.fixture
def local_db(tmp_path, monkeypatch):
    path = str(tmp_path / "db.json")
    monkeypatch.setattr(storage, "_LOCAL_DB", path)
    return path


def test_save_and_read_latest_briefing(local_db):
    storage.save_briefing("u1", "2026-07-01", "old", {})
    storage.save_briefing("u1", "2026-07-04", "new", {"job_mail": 3})
    latest = storage.latest_briefing("u1")
    assert latest["markdown"] == "new"
    assert latest["SK"] == "BRIEFING#2026-07-04"


def test_latest_briefing_none_when_empty(local_db):
    assert storage.latest_briefing("nobody") is None


def test_save_jobs_and_seen_ids(local_db):
    storage.save_jobs("u1", [{"id": "aaa", "url": "x"}, {"id": "bbb", "url": "y"}])
    assert storage.seen_job_ids("u1") == {"aaa", "bbb"}


def test_user_isolation(local_db):
    storage.save_briefing("u1", "2026-07-04", "u1 briefing", {})
    storage.save_jobs("u1", [{"id": "j1"}])
    storage.save_briefing("u2", "2026-07-04", "u2 briefing", {})

    assert storage.latest_briefing("u1")["markdown"] == "u1 briefing"
    assert storage.latest_briefing("u2")["markdown"] == "u2 briefing"
    assert storage.seen_job_ids("u2") == set()  # u1's jobs don't leak to u2


def test_save_jobs_is_idempotent_on_id(local_db):
    storage.save_jobs("u1", [{"id": "dup", "url": "first"}])
    storage.save_jobs("u1", [{"id": "dup", "url": "second"}])
    assert storage.seen_job_ids("u1") == {"dup"}  # not duplicated
