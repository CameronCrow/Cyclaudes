---
type: reference
tags: [repo/Cyclaudes]
up: "[[Cyclaudes]]"
---
# Planning

**Status:** Phases 1 & 2 built, green, and dogfooded on a real project (LLT, 2026-07-22); Phase 3
(autonomous trigger) A+B landed; **Phase 4 (vision fallback) built (2026-07-23).** See Current
State below.

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
| 5 | **Cross-platform (macOS)** | Confirmed — Cameron switches to a Mac ~2026-08-03. |

The ordering is deliberate: **the trigger comes last.** A trigger that fires unreliable
verification is worse than no trigger — it converts a visible stall into an invisible false pass.
Phases 1–2 exist to earn the right to fire automatically.

## Current State (2026-07-22 — Phases 1 & 2 built and green; NOT YET dogfooded)

**Phases 1 and 2 are built, unit/acceptance-tested, and now dogfooded on a real project.** The
first real-project dogfood (2026-07-22, against the LLT Import UI — a pywebview/WebView2 app)
validated Phase 1 end-to-end and surfaced two Phase-2 gaps, both since fixed (see Dogfood results).

Phase 1 (the verification contract) and Phase 2 (driving the app) both merged to `main`; CI runs
the suite on every PR (`.github/workflows/tests.yml`, Windows-only for now). Also packaged as an
installable plugin (`/plugin install cyclaudes@cyclaudes`) with the touchpoint MCP bundled, and a
one-command engine install (`pip install git+https://github.com/CameronCrow/Cyclaudes.git`).

Phase 1 landed:
- `src/cyclaudes/ui.py` — discipline layer over touchpoint (#2, PR #9). Actions return `None`
  unconditionally, name-only API, own window/element matching ladder that raises on ambiguity.
- `src/cyclaudes/abstain.py` + `pytest_plugin.py` — `CannotVerify` with its own pytest outcome
  and exit code 12 (#1, PR #10).
- `src/cyclaudes/pytest_ui.py` — the shipped `window` fixture + `@pytest.mark.window` (#3).
- `skills/verify-ui/SKILL.md` — the workflow doc (#4, PR #8).
- `tests/test_notepad_live.py` — the first real-UI check, ran green 3× (#5).
- `tests/test_success_criterion_2.py` / `_3.py` — no-false-pass (#6) and abstention-is-never-
  success (#7), both driven through the real discipline layer.

Phase 2 landed (issues #12–#16, PRs #17–#22):
- PID-scoped window ownership — never touch/enumerate a window we did not launch; ambiguity raises
  (#12). The single most important deliverable.
- `app_session` fixture with modal-safe teardown + force-kill fallback (#13).
- Scratch workspace isolation — throwaway temp working dir, can't mutate real user files (#15).
- Precondition helpers `assert_owned` / `wait_until_ready` / `reset_to_known_state` (#14).
- Acceptance suite proving the suite runs beside real apps and touches none (#16).

### Dogfood results (2026-07-22 — first real-project contact, LLT Import UI)

Target: `Ladder-Logic-Translator-LLT/ui/app.py`, a **pywebview / WebView2** app (HTML/JS in an
embedded Edge Chromium window, not native Win32) — deliberately the hard case.

- **Phase 1 VALIDATED end-to-end.** A live probe through the unowned `window` fixture asserted real
  post-conditions against the WebView2 DOM (action button present, loaded `TOY.txt` text node read,
  `"No import set yet"` empty-state) — all passed — and a deliberately false claim **FAILED** (not
  passed, not abstained). No-false-pass held on a live app; cyclaudes reads text nodes, not just
  interactive elements. Whole run 4.16s. The fast-verification premise holds on real web UIs.
- **Phase 2 had two gaps, both now FIXED and merged:**
  - **Subtree-aware ownership (#23, PR #26).** `app_session` owned by `Popen(...).pid`, but Windows
    Store `python` is an App-Execution-Alias shim that re-execs the real interpreter as a *child* —
    the window belongs to the child PID, so `app_session` false-refused its own app
    (`AppSessionError`). Fixed: `is_owned` now accepts a PID descending from an owned PID (process
    ancestry via a new `ancestry.py` ctypes seam), strictly ancestry-scoped so unrelated/sibling
    PIDs are still refused. Generalizes to `.cmd`/`.bat`/`npx`/Java/Electron launchers.
  - **Content-aware `wait_until_ready` (#24, PR #25).** WebView2's a11y tree is lazy — the first
    read after launch is empty `landmark` wrappers, so the first assertion would false-abstain.
    Fixed: `wait_until_ready(signal=...)` gates on real DOM content (element name or predicate),
    polled fresh, backward-compatible, still abstains honestly at the deadline.

### Current status — Phase 3 in progress (scoped 2026-07-22)

The full end-to-end `app_session` dogfood on LLT **passed green** — the fixture launched the LLT
UI itself (through the Store-Python re-exec), warmed the lazy tree with `wait_until_ready(signal=…)`,
asserted real post-conditions, and tore down cleanly. The one gap it surfaced — teardown's
force-kill reaching only the launched PID, so a re-exec'd child could orphan on a blocked close —
is fixed (#29). So the core three phases' first two are proven on a real app, and **Phase 3 (the
autonomous trigger) is now underway.**

Phase 3 is scoped in `planning/PHASE_3.md` (Implementation design): a `PostToolUse` hook flags
UI-affecting edits, a `Stop` hook blocks completion until verification has run (pass/abstain
satisfies the gate; fail re-blocks with the diff; abstain escalates rather than thrashing). Trigger
points were confirmed against the Claude Code hooks contract. Decomposed into issues **A** (relevance
detector + session state), **B** (Stop-gate + routing + retry + instrumentation), and **C** (end-to-end
acceptance, deferred until A+B land — now landed: `tests/test_acceptance_phase3.py` proves one full
unattended cycle plus the non-UI, abstain-never-thrashes, and fail/bounded-retry guards); A and B
build in parallel against a frozen state/decision interface. Issue #20 (migrate the stale Phase-1
live check off tabbed Notepad → mspaint) is resolved: `tests/test_notepad_live.py` is replaced by
`tests/test_mspaint_live.py`, unrelated cleanup to the trigger work above.

### Phase 4 BUILT (2026-07-23)

Phases 3-A and 3-B landed (PRs #34/#35), so Phase 4 (vision fallback) followed — and is now **built**
in `src/cyclaudes/vision.py` (see `planning/PHASE_4.md` Status). All four deliverables, every
assertion deterministic (no model — model judgment stays deferred): region-scoped `capture()`,
`assert_rendered` (blank/unpainted), `assert_within_viewport` (clipped/off-screen), `assert_not_occluded`
(hit-test), `assert_matches_baseline` (deterministic PNG diff, explicit opt-in re-baseline), and
`assert_visible` (the structural→vision routing rule: cheap gate first, escalate on success). Capture
/ geometry / baseline that can't be evaluated abstain via `VisionAbstention` subclasses wired into the
abstention seam — never a false pass. Proven by `tests/test_vision.py` + the `tests/test_acceptance_phase4.py`
success-criterion suite (structural passes while vision catches blank/occluded/clipped; a good layout
passes; ambiguous capture abstains). **Live-dogfooded on the real LLT Import UI (WebView2, 2026-07-23)**
— capture, `assert_rendered`, `assert_within_viewport`, and the full `assert_matches_baseline` cycle all
work on real WebView2 pixels; `assert_not_occluded`/`assert_visible` honestly **abstain** there because
`touchpoint.element_at` is coordinate/DPI-unreliable on WebView2 (hardened with a trust guard — never a
false pass/fail; foreign-process occlusion is still caught). See PHASE_4 "Live dogfood". Remaining: the
model-judgment path (deferred by design) and robust web occlusion (#40 — **investigated 2026-07-23,
blocked on an upstream touchpoint DOM z-order primitive; the abstain is validated-correct**). Next core
work is Phase 5 (macOS) and the tracked limitations (#36 enumeration done, #37 React DOM reads —
confirmed working live, #40 occlusion — blocked upstream).

### Known limitations (tracked)

- **React / div-soup apps expose thin accessibility trees (#37).** Structural verification only sees
  what an app puts in its a11y tree; role-sparse React UIs give little to assert on even though they
  render fine. React is one of the most popular stacks, so making the tool robust to it is a priority.
  Lead: touchpoint already carries a CDP seam (`_get_cdp`/`_is_cdp_id`) that reads the real DOM, not
  just the a11y projection — a candidate path for Chromium/Electron/WebView2 targets. Must abstain,
  never false-pass, on a UI it can't actually read.
- **Enumeration cost (#36) — addressed 2026-07-23.** `touchpoint.windows()` is ~8s on a busy desktop
  (~30s with a UI-thread-blocked app like Logix Designer). New `src/cyclaudes/windowing.py` ctypes seam
  removes it from two hot paths: `WindowHandle` liveness now uses `IsWindow`/`GetWindowThreadProcessId`
  on the HWND captured at resolve time (a settle loop on a gone/empty tree no longer re-enumerates every
  poll), and the `app_session` launch-wait gates the expensive resolve behind a pure-`ctypes` `EnumWindows`
  sweep (`ui.any_owned_window_visible`), so it stops enumerating on every poll while an app is starting.
  Both fail open to the old enumeration path when the ctypes probe can't decide. The *general* first-time
  resolution cost remains (no public touchpoint path) — an upstream feature request (`find_window(pid=/hwnd=)`
  / `ElementFromHandle`), tolerable per this plan.

### The two live-UI findings — RESOLVED (2026-07-20)

Both reproduced against live Notepad and fixed; each has a regression test.

1. **Enum bug — CONFIRMED, portability defect, fixed** (`ee216df`). Touchpoint returns roles/states
   as `enum.Enum` members; `ui.py` stringified them with `str()`, yielding `"State.CHECKED"` instead
   of the portable `.value` `"checked"`, so every `assert_state`/unified-role filter silently never
   matched the real driver. The 54 fake-driven tests used plain-string states and never caught it.
   Fixed with a `.value`-aware `_val()` (also correct on macOS, where AX reports bare strings).
2. **~30s snapshots — CONFIRMED, not inherent, fixed** (`b4a833c`). Root cause was `_require_window()`
   calling `_tp.windows()` (a full top-level enumeration, ~8s with 19 windows open, ~30s with a big
   tree like Logix Designer also open) on *every* `_snapshot()`. A scoped `_tp.elements(window_id=…)`
   read is ~50ms, so the hot path now does just that; `windows()` is paid only on the empty-read
   path, to tell `WindowGone` from a denied-a11y `EmptyTree`. **Measured live: 8000ms → 44ms per
   snapshot; a 10-assert check 80s → 0.42s.** The tool's cheap-verification premise holds.

### Known gap — CLOSED (#3)

`ABSTENTION_CONDITIONS = (EmptyTree, WindowGone)` now connects to `CannotVerify` via a small registry
in `abstain.py` (`register_abstention_types`), which `ui.py` populates at import. An empty tree /
vanished window abstains rather than failing; the registry refuses any `AssertionError` subclass, so
a real UI failure can never be reclassified as "could not verify".

### Secondary cost to settle in Phase 2 (issue #11)

Distinct from the fixed per-assertion snapshot: `_tp.windows()` itself is ~8s on a busy desktop, so
window *resolution* (`ui.window`) and the `close()` / `WindowGone` liveness polling still pay it
once per check. Tolerable now (resolution is one-time), but PID-scoped ownership in Phase 2 should
look for a lighter "does this one window still exist" path than a full enumeration.

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
