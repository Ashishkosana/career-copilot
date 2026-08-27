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

from copilot.domain.demo_boards import is_demo_tenant
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


# An internship is not a junior role, it is a different product: it cannot support
# the work authorisation this search exists to obtain. Left ungated, 50 of them
# survived, and 5 of those ranked *exact match* — crowding the one list whose whole
# job is to be short and correct.
#
# Every token here is `\b`-anchored, and that is not decoration. Unanchored `intern`
# matches "Internal Tools Engineer", "Internationalization Engineer" and "Internals
# Engineer" — three real title shapes in this corpus. This is the same missing
# word-boundary bug that made `itar` match "sanitary" (2,476 false exclusions) and
# `rust` match "robust"; it has now cost us three times, so the boundaries here are
# asserted by name in the test suite rather than trusted.
_INTERNSHIP_TITLE = re.compile(
    r"""\b(
        intern | interns | internship | internships
      | co-?ops? | placement\s+student | industrial\s+placement
      | apprentice(?:ship)? | working\s+student | werkstudent
      | summer\s+analyst | new\s+grad\s+intern
    )\b""",
    re.I | re.X,
)

#: ATS ``employment_type`` values that mean "internship". Free text across five
#: boards, so matched case-insensitively on the substring after stripping — the
#: corpus really contains ``Intern``, ``Internship``, ``Temporary FT`` and
#: ``Permanent contract & B2B`` in the same field.
_INTERNSHIP_TYPES = frozenset({"intern", "internship", "co-op", "coop", "apprenticeship"})

#: Sorts undated postings last without dropping them.
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

#: Countries and cities that positively mean "not in the US". Measured 2026-08-26:
#: 52 of 250 live worklist rows (21%) were Bangalore, London, Munich, Berlin,
#: Kraków, Warsaw or "Home based - Worldwide", and 22 of those ranked STRONG. On
#: F-1 OPT a role outside the US is not a weaker match, it is unusable — the same
#: reason clearance and citizenship are gates rather than score penalties.
#:
#: This fires ONLY on a positive non-US match, never on the absence of a US marker.
#: The first draft of this check inverted it — "flag anything without a US state
#: code" — and read "Clifton Park, New York" and "San Francisco" as foreign, which
#: would have excluded 155 rows instead of 52. A false negative here hides a real
#: job; a false positive only leaves one row to skim.
_OUTSIDE_US = re.compile(
    r"""\b(
        india | bangalore | bengaluru | hyderabad | pune | mumbai | delhi | chennai
      | noida | gurgaon | gurugram | kolkata | ahmedabad
      | london | united\s+kingdom | england | scotland | wales | manchester
      | ireland | dublin | belfast
      | germany | berlin | munich | münchen | hamburg | frankfurt
      | france | paris | netherlands | amsterdam | utrecht
      | spain | madrid | barcelona | portugal | lisbon | porto
      | poland | warsaw | warszawa | krakow | kraków | wroclaw | wrocław
      | italy | rome | milan | greece | athens | romania | bucharest
      | czech | prague | hungary | budapest | austria | vienna
      | sweden | stockholm | denmark | copenhagen | norway | oslo | finland | helsinki
      | switzerland | zurich | zürich | geneva | belgium | brussels
      | canada | toronto | vancouver | montreal | montréal | ottawa | waterloo,\s*on
      | australia | sydney | melbourne | brisbane | new\s+zealand | auckland
      | singapore | japan | tokyo | osaka | korea | seoul
      | china | shanghai | beijing | shenzhen | hong\s+kong | taiwan | taipei
      | israel | tel\s+aviv | haifa | united\s+arab\s+emirates | dubai | abu\s+dhabi
      | brazil | são\s+paulo | sao\s+paulo | argentina | buenos\s+aires
      | mexico\s+city | colombia | bogot[áa] | chile | santiago
      | worldwide | emea | apac | latam | anywhere\s+in\s+europe
    )\b""",
    re.I | re.X,
)


def is_outside_us(location: str) -> bool:
    """True only when the location positively names somewhere outside the US.

    A blank or ambiguous location is NOT outside the US. Roughly a third of
    postings carry a bare city with no country, and treating those as foreign
    would drop more real jobs than it saves reading time.
    """
    return bool(location) and bool(_OUTSIDE_US.search(location))


class Exclusion(StrEnum):
    """Why a posting was removed. Ordered by the gate that caught it."""

    NOT_SWE = "not_a_software_role"
    DEMO_BOARD = "ats_vendor_demo_board"
    INTERNSHIP = "internship_not_full_time"
    LEVEL = "wrong_seniority_band"
    CLEARANCE = "security_clearance_required"
    CITIZENSHIP = "citizenship_or_itar_restricted"
    NO_SPONSORSHIP = "employer_will_not_sponsor"
    OUTSIDE_US = "outside_the_us"


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
        reasons: dict[Exclusion, str] = {
            Exclusion.NOT_SWE: f"not a software engineering role: {self.posting.title!r}",
            Exclusion.DEMO_BOARD: (
                f"{self.posting.company!r} is an ATS vendor demo board, not an employer"
            ),
            Exclusion.INTERNSHIP: self._internship_reason(),
            Exclusion.LEVEL: f"seniority band is {self.level.value}, not entry-level",
            Exclusion.CLEARANCE: f"clearance required — {quoted.get('clearance', '')!r}",
            Exclusion.CITIZENSHIP: f"citizenship/ITAR — {quoted.get('citizenship', '')!r}",
            Exclusion.NO_SPONSORSHIP: (
                f"will not sponsor — {quoted.get('no_sponsorship', '')!r}"
            ),
            Exclusion.OUTSIDE_US: (
                f"located outside the US — {self.posting.location!r}"
            ),
        }
        return reasons.get(exclusion, exclusion.value)

    def _internship_reason(self) -> str:
        """Quote whichever signal fired, since the title and the type disagree often."""
        hit = _INTERNSHIP_TITLE.search(self.posting.title)
        if hit:
            return f"internship, not full-time — {hit.group(0)!r} in the title"
        return f"internship, not full-time — employment type {self.posting.employment_type!r}"

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


def is_internship(title: str, employment_type: str = "") -> bool:
    """Whether a posting is an internship, co-op or apprenticeship.

    Reads the title *and* the ATS ``employment_type``, because neither alone is
    enough: 22 postings in this corpus declare type ``Intern``/``Internship`` with a
    clean title like "Software Engineer, Product", and 27 carry it only in the title
    while leaving the type field empty. 382 of 880 postings have no type at all, so
    a type-only gate would miss nearly half the corpus by construction.
    """
    if title and _INTERNSHIP_TITLE.search(title):
        return True
    kind = employment_type.strip().lower().replace(" ", "")
    return any(kind == t.replace("-", "").replace(" ", "") for t in _INTERNSHIP_TYPES)


def screen(
    posting: Posting,
    *,
    wanted: frozenset[Level] = JUNIOR_BANDS,
    include_internships: bool = False,
) -> ScreenDecision:
    """Run the funnel over one posting. Gates are cheapest-first.

    ``include_internships`` defaults to False because this search exists to obtain
    full-time work authorisation, which an internship cannot support. It is a flag
    rather than a hardcoded rule because it is a *product* decision, not a
    correctness one, and because "Fall 2026" internships at large employers are a
    real entry path for some people — flipping it back should cost one argument.

    Note that ``Level.INTERN`` deliberately stays in :data:`JUNIOR_BANDS`. Level
    classification answers "what band is this posting", which is a fact about the
    posting; whether internships are wanted is a fact about the searcher. Collapsing
    the two would mean a candidate who *does* want internships could not express it
    without editing the seniority model.
    """
    exclusions: list[Exclusion] = []

    if not is_software_role(posting.title):
        exclusions.append(Exclusion.NOT_SWE)

    # Gated here as well as in the watchlist parser, and the redundancy is the point:
    # the parser stops the *next* fetch, but 317 demo postings are already stored and
    # no store exposes a delete. A read-time gate is the only one that is portable
    # across SQLite and DynamoDB without inventing a purge port, and it means a demo
    # board that slips past discovery can never reach the page.
    if is_demo_tenant(posting.company):
        exclusions.append(Exclusion.DEMO_BOARD)

    if not include_internships and is_internship(posting.title, posting.employment_type):
        exclusions.append(Exclusion.INTERNSHIP)

    # A string test on one field, so it belongs up here with the cheap gates. This
    # is a gate and not a score penalty for the same reason CLEARANCE is: on F-1
    # OPT a Bangalore or London posting is not a worse match, it is one he cannot
    # take. It went missing until 2026-08-26, when 21% of the live worklist turned
    # out to be non-US and 22 of those were ranked STRONG.
    if is_outside_us(posting.location):
        exclusions.append(Exclusion.OUTSIDE_US)

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
    postings: Sequence[Posting],
    *,
    wanted: frozenset[Level] = JUNIOR_BANDS,
    include_internships: bool = False,
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
        decision = screen(posting, wanted=wanted, include_internships=include_internships)
        report.note(decision)
        (kept if decision.kept else excluded).append(decision)
    kept.sort(key=_recency, reverse=True)
    return kept, excluded, report


def _recency(decision: ScreenDecision) -> datetime:
    """Sort key that survives a naive ``posted_at``.

    ``posted_at or _EPOCH`` was the obvious spelling and it raises: ``_EPOCH`` is
    aware, the ATS adapters do not all return an offset, and Python refuses to compare
    an aware datetime with a naive one. So *one* dateless-offset posting in a batch took
    the whole call down with ``TypeError: can't compare offset-naive and offset-aware
    datetimes`` — and only once at least two postings were kept, since a one-element
    sort never compares anything. Naive input is read as UTC, matching
    ``ports.postingstore.sort_stamp``, which is what writes this ordering down.
    """
    posted = decision.posting.posted_at
    if posted is None:
        return _EPOCH
    return posted if posted.tzinfo is not None else posted.replace(tzinfo=UTC)
