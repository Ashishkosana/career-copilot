"""Daily cron handler: build adapters from Settings and run the briefing.

Invoked on a schedule (EventBridge). It wires the real adapters, runs
:class:`DailyBriefingService`, and returns a compact JSON summary for logs/tests.
The wiring is split from the transport so tests can drive :func:`run_briefing`
and :func:`briefing_response` with in-memory fakes — no cloud, no network.

**No API key is required.** Job supply is plain HTTP against public ATS boards
and the gates are regex; interpreting an ambiguous posting is a separate, cached
path (``ports.interpreter``). Reply drafting is the only LLM call and degrades to
"no draft" when the key is absent, so a keyless deploy still produces real supply.

What this handler used to do — build the service from ``JaJobSource``, pointed at
a ``ja`` SQLite database that exists on no machine, and therefore serve a bundled
4-row fixture of invented companies as real matches — is gone, along with the
adapter. The summary below exists so that failure mode cannot come back quietly:
every count is reported, and a run that fetched nothing says so.
"""
from __future__ import annotations

from typing import Any

from copilot.adapters.ats import WatchlistPostingSource, load_watchlist
from copilot.adapters.dynamodb_store import DynamoDbStore
from copilot.adapters.gmail_mailbox import GmailMailbox
from copilot.adapters.llm_reply import LlmReplyDrafter
from copilot.adapters.ssm_secrets import AwsSecrets
from copilot.config import Settings, load_settings
from copilot.handlers.worklist_api import build_store as build_posting_store
from copilot.handlers.worklist_api import load_resume_text
from copilot.logging import get_logger
from copilot.services.daily_briefing import DailyBriefingService, DailyRun

log = get_logger("copilot.handlers.cron")


def build_source(settings: Settings) -> WatchlistPostingSource:
    """The real job supply: every board on the watchlist, fanned out.

    Concurrency bounding and per-source isolation are the source's own guarantees
    and are not re-implemented here — ``fetch_workers`` caps the sweep because
    these are other people's public job boards, and a dead board is contained and
    counted rather than raised.
    """
    entries = load_watchlist(settings.watchlist_path)
    if not entries:
        # The one failure mode that produced the fixture bug: no source configured,
        # reading downstream as "no roles today". Say it out loud.
        log.warning(
            "empty_watchlist", extra={"extra_fields": {"path": str(settings.watchlist_path)}}
        )
    return WatchlistPostingSource(
        entries, search_text=settings.search_text, max_workers=settings.fetch_workers
    )


def build_service(settings: Settings) -> DailyBriefingService:
    """Assemble the service from real adapters (mypy verifies port contracts).

    The posting store is built by ``worklist_api.build_store`` on purpose: the cron
    writes the corpus the worklist API reads, and one shared factory is what keeps
    them from drifting onto different stores.

    **One** :class:`AwsSecrets` for the whole invocation, passed to every consumer
    that might need a credential. Per-instance rather than module-level because the
    resolver caches what it reads and a warm Lambda container lives for hours: a
    module-level cache would keep serving a key that has since been rotated. Sharing
    it across the adapters *within* one run is the other half of the same trade — the
    inbox and the reply drafter then cost one lookup each per invocation, not one per
    call.

    Passing the port is what makes the credential tiers real. Until it was wired
    here, ``adapters/ssm_secrets.py`` existed, the stack granted the cron
    ``ssm:GetParameter`` on both key paths and set ``COPILOT_*_SECRET_ID`` to them,
    and the runtime still read neither: every consumer was constructed without a
    resolver, so ``inbox_ok`` could only ever be false and no reply could ever be
    drafted. A granted permission that nothing exercises looks identical to a
    working feature from the console, which is why this is asserted in
    ``tests/test_handlers.py`` rather than left to a reading of this function.

    Keyless stays the default and stays whole: every lookup below degrades to ``""``
    or ``None`` rather than raising, so with no parameters created and no credentials
    at all the supply half still fetches, screens and scores.
    """
    secrets = AwsSecrets(region=settings.aws_region)
    return DailyBriefingService(
        mailbox=GmailMailbox(secrets=secrets, secret_id=settings.gmail_secret_id),
        store=DynamoDbStore(settings.table_name, region=settings.aws_region),
        postings=build_source(settings),
        posting_store=build_posting_store(settings),
        llm=LlmReplyDrafter(
            api_key=settings.llm_api_key,
            secrets=secrets,
            secret_id=settings.llm_secret_id,
        ),
        resume_text=load_resume_text(settings),
        max_jobs=settings.max_jobs,
    )


def run_briefing(service: DailyBriefingService, settings: Settings) -> DailyRun:
    """Run the daily pipeline for the configured owner."""
    return service.run(
        user_id=settings.owner_user_id,
        my_email=settings.my_email or None,
    )


def briefing_response(run: DailyRun) -> dict[str, Any]:
    """Shape a compact JSON-safe summary of a run (pure).

    Every count is present on every run, because the only way a shrinking corpus
    stays invisible is if the numbers are conditional: ``fetched`` with
    ``sources_ok`` distinguishes "no roles today" from "no source answered",
    ``kept`` is the full applicable list rather than the ``jobs`` slice that fit in
    the briefing, and ``closed`` says how much of the corpus this run retired.

    ``inbox_ok`` is unconditional for the same reason: the mailbox is the one
    dependency this deploy has no credential for yet, and ``scanned: 0`` means
    "quiet morning" or "Gmail refused us" depending on it. A metric filter on
    ``inbox_ok`` is the natural companion alarm to the supply ones.

    The screening-view counts are here for exactly the same reason. The public read
    API now serves a view the cron writes, so a screen that silently produced
    nothing is a blank website with a green cron — the 2026 version of the fixture
    bug. ``screened`` says how much of the corpus the pass covered, ``eligible`` and
    ``internships`` are the two collections the page renders, and ``view_rows`` says
    how much was actually written: a run with ``screened: 47538`` and
    ``view_rows: 0`` screened fine and failed to publish, which is a different fault
    with a different fix. All four are unconditional, so a metric filter can alarm
    on ``eligible`` dropping without having to guess whether the key exists.

    The four extra keys appear only when something is wrong, so a healthy summary
    stays small: ``close_skipped`` names the reason nothing was closed,
    ``screen_skipped`` names the reason the view was not published,
    ``failed_sources`` lists the boards that did not answer, and ``inbox_error``
    names the mailbox failure.
    """
    supply = run.supply
    summary: dict[str, Any] = {
        "ok": True,
        "day": run.briefing.day.isoformat(),
        # inbox
        "inbox_ok": run.inbox_ok,
        "scanned": run.briefing.scanned,
        "needs_action": len(run.briefing.needs_action),
        "drafts": run.drafts,
        # supply
        "fetched": supply.fetched,
        "new": supply.new,
        "closed": supply.closed,
        "kept": supply.kept,
        "excluded": supply.excluded,
        "sources_ok": supply.sources_ok,
        "sources_failed": supply.sources_failed,
        # the materialised screening view the read API serves
        "screened": supply.screened,
        "eligible": supply.eligible,
        "internships": supply.internships,
        "view_rows": supply.view_rows,
        # what reached the briefing
        "jobs": len(run.briefing.jobs),
        "scored": run.scored,
    }
    if supply.close_skipped:
        summary["close_skipped"] = supply.close_skipped
    if supply.screen_skipped:
        summary["screen_skipped"] = supply.screen_skipped
    if supply.failed_sources:
        summary["failed_sources"] = list(supply.failed_sources)
    if run.inbox_error:
        summary["inbox_error"] = run.inbox_error
    return summary


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Lambda entrypoint for the scheduled daily run."""
    settings = load_settings()
    service = build_service(settings)
    run = run_briefing(service, settings)
    response = briefing_response(run)
    log.info("cron_complete", extra={"extra_fields": response})
    return response
