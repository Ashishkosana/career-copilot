"""Eligibility gates — categorical bars, never weights.

A security clearance you cannot obtain and a role restricted to US citizens are
not "worse fits", they are impossibilities. Scoring them at all is a category
error, so they are excluded and listed separately with the phrase that triggered
the exclusion quoted, which makes every decision auditable.

Two design rules learned the hard way:

* **These gates only run when a description exists.** Workday's list endpoint
  returns none, and an empty string matches no exclusion pattern — so an
  unguarded gate silently *passes* every Workday posting. Callers must respect
  ``desc_available``.
* **Sponsorship is tri-state, and "unstated" is the common case.** Most postings
  say nothing. Treating silence as "will not sponsor" would discard most of the
  market; treating it as "will sponsor" would be wishful. It stays unstated and a
  human decides.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum


class Sponsorship(StrEnum):
    """What a posting actually says about visa sponsorship."""

    WILL_SPONSOR = "will_sponsor"
    WILL_NOT_SPONSOR = "will_not_sponsor"
    UNSTATED = "unstated"


CLEARANCE = re.compile(
    r"""(
        \bsecurity\s+clearance\b | \bclearance\s+(?:is\s+)?required\b
      | \bactive\s+clearance\b | \bts\s*/\s*sci\b | \btop\s+secret\b
      | \bsecret\s+clearance\b | \bsci\s+clearance\b | \bpolygraph\b
      | \bpublic\s+trust\b | \bq\s+clearance\b | \bl\s+clearance\b
      | \bdod\s+clearance\b | \bability\s+to\s+obtain\s+a\s+clearance\b
    )""",
    re.I | re.X,
)

# Every alternative is boundary-anchored. Without ``\b`` the bare ``itar``
# alternative matched *inside* ordinary words — "military", "sanitary" and
# "solitary" all contain the letters i-t-a-r, so an unanchored pattern excluded
# entry-level roles whose only sin was the word "military" in a benefits blurb.
#: Written ``(?:us|u\.s\.|united\s+states)`` throughout. Spelled out once as a named
#: group would read better, but the alternatives below each need it in a different
#: position, and a shared group made the boundary cases harder to eyeball — which is
#: the only thing that has ever gone wrong in this file.
_US = r"(?:us|u\.s\.|united\s+states)"

CITIZENSHIP = re.compile(
    rf"""(
        \b(?:must|required\s+to)\s+be\s+a?\s*{_US}\s+citizens?\b
      | \b{_US}\s+citizenship\s+(?:is\s+)?(?:required|mandatory)\b
      | \b{_US}\s+citizens?\s+only\b
      | \bcitizenship\s+is\s+required\b
      | \bitar\b
      | \bexport\s+control(?:led|s)?\s+(?:regulations?|requirements?|laws?)\b
      | \bmust\s+be\s+a\s+(?:us|u\.s\.)\s+person\b
      # --- added after measuring the gap on 25,294 live postings ---
      # "This position requires US citizenship" — verb first, so the
      # `citizenship ... required` alternative above cannot reach it.
      | \brequires?\s+{_US}\s+citizenship\b
      # "Candidates must hold US citizenship" — "hold"/"have", not "be".
      | \bmust\s+(?:hold|have|possess)\s+{_US}\s+citizenship\b
      # "US citizen or permanent resident" as a stated requirement. This is the one
      # that was actually costing something: 2 of 813 kept roles, both listing it
      # under "Eligibility". It excludes an F-1/OPT candidate as hard as "citizens
      # only" does, and it is the standard phrasing in government-adjacent postings —
      # so as the watchlist grows this is the alternative that will earn its keep.
      | \b{_US}\s+citizens?\s+or\s+(?:lawful\s+)?permanent\s+residents?\b
      | \bcitizenship\s+(?:is\s+)?(?:a\s+)?(?:requirement|prerequisite)\b
    )""",
    re.I | re.X,
)

#: Phrasing that mentions citizenship in order to say it is *not* a barrier. Checked
#: in a window around a :data:`CITIZENSHIP` hit, and a hit here vetoes the exclusion.
#:
#: This guard is the whole reason the "citizen or permanent resident" alternative is
#: safe to add. "Open to US citizens, permanent residents, and candidates requiring
#: visa sponsorship" contains the exclusionary phrase verbatim while meaning the
#: opposite, and 6 postings in the live corpus put citizenship next to
#: work-authorisation language. Excluding those would repeat the ``itar`` mistake in
#: a new place: that pattern matched inside "military" and "sanitary" and threw away
#: 2,476 roles, and the lesson was that a gate which over-fires is worse than one
#: that under-fires, because a false exclusion is invisible.
CITIZENSHIP_NOT_REQUIRED = re.compile(
    r"""(
        \bregardless\s+of\s+(?:citizenship|immigration|national\s+origin)
      | \b(?:any|all)\s+(?:form\s+of\s+)?work\s+authoriz\w+
      | \bor\s+(?:candidates\s+)?(?:requiring|needing|with)\s+(?:visa\s+)?sponsorship
      | \bor\s+(?:those\s+)?(?:who\s+)?(?:require|need)\s+sponsorship
      | \b(?:opt|cpt|f-?1|h-?1b|ead|tn\s+visa)\b
      | \bvisa\s+holders?\b
      | \bwe\s+sponsor\b
    )""",
    re.I | re.X,
)

#: How far either side of a citizenship hit to look for the veto above. One long
#: sentence or one bullet and its neighbour; wide enough to catch "…permanent
#: resident, or requiring sponsorship", narrow enough that an unrelated benefits
#: paragraph three screens away cannot cancel a real requirement.
CITIZENSHIP_VETO_WINDOW = 160

# Negative sponsorship. Ordered so the most explicit phrasings appear first; the
# patterns deliberately require a sponsorship/visa object so that "unable to
# provide relocation" does not trip them.
NO_SPONSORSHIP = re.compile(
    r"""(
        (?:not|unable|cannot|can\s?not|do\s+not|does\s+not|will\s+not|won'?t)
        [^.]{0,40}?
        (?:sponsor(?:ship|ing)?|visa\s+sponsorship|work\s+visas?)
      | no\s+(?:visa\s+)?sponsorship (?:\s+(?:is\s+)?(?:available|offered|provided))?
      | without\s+(?:the\s+need\s+for\s+)?(?:visa\s+)?sponsorship
      | sponsorship\s+is\s+not\s+(?:available|offered|provided)
      | we\s+are\s+not\s+able\s+to\s+sponsor
    )""",
    re.I | re.X,
)

YES_SPONSORSHIP = re.compile(
    r"""(
        (?:will|do|does|can|happy\s+to|able\s+to|open\s+to)
        \s+(?:provide\s+|offer\s+)?
        (?:visa\s+)?sponsor(?:ship|ing)?
      | (?:visa\s+)?sponsorship\s+(?:is\s+)?(?:available|offered|provided)
      | we\s+sponsor\s+visas?
      | h-?1b\s+(?:transfers?\s+)?(?:welcome|accepted|supported)
    )""",
    re.I | re.X,
)


@dataclass(frozen=True)
class Eligibility:
    """Outcome of the hard gates for one posting."""

    clearance_required: bool = False
    citizenship_required: bool = False
    sponsorship: Sponsorship = Sponsorship.UNSTATED
    evidence: tuple[tuple[str, str], ...] = ()
    checked: bool = True

    @property
    def excluded(self) -> bool:
        """True when the posting is categorically closed to an F-1 OPT candidate."""
        return (
            self.clearance_required
            or self.citizenship_required
            or self.sponsorship is Sponsorship.WILL_NOT_SPONSOR
        )

    @property
    def exclusion_reasons(self) -> tuple[str, ...]:
        reasons: list[str] = []
        if self.clearance_required:
            reasons.append("security clearance required")
        if self.citizenship_required:
            reasons.append("US citizenship / ITAR restricted")
        if self.sponsorship is Sponsorship.WILL_NOT_SPONSOR:
            reasons.append("employer states it will not sponsor")
        return tuple(reasons)


def _first_match(pattern: re.Pattern[str], text: str) -> str:
    found = pattern.search(text)
    if not found:
        return ""
    start = max(0, found.start() - 40)
    return " ".join(text[start : found.end() + 40].split())


def _citizenship_requirement(description: str) -> str:
    """The quoted span proving citizenship is required, or ``""``.

    Every :data:`CITIZENSHIP` hit is checked against
    :data:`CITIZENSHIP_NOT_REQUIRED` in a window around it, and a veto discards that
    hit and keeps looking — it does not abandon the whole description. A posting can
    say "open to citizens, residents, or those needing sponsorship" in its intro and
    "US citizens only" in its requirements, and the second one is the binding
    statement. Returning on the first veto would let the friendly sentence launder
    the restrictive one.
    """
    for match in CITIZENSHIP.finditer(description):
        start = max(0, match.start() - CITIZENSHIP_VETO_WINDOW)
        window = description[start : match.end() + CITIZENSHIP_VETO_WINDOW]
        if CITIZENSHIP_NOT_REQUIRED.search(window):
            continue
        return match.group(0).strip()
    return ""


def screen_eligibility(description: str | None, *, desc_available: bool = True) -> Eligibility:
    """Run the hard gates over a description.

    With no description there is nothing to screen, so the result is marked
    ``checked=False`` — *not* "eligible". A caller that conflates the two silently
    admits every Workday posting.
    """
    if not desc_available or not description:
        return Eligibility(checked=False)

    evidence: list[tuple[str, str]] = []

    clearance_span = _first_match(CLEARANCE, description)
    if clearance_span:
        evidence.append(("clearance", clearance_span))

    citizenship_span = _citizenship_requirement(description)
    if citizenship_span:
        evidence.append(("citizenship", citizenship_span))

    negative = _first_match(NO_SPONSORSHIP, description)
    positive = _first_match(YES_SPONSORSHIP, description)
    if negative:
        # An explicit refusal outranks boilerplate that merely mentions sponsorship.
        sponsorship = Sponsorship.WILL_NOT_SPONSOR
        evidence.append(("no_sponsorship", negative))
    elif positive:
        sponsorship = Sponsorship.WILL_SPONSOR
        evidence.append(("sponsorship", positive))
    else:
        sponsorship = Sponsorship.UNSTATED

    return Eligibility(
        clearance_required=bool(clearance_span),
        citizenship_required=bool(citizenship_span),
        sponsorship=sponsorship,
        evidence=tuple(evidence),
    )
