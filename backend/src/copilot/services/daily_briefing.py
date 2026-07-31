"""The daily-briefing orchestration — the product's spine.

Two independent halves run here, and they fail independently:

* **Inbox** — fetch → triage → draft replies. Replies are created as DRAFTS,
  never auto-sent; a draft that fails to generate is skipped, not raised.
* **Supply** — fetch the watchlist → sync to the posting store → close what
  vanished → screen the whole stored corpus once → **publish that screening as a
  materialised view** → score the handful of roles that reach the digest.

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

**The screen belongs here, once a day, and nowhere else.** It used to run again on
every public read, over the whole corpus, because the funnel counts describe the
whole set. Measured: 2.2 s to read 25,294 rows, 38.1 s to screen and
shape them (1.506 ms a posting) — i.e. ~72 s at the deployed 47,538 — against a
29 s API Gateway ceiling, so every single read 504'd, ``?limit=1`` included.

The cron has 900 s and the board sweep uses ~426 s of it. The screen is ~72 s, the
corpus read ~10 s over the wire, and publishing ~84,900 rows is ~3,400
BatchWriteItem requests, so the run lands near 600 s — it fits, with margin, and it
turns a read into an O(page) lookup (8.7 ms measured: summary + page + hydrate). See
:func:`build_screening_view` and :mod:`copilot.ports.postingstore`.

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
from copilot.domain.screening import (
    Exclusion,
    ScreenDecision,
    ScreenReport,
    is_internship,
    screen,
)
from copilot.domain.seniority import JUNIOR_BANDS, Level, LevelSource
from copilot.domain.triage import triage_all
from copilot.logging import get_logger
from copilot.ports import LLMPort, MailboxPort, StorePort
from copilot.ports.postingsource import PostingSourcePort
from copilot.ports.postingstore import (
    VIEW_INTERNSHIPS,
    VIEW_KEPT,
    PostingStorePort,
    ScreenedRow,
    ScreenSummary,
    cap_quote,
)

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


# ---------------------------------------------------------------------------
# The materialised screening view
# ---------------------------------------------------------------------------

#: Which eligibility evidence key explains which gate. Same mapping the read API
#: uses; the gates whose evidence is not in ``Eligibility`` are handled by name.
_ELIGIBILITY_EVIDENCE_KEY: dict[Exclusion, str] = {
    Exclusion.CLEARANCE: "clearance",
    Exclusion.CITIZENSHIP: "citizenship",
    Exclusion.NO_SPONSORSHIP: "no_sponsorship",
}


@dataclass(frozen=True)
class ScreeningView:
    """One screening pass over the stored corpus, ready to publish and to read.

    Carries both shapes on purpose: the decisions the digest needs today, and the
    rows/summary the read path serves for the next 24 hours. They come from **one**
    pass, which is the property that makes the funnel counts and the evidence shown
    on a card describe the same screening.
    """

    kept: tuple[ScreenDecision, ...]
    internships: tuple[ScreenDecision, ...]
    report: ScreenReport
    #: Ready to publish, with one exception: :attr:`ScreenedRow.first_seen` is left
    #: unset for the store to stamp. See :func:`_row`.
    rows: tuple[ScreenedRow, ...]
    summary: ScreenSummary


def _quote_for(decision: ScreenDecision, gate: Exclusion) -> str:
    """The text that tripped one specific gate, capped.

    Addressed per gate rather than taken from the first exclusion: a posting
    routinely fails several gates at once, and a grouped view must show the
    evidence for the gate it is displaying.
    """
    posting = decision.posting
    if gate is Exclusion.NOT_SWE:
        return cap_quote(posting.title)
    if gate is Exclusion.INTERNSHIP:
        # The two signals disagree often — 27 postings carry it only in the title
        # and 22 only in the ATS employment type — so quote whichever fired.
        return cap_quote(
            posting.title if is_internship(posting.title) else posting.employment_type
        )
    if gate is Exclusion.LEVEL:
        verdict = decision.level_verdict
        return cap_quote(verdict.evidence) if verdict is not None else ""
    key = _ELIGIBILITY_EVIDENCE_KEY.get(gate)
    if key is None:
        # DEMO_BOARD has no quotable phrase: the evidence is the tenant slug, and
        # ``reason`` already names it. An empty quote is the honest answer, not a
        # missing one.
        return ""
    return cap_quote(dict(decision.eligibility.evidence).get(key, ""))


def _row(decision: ScreenDecision, *, view: str, gate: Exclusion | None) -> ScreenedRow:
    """One (posting, view) row from one screening decision.

    ``first_seen`` is **not** set here, and that is the one field on the row this
    function must not touch. When we first saw a posting is storage history — it is a
    column on the stored row, deliberately absent from ``Posting`` — so
    ``PostingStorePort.save_screening`` stamps it from the corpus it owns. Setting it
    here would mean either handing this pure pass a map of storage timestamps it has no
    other use for, or stamping the run's own clock: the second is the dangerous one,
    because it would report all 2,569 kept roles as first seen today and turn a
    "358 new" morning into "everything is new", which is precisely the amnesia the
    store exists to fix.
    """
    posting = decision.posting
    verdict = decision.level_verdict
    return ScreenedRow(
        posting_id=posting.id,
        view=view,
        posted_at=posting.posted_at,
        kept=decision.kept,
        level=decision.level.value,
        level_source=verdict.source.value if verdict is not None else LevelSource.NONE.value,
        level_why=verdict.explain() if verdict is not None else "",
        eligibility_checked=decision.eligibility.checked,
        sponsorship=decision.eligibility.sponsorship.value,
        gate="" if gate is None else gate.value,
        reason="" if gate is None else decision.reason_for(gate),
        quote="" if gate is None else _quote_for(decision, gate),
    )


def _internship_collection(
    excluded: Sequence[ScreenDecision], *, wanted: frozenset[Level]
) -> list[ScreenDecision]:
    """The internships population: re-screened with the preference flipped.

    Measured on the live corpus: 318 postings hit the internship gate and **48**
    come out of here. The other 270 fail a gate that has nothing to do with being
    an internship — 264 are not software roles ("Marketing Intern", "Finance
    Co-op"), 12 are ATS vendor demo fixtures, the rest want a clearance or a
    citizenship. Publishing all 318 under an "internships" heading would put a
    vendor's invented roles and a marketing job on the page.

    Derived as a second pass over the postings the internship gate removed, rather
    than by running the whole funnel with ``include_internships=True``: the primary
    pass is the one whose numbers are published, and flipping the flag there would
    quietly restate "813 kept of 47,538" as "1,131 kept".

    ``screen(..., include_internships=True)`` is the mechanism deliberately, not a
    bespoke "is this an internship" check — that is what guarantees every *other*
    gate still applies. A second mechanism would have to re-implement all of them.
    """
    kept: list[ScreenDecision] = []
    for decision in excluded:
        if Exclusion.INTERNSHIP not in decision.exclusions:
            continue
        allowed = screen(decision.posting, wanted=wanted, include_internships=True)
        if allowed.kept:
            kept.append(allowed)
    return kept


def build_screening_view(
    postings: Sequence[Posting],
    *,
    now: datetime,
    wanted: frozenset[Level] = JUNIOR_BANDS,
) -> ScreeningView:
    """Screen a corpus once and shape the result into publishable rows.

    Pure: no I/O, no clock, no store. That is what lets the whole materialisation
    be asserted against a list of postings instead of against a table.

    ``screening.screen_all`` is deliberately not used. It sorts kept rows with a key
    that mixes an aware epoch with whatever tzinfo a posting carries, so one naive
    ``posted_at`` in the store raises ``TypeError`` mid-run — and here the ordering
    is not needed at all, because the recency order lives in
    :attr:`ScreenedRow.sort_key` and is applied by the store's range query. Sorting
    47,538 decisions to then throw the order away would be the third place in this
    codebase to re-derive it.

    One row per (posting, view): kept postings under :data:`VIEW_KEPT`, software
    internships under :data:`VIEW_INTERNSHIPS`, and excluded postings under **every**
    gate they failed — which is why ``len(rows)`` exceeds ``len(postings)``.
    """
    report = ScreenReport()
    kept: list[ScreenDecision] = []
    excluded: list[ScreenDecision] = []
    rows: list[ScreenedRow] = []
    for posting in postings:
        decision = screen(posting, wanted=wanted)
        report.note(decision)
        if decision.kept:
            kept.append(decision)
            rows.append(_row(decision, view=VIEW_KEPT, gate=None))
            continue
        excluded.append(decision)
        rows.extend(
            _row(decision, view=gate.value, gate=gate) for gate in decision.exclusions
        )

    internships = _internship_collection(excluded, wanted=wanted)
    rows.extend(_row(d, view=VIEW_INTERNSHIPS, gate=None) for d in internships)

    summary = ScreenSummary(
        # Microseconds are in the generation on purpose: two runs inside one second
        # would otherwise write into the same partitions, mixing two passes into one
        # view — which is precisely the half-written state the generation exists to
        # make impossible.
        generation=now.astimezone(UTC).strftime("%Y%m%dT%H%M%S.%fZ"),
        screened_at=now,
        corpus_size=len(postings),
        screened=report.total,
        kept=report.kept,
        excluded=report.excluded,
        gates=dict(report.by_exclusion),
        needs_level_check=report.needs_llm,
        eligible_total=len(kept),
        internship_total=len(internships),
    )
    return ScreeningView(
        kept=tuple(kept),
        internships=tuple(internships),
        report=report,
        rows=tuple(rows),
        summary=summary,
    )


@dataclass(frozen=True)
class SupplySummary:
    """What the supply half did, in counts. Shrinkage is visible or it is a bug.

    ``kept`` is the whole applicable list, not the slice that reached the briefing:
    a ``max_jobs`` cap must never read as "that was all there was". ``excluded`` is
    ``total - kept``, **not** the sum of the per-gate counts, which overcount by
    design because a role fails several gates at once.

    ``fetched`` and ``screened`` are different numbers and both are reported.
    ``fetched`` is what the boards returned this morning; ``screened`` is the open
    corpus the view was built over, which is larger whenever closing was skipped and
    smaller than the raw fetch because the fetch is deduped on the way in. A run
    where they diverge wildly is a run worth looking at, and that is only visible if
    both are present.
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
    # --- the materialised screening view -------------------------------------
    #: Open postings the screen covered. 0 with a non-empty corpus means the screen
    #: did not run, which is why it is reported rather than inferred from ``kept``.
    screened: int = 0
    #: Size of the two published collections — what the worklist page will show.
    eligible: int = 0
    internships: int = 0
    #: Rows written to the view. 1.79 per posting measured, because an excluded one is
    #: filed under every gate it failed. 0 alongside a non-zero ``screened`` means
    #: the screen ran and the publish did not.
    view_rows: int = 0
    #: ``""`` when the view was published, otherwise why it was not.
    screen_skipped: str = ""

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
        """Inbox: fetch → triage → draft. Supply: fetch → sync → close → screen → publish.

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
                "screened": supply.screened,
                "eligible": supply.eligible,
                "internships": supply.internships,
                "view_rows": supply.view_rows,
                "screen_skipped": supply.screen_skipped,
            }},
        )
        return run

    # --- supply ---------------------------------------------------------------

    def _refresh_supply(
        self, *, now: datetime
    ) -> tuple[list[ScreenDecision], list[str], SupplySummary]:
        """Fetch → sync → close → screen the corpus → publish. Returns the kept set.

        The whole fetch is synced, not just the survivors: the store is the corpus
        the worklist API reads, and its ``/excluded`` trust surface can only quote a
        rejection reason for a posting it actually has.

        **The screen runs after the sync, over the stored corpus, not over the
        fetch.** Three reasons, and all three were bugs waiting:

        * The store *merges* descriptions — a Workday row that ships none keeps the
          text Greenhouse gave it yesterday. Screening the fetch would judge the
          Workday version and publish a verdict the read path could never reproduce
          from what it actually serves.
        * When closing is skipped (a degraded sweep) the open corpus is larger than
          the fetch. The page shows open postings, so the view has to cover them.
        * It is one screening pass instead of two. The old code screened the fetch
          here and the read path screened the corpus again on every request; at
          ~1.5 ms a posting, doing it twice in the cron would cost a needless ~70 s.
        """
        # Concurrency bounding and per-source isolation live in the source itself
        # (``WatchlistPostingSource``), so one dead board cannot sink the run and a
        # sweep of several hundred public boards stays polite.
        fetched = list(self.postings.fetch())
        health = read_source_health(self.postings)

        new_ids, known_ids = self.posting_store.sync(fetched, now=now)
        closed, close_skipped = self._close_vanished(fetched, now=now, health=health)
        view, screen_skipped = self._publish_screening(now=now)

        summary = view.summary
        return list(view.kept), new_ids, SupplySummary(
            fetched=len(fetched),
            new=len(new_ids),
            known=len(known_ids),
            closed=closed,
            kept=summary.kept,
            excluded=summary.excluded,
            sources_ok=health.ok,
            failed_sources=health.failed[:MAX_REPORTED_FAILURES],
            close_skipped=close_skipped,
            screened=summary.screened,
            eligible=summary.eligible_total,
            internships=summary.internship_total,
            view_rows=len(view.rows) if not screen_skipped else 0,
            screen_skipped=screen_skipped,
        )

    def _publish_screening(self, *, now: datetime) -> tuple[ScreeningView, str]:
        """Screen the stored corpus and publish it. Returns ``(view, skipped_reason)``.

        Contained rather than raising, and the containment is the same argument the
        inbox half makes: by this point the corpus is already synced, so a failure
        here must not cost a day of history, and an EventBridge retry would refetch
        several hundred boards to re-attempt a CPU-bound pass.

        There is deliberately **no fallback to screening the fetch** for the digest.
        A fallback would produce counts from a pass that was never published, so the
        numbers in the morning email would disagree with the numbers on the page —
        which is the exact class of "two sources of truth for one funnel" bug this
        view exists to remove. A run that could not screen reports ``kept: 0`` with
        ``screen_skipped`` naming the reason, and the previously published view stays
        current until tomorrow.
        """
        try:
            corpus = self.posting_store.open_postings()
            view = build_screening_view(corpus, now=now, wanted=self.wanted_levels)
            # Rows first, summary last — the store's contract, not this caller's
            # choice. Its presence is what makes the new view readable at all.
            self.posting_store.save_screening(view.rows, summary=view.summary)
        except Exception as exc:
            self.log.warning("screening_view_publish_failed", exc_info=True)
            return build_screening_view([], now=now), _error_label(exc)
        self.log.info(
            "screening_view_published",
            extra={"extra_fields": {
                "screened": view.summary.screened,
                "eligible": view.summary.eligible_total,
                "internships": view.summary.internship_total,
                "rows": len(view.rows),
                "generation": view.summary.generation,
            }},
        )
        return view, ""

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
