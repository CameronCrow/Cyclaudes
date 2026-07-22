---
type: reference
tags: [repo/Cyclaudes]
up: "[[Repos/Cyclaudes/planning/PLAN_MAIN|PLAN_MAIN]]"
---
# Phase 3 - The autonomous trigger

**Goal:** verification fires **without being asked**. This is the phase that actually removes
Cameron from the loop — everything before it is machinery.

Deliberately sequenced last of the core three. A trigger that fires *unreliable* verification is
worse than no trigger: it converts a visible stall into an invisible false pass.

## Deliverables

**1. Plugin packaging.**
The delivery shape decided in [[cyclaudes-scope-decisions]]. The plugin exists to carry the
trigger — that is its whole justification over a bare MCP server.

**2. Criteria capture at implement-time.**
The hard problem from [[cyclaudes-core-use-case]]. Before/while implementing a UI-affecting change,
the agent writes down expected post-conditions as checks. Captured *before* the change, so they
describe intent rather than rationalising whatever the code ended up doing.

**3. The trigger itself.**
A hook/skill firing after a change lands, when the result is visual or interactive. Must include a
cheap **relevance test** — most changes don't touch the UI, and verifying every one would be slow
enough that Cameron disables the tool.

**4. Loop integration — the three outcomes.**
- **Pass** → continue. No interruption.
- **Fail** → the agent gets an actionable diff (expected vs actual tree state) and self-corrects,
  then re-verifies.
- **Abstain** → escalate to Cameron *with specifics*: what it tried, why it couldn't tell. An
  abstention should read as a useful question, not a shrug.

**5. Bounded retry.**
Cap the correct→verify cycles. On exhaustion, escalate. An agent thrashing invisibly against a
check it cannot satisfy is a worse failure than the original stall — it burns tokens and time
while looking like progress.

## Key risk

This phase is where a false pass becomes *invisible*. In Phases 1–2 a human is reading the output;
here nobody is. The abstention path must stay loud and frequent. If early runs show abstentions
being quietly swallowed or rationalised into passes, stop and fix that before proceeding.

## Success criteria

1. A full issue resolution — implement, verify, self-correct, re-verify — completes with **zero**
   Cameron input.
2. An unverifiable change escalates **promptly**, rather than looping or guessing.
3. A change that breaks the UI is caught by the trigger, not by Cameron noticing later.
4. Non-UI changes are not slowed down measurably.

## Implementation design (scoped 2026-07-22)

Trigger points were confirmed against the Claude Code hooks contract (a `claude-code-guide`
research pass): the deterministic shape this phase needs **is** supported.

### Mechanism

- **`PostToolUse` hook** (matcher `Edit|Write`) receives `tool_input.file_path`. If the path
  matches this repo's UI globs, it records the file in a session-scoped state file. Cheap,
  deterministic, and free on non-UI changes — this is the relevance test (deliverable 3).
- **`Stop` hook** reads that state on every turn-end. If UI files were touched and are not yet
  covered by a passing verification, it returns `{"decision":"block","reason":…}` — Claude gets
  the reason and keeps working (deliverable 4). Once verification has run with a pass-or-abstain
  outcome covering the touched files, the gate is satisfied and the agent may stop.

Both ship in the plugin at `hooks/hooks.json` (`${CLAUDE_PLUGIN_ROOT}`-relative scripts).

### Hook-contract facts that constrain the design (do not violate)

- **`Stop` fires on every turn end, not only at task completion**, and there is an **8-consecutive-
  block cap** after which Claude Code overrides the hook and lets the agent stop. The gate must
  therefore be idempotent and cheap to satisfy, and must read `stop_hook_active` to detect
  re-entry — never nag a turn with nothing to verify, never burn the block budget.
- **`Stop`/`PostToolUse` cannot inject `additionalContext`** (only `UserPromptSubmit` can). The
  Stop hook's only channel to the model is the block `reason`; the fail-diff and the abstain
  escalation both travel through that one field.
- Hooks communicate via stdin JSON / stdout JSON / exit codes only; they cannot call a `/skill`
  directly (they can run a shell command / `python -m pytest`).

### FROZEN INTERFACE — the contract issues build against in parallel

State lives under `<project>/.cyclaudes/` (git-ignored), keyed by `session_id`. **Do not change
these shapes without updating this section first** — A and B are built in parallel against them.

**`pending-ui/<session_id>.json`** — written by the PostToolUse hook, read by the Stop hook:

```json
{ "session_id": "…", "ui_touched": ["relpath/one.tsx", "relpath/two.xaml"] }
```

`ui_touched` is the de-duplicated set of UI-glob-matching files edited this session.

**`verify-result/<session_id>.json`** — written when UI verification runs, read by the Stop hook:

```json
{ "session_id": "…", "outcome": "pass|fail|abstain",
  "covered": ["relpath/one.tsx"], "detail": "expected-vs-actual, or the abstain reason",
  "at": "<iso8601>" }
```

**Stop-gate decision (the load-bearing rule):**

- UI touched, no matching `verify-result` yet → **block**; reason names the files to verify.
- `outcome == "fail"` → **block**; reason = the `detail` expected-vs-actual, so the agent self-corrects.
- `outcome == "pass"` (covering the touched set) → **allow**.
- `outcome == "abstain"` → **allow, AND surface the abstain `detail` to the user**. Abstain is a
  legitimate stopping point that escalates, **not** a reason to keep blocking. Getting this wrong
  makes an unverifiable change thrash into the 8-block cap and then false-pass anyway — this is the
  single most important correctness rule in the phase.

The gate is satisfied only when `covered` covers `ui_touched`; a later edit to a new UI file
re-opens it.

### Decomposition (issues)

- **A — relevance detector + session state (PostToolUse).** The `Edit|Write` hook, the per-repo
  UI-glob config, writing `pending-ui/<session_id>.json` per the frozen schema, and the
  `.cyclaudes/` gitignore. Touches `hooks/` + `hooks.json` + `.gitignore` only.
- **B — Stop-gate + three-outcome routing + bounded retry + instrumentation.** The Stop hook
  (idempotent, `stop_hook_active`-aware, 8-block-cap-safe), the decision rules above, the retry
  cap, pass/fail/abstain audit counters, and the mechanism that writes
  `verify-result/<session_id>.json` when UI checks run (a small addition to the cyclaudes pytest
  plugin, or a thin `cyclaudes verify` wrapper). Reads the frozen schema A writes. Touches
  `hooks/` (Stop script) + `hooks.json` + `src/cyclaudes/` (result-writer).
- **C — end-to-end acceptance (deferred until A + B land).** One full unattended UI
  issue-resolution cycle — implement → trigger fires → verify → self-correct → re-verify — with
  zero human input, dogfooded on a real LLT UI change; plus the guard tests: a non-UI change does
  **not** fire, and an abstain escalates rather than thrashing.

A and B build in parallel against the frozen interface (they collide only on `hooks.json`, a
trivial rebase). C is last by construction.

## Open questions — resolved during scoping (2026-07-22)

- **Hook vs skill vs both → resolved:** hooks for deterministic firing (`PostToolUse` + `Stop`),
  the `verify-ui` skill for the actual verification work the block triggers. Trigger points
  confirmed supported (above).
- **Where captured criteria live → resolved:** durable regression tests in the repo's test tree
  (the `verify-ui` skill already writes them there), so the phase's output compounds into a
  growing UI regression suite rather than evaporating per task.
- **How to decide "UI-affecting" cheaply → resolved:** a file-glob relevance test on the edited
  path in the `PostToolUse` hook (per-repo globs); the model's judgment can be a second gate,
  never the only one.

Still genuinely open (settle during build):
- Exactly who writes `verify-result` — a cyclaudes pytest-plugin addition vs. a `cyclaudes verify`
  CLI wrapper (issue B decides, behind the frozen file contract).
- Default UI-glob set and how a repo overrides it.

## Related

- [[Repos/Cyclaudes/planning/PHASE_2|PHASE_2]] — must be reliable before this is safe
- [[Repos/Cyclaudes/planning/PLAN_MAIN|PLAN_MAIN]]
