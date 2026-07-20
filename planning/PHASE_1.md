---
type: reference
tags: [repo/Cyclaudes]
up: "[[Repos/Cyclaudes/planning/PLAN_MAIN|PLAN_MAIN]]"
---
# Phase 1 - The verification contract

**Goal:** prove an agent can declare UI acceptance criteria *before* a change and assert them
*after* — with no human in the loop — and honestly report when it cannot.

This phase deliberately targets the hard part (judgment) and not the easy part (plumbing). The
auto-trigger is trivial to add later; a trigger that fires unreliable verification is worse than
no trigger at all. See [[cyclaudes-core-use-case]].

## Key decision: a UI check is a pytest test

Not a new DSL, not a new runner, not a new report format.

`touchpoint` reads the accessibility tree; pytest supplies everything around it — discovery,
fixtures, setup/teardown, assertions, failure reporting, and an exit code the agent already knows
how to interpret. Cameron's "expand verification beyond pytest" means widening *what gets
asserted* (UI state, not just function returns), not replacing the runner.

Consequences:
- The agent already knows how to write, run, and read these. No new skill to learn.
- Claude Code's existing test-verify loop works unchanged.
- Checks are durable artifacts — they become regression tests for free.

## Portability constraint (not a non-goal)

**Cameron moves to a Mac in ~2 weeks** (confirmed 2026-07-20). macOS *validation* stays in
[[Repos/Cyclaudes/planning/PHASE_5|PHASE_5]], but **portability is a Phase 1 constraint** — if this
bakes in Windows assumptions, it stops working exactly when he needs it most.

Concretely, in this phase:
- **Never hardcode state vocabulary.** `checked`/`pressed` are UIA-specific; AX differs. Compare
  states as opaque strings discovered from the tree, and always report actual states on failure.
- **Never hardcode role names** or assume UIA's fixed control-type enum — the AX tree is untyped
  by comparison.
- **No `uia`-prefixed ID parsing.** IDs are opaque handles; treat them as such.
- Permission failure must be a distinct, clearly-named **abstention** — on macOS a missing TCC
  Accessibility grant yields an empty tree, which must never read as "nothing is broken."

## Non-goals for Phase 1

Called out so they don't creep in:
- Auto-trigger (hooks / skill firing on its own) — Phase 3, once verification is trustworthy.
- Vision fallback — Phase 4. Structural only for now.
- macOS *validation and permissions work* — Phase 5. Portability discipline applies now; running
  and proving it on a Mac does not.
- App lifecycle orchestration (launch, navigate to state-under-test) — acknowledged as the
  biggest under-scoped piece; deferred to Phase 2 with a manual precondition for now.

## Deliverables

**1. `src/cyclaudes/ui.py` — a thin discipline layer over touchpoint.**
Not a re-implementation. Its only job is making the four known footguns unrepresentable
(see [[cyclaudes-touchpoint-findings]]):

| Footgun | Mitigation baked into the API |
|---|---|
| Action returns lie (`close_window: OK` while blocked) | Every action re-snapshots and re-asserts; never returns the raw touchpoint result |
| Element IDs churn on tree mutation | API takes *names/queries only* — raw IDs are never exposed to the caller, so they can't be cached |
| `wait_for_window` substring-matches and grabs the wrong window | Explicit window resolution via `windows()`; raise loudly on ambiguous matches instead of picking one |
| State vocabulary must be guessed (`checked` vs `selected`) | Failed state assertions report the element's *actual* states in the message |

Sketch (shape, not final):
```python
win = ui.window(app="Notepad", title="Untitled")   # raises on ambiguity, never guesses
win.set_value("Text editor", "hello", replace=True)
win.assert_text("Text editor", "hello")            # re-snapshots; does not trust set_value
win.assert_state("Bold (Ctrl+B)", "checked")       # failure prints actual states
```

**2. `CannotVerify` + a pytest outcome for it.**
The trust boundary, and the one place not to be lazy. A bogus pass is worse than the stall it
replaces: it silently ships broken work and permanently burns trust in the tool.

- `raise CannotVerify("reason")` from a check that genuinely cannot be evaluated.
- A `pytest_runtest_makereport` hook reports these as their own outcome — visually distinct from
  both pass and fail, so the agent can never read abstention as success.
- Abstention must be a *normal, frequently-taken* path, not a rare error case.

**3. A `verify-ui` skill.**
Tells the agent the workflow: declare expected post-conditions *before* implementing, write them
as checks, run them after, and abstain rather than guess. Carries the four discipline rules.

**4. Self-check.**
The Notepad round-trip already run by hand on 2026-07-20, committed as the first real check —
write text, read it back independently, assert a toggle's state, handle the modal save dialog.

## Success criteria

1. An agent completes a change→verify cycle against a running app with zero Cameron input.
2. Verification correctly **fails** on a deliberately broken change (no false pass).
3. Verification correctly **abstains** on a deliberately unverifiable assertion, and the abstention
   is not reported as success. *Test this explicitly — it is the property the whole tool rests on.*

## Open questions

- What's the first *real* target app (determines whether Touchpoint's Electron/CDP path matters
  early, or whether native UIA is enough)? Notepad proves the mechanism but nothing else.
- How much of "navigate to the state under test" can be a plain pytest fixture before it needs
  real orchestration?

## Related

- [[Repos/Cyclaudes/planning/PLAN_MAIN|PLAN_MAIN]]
- [[Repos/Cyclaudes/planning/TODO|TODO]]
- `related-work/accessibility-tree-agent-tooling.md` — prior-art sweep + smoke-test findings
