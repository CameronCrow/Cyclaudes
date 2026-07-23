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
      (originally `tests/test_notepad_live.py`; migrated to `tests/test_mspaint_live.py`
      (issue #20) once current Win11's single-process *tabbed* Notepad proved
      unownable by PID-scoped ownership (#12) — ran green on mspaint: tool-selection
      round-trip, opaque states (the live `checked,pressed` vocabulary), right-window-
      among-several, modal-on-close asserted structurally, clean dismissal)
- [x] Success criterion 2: deliberately broken change → check fails (no false pass)
      (`tests/test_success_criterion_2.py`: 5 broken cases incl. the OK-but-
      silently-failed close, each → pytest fail/exit 1, never abstention)
- [x] Success criterion 3: deliberately unverifiable assertion → abstains, and is **not** reported
      as success (`tests/test_success_criterion_3.py`: empty-tree/TCC, absent
      element, uninterpretable state → abstain/exit 12, all-abstain ≠ verified)

## Phase 2 — Driving the app → Phase 1

- [x] PID-scoped window ownership — refuse to enumerate or act on windows we did not launch
      *(highest-value task in the phase; fixes the smoke-test near-miss on Cameron's real files)*
      (`ui.py`: `own`/`disown`/`owning` registry + `owned_window`/`owned_windows` that only
      resolve/enumerate owned PIDs and **raise** `UnownedWindow`/`NoOwnedWindows` rather than
      guess; owned `WindowHandle` re-checks ownership on every read/action; also folded in #11 —
      `close()` liveness now polls a scoped per-window read, not a full `windows()` walk.
      `tests/test_ui.py::TestOwnership`/`TestOwnedLiveness`)
- [x] `app_session` fixture — launch, wait-for-ready, yield owned handle
      (`pytest_ui.py`: `@pytest.mark.app_session(cmd, ...)` launches the target,
      `ui.owning(pid)` claims it for the check, an inline PID-scoped wait resolves
      its first window via `ui.owned_window`, and the owned handle is yielded.
      `tests/test_app_session.py`; a `live` mspaint test proves launch→own→attach
      end to end. Finding: current Win11 `notepad.exe` is single-process tabbed,
      so PID ownership can't launch-and-own it — the fixture fails *safe* there.)
- [x] Teardown that survives blocking modals (dismiss non-destructively; force-kill last resort)
      and runs even when the check fails → `app_session`
      (teardown builds on `WindowHandle.close()` raising `ActionNotVerified` when a
      modal blocks it, dismisses non-destructively — *Don't Save*, never *Save* —
      retries, then force-kills by PID as last resort; runs from the fixture
      finalizer so it fires on pass/fail/error. Proven by fake-driven unit tests
      + pytester lifecycle tests.)
- [x] Scratch workspace/profile isolation — runs cannot mutate real user data
      (`app_session` (`pytest_ui.py`): every session gets its own
      `tempfile.mkdtemp()` directory as `cwd=`, never Cameron's real working
      directory; `scratch_arg=` on the marker lets an app-specific
      profile/data-dir flag point at the same directory for apps that need
      more than `cwd=`. The directory is removed in the fixture's outermost
      finalizer — after the process is confirmed dead — on pass, fail, *and*
      error, folded into #13's existing teardown chain so the force-kill
      guarantee still holds. `tests/test_app_session.py::TestScratchCommand`/
      `TestScratchWorkspaceIsolation` assert path containment (cwd == the
      handed-out `.scratch_dir`, a write during the check resolves *inside*
      it) rather than eyeballing, and prove removal on all three outcomes.)
- [x] Precondition helpers: `wait_until_ready`, `assert_owned`, `reset_to_known_state`
      (`ui.py`: `assert_owned` — hard `UnownedWindow` guard built on `is_owned`, never an
      abstention; `wait_until_ready` — blocks until the owned tree is non-empty else abstains
      `EmptyTree`/`WindowGone`, re-checks ownership; `reset_to_known_state` — minimal
      run-reset-then-`wait_until_ready` convention. `tests/test_ui.py::TestAssertOwned`/
      `TestWaitUntilReady`/`TestResetToKnownState`)
- [x] Success criterion: full suite runs alongside Cameron's open apps, provably touching none
      (assert on PID ownership, not absence of visible damage)
      (`tests/test_acceptance_phase2.py` — the cohesive Phase-2 acceptance proof of all four
      criteria. Fake-driven (default `pytest`, green with no desktop): a fake desktop holds our
      owned window plus simulated *Cameron's real apps* (open log Notepad + Logix Designer w/
      unsaved changes); `TestUnownedWindowsAreUntouchable` proves criteria 1 & 4 structurally,
      and `test_shipped_suite_*` runs the **shipped** `app_session` fixture as a real
      multi-check pytest suite via `pytester` — one check abandons a modal — auditing
      touched-none + no-residue from outside the run (criteria 1/2/3, structural not eyeballed).
      A `live` mspaint test proves criteria 1 & 4 against a real desktop; ran green here, no
      stray process/scratch left. Criteria 2/3 are teardown properties proven deterministically
      by the fake suite rather than risked live.)

## Phase 3 — The autonomous trigger → Phase 2

- [x] Confirm what trigger points Claude Code plugins actually support *(spike; do first — it
      constrains everything else in this phase)*
      (Confirmed against the plugins reference: `PostToolUse` + `Stop` are valid hook events; a
      plugin ships them via `hooks/hooks.json` auto-discovered at the plugin root, with
      `${CLAUDE_PLUGIN_ROOT}`-relative `command`s; `skills/` and inline `mcpServers` compose in the
      same plugin. The shipped `flag_ui_change.py`/`stop_gate.py` scripts were smoke-tested via
      stdin exactly as Claude Code invokes them — flag writes pending-ui, non-UI is skipped, Stop
      blocks with no result and allows with a covering pass.)
- [x] Plugin packaging
      (`.claude-plugin/plugin.json` completed: metadata + explicit `hooks`/`skills` + inline
      touchpoint `mcpServers`; `.claude-plugin/marketplace.json` describes the full plugin;
      `hooks/hooks.json` wires PostToolUse+Stop with `${CLAUDE_PLUGIN_ROOT}`; README rewritten as a
      real two-runtime install/usage guide (plugin + `pip install` engine). Hooks are stdlib-only,
      so the trigger fires from the plugin alone; the engine runs the checks.)
- [ ] Criteria capture at implement-time (post-conditions written before the change)
      (Embodied as Step 1 of the `verify-ui` skill — declare post-conditions before implementing —
      but not yet an *enforced* mechanism; left unchecked until it's more than a workflow convention.)
- [x] Trigger + cheap relevance test (don't verify non-UI changes)
      (`hooks/flag_ui_change.py` + `hooks/hooks.json`: `PostToolUse` hook, matcher
      `Edit|Write`, matches `tool_input.file_path` against a per-repo UI-glob set
      (default `ui/**`, `**/*.tsx`, `**/*.jsx`, `**/*.xaml`, `**/*.css`, `frontend/**`;
      overridable via `.cyclaudes/ui-globs.txt`) and appends the de-duplicated
      repo-relative path to `.cyclaudes/pending-ui/<session_id>.json` per the frozen
      schema (`planning/PHASE_3.md`) the Stop hook (issue #32) reads. No-op on a
      non-UI path; never blocks the tool call. `tests/test_flag_ui_change.py`)
- [x] Loop integration: pass → continue; fail → actionable diff + self-correct; abstain →
      escalate with specifics
- [x] Bounded retry — cap correct→verify cycles, escalate on exhaustion
- [x] Success criterion: a full issue resolution completes with zero Cameron input
      (`tests/test_acceptance_phase3.py` — the cohesive Phase-3 acceptance proof, driving the
      three real cores wired as Claude Code runs them: PostToolUse `flag_ui_change.flag` →
      `verify_result.write_result` (the `cyclaudes verify` writer) → Stop `stop_gate.decide`.
      Proves one full unattended cycle (edit → flag → block → pass → allow), the self-correct
      loop (fail → block with the expected-vs-actual diff → fix → re-verify pass → allow), and
      the three guards: a **non-UI** edit never flags and the gate never blocks (criterion 4);
      an **abstain** allows-and-escalates and provably never consumes the block budget — 12
      re-entries stay `block_count == 0`, tallied once, so it can't thrash into the 8-block cap
      and false-pass (criterion 2, the load-bearing rule); and a **fail** blocks with the diff
      then the bounded retry caps and escalates on exhaustion. Deterministic/fake-driven, green
      under default `python -m pytest`; live LLT dogfood is the field confirmation to run on top.)

## Phase 4 — Vision fallback → Phase 3

- [x] Region-scoped capture (element bounding box, not whole desktop)
      (`src/cyclaudes/vision.py`: `capture(handle, query=None, padding=…)` over
      `touchpoint.screenshot(element=/window_id=)` → `PIL.Image`; owned-only via
      the handle's fresh re-resolve, abstains (`CaptureUnavailable`, wired into
      the abstention seam) when pixels can't be had — zero-area/no-backend —
      rather than false-passing. `tests/test_vision.py`)
- [x] Structural-gap assertions: `assert_not_occluded`, `assert_rendered`,
      `assert_within_viewport` (all in `vision.py`, each deterministic — no
      model): `assert_rendered` (per-channel extrema span → blank/unpainted),
      `assert_within_viewport` (element rect ⊄ window rect → clipped/off-screen,
      pure geometry, no capture), `assert_not_occluded` (centre hit-test via
      `touchpoint.element_at` → something on top). Real defect ⇒ fail;
      unmeasurable ⇒ abstain (`GeometryUnavailable`).
- [x] Baseline capture + deterministic diff, with an explicit re-baseline step
      (`assert_matches_baseline` in `vision.py`: capture vs stored PNG, numpy-free
      max-channel diff → changed-pixel fraction; size change or over-tolerance ⇒
      fail. Re-baseline is opt-in via `CYCLAUDES_REBASELINE`; a first run or a
      re-baseline **abstains** (`BaselineUnavailable`) — a freshly written
      baseline never counts as a pass. `tests/test_vision.py`)
- [x] Routing rule: when a check escalates from structural to vision
      (`assert_visible` composes the rule: cheapest structural gate first
      (`assert_exists`) → geometry (`assert_within_viewport`) → one hit-test
      (`assert_not_occluded`) → capture (`assert_rendered`), short-circuiting on
      the first failure/abstention so a missing element never pays for a
      screenshot. Documented in `vision.py`'s module docstring + verify-ui skill.)
- [x] Success criterion: catches a defect structural passed; does not pass a broken layout
      (`tests/test_acceptance_phase4.py`: for blank/occluded/clipped, structural
      `assert_exists`/`assert_state` pass on the same tree while the vision
      assertion fails; a good layout passes `assert_visible`; a capture that
      can't be taken abstains — never passes. Deterministic, fake-driven, same
      pattern as the Phase-2 acceptance proof; live LLT dogfood is the field
      confirmation to run on top.)

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
