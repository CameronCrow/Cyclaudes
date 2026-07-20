---
type: reference
tags: [repo/Cyclaudes]
up: "[[Repos/Cyclaudes/planning/PLAN_MAIN|PLAN_MAIN]]"
---
# Phase 3 - The autonomous trigger

**Goal:** verification fires **without being asked**. This is the phase that actually removes
Cameron from the loop — everything before it is machinery.

Deliberately sequenced last of the core three. A trigger that fires *unreliable* verification is
worse than no trigger: it converts a visible stall into an invisible false pass.

## Deliverables

**1. Plugin packaging.**
The delivery shape decided in [[cyclaudes-scope-decisions]]. The plugin exists to carry the
trigger — that is its whole justification over a bare MCP server.

**2. Criteria capture at implement-time.**
The hard problem from [[cyclaudes-core-use-case]]. Before/while implementing a UI-affecting change,
the agent writes down expected post-conditions as checks. Captured *before* the change, so they
describe intent rather than rationalising whatever the code ended up doing.

**3. The trigger itself.**
A hook/skill firing after a change lands, when the result is visual or interactive. Must include a
cheap **relevance test** — most changes don't touch the UI, and verifying every one would be slow
enough that Cameron disables the tool.

**4. Loop integration — the three outcomes.**
- **Pass** → continue. No interruption.
- **Fail** → the agent gets an actionable diff (expected vs actual tree state) and self-corrects,
  then re-verifies.
- **Abstain** → escalate to Cameron *with specifics*: what it tried, why it couldn't tell. An
  abstention should read as a useful question, not a shrug.

**5. Bounded retry.**
Cap the correct→verify cycles. On exhaustion, escalate. An agent thrashing invisibly against a
check it cannot satisfy is a worse failure than the original stall — it burns tokens and time
while looking like progress.

## Key risk

This phase is where a false pass becomes *invisible*. In Phases 1–2 a human is reading the output;
here nobody is. The abstention path must stay loud and frequent. If early runs show abstentions
being quietly swallowed or rationalised into passes, stop and fix that before proceeding.

## Success criteria

1. A full issue resolution — implement, verify, self-correct, re-verify — completes with **zero**
   Cameron input.
2. An unverifiable change escalates **promptly**, rather than looping or guessing.
3. A change that breaks the UI is caught by the trigger, not by Cameron noticing later.
4. Non-UI changes are not slowed down measurably.

## Open questions

- Hook vs skill vs both — depends on what Claude Code plugins can actually fire on. Confirm the
  supported trigger points before committing to a shape.
- Where do captured criteria live — alongside the code as durable regression tests (preferred), or
  as ephemeral per-task artifacts?
- How does the agent decide a change is "UI-affecting" cheaply and reliably?

## Related

- [[Repos/Cyclaudes/planning/PHASE_2|PHASE_2]] — must be reliable before this is safe
- [[Repos/Cyclaudes/planning/PLAN_MAIN|PLAN_MAIN]]
