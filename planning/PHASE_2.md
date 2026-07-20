---
type: reference
tags: [repo/Cyclaudes]
up: "[[Repos/Cyclaudes/planning/PLAN_MAIN|PLAN_MAIN]]"
---
# Phase 2 - Driving the app

**Goal:** get the application into the state under test — safely — without touching Cameron's real
working session.

Phase 1 can only assert on whatever happens to be on screen. That is not enough to run unattended:
if the app isn't open at the right screen, every check abstains. This phase makes checks
self-sufficient.

## Key decision: framework owns lifecycle, checks own navigation

The framework provides **launch → attach → isolate → teardown**. Getting from "app is open" to
"the specific screen under test" is ordinary per-check pytest fixture code written by the agent.

Do **not** build a navigation DSL. Navigation is inherently app-specific; a generic abstraction
over it would be speculative and would need escaping constantly. Plain fixtures compose better and
the agent already knows how to write them.

## The safety requirement

During the Notepad smoke test, `wait_for_window("Notepad")` substring-matched and auto-activated
**Cameron's real open log file** instead of the test window. Had the next call been `type_text`,
it would have typed into his work. Logix Designer was also open with unsaved changes.

An unattended verification run on a live desktop is the normal case, not the exception. So:
**never act on a window we did not launch.** Ambiguity must raise, never resolve to a guess.

## Deliverables

**1. PID-scoped window ownership.**
Touchpoint resolves windows by title/app, which is why the smoke test grabbed the wrong one. The
discipline layer tracks the PID of processes *it* launched and refuses to return, act on, or even
enumerate windows outside that set. This is the single most important deliverable in the phase.

**2. `app_session` fixture — launch / attach / teardown.**
- Launch the target, wait for its first window, yield an owned handle.
- Teardown must survive **blocking modals**: `close_window()` returned `OK` while a save prompt
  silently blocked it (see [[cyclaudes-touchpoint-findings]]). Teardown detects the modal,
  dismisses it non-destructively, and escalates to force-kill only as a last resort.
- Teardown runs even when the check fails or errors, so a wedged run can't poison later ones.

**3. Scratch workspace isolation.**
Runs use a throwaway profile / temp working directory, never Cameron's real files or app config.
A verification run must be incapable of mutating real user data.

**4. Precondition helpers.**
Small, boring building blocks for fixtures: `wait_until_ready`, `assert_owned`, and a
`reset_to_known_state` convention so checks don't inherit each other's leftovers.

## Success criteria

1. The check suite runs to completion **while Cameron's own apps are open**, and provably touches
   none of them — assert on PID ownership, not on absence of visible damage.
2. A deliberately abandoned modal in one check does not wedge the suite; teardown recovers.
3. A run leaves no residue: no stray processes, no modified user files, no changed app config.
4. Attempting to act on an unowned window **raises** rather than proceeding.

## Open questions

- Apps that refuse a second instance (single-instance enforcement) — attach-to-existing may be
  unavoidable, which weakens PID ownership. Needs a documented, explicitly-opted-into escape hatch.
- Apps requiring login/auth to reach the state under test.
- Whether long-lived app startup makes per-check launch too slow, forcing a session-scoped fixture
  (and with it, cross-check state bleed).

## Related

- [[Repos/Cyclaudes/planning/PHASE_1|PHASE_1]] — depends on its discipline layer
- [[Repos/Cyclaudes/planning/PHASE_3|PHASE_3]] — the trigger needs this to be reliable first
- [[Repos/Cyclaudes/planning/PLAN_MAIN|PLAN_MAIN]]
