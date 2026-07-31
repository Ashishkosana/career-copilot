"""The one invariant that is a promise to the user, not a design preference.

**Nothing in this codebase may submit an application, anywhere, ever.** Not on a
schedule, not on a button, not "with confirmation". The value of the product is
that a human decides; a tool that can apply on your behalf is a different and much
worse product, and the failure is unrecoverable — you cannot un-apply.

That is normally enforced by a code review noticing. This module enforces it by
walking the AST of every shipped module, because the review that matters is the one
that happens on the change nobody thought was risky. Three properties are asserted:

1. Exactly one function in the package can issue a request with a body, it lives in
   the ATS HTTP helper, and its only caller is Workday's *search* endpoint — a read
   that happens to be spelled POST because that is the API Workday exposes.
2. No module outside the ATS package imports an HTTP client at all. The read API,
   the domain and the services have no way to reach the network.
3. ``POST /applied`` writes one timestamp through ``PostingStorePort.mark_applied``
   and calls nothing else that could reach a third party.

A grep would be fooled by ``getattr(client, "po" + "st")``; the AST walk is not
fooled by formatting, and unlike a comment it fails the build.
"""
from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "copilot"

#: The only module allowed to hold an HTTP client. Everything the product does over
#: the network is a public job board read, and it all goes through here.
HTTP_MODULE = SRC / "adapters" / "ats" / "_http.py"

#: Modules permitted to reach the network at all. ``ats/`` reads job boards;
#: ``gmail_mailbox`` and ``claude_interpreter``/``llm_reply`` reach their own
#: vendor SDK, which is not an HTTP client import.
NETWORK_LIBRARIES = frozenset(
    {"urllib", "urllib.request", "http.client", "requests", "httpx", "aiohttp", "socket"}
)


def _modules() -> list[Path]:
    return sorted(p for p in SRC.rglob("*.py") if "__pycache__" not in p.parts)


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _imported_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module)
    return names


class TestNoHttpClientOutsideTheAtsReader:
    def test_only_the_ats_helper_imports_an_http_client(self) -> None:
        offenders: dict[str, set[str]] = {}
        for path in _modules():
            if path == HTTP_MODULE:
                continue
            found = _imported_names(_tree(path)) & NETWORK_LIBRARIES
            # urllib.parse is string manipulation, not a client.
            found -= {"urllib.parse"}
            if found:
                offenders[str(path.relative_to(SRC))] = found
        assert offenders == {}, (
            f"new network reach outside the ATS reader: {offenders}. "
            "If this is a job-board read it belongs in adapters/ats/_http.py; if it "
            "is anything else, it must not exist."
        )

    def test_the_ats_helper_is_the_only_place_a_request_body_is_built(self) -> None:
        """``data=`` on a urllib Request is the only way this package can write."""
        senders: list[str] = []
        for path in _modules():
            for node in ast.walk(_tree(path)):
                if not isinstance(node, ast.Call):
                    continue
                if any(kw.arg == "data" for kw in node.keywords) and _is_urllib_request(node):
                    senders.append(str(path.relative_to(SRC)))
        assert senders == ["adapters/ats/_http.py"], senders


def _is_urllib_request(node: ast.Call) -> bool:
    target = node.func
    name = target.attr if isinstance(target, ast.Attribute) else getattr(target, "id", "")
    return name == "Request"


class TestPostJsonIsOnlyEverAWorkdaySearch:
    def test_exactly_one_caller(self) -> None:
        """If a second module starts POSTing, this test names it.

        Workday's job *search* is a POST because that is the endpoint Workday
        publishes — the body is ``{searchText, limit, offset}``. It is a read.
        """
        callers = [
            str(path.relative_to(SRC))
            for path in _modules()
            if any(
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "post_json"
                for node in ast.walk(_tree(path))
            )
        ]
        assert callers == ["adapters/ats/workday.py"], callers

    def test_the_workday_body_is_a_search_not_a_submission(self) -> None:
        source = (SRC / "adapters" / "ats" / "workday.py").read_text(encoding="utf-8")
        keys = {"appliedFacets", "limit", "offset", "searchText"}
        for key in keys:
            assert f'"{key}"' in source, f"the Workday POST body no longer contains {key}"
        for forbidden in ("candidate", "resume", "coverLetter", "application", "answers"):
            assert forbidden not in source, (
                f"{forbidden!r} appeared in the Workday adapter — this adapter reads "
                "a job search and must never carry applicant data"
            )


class TestRecordAppliedOnlyRecords:
    def test_it_calls_nothing_but_mark_applied_on_the_store(self) -> None:
        """The handler that sounds most like it applies must be the one that cannot."""
        module = _tree(SRC / "handlers" / "worklist_api.py")
        marker = next(
            node
            for node in ast.walk(module)
            if isinstance(node, ast.FunctionDef) and node.name == "_mark_applied"
        )
        store_calls = {
            node.func.attr
            for node in ast.walk(marker)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        assert "mark_applied" in store_calls
        assert store_calls <= {"mark_applied", "exception"}, store_calls

    def test_the_response_says_it_did_not_submit(self) -> None:
        source = (SRC / "handlers" / "worklist_api.py").read_text(encoding="utf-8")
        assert '"submitted": False' in source
        assert "never submits an application anywhere" in source


class TestTheMailboxCannotSendToAThirdParty:
    def test_replies_are_drafted_and_only_the_owner_is_ever_sent_to(self) -> None:
        """``create_draft`` for other people; ``send`` only for ``my_email``.

        A single misplaced ``send`` here would email a recruiter an LLM-written
        reply nobody read — the same irreversible class of mistake as auto-applying.
        """
        service = _tree(SRC / "services" / "daily_briefing.py")
        sends = [
            node
            for node in ast.walk(service)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "send"
        ]
        assert len(sends) == 1, "more than one mailbox.send in the service"
        [call] = sends
        recipients = [kw.value for kw in call.keywords if kw.arg == "to"]
        assert len(recipients) == 1
        # `to=to`, where `to` is `_email_briefing`'s keyword-only owner address.
        assert isinstance(recipients[0], ast.Name) and recipients[0].id == "to"
