"""Daily cron handler: build adapters from Settings and run the briefing.

Invoked on a schedule (EventBridge). It wires the real adapters, runs
:class:`DailyBriefingService`, and returns a small JSON summary for logs/tests.
The wiring is split from the transport so tests can drive :func:`run_briefing`
and :func:`briefing_response` with in-memory fakes — no cloud, no network.
"""
from __future__ import annotations

from typing import Any

from copilot.adapters.dynamodb_store import DynamoDbStore
from copilot.adapters.gmail_mailbox import GmailMailbox
from copilot.adapters.ja_jobsource import JaJobSource
from copilot.adapters.llm_reply import LlmReplyDrafter
from copilot.config import Settings, load_settings
from copilot.domain.models import Briefing
from copilot.logging import get_logger
from copilot.services.daily_briefing import DailyBriefingService

log = get_logger("copilot.handlers.cron")


def build_service(settings: Settings) -> DailyBriefingService:
    """Assemble the service from real adapters (mypy verifies port contracts)."""
    return DailyBriefingService(
        mailbox=GmailMailbox(),
        jobs=JaJobSource(settings.ja_db_path),
        llm=LlmReplyDrafter(api_key=settings.llm_api_key),
        store=DynamoDbStore(settings.table_name, region=settings.aws_region),
        min_score=settings.min_job_score,
        max_jobs=settings.max_jobs,
    )


def run_briefing(service: DailyBriefingService, settings: Settings) -> Briefing:
    """Run the daily briefing for the configured owner."""
    return service.run(
        user_id=settings.owner_user_id,
        my_email=settings.my_email or None,
    )


def briefing_response(briefing: Briefing) -> dict[str, Any]:
    """Shape a compact JSON-safe summary of a run (pure)."""
    return {
        "ok": True,
        "day": briefing.day.isoformat(),
        "scanned": briefing.scanned,
        "needs_action": len(briefing.needs_action),
        "jobs": len(briefing.jobs),
    }


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Lambda entrypoint for the scheduled daily briefing."""
    settings = load_settings()
    service = build_service(settings)
    briefing = run_briefing(service, settings)
    response = briefing_response(briefing)
    log.info("cron_complete", extra={"extra_fields": response})
    return response
