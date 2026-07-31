"""The screening funnel: role family → level → eligibility → worklist.

This is the whole architectural correction. The old path scored every posting on
one additive axis and took the top 8; measured over 4,515 live postings it had
**10% recall** on junior SWE roles and **2.6% precision**, and 0 of the 8 roles it
actually surfaced were junior. Two of the eight were "AI Solutions Engineer" — a
sales-adjacent role, listed twice, with no dedupe.

The replacement is a funnel of categorical gates, in cheapest-first order, and the
output is a **worklist sorted by recency — not a ranked list with a match
percentage**. Nothing is scored, because at a 0.66% base rate the useful question
is "is this even applicable", not "which of these is best".

Each gate can only *exclude*. A posting that survives every gate is shown; a
posting that fails one is listed separately with the reason and the quoted phrase,
so nothing disappears silently.
"""
from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

from copilot.domain.eligibility import Eligibility, Sponsorship, screen_eligibility
from copilot.domain.posting import Posting
from copilot.domain.seniority import JUNIOR_BANDS, Level, LevelVerdict, decide_level

_SWE_TITLE = re.compile(
    r"""\b(
        software\s+(?:engineer|developer|dev)
      | swe | sde | sdet
      | (?:backend|back[-\s]end|frontend|front[-\s]end|full[-\s]?stack|mobile
         |ios|android|platform|infrastructure|systems|embedded|web)
        \s+(?:engineer|developer)
      | engineer(?:ing)?,\s*(?:backend|frontend|full[-\s]?stack|mobile|platform)
      | programmer | developer
      | software\s+engineering
    )\b""",
    re.I | re.X,
)

# Titles that contain "engineer" or "developer" but are not software engineering
# roles. "AI Solutions Engineer" is the canonical false positive from the old path.
_NOT_SWE_TITLE = re.compile(
    r"""\b(
        sales | solutions? | pre[-\s]?sales | support | customer | success | account
      | marketing | recruit\w* | talent | people | finance | legal | procurement
      | product\s+manager | program\s+manager | project\s+manager | technical\s+writer
      | data\s+(?:scientist|analyst) | analytics\s+engineer | research\s+scientist
      | business | operations\s+engineer | field\s+engineer | implementation
      | quality\s+(?:assurance\s+)?(?:manager|lead) | designer | ux | ui\s+designer
      | mechanical | electrical | civil | chemical | industrial | hardware
      | manufacturing | process\s+engineer | test\s+technician | validation
    )\b""",
    re.I | re.X,
)


#: Sorts undated postings last without dropping them.
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


class Exclusion(StrEnum):
    """Why a posting was removed. Ordered by the gate that caught it."""

    NOT_SWE = "not_a_software_role"
    LEVEL = "wrong_seniority_band"
    CLEARANCE = "security_clearance_required"
    CITIZENSHIP = "citizenship_or_itar_restricted"
    NO_SPONSORSHIP = "employer_will_not_sponsor"


@dataclass(frozen=True)
class ScreenDecision:
    """The verdict for one posting, with its evidence."""

    posting: Posting
    level: Level
    eligibility: Eligibility
    exclusions: tuple[Exclusion, ...] = ()
    #: How ``level`` was decided. A UI that shows the band must be able to say why.
    level_verdict: LevelVerdict | None = None

    @property
    def kept(self) -> bool:
        return not self.exclusions

    def reason_for(self, exclusion: Exclusion) -> str:
        """The explanation for one specific gate, quoting the triggering phrase.

        Addressable per gate rather than a flat list: a posting can fail several
        gates at once, and a UI that groups by gate must be able to show the
        matching evidence rather than whichever reason happened to come first.
        """
        quoted = dict(self.eligibility.evidence)
        if exclusion is Exclusion.NOT_SWE:
            return f"not a software engineering role: {self.posting.title!r}"
        if exclusion is Exclusion.LEVEL:
            return f"seniority band is {self.level.value}, not entry-level"
        if exclusion is Exclusion.CLEARANCE:
            return f"clearance required — {quoted.get('clearance', '')!r}"
        if exclusion is Exclusion.CITIZENSHIP:
            return f"citizenship/ITAR — {quoted.get('citizenship', '')!r}"
        if exclusion is Exclusion.NO_SPONSORSHIP:
            return f"will not sponsor — {quoted.get('no_sponsorship', '')!r}"
        return exclusion.value

    @property
    def reasons(self) -> tuple[str, ...]:
        """All exclusion reasons, in gate order."""
        return tuple(self.reason_for(exclusion) for exclusion in self.exclusions)

    @property
    def reasons_by_gate(self) -> dict[str, str]:
        """Gate name → its own explanation. What a grouped UI needs."""
        return {exclusion.value: self.reason_for(exclusion) for exclusion in self.exclusions}


def is_software_role(title: str) -> bool:
    """Title-only role-family gate."""
    if not title:
        return False
    if _NOT_SWE_TITLE.search(title):
        return False
    return bool(_SWE_TITLE.search(title))


def screen(posting: Posting, *, wanted: frozenset[Level] = JUNIOR_BANDS) -> ScreenDecision:
    """Run the funnel over one posting. Gates are cheapest-first."""
    exclusions: list[Exclusion] = []

    if not is_software_role(posting.title):
        exclusions.append(Exclusion.NOT_SWE)

    verdict = decide_level(
        posting.title, posting.description, desc_available=posting.desc_available
    )
    level = verdict.level
    # UNKNOWN survives the level gate on purpose: 45% of real SWE titles carry no
    # level marker at all, and dropping them would discard the largest slice of
    # the market. Resolving UNKNOWN is what the LLM tier is for.
    if level is not Level.UNKNOWN and level not in wanted:
        exclusions.append(Exclusion.LEVEL)

    eligibility = screen_eligibility(
        posting.description, desc_available=posting.desc_available
    )
    if eligibility.clearance_required:
        exclusions.append(Exclusion.CLEARANCE)
    if eligibility.citizenship_required:
        exclusions.append(Exclusion.CITIZENSHIP)
    if eligibility.sponsorship is Sponsorship.WILL_NOT_SPONSOR:
        exclusions.append(Exclusion.NO_SPONSORSHIP)

    return ScreenDecision(
        posting=posting,
        level=level,
        eligibility=eligibility,
        exclusions=tuple(exclusions),
        level_verdict=verdict,
    )


@dataclass
class ScreenReport:
    """Funnel accounting — every stage's drop is visible, none are silent."""

    total: int = 0
    kept: int = 0
    by_exclusion: dict[str, int] = field(default_factory=dict)
    needs_llm: int = 0

    @property
    def excluded(self) -> int:
        """Postings removed. **Not** the sum of ``by_exclusion``.

        A posting routinely fails several gates at once — senior *and* clearance
        *and* citizenship — so the per-gate counts sum to far more than the number
        of postings actually removed (43,602 vs 24,414 on a real run). Any UI that
        renders the funnel as a subtraction chain of gate counts produces nonsense.
        """
        return self.total - self.kept

    @property
    def gate_count_total(self) -> int:
        """Sum of per-gate counts, which overcounts by design. Label it as such."""
        return sum(self.by_exclusion.values())

    def note(self, decision: ScreenDecision) -> None:
        self.total += 1
        if decision.kept:
            self.kept += 1
            if decision.level is Level.UNKNOWN:
                self.needs_llm += 1
            return
        for exclusion in decision.exclusions:
            self.by_exclusion[exclusion.value] = self.by_exclusion.get(exclusion.value, 0) + 1


def screen_all(
    postings: Sequence[Posting], *, wanted: frozenset[Level] = JUNIOR_BANDS
) -> tuple[list[ScreenDecision], list[ScreenDecision], ScreenReport]:
    """Screen a batch. Returns ``(kept, excluded, report)``.

    ``kept`` is sorted **newest first** — recency is the only ordering, because a
    worklist answers "what is open and applicable", not "what is the best match".
    Undated postings sort last rather than being dropped.
    """
    report = ScreenReport()
    kept: list[ScreenDecision] = []
    excluded: list[ScreenDecision] = []
    for posting in postings:
        decision = screen(posting, wanted=wanted)
        report.note(decision)
        (kept if decision.kept else excluded).append(decision)
    kept.sort(key=lambda d: d.posting.posted_at or _EPOCH, reverse=True)
    return kept, excluded, report
