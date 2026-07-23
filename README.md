# Cyclaudes

**Structural UI verification for the autonomous loop.** Cyclaudes lets a Claude Code
agent drive a real desktop app through the accessibility tree, assert post-conditions,
and — crucially — **honestly abstain instead of false-passing**. A Stop-gate then blocks
the agent from calling a UI change "done" until it has actually been verified.

The point isn't "expose accessibility-tree primitives to Claude." It's to **take the
human out of the verification path**: the agent verifies its own UI work the same way it
already trusts a passing test suite, instead of stalling on "does this look right?"

## Three outcomes, always

Every check resolves to exactly one of:

- **pass** — post-conditions held.
- **fail** — a post-condition was violated (with the actual observed state).
- **abstain** (`CannotVerify`) — the check could not be evaluated (empty tree, denied
  accessibility, capture unavailable, …).

A **false pass is the worst outcome** — it silently ships broken work and burns trust.
So abstention is a first-class result, never quietly reported as success.

## How it fits together — two runtimes

| Piece | What it is | Install |
|---|---|---|
| **Plugin** | The `verify-ui` skill (how to author UI checks), a **PostToolUse** hook that flags UI-affecting edits, a **Stop** hook that blocks "done" until verification ran, and the **touchpoint** MCP (structural UI tools for interactive use). | `/plugin install` |
| **Engine** | The pytest-based verification engine — **a UI check *is* a pytest test** — plus the `cyclaudes verify` CLI the Stop-gate points at. | `pip install` |

The hooks are stdlib-only, so the trigger fires from the plugin alone; the engine is what
actually *runs* the checks.

## Requirements

- **Windows** (Windows 11 tested). macOS is planned but not yet supported.
- **Python 3.10+ on `PATH`** — and it must be the *same* interpreter you install the engine
  into, because the plugin's hooks and MCP invoke bare `python`. (If you use a venv, make sure
  that venv's `python` is the one on `PATH` when Claude Code runs.)

## Install

**1. The engine** (gives you `touchpoint`, the `cyclaudes` package, and the `cyclaudes` CLI):

```
pip install git+https://github.com/CameronCrow/Cyclaudes.git
```

**2. The plugin:**

```
/plugin marketplace add CameronCrow/Cyclaudes
/plugin install cyclaudes@cyclaudes
```

## How the autonomous loop works

Once both are installed, the loop closes without you:

1. The agent edits a UI file (`**/*.tsx`, `**/*.jsx`, `**/*.xaml`, `**/*.css`, `ui/**`,
   `frontend/**` by default — override in `.cyclaudes/ui-globs.txt`).
2. The **PostToolUse** hook flags it into `.cyclaudes/pending-ui/<session>.json`.
3. When the agent tries to stop, the **Stop** hook checks for a covering verification result:
   - **no result yet →** blocks, telling the agent to run the checks.
   - **pass covering the edits →** allows.
   - **fail →** re-blocks with the diff so the agent self-corrects.
   - **abstain →** allows *and escalates* (a blocking abstain would thrash Claude Code's
     consecutive-block cap and false-pass — so abstain never blocks).
4. The agent authors/runs a cyclaudes check (guided by the `verify-ui` skill), which records
   a result via `cyclaudes verify`. The gate then lets it finish.

## Writing a check

A check is a plain pytest test that launches, drives, and asserts on a real app. The
`verify-ui` skill walks through it (declare post-conditions *first*, then assert). Sketch:

```python
import pytest
from cyclaudes import ui

@pytest.mark.app_session(["python", r"C:\path\to\app.py"], title_contains="My App")
def test_import_shows_loaded_file(app_session):
    ui.wait_until_ready(app_session, signal="Assemble import set")   # warm lazy web UIs
    assert app_session.exists("Assemble import set", role="button")
    assert app_session.exists("data.txt")                            # a loaded-file text node
    assert not app_session.exists("Import set: 5 files")             # must be false — no false pass
```

Run the checks and record the outcome the Stop-gate reads:

```
cyclaudes verify -- -m "ui"      # wraps pytest; maps pass / fail / abstain(exit 12)
```

Vision assertions (Phase 4) are opt-in, for what the tree can't encode:
`cyclaudes.vision.assert_rendered` / `assert_within_viewport` / `assert_matches_baseline`.

## Status & limitations

- **Windows-only** for now (macOS planned).
- **Structural verification:** solid, dogfooded live on a real WebView2 app.
- **Vision:** capture, `assert_rendered`, `assert_within_viewport`, and baseline diff work on
  WebView2; `assert_not_occluded` abstains on WebView2/high-DPI (blocked on an upstream
  touchpoint primitive — see issue #40).
- **Role-sparse React:** the a11y tree can be thin; a CDP DOM-read path (`read_dom_text`) exists
  but isn't yet wired into the launch flow (needs remote-debugging launch support).
- **The autonomous trigger is built and acceptance-tested**; the full hands-off loop inside a live
  Claude Code session is the current dogfood target.

## Docs

- `planning/PLAN_MAIN.md` — the brief, roadmap, and current state.
- `skills/verify-ui/SKILL.md` — the check-authoring workflow.
