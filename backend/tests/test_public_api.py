"""The unauthenticated read route, driven with an in-memory store — no cloud, no network.

This endpoint is open to the internet, so the tests are mostly about what *cannot*
come out of it. Each one names the failure it prevents; these are the ones that
matter:

* a field added to the model later reaching a public page because the sanitiser was
  a blocklist — asserted by spiking the upstream serialiser with ``ownerId`` and
  ``appliedAt`` and proving they are absent here while present upstream;
* description prose republished in bulk, which is a different act from quoting a
  sentence to justify a decision (and which Workday's terms forbid);
* résumé text — an address, a phone number, a sentence of prose — travelling out
  through the score, which this Lambda has to compute and therefore has to load;
* a write reaching the store through an open endpoint: ``POST /public/worklist``
  must never touch ``mark_applied``, asserted by auditing the store's call log and
  again structurally over the module's own source;
* an evidence excerpt arriving uncapped because the field carrying it was added to
  the allowlist without being marked as prose;
* an open endpoint that re-screens 25,294 postings per request, which is a bill.
"""
from __future__ import annotations

import ast
import json
from collections.abc import Callable, Iterable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from copilot.domain.posting import Posting
from copilot.domain.screening import ScreenDecision
from copilot.handlers import public_api as public
from copilot.handlers import worklist_api
from copilot.ports.postingstore import PostingStorePort

NOW = datetime(2026, 7, 30, 12, 0, tzinfo=UTC)

#: A sentence no gate reacts to, so if it appears in a response the *description*
#: leaked rather than an evidence quote being shown.
PROSE_MARKER = "Our office has a rooftop garden and a very old dog."

JD_BACKEND = f"""
About the role
{PROSE_MARKER}

Requirements
- Python and PostgreSQL in production
- AWS (Lambda, DynamoDB)
- Docker

Nice to have
- Kubernetes
"""

#: A résumé shaped like the real one: contact details and prose above the skills.
#: Every line of it is what must not reach the wire, and the skills line is the only
#: part the score is allowed to reflect — as canonical vocabulary tokens, not text.
RESUME = """
Ashish Kosana — ashish.private@example.com — (555) 010-4242
Summary: I ship backend services and own them in production.
Skills: Python, PostgreSQL, AWS, Lambda, DynamoDB, pytest
"""
RESUME_SECRETS = (
    "ashish.private@example.com",
    "(555) 010-4242",
    "I ship backend services",
)


# ---------------------------------------------------------------------------
# Fakes and builders
# ---------------------------------------------------------------------------

class FakePostingStore:
    """In-memory PostingStorePort. ``calls`` is the audit trail the read-only tests read.

    Written out in full rather than shared with ``test_worklist_api`` on purpose: the
    claim under test here is "an open endpoint touches exactly one store method", and
    that claim is only as good as this fake being a complete ``PostingStorePort`` —
    every method a public request could possibly reach has to exist to be observed.
    """

    def __init__(self, postings: Iterable[Posting] = (), *, raises: bool = False) -> None:
        self.postings = list(postings)
        self.raises = raises
        self.applied: dict[str, datetime] = {}
        self.calls: list[str] = []
        self.reads = 0

    def _note(self, what: str) -> None:
        self.calls.append(what)
        if self.raises:
            raise RuntimeError("sqlite file is gone")

    def open_postings(self) -> list[Posting]:
        self._note("open_postings")
        self.reads += 1
        return list(self.postings)

    def mark_applied(self, posting_id: str, *, now: datetime) -> None:
        self._note("mark_applied")
        self.applied.setdefault(posting_id, now)

    def sync(self, postings: list[Posting], *, now: datetime) -> tuple[list[str], list[str]]:
        self._note("sync")
        self.postings.extend(postings)
        return [p.id for p in postings], []

    def close_missing(self, *, now: datetime, seen_ids: set[str]) -> int:
        self._note("close_missing")
        return 0

    def new_since(self, since: datetime) -> list[Posting]:
        self._note("new_since")
        return []

    def cached_interpretation(self, posting_id: str) -> dict[str, Any] | None:
        self._note("cached_interpretation")
        return None

    def save_interpretation(self, posting_id: str, payload: dict[str, Any]) -> None:
        self._note("save_interpretation")


def test_fake_satisfies_the_port() -> None:
    """If the port grows a write, this fake must grow with it — or it proves nothing."""
    store: PostingStorePort = FakePostingStore()
    assert store.open_postings() == []


def make(
    title: str = "Junior Software Engineer",
    *,
    company: str = "Acme",
    desc: str = JD_BACKEND,
    has_desc: bool | None = None,
    day: int = 1,
    url: str | None = None,
    employment_type: str = "",
    tenant: str = "acme-board",
    req_id: str = "REQ-4242",
) -> Posting:
    return Posting(
        title=title,
        company=company,
        url=url or f"https://boards.example/{company}/{day}".replace(" ", "-"),
        ats="greenhouse",
        tenant=tenant,
        req_id=req_id,
        location="Remote",
        description=desc,
        desc_available=bool(desc) if has_desc is None else has_desc,
        posted_at=datetime(2026, 7, day, 9, 0, tzinfo=UTC),
        employment_type=employment_type,
    )


def an_internship() -> Posting:
    return make(title="Software Engineer Intern", url="https://x/intern")


def request(
    *,
    path: str = "/public/worklist",
    method: str = "GET",
    query: dict[str, str] | None = None,
    path_params: dict[str, str] | None = None,
    body: str | None = None,
    authorizer: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One API Gateway event with **no authorizer** — which is the whole point.

    ``authorizer`` exists so a test can prove that a caller who *sends* claims does
    not get them used: this route has no notion of an identity, and an attacker
    supplying one must not be able to influence a read.
    """
    built: dict[str, Any] = {
        "routeKey": f"{method} {path}",
        "rawPath": path,
        "requestContext": {"http": {"method": method}},
        "queryStringParameters": query,
        "pathParameters": path_params,
    }
    if authorizer is not None:
        built["requestContext"]["authorizer"] = authorizer
    if body is not None:
        built["body"] = body
    return built


def authenticated(path: str, **kwargs: Any) -> dict[str, Any]:
    """The same request through the Cognito-protected route, for side-by-side tests."""
    event = request(path=path, **kwargs)
    event["routeKey"] = f"GET {path}"
    event["requestContext"]["authorizer"] = {"jwt": {"claims": {"sub": "cognito-sub-1"}}}
    return event


def body_of(response: dict[str, Any]) -> dict[str, Any]:
    parsed: dict[str, Any] = json.loads(response["body"])
    return parsed


def get(store: FakePostingStore, path: str = "/public/worklist", **kwargs: Any) -> dict[str, Any]:
    resp = public.route(
        store, request(path=path, **kwargs), resume_text=RESUME, now=NOW
    )
    assert resp["statusCode"] == 200, resp["body"]
    return body_of(resp)


def detail(store: FakePostingStore, posting: Posting) -> dict[str, Any]:
    return get(
        store,
        path=f"/public/worklist/{posting.id}",
        path_params={"id": posting.id},
    )


def blob(payload: dict[str, Any]) -> str:
    """The response as one string, for "this must appear nowhere in it" assertions."""
    return json.dumps(payload, ensure_ascii=False)


def ids_of(page: dict[str, Any]) -> list[str]:
    return [item["id"] for item in page["items"]]


def _strings(node: Any, path: str = "$") -> Iterator[tuple[str, str]]:
    """Every string in a payload with its JSON path, for whole-payload properties.

    Assertions written over this hold for fields nobody has added yet, which is the
    only kind of length check that survives the next edit to the allowlist.
    """
    if isinstance(node, dict):
        for key, value in node.items():
            yield from _strings(value, f"{path}.{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _strings(value, f"{path}[{index}]")
    elif isinstance(node, str):
        yield path, node


ALL_PATHS = ("/public/worklist", "/public/internships", "/public/excluded")


# ---------------------------------------------------------------------------
# What the route publishes
# ---------------------------------------------------------------------------

class TestPublishedShape:
    def test_the_worklist_card_is_the_card_the_page_already_renders(self) -> None:
        """Same field names as the snapshot, or the live page renders "undefined"."""
        page = get(FakePostingStore([make()]))
        [item] = page["items"]
        assert set(item) == {
            "id", "title", "company", "location", "url", "ats", "level", "levelSource",
            "levelWhy", "postedAt", "remote", "employmentType", "descAvailable",
            "descriptionStatus", "descriptionWithheld", "score",
        }
        assert page["collection"] == "worklist"
        assert page["prosePublished"] is False
        assert page["quoteMaxChars"] == worklist_api.QUOTE_MAX_CHARS

    def test_the_score_travels_with_its_components(self) -> None:
        """A bare total reads as a match percentage; gap.py exists to refuse that."""
        score = get(FakePostingStore([make()]))["items"][0]["score"]
        assert score["tier"] == "strong"
        assert "covers 5/6 required" in score["explain"]
        assert score["required"]["missing"] == ["Docker"]
        assert score["levelConfirmed"] is True

    def test_the_funnel_and_its_overcount_warning_survive(self) -> None:
        """A UI rendering gate counts as a subtraction chain produces nonsense."""
        page = get(FakePostingStore([make(), make(title="Product Manager", day=2)]))
        assert page["funnel"]["screened"] == 2
        assert page["funnel"]["kept"] == 1
        assert page["funnel"]["gateCountsOvercount"] is True
        assert page["funnel"]["gates"]["not_a_software_role"] == 1

    def test_excluded_is_grouped_by_gate_with_quoted_evidence(self) -> None:
        store = FakePostingStore(
            [make(title="Software Engineer", desc="Requirements\nUS citizenship required.\n")]
        )
        page = get(store, path="/public/excluded")
        group = next(g for g in page["groups"] if g["gate"] == "citizenship_or_itar_restricted")
        assert group["count"] == 1
        assert "citizenship required" in group["items"][0]["quote"]
        assert page["counts"]["citizenship_or_itar_restricted"] == 1

    def test_the_internships_section_is_published_separately(self) -> None:
        store = FakePostingStore([make(), an_internship()])
        assert ids_of(get(store)) == [make().id]
        section = get(store, path="/public/internships")
        assert ids_of(section) == [an_internship().id]
        assert section["collection"] == "internships"
        assert section["internshipTotal"] == 1
        assert set(section["items"][0]) == set(get(store)["items"][0])

    def test_a_detail_read_publishes_the_verdict_and_no_prose(self) -> None:
        posting = make()
        found = detail(FakePostingStore([posting]), posting)["posting"]
        assert "description" not in found
        assert found["descriptionChars"] == len(JD_BACKEND)
        assert found["descriptionWithheld"] is True
        assert found["screening"]["kept"] is True
        assert found["screening"]["eligibility"]["checked"] is True

    def test_a_source_with_no_description_is_not_reported_as_withheld(self) -> None:
        """"We are not reprinting this" and "there is nothing here" are different facts."""
        posting = make(desc="", has_desc=False)
        found = detail(FakePostingStore([posting]), posting)["posting"]
        assert found["descriptionStatus"] == worklist_api.DESC_NOT_PROVIDED
        assert found["descriptionWithheld"] is False
        assert found["descriptionChars"] is None

    def test_an_excluded_posting_is_readable_so_the_evidence_is_checkable(self) -> None:
        """The trust surface is only a trust surface if a visitor can click through."""
        posting = make(title="Senior Software Engineer")
        found = detail(FakePostingStore([posting]), posting)["posting"]
        assert found["screening"]["kept"] is False
        assert [e["gate"] for e in found["screening"]["exclusions"]] == ["wrong_seniority_band"]


# ---------------------------------------------------------------------------
# Nothing personal can escape. This section is the guarantee.
# ---------------------------------------------------------------------------

def spike_card(extra: dict[str, Any]) -> Callable[[ScreenDecision], dict[str, Any]]:
    """Wrap the upstream card serialiser so it emits fields it must never publish.

    This is how the allowlist is tested as a *property* rather than by reading it: it
    simulates the thing that actually happens — six months from now the model grows
    an ``appliedAt``, someone adds it to the authenticated card, and nobody
    remembers this file exists.
    """
    original = worklist_api._card

    def spiked(decision: ScreenDecision) -> dict[str, Any]:
        return {**original(decision), **extra}

    return spiked


PRIVATE_FIELDS = {
    "ownerId": "cognito-sub-8f21",
    "appliedAt": "2026-07-29T09:00:00+00:00",
    "applied": True,
    "resumeText": "Ashish Kosana — ashish.private@example.com",
}


class TestNothingPersonalEscapes:
    def test_a_field_added_upstream_is_not_published(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The whole guarantee: applied state and owner id cannot reach a public page.

        A blocklist would publish all four of these, because none of them existed
        when the sanitiser was written.
        """
        store = FakePostingStore([make(), an_internship()])
        monkeypatch.setattr(worklist_api, "_card", spike_card(PRIVATE_FIELDS))

        # The spike really is upstream — otherwise this test proves nothing.
        upstream = body_of(
            worklist_api.list_worklist(
                store, authenticated("/worklist"), resume_text=RESUME, now=NOW
            )
        )
        assert set(PRIVATE_FIELDS) <= set(upstream["items"][0])

        for path in ALL_PATHS:
            published = blob(get(store, path=path))
            for name, value in PRIVATE_FIELDS.items():
                assert name not in published, f"{path} published {name}"
                assert str(value) not in published

        posting = make()
        found = detail(store, posting)["posting"]
        assert not set(PRIVATE_FIELDS) & set(found)

    def test_no_resume_text_reaches_the_wire(self) -> None:
        """This Lambda loads the résumé to score, so the projection is the only guard.

        The score is still published — that is the point — but only as tokens from
        ``gap.VOCAB``, never as text from either document.
        """
        store = FakePostingStore([make(), an_internship()])
        for path in ALL_PATHS:
            published = blob(get(store, path=path))
            for secret in RESUME_SECRETS:
                assert secret not in published, f"{path} leaked {secret!r}"
        posting = make()
        published = blob(detail(store, posting))
        assert all(secret not in published for secret in RESUME_SECRETS)
        assert "Python" in blob(get(store))  # a vocabulary token, not résumé prose

    def test_no_description_prose_is_published(self) -> None:
        """Bulk republication of 234 companies' text is a different act from quoting."""
        posting = make()
        store = FakePostingStore([posting])
        assert PROSE_MARKER not in blob(detail(store, posting))
        assert PROSE_MARKER not in blob(get(store))
        assert PROSE_MARKER not in blob(get(store, path="/public/excluded"))

    def test_board_and_requisition_identifiers_are_dropped(self) -> None:
        """``tenant`` and ``reqId`` are not on the published page and nothing needs them."""
        posting = make()
        found = detail(FakePostingStore([posting]), posting)["posting"]
        assert "tenant" not in found
        assert "reqId" not in found
        assert "acme-board" not in blob(found)
        assert "REQ-4242" not in blob(found)

    def test_the_synthetic_principal_is_never_echoed(self) -> None:
        store = FakePostingStore([make()])
        assert public.PUBLIC_PRINCIPAL not in blob(get(store))

    def test_a_caller_supplied_identity_is_discarded(self) -> None:
        """An open route must not let a caller hand it an identity to read with."""
        hostile = request(authorizer={"jwt": {"claims": {"sub": "someone-elses-sub"}}})
        inner = public.read_event(hostile, "/worklist")
        claims = inner["requestContext"]["authorizer"]["jwt"]["claims"]
        assert claims == {"sub": public.PUBLIC_PRINCIPAL}
        assert "someone-elses-sub" not in blob(get(FakePostingStore([make()])))

    def test_the_read_path_is_not_personalised_at_all(self) -> None:
        """If it ever becomes so, this route is synthesising an id that selects data.

        The synthetic principal is only safe because the corpus is per-installation:
        the JWT subject is an authentication gate, never a selector. Two different
        subjects must therefore produce byte-identical bodies.
        """
        store = FakePostingStore([make(), an_internship()])
        one = authenticated("/worklist")
        two = authenticated("/worklist")
        two["requestContext"]["authorizer"]["jwt"]["claims"]["sub"] = "cognito-sub-2"
        first = worklist_api.list_worklist(store, one, resume_text=RESUME, now=NOW)
        second = worklist_api.list_worklist(store, two, resume_text=RESUME, now=NOW)
        assert first["body"] == second["body"]

    def test_an_unnamed_field_is_dropped_from_a_hand_built_payload(self) -> None:
        """The projection itself, without a route around it."""
        store = FakePostingStore([make()])
        upstream = body_of(
            worklist_api.list_worklist(
                store, authenticated("/worklist"), resume_text=RESUME, now=NOW
            )
        )
        upstream["ownerId"] = "cognito-sub-8f21"
        upstream["items"][0]["appliedAt"] = "2026-07-29T09:00:00+00:00"
        upstream["items"][0]["score"]["resumeText"] = RESUME
        published = public.sanitise_collection(upstream)
        assert "ownerId" not in published
        assert "appliedAt" not in published["items"][0]
        assert "resumeText" not in published["items"][0]["score"]

    @pytest.mark.parametrize("field", ["location", "remote", "descriptionStatus", "level"])
    def test_a_scalar_that_grows_a_body_fails_closed(self, field: str) -> None:
        """Allowlisting a name only guards one level; the value has to be checked too.

        Without this, ``location`` becoming ``{"city": ..., "ownerNotes": ...}``
        upstream would publish every key inside it, none of them ever named here.

        Parametrised across both leaf kinds after ``location`` was moved from a plain
        scalar to ``BOUNDED``: that changed which branch of :func:`public.project`
        checks it, and the bug this test exists to catch is the same for either. A
        version of this test that named only ``location`` would have gone on passing
        while the whole ``_scalar`` guard rotted, because nothing else exercised it.
        """
        store = FakePostingStore([make()])
        upstream = body_of(
            worklist_api.list_worklist(
                store, authenticated("/worklist"), resume_text=RESUME, now=NOW
            )
        )
        upstream["items"][0][field] = {"city": "Tempe", "ownerNotes": "applied 7/29"}
        with pytest.raises(public.ProjectionError):
            public.sanitise_collection(upstream)

    def test_a_renamed_upstream_field_is_a_500_not_a_blank_card(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fail closed: an outage is recoverable, a page of empty columns is not noticed."""
        original = worklist_api._card

        def renamed(decision: ScreenDecision) -> dict[str, Any]:
            card = original(decision)
            card["jobTitle"] = card.pop("title")
            return card

        monkeypatch.setattr(worklist_api, "_card", renamed)
        resp = public.route(
            FakePostingStore([make()]), request(), resume_text=RESUME, now=NOW
        )
        assert resp["statusCode"] == 500
        assert body_of(resp) == {"error": "public_projection_failed"}

    def test_evidence_is_capped_even_when_the_upstream_field_is_not(self) -> None:
        """``reason`` interpolates a title, and real Workday titles run to hundreds of chars."""
        store = FakePostingStore([make(title="Program Manager " * 40)])
        group = next(
            g for g in get(store, path="/public/excluded")["groups"]
            if g["gate"] == "not_a_software_role"
        )
        [row] = group["items"]
        assert len(row["reason"]) == worklist_api.QUOTE_MAX_CHARS
        assert row["reason"].endswith("…")
        assert len(row["quote"]) == worklist_api.QUOTE_MAX_CHARS

    def test_a_long_eligibility_quote_is_capped(self) -> None:
        filler = "We are a wonderful company with many opportunities. " * 12
        posting = make(
            title="Software Engineer",
            desc=f"{filler}\nActive security clearance required.\n{filler}",
        )
        found = detail(FakePostingStore([posting]), posting)["posting"]
        for evidence in found["screening"]["eligibility"]["evidence"]:
            assert len(evidence["quote"]) <= worklist_api.QUOTE_MAX_CHARS
        for exclusion in found["screening"]["exclusions"]:
            assert len(exclusion["reason"]) <= worklist_api.QUOTE_MAX_CHARS

    @pytest.mark.parametrize("field", ["title", "company", "location"])
    def test_card_identity_text_is_bounded_not_merely_allowlisted(self, field: str) -> None:
        """The hole this closes: allowlisting a name says nothing about its length.

        ``title``, ``company`` and ``location`` were plain scalars, so the public route
        republished whatever an upstream feed contained — a 5,000-character value came
        straight out, on an open metered endpoint, while the module docstring claimed
        every field carrying posting text was capped. Bounded at ``CARD_TEXT_MAX_CHARS``
        rather than at 180 because 26 postings in the real corpus have a legitimate
        multi-city ``location`` longer than that, the longest 347 characters.
        """
        store = FakePostingStore([make()])
        upstream = body_of(
            worklist_api.list_worklist(
                store, authenticated("/worklist"), resume_text=RESUME, now=NOW
            )
        )
        upstream["items"][0][field] = "X" * 5000
        row = public.sanitise_collection(upstream)["items"][0]
        assert len(row[field]) == public.CARD_TEXT_MAX_CHARS
        assert row[field].endswith("…")

    def test_a_location_at_the_real_corpus_maximum_is_published_whole(self) -> None:
        """The bound must not truncate real data, or it is a bug wearing a cap's clothes.

        347 characters is the longest ``location`` in the live corpus: a Workday role
        listing 24 cities. Capping card text at ``QUOTE_MAX_CHARS`` would have silently
        cut 26 postings' locations mid-city.
        """
        store = FakePostingStore([make()])
        upstream = body_of(
            worklist_api.list_worklist(
                store, authenticated("/worklist"), resume_text=RESUME, now=NOW
            )
        )
        cities = "; ".join(f"City {n}, ST" for n in range(24))
        assert 180 < len(cities) < public.CARD_TEXT_MAX_CHARS
        upstream["items"][0]["location"] = cities
        row = public.sanitise_collection(upstream)["items"][0]
        assert row["location"] == cities

    def test_an_oversized_url_fails_closed_rather_than_publishing_a_broken_link(self) -> None:
        """Truncation is honest for a sentence and a lie for a URL.

        A shortened ``location`` still reads as a location. A shortened ``url`` renders
        as a live "Apply" button that 404s, so this is the one card field where going
        over the bound raises instead of trimming — and the route turns that into a 500
        rather than a page of dead links.
        """
        store = FakePostingStore([make()])
        upstream = body_of(
            worklist_api.list_worklist(
                store, authenticated("/worklist"), resume_text=RESUME, now=NOW
            )
        )
        upstream["items"][0]["url"] = "https://boards.example/" + "x" * 5000
        with pytest.raises(public.ProjectionError, match="broken link"):
            public.sanitise_collection(upstream)

    def test_every_published_string_in_a_full_sweep_is_bounded(self) -> None:
        """The property, not the four field names: nothing unbounded reaches the wire.

        Written as a walk over the whole payload so that a *new* allowlisted field
        added as a plain scalar fails here, which is the mistake this pair of caps was
        introduced to catch in the first place.
        """
        store = FakePostingStore([make(title="Program Manager " * 40), make(day=2)])
        payloads = [
            get(store, path="/public/worklist"),
            get(store, path="/public/excluded"),
            get(store, path="/public/internships"),
        ]
        for payload in payloads:
            for path, text in _strings(payload):
                assert len(text) <= public.CARD_TEXT_MAX_CHARS, path

    def test_a_count_map_cannot_smuggle_an_object(self) -> None:
        """``counts`` has open keys, so its *values* are what has to be checked."""
        store = FakePostingStore([make(title="Product Manager")])
        upstream = body_of(
            worklist_api.list_excluded(store, authenticated("/excluded"), now=NOW)
        )
        upstream["counts"]["not_a_software_role"] = {"n": 1, "ownerId": "cognito-sub-8f21"}
        with pytest.raises(public.ProjectionError):
            public.sanitise_excluded(upstream)


# ---------------------------------------------------------------------------
# Read-only, structurally
# ---------------------------------------------------------------------------

class TestReadOnly:
    def test_a_full_sweep_touches_only_open_postings(self) -> None:
        """Every public route, one store: nothing but reads may be observed."""
        posting = make()
        store = FakePostingStore([posting, an_internship()])
        for path in ALL_PATHS:
            get(store, path=path)
        detail(store, posting)
        assert set(store.calls) == {"open_postings"}
        assert store.applied == {}

    @pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
    def test_a_write_verb_never_reaches_the_store(self, method: str) -> None:
        """The method check runs before dispatch, so no handler and no store is touched."""
        store = FakePostingStore([make()])
        resp = public.route(
            store,
            request(method=method, body=json.dumps({"postingId": make().id})),
            resume_text=RESUME,
            now=NOW,
        )
        assert resp["statusCode"] == 405
        assert body_of(resp) == {"error": "method_not_allowed"}
        assert store.calls == []
        assert store.applied == {}

    def test_there_is_no_public_applied_route(self) -> None:
        """``/applied`` is not addressable from here under any verb."""
        store = FakePostingStore([make()])
        for method in ("GET", "POST"):
            resp = public.route(
                store, request(path="/public/applied", method=method), now=NOW
            )
            assert resp["statusCode"] == 404
        assert store.applied == {}

    def test_a_body_on_a_public_read_is_ignored(self) -> None:
        """A GET carrying an applied payload is served as the read it claimed to be."""
        posting = make()
        store = FakePostingStore([posting])
        page = get(store, body=json.dumps({"postingId": posting.id}))
        assert ids_of(page) == [posting.id]
        assert store.applied == {}
        assert "body" not in public.read_event(
            request(body='{"postingId": "x"}'), "/worklist"
        )

    def test_the_module_cannot_submit_or_mutate_anything(self) -> None:
        """Asserted structurally: "nothing may auto-apply" is a product invariant.

        A comment does not enforce it, and this module is the one that faces the
        open internet.
        """
        tree = ast.parse(Path(public.__file__).read_text())
        imported: set[str] = set()
        # Identifiers the code actually *uses*, not strings found in the file: the
        # module docstring explains at length which writes are unreachable, and a
        # grep over raw source would fail on its own explanation.
        used: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
            elif isinstance(node, ast.Name):
                used.add(node.id)
            elif isinstance(node, ast.Attribute):
                used.add(node.attr)
        forbidden_imports = {
            "urllib", "http", "requests", "httpx", "socket", "smtplib", "ftplib",
            "webbrowser", "selenium", "playwright", "boto3", "botocore", "subprocess",
        }
        assert not imported & forbidden_imports, f"network-capable: {imported & forbidden_imports}"
        writes = {"record_applied", "mark_applied", "sync", "close_missing",
                  "save_interpretation", "urlopen", "webdriver"}
        assert not used & writes, f"the public module reaches {used & writes}"


# ---------------------------------------------------------------------------
# Requests: the query allowlist, paging, errors
# ---------------------------------------------------------------------------

class TestRequestProjection:
    def test_the_documented_filters_are_carried_across(self) -> None:
        store = FakePostingStore([make(), make(title="Software Engineer", day=2)])
        assert get(store, query={"level": "entry"})["matched"] == 1
        assert get(store, query={"level": "unknown"})["matched"] == 1
        assert get(store, query={"tier": "strong"})["matched"] == 2
        assert get(store, query={"tier": "exact_match"})["matched"] == 0
        assert get(store, query={"ats": "workday"})["matched"] == 0

    def test_a_parameter_nobody_documented_is_dropped(self) -> None:
        """It cannot change a read it never reaches — including one named like a claim."""
        store = FakePostingStore([make()])
        plain = get(store)
        assert get(store, query={"sub": "someone-else", "resume": "/etc/passwd"}) == plain

    def test_repeated_parameters_survive_the_projection(self) -> None:
        """REST APIs put repeats in multiValueQueryStringParameters and nowhere else."""
        raw = request()
        raw["multiValueQueryStringParameters"] = {"level": ["entry", "unknown"], "x": ["y"]}
        resp = public.route(
            FakePostingStore([make(), make(title="Software Engineer", day=2)]),
            raw,
            resume_text=RESUME,
            now=NOW,
        )
        assert body_of(resp)["matched"] == 2
        assert "x" not in public.read_event(raw, "/worklist")[
            "multiValueQueryStringParameters"
        ]

    def test_paging_works_end_to_end_through_the_public_route(self) -> None:
        store = FakePostingStore(make(day=d, url=f"https://x/{d}") for d in range(1, 8))
        first = get(store, query={"limit": "3"})
        assert first["page"]["hasMore"] is True
        second = get(store, query={"limit": "3", "cursor": first["page"]["nextCursor"]})
        assert not set(ids_of(first)) & set(ids_of(second))

    def test_an_internships_cursor_does_not_continue_the_worklist(self) -> None:
        store = FakePostingStore(
            [make(day=d, url=f"https://x/{d}") for d in range(1, 6)] + [an_internship()]
        )
        cursor = get(store, query={"limit": "2"})["page"]["nextCursor"]
        resp = public.route(
            store,
            request(path="/public/internships", query={"limit": "2", "cursor": cursor}),
            resume_text=RESUME,
            now=NOW,
        )
        assert resp["statusCode"] == 400
        assert body_of(resp) == {"error": "cursor_filter_mismatch"}

    @pytest.mark.parametrize(
        ("query", "code"),
        [
            ({"limit": "10000"}, "invalid_limit"),
            ({"tier": "amazing"}, "invalid_tier"),
            ({"cursor": "not-base64-!!"}, "invalid_cursor"),
        ],
    )
    def test_a_bad_parameter_is_the_same_400_it_is_upstream(
        self, query: dict[str, str], code: str
    ) -> None:
        """Refused, not clamped: a public ``limit=10000`` must not look like it worked."""
        resp = public.route(
            FakePostingStore([make()]), request(query=query), resume_text=RESUME, now=NOW
        )
        assert resp["statusCode"] == 400
        assert body_of(resp) == {"error": code}

    def test_an_unknown_posting_is_404(self) -> None:
        resp = public.route(
            FakePostingStore([make()]),
            request(path="/public/worklist/0000", path_params={"id": "0000"}),
            now=NOW,
        )
        assert resp["statusCode"] == 404
        assert body_of(resp) == {"error": "posting_not_found"}

    def test_an_id_only_in_the_raw_path_still_resolves(self) -> None:
        """API Gateway does not always attach pathParameters; the tail is the fallback."""
        posting = make()
        resp = public.route(
            FakePostingStore([posting]),
            request(path=f"/public/worklist/{posting.id}"),
            resume_text=RESUME,
            now=NOW,
        )
        assert resp["statusCode"] == 200
        assert body_of(resp)["posting"]["id"] == posting.id

    def test_an_unsubstituted_route_template_is_400(self) -> None:
        """`/public/worklist/{id}` with no pathParameters is a wiring fault, not a 404."""
        resp = public.route(
            FakePostingStore([make()]),
            request(path="/public/worklist/{id}"),
            resume_text=RESUME,
            now=NOW,
        )
        assert resp["statusCode"] == 400
        assert body_of(resp) == {"error": "missing_posting_id"}

    def test_a_store_outage_is_503_not_500(self) -> None:
        resp = public.route(
            FakePostingStore([make()], raises=True), request(), resume_text=RESUME, now=NOW
        )
        assert resp["statusCode"] == 503
        assert body_of(resp) == {"error": "store_unavailable"}

    def test_a_lost_principal_reads_as_our_fault_not_the_visitor_s(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A 401 on an anonymous route would ask a visitor to fix a wiring bug."""
        monkeypatch.setattr(public, "PUBLIC_PRINCIPAL", "")
        resp = public.route(
            FakePostingStore([make()]), request(), resume_text=RESUME, now=NOW
        )
        assert resp["statusCode"] == 500
        assert body_of(resp) == {"error": "public_route_misconfigured"}

    def test_scoring_degrades_rather_than_reporting_zeros(self) -> None:
        """With no résumé configured, every requirement would read as missing."""
        page = body_of(
            public.route(FakePostingStore([make()]), request(), resume_text="", now=NOW)
        )
        assert page["scoring"] == {"available": False, "reason": "no_resume_configured"}
        assert page["items"][0]["score"] is None


# ---------------------------------------------------------------------------
# Routing, CORS, cost
# ---------------------------------------------------------------------------

class TestRoutingAndCost:
    def test_every_public_route_is_reachable(self) -> None:
        posting = make()
        store = FakePostingStore([posting, an_internship()])
        for path in ALL_PATHS:
            assert public.route(store, request(path=path), now=NOW)["statusCode"] == 200
        assert public.route(
            store,
            request(path=f"/public/worklist/{posting.id}", path_params={"id": posting.id}),
            now=NOW,
        )["statusCode"] == 200

    def test_a_rest_api_event_shape_routes(self) -> None:
        """REST APIs send httpMethod + resource; the HTTP API sends routeKey."""
        raw = {"httpMethod": "GET", "resource": "/public/excluded"}
        assert public.route(FakePostingStore([make()]), raw, now=NOW)["statusCode"] == 200

    def test_a_trailing_slash_lists_rather_than_404s(self) -> None:
        store = FakePostingStore([make()])
        assert public.route(store, request(path="/public/worklist/"), now=NOW)[
            "statusCode"
        ] == 200

    def test_an_unknown_path_is_404(self) -> None:
        resp = public.route(FakePostingStore([make()]), request(path="/public/nope"), now=NOW)
        assert resp["statusCode"] == 404
        assert body_of(resp) == {"error": "not_found"}

    def test_the_authenticated_paths_are_not_served_here(self) -> None:
        """This function answers ``/public/*`` only; the bare paths keep their authorizer."""
        store = FakePostingStore([make()])
        for path in ("/worklist", "/excluded", "/internships", "/applied"):
            assert public.route(store, request(path=path), now=NOW)["statusCode"] == 404

    def test_preflight_advertises_reads_only_and_needs_no_store(self) -> None:
        resp = public.route(
            FakePostingStore(raises=True), request(method="OPTIONS"), now=NOW
        )
        assert resp["statusCode"] == 204
        assert resp["body"] == ""
        assert resp["headers"]["Access-Control-Allow-Methods"] == "GET,OPTIONS"
        # No Authorization header is advertised: this route has no caller identity.
        assert "Authorization" not in resp["headers"]["Access-Control-Allow-Headers"]

    def test_a_successful_read_is_cacheable_so_repeats_cost_nothing(self) -> None:
        """An open endpoint with no cache header pays for a Lambda on every reload."""
        store = FakePostingStore([make()])
        cacheable = f"public, max-age={public.CACHE_MAX_AGE_SECONDS}"
        for event in (request(), request(method="OPTIONS")):
            resp = public.route(store, event, resume_text=RESUME, now=NOW)
            assert resp["headers"]["Cache-Control"] == cacheable

    @pytest.mark.parametrize(
        ("event", "expected"),
        [
            (request(path="/public/nope"), 404),
            (request(query={"limit": "10000"}), 400),
            (request(method="POST"), 405),
        ],
    )
    def test_an_error_is_never_cached(self, event: dict[str, Any], expected: int) -> None:
        """A cached error outlives its cause: a 503 keeps the page broken after recovery."""
        resp = public.route(
            FakePostingStore([make()]), event, resume_text=RESUME, now=NOW
        )
        assert resp["statusCode"] == expected
        assert resp["headers"]["Cache-Control"] == "no-store"

    def test_a_store_outage_is_not_cached_either(self) -> None:
        resp = public.route(
            FakePostingStore([make()], raises=True), request(), resume_text=RESUME, now=NOW
        )
        assert resp["statusCode"] == 503
        assert resp["headers"]["Cache-Control"] == "no-store"

    def test_the_cache_header_never_outlives_the_index(self) -> None:
        """Promising fresher data than the index can hold would be a lie in a header."""
        assert public.CACHE_MAX_AGE_SECONDS <= worklist_api.INDEX_TTL_SECONDS

    def test_a_warm_container_screens_once_across_all_four_routes(self) -> None:
        """25,294 postings re-screened per request is the bill this prevents."""
        posting = make()
        store = FakePostingStore([posting, an_internship()])
        cache = worklist_api.IndexCache(ttl_seconds=300)
        for path in ALL_PATHS:
            public.route(store, request(path=path), resume_text=RESUME, now=NOW, cache=cache)
        public.route(
            store,
            request(path=f"/public/worklist/{posting.id}", path_params={"id": posting.id}),
            resume_text=RESUME,
            now=NOW,
            cache=cache,
        )
        assert store.reads == 1

    def test_the_index_still_expires(self) -> None:
        store = FakePostingStore([make()])
        cache = worklist_api.IndexCache(ttl_seconds=60)
        public.route(store, request(), resume_text=RESUME, now=NOW, cache=cache)
        public.route(
            store, request(), resume_text=RESUME, now=NOW + timedelta(seconds=61), cache=cache
        )
        assert store.reads == 2

    def test_a_page_is_capped_so_one_request_cannot_pull_the_corpus(self) -> None:
        store = FakePostingStore(make(day=d, url=f"https://x/{d}") for d in range(1, 29))
        assert get(store)["page"]["count"] == worklist_api.DEFAULT_LIMIT
        resp = public.route(
            store,
            request(query={"limit": str(worklist_api.MAX_LIMIT + 1)}),
            resume_text=RESUME,
            now=NOW,
        )
        assert resp["statusCode"] == 400
