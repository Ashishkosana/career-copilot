"""The adversarial regression suite.

Every case here is a rewrite of one of Ashish's **real** bullets that is false or
materially inflated. They exist because a red team wrote 16 of them and **all 16
passed the original six validators** — after I had tested with five attacks of my
own invention and reported four caught. Validating your own work against your own
assumptions is worth close to nothing; these are the attacks someone else wrote
while trying to break it.

Two classes, and the split is the whole point:

* ``BLOCKED`` — caught by v1-v10. If one of these ever starts passing, a validator
  has regressed and this suite fails.
* ``KNOWN_UNCAUGHT`` — **still passes every validator.** Each swaps one lowercase
  common word for another: no new technology, no new number, no stronger verb,
  nothing deleted. Catching them needs a judgement about whether claim strength
  increased, which is semantic rather than set-theoretic. They are asserted to
  *still pass*, so that if a future validator catches one, this suite fails loudly
  and tells us to promote it — and so that nobody reads a green suite as "safe".

The honest consequence: a human must read a word-level diff before any render.
These six are why.
"""
from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from copilot.domain.tailoring import (
    RELEASED,
    Bullet,
    Metric,
    validate,
    validate_strict,
)

#: The bullet library, keyed by id.
Bullets = dict[str, Bullet]
#: An attack: given the real bullets, return one rewritten bullet's text. Typed as
#: a Callable rather than ``object`` so ``--strict`` checks the call sites — an
#: attack list whose third element is not callable is a suite that silently
#: asserts nothing.
Attack = Callable[[Bullets], str]

# tests/ -> backend/ -> career-copilot/ ; parents[2] is the repo root.
CONTENT = Path(__file__).resolve().parents[2] / "private" / "resume" / "content.json"

pytestmark = pytest.mark.skipif(
    not CONTENT.exists(),
    reason="private/resume/content.json is personal and gitignored; see private.example/",
)


def _bullets() -> dict[str, Bullet]:
    doc = json.loads(CONTENT.read_text())
    out: dict[str, Bullet] = {}
    for entry in doc["entries"]:
        for b in entry["bullets"]:
            out[b["id"]] = Bullet(
                id=b["id"],
                text=b["text"],
                tech=frozenset(b["tech"]),
                metrics=tuple(
                    Metric(
                        id=m["id"], numbers=tuple(m["numbers"]), unit=m["unit"],
                        subject_head=m["subject_head"], claim=m["claim"],
                        evidence=m["evidence"],
                    )
                    for m in b["metrics"]
                ),
                entry_start=entry["date_start"],
                allowed_verbs=frozenset(b["allowed_verbs"]),
                must_keep=tuple(b["must_keep_verbatim"]),
            )
    return out


@pytest.fixture(scope="module")
def bullets() -> Bullets:
    return _bullets()


def _swap(bullets: Bullets, bid: str, old: str, new: str) -> str:
    text = bullets[bid].text
    assert old in text, f"{bid}: attack setup is stale, {old!r} not in the source"
    return text.replace(old, new, 1)


# --- attacks the validators DO catch ----------------------------------------
# (id, bullet, builder) — builder takes the bullet map and returns the rewrite.
BLOCKED: list[tuple[str, str, Attack]] = [
    ("A1-verb-and-noun-enlargement", "exp.crewtron.b2",
     lambda b: "Established the REST API platform secured with AWS Cognito, provisioned "
               "the backend as code with AWS CDK and scoped IAM, and automated "
               "review-request outreach by email and SMS."),
    ("A2-leadership-verb", "exp.crewtron.b1",
     lambda b: _swap(b, "exp.crewtron.b1", "Shipped", "Directed")),
    ("A5-benchmark-becomes-capacity", "proj.ledgerline.b2",
     lambda b: _swap(b, "proj.ledgerline.b2",
                     "Benchmark: zero double-charges under 200 concurrent identical "
                     "requests, ~1,460 payments/sec.",
                     "Handles 200 concurrent identical requests with zero double-charges "
                     "at ~1,460 payments/sec.")),
    ("A7-sole-credit", "exp.crewtron.b5",
     lambda b: _swap(b, "exp.crewtron.b5", "Automated the", "Single-handedly built the")),
    ("A11-borrowed-digit-10", "exp.crewtron.b5",
     lambda b: _swap(b, "exp.crewtron.b5", "OWASP Top 10 reviews.",
                     "OWASP Top 10 reviews across 10 services.")),
    ("A13-salience-deletion", "proj.ledgerline.b2",
     lambda b: "Solves the dual-write problem with an event relay and consumer-side "
               "dedup. Benchmark: zero double-charges under 200 concurrent identical "
               "requests, ~1,460 payments/sec."),
    ("A14-borrowed-digit-200", "proj.ledgerline.b2",
     lambda b: _swap(b, "proj.ledgerline.b2", "200 concurrent identical requests,",
                     "200 concurrent identical requests across 200 accounts,")),
    ("A15-off-vocabulary-services", "exp.crewtron.b1",
     lambda b: _swap(b, "exp.crewtron.b1", "Python on AWS Lambda, DynamoDB",
                     "Python on AWS Lambda, SQS, API Gateway, CloudWatch, DynamoDB")),
    ("V1-delete-the-Benchmark-qualifier", "proj.ledgerline.b2",
     lambda b: _swap(b, "proj.ledgerline.b2", "Benchmark: ", "")),
    ("E-scope-inflation", "exp.crewtron.b2",
     lambda b: "Architected and owned the REST API layer secured with AWS Cognito, "
               "provisioning the backend as code with AWS CDK and scoped IAM."),
]

# --- attacks that STILL PASS everything -------------------------------------
# Each is one lowercase common word swapped for another. Documented, not fixed.
KNOWN_UNCAUGHT: list[tuple[str, str, Attack, str]] = [
    ("A3-in-production", "exp.crewtron.b1",
     lambda b: _swap(b, "exp.crewtron.b1", "live crew map end to end",
                     "live crew map in production"),
     "claims production traffic the source never asserts"),
    ("A4-client-becomes-customer", "proj.ledgerline.b1",
     lambda b: _swap(b, "proj.ledgerline.b1", "client retries", "customer retries"),
     "an HTTP client becomes a paying merchant; a solo repo becomes a live service"),
    ("A6-depth-on-an-allowed-token", "proj.ledgerline.b1",
     lambda b: "Prevents double-charges using deep distributed systems experience with "
               "storage-layer idempotency (a unique key committed in the same transaction "
               "as the charge) over an explicit payment state machine, and records money "
               "movement in an append-only double-entry ledger.",
     "'Distributed systems' is declared, so v1 is satisfied; the claim is now about him"),
    ("A8-word-number-halved", "exp.crewtron.b3",
     lambda b: _swap(b, "exp.crewtron.b3", "cut LLM token cost", "halved LLM token cost"),
     "a precise 50% claim with no digits, and b3 declares no metric"),
    ("A10-implied-live-revenue", "exp.crewtron.b4",
     lambda b: _swap(b, "exp.crewtron.b4",
                     "on the Stripe API (invoicing, quotes, payments)",
                     "on the Stripe API that process customer invoicing, quotes and payments"),
     "present tense plus 'customer' implies money moving through his code"),
    ("A12-metric-meaning-shift", "exp.crewtron.b5",
     lambda b: _swap(b, "exp.crewtron.b5", "reaching ~82% coverage running on every PR",
                     "at ~82% coverage of the production API"),
     "~82% is the suite's coverage, not coverage of a production API"),
]

# Honest rewrites that must never be flagged, or the validators are useless.
CONTROLS: list[tuple[str, str, Attack]] = [
    ("clause-reorder", "exp.crewtron.b2",
     lambda b: "Designed REST APIs secured with AWS Cognito, automated merchant "
               "review-request outreach over email and SMS, and provisioned the backend "
               "as code with AWS CDK and scoped IAM."),
    ("unchanged", "exp.crewtron.b1", lambda b: b["exp.crewtron.b1"].text),
    ("unchanged-ledgerline", "proj.ledgerline.b2",
     lambda b: b["proj.ledgerline.b2"].text),
]


def _run(bullets: Bullets, bid: str, text: str, *, strict: bool) -> bool:
    fn = validate_strict if strict else validate
    return fn([bullets[bid]], {bid: text}, released=RELEASED).ok


class TestBlockedAttacks:
    @pytest.mark.parametrize(("name", "bid", "build"), BLOCKED, ids=[c[0] for c in BLOCKED])
    def test_caught_by_v1_v10(
        self, bullets: Bullets, name: str, bid: str, build: Attack
    ) -> None:
        assert not _run(bullets, bid, build(bullets), strict=True), (
            f"{name} is no longer caught — a validator has regressed"
        )

    @pytest.mark.parametrize(("name", "bid", "build"), BLOCKED, ids=[c[0] for c in BLOCKED])
    def test_all_of_them_passed_the_original_six(
        self, bullets: Bullets, name: str, bid: str, build: Attack
    ) -> None:
        """Documents why v7-v10 exist: every one of these got through v1-v6."""
        assert _run(bullets, bid, build(bullets), strict=False), (
            f"{name} was already caught by v1-v6 — the historical record is wrong"
        )


class TestKnownUncaught:
    """These still ship. Asserted so a green suite is never mistaken for 'safe'."""

    @pytest.mark.parametrize(
        ("name", "bid", "build", "why"), KNOWN_UNCAUGHT, ids=[c[0] for c in KNOWN_UNCAUGHT]
    )
    def test_still_passes_and_therefore_needs_human_review(
        self, bullets: Bullets, name: str, bid: str, build: Attack, why: str
    ) -> None:
        assert _run(bullets, bid, build(bullets), strict=True), (
            f"{name} is now CAUGHT — promote it into BLOCKED and delete it from here"
        )

    def test_the_uncaught_set_is_exactly_six(self) -> None:
        """If this number changes, the human-review requirement changed with it."""
        assert len(KNOWN_UNCAUGHT) == 6


class TestControls:
    @pytest.mark.parametrize(("name", "bid", "build"), CONTROLS, ids=[c[0] for c in CONTROLS])
    def test_honest_rewrites_are_not_flagged(
        self, bullets: Bullets, name: str, bid: str, build: Attack
    ) -> None:
        assert _run(bullets, bid, build(bullets), strict=True), (
            f"{name} is a false positive — the validators reject an honest rewrite"
        )


class TestCoverage:
    def test_every_bullet_declares_what_validators_need(self, bullets: Bullets) -> None:
        for bullet in bullets.values():
            assert bullet.allowed_verbs, f"{bullet.id} has no verb ceiling"
            assert bullet.must_keep, f"{bullet.id} protects no text"
            first_word = bullet.text.split()[0]
            assert first_word in bullet.allowed_verbs, (
                f"{bullet.id}: its own first word {first_word!r} is not in its ceiling"
            )

    def test_every_protected_string_is_really_in_its_source(self, bullets: Bullets) -> None:
        for bullet in bullets.values():
            for protected in bullet.must_keep:
                assert protected in bullet.text, f"{bullet.id}: {protected!r} not in source"
