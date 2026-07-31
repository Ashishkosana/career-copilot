#!/usr/bin/env python3
"""Point this at a job posting and get an actionable gap report.

    python scripts/tailor.py https://job-boards.greenhouse.io/flexport/jobs/7311835
    python scripts/tailor.py https://jobs.lever.co/palantir/<uuid>
    python scripts/tailor.py --resume ~/projects/resumes/Ashish_Kosana_Resume.pdf <url>

Fetches the real posting through the ATS adapters, extracts the technologies it
names, and diffs them against what the résumé actually says. Reports a **set**,
never a percentage.

Deliberately does not rewrite anything. The research verdict is that tailoring is
selection-dominant, the dominant public scorer does not read prose, and generative
rewriting in the per-application path has measurable fabrication cost with ~zero
keyword upside. So this tells you what to *choose*, and leaves the writing to you.
"""
from __future__ import annotations

import argparse
import re
import sys
import urllib.parse
from collections.abc import Callable
from pathlib import Path

from copilot.adapters.ats import (
    AshbySource,
    AtsFetchError,
    GreenhouseSource,
    LeverSource,
    classify_apply_url,
)
from copilot.domain.gap import GapReport, Variant, build_report
from copilot.domain.posting import Posting
from copilot.ports.postingsource import PostingSourcePort

DEFAULT_RESUME = Path.home() / "projects" / "resumes" / "Ashish_Kosana_Resume.pdf"


def read_resume(path: Path) -> str:
    """Extract the résumé's text layer the way a parser would."""
    if path.suffix.lower() in {".html", ".htm", ".md", ".txt"}:
        raw = path.read_text()
        return re.sub(r"<[^>]+>", " ", raw)
    # Imported lazily: pdfplumber and pypdf are optional, and pdfplumber is
    # preferred because it is position-aware the way real ATS parsers are.
    try:
        import pdfplumber  # noqa: PLC0415

        with pdfplumber.open(path) as doc:
            return "\n".join(page.extract_text() or "" for page in doc.pages)
    except ImportError:
        from pypdf import PdfReader  # noqa: PLC0415

        return "\n".join(page.extract_text() or "" for page in PdfReader(path).pages)


def fetch_posting(url: str) -> Posting:
    """Resolve a posting URL to the posting itself, via its own ATS."""
    entry = classify_apply_url(url)
    if entry is None:
        raise SystemExit(
            f"Could not tell which ATS this URL belongs to:\n  {url}\n"
            "Supported: job-boards.greenhouse.io, boards.greenhouse.io, "
            "jobs.ashbyhq.com, jobs.lever.co"
        )

    # Annotated rather than inferred: mypy reads a dict of three different classes
    # as ``dict[str, object]``, and ``object`` has no ``fetch`` — so an entry that
    # was not a posting source would type-check fine and fail at runtime.
    sources: dict[str, Callable[[str], PostingSourcePort]] = {
        "greenhouse": GreenhouseSource,
        "ashby": AshbySource,
        "lever": LeverSource,
    }
    factory = sources.get(entry.ats)
    if factory is None:
        raise SystemExit(
            f"{entry.ats} boards do not expose a single-posting read API here.\n"
            "Paste the description into a .txt file and pass --description instead."
        )

    try:
        postings = factory(entry.tenant).fetch()
    except AtsFetchError as exc:
        raise SystemExit(f"Could not read the {entry.ats} board: {exc}") from exc

    # Match on the identifier in the URL rather than the whole URL, since boards
    # redirect and decorate.
    ident = [p for p in urllib.parse.urlparse(url).path.split("/") if p][-1]
    for posting in postings:
        if ident and ident in posting.url:
            return posting
        if ident and posting.req_id and ident == posting.req_id:
            return posting
    raise SystemExit(
        f"Fetched {len(postings)} postings from {entry.tenant} but none matched {ident!r}.\n"
        "The posting may have closed."
    )


def render(report: GapReport) -> str:
    out: list[str] = []
    w = out.append

    w("")
    w(f"  {report.title}")
    w(f"  {report.company}")
    w(f"  {report.url}")
    w("")

    if report.unparsed:
        w("  This source returned no description, so there is nothing to compare.")
        w("  Only the title was available.")
        return "\n".join(out)

    w(f"  {report.coverage_line}")
    w("")

    if report.missing_required:
        w("  MISSING, and the posting calls it required")
        for token in report.missing_required:
            w(f"    - {token}")
        w("")
    if report.missing_preferred:
        w("  Missing, listed as preferred")
        w(f"    {', '.join(report.missing_preferred)}")
        w("")
    if report.have_required:
        w("  Already on your résumé, and required here")
        w(f"    {', '.join(report.have_required)}")
        w("")
    if report.have_preferred:
        w("  Already on your résumé, preferred here")
        w(f"    {', '.join(report.have_preferred)}")
        w("")

    label = {
        Variant.BACKEND: "send the BACKEND-leaning résumé",
        Variant.FULL_STACK: "send the FULL-STACK-leaning résumé",
        Variant.EITHER: "either résumé — this posting gives no strong signal",
    }[report.variant]
    w(f"  Variant: {label}")
    w(f"    (backend signals {report.backend_signal}, frontend signals {report.frontend_signal})")
    w("")

    if report.missing_required:
        w("  What to do")
        w("    Anything above that you have genuinely done but have not written down is a")
        w("    free win — add it. Anything you have not done, leave alone: a claim you")
        w("    cannot defend in an interview costs more than a keyword gains.")
    else:
        w("  What to do")
        w("    Nothing missing that this posting names. Apply.")
    w("")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("url", nargs="?", help="a Greenhouse, Ashby or Lever posting URL")
    ap.add_argument("--resume", type=Path, default=DEFAULT_RESUME)
    ap.add_argument("--description", type=Path, help="a .txt file, instead of fetching a URL")
    ap.add_argument("--title", default="(pasted description)")
    ap.add_argument("--company", default="")
    args = ap.parse_args()

    if not args.resume.exists():
        print(f"No résumé at {args.resume}", file=sys.stderr)
        return 2
    resume_text = read_resume(args.resume)

    if args.description:
        report = build_report(
            title=args.title,
            company=args.company,
            url=str(args.description),
            description=args.description.read_text(),
            resume_text=resume_text,
        )
    elif args.url:
        posting = fetch_posting(args.url)
        report = build_report(
            title=posting.title,
            company=posting.company,
            url=posting.url,
            description=posting.description,
            resume_text=resume_text,
        )
    else:
        ap.print_help()
        return 2

    print(render(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
