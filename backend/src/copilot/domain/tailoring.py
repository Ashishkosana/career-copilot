"""Validators for a rewritten résumé bullet. Deterministic, and they fail closed.

An LLM rewrite is **untrusted input**. These checks decide whether it may reach a
PDF, and a tripped check produces no résumé rather than a résumé with a false
claim in it.

The scoping matters more than the checks. An earlier version scoped the technology
check to the *whole document*, and because the Skills block enumerates ~60
technologies, injecting ``PostgreSQL`` into a DynamoDB bullet produced an empty set
difference. Eight adversarial rewrites of real bullets passed all six validators —
every one a false or materially inflated claim. So every check here is scoped to
**the bullet's own declarations**.

What these validators still cannot catch, stated plainly: **inflated scope and
seniority.** "Helped build" → "led the design of" introduces no new technology and
no new number, so it passes everything below. That is a human review step, not a
mechanical one, and pretending otherwise would be the dangerous mistake.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

from copilot.domain.gap import VOCAB, tokens_in


@dataclass(frozen=True)
class Metric:
    """A number the bullet is allowed to state, and what it measures."""

    id: str
    numbers: tuple[str, ...]
    unit: str
    subject_head: str
    claim: str
    evidence: str

    def mentions_subject(self, text: str, number: str, window: int = 80) -> bool:
        """Is ``subject_head`` near this number?

        Without this, a rewrite can move a real number onto a different subject —
        "~82% coverage" becoming "~82% reduction in token cost" — which is verbatim
        from the source and invisible to a digit-presence check.
        """
        for match in re.finditer(re.escape(number), text):
            start = max(0, match.start() - window)
            if self.subject_head.lower() in text[start : match.end() + window].lower():
                return True
        return False


@dataclass(frozen=True)
class Bullet:
    """One authored bullet with everything a validator needs declared up front."""

    id: str
    text: str
    tech: frozenset[str]
    metrics: tuple[Metric, ...] = ()
    entry_start: str = ""  # ISO YYYY-MM of the parent role
    entry_end: str | None = None
    #: Ceiling for the rewrite's first word. An allowlist — a blacklist of five
    #: verbs let Directed/Established/Orchestrated/Headed/Pioneered through.
    allowed_verbs: frozenset[str] = frozenset()
    #: Strings that must survive character-identical: mechanisms, and qualifiers
    #: like "Benchmark:" whose deletion fabricates by omission.
    must_keep: tuple[str, ...] = ()

    @property
    def allowed_numbers(self) -> frozenset[str]:
        return frozenset(n for m in self.metrics for n in m.numbers)


@dataclass
class Violation:
    """A specific reason a rewrite must not ship."""

    validator: str
    bullet_id: str
    detail: str


@dataclass
class ValidationResult:
    violations: list[Violation] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.violations

    def report(self) -> str:
        if self.ok:
            return "PASS — all validators"
        lines = [f"FAIL — {len(self.violations)} violation(s), nothing will be rendered:"]
        for v in self.violations:
            lines.append(f"  [{v.validator}] {v.bullet_id}: {v.detail}")
        return "\n".join(lines)


# Numbers that are part of a technology's own name, not a claim about impact.
_TECH_EMBEDDED_NUMBERS = re.compile(
    r"(oauth\s*2\.0|owasp\s+top\s+10|http/?[12]|python\s*3|pydantic\s+v2|s3|ec2|"
    r"top\s+10|utf-8|base64|sha-?256|oauth2)",
    re.I,
)
_NUMBER = re.compile(r"\b\d[\d,.]*\b")


def _numbers_in(text: str) -> set[str]:
    """Numbers a reader would take as a claim, excluding version/product numbers."""
    cleaned = _TECH_EMBEDDED_NUMBERS.sub(" ", text)
    return {m.group(0).rstrip(".") for m in _NUMBER.finditer(cleaned)}


def v1_technologies(bullet: Bullet, rewritten: str) -> list[Violation]:
    """Only technologies this bullet declares may appear in it.

    Scoped to the bullet, not the document. This is the check that catches
    cross-provider substitution — the measured dominant failure mode, where a
    GCP-flavoured job pulls Azure and AWS into a rewrite that had neither.
    """
    introduced = tokens_in(rewritten) - bullet.tech
    if not introduced:
        return []
    return [
        Violation(
            "v1-technology",
            bullet.id,
            f"introduces {sorted(introduced)} — this bullet only evidences "
            f"{sorted(bullet.tech)}",
        )
    ]


def v2_numbers(bullet: Bullet, rewritten: str) -> list[Violation]:
    """Only declared numbers may appear, and each must stay on its own subject."""
    out: list[Violation] = []
    found = _numbers_in(rewritten)
    allowed = bullet.allowed_numbers
    invented = {n for n in found if n.replace(",", "") not in
                {a.replace(",", "") for a in allowed}}
    if invented:
        out.append(
            Violation("v2-number", bullet.id,
                      f"states {sorted(invented)}, which is not a declared metric")
        )
    for metric in bullet.metrics:
        for number in metric.numbers:
            if number in rewritten and not metric.mentions_subject(rewritten, number):
                out.append(
                    Violation(
                        "v2-subject", bullet.id,
                        f"uses {number!r} but {metric.subject_head!r} is not near it — "
                        f"the number has been moved onto a different claim",
                    )
                )
                break
    return out


def v3_temporal(bullet: Bullet, rewritten: str, released: dict[str, str]) -> list[Violation]:
    """A bullet may not name a technology released after the role began."""
    if not bullet.entry_start:
        return []
    out: list[Violation] = []
    for token in tokens_in(rewritten):
        ga = released.get(token)
        if ga and ga > bullet.entry_start:
            out.append(
                Violation("v3-temporal", bullet.id,
                          f"{token} was released {ga}, after this role began "
                          f"{bullet.entry_start}")
            )
    return out


def v4_structure(originals: list[Bullet], rewrites: dict[str, str]) -> list[Violation]:
    """Exact bullet-ID bijection: nothing added, dropped, merged or relocated."""
    expected = {b.id for b in originals}
    got = set(rewrites)
    out: list[Violation] = []
    for missing in sorted(expected - got):
        out.append(Violation("v4-structure", missing, "bullet is missing from the rewrite"))
    for extra in sorted(got - expected):
        out.append(Violation("v4-structure", extra, "bullet was invented by the rewrite"))
    for bullet in originals:
        if bullet.id in rewrites and not rewrites[bullet.id].strip():
            out.append(Violation("v4-structure", bullet.id, "bullet was emptied"))
    return out


_PLACEHOLDER = re.compile(r"\[[A-Z]\]|\[X+\]|\bTODO\b|\bTBD\b")


def v5_placeholders(bullet: Bullet, rewritten: str) -> list[Violation]:
    """A placeholder is for a human to fill; it must never reach a PDF."""
    found = _PLACEHOLDER.findall(rewritten)
    if not found:
        return []
    return [Violation("v5-placeholder", bullet.id, f"unfilled placeholder(s) {found}")]


def v6_width(bullet: Bullet, rewritten: str, budget: int) -> list[Violation]:
    """Stay inside the measured character budget for this bullet.

    A character budget, not a percentage. Measured slack on the real résumé is
    ~0.4 of one line box, and per-bullet headroom ranges from +1 to +89
    characters — so a ±10% rule is wrong by up to 4x in both directions. On one
    real bullet, +6 characters produces a two-page résumé.
    """
    delta = len(rewritten) - len(bullet.text)
    if delta <= budget:
        return []
    return [
        Violation("v6-width", bullet.id,
                  f"grew by {delta} characters; the measured budget is +{budget}")
    ]


def validate(
    originals: list[Bullet],
    rewrites: dict[str, str],
    *,
    released: dict[str, str] | None = None,
    budgets: dict[str, int] | None = None,
) -> ValidationResult:
    """Run every validator. Any violation means no résumé is produced."""
    result = ValidationResult()
    result.violations.extend(v4_structure(originals, rewrites))

    by_id = {b.id: b for b in originals}
    for bullet_id, rewritten in rewrites.items():
        bullet = by_id.get(bullet_id)
        if bullet is None:
            continue  # already reported by v4
        result.violations.extend(v1_technologies(bullet, rewritten))
        result.violations.extend(v2_numbers(bullet, rewritten))
        result.violations.extend(v3_temporal(bullet, rewritten, released or {}))
        result.violations.extend(v5_placeholders(bullet, rewritten))
        if budgets and bullet_id in budgets:
            result.violations.extend(v6_width(bullet, rewritten, budgets[bullet_id]))
    return result


#: Vendor GA dates, for v3. Only tokens where an anachronism is plausible.
RELEASED: dict[str, str] = {
    "FastAPI": "2018-12",
    "Next.js": "2016-10",
    "Kubernetes": "2015-07",
    "Terraform": "2014-07",
    "DynamoDB": "2012-01",
    "Lambda": "2014-11",
    "EventBridge": "2019-07",
    "Flutter": "2018-12",
    "TypeScript": "2012-10",
    "pytest": "2009-11",
    "gRPC": "2016-08",
    "GraphQL": "2015-07",
}

#: Sanity check: every RELEASED key must be a real vocabulary token.
assert set(RELEASED) <= set(VOCAB), sorted(set(RELEASED) - set(VOCAB))

_TODAY = date.today().isoformat()[:7]


# ---------------------------------------------------------------------------
# The checks below exist because 21 adversarial rewrites passed v1-v6 — every
# single one. The reason: v1-v6 are a fence around *additions*, and every
# successful attack instead deleted a word, renamed a noun, reused a digit that
# was part of a name, or strengthened a verb. Addition was a whitelist; deletion
# and rearrangement were a blacklist.
# ---------------------------------------------------------------------------

_WORD = re.compile(r"[A-Za-z][A-Za-z'’\-]*")


def v7_lead_verb(bullet: Bullet, rewritten: str) -> list[Violation]:
    """The first word must come from an explicit **ceiling** of allowed verbs.

    An allowlist, not a blacklist. The previous 5-item blacklist
    (led/owned/architected/drove/responsible-for) let ``Directed``,
    ``Established``, ``Orchestrated``, ``Headed``, ``Pioneered``,
    ``Single-handedly`` and ``Solely`` straight through.
    """
    if not bullet.allowed_verbs:
        return []
    first = _WORD.search(rewritten)
    if first is None:
        return [Violation("v7-verb", bullet.id, "rewrite has no leading word")]
    verb = first.group(0)
    if verb in bullet.allowed_verbs:
        return []
    return [
        Violation("v7-verb", bullet.id,
                  f"leads with {verb!r}; the ceiling for this bullet is "
                  f"{sorted(bullet.allowed_verbs)}")
    ]


def v8_must_keep(bullet: Bullet, rewritten: str) -> list[Violation]:
    """Protected strings must survive character-identical.

    This is the check for **fabrication by omission**. Deleting ``Benchmark:``
    turns a local measurement into a claimed production capability while adding
    zero words; deleting ``transactional outbox`` and ``dead-letter queue``
    removes the evidence that made the bullet true.
    """
    missing = [s for s in bullet.must_keep if s not in rewritten]
    if not missing:
        return []
    return [
        Violation("v8-must-keep", bullet.id,
                  f"dropped protected text {missing} — deleting a mechanism or a "
                  f"qualifier fabricates by omission")
    ]


def v9_borrowed_digits(bullet: Bullet, rewritten: str) -> list[Violation]:
    """A declared number may not appear more often than it does in the source.

    ``10`` is declared only because "OWASP Top 10" is the *name* of a standard.
    Counting occurrences is what stops it being reused as "across 10 services":
    the source has it once, the attack has it twice.
    """
    out: list[Violation] = []
    for number in sorted(bullet.allowed_numbers):
        before = bullet.text.count(number)
        after = rewritten.count(number)
        if after > before:
            out.append(
                Violation("v9-borrowed-digit", bullet.id,
                          f"{number!r} appears {after}x but only {before}x in the source — "
                          f"a declared number has been reused as a new claim")
            )
    return out


def v10_new_proper_nouns(bullet: Bullet, rewritten: str) -> list[Violation]:
    """No proper noun or acronym that is not already in the source text.

    ``tokens_in`` is a ~70-token closed vocabulary, so ``SQS``, ``API Gateway``,
    ``CloudWatch``, ``Kinesis`` and ``Step Functions`` are invisible to v1 — which
    means v1 cannot actually enforce "only declared technologies". This is the
    open-world backstop: anything capitalised that was not there before is flagged
    for a human, because a product name is the cheapest possible fabrication.
    """
    words = _WORD.findall(rewritten)
    if not words:
        return []
    source_words = set(_WORD.findall(bullet.text))
    suspicious = {
        w for w in words[1:]  # skip the leading verb, which v7 governs
        if (w[0].isupper() or w.isupper()) and w not in source_words and len(w) > 1
    }
    if not suspicious:
        return []
    return [
        Violation("v10-proper-noun", bullet.id,
                  f"introduces capitalised term(s) {sorted(suspicious)} not present in the "
                  f"source — if legitimate, add it to this bullet's declarations first")
    ]


def validate_strict(
    originals: list[Bullet],
    rewrites: dict[str, str],
    *,
    released: dict[str, str] | None = None,
    budgets: dict[str, int] | None = None,
) -> ValidationResult:
    """Every validator, v1 through v10. This is the one callers should use."""
    result = validate(originals, rewrites, released=released, budgets=budgets)
    by_id = {b.id: b for b in originals}
    for bullet_id, rewritten in rewrites.items():
        bullet = by_id.get(bullet_id)
        if bullet is None:
            continue
        result.violations.extend(v7_lead_verb(bullet, rewritten))
        result.violations.extend(v8_must_keep(bullet, rewritten))
        result.violations.extend(v9_borrowed_digits(bullet, rewritten))
        result.violations.extend(v10_new_proper_nouns(bullet, rewritten))
    return result
