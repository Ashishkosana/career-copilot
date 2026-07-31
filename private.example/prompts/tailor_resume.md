# Résumé tailoring prompt (placeholder)

Copy to `private/prompts/tailor_resume.md` and write your own. This placeholder
documents the **contract** the code expects, so the repo runs without the real one.

## What the code guarantees around this prompt

The prompt is not trusted. Whatever it returns passes through deterministic
validators in `copilot.domain.tailoring` that **fail closed** — if a check trips,
the build produces no résumé rather than a résumé with a false claim in it:

1. **Technology-token check** — every technology named in the output must already
   appear in the source résumé. Set difference must be empty.
2. **Numeric-verbatim check** — every number in the output must appear verbatim in
   the source. Invented metrics are the most common LLM résumé failure.
3. **Temporal check** — dates and durations are unchanged.
4. **Structural check** — bullet count and section order are unchanged.
5. **Placeholder check** — no `[X]` markers survive into the final document.
6. **Page-count check** — the rendered PDF is still exactly one page.

## Required output shape

Structured output only, keyed by the stable bullet IDs from the content file:

```json
{
  "bullets": [
    {
      "id": "exp.crewtron.b1",
      "rewritten": "...",
      "source_span": "the exact text from the original this is derived from"
    }
  ]
}
```

`source_span` exists so validator 1 and 2 can be checked mechanically rather than
trusted.

## Constraints the prompt must state

- Rewrite only. Do not add, remove, merge or reorder bullets.
- Use no technology, tool, company or metric that is not in the source résumé.
- If a metric would strengthen a bullet but is not in the source, emit `[X]` and
  let the human fill it in. Never estimate one.
- Preserve first-person voice and tense.
- Keep each rewritten bullet within ±10% of the original character count, because
  the layout has almost no vertical slack.
