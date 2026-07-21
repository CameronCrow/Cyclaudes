---
type: reference
tags: [repo/Cyclaudes]
up: "[[Cyclaudes]]"
---
# TODO

Tasks are sized to become GitHub issues. `→` marks a hard dependency.
Phases are **sequential**; parallelism lives *within* a phase, not across phases.

## Phase 0 — Scoping (done)

- [x] Answer the four open scoping questions (delivery shape, multi-modal scope, LLT
      relationship, verification-loop trigger)
- [x] Prior-art sweep — existing accessibility-tree agent tooling + cross-platform options
- [x] Smoke-test Touchpoint against a real app (Notepad, Windows 11)
- [x] Record the core use case: remove Cameron as the blocking manual verifier
- [x] Write PHASE_1
- [x] Write PHASE_2 through PHASE_5 (full roadmap)

## Phase 1 — The verification contract

Tightly coupled; best done by **one agent**, not fanned out.

- [x] `src/cyclaudes/ui.py` — discipline layer over touchpoint: name-only API (no raw IDs
      exposed), actions re-snapshot instead of trusting their return, explicit window resolution
      that raises on ambiguity, failure messages that print actual states
- [x] `CannotVerify` + `pytest_runtest_makereport` hook giving abstention its own outcome,
      visually distinct from both pass and fail → *depends on nothing; can start immediately*
- [x] `conftest.py` fixtures exposing the discipline layer to checks → `ui.py`
      (shipped as the `window` fixture + `@pytest.mark.window` in `pytest_ui.py`;
      also wired the `EmptyTree`/`WindowGone` → abstention seam via an
      `abstain` registry — closes the "nothing connects them" gap)
- [x] `verify-ui` skill — declare post-conditions before implementing; abstain rather than guess
      (`skills/verify-ui/SKILL.md`)
- [x] Port the Notepad round-trip into the first committed check → `ui.py`, fixtures
      (`tests/test_notepad_live.py`, marked `live`; ran green 3x — round-trip,
      opaque states, right-window-among-several, modal-on-close asserted
      structurally, clean dismissal)
- [ ] Success criterion 2: deliberately broken change → check fails (no false pass)
- [ ] Success criterion 3: deliberately unverifiable assertion → abstains, and is **not** reported
      as success

## Phase 2 — Driving the app → Phase 1

- [ ] PID-scoped window ownership — refuse to enumerate or act on windows we did not launch
      *(highest-value task in the phase; fixes the smoke-test near-miss on Cameron's real files)*
- [ ] `app_session` fixture — launch, wait-for-ready, yield owned handle
- [ ] Teardown that survives blocking modals (dismiss non-destructively; force-kill last resort)
      and runs even when the check fails → `app_session`
- [ ] Scratch workspace/profile isolation — runs cannot mutate real user data
- [ ] Precondition helpers: `wait_until_ready`, `assert_owned`, `reset_to_known_state`
- [ ] Success criterion: full suite runs alongside Cameron's open apps, provably touching none
      (assert on PID ownership, not absence of visible damage)

## Phase 3 — The autonomous trigger → Phase 2

- [ ] Confirm what trigger points Claude Code plugins actually support *(spike; do first — it
      constrains everything else in this phase)*
- [ ] Plugin packaging
- [ ] Criteria capture at implement-time (post-conditions written before the change)
- [ ] Trigger + cheap relevance test (don't verify non-UI changes)
- [ ] Loop integration: pass → continue; fail → actionable diff + self-correct; abstain →
      escalate with specifics
- [ ] Bounded retry — cap correct→verify cycles, escalate on exhaustion
- [ ] Success criterion: a full issue resolution completes with zero Cameron input

## Phase 4 — Vision fallback → Phase 3

- [ ] Region-scoped capture (element bounding box, not whole desktop)
- [ ] Structural-gap assertions: `assert_not_occluded`, `assert_rendered`,
      `assert_within_viewport`
- [ ] Baseline capture + deterministic diff, with an explicit re-baseline step
      *(expected to carry most of the phase's value — prefer over model judgment)*
- [ ] Routing rule: when a check escalates from structural to vision
- [ ] Success criterion: catches a defect structural passed; does not pass a broken layout

## Phase 5 — Cross-platform (macOS) — **confirmed, ~2026-08-03**

Portability *discipline* is enforced in Phase 1 (see its portability constraint). This phase is
validation on real hardware.

- [ ] TCC/Accessibility permission handling + clear diagnostic on missing grant
- [ ] macOS state-vocabulary mapping
- [ ] PID ownership equivalent on macOS → Phase 2
- [ ] Re-run the Phase 1–2 suite unchanged as the acceptance test

## Related

- [[Repos/Cyclaudes/planning/PLAN_MAIN|PLAN_MAIN]]
- [[Repos/Cyclaudes/planning/PHASE_1|PHASE_1]] · [[Repos/Cyclaudes/planning/PHASE_2|PHASE_2]] ·
  [[Repos/Cyclaudes/planning/PHASE_3|PHASE_3]] · [[Repos/Cyclaudes/planning/PHASE_4|PHASE_4]] ·
  [[Repos/Cyclaudes/planning/PHASE_5|PHASE_5]]
- [[Cyclaudes]]
