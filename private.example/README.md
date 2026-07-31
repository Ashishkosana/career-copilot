# `private.example/` — placeholders for the personal half

This repository is public; the content that makes it *mine* is not. Everything
in `private/` is gitignored: the resume template, the tailoring prompts, and the
answers I give on application forms.

This directory is the committed skeleton. Copy it and fill it in:

```bash
cp -r private.example private
```

Nothing in the codebase hardcodes personal values — every path below is read
through `copilot.config.Settings`, so the code is public and complete while the
content stays local.

## What goes where

| Path | What it holds | Why it is private |
|---|---|---|
| `resume/` | The résumé HTML + CSS template, and the content source | It is a personal document, and the template is the thing every commercial tool replaces with its own |
| `prompts/` | The tailoring and answer-writing prompts | These are tuned against my own material and encode how I want to be written about |
| `answers.json` | Application-form answer library | Work authorisation, sponsorship, salary expectation, EEO responses |
| `profile.json` | Name, email, phone, links | Contact details |

## What is deliberately *not* private

- **All source code**, including the validators, the gates, and the ATS adapters.
- `data/watchlist.json` — derived entirely from Simplify's public listings feed.
- `data/sponsorship_hints.json` — same source.

The interesting engineering is public. Only the personal content is not.

## Answer-library shape

`answers.json` is keyed by a normalised hash of the question label, so the same
question phrased differently across two employers resolves to one answer. Each
entry carries an **ordered alias list**: dropdown option sets differ per company,
so the tool picks the first alias present in the actual options and — if none
match — flags the question rather than guessing.

```json
{
  "work_authorization": {
    "question_examples": [
      "Are you legally authorized to work in the United States?"
    ],
    "aliases": ["Yes", "Yes, I am authorized to work in the US"],
    "locked": true
  },
  "requires_sponsorship": {
    "question_examples": [
      "Will you now or in the future require sponsorship?"
    ],
    "aliases": ["Yes"],
    "locked": true,
    "note": "Answered honestly. Locked fields require an explicit confirm to change."
  }
}
```

`locked: true` marks fields where a wrong value is genuinely costly. The tool
refuses to change them without an explicit confirmation.
