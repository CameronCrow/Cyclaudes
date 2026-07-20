---
type: reference
tags: [repo/Cyclaudes]
up: "[[Cyclaudes]]"
---
# Planning

**Status:** Scoped (2026-07-20). The four open questions are answered, prior art is swept, the
closest existing tool is smoke-tested, and [[Repos/Cyclaudes/planning/PHASE_1|PHASE_1]] is written.
Ready to implement.

## Problem

Cameron develops mostly hands-off — the agent works autonomously. But the loop **stalls after
every issue resolution**, because the agent finishes a change and then waits on Cameron to open
the real app and confirm it worked. He is the blocking verifier, and that is the bottleneck.

## Goal

Take Cameron out of the verification path: the agent verifies its own work against the running
application. This means expanding "verification" beyond pytest to be multi-modal — structural UI
state, visual state, and interactive behaviour all become things the agent can assert on its own,
the same way it already asserts on a passing test suite.

The unit of value is **unblocking the autonomous loop** — not "exposing accessibility-tree
primitives to Claude." Primitives are a means. Judge every design choice by whether it lets the
agent close the loop without Cameron.

## Decisions (settled 2026-07-20)

1. **Delivery shape: a Claude Code plugin.** Not a bare MCP server — an MCP server only makes
   tools *available*; nothing makes the agent reach for them at the right moment, so the loop
   would still stall. The plugin's real job is carrying the *trigger*.
2. **Multi-modal scope: structural first, vision as fallback** — for two cases, not one:
   (a) surfaces with no accessibility tree (some web UIs, games), and (b) properties a tree
   *cannot encode* even when present — layout, overlap, clipping, colour, "does this look
   broken." A tree will happily report a button as enabled while it renders behind a modal.
3. **Relationship to LLT: start fresh.** Borrow lessons from `src/llt/importer/driver.py`, but it
   is app-specific and won't generalise. Do not disturb LLT before its ~Jul 24 deadline.
4. **Trigger: after implementing**, when the result requires user interaction or is visual — a
   targeted verification step, not a continuous side channel.

## Build vs reuse

**Reuse [Touchpoint](https://github.com/Touchpoint-Labs/touchpoint) as the driver layer; build the
verification loop on top.** Smoke-tested against Notepad on Windows 11 (2026-07-20): the
act→structurally-verify round-trip works with zero setup, and modal dialogs, element states, and
text values are all readable without a vision model.

The gap Cyclaudes fills is *not* the driver — it is the disciplined wrapper: re-assert after every
action, never cache element IDs across mutations, resolve windows explicitly. Four concrete
footguns justify that wrapper; see `related-work/accessibility-tree-agent-tooling.md` and
[[cyclaudes-touchpoint-findings]].

## The hard part

Not tree-reading — **acceptance criteria**. What disappears when Cameron steps out of the loop is
the thing he was silently supplying: *"yeah, that looks right."* So expected post-conditions must
be declared *before/while* implementing, then asserted after. Test-first, but for UI state.

**And the safety property:** a false-positive "verified" is worse than the stall it replaces.
Stalling costs Cameron time; a bogus pass silently ships broken work and permanently burns trust
in the tool — after which he'd go back to checking manually anyway and we'd have built nothing.
Honest abstention matters more than coverage.

## Roadmap

Sequential — each phase depends on the one before. Parallelism lives *within* a phase.

| Phase | What | Why it's here |
|---|---|---|
| 1 | **The verification contract** | The hard part: acceptance criteria + honest abstention. UI checks are plain pytest tests. |
| 2 | **Driving the app** | Lifecycle + PID-scoped isolation, so checks are self-sufficient and can't touch Cameron's real session. |
| 3 | **The autonomous trigger** | The phase that actually removes Cameron. Last of the core three by design. |
| 4 | **Vision fallback** | Only what the tree cannot encode — occlusion, clipping, blank renders. |
| 5 | **Cross-platform (macOS)** | Speculative. Confirm a real need before building. |

The ordering is deliberate: **the trigger comes last.** A trigger that fires unreliable
verification is worse than no trigger — it converts a visible stall into an invisible false pass.
Phases 1–2 exist to earn the right to fire automatically.

## Current State

Scoped and fully planned through Phase 5; no code yet. Touchpoint installed (`touchpoint-py`
0.3.0) and registered as a project-local MCP server in no-vision mode. Next: implement Phase 1.

## Related

- [[Repos/Cyclaudes/planning/PHASE_1|PHASE_1]] — the verification contract
- [[Repos/Cyclaudes/planning/PHASE_2|PHASE_2]] — driving the app
- [[Repos/Cyclaudes/planning/PHASE_3|PHASE_3]] — the autonomous trigger
- [[Repos/Cyclaudes/planning/PHASE_4|PHASE_4]] — vision fallback
- [[Repos/Cyclaudes/planning/PHASE_5|PHASE_5]] — cross-platform (speculative)
- [[Repos/Cyclaudes/planning/TODO|TODO]]
- `related-work/accessibility-tree-agent-tooling.md` — prior-art sweep + smoke-test findings
- `Ladder-Logic-Translator-LLT` — `src/llt/importer/driver.py` — the seed UIA implementation
- `workforce` repo Projects registry — "Multi-modal Claude Verification" entry points here
- [[Cyclaudes]]
