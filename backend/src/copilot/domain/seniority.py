"""Seniority classification — a **gate**, not a score.

The scorer this replaces put junior and senior roles on one additive axis, so a
senior role could always buy its way up with skill overlap: measured on 4,515 real
postings, "New Grad Software Engineer" scored 36 and was rejected at the 40
threshold while "Senior Staff Engineer" scored 42 and passed. The defect is
structural — ``sum(weight for kw in text)`` is monotonically increasing in
description verbosity, and verbosity tracks seniority.

So level is decided here, categorically, and a role outside the wanted band is
*excluded* rather than down-weighted.

Every regex below exists because a simpler version got something wrong on real
data:

* ``\\bintern\\b`` had to exclude ``International`` — a naive ``intern(?!al)``
  matched Affirm's "Senior Software Engineer, Backend (Identity International)".
* ``Engineer I`` must not match ``Engineer II``/``III``, and a senior marker must
  beat a level numeral: Samsara really does post "Senior Software Engineer I".
* Years-of-experience must ignore "4-year degree" and "graduating in 2026", which
  are education phrases, not experience requirements.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum


# Ordered least → most senior. Comparisons use ``RANK``, never the enum itself.
class Level(StrEnum):
    """Seniority band of a posting."""

    INTERN = "intern"
    ENTRY = "entry"
    MID = "mid"
    SENIOR = "senior"
    UNKNOWN = "unknown"


RANK: dict[Level, int] = {
    Level.INTERN: 0,
    Level.ENTRY: 1,
    Level.MID: 2,
    Level.SENIOR: 3,
    Level.UNKNOWN: 2,  # sorts with mid; callers that care must check explicitly
}

#: Bands an entry-level candidate should ever see.
JUNIOR_BANDS: frozenset[Level] = frozenset({Level.INTERN, Level.ENTRY})

# --- title markers -----------------------------------------------------------

# Senior markers win over everything else. "Sr" needs a boundary so it does not
# match "Sr" inside another token; "lead" must not match "leadership".
_SENIOR = re.compile(
    r"""\b(
        senior | sr\.? | staff | principal | distinguished | fellow
      | lead(?!ership) | leader | manager | mgr | director | head\s+of
      | architect | vp | vice\s+president | chief | president
      | expert | specialist\s+iv
    )\b""",
    re.I | re.X,
)

# Level numerals. Roman II/III/IV and arabic 2-5 read as mid-or-above; a bare
# I/1 reads as entry. The negative lookahead stops "I" matching the "I" of "II".
_LEVEL_MID_UP = re.compile(r"\b(?:i{2,3}|iv|v|[2-9])\b", re.I)
_LEVEL_ONE = re.compile(r"\b(?:i|1)\b(?![ivx])", re.I)

_INTERN = re.compile(r"\b(?:intern(?![a-z])|internships?|co-?op)\b", re.I)

_ENTRY = re.compile(
    r"""\b(
        new\s?grad(?:uate)? | grad(?:uate)?\s+(?:program|scheme|engineer|hire)
      | entry[-\s]?level | junior | jr\.? | associate | apprentice
      | early\s+career | campus | university\s+grad(?:uate)?
      | new\s+college\s+grad(?:uate)?
    )\b""",
    re.I | re.X,
)


# Evaluated in order — the first match wins, so senior markers beat junior ones.
# "Senior Software Engineer I" is a senior role with a level numeral, and Samsara
# posts exactly that.
_TITLE_RULES: tuple[tuple[re.Pattern[str], Level], ...] = (
    (_SENIOR, Level.SENIOR),
    (_INTERN, Level.INTERN),
    (_ENTRY, Level.ENTRY),
    (_LEVEL_MID_UP, Level.MID),
    (_LEVEL_ONE, Level.ENTRY),
)


def classify_level(title: str) -> Level:
    """Decide a posting's band from its title alone."""
    if not title:
        return Level.UNKNOWN
    for pattern, level in _TITLE_RULES:
        if pattern.search(title):
            return level
    return Level.UNKNOWN


# --- years of experience -----------------------------------------------------

# Phrases that look numeric but describe education, not experience.
_EDUCATION_NOISE = re.compile(
    r"""(
        \d\s*[-–]?\s*year\s+(?:degree|program|course|university|college|bachelor)
      | (?:graduat\w+|complet\w+|finish\w+)[^.]{0,30}\b20\d{2}
      | \b20\d{2}\b\s*(?:graduate|grad|cohort|start)
    )""",
    re.I | re.X,
)

# ``years'`` is two characters after "year" (plural + apostrophe), so the
# possessive and the plural need separate optional groups — a single character
# class silently failed to match "3-5 years' experience".
# Real postings use both a straight and a curly apostrophe in the possessive.
_YEARS = r"year(?:s)?(?:['’]s?)?"
_QUALIFIER = (
    r"(?:relevant\s+|professional\s+|industry\s+|software\s+|hands[-\s]?on\s+|"
    r"work\s+|practical\s+|full[-\s]?time\s+|engineering\s+)?"
)
# An en dash appears in year ranges as often as a hyphen does.
_COUNT = r"(\d{1,2})\s*(?:\+|plus)?\s*(?:[-–]|to)?\s*(?:\d{1,2})?\s*(?:\+)?\s*"

_YOE_PATTERNS = (
    # "3+ years of experience", "3 to 5 years experience", "3-5 years' experience"
    re.compile(rf"{_COUNT}{_YEARS}\s*(?:of\s+)?{_QUALIFIER}(?:experience|exp\b)", re.I),
    # "2+ years required" — plenty of JDs never say the word "experience".
    re.compile(
        rf"{_COUNT}{_YEARS}\s*(?:of\s+)?{_QUALIFIER}"
        r"(?:required|requirement|minimum|min\b|preferred|in\s+a\s+similar)",
        re.I,
    ),
    re.compile(rf"minimum\s+(?:of\s+)?(\d{{1,2}})\s*(?:\+)?\s*{_YEARS}", re.I),
    re.compile(rf"at\s+least\s+(\d{{1,2}})\s*(?:\+)?\s*{_YEARS}", re.I),
)

_MAX_PLAUSIBLE_YEARS = 25


def extract_min_years(description: str | None) -> int | None:
    """Smallest plausible years-of-experience requirement stated in a JD.

    ``min`` rather than ``max``: a posting saying "2+ years required, 5+
    preferred" is open to a candidate with 2. Returns ``None`` when nothing is
    stated, which is the common case — only 16% of unmarked SWE postings state
    years at all, which is exactly why the LLM tier exists.
    """
    if not description:
        return None
    text = _EDUCATION_NOISE.sub(" ", description)
    found: list[int] = []
    for pattern in _YOE_PATTERNS:
        for match in pattern.finditer(text):
            try:
                value = int(match.group(1))
            except (TypeError, ValueError):
                continue
            if 0 <= value <= _MAX_PLAUSIBLE_YEARS:
                found.append(value)
    return min(found) if found else None


#: A new grad can credibly apply up to this stated requirement; beyond it the
#: posting is asking for someone else, regardless of how the title reads.
ENTRY_MAX_YEARS = 2
MID_MAX_YEARS = 5


def level_from_years(years: int | None) -> Level:
    """Map a stated years requirement onto a band."""
    if years is None:
        return Level.UNKNOWN
    if years <= ENTRY_MAX_YEARS:
        return Level.ENTRY
    if years <= MID_MAX_YEARS:
        return Level.MID
    return Level.SENIOR


class LevelSource(StrEnum):
    """Where a level decision came from. Required for the UI to be honest.

    Without this the interface has to guess *why* a band was assigned, and it
    guesses wrong: a plain "Software Engineer" whose description asks for 1 year
    resolves to ENTRY, and a UI holding only the label will happily claim "the
    title names an entry-level role" — which is false. The label alone is not
    enough to explain itself.
    """

    TITLE = "title"
    YEARS = "years"
    NONE = "none"


@dataclass(frozen=True)
class LevelVerdict:
    """A band plus the reason for it, with the text that decided it."""

    level: Level
    source: LevelSource
    evidence: str = ""

    def explain(self) -> str:
        if self.source is LevelSource.TITLE:
            article = "an" if self.level.value[0] in "aeiou" else "a"
            return f"the title carries {article} {self.level.value} marker"
        if self.source is LevelSource.YEARS:
            return f"the description asks for {self.evidence}"
        return "neither the title nor the description states a level"


def decide_level(
    title: str, description: str | None, *, desc_available: bool = True
) -> LevelVerdict:
    """Resolve a band and record how it was decided.

    **An explicit title marker wins.** Years-of-experience only decides the band
    when the title says nothing — which is the case for 45% of real SWE postings.
    Letting years override an explicit marker cost real recall: "Software Engineer
    I" postings were being reclassified as mid because a *preferred*-qualifications
    bullet mentioned three years.
    """
    from_title = classify_level(title)
    if from_title is not Level.UNKNOWN:
        return LevelVerdict(from_title, LevelSource.TITLE, title)
    if not desc_available:
        return LevelVerdict(Level.UNKNOWN, LevelSource.NONE)
    years = extract_min_years(description)
    if years is None:
        return LevelVerdict(Level.UNKNOWN, LevelSource.NONE)
    plural = "year" if years == 1 else "years"
    return LevelVerdict(level_from_years(years), LevelSource.YEARS, f"{years}+ {plural}")


def resolve_level(title: str, description: str | None, *, desc_available: bool = True) -> Level:
    """The band alone. Prefer :func:`decide_level` when the reason matters."""
    return decide_level(title, description, desc_available=desc_available).level
