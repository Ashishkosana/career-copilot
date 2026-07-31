"""The daily-briefing orchestration — the product's spine.

Two independent halves run here, and they fail independently:

* **Inbox** — fetch → triage → draft replies. Replies are created as DRAFTS,
  never auto-sent; a draft that fails to generate is skipped, not raised.
* **Supply** — fetch the watchlist → screen → sync to the posting store → close
  what vanished → score what survived.

"Independently" is load-bearing and used not to be. ``run`` called
``mailbox.fetch_recent`` as its first statement, and ``GmailMailbox`` with no
credentials — which is how ``handlers.cron`` builds it, because no code fetches
the Gmail secret yet — raises there. So *every* scheduled run died before a single
board was fetched: 25k postings of working, keyless supply held hostage by an
OAuth token. The inbox half is now contained, and :attr:`DailyRun.inbox_ok` says
so, because ``scanned: 0`` from a broken mailbox and ``scanned: 0`` from a quiet
morning must not be the same log line.

The supply half used to read a ``ja`` SQLite tracker that exists on no machine,
so it always fell through to a bundled 4-row fixture and the briefing rendered
invented companies as real matches. It now reads real ATS boards, and the run
summary carries every count, so "no roles today" can never again be
indistinguishable from "no source configured".

**No LLM is required for supply.** Interpreting an ambiguous posting is a
separate, cached concern (``ports.interpreter``), so a daily run needs no API
key: the fetch is plain HTTP against public boards and the gates are regex.
Reply drafting is the only LLM call, ``llm`` is optional, and a drafter with no
key returns ``""`` — the run still completes.

Everything is behind port Protocols, so the same service runs against real cloud
adapters or in-memory fakes with no network.
"""
from __future__ import annotations

import logging
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

from copilot.domain.briefing import build_briefing, render_markdown
from copilot.domain.gap import build_report, score_report
from copilot.domain.models import Briefing, Email, Job, TriagedEmail
from copilot.domain.posting import Posting
from copilot.domain.screening import ScreenDecision, ScreenReport, screen
from copilot.domain.seniority import JUNIOR_BANDS, Level, LevelSource
from copilot.domain.triage import triage_all
from copilot.logging import get_logger
from copilot.ports import LLMPort, MailboxPort, StorePort
from copilot.ports.postingsource import PostingSourcePort
from copilot.ports.postingstore import PostingStorePort

#: Sorts undated postings last without dropping them.
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

#: Above this share of failed boards, "absent from the fetch" stops meaning
#: "closed". A sweep that lost a quarter of its sources is a broken run, not a
#: morning on which every role at those companies vanished — closing them would
#: delete real inventory that reappears tomorrow. Same reasoning as the stores'
#: refusal to mass-close on an *empty* fetch, applied one step earlier.
MAX_FAILED_SOURCE_SHARE = 0.25

#: Failure labels carried in the summary: enough to name the pattern, not so many
#: that one log line becomes a list of 400 boards.
MAX_REPORTED_FAILURES = 10


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _posted_key(decision: ScreenDecision) -> datetime:
    """Recency sort key that survives a naive ``posted_at``.

    Comparing an aware epoch against a naive timestamp raises ``TypeError``, which
    would take a whole run down for one odd row, so tzinfo is coerced here rather
    than trusted.
    """
    posted = decision.posting.posted_at
    if posted is None:
        return _EPOCH
    return posted if posted.tzinfo else posted.replace(tzinfo=UTC)


@dataclass(frozen=True)
class SourceHealth:
    """How much of the fan-out actually answered."""

    ok: int = 0
    failed: tuple[str, ...] = ()

    @property
    def attempted(self) -> int:
        return self.ok + len(self.failed)

    @property
    def degraded(self) -> bool:
        """True when too little of the watchlist answered to read absence as closure."""
        if not self.attempted:
            return False
        return len(self.failed) / self.attempted > MAX_FAILED_SOURCE_SHARE


def read_source_health(source: object) -> SourceHealth:
    """Best-effort read of a fan-out source's own report.

    Read structurally rather than by importing the adapter: ``PostingSourcePort``
    promises only ``fetch()``, and the service must not learn which adapter it is
    holding. ``WatchlistPostingSource`` publishes ``.report`` after every fetch; a
    source that publishes nothing reads as healthy, which is the right reading of
    "no failures were recorded".
    """
    report = getattr(source, "report", None)
    if report is None:
        return SourceHealth()
    ok = getattr(report, "ok_sources", 0)
    raw_failures = getattr(report, "failed_sources", ())
    failed: tuple[str, ...] = ()
    if isinstance(raw_failures, list | tuple):
        failed = tuple(_failure_label(entry) for entry in raw_failures)
    return SourceHealth(ok=ok if isinstance(ok, int) else 0, failed=failed)


def _failure_label(entry: object) -> str:
    """``(label, error)`` pairs collapse to the label; anything else stringifies."""
    if isinstance(entry, list | tuple) and entry:
        return str(entry[0])
    return str(entry)


def _error_label(exc: BaseException) -> str:
    """``"TypeName: message"``, truncated, and never empty.

    The type is kept because several of these carry no message at all (a bare
    ``ConnectionResetError``), and ``inbox_error: ""`` would then read as "no
    failure" while ``inbox_ok`` said otherwise.
    """
    message = f"{type(exc).__name__}: {exc}".strip().rstrip(":").strip()
    return message[:MAX_ERROR_CHARS]


@dataclass(frozen=True)
class SupplySummary:
    """What the supply half did, in counts. Shrinkage is visible or it is a bug.

    ``kept`` is the whole applicable list, not the slice that reached the briefing:
    a ``max_jobs`` cap must never read as "that was all there was". ``excluded`` is
    ``total - kept``, **not** the sum of the per-gate counts, which overcount by
    design because a role fails several gates at once.
    """

    fetched: int = 0
    new: int = 0
    known: int = 0
    closed: int = 0
    kept: int = 0
    excluded: int = 0
    sources_ok: int = 0
    failed_sources: tuple[str, ...] = ()
    #: Why closing was skipped: ``""``, ``"empty_fetch"`` or ``"degraded_fetch"``.
    close_skipped: str = ""

    @property
    def sources_failed(self) -> int:
        return len(self.failed_sources)


#: Inbox errors are carried into a log line, so the message is truncated rather
#: than allowed to paste a whole OAuth failure into CloudWatch.
MAX_ERROR_CHARS = 200


@dataclass(frozen=True)
class DailyRun:
    """One daily run: the briefing, plus how it was arrived at."""

    briefing: Briefing
    supply: SupplySummary
    drafts: int = 0
    #: False when no résumé is configured, so a 0 reads as "not scored" rather
    #: than "scored zero".
    scored: bool = False
    #: False when any part of the inbox half failed. Distinguishes an empty
    #: mailbox from an unreachable one — the same reasoning as ``sources_ok``
    #: for supply. Never inferred from ``scanned``, which is 0 in both cases.
    inbox_ok: bool = True
    #: ``"<ExceptionType>: <message>"`` for the first inbox failure, truncated.
    inbox_error: str = ""


@dataclass
class DailyBriefingService:
    mailbox: MailboxPort
    store: StorePort
    postings: PostingSourcePort
    posting_store: PostingStorePort
    #: Reply drafting only. ``None`` — or a drafter with no key — means no drafts.
    llm: LLMPort | None = None
    #: Résumé text for coverage scoring. Empty means "do not pretend to score".
    resume_text: str = ""
    max_jobs: int = 8
    wanted_levels: frozenset[Level] = JUNIOR_BANDS
    now: Callable[[], datetime] = _utcnow
    log: logging.Logger = field(
        default_factory=lambda: get_logger("copilot.services.daily_briefing")
    )

    @property
    def scoring_available(self) -> bool:
        return bool(self.resume_text.strip())

    def run(
        self,
        *,
        user_id: str,
        my_email: str | None = None,
        query: str = "newer_than:2d",
        max_emails: int = 50,
        draft_replies: bool = True,
    ) -> DailyRun:
        """Inbox: fetch → triage → draft. Supply: fetch → screen → sync → close → score.

        The supply half runs even when the mailbox is unreachable. It is ordered
        *after* the inbox fetch only because the briefing needs both; nothing in it
        depends on an email, so a credential the deploy does not have yet must not
        cost a day of corpus history.
        """
        now = self.now()
        failures: list[str] = []
        emails = self._fetch_inbox(query=query, max_results=max_emails, failures=failures)
        triaged = triage_all(emails)

        kept, new_ids, supply = self._refresh_supply(now=now)
        jobs = self._todays_jobs(kept, new_ids=new_ids)
        if jobs:
            self.store.save_jobs(user_id, jobs)

        briefing = build_briefing(triaged, jobs, now=now)
        self.store.save_briefing(user_id, briefing)

        drafts = self._draft_replies(triaged, failures=failures) if draft_replies else 0

        if my_email:
            self._email_briefing(briefing, to=my_email, failures=failures)

        run = DailyRun(
            briefing=briefing,
            supply=supply,
            drafts=drafts,
            scored=self.scoring_available,
            inbox_ok=not failures,
            inbox_error=failures[0] if failures else "",
        )
        self.log.info(
            "daily_briefing_complete",
            extra={"extra_fields": {
                "user_id": user_id,
                "needs_action": len(briefing.needs_action),
                "jobs": len(briefing.jobs),
                "scanned": briefing.scanned,
                "drafts": drafts,
                "inbox_ok": run.inbox_ok,
                "inbox_error": run.inbox_error,
                "fetched": supply.fetched,
                "new": supply.new,
                "closed": supply.closed,
                "kept": supply.kept,
                "sources_failed": supply.sources_failed,
                "close_skipped": supply.close_skipped,
            }},
        )
        return run

    # --- supply ---------------------------------------------------------------

    def _refresh_supply(
        self, *, now: datetime
    ) -> tuple[list[ScreenDecision], list[str], SupplySummary]:
        """Fetch → screen → sync → close. Returns ``(kept, new_ids, summary)``.

        The whole fetch is synced, not just the survivors: the store is the corpus
        the worklist API screens on read, and its ``/excluded`` trust surface can
        only quote a rejection reason for a posting it actually has.
        """
        # Concurrency bounding and per-source isolation live in the source itself
        # (``WatchlistPostingSource``), so one dead board cannot sink the run and a
        # sweep of several hundred public boards stays polite.
        fetched = list(self.postings.fetch())
        health = read_source_health(self.postings)

        kept, report = self._screen(fetched)
        new_ids, known_ids = self.posting_store.sync(fetched, now=now)
        closed, close_skipped = self._close_vanished(fetched, now=now, health=health)

        return kept, new_ids, SupplySummary(
            fetched=len(fetched),
            new=len(new_ids),
            known=len(known_ids),
            closed=closed,
            kept=report.kept,
            excluded=report.excluded,
            sources_ok=health.ok,
            failed_sources=health.failed[:MAX_REPORTED_FAILURES],
            close_skipped=close_skipped,
        )

    def _screen(self, postings: Sequence[Posting]) -> tuple[list[ScreenDecision], ScreenReport]:
        """Run the funnel, keeping the survivors and the counts.

        ``screening.screen_all`` is deliberately not used: it sorts kept rows with a
        key that mixes an aware epoch with whatever tzinfo a posting carries, and it
        materialises the ~24k excluded decisions this path never reads. Same
        reasoning as ``worklist_api.build_index``.
        """
        report = ScreenReport()
        kept: list[ScreenDecision] = []
        for posting in postings:
            decision = screen(posting, wanted=self.wanted_levels)
            report.note(decision)
            if decision.kept:
                kept.append(decision)
        return kept, report

    def _close_vanished(
        self, fetched: Sequence[Posting], *, now: datetime, health: SourceHealth
    ) -> tuple[int, str]:
        """Close what stopped appearing — unless this fetch cannot be trusted to say so."""
        if not fetched:
            self.log.warning("close_skipped_empty_fetch")
            return 0, "empty_fetch"
        if health.degraded:
            self.log.warning(
                "close_skipped_degraded_fetch",
                extra={"extra_fields": {
                    "sources_ok": health.ok,
                    "sources_failed": len(health.failed),
                    "failed": list(health.failed[:MAX_REPORTED_FAILURES]),
                }},
            )
            return 0, "degraded_fetch"
        closed = self.posting_store.close_missing(now=now, seen_ids={p.id for p in fetched})
        return closed, ""

    # --- what reaches the briefing -------------------------------------------

    def _todays_jobs(
        self, kept: Sequence[ScreenDecision], *, new_ids: Collection[str]
    ) -> list[Job]:
        """The applicable roles that are *new* this run, newest first.

        Two deliberate choices. **New, not merely open**: the store exists so the
        digest can say what changed, and re-listing 880 standing roles every
        morning is the amnesia it was built to fix. **Recency, not score**: at a
        0.66% base rate the question is "can I apply to this", not "which of these
        is best" (see ``domain.screening``). Anything past the cap is still in the
        store and on the worklist API, and ``SupplySummary.kept`` reports the full
        count so the cap is never mistaken for the supply.
        """
        fresh_ids = set(new_ids)
        fresh = [d for d in kept if d.posting.id in fresh_ids]
        fresh.sort(key=_posted_key, reverse=True)
        return [self._as_job(d) for d in fresh[: self.max_jobs]]

    def _as_job(self, decision: ScreenDecision) -> Job:
        posting = decision.posting
        return Job(
            id=posting.id,
            title=posting.title,
            company=posting.company,
            url=posting.url,
            location=posting.location,
            score=self._score(decision),
        )

    def _score(self, decision: ScreenDecision) -> int:
        """Coverage of the requirements *this posting names*, or 0 when unscored.

        With no résumé configured every requirement would read as "missing" and
        every role would carry a confident 0, so nothing is scored at all and
        ``DailyRun.scored`` says which happened — the same degradation shape as
        ``worklist_api.Scorer``.
        """
        if not self.scoring_available:
            return 0
        posting = decision.posting
        report = build_report(
            title=posting.title,
            company=posting.company,
            url=posting.url,
            # An unavailable description must not be passed off as prose we read.
            description=posting.description if posting.desc_available else "",
            resume_text=self.resume_text,
        )
        verdict = decision.level_verdict
        confirmed = verdict is not None and verdict.source is not LevelSource.NONE
        score = score_report(report, level_confirmed=confirmed).total
        return max(0, min(100, score))

    # --- inbox ----------------------------------------------------------------

    def _fetch_inbox(
        self, *, query: str, max_results: int, failures: list[str]
    ) -> list[Email]:
        """Read the mailbox, or record why not and return nothing.

        Broad ``except`` on purpose. The mailbox is a third-party OAuth API reached
        through a lazily imported SDK, so the failure surface is open-ended:
        ``RuntimeError`` for an unconfigured credential, ``HttpError`` for a revoked
        token, ``socket`` errors for a network blip, ``ImportError`` if the bundle
        shipped without the Google client. Enumerating those would be a list that
        goes stale, and every one of them has the same correct handling — the
        supply half still runs, and the run says the inbox did not.
        """
        try:
            return self.mailbox.fetch_recent(query=query, max_results=max_results)
        except Exception as exc:
            failures.append(_error_label(exc))
            self.log.warning("inbox_fetch_failed", exc_info=True)
            return []

    def _email_briefing(self, briefing: Briefing, *, to: str, failures: list[str]) -> None:
        """Send the owner their digest. A send failure must not lose the run.

        By this point the corpus is already synced and the briefing is already
        stored, so raising here would report a total failure for a run that did
        almost all of its work — and, worse, an EventBridge retry would redo the
        whole fetch to re-attempt one email.
        """
        try:
            self.mailbox.send(
                to=to,
                subject=f"Career Copilot — {briefing.day.isoformat()}",
                body=render_markdown(briefing),
            )
        except Exception as exc:
            failures.append(_error_label(exc))
            self.log.warning("briefing_email_failed", exc_info=True)

    def _draft_replies(self, triaged: list[TriagedEmail], *, failures: list[str]) -> int:
        """Draft a reply per needs-action email (DRAFTS only). One failure never sinks the run."""
        if self.llm is None:
            return 0
        created = 0
        for t in triaged:
            if not t.needs_action:
                continue
            try:
                body = self.llm.draft_reply(t.email)
            except Exception:
                self.log.warning(
                    "draft_reply_failed",
                    extra={"extra_fields": {"subject": t.email.subject}},
                    exc_info=True,
                )
                continue
            if not body.strip():
                continue
            try:
                self.mailbox.create_draft(
                    to=t.email.sender, subject=f"Re: {t.email.subject}", body=body
                )
            except Exception as exc:
                # Counted as an inbox failure, not swallowed: a run that reports
                # "3 drafts" when the mailbox rejected all three is a lie the user
                # only discovers by opening Gmail and finding nothing.
                failures.append(_error_label(exc))
                self.log.warning(
                    "create_draft_failed",
                    extra={"extra_fields": {"subject": t.email.subject}},
                    exc_info=True,
                )
                continue
            created += 1
        return created
