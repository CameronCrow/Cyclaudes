---
name: verify-ui
description: Use this skill when a change affects what a user would see or do in a running application — a window, dialog, control, layout, enabled/disabled state, text a user reads, or anything you would normally hand back to a human with "can you check this looks right?". Triggers on "verify the UI", "check this in the real app", "does this actually work on screen", "write UI acceptance criteria", "verify-ui", and on any UI-affecting change you are about to call done. Declares expected post-conditions BEFORE implementing, asserts them after as plain pytest checks against the live accessibility tree, and abstains honestly (CannotVerify) rather than guessing. Invoke it deliberately — it does not fire on its own yet.
---

# verify-ui

The whole point of Cyclaudes is to remove Cameron as the blocking manual verifier. What
disappears when he steps out of the loop is not tree-reading — it is the thing he was silently
supplying: *"yeah, that looks right."* This skill is how you supply it instead.

Two rules carry the entire skill. Everything else is detail:

1. **Write the post-conditions before you write the code.** Criteria written afterwards
   rationalise whatever the code happened to do.
2. **Abstain rather than guess.** A false "verified" is worse than the stall it replaces.
   Stalling costs Cameron time; a bogus pass silently ships broken work and permanently burns
   trust in the tool — after which he goes back to checking by hand and this was all for nothing.

## Scope

- **Invoked deliberately.** Automatic firing is Phase 3 (`planning/PHASE_3.md`). Right now
  a human or a calling agent asks for this. Don't build or expect a hook.
- **Structural only.** You assert on the accessibility tree. Vision is Phase 4. If the property
  you care about is *visual* — occlusion, clipping, colour, "does this look broken" — the tree
  cannot encode it. That is an abstention today, not a workaround.
- **The framework launches and tears down the app; you write the navigation.** `app_session`
  (Phase 2, shipped) launches the target in an isolated scratch workspace, owns it by PID —
  including any process it re-execs into, so Store-Python / `.cmd` / `npx` / Electron launchers
  work — waits for its first window, yields an owned handle, and tears it down modal-safely even
  when the check fails. Getting from "app is open" to the specific screen under test is ordinary
  per-check fixture code; there is deliberately no navigation DSL. If you genuinely cannot reach
  the state under test, that is an abstention — do not assert against whatever screen you happened
  to land on.

## The workflow

### Step 1 — Declare post-conditions, before touching the code

Do this *first*, while you still only know what the change is *supposed* to do. Write them down
where the user can see them, in plain language, before implementing:

> After this change:
> - The Settings dialog exposes a "Enable telemetry" checkbox.
> - It reads as off on first open.
> - Toggling it and reopening the dialog shows it still on.

Rules for a usable post-condition:

- **Observable in the accessibility tree.** "The button is enabled" is checkable; "the button
  feels responsive" is not. If you cannot name the element and the property you'd read off it,
  you don't have a post-condition — you have a hope.
- **Specific about the element and the expected value.** Not "the field updates" — "the document
  element's text reads exactly `hello`".
- **Falsifiable *by the bug you are fixing*** — not merely falsifiable in principle. If the
  assertion would have read the same before your change as after, it is not evidence of anything,
  however green it goes. Fixing a button that renders behind a modal? "The button is enabled" was
  already true while the bug was live. Ask of every criterion: *what tree state would this have
  had under the broken behaviour?* If the answer is "the same one", you have no check yet.
- **Written before, kept honest after.** If implementing teaches you a criterion was wrong,
  change it *and say out loud that you changed it, and why*. Silently editing criteria to match
  the code you just wrote is the exact failure this skill exists to prevent. A visible amendment
  is fine. An invisible one is fraud.

If a criterion is not checkable structurally, still write it down — and mark it as one you will
have to abstain on. That is useful information for Cameron; a quietly dropped criterion is not.

### Step 2 — Turn them into checks

**A UI check is a plain pytest test.** Not a new DSL, not a new runner, not a new report format.
pytest supplies discovery, fixtures, setup/teardown, assertion reporting, and an exit code you
already know how to read. Write the checks in the repo's test tree so they become durable
regression tests, not throwaway scratch work.

Drive the UI through `cyclaudes.ui` (the discipline layer over touchpoint), not raw touchpoint
calls. Its entire reason to exist is making the four footguns below unrepresentable — reaching
past it to raw touchpoint re-arms every one of them. **Read the module's actual API before
writing checks; do not assume signatures from memory or from any sketch in the planning docs.**

Writing the checks before implementing is normal and expected. They will fail. That is the point
— a check that has never been seen to fail has not been shown to check anything.

#### The API you actually call

A map, not a substitute — read the real signatures and docstrings in `src/cyclaudes/pytest_ui.py`
(the fixtures) and `src/cyclaudes/ui.py` (the driver and helpers). Everything imports from
`cyclaudes`:

- **`app_session` fixture** — `@pytest.mark.app_session(cmd, *, title_contains=, app=, title=, ready_timeout=, ready_poll=, timeout=, poll=, scratch_arg=)`. Launches `cmd` (str or list, as `subprocess.Popen` takes it) in a fresh temp cwd, owns it, waits for its first window, yields an owned `WindowHandle`, and tears it down (graceful modal-safe close, then force-kill, then scratch cleanup) no matter how the check ends. The default for a self-contained check.
- **`window` fixture** — `@pytest.mark.window(app=, title=, title_contains=, pid=)`. Attaches to an *already-open* window; no launch, no teardown. Use only when the app is a given the check does not own.
- **`ui.wait_until_ready(handle, *, signal=, timeout=, poll=)`** — block until the window is genuinely ready, then return the handle. Pass `signal=` — an element name, or a `(handle) -> bool` predicate — to gate on **real content**, not merely a non-empty tree. Required for lazy web UIs (see gotchas).
- **`ui.assert_owned(handle_or_pid)`** — hard-assert ownership before acting; returns the pid.
- **`ui.reset_to_known_state(handle, reset)`** — run your app-specific `reset(handle)` callable, then wait until ready, so one check's leftovers can't bleed into the next.
- **`WindowHandle`** — all reads/asserts re-read the live tree: reads `exists`, `read_text`, `states`, `title`; assertions `assert_text`, `assert_state`, `assert_not_state`, `assert_exists`, `assert_gone`; actions `click`, `set_value`, `close` (each re-verifies from a fresh snapshot — never trust the action's own return value, rule 1). Elements are addressed by **name/role**, never by cached IDs (rule 2).

#### A worked check, end to end

The launch → warm → assert → automatic-teardown flow (this is the real LLT Import UI run, a
pywebview/WebView2 app, that passed green):

```python
import pytest
from cyclaudes import ui

@pytest.mark.app_session(
    ["python", r"C:\...\Ladder-Logic-Translator-LLT\ui\app.py", r"C:\...\TOY.txt"],
    title_contains="LLT Import",   # resolve OUR owned window — never a stray title match (rule 3)
    ready_timeout=40,              # WebView2 cold start is slow
)
def test_import_ui_shows_loaded_source(app_session):
    # WebView2's a11y tree is lazy — warm it until real content is present; don't assert cold:
    ui.wait_until_ready(app_session, signal="Assemble import set", timeout=40)

    # Post-conditions, declared before the code, asserted against a fresh read:
    assert app_session.exists("Assemble import set", role="button")
    assert app_session.exists("TOY.txt")                  # loaded-file name is a text node
    assert not app_session.exists("Import set: 5 files")  # nothing assembled yet — must be false
    # teardown (modal-safe close, force-kill fallback, scratch cleanup) runs automatically.
```

#### Gotchas the live desktop will hit you with

- **Lazy web UIs (WebView2/Chromium, pywebview, Electron).** The accessibility tree is built on
  demand: the first read right after launch is empty `landmark` wrappers with no DOM, so a bare
  assertion **false-abstains**. Always `wait_until_ready(win, signal="<a real element>")` before
  asserting. Once warmed the whole DOM is there; loaded text (a filename, a status) is a `text`
  node that `exists` / `read_text` see.
- **Re-exec'ing launchers are handled for you.** `python` (the Windows Store shim), `.cmd`/`.bat`,
  `npx`, Java, and Electron helpers re-exec the real process as a child; `app_session` owns that
  child by process ancestry, so you launch with the normal command and it still resolves *your*
  window rather than refusing it or grabbing a pre-existing one.
- **Install / run.** `pip install git+https://github.com/CameronCrow/Cyclaudes.git` yields a
  runnable verifier (pytest and touchpoint come with it); a check is a plain pytest test, run with
  `python -m pytest`. Live checks that drive a real desktop are marked and deselected by default.

### Step 3 — Implement.

### Step 4 — Run the checks and report one of exactly three outcomes

| Outcome | Meaning | What you do |
|---|---|---|
| **Pass** | Every declared post-condition asserted true against a re-read of the live tree | Report it, name what was asserted |
| **Fail** | A post-condition was genuinely evaluated and came out false | Report expected vs *actual observed state*, self-correct, re-verify |
| **Abstain** | The check could not be evaluated at all | Report `CannotVerify` with the specific reason — see below |

Never collapse these into two. Abstain is not a soft pass and not a soft fail.

## Abstention: `CannotVerify`

Abstention is a **normal, frequently-taken path**, not an error case and not something to be
embarrassed about. Raise `CannotVerify` (from `cyclaudes`) with a reason whenever a check cannot
genuinely be evaluated. The pytest integration gives it its own outcome, visually distinct from
both pass and fail, so it can never be misread as success.

Abstain when — non-exhaustively:

- The app, window, or state under test could not be reached.
- The accessibility tree came back empty or absent. **An empty tree is never "nothing is
  broken."** On macOS a missing TCC Accessibility grant produces exactly this, and on any
  platform so does an app that never finished starting.
- The element you need to read is not in the tree, and you cannot tell whether that is the bug
  or the harness.
- A modal or unexpected window is blocking the state under test.
- The property is visual, not structural (occlusion, layout, colour) — the tree cannot answer it.
- The window could not be resolved unambiguously (see rule 3).
- You would have to guess at state vocabulary to make the assertion (see rule 4).

**Say why, plainly and specifically.** An abstention should read as a useful question, not a
shrug:

- Good: `CannotVerify("Settings dialog never appeared after clicking 'Preferences'; the tree
  shows only the main window, so I cannot tell whether the checkbox is missing or the dialog
  failed to open.")`
- Useless: `CannotVerify("couldn't check")`

**Things that are never acceptable:**

- Loosening an assertion until it passes.
- Asserting on something adjacent and easier, then reporting the original criterion as verified.
- Dropping a criterion you couldn't check and reporting the rest as "all checks pass".
- Retrying until a flake goes green and calling that a pass.

If you catch yourself reaching for any of those, that is the signal to abstain.

## The four discipline rules

Each one is a live failure observed in the 2026-07-20 Touchpoint smoke test against Notepad
(`related-work/accessibility-tree-agent-tooling.md`). They are not hypothetical.

### 1. Never trust an action's own return value. Re-assert independently.

`close_window()` returned a bare `OK` while a modal save prompt silently blocked the close. The
window stayed open, and no `(new window: …)` flag was emitted. Auto-verify flags are best-effort,
not a guarantee — trusting one is a false positive waiting to happen.

After every action, **re-read the tree and assert on what you read**. The read that proves the
change must be independent of the call that made it. A `set_value` returning `OK` is not evidence
the text is there; reading the text back is.

### 2. Never cache element IDs across a mutation. Re-snapshot.

When the Notepad dialog opened, the document element went `uia22` → `uia52` and every toolbar ID
renumbered wholesale. IDs are **per-snapshot handles, not durable references**.

Any multi-step check re-snapshots after anything that could mutate the tree — clicks, typing,
dialogs opening or closing, navigation. Prefer the discipline layer's name/query-based API, which
does not hand you raw IDs to cache in the first place. And do not parse IDs: no `uia`-prefix
assumptions, no ordering assumptions. They are opaque handles on every platform.

### 3. Resolve windows explicitly. Never let a title substring pick for you.

`wait_for_window(title)` substring-matched and auto-activated a *pre-existing, unrelated* window —
it grabbed one of Cameron's real open files. That was one keystroke away from typing test input
into his actual work.

Enumerate windows and resolve the one you mean, explicitly. If more than one matches, that is an
**abstention, not a coin flip** — raise loudly rather than picking. "It probably meant that one"
is how a verification tool ends up editing a user's real document.

If you discover *after the fact* that you acted on an ambiguous match, the run is **void**, not
salvageable: every observation downstream of it inherits the ambiguity. Re-resolve and start the
check over, and disclose that you did.

### 4. Discover state vocabulary from the tree. Never assume it.

Guessing `selected` for a toggle matched nothing. The real states were `checked,pressed`.

Snapshot first and read the states the element actually reports, then assert against those.
Never hardcode a vocabulary, and never hardcode role names: `checked`/`pressed` are UIA-specific,
macOS AX differs, and Cameron is on a Mac in about two weeks. Treat states as **opaque strings
discovered from the tree**, and always report the element's *actual* states in a failure message
— a failure that says only "expected `selected`" teaches nobody anything, while "expected
`selected`, actual `checked,pressed`" fixes itself.

Note the trap in this rule: a state assertion that matches nothing looks exactly like a state
assertion that is legitimately false. If you did not first confirm the vocabulary exists in the
tree, you do not have a fail — you have an abstention.

## Reporting

Say, in this order:

1. The post-conditions you declared, and **when** you declared them (before implementing —
   or, if amended, what changed and why).
2. The outcome of each: pass / fail / abstain.
3. For every failure, expected vs **actual observed** tree state.
4. For every abstention, the specific reason and what you tried.
5. Any criterion you knew from the start you could not check structurally.

Never report "verified" as a bare word. Report *what* was asserted. "Verified" with nothing
behind it is indistinguishable from a guess, which is the whole problem.

## Ground rules

- **Post-conditions before code.** If you're writing criteria after the diff exists, you are
  doing the thing this skill was written to stop.
- **A false pass is the worst outcome available to you.** Worse than a fail, worse than an
  abstention, worse than stalling. Rank accordingly when tempted.
- **Three outcomes, never two.** Abstention is normal and frequent.
- **Independent re-read, always.** Never let an action vouch for itself.
- **Structural only, for now.** Visual properties abstain; that's Phase 4's job, not a hack's.
- **Don't reach past the discipline layer** to raw touchpoint to make something work. If the
  layer won't let you do it, that is usually the layer working.

## Related

- `planning/PHASE_1.md` — the verification contract this skill implements
- `planning/PLAN_MAIN.md` — "The hard part"
- `planning/PHASE_3.md` — where this fires automatically (not yet)
- `related-work/accessibility-tree-agent-tooling.md` — the smoke test behind the four rules
