---
type: reference
tags: [repo/Cyclaudes]
up: "[[Cyclaudes]]"
---
# TODO

## Phase 0 — Scoping (done)

- [x] Answer the four open scoping questions (delivery shape, multi-modal scope, LLT
      relationship, verification-loop trigger)
- [x] Prior-art sweep — existing accessibility-tree agent tooling + cross-platform options
- [x] Smoke-test Touchpoint against a real app (Notepad, Windows 11)
- [x] Record the core use case: remove Cameron as the blocking manual verifier
- [x] Write PHASE_1

## Phase 1 — The verification contract

- [ ] `src/cyclaudes/ui.py` — discipline layer over touchpoint (name-only API, re-asserting
      actions, explicit window resolution, actual-state failure messages)
- [ ] `CannotVerify` exception + `pytest_runtest_makereport` hook giving abstention its own
      outcome, distinct from pass and fail
- [ ] `conftest.py` fixtures exposing the above to checks
- [ ] `verify-ui` skill — declare post-conditions before implementing; abstain rather than guess
- [ ] Port the Notepad round-trip into the first committed check
- [ ] Prove success criterion 2: deliberately broken change → check fails (no false pass)
- [ ] Prove success criterion 3: deliberately unverifiable assertion → abstains, not reported
      as success

## Later (not Phase 1)

- [ ] Auto-trigger the verification step (hook/skill firing without being asked) — Phase 2
- [ ] App lifecycle: launch + navigate to the state under test — Phase 2
- [ ] Vision fallback for what the tree cannot encode (layout, overlap, clipping, colour) — Phase 3
- [ ] macOS / AX backend

## Related

- [[Repos/Cyclaudes/planning/PLAN_MAIN|PLAN_MAIN]]
- [[Repos/Cyclaudes/planning/PHASE_1|PHASE_1]]
- [[Cyclaudes]]
