"""Corpus-scale evaluation of the screening funnel against the shipped scorer.

Run against a corpus produced by the ATS adapters:

    python scripts/eval_screening.py ../../career-copilot-prototype/scorer-eval/corpus_v2.json

Reports the funnel, the inversion rate, recall against title-labelled ground
truth, and a side-by-side with the old ``rank()`` path on the same postings.
"""
from __future__ import annotations

import json
import random
import sys
from collections import Counter
from pathlib import Path

from copilot.domain.posting import Posting
from copilot.domain.scoring import SWE_PROFILE, rank
from copilot.domain.screening import Exclusion, is_software_role, screen, screen_all
from copilot.domain.seniority import Level, classify_level

SAMPLE_SEED = 20260729


def load(path: str) -> list[Posting]:
    raw = json.loads(Path(path).read_text())
    return [Posting.model_validate(row) for row in raw]


def ground_truth_band(posting: Posting) -> str:
    """Label from unambiguous title markers only — free, and the set the old path fails."""
    if not is_software_role(posting.title):
        return "not_swe"
    level = classify_level(posting.title)
    if level in {Level.ENTRY, Level.INTERN}:
        return "junior"
    if level is Level.SENIOR:
        return "senior"
    if level is Level.MID:
        return "mid"
    return "unmarked"


def main(path: str) -> int:
    postings = load(path)
    print(f"corpus: {len(postings)} live postings\n")

    labels = Counter(ground_truth_band(p) for p in postings)
    print("=== GROUND TRUTH (title markers only) ===")
    for name, count in labels.most_common():
        print(f"  {name:10s} {count:6d}")

    kept, excluded, report = screen_all(postings)
    print("\n=== FUNNEL ===")
    print(f"  in .................. {report.total}")
    for reason, count in sorted(report.by_exclusion.items(), key=lambda kv: -kv[1]):
        print(f"  excluded: {reason:32s} {count:6d}")
    print(f"  kept ................ {report.kept}")
    print(f"  of kept, level unknown (needs LLM tier) ... {report.needs_llm}")

    print("\n=== RECALL on title-labelled junior SWE roles ===")
    juniors = [p for p in postings if ground_truth_band(p) == "junior"]
    kept_urls = {d.posting.url for d in kept}
    recovered = [p for p in juniors if p.url in kept_urls]
    print(f"  junior SWE in corpus .......... {len(juniors)}")
    print(f"  surfaced by the funnel ........ {len(recovered)}")
    pct = 100 * len(recovered) / len(juniors) if juniors else 0.0
    print(f"  RECALL ........................ {pct:.1f}%")
    missed = [p for p in juniors if p.url not in kept_urls]
    if missed:
        print("  sample missed (each should have a real reason):")
        for p in missed[:5]:
            reasons = screen(p).reasons
            print(f"      {p.title[:52]:52s} -> {reasons[0][:60] if reasons else '?'}")

    print("\n=== PRECISION of what the funnel surfaces ===")
    surfaced_bands = Counter(ground_truth_band(d.posting) for d in kept)
    for name, count in surfaced_bands.most_common():
        share = 100 * count / max(1, len(kept))
        print(f"  {name:10s} {count:6d}  ({share:4.1f}%)")
    seniors_surfaced = surfaced_bands["senior"]
    print(f"  senior roles surfaced (must be 0) ... {seniors_surfaced}")

    print("\n=== INVERSION RATE ===")
    senior_kept = {d.posting.url for d in kept if ground_truth_band(d.posting) == "senior"}
    print(f"  labelled-senior postings in the worklist: {len(senior_kept)}")
    print(f"  inversion_rate = {len(senior_kept) / max(1, len(kept)):.4f}   (target 0)")

    print("\n=== SIDE BY SIDE with the shipped scorer on the same corpus ===")
    raw = [
        {
            "title": p.title,
            "company": p.company,
            "url": p.url,
            "location": p.location,
            "description": p.description,
        }
        for p in postings
    ]
    old_top = rank(raw, profile=SWE_PROFILE, min_score=40, limit=8)
    by_url = {p.url: p for p in postings}
    print("  OLD rank() top 8:")
    for job in old_top:
        band = ground_truth_band(by_url[job.url]) if job.url in by_url else "?"
        flag = "OK " if band == "junior" else "BAD"
        print(f"    [{flag}] {job.score:3d}%  {job.title[:50]:50s} ({band})")
    print("  NEW worklist head (newest first, no score):")
    for decision in kept[:8]:
        band = ground_truth_band(decision.posting)
        flag = "OK " if band in {"junior", "unmarked"} else "BAD"
        day = decision.posting.posted_at.date() if decision.posting.posted_at else "undated"
        print(f"    [{flag}] {day}  {decision.posting.title[:44]:44s} ({band})")

    print("\n=== GATE EVIDENCE — hand-check sample (50) ===")
    gated = [d for d in excluded if Exclusion.CLEARANCE in d.exclusions or
             Exclusion.CITIZENSHIP in d.exclusions or Exclusion.NO_SPONSORSHIP in d.exclusions]
    rng = random.Random(SAMPLE_SEED)
    for decision in rng.sample(gated, min(50, len(gated)))[:12]:
        print(f"  {decision.posting.title[:44]:44s} | {decision.reasons[0][:88]}")
    print(f"  (total eligibility-gated: {len(gated)}; printed 12 of a 50-row sample)")

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "corpus_v2.json"))
