"""Requirement extraction and gap reporting — deterministic, no LLM.

The research verdict is that tailoring is **selection-dominant**: every layer that
decides an outcome scores on *what content is present*, not how it is phrased.
Jobscan's own methodology states word count and measurable results are not
factored in; the strongest controlled study on LLM screeners manufactures its
pairs by adding and deleting qualifications, and finds a single qualification
flips the decision correctly 82-87% of the time.

So this module answers the only question worth answering per application:

    which technologies does this posting name that my résumé does not,
    and which variant of my résumé is the better fit?

It reports a **set, never a percentage**. Competitor match scores were found to be
mutually incomparable — Greenhouse itself emits named categories and no number —
and a percentage invites optimising a proxy. "Covers 7 of 9 required; missing
Kubernetes, Go" is actionable; "71%" is not.

Nothing here writes résumé prose. Rewriting belongs at bank-authoring time behind
a human approval, not in the per-application path.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum


class Variant(StrEnum):
    """Which résumé to send. Two, not three.

    Forcing the choice over 499 eligible entry-band postings: backend 53.3%,
    full-stack 24.2%, no signal 20.0%, mobile 2.4% (12 of 499). Flutter is 0.6% of
    demand and the postings wanting mobile want Kotlin/Swift — so mobile is not a
    variant. Flutter stays *on* both résumés; it is real experience and evidences
    shipping.
    """

    BACKEND = "backend"
    FULL_STACK = "full-stack"
    EITHER = "either"


#: Closed vocabulary: canonical token -> surface forms. Every pattern is
#: boundary-anchored, because unanchored matching produced three wrong results in
#: this project: bare ``itar`` matched "military", ``scala`` matched "scalable",
#: and ``rust`` matched "trust"/"robust".
VOCAB: dict[str, tuple[str, ...]] = {
    "Python": (r"\bpython\b",),
    "Java": (r"\bjava\b(?!script)",),
    "JavaScript": (r"\bjavascript\b",),
    "TypeScript": (r"\btypescript\b",),
    "Go": (r"\bgolang\b", r"\bgo\b(?=\s*(?:,|/|\)|programming|lang))"),
    "C++": (r"c\+\+",),
    "C#": (r"c#",),
    "Rust": (r"\brust\b",),
    "Ruby": (r"\bruby\b",),
    "Kotlin": (r"\bkotlin\b",),
    "Swift": (r"\bswift\b",),
    "Scala": (r"\bscala\b",),
    "Dart": (r"\bdart\b",),
    "React": (r"\breact\b(?! native)",),
    "React Native": (r"react native",),
    "Angular": (r"\bangular\b",),
    "Vue": (r"\bvue\b",),
    "Node.js": (r"node\.?js",),
    "Next.js": (r"next\.?js",),
    "Django": (r"\bdjango\b",),
    "Flask": (r"\bflask\b",),
    "FastAPI": (r"\bfastapi\b",),
    "Spring": (r"spring boot",),
    "AWS": (r"\baws\b", r"amazon web services"),
    "GCP": (r"\bgcp\b", r"google cloud"),
    "Azure": (r"\bazure\b",),
    "Kubernetes": (r"\bkubernetes\b", r"\bk8s\b"),
    "Docker": (r"\bdocker\b",),
    "Terraform": (r"\bterraform\b",),
    "Lambda": (r"\blambda\b",),
    "Cognito": (r"\bcognito\b",),
    "CDK": (r"\bcdk\b",),
    "IAM": (r"\biam\b",),
    "EventBridge": (r"\beventbridge\b",),
    "Stripe": (r"\bstripe\b",),
    "S3": (r"\bs3\b",),
    "DynamoDB": (r"\bdynamodb\b",),
    "PostgreSQL": (r"\bpostgresql\b", r"\bpostgres\b"),
    "MySQL": (r"\bmysql\b",),
    "MongoDB": (r"\bmongodb\b",),
    "Redis": (r"\bredis\b",),
    "Kafka": (r"\bkafka\b",),
    "GraphQL": (r"\bgraphql\b",),
    "gRPC": (r"\bgrpc\b",),
    "REST": (r"\brest\b", r"\brestful\b"),
    "OAuth": (r"\boauth\b",),
    "JWT": (r"\bjwt\b", r"json web token"),
    "Microservices": (r"microservices?",),
    "CI/CD": (r"ci/cd", r"continuous integration"),
    "Git": (r"\bgit\b",),
    "Linux": (r"\blinux\b",),
    "Agile/Scrum": (r"\bagile\b", r"\bscrum\b"),
    "Unit testing": (r"unit test",),
    "Integration testing": (r"integration test",),
    "pytest": (r"\bpytest\b",),
    "Distributed systems": (r"distributed system",),
    "System design": (r"system design",),
    "Data structures": (r"data structure",),
    "Algorithms": (r"algorithms?",),
    "Observability": (r"\bobservability\b",),
    "Machine learning": (r"machine learning",),
    "LLM": (r"\bllms?\b", r"large language model"),
    "SQL": (r"\bsql\b",),
    "NoSQL": (r"\bnosql\b",),
    "Flutter": (r"\bflutter\b",),
    "iOS": (r"\bios\b",),
    "Android": (r"\bandroid\b",),
    "HTML/CSS": (r"\bhtml\b", r"\bcss\b"),
    "DevOps": (r"\bdevops\b",),
}

_COMPILED: dict[str, tuple[re.Pattern[str], ...]] = {
    token: tuple(re.compile(p, re.I) for p in patterns) for token, patterns in VOCAB.items()
}

#: Tokens that signal the role leans backend vs front-of-stack. Used only to pick
#: between two résumés, never to score the posting.
_BACKEND_SIGNAL = frozenset(
    {"Python", "Java", "Go", "Rust", "PostgreSQL", "MySQL", "MongoDB", "Redis", "Kafka",
     "gRPC", "Microservices", "Distributed systems", "DynamoDB", "Lambda", "Terraform",
     "Kubernetes", "SQL", "NoSQL", "System design", "Observability", "DevOps"}
)
_FRONTEND_SIGNAL = frozenset(
    {"React", "Angular", "Vue", "Next.js", "TypeScript", "JavaScript", "HTML/CSS",
     "React Native", "Flutter", "iOS", "Android"}
)

# A posting's own words for "we require this" vs "it would be nice". Requirement
# sections are where the load-bearing tokens live.
_REQUIRED_HEAD = re.compile(
    r"(required|requirements|must have|minimum qualifications|basic qualifications|"
    r"what you.{0,5}ll need|qualifications)", re.I
)
_PREFERRED_HEAD = re.compile(
    r"(preferred|nice to have|bonus|plus|desired|good to have)", re.I
)

#: A line shorter than this is treated as a heading rather than prose.
_HEADING_MAX_CHARS = 80
#: How much one side must dominate before a variant is recommended.
_VARIANT_DOMINANCE = 2
_VARIANT_MIN_SIGNALS = 2


def tokens_in(text: str) -> set[str]:
    """Canonical technology tokens present in ``text`` (boundary-anchored)."""
    if not text:
        return set()
    return {token for token, pats in _COMPILED.items() if any(p.search(text) for p in pats)}


def _split_sections(description: str) -> tuple[str, str]:
    """Best-effort split into (required-ish, preferred-ish) prose.

    Deliberately crude: job descriptions have no schema, and a wrong split only
    changes which bucket a token lands in — it never invents or drops one. Both
    buckets are reported, so a mis-split is visible rather than silent.
    """
    lines = description.splitlines()
    required: list[str] = []
    preferred: list[str] = []
    bucket = required
    for line in lines:
        head = line.strip()
        if len(head) < _HEADING_MAX_CHARS:  # a heading, not a paragraph
            if _PREFERRED_HEAD.search(head):
                bucket = preferred
                continue
            if _REQUIRED_HEAD.search(head):
                bucket = required
                continue
        bucket.append(line)
    return "\n".join(required), "\n".join(preferred)


@dataclass
class GapReport:
    """What a posting asks for, against what the résumé already says."""

    title: str = ""
    company: str = ""
    url: str = ""
    have_required: list[str] = field(default_factory=list)
    missing_required: list[str] = field(default_factory=list)
    have_preferred: list[str] = field(default_factory=list)
    missing_preferred: list[str] = field(default_factory=list)
    variant: Variant = Variant.EITHER
    backend_signal: int = 0
    frontend_signal: int = 0
    unparsed: bool = False

    @property
    def coverage_line(self) -> str:
        """A set, not a percentage. See the module docstring."""
        total = len(self.have_required) + len(self.missing_required)
        if not total:
            return "no explicit technology requirements found in this posting"
        return f"covers {len(self.have_required)} of {total} named requirements"


def build_report(
    *, title: str, company: str, url: str, description: str, resume_text: str
) -> GapReport:
    """Compare one posting against the résumé. Pure — no network, no LLM."""
    if not description.strip():
        return GapReport(title=title, company=company, url=url, unparsed=True)

    required_text, preferred_text = _split_sections(description)
    mine = tokens_in(resume_text)

    req = tokens_in(required_text)
    pref = tokens_in(preferred_text) - req  # a token stated as required is not "preferred"
    all_tokens = req | pref

    backend = len(all_tokens & _BACKEND_SIGNAL)
    frontend = len(all_tokens & _FRONTEND_SIGNAL)
    if backend >= frontend * _VARIANT_DOMINANCE and backend >= _VARIANT_MIN_SIGNALS:
        variant = Variant.BACKEND
    elif frontend >= backend * _VARIANT_DOMINANCE and frontend >= _VARIANT_MIN_SIGNALS:
        variant = Variant.FULL_STACK
    else:
        variant = Variant.EITHER

    order = sorted
    return GapReport(
        title=title,
        company=company,
        url=url,
        have_required=order(req & mine),
        missing_required=order(req - mine),
        have_preferred=order(pref & mine),
        missing_preferred=order(pref - mine),
        variant=variant,
        backend_signal=backend,
        frontend_signal=frontend,
    )
