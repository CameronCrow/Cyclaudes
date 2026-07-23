---
type: reference
tags: [repo/Cyclaudes]
up: "[[Repos/Cyclaudes/planning/PLAN_MAIN|PLAN_MAIN]]"
---
# Phase 4 - Vision fallback

**Goal:** assert on what the accessibility tree **cannot encode**.

Two cases, per decision 2 in [[Repos/Cyclaudes/planning/PLAN_MAIN|PLAN_MAIN]]:
- Surfaces with no usable tree (some web UIs, canvas apps, games).
- Properties a tree structurally cannot express *even when it exists* — layout, overlap, clipping,
  colour, blank/unrendered regions. **A tree will report a button as present and enabled while it
  renders behind a modal or off-screen.**

## Key decision: narrow pre-declared questions, never "does this look right"

Vision is where false passes creep back in. A vision model will agree with almost anything it is
shown, especially when the prompt implies the expected answer. Left open-ended, it re-introduces
exactly the failure mode Phases 1–3 were built to prevent — but harder to detect, because the
output is confident prose.

Rules:
- Vision answers **specific, pre-declared** questions ("is this element occluded?"), never
  open-ended judgment ("is this correct?").
- **Default to abstain** on uncertainty. Vision assertions should abstain more readily than
  structural ones, not less.
- **Prefer deterministic image comparison over model judgment** wherever a baseline exists.
  A pixel/perceptual diff against a known-good baseline is far more trustworthy than asking a
  model, and it fails loudly instead of agreeably.

## Deliverables

**1. Region-scoped capture.** Touchpoint's vision mode provides `screenshot()`, croppable to an
app window; extend to element bounding boxes so assertions target a region, not the whole desktop.

**2. Narrow structural-gap assertions.** The specific things the tree gets wrong:
`assert_not_occluded`, `assert_rendered` (not blank/white), `assert_within_viewport`. These are the
high-value cases — each corresponds to a real defect class structural checks pass silently.

**3. Baseline regression compare.** Capture-and-diff against an approved baseline, with an explicit
re-baseline step. The most reliable variant; expect it to carry most of the phase's value.

**4. Routing rule.** When does a check escalate from structural to vision? Must be explicit and
cheap — vision is slower and costlier, and defaulting to it would undo Phase 1's whole premise.

## Success criteria

1. Catches a real defect that structural verification passed — e.g. an element present and enabled
   in the tree but rendered behind a modal or clipped out of the viewport.
2. Does **not** pass a visibly broken layout.
3. Abstains rather than guessing when the capture is ambiguous.
4. Structural-only checks are not slowed down — vision stays opt-in per assertion.

## Status — BUILT (2026-07-23)

Shipped in `src/cyclaudes/vision.py` (tests: `tests/test_vision.py`, acceptance:
`tests/test_acceptance_phase4.py`). All four deliverables, every assertion deterministic (no model —
model judgment stays deferred per the key decision):

- **Region-scoped capture** — `capture(handle, query=None, padding=)` over `touchpoint.screenshot`;
  owned-only via the handle's fresh re-resolve; `CaptureUnavailable` (abstention) when pixels can't
  be had (no backend, zero-area region), never a placeholder image.
- **Structural-gap assertions** — `assert_rendered` (per-channel extrema span → blank/unpainted),
  `assert_within_viewport` (element rect ⊄ window rect → clipped/off-screen, pure geometry),
  `assert_not_occluded` (centre hit-test via `touchpoint.element_at` → something on top).
- **Baseline diff** — `assert_matches_baseline(name)`: capture vs stored PNG, numpy-free max-channel
  diff → changed-pixel fraction; size-change or over-tolerance fails. Explicit opt-in re-baseline via
  `CYCLAUDES_REBASELINE`; a first run or re-baseline **abstains** (`BaselineUnavailable`) so a freshly
  written baseline never passes against itself. This carries most of the phase's value, as predicted.
- **Routing rule** — `assert_visible` runs the cheap structural gate first and escalates to each
  costlier vision check only on success (`assert_exists` → viewport → occlusion → rendered),
  short-circuiting so a missing element never pays for a screenshot. Vision stays opt-in per
  assertion (criterion 4); a check reaches for a vision assertion only for a property the tree
  structurally can't encode.

Abstention discipline holds throughout: `CaptureUnavailable` / `GeometryUnavailable` /
`BaselineUnavailable` all subclass `VisionAbstention` and are wired into the abstention seam, so
"couldn't see / couldn't measure / no baseline yet" surfaces as cannot-verify — never a false pass
(criterion 3). The open questions (deterministic-vs-model, baseline churn, settle-before-capture) are
answered conservatively: deterministic only, re-baseline explicit, and capture reads the current tree
fresh each call. A live LLT dogfood is the remaining field confirmation on top of the deterministic
acceptance suite.

## Open questions

- How much to lean on deterministic diff vs model judgment. Current lean: deterministic wherever a
  baseline is possible; model judgment only for genuinely novel states.
- Baseline storage and churn — UI baselines rot fast and noisy diffs train the agent to ignore them.
- Whether flaky rendering timing needs a settle-and-retry before capture.

## Related

- [[Repos/Cyclaudes/planning/PHASE_1|PHASE_1]] — structural remains the default path
- [[Repos/Cyclaudes/planning/PLAN_MAIN|PLAN_MAIN]]
