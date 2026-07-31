"""Port for the one judgement rules cannot make: what level is *this* role?

``domain.seniority`` resolves a band from the title, and from a years-of-experience
regex when the title says nothing. That covers most postings and it is the tier
that should stay deterministic. It runs out on the 652 eligible postings whose
title is a bare "Software Engineer" and whose description never states a number:
no rule distinguishes a new-grad role from a ten-year role there, and the
description is the only place the answer exists. That — and only that — is what
this port is for. Everything upstream and downstream of it stays deterministic.

Two shape decisions matter, and both exist to keep an LLM answer *checkable*:

**A discrete band and a quoted span, never a number and never prose.** The
implementation returns a :class:`~copilot.domain.seniority.Level` plus the
substring of the description that decided it. A free-text rationale cannot be
verified by anything but a human; a span can be looked up in the source text by
a checker. So the contract asks for the span.

**A span is an index, not proof.** Production citation systems fully support only
about half the sentences they cite, so an adapter must confirm the span is
actually present in the description and say so when it is not — which is what
:attr:`Interpretation.evidence_verified` and the confidence downgrade record.
A caller must be able to tell "the model was unsure" apart from "the model quoted
something that is not in the posting".

Confidence is a named tier, not a probability. A model-emitted ``0.87`` is not
calibrated against anything, and this codebase already refuses to report bare
percentages (see ``domain.gap.score_report``); three tiers carry the same
information without inviting arithmetic on it.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, ClassVar, Protocol

from copilot.domain.posting import Posting
from copilot.domain.seniority import Level


class Confidence(StrEnum):
    """How much weight a caller should put on an interpretation."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"

    def downgraded(self) -> Confidence:
        """One tier lower, floored at ``LOW``.

        Used by adapters when a check fails — an unverifiable span or a band that
        contradicts the years it was derived from. Downgrading rather than
        discarding keeps the answer available while marking it as weaker than the
        model claimed, which is the honest reading of a failed check.
        """
        order = (Confidence.LOW, Confidence.MEDIUM, Confidence.HIGH)
        return order[max(0, order.index(self) - 1)]


@dataclass(frozen=True)
class Interpretation:
    """A level read out of a description, with the span that decided it."""

    band: Level
    min_years: int | None = None
    #: Verbatim substring of the description. Empty when the description states
    #: nothing — which is a legitimate answer, not a failure.
    evidence: str = ""
    confidence: Confidence = Confidence.LOW
    #: ``False`` when :attr:`evidence` was *not* found in the description. Kept
    #: separate from :attr:`confidence` because a low-confidence honest answer and
    #: a fabricated quote are different problems and a UI must not conflate them.
    evidence_verified: bool = True

    #: Bumped when the cached payload shape changes. ``from_payload`` refuses any
    #: other value, so an old cache row degrades to a cache miss instead of
    #: deserialising into a wrong-shaped object.
    PAYLOAD_SCHEMA: ClassVar[int] = 1

    def to_payload(self) -> dict[str, Any]:
        """JSON-safe dict for :meth:`PostingStorePort.save_interpretation`."""
        return {
            "schema": self.PAYLOAD_SCHEMA,
            "band": self.band.value,
            "min_years": self.min_years,
            "evidence": self.evidence,
            "confidence": self.confidence.value,
            "evidence_verified": self.evidence_verified,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any] | None) -> Interpretation | None:
        """Rebuild from a cache row, or ``None`` if the row is unusable.

        Total rather than raising: the cache is a persisted file written by an
        older version of this code, and a daily cron must survive a row it does
        not recognise. Every failure here is a cache miss, never a crash.
        """
        if not payload or payload.get("schema") != cls.PAYLOAD_SCHEMA:
            return None
        try:
            band = Level(payload["band"])
            confidence = Confidence(payload["confidence"])
        except (KeyError, ValueError):
            return None
        years = payload.get("min_years")
        if years is not None and not isinstance(years, int):
            return None
        evidence = payload.get("evidence")
        return cls(
            band=band,
            min_years=years,
            evidence=evidence if isinstance(evidence, str) else "",
            confidence=confidence,
            evidence_verified=bool(payload.get("evidence_verified", False)),
        )


class InterpreterPort(Protocol):
    """Read a seniority band out of a posting's description.

    Implementations must return ``None`` — never guess and never raise — when they
    cannot answer: no credentials, no description, an API failure, a malformed
    reply. ``None`` means "I have nothing to add", and the caller keeps the
    rule-based verdict from ``domain.seniority.decide_level``. That is what makes
    this tier optional: the pipeline is fully functional without it.
    """

    def interpret(self, posting: Posting) -> Interpretation | None:
        """Level for one posting, or ``None`` when unavailable."""
        ...

    def interpret_many(self, postings: Sequence[Posting]) -> dict[str, Interpretation]:
        """Levels keyed by ``Posting.id``, omitting postings with no answer."""
        ...
