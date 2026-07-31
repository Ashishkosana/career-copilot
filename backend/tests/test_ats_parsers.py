"""Parser tests for the ATS adapters.

Every ``parse`` function is pure, so these run offline against payloads shaped
like the real ones. The payload quirks asserted here are all things the live APIs
actually do — escaped markup, missing descriptions, unlisted drafts, epoch
milliseconds — and each one silently corrupted a gate before it was handled.
"""
from __future__ import annotations

from typing import Any, ClassVar

from copilot.adapters.ats import ashby, greenhouse, lever, workable, workday
from copilot.adapters.ats._text import html_to_text


class TestHtmlToText:
    def test_unescapes_greenhouse_double_encoding(self) -> None:
        raw = "&lt;div class=&quot;intro&quot;&gt;&lt;p&gt;Build things&lt;/p&gt;&lt;/div&gt;"
        assert html_to_text(raw) == "Build things"

    def test_strips_script_and_style_bodies(self) -> None:
        raw = "<p>Real text</p><script>var x = 1;</script><style>p{color:red}</style>"
        text = html_to_text(raw)
        assert "Real text" in text
        assert "var x" not in text
        assert "color:red" not in text

    def test_block_ends_become_newlines(self) -> None:
        assert html_to_text("<li>One</li><li>Two</li>") == "One\nTwo"

    def test_does_not_truncate(self) -> None:
        """The legal tail (sponsorship, clearance) lives at the very end of a JD."""
        body = "<p>" + ("x" * 12000) + "</p><p>We do not sponsor visas.</p>"
        text = html_to_text(body)
        assert len(text) > 12000
        assert text.endswith("We do not sponsor visas.")

    def test_empty_and_none(self) -> None:
        assert html_to_text(None) == ""
        assert html_to_text("") == ""


class TestGreenhouse:
    payload: ClassVar[dict[str, Any]] = {
        "jobs": [
            {
                "title": "Software Engineer I",
                "absolute_url": "https://job-boards.greenhouse.io/acme/jobs/123",
                "company_name": "Acme",
                "location": {"name": "Austin, TX"},
                "content": "&lt;p&gt;Python and AWS&lt;/p&gt;",
                "requisition_id": "R-9",
                "first_published": "2026-07-01T10:00:00Z",
                "updated_at": "2026-07-20T10:00:00Z",
            },
            {"title": "No URL", "absolute_url": ""},
        ]
    }

    def test_parses_and_unescapes(self) -> None:
        [job] = greenhouse.parse(self.payload, "acme")
        assert job.title == "Software Engineer I"
        assert job.company == "Acme"
        assert job.location == "Austin, TX"
        assert job.description == "Python and AWS"
        assert job.desc_available is True
        assert job.ats == "greenhouse"
        assert job.req_id == "R-9"
        assert job.posted_at is not None
        assert job.posted_at.year == 2026

    def test_skips_rows_without_url(self) -> None:
        assert len(greenhouse.parse(self.payload, "acme")) == 1

    def test_missing_content_flags_desc_unavailable(self) -> None:
        payload = {"jobs": [{"title": "T", "absolute_url": "https://x/1"}]}
        [job] = greenhouse.parse(payload, "acme")
        assert job.desc_available is False

    def test_falls_back_to_updated_at(self) -> None:
        payload = {
            "jobs": [
                {"title": "T", "absolute_url": "https://x/1", "updated_at": "2026-03-04T00:00:00Z"}
            ]
        }
        [job] = greenhouse.parse(payload, "acme")
        assert job.posted_at is not None
        assert job.posted_at.month == 3

    def test_tolerates_garbage(self) -> None:
        assert greenhouse.parse(None, "acme") == []
        assert greenhouse.parse({"jobs": ["nope"]}, "acme") == []


class TestAshby:
    def test_skips_unlisted(self) -> None:
        payload = {
            "jobs": [
                {
                    "title": "Live",
                    "jobUrl": "https://a/1",
                    "isListed": True,
                    "descriptionPlain": "d",
                },
                {"title": "Draft", "jobUrl": "https://a/2", "isListed": False},
            ]
        }
        titles = [p.title for p in ashby.parse(payload, "acme")]
        assert titles == ["Live"]

    def test_html_description_fallback(self) -> None:
        payload = {
            "jobs": [
                {"title": "T", "applyUrl": "https://a/1", "descriptionHtml": "<p>Hi</p>"}
            ]
        }
        [job] = ashby.parse(payload, "acme")
        assert job.description == "Hi"

    def test_remote_and_employment_type(self) -> None:
        payload = {
            "jobs": [
                {
                    "title": "T",
                    "jobUrl": "https://a/1",
                    "descriptionPlain": "d",
                    "isRemote": True,
                    "employmentType": "FullTime",
                    "publishedAt": "2026-05-20T21:12:29.666Z",
                }
            ]
        }
        [job] = ashby.parse(payload, "acme")
        assert job.remote is True
        assert job.employment_type == "FullTime"
        assert job.posted_at is not None


class TestLever:
    def test_concatenates_description_parts(self) -> None:
        payload = [
            {
                "text": "Backend Engineer",
                "hostedUrl": "https://jobs.lever.co/acme/uuid",
                "descriptionPlain": "Main body",
                "additionalPlain": "Benefits",
                "categories": {"location": "Remote", "commitment": "Full-time"},
                "createdAt": 1711403416463,
            }
        ]
        [job] = lever.parse(payload, "acme")
        assert job.description == "Main body\n\nBenefits"
        assert job.location == "Remote"
        assert job.employment_type == "Full-time"
        assert job.posted_at is not None
        assert job.posted_at.year == 2024

    def test_empty_description_is_flagged_not_passed_as_blank(self) -> None:
        """11 of 389 real Lever records do this; blank strings pass every gate."""
        payload = [{"text": "T", "hostedUrl": "https://l/1", "descriptionPlain": ""}]
        [job] = lever.parse(payload, "acme")
        assert job.description == ""
        assert job.desc_available is False

    def test_requires_list_payload(self) -> None:
        assert lever.parse({"jobs": []}, "acme") == []


class TestWorkable:
    def test_search_flattens_nested_company_and_location(self) -> None:
        payload = {
            "jobs": [
                {
                    "id": "abc",
                    "title": "Software Engineer",
                    "url": "https://jobs.workable.com/view/x/software-engineer",
                    "description": "<p>Work</p>",
                    "company": {"title": "PrePass"},
                    "location": {
                        "city": "Phoenix",
                        "subregion": None,
                        "countryName": "United States",
                    },
                    "created": "2026-07-17T08:28:53.105Z",
                    "workplace": "remote",
                }
            ],
            "nextPageToken": "tok123",
        }
        postings, token = workable.parse_search(payload)
        assert token == "tok123"
        [job] = postings
        assert job.company == "PrePass"
        assert job.location == "Phoenix, United States"
        assert job.description == "Work"
        assert job.remote is True

    def test_search_returns_empty_token_when_absent(self) -> None:
        postings, token = workable.parse_search({"jobs": []})
        assert postings == []
        assert token == ""

    def test_widget_prefers_application_url(self) -> None:
        payload = {
            "name": "Hotjar",
            "jobs": [
                {
                    "title": "Engineer",
                    "application_url": "https://apply.workable.com/hotjar/j/ABC/apply",
                    "url": "https://apply.workable.com/hotjar/j/ABC",
                    "description": "<p>d</p>",
                }
            ],
        }
        [job] = workable.parse_widget(payload, "hotjar")
        assert job.url.endswith("/apply")
        assert job.company == "Hotjar"


class TestWorkday:
    payload: ClassVar[dict[str, Any]] = {
        "total": 1695,
        "jobPostings": [
            {
                "title": "Software Engineer, New College Grad",
                "externalPath": "/job/Santa-Clara/Software-Engineer_JR2016506",
                "locationsText": "US, CA, Santa Clara",
                "postedOn": "Posted 5 Days Ago",
                "remoteType": "Remote",
            }
        ],
    }

    def test_builds_absolute_url(self) -> None:
        [job] = workday.parse(self.payload, "nvidia", "wd5", "NVIDIAExternalCareerSite")
        assert job.url == (
            "https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite"
            "/job/Santa-Clara/Software-Engineer_JR2016506"
        )
        assert job.req_id == "JR2016506"
        assert job.remote is True

    def test_never_claims_a_description(self) -> None:
        """The CXS list endpoint returns none, so these must go to title-only gates."""
        [job] = workday.parse(self.payload, "nvidia", "wd5", "site")
        assert job.description == ""
        assert job.desc_available is False

    def test_posted_on_is_not_parsed_as_a_date(self) -> None:
        """'Posted 5 Days Ago' is a human string; guessing a timestamp is worse."""
        [job] = workday.parse(self.payload, "nvidia", "wd5", "site")
        assert job.posted_at is None
