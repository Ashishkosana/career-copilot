"""Tests for Claude draft-replies — the key safety property is: DRAFTS only.

We never call the real Anthropic API here; draft_reply is monkeypatched so the
loop logic (per-email drafting, counting, error isolation) is what's covered.
"""
from career_copilot import drafts


def test_no_api_key_is_noop(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    calls = []
    n = drafts.draft_replies(
        [{"from": "r@x.com", "subject": "Interview?", "snippet": "schedule"}],
        lambda to, subj, body: calls.append((to, subj, body)),
    )
    assert n == 0
    assert calls == []  # never touched Gmail without a key


def test_creates_one_draft_per_email(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setattr(drafts, "draft_reply", lambda msg: f"Reply to {msg['subject']}")
    calls = []
    msgs = [
        {"from": "a@x.com", "subject": "Interview with Cribl", "snippet": "..."},
        {"from": "b@x.com", "subject": "Coding assessment", "snippet": "..."},
    ]
    n = drafts.draft_replies(msgs, lambda to, subj, body: calls.append((to, subj, body)))
    assert n == 2
    assert calls[0] == ("a@x.com", "Re: Interview with Cribl", "Reply to Interview with Cribl")


def test_one_bad_draft_does_not_sink_the_batch(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")

    def flaky(msg):
        if "boom" in msg["subject"]:
            raise RuntimeError("model error")
        return "ok"

    monkeypatch.setattr(drafts, "draft_reply", flaky)
    n = drafts.draft_replies(
        [{"from": "a", "subject": "boom", "snippet": ""},
         {"from": "b", "subject": "fine", "snippet": ""}],
        lambda *a: None,
    )
    assert n == 1  # the good one still drafted


def test_empty_body_is_not_drafted(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setattr(drafts, "draft_reply", lambda msg: "")
    calls = []
    n = drafts.draft_replies([{"from": "a", "subject": "x", "snippet": ""}],
                             lambda *a: calls.append(a))
    assert n == 0 and calls == []
