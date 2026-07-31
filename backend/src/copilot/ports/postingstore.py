"""The posting corpus, and the **materialised screening view** built over it.

Without this port the pipeline is amnesiac: it refetches ~47k postings, shows you
the same roles every day, cannot say what is *new*, cannot tell when a role
closes, and re-pays the LLM for descriptions it already read. Persistence is what
turns a fetch script into a daily product.

The second half of this module exists because of a measured production failure.
``handlers/worklist_api.py`` screened the **entire corpus on every request**:

    read 25,294 rows: 1.7 s    screen: 37.8 s    total 39.4 s

Screening costs a measured **1.506 ms per posting**, so at the deployed size —
47,538 open postings — that is ~72 s of pure CPU before the ~268 MB DynamoDB
page-through, against an API Gateway REST integration ceiling of **29 s, hard and
non-negotiable**. Every read 504'd, including ``?limit=1``,
because the limit is applied *after* screening: ``eligibleTotal`` and the funnel
counts describe the whole set, so the whole set had to be screened to produce
them. Memory peaked at 496 MB of 3008, so it was never a memory problem and no
amount of memory could fix it.

The fix is to stop re-deriving what the cron already computes. The split follows
what each half depends on, and that split is what decides the record shape:

* **Screening is résumé-independent.** Whether a posting is a software role, an
  internship, a vendor demo board, the wrong seniority band, or barred by
  clearance/citizenship/sponsorship is a fact about the *posting*. The cron
  already screens the whole corpus once a day, so it writes the answer down.
* **Scoring is résumé-dependent** and cheap per item — it runs over one page of
  ~25, not 47,538. It is computed at read time and deliberately **not** baked
  into the store: the résumé changes independently of the corpus, and a stale
  baked score would silently misrank without anything looking broken.

So a read becomes: read the summary (one item) → query one page of the wanted
view in recency order → hydrate that page → score that page → serve. Measured
end-to-end against the real 25,294-posting corpus on SQLite: **8.7 ms** for the
summary, a 25-row page and the hydrate, and 0.13 ms for a page of the largest
view (22,074 rows) — the page cost does not move with the size of the view, which
is the whole property. On DynamoDB the same read is a GetItem plus 16 bounded
shard queries plus ≤25 GetItems: ~200 ms of round trips, and it cannot approach
29 s at any corpus size.
"""
from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from copilot.domain.posting import Posting
from copilot.domain.screening import Exclusion

#: The whole applicable worklist: every posting that survived every gate.
VIEW_KEPT = "kept"
#: The internships collection — postings the internship gate removed that pass
#: every *other* gate. A separate population, not a filter over :data:`VIEW_KEPT`.
VIEW_INTERNSHIPS = "internships"

#: Every addressable view. The gate views are named by :class:`Exclusion` value, so
#: ``/excluded?gate=wrong_seniority_band`` needs no translation table and a new gate
#: becomes a new view automatically. Adapters reject anything not in here rather
#: than returning an empty page: a typo'd view name that reads as "no postings
#: matched" is indistinguishable from a screen that produced nothing, and telling
#: those two apart is the entire reason this view exists.
SCREEN_VIEWS: frozenset[str] = frozenset(
    {VIEW_KEPT, VIEW_INTERNSHIPS, *(gate.value for gate in Exclusion)}
)

#: Bumped when :class:`ScreenedRow` or :class:`ScreenSummary` changes shape, so a
#: view written by an older deploy is reported as *absent* rather than decoded
#: wrongly. Costs one stale day; the alternative is a page of plausible nonsense.
VIEW_VERSION = 1

#: Sorts undated postings last in a descending recency query without dropping them.
#: A literal rather than a formatted epoch because it must be lexicographically
#: below every real ISO stamp — sort keys are compared as **bytes**, so "0000" is
#: the only sentinel that is safely lower than "1970" and every year after it.
UNDATED_SORT_STAMP = "0000-00-00T00:00:00+00:00"

#: How old a materialised view may be before a reader should call it stale. The
#: cron runs daily, so one missed run (~48 h) still serves and two do not. Chosen
#: rather than comparing the view against a live corpus count: that count is
#: ``O(corpus)`` on DynamoDB (33 paged COUNT queries over 47k index entries) and
#: paying it per request would reintroduce the exact cost this view removes.
VIEW_STALE_AFTER_HOURS = 48.0


#: Cap on a stored evidence excerpt. The public route publishes no description
#: prose, and an excerpt exists only to explain one filtering decision — so the cap
#: belongs at the point the row is *written*, not only at the point it is
#: serialised. An uncapped quote would put unbounded description text into a
#: structure that is copied once per view a posting sits in, and would make the
#: 180-character promise on the public wire depend on a projection remembering to
#: truncate what the store already handed it.
QUOTE_MAX_CHARS = 180


def cap_quote(text: str) -> str:
    """Normalise whitespace and cap one evidence excerpt.

    Whitespace is collapsed because these come out of HTML-derived descriptions,
    where a "sentence" routinely spans newlines and runs of non-breaking spaces.
    """
    cleaned = " ".join(text.split())
    if len(cleaned) <= QUOTE_MAX_CHARS:
        return cleaned
    return cleaned[: QUOTE_MAX_CHARS - 1] + "…"


def sort_stamp(posted_at: datetime | None) -> str:
    """UTC-normalised ISO-8601, or the undated sentinel.

    Normalised here and nowhere else because this string ends up **inside a sort
    key**, and DynamoDB compares sort keys as bytes:
    ``2026-07-01T09:00:00+05:00`` sorts *after* ``2026-07-01T10:00:00+00:00``
    while being the earlier instant. Mixing offsets would quietly corrupt the
    recency ordering of the worklist, which is its only ordering. Naive input is
    read as UTC — the pipeline always passes aware UTC, this is a guard.
    """
    if posted_at is None:
        return UNDATED_SORT_STAMP
    aware = posted_at if posted_at.tzinfo is not None else posted_at.replace(tzinfo=UTC)
    return aware.astimezone(UTC).isoformat()


def posted_at_from_sort_key(sort_key: str) -> datetime | None:
    """The date half of a :attr:`ScreenedRow.sort_key`, or ``None`` if undated.

    Both adapters read ``posted_at`` back out of the sort key rather than storing it
    in a column of its own, and that is not a space optimisation. The sort key has
    to hold the timestamp *UTC-normalised* to be byte-comparable, while a column
    would keep whatever offset the ATS supplied — so the same posting would come back
    as ``09:00+05:00`` from SQLite and ``04:00+00:00`` from DynamoDB. Those compare
    equal as instants, which is exactly why the divergence would survive an equality
    test and then surface as two different strings on the wire.
    """
    stamp, _, _ = sort_key.partition("#")
    try:
        return datetime.fromisoformat(stamp)
    except ValueError:
        # The undated sentinel is deliberately not a parseable date.
        return None


@dataclass(frozen=True)
class ScreenedRow:
    """One posting's membership of one view, carrying that view's evidence.

    **One row per (posting, view) pair, not one per posting.** A posting routinely
    fails several gates at once — senior *and* clearance *and* citizenship — and
    ``/excluded`` groups by gate, showing the evidence for the gate it is
    displaying. A single row per posting could not be paged per gate without
    reading the other gates' postings too, and a DynamoDB index gives exactly one
    entry per item, so multi-membership has to be multi-row. Measured on the real
    corpus that is **1.79 rows per posting**: 45,158 rows over 25,294 postings, being
    811 kept + 48 internships + 44,299 gate fires.

    Every posting is in at least one view, so every posting's verdict is
    materialised: kept ones under :data:`VIEW_KEPT`, excluded ones under each gate
    they failed. Nothing is stored twice for the same reason.

    The row carries **only what its view renders** — the level verdict every card
    shows, plus this one gate's reason and quote. Not all seven gates' quotes: that
    would store the same 180-character excerpt up to seven times for one posting.
    A detail read (``GET /worklist/{id}``) that wants the full exclusion list
    re-screens that **single** posting instead, which is 1.5 ms and exact.

    ``description`` is deliberately absent. It is the bulk of a posting item —
    5.6 KB on the mean, 25 KB worst case, 268 MB across the deployed corpus — so
    copying it here would take a row from 1 WCU to ~6 and multiply that by the 1.79
    views a posting sits in. A row is 424 bytes as it stands. The page hydrates
    through :meth:`PostingStorePort.postings_by_id` instead.
    """

    posting_id: str
    view: str
    #: Ordering only, and stored *inside* :attr:`sort_key` rather than beside it —
    #: see :func:`posted_at_from_sort_key`. The displayed date comes from the
    #: hydrated ``Posting``, so this cannot drift into being a second source of
    #: truth for it.
    posted_at: datetime | None
    kept: bool
    #: ``Level`` value. A string rather than the enum because this crosses a
    #: storage boundary and an unknown value must decode, not raise.
    level: str
    level_source: str
    level_why: str
    #: False when the source shipped no description, so the eligibility gates could
    #: not run. Unchecked is **not** eligible, and a view that lost the distinction
    #: would let every Workday posting read as clean.
    eligibility_checked: bool
    sponsorship: str
    #: The gate this row is filed under; ``""`` for the kept and internship views.
    gate: str = ""
    reason: str = ""
    quote: str = ""

    @property
    def sort_key(self) -> str:
        """``<stamp>#<id>`` — newest first when read descending, undated last.

        The id is in the key so the ordering is total: ~4,700 postings in this
        corpus share a ``posted_at`` to the second, and a non-total sort key makes
        keyset pagination skip or repeat rows at every page boundary.
        """
        return f"{sort_stamp(self.posted_at)}#{self.posting_id}"


@dataclass(frozen=True)
class ScreenSummary:
    """The funnel for one screening pass, and what corpus it covered.

    Written **last**, so its presence is the signal that the view behind it is
    complete. A screen that dies half-way leaves orphan rows under a generation no
    reader can name, and the previous complete view still published — aging, but
    never half-written.

    Every total a read needs is here, which is what makes a read O(page): nothing
    has to touch the corpus to say "811 eligible of 25,294".
    """

    #: Identifies the pass. Rows are keyed by it, so publishing this record *is*
    #: the atomic swap from the old view to the new one.
    generation: str
    screened_at: datetime
    #: Open postings the pass read. Equal to ``screened`` unless a posting was
    #: skipped, in which case the difference is the bug.
    corpus_size: int
    screened: int
    kept: int
    #: Postings removed. **Not** the sum of ``gates`` — see :attr:`gate_counts_overcount`.
    excluded: int
    #: Gate value → how many postings that gate fired on.
    gates: Mapping[str, int]
    #: Kept postings whose seniority band could not be read from title or years.
    needs_level_check: int
    #: ``len(kept view)`` and ``len(internships view)``. Carried explicitly rather
    #: than inferred: ``eligible_total`` equals ``kept`` today, and the day a
    #: filter is added to the kept view it will not.
    eligible_total: int
    internship_total: int
    view_version: int = VIEW_VERSION

    @property
    def gate_count_total(self) -> int:
        """Sum of the per-gate counts, which overcounts by design."""
        return sum(self.gates.values())

    @property
    def gate_counts_overcount(self) -> bool:
        """Always true, and on the wire so a UI cannot quietly assume otherwise.

        A posting fails several gates at once, so the per-gate counts sum to far
        more than the number of postings removed — 43,602 against 24,414 on a real
        run. Any interface that renders the funnel as a subtraction chain of gate
        counts produces nonsense. The most visible instance is the internship gate:
        it fires 318 times while the internships collection is 48 postings, because
        264 of those are not software roles at all ("Marketing Intern"), 12 sit on
        ATS vendor demo boards, and the rest want a clearance or a citizenship.
        Both numbers ship in this record so the gap is legible instead of looking
        like an off-by-270 bug.
        """
        return True

    def age_hours(self, now: datetime) -> float:
        """How old this view is. Negative if the clock moved backwards."""
        return (now - self.screened_at).total_seconds() / 3600.0

    def is_stale(self, now: datetime, *, max_age_hours: float = VIEW_STALE_AFTER_HOURS) -> bool:
        """Whether a reader should refuse to present this as current.

        A negative age counts as stale too: a summary stamped in the future means
        the writer's clock and the reader's disagree, and serving it would report a
        ``screenedAt`` a reader cannot reconcile with anything.
        """
        age = self.age_hours(now)
        return age < 0.0 or age > max_age_hours


@dataclass(frozen=True)
class ScreenedPage:
    """One keyset page of one view.

    ``next_token`` is the sort key of the last row returned, opaque to callers and
    ``None`` when the view is exhausted. Keyset and not offset for the reason the
    read API already documents: postings close while a human reads, and an offset
    page 2 taken after a row closes silently *skips* the posting that slid into the
    boundary.
    """

    rows: tuple[ScreenedRow, ...]
    next_token: str | None = None


def summary_to_payload(summary: ScreenSummary) -> dict[str, Any]:
    """JSON-safe form of a summary.

    Here rather than in either adapter so the two cannot encode it differently —
    the same reasoning that keeps the LLM interpretation cache a JSON *string* in
    both stores. It also sidesteps DynamoDB's refusal of ``float`` and its
    Decimal-on-read surprise, since every field below is a string, bool or int.
    """
    return {
        "generation": summary.generation,
        "screened_at": summary.screened_at.isoformat(),
        "corpus_size": summary.corpus_size,
        "screened": summary.screened,
        "kept": summary.kept,
        "excluded": summary.excluded,
        "gates": dict(summary.gates),
        "needs_level_check": summary.needs_level_check,
        "eligible_total": summary.eligible_total,
        "internship_total": summary.internship_total,
        "view_version": summary.view_version,
    }


def summary_from_payload(payload: Mapping[str, Any]) -> ScreenSummary | None:
    """Decode a stored summary, or ``None`` if it cannot be trusted.

    ``None`` rather than an exception, and ``None`` rather than a best-effort
    partial: a reader's only two honest answers are "here is the view" and "there
    is no usable view yet". A summary from an older :data:`VIEW_VERSION`, or one
    missing a field, is the second — it must not be presented as the first, and it
    must not take the endpoint down either, because a 500 on the public page is
    indistinguishable from the 504 this whole change exists to remove.
    """
    if int(payload.get("view_version", 0)) != VIEW_VERSION:
        return None
    try:
        gates = payload["gates"]
        if not isinstance(gates, Mapping):
            return None
        return ScreenSummary(
            generation=str(payload["generation"]),
            screened_at=datetime.fromisoformat(str(payload["screened_at"])),
            corpus_size=int(payload["corpus_size"]),
            screened=int(payload["screened"]),
            kept=int(payload["kept"]),
            excluded=int(payload["excluded"]),
            gates={str(gate): int(count) for gate, count in gates.items()},
            needs_level_check=int(payload["needs_level_check"]),
            eligible_total=int(payload["eligible_total"]),
            internship_total=int(payload["internship_total"]),
        )
    except (KeyError, TypeError, ValueError):
        return None


def summary_to_json(summary: ScreenSummary) -> str:
    return json.dumps(summary_to_payload(summary), sort_keys=True)


def summary_from_json(raw: str) -> ScreenSummary | None:
    try:
        payload = json.loads(raw)
    except ValueError:
        return None
    return summary_from_payload(payload) if isinstance(payload, Mapping) else None


class PostingStorePort(Protocol):
    """Remember postings across runs, and the screening verdict over them."""

    # --- the corpus ----------------------------------------------------------

    def sync(self, postings: list[Posting], *, now: datetime) -> tuple[list[str], list[str]]:
        """Upsert a fetch. Returns ``(newly_seen_ids, already_known_ids)``."""
        ...

    def close_missing(self, *, now: datetime, seen_ids: set[str]) -> int:
        """Mark postings absent from this fetch as closed. Returns how many."""
        ...

    def new_since(self, since: datetime) -> list[Posting]:
        """Postings first seen after ``since`` — the 'what changed' feed."""
        ...

    def open_postings(self) -> list[Posting]:
        """Everything not yet marked closed.

        The whole corpus, and therefore **not** a read-path call any more: at
        47,538 postings this is ~268 MB and ~72 s of screening, which is what put
        the public API over the 29 s gateway ceiling. The cron still calls it once
        a day to build the view; a request serves :meth:`screened_page` instead.
        """
        ...

    def postings_by_id(self, posting_ids: Sequence[str]) -> dict[str, Posting]:
        """Hydrate one page of postings. Unknown ids are simply absent.

        Absent rather than raising because the corpus moves under a reader: a
        posting can close and be reaped between the view being written and a page
        being served, and one missing row must cost that row, not the request.
        """
        ...

    # --- the LLM cache -------------------------------------------------------

    def cached_interpretation(self, posting_id: str) -> dict[str, Any] | None:
        """A previously stored LLM result, or ``None``. The main cost lever."""
        ...

    def save_interpretation(self, posting_id: str, payload: dict[str, Any]) -> None:
        """Store an LLM result so the description is never re-read."""
        ...

    def mark_applied(self, posting_id: str, *, now: datetime) -> None:
        """Record that a human applied — the handoff into the application pipeline."""
        ...

    # --- the materialised screening view ------------------------------------

    def save_screening(self, rows: Iterable[ScreenedRow], *, summary: ScreenSummary) -> None:
        """Publish one screening pass: all rows, **then** the summary.

        ``Iterable`` and not ``Sequence`` because a store only ever walks this once,
        and typing it honestly is what lets the suite prove the claim below: it can
        hand an implementation a producer that dies part-way and assert that the
        previously published view is still the one being served.

        The order is the contract, not an implementation detail. Rows are keyed by
        ``summary.generation``, so until the summary names that generation no
        reader can find them; writing the summary is the atomic publish. A run
        that dies part-way therefore leaves orphan rows and the *previous*
        complete view still current, rather than a half-written view that reads as
        authoritative. That failure mode is not hypothetical — the first live cron
        crashed after the corpus landed and before the run finished.

        One call rather than a ``begin``/``add``/``commit`` trio precisely so a
        caller cannot publish the summary first.
        """
        ...

    def screening_summary(self) -> ScreenSummary | None:
        """The current view's funnel, or ``None`` if there is no usable view.

        ``None`` means "the corpus has not been screened yet" and a reader must say
        so, fast. It must **not** fall back to screening live: that is precisely
        what 504s, and an endpoint that hangs for 29 s and dies is worse than one
        that says "not ready", because a timeout is indistinguishable from an
        outage.
        """
        ...

    def screened_page(
        self, view: str, *, generation: str, limit: int, after: str | None = None
    ) -> ScreenedPage:
        """One recency page of one view, newest first.

        ``generation`` is passed in rather than looked up so a request reads the
        summary once and then pages inside that one consistent snapshot — a page
        that silently followed a generation swap mid-pagination would mix two
        screening passes into one list.

        Raises ``ValueError`` for a view outside :data:`SCREEN_VIEWS`.
        """
        ...
