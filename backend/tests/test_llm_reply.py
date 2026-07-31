"""Unit tests for the LLM reply drafter: prompt building, degrade, injected client."""
from __future__ import annotations

from typing import Any

from copilot.adapters.llm_reply import LlmReplyDrafter
from copilot.domain.models import Email

_EMAIL = Email(
    sender="recruiter@acme.com",
    subject="Interview invitation",
    snippet="Are you free next week?",
)


def test_build_prompt_includes_email_fields() -> None:
    prompt = LlmReplyDrafter._build_prompt(_EMAIL)
    assert "recruiter@acme.com" in prompt
    assert "Interview invitation" in prompt
    assert "Are you free next week?" in prompt


def test_draft_reply_degrades_to_empty_without_key() -> None:
    assert LlmReplyDrafter(api_key="").draft_reply(_EMAIL) == ""


class _FakeBlock:
    """One content block. Typed, because the adapter joins only ``type == "text"``."""

    def __init__(self, text: str, kind: str = "text") -> None:
        self.text = text
        self.type = kind


class _FakeResp:
    def __init__(self, *blocks: _FakeBlock) -> None:
        self.content = list(blocks)


class _FakeMessages:
    def __init__(self, *blocks: _FakeBlock) -> None:
        self._blocks = blocks
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> _FakeResp:
        self.calls.append(kwargs)
        return _FakeResp(*self._blocks)


class _FakeClient:
    """Shaped like ``anthropic.Anthropic``: ``client.messages.create(...)``.

    The reply drafter used to call a different vendor's ``models.generate_content``
    and read ``resp.text``. This fake is the reason the switch was more than a
    model-name change — a response here is a *list of typed blocks*, so a fake with
    a bare ``.text`` would have let the adapter ship reading an attribute that does
    not exist on a real response.
    """

    def __init__(self, text: str) -> None:
        self.messages = _FakeMessages(_FakeBlock(text))


def test_draft_reply_uses_injected_client() -> None:
    client = _FakeClient("  Happy to chat — I'm free Tuesday.  ")
    drafter = LlmReplyDrafter(model="test-model", client=client)

    body = drafter.draft_reply(_EMAIL)

    assert body == "Happy to chat — I'm free Tuesday."
    assert client.messages.calls[0]["model"] == "test-model"
    assert client.messages.calls[0]["max_tokens"] > 0, "an uncapped draft can run away"


def test_draft_reply_empty_text_degrades_to_empty() -> None:
    drafter = LlmReplyDrafter(client=_FakeClient("   "))
    assert drafter.draft_reply(_EMAIL) == ""


def test_only_text_blocks_reach_the_draft() -> None:
    """A non-text block must be skipped, not stringified into a recruiter reply.

    The response is a list of typed blocks. Joining them blindly would put a repr
    of a future block type into an email, and a reply to a recruiter is the last
    place to find that out.
    """
    client = _FakeClient("ignored")
    client.messages = _FakeMessages(
        _FakeBlock("Happy to chat.", "text"),
        _FakeBlock("{...}", "tool_use"),
        _FakeBlock(" Tuesday works.", "text"),
    )
    drafter = LlmReplyDrafter(client=client)

    assert drafter.draft_reply(_EMAIL) == "Happy to chat.\n Tuesday works."


def test_a_response_with_no_text_block_degrades_to_empty() -> None:
    client = _FakeClient("ignored")
    client.messages = _FakeMessages(_FakeBlock("{...}", "tool_use"))
    assert LlmReplyDrafter(client=client).draft_reply(_EMAIL) == ""
