---
type: reference
tags: [repo/Cyclaudes]
up: "[[Repos/Cyclaudes/planning/PLAN_MAIN|PLAN_MAIN]]"
---
# Phase 5 - Cross-platform (macOS)

**Status: confirmed and time-boxed.** Cameron moves to a Mac in **~2 weeks** (confirmed
2026-07-20, i.e. around 2026-08-03). This is no longer speculative.

**This changes the plan's shape, not just its priority.** If the tool is Windows-only when he
switches machines, it stops working exactly when he needs it. So portability splits in two:
- **Portability discipline is a Phase 1 constraint** — no hardcoded state vocabulary, role names,
  or ID formats. Applied now, from the first line of code.
- **macOS validation and permissions work is this phase** — running it on real hardware.

Aim to have Phases 1–2 portable-by-construction before the switch, so this phase is a validation
pass rather than a port.

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

- Which Mac / OS version, and is the target app the same one as on Windows?
- Linux/AT-SPI2 is available via the same abstraction but has no stated use case at all — leave it.

## Related

- [[Repos/Cyclaudes/planning/PHASE_2|PHASE_2]] — PID ownership must be re-proven per platform
- [[Repos/Cyclaudes/planning/PLAN_MAIN|PLAN_MAIN]]
