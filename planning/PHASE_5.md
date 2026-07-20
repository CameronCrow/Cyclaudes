---
type: reference
tags: [repo/Cyclaudes]
up: "[[Repos/Cyclaudes/planning/PLAN_MAIN|PLAN_MAIN]]"
---
# Phase 5 - Cross-platform (macOS)

**Status: speculative — confirm the need before building.**

Cameron develops on Windows 11. macOS support was raised as "if possible," not as a requirement.
If he isn't actually developing on a Mac, this phase is YAGNI and should stay unbuilt; the cost is
not the initial port but the permanent doubling of the platform surface every later phase must
support.

**Do not start this phase without confirming a real macOS use case.**

## Why it's cheap if the need is real

Touchpoint already abstracts Windows UIA / macOS AX / Linux AT-SPI2 behind one API, and Cyclaudes'
own layers (discipline wrapper, fixtures, criteria, trigger) are platform-agnostic by construction.
So this is mostly **validation and platform-specific gotchas**, not a rewrite.

## Deliverables

**1. Permissions.** macOS Accessibility is granted via TCC to the **host process** (Terminal, VS
Code, whatever spawns the run), not to the library. Unattended runs need this pre-granted, and a
clear diagnostic when it isn't — a permissions failure must be an obvious abstention, not a
mysterious empty tree.

**2. State-vocabulary mapping.** Phase 1 already learned that state names are platform-specific
(`checked,pressed` on UIA, not `selected`). AX differs again. The discipline layer's
actual-states-in-failure-message behaviour matters more here, not less.

**3. AX limitations.** The AX tree is untyped relative to UIA's fixed control-type enum, and has no
batch subtree read. Expect slower enumeration and looser role matching; the name-based API should
absorb most of this.

**4. PID ownership on macOS.** Phase 2's core safety property needs a macOS equivalent — verify it
holds, since window/process association differs.

**5. Re-run the suite.** The existing checks are the acceptance test.

## Success criteria

1. The Phase 1–2 check suite passes on macOS with no check-level changes.
2. Missing Accessibility permission produces a clear abstention naming the fix, never a silent pass.
3. PID-scoped ownership holds — an unowned window still raises.

## Open questions

- **Does Cameron actually develop UI work on macOS?** If no, close this phase unbuilt.
- Linux/AT-SPI2 is available via the same abstraction but has no stated use case at all.

## Related

- [[Repos/Cyclaudes/planning/PHASE_2|PHASE_2]] — PID ownership must be re-proven per platform
- [[Repos/Cyclaudes/planning/PLAN_MAIN|PLAN_MAIN]]
