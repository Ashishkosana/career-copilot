"""A bank of **authored** bullet variants, and a selector that picks one per bullet.

Tailoring here is selection, never generation. Two measured reasons:

* The layers that decide an outcome score on *which facts are present*, not on
  phrasing. Greenhouse keyword matching is exact-match boolean, and Jobscan's own
  documentation excludes word count and "measurable results" from its match rate.
  So rewriting the same facts buys nothing a screener can see.
* Rewriting has a real downside. 16 adversarial rewrites passed the original six
  validators and **6 still pass all ten** — each swapping one lowercase common word
  for another, which no set-theoretic check can catch (see
  ``tests/test_tailoring_attacks.py``). Every uncaught one is a sentence he would
  have to defend in an interview without having written it.

So the user writes several variants of a bullet **once**, by hand, and this module
**picks** one per posting. Every sentence in the rendered résumé is his.

Three design points worth stating, because each one cost something:

1. A variant is declared with the **same keys as a bullet** and is materialised as
   a :class:`~copilot.domain.tailoring.Bullet`, so the existing validators apply
   unchanged and there is no parallel scheme to keep in sync. Anything a variant
   omits is inherited from the bullet-level declarations.
2. Coverage is measured with ``tokens_in(variant.text)`` — what the *text* says —
   not with the declared ``tech`` allowlist. ``tech`` is a ceiling on what may
   appear, not a promise that it does: ``exp.crewtron.b5`` declares ``Git`` and
   ``CI/CD`` but its text says "GitHub Actions", which ``VOCAB`` does not match. If
   coverage read the allowlist, the selector would report a requirement covered
   that the rendered document does not contain, and ``gap.build_report`` — which
   tokenises the rendered résumé — would then disagree with it.
3. Selection order is **sorted bullet id**, never document or dict order, so
   reordering entries in the JSON cannot silently change which variant ships.

What this module deliberately does not do: rewrite prose, call an LLM, or decide
whether a variant is honest. That last one is authoring-time human work.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from copilot.domain.gap import VOCAB, tokens_in
from copilot.domain.tailoring import (
    Bullet,
    Metric,
    ValidationResult,
    Violation,
    v1_technologies,
    v2_numbers,
    v6_width,
    v8_must_keep,
)

#: Case-insensitive lookup onto the canonical vocabulary. A caller that passes
#: ``"python"`` instead of ``"Python"`` would otherwise get a silent miss —
#: requirement sets arrive from ``GapReport`` (canonical) *and* from humans typing
#: at a CLI (not), and a miss looks identical to "no variant covers it".
_CANONICAL: dict[str, str] = {token.lower(): token for token in VOCAB}


class BankError(ValueError):
    """The bank document is structurally unusable — bad keys, ids or empty text."""


class BankValidationError(BankError):
    """A variant contradicts its own declarations, so the bank is rejected at load.

    Rejecting here rather than at render is the whole point: a badly authored
    variant that only fails when a résumé is being produced fails at the moment the
    user is applying to something, and the tempting fix at that moment is to ship
    it anyway.
    """

    def __init__(self, result: ValidationResult) -> None:
        super().__init__(result.report())
        self.result = result


@dataclass(frozen=True)
class BulletVariant:
    """One authored phrasing of a bullet, carrying its own declarations.

    ``bullet.id`` is the **bullet's** id, not the variant's, so a chosen variant is
    a drop-in for ``validate``/``validate_strict`` and for a renderer that keys by
    bullet id. The variant's own identity lives in ``variant_id``.
    """

    variant_id: str
    bullet: Bullet
    label: str = ""
    #: ``tokens_in(text)``, precomputed. Filled in ``__post_init__``; do not pass.
    tokens: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        # ~70 boundary-anchored regexes per call, and the selector touches every
        # variant of every bullet on every posting. Pay it once, at load.
        object.__setattr__(self, "tokens", frozenset(tokens_in(self.bullet.text)))

    @property
    def text(self) -> str:
        return self.bullet.text

    def covers(self, requirements: frozenset[str]) -> frozenset[str]:
        """Which of ``requirements`` this variant's text actually states."""
        return self.tokens & requirements


@dataclass(frozen=True)
class BankBullet:
    """One résumé line and every authored way of saying it.

    ``variants[0]`` is the **baseline**: the wording currently on the résumé. It is
    the reference for the character budget (matching ``v6_width``'s delta
    semantics) and it wins every tie, so a posting that gives no signal produces
    the résumé he already has.
    """

    id: str
    variants: tuple[BulletVariant, ...]

    @property
    def baseline(self) -> BulletVariant:
        return self.variants[0]


@dataclass(frozen=True)
class BankEntry:
    """A role or project. Carries the dates the temporal validator needs."""

    id: str
    kind: str
    role: str
    org: str
    date_start: str
    date_end: str | None
    bullets: tuple[BankBullet, ...]


@dataclass(frozen=True)
class Bank:
    """Every entry, every bullet, every authored variant — already validated."""

    entries: tuple[BankEntry, ...]
    schema_version: int = 1

    @property
    def bullets(self) -> tuple[BankBullet, ...]:
        """Document order. Selection does not use this order — see ``select``."""
        return tuple(b for entry in self.entries for b in entry.bullets)

    def bullet(self, bullet_id: str) -> BankBullet:
        for bullet in self.bullets:
            if bullet.id == bullet_id:
                return bullet
        raise KeyError(bullet_id)

    def baseline_texts(self) -> dict[str, str]:
        """The résumé as it stands today, keyed by bullet id."""
        return {b.id: b.baseline.text for b in self.bullets}

    def coverable(self) -> frozenset[str]:
        """Every requirement any variant could surface.

        A requirement outside this set is an authoring gap, not a selection
        failure — no amount of picking will produce it.
        """
        return frozenset().union(*(v.tokens for b in self.bullets for v in b.variants))


# ---------------------------------------------------------------------------
# Loading. Pure: the caller does the file I/O and hands over a parsed document,
# because domain/ has no I/O.
# ---------------------------------------------------------------------------


def _obj(value: object, where: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise BankError(f"{where}: expected an object, got {type(value).__name__}")
    return value


def _seq(value: object, where: str) -> Sequence[Any]:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        raise BankError(f"{where}: expected a list, got {type(value).__name__}")
    return value


def _text(node: Mapping[str, Any], key: str, where: str) -> str:
    value = node.get(key)
    if not isinstance(value, str) or not value.strip():
        raise BankError(f"{where}: {key!r} must be a non-empty string")
    return value


def _strs(node: Mapping[str, Any], key: str, where: str) -> tuple[str, ...]:
    raw = node.get(key, [])
    items = _seq(raw, f"{where}.{key}")
    for item in items:
        if not isinstance(item, str):
            raise BankError(f"{where}.{key}: every item must be a string")
    return tuple(str(item) for item in items)


def _metrics(node: Mapping[str, Any], where: str) -> tuple[Metric, ...]:
    out: list[Metric] = []
    for i, raw in enumerate(_seq(node.get("metrics", []), f"{where}.metrics")):
        m = _obj(raw, f"{where}.metrics[{i}]")
        at = f"{where}.metrics[{i}]"
        out.append(
            Metric(
                id=_text(m, "id", at),
                numbers=_strs(m, "numbers", at),
                unit=_text(m, "unit", at),
                subject_head=_text(m, "subject_head", at),
                claim=_text(m, "claim", at),
                evidence=_text(m, "evidence", at),
            )
        )
    return tuple(out)


def _variant(
    node: Mapping[str, Any],
    *,
    bullet_id: str,
    entry_start: str,
    entry_end: str | None,
    variant_id: str,
    defaults: Mapping[str, Any] | None,
    where: str,
) -> BulletVariant:
    """Build one variant, inheriting anything it does not declare.

    Inheritance is what keeps authoring cheap: a variant that only rephrases needs
    a ``text`` and nothing else. It is also why ``must_keep_verbatim`` inheritance
    can reject a variant that deliberately drops a protected phrase — that is the
    intended behaviour. The author must then say so explicitly in the variant,
    which forces the question "is the claim still true without it?".
    """
    base: Mapping[str, Any] = defaults or {}

    def declared(key: str) -> Mapping[str, Any]:
        return node if key in node else base

    return BulletVariant(
        variant_id=variant_id,
        label=str(node.get("label", "")),
        bullet=Bullet(
            id=bullet_id,
            text=_text(node, "text", where),
            tech=frozenset(_strs(declared("tech"), "tech", where)),
            metrics=_metrics(declared("metrics"), where),
            entry_start=entry_start,
            entry_end=entry_end,
            allowed_verbs=frozenset(_strs(declared("allowed_verbs"), "allowed_verbs", where)),
            must_keep=_strs(declared("must_keep_verbatim"), "must_keep_verbatim", where),
        ),
    )


def validate_variant(variant: BulletVariant) -> list[Violation]:
    """Check an authored variant against **its own** declarations: v1, v2, v8.

    Only three of the ten apply, and the reason is that the other seven compare a
    rewrite against a source. Here the variant *is* the source:

    * v9 (borrowed digits) and v10 (new proper nouns) count occurrences against
      ``bullet.text``, which is this same string — tautologically satisfied.
    * v4 is a document-level bijection, and v6 is the character budget, which is a
      per-posting constraint applied in ``select`` rather than a property of the
      authored text.
    * v3 (temporal) and v7 (verb ceiling) are meaningful here but need a released
      table and a policy call the orchestrator owns; ``v1/v2/v8`` are the three
      that catch a variant that contradicts what it declares about *itself* —
      naming a technology it does not evidence, stating an undeclared number or one
      moved onto the wrong subject, or dropping a phrase it promised to keep.

    Violations are re-keyed to the **variant** id, because "bullet b1 is invalid"
    does not tell an author which of five phrasings to fix.
    """
    b = variant.bullet
    raw = v1_technologies(b, b.text) + v2_numbers(b, b.text) + v8_must_keep(b, b.text)
    return [Violation(v.validator, variant.variant_id, v.detail) for v in raw]


def load_bank(doc: Mapping[str, Any]) -> Bank:
    """Parse and validate a bank document (``private/resume/content.json`` shape).

    A bullet with no ``variants`` key is a one-variant bullet, so the résumé
    content authored before this module existed loads unchanged — the migration is
    the empty diff.

    Raises ``BankError`` for a structural problem and ``BankValidationError`` if
    any variant contradicts its own declarations.
    """
    entries_raw = _seq(_obj(doc, "bank").get("entries", []), "bank.entries")
    entries: list[BankEntry] = []
    violations: list[Violation] = []
    seen_bullets: set[str] = set()
    seen_variants: set[str] = set()

    for ei, entry_raw in enumerate(entries_raw):
        entry = _obj(entry_raw, f"entries[{ei}]")
        where_entry = f"entries[{ei}]"
        entry_id = _text(entry, "id", where_entry)
        date_start = str(entry.get("date_start", ""))
        date_end_raw = entry.get("date_end")
        date_end = str(date_end_raw) if isinstance(date_end_raw, str) else None

        bullets: list[BankBullet] = []
        for bi, bullet_raw in enumerate(_seq(entry.get("bullets", []), f"{where_entry}.bullets")):
            node = _obj(bullet_raw, f"{where_entry}.bullets[{bi}]")
            bullet_id = _text(node, "id", f"{where_entry}.bullets[{bi}]")
            if bullet_id in seen_bullets:
                raise BankError(f"duplicate bullet id {bullet_id!r}")
            seen_bullets.add(bullet_id)

            variants = [
                _variant(
                    node,
                    bullet_id=bullet_id,
                    entry_start=date_start,
                    entry_end=date_end,
                    variant_id=f"{bullet_id}#base",
                    defaults=None,
                    where=bullet_id,
                )
            ]
            extra = _seq(node.get("variants", []), f"{bullet_id}.variants")
            for vi, variant_raw in enumerate(extra):
                vnode = _obj(variant_raw, f"{bullet_id}.variants[{vi}]")
                vid = str(vnode.get("id") or f"{bullet_id}#v{vi + 1}")
                variants.append(
                    _variant(
                        vnode,
                        bullet_id=bullet_id,
                        entry_start=date_start,
                        entry_end=date_end,
                        variant_id=vid,
                        defaults=node,
                        where=vid,
                    )
                )

            for variant in variants:
                if variant.variant_id in seen_variants:
                    raise BankError(f"duplicate variant id {variant.variant_id!r}")
                seen_variants.add(variant.variant_id)
                violations.extend(validate_variant(variant))

            bullets.append(BankBullet(id=bullet_id, variants=tuple(variants)))

        entries.append(
            BankEntry(
                id=entry_id,
                kind=str(entry.get("kind", "")),
                role=str(entry.get("role", "")),
                org=str(entry.get("org", "")),
                date_start=date_start,
                date_end=date_end,
                bullets=tuple(bullets),
            )
        )

    if violations:
        raise BankValidationError(ValidationResult(violations))

    version = doc.get("schema_version", 1)
    return Bank(entries=tuple(entries), schema_version=int(version) if version else 1)


# ---------------------------------------------------------------------------
# Selection.
# ---------------------------------------------------------------------------


def normalise_requirements(requirements: Iterable[str]) -> frozenset[str]:
    """Map requirement strings onto canonical ``VOCAB`` tokens where possible.

    Anything unrecognised is kept as-is rather than dropped, so it shows up in
    ``Selection.uncovered`` instead of vanishing: "no bullet can surface this" is
    information the user needs, and a silently discarded requirement looks
    identical to a satisfied one.
    """
    out: set[str] = set()
    for raw in requirements:
        token = raw.strip()
        if token:
            out.add(_CANONICAL.get(token.lower(), token))
    return frozenset(out)


@dataclass(frozen=True)
class Choice:
    """Which variant was chosen for one bullet, and why. The audit record."""

    bullet_id: str
    variant_id: str
    text: str
    #: Requirements this variant's text states.
    covered: tuple[str, ...]
    #: Of those, the ones no earlier-chosen variant had already surfaced. This is
    #: the quantity the selector actually maximises.
    newly_covered: tuple[str, ...]
    #: Characters relative to the baseline variant — the same delta ``v6_width``
    #: measures.
    delta_chars: int
    budget: int | None
    within_budget: bool
    is_baseline: bool
    reason: str

    def line(self) -> str:
        tag = "baseline" if self.is_baseline else "switched"
        return (
            f"  {self.bullet_id}: {self.variant_id} [{tag}] {self.delta_chars:+d} chars\n"
            f"      {self.reason}"
        )


@dataclass(frozen=True)
class Selection:
    """One variant per bullet, plus everything needed to defend the choice."""

    choices: tuple[Choice, ...]
    requirements: frozenset[str]

    @property
    def covered(self) -> frozenset[str]:
        return frozenset(t for c in self.choices for t in c.covered)

    @property
    def uncovered(self) -> frozenset[str]:
        """Requirements no chosen variant states — an authoring gap, not a bug."""
        return self.requirements - self.covered

    @property
    def overflowed(self) -> tuple[Choice, ...]:
        """Bullets where no variant fit the budget and the shortest was used."""
        return tuple(c for c in self.choices if not c.within_budget)

    def texts(self) -> dict[str, str]:
        """bullet id -> chosen text. What a renderer consumes."""
        return {c.bullet_id: c.text for c in self.choices}

    def variant_ids(self) -> dict[str, str]:
        return {c.bullet_id: c.variant_id for c in self.choices}

    def report(self) -> str:
        """Plain text an author can read before rendering anything."""
        total = len(self.requirements)
        head = (
            f"covers {len(self.covered)} of {total} requirements"
            if total
            else "no requirements given — baseline résumé, unchanged"
        )
        lines = [head, ""]
        lines.extend(c.line() for c in self.choices)
        if self.uncovered:
            gaps = ", ".join(sorted(self.uncovered))
            lines += ["", f"  not covered by any chosen variant: {gaps}"]
        if self.overflowed:
            ids = ", ".join(c.bullet_id for c in self.overflowed)
            lines += ["", f"  OVER BUDGET, shortest variant used: {ids}"]
        return "\n".join(lines)


def _fits(baseline: BulletVariant, candidate: BulletVariant, budget: int | None) -> bool:
    """Is ``candidate`` inside this bullet's character budget?

    Delegates to ``v6_width`` so there is exactly one definition of "fits" in the
    codebase. ``None`` means the budget was never measured for this bullet, and
    an unmeasured budget cannot be enforced — the caller sees ``budget=None`` on
    the Choice and can refuse to render.
    """
    if budget is None:
        return True
    return not v6_width(baseline.bullet, candidate.text, budget)


def _best(
    fitting: Sequence[tuple[int, BulletVariant]],
    wanted: frozenset[str],
    covered: frozenset[str],
) -> tuple[int, BulletVariant]:
    """The greedy pick, with the tie-break rule in one place.

    Keyed on ``(-newly covered, authored index)``, and both halves are deliberate:

    * **Only new coverage counts.** Restating a requirement an earlier bullet
      already states is not a tiebreak, because Greenhouse keyword matching is
      exact-match boolean — a second mention changes no screener's answer, so it
      would be churn in exchange for nothing measurable.
    * **Authored index last** means the baseline (index 0, the wording already on
      the résumé) wins every tie. A posting that gives no signal therefore returns
      the résumé he already has.

    Length is *not* a tiebreak; it is a hard constraint enforced before this
    function is reached. ``covered`` is passed explicitly rather than closed over,
    so nothing here reads a set the caller is mutating mid-loop — that is how a
    "deterministic" selector quietly stops being one.
    """

    def rank(item: tuple[int, BulletVariant]) -> tuple[int, int]:
        index, variant = item
        return (-len(variant.covers(wanted) - covered), index)

    return min(fitting, key=rank)


def _reason(
    *,
    within_budget: bool,
    budget: int | None,
    delta: int,
    new: frozenset[str],
    covered: frozenset[str],
) -> str:
    # ``budget is None`` means unmeasured, and an unmeasured budget always fits, so
    # this pair can only occur together. Narrowed explicitly rather than asserted.
    if not within_budget and budget is not None:
        return (
            f"no variant fits a {budget:+d}-character budget; fell back to the shortest "
            f"({delta:+d}) rather than overflow the page"
        )
    if new:
        return f"surfaces {', '.join(sorted(new))}, not stated by any earlier bullet"
    if covered:
        return (
            f"states {', '.join(sorted(covered))}, already covered earlier; "
            f"no variant adds an uncovered requirement"
        )
    return "no variant states any of this posting's requirements"


def select(
    bank: Bank,
    requirements: Iterable[str],
    budgets: Mapping[str, int] | None = None,
) -> Selection:
    """Pick one variant per bullet to maximise coverage of ``requirements``.

    **Algorithm.** Greedy marginal-gain over bullets in sorted-id order. For each
    bullet, from the variants that fit the budget, take the one adding the most
    *not-yet-covered* requirements. Maximising the union exactly is max-coverage,
    which is NP-hard, and greedy is the standard ``1 - 1/e`` approximation; the
    alternative — per-bullet argmax — is not even that, since it happily spends two
    bullets on the same token. Sorted-id order (not document, not dict order) is
    what makes the outcome independent of how the JSON happens to be arranged.

    **Ties** break on **authored order**, so the baseline wins — see ``_best`` for
    why redundant coverage and length are both excluded from the key. Consequence:
    a posting with no signal returns the résumé he already has, which is the
    correct default for a document he has to defend.

    **Budget** is headroom in characters over the baseline variant, exactly
    ``v6_width``'s delta. It may be negative, meaning this bullet has to *shrink*
    to keep the résumé on one page — that is the case where every variant can miss,
    and then the shortest variant is used and flagged in ``Selection.overflowed``.
    Measured slack on the real résumé is ~0.4 of a line box and per-bullet headroom
    ranges +1 to +89 characters; on one bullet +6 characters produces a two-page
    résumé, so overflowing is never the lesser evil.

    **Complexity.** ``O(B log B + Σ_b V_b · R)`` — one pass over every variant of
    every bullet, intersecting a precomputed token set with the requirements.
    Tokenisation happens once at load, not here.
    """
    wanted = normalise_requirements(requirements)
    covered_so_far: set[str] = set()
    choices: list[Choice] = []

    for bullet in sorted(bank.bullets, key=lambda b: b.id):
        budget = budgets.get(bullet.id) if budgets else None
        baseline = bullet.baseline
        fitting = [
            (i, v) for i, v in enumerate(bullet.variants) if _fits(baseline, v, budget)
        ]
        within_budget = bool(fitting)
        if not fitting:
            # Nothing fits. Shortest text wins, authored order breaks a length tie.
            fitting = [min(enumerate(bullet.variants), key=lambda iv: (len(iv[1].text), iv[0]))]

        index, chosen = _best(fitting, wanted, frozenset(covered_so_far))
        cov = chosen.covers(wanted)
        new = cov - covered_so_far
        delta = len(chosen.text) - len(baseline.text)
        choices.append(
            Choice(
                bullet_id=bullet.id,
                variant_id=chosen.variant_id,
                text=chosen.text,
                covered=tuple(sorted(cov)),
                newly_covered=tuple(sorted(new)),
                delta_chars=delta,
                budget=budget,
                within_budget=within_budget,
                is_baseline=index == 0,
                reason=_reason(
                    within_budget=within_budget,
                    budget=budget,
                    delta=delta,
                    new=new,
                    covered=cov,
                ),
            )
        )
        covered_so_far |= cov

    return Selection(choices=tuple(choices), requirements=wanted)
