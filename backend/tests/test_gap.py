"""Gap reporting — the deterministic per-application output.

The design verdict is selection-dominant: report which technologies a posting
names that the résumé does not, and which variant to send. Never a percentage,
never rewritten prose.
"""
from __future__ import annotations

from copilot.domain.gap import Variant, build_report, tokens_in

RESUME = """
Python, TypeScript, JavaScript, Dart, SQL, Bash, Linux
REST, HTTP, FastAPI, SQLAlchemy, OAuth 2.0, JSON Web Tokens (JWT), microservices,
distributed systems (idempotency, transactional outbox, exactly-once delivery)
AWS Lambda, API Gateway, Cognito, CDK, DynamoDB, PostgreSQL, Docker
Next.js, React, Node.js, HTML, CSS, Flutter
pytest, unit testing, integration testing, Continuous Integration/Continuous Deployment (CI/CD)
data structures, algorithms
"""


class TestTokenExtraction:
    def test_finds_declared_technologies(self) -> None:
        found = tokens_in("We use Python, Kubernetes and Go for our backend.")
        assert "Python" in found
        assert "Kubernetes" in found

    def test_boundary_anchored(self) -> None:
        """The three real failures from this project: military/scalable/robust."""
        assert "Scala" not in tokens_in("We build scalable services.")
        assert "Rust" not in tokens_in("A robust and trusted platform.")
        assert "Go" not in tokens_in("Go above and beyond for customers.")

    def test_java_is_not_javascript(self) -> None:
        assert tokens_in("JavaScript only") == {"JavaScript"}
        assert "Java" in tokens_in("Java and Spring Boot")

    def test_react_native_is_distinct_from_react(self) -> None:
        assert "React" not in tokens_in("We ship with React Native.")
        assert "React Native" in tokens_in("We ship with React Native.")

    def test_empty(self) -> None:
        assert tokens_in("") == set()


class TestRequiredVsPreferred:
    JD = """
About the role
We are hiring a Software Engineer I.

Required Qualifications
- Proficiency in Python
- Experience with Kubernetes
- Familiarity with SQL

Preferred Qualifications
- Exposure to Rust
- Some experience with Terraform
"""

    def test_splits_on_headings(self) -> None:
        r = build_report(title="SWE I", company="Acme", url="u",
                         description=self.JD, resume_text=RESUME)
        assert "Python" in r.have_required
        assert "SQL" in r.have_required
        assert "Kubernetes" in r.missing_required
        assert "Rust" in r.missing_preferred
        assert "Terraform" in r.missing_preferred

    def test_a_required_token_is_not_also_preferred(self) -> None:
        jd = self.JD + "\nPreferred\n- Python again\n"
        r = build_report(title="t", company="c", url="u", description=jd, resume_text=RESUME)
        assert "Python" in r.have_required
        assert "Python" not in r.have_preferred + r.missing_preferred

    def test_reports_a_set_never_a_percentage(self) -> None:
        r = build_report(title="t", company="c", url="u",
                         description=self.JD, resume_text=RESUME)
        assert r.coverage_line == "covers 2 of 3 named requirements"
        assert "%" not in r.coverage_line


class TestVariantChoice:
    def test_backend_leaning(self) -> None:
        jd = ("Required\nPython, PostgreSQL, Kafka, gRPC, microservices, "
              "distributed systems, Redis")
        r = build_report(title="t", company="c", url="u", description=jd, resume_text=RESUME)
        assert r.variant is Variant.BACKEND

    def test_full_stack_leaning(self) -> None:
        jd = "Required\nReact, TypeScript, Next.js, HTML, CSS, Angular"
        r = build_report(title="t", company="c", url="u", description=jd, resume_text=RESUME)
        assert r.variant is Variant.FULL_STACK

    def test_no_strong_signal_stays_neutral(self) -> None:
        """Mixed postings must not be forced into a variant — 20% give no signal."""
        jd = "Required\nPython, React"
        r = build_report(title="t", company="c", url="u", description=jd, resume_text=RESUME)
        assert r.variant is Variant.EITHER

    def test_there_is_no_mobile_variant(self) -> None:
        """Mobile served 12 of 499 postings; Flutter stays on both résumés instead."""
        jd = "Required\nFlutter, iOS, Android, Dart"
        r = build_report(title="t", company="c", url="u", description=jd, resume_text=RESUME)
        assert r.variant in {Variant.FULL_STACK, Variant.EITHER}
        assert not hasattr(Variant, "MOBILE")


class TestDegradation:
    def test_no_description_is_reported_not_guessed(self) -> None:
        """Workday returns no description; claiming full coverage would be a lie."""
        r = build_report(title="SWE", company="Acme", url="u", description="",
                         resume_text=RESUME)
        assert r.unparsed is True
        assert r.have_required == []
        assert "nothing to compare" not in r.coverage_line  # rendered separately

    def test_a_posting_with_no_named_technologies(self) -> None:
        r = build_report(title="t", company="c", url="u",
                         description="We value curiosity and kindness.", resume_text=RESUME)
        assert "no explicit technology requirements" in r.coverage_line

    def test_an_empty_resume_reports_everything_missing(self) -> None:
        r = build_report(title="t", company="c", url="u",
                         description="Required\nPython, Go", resume_text="")
        assert set(r.missing_required) >= {"Python"}
        assert r.have_required == []
