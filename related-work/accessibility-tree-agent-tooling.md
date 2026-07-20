---
type: research-notes
tags: [repo/Cyclaudes, related-work]
---
# Research: existing tools + cross-platform options for accessibility-tree agent control

**Date:** 2026-07-20
**Research question:** For Cyclaudes (a Claude Code plugin giving Claude structural/UIA-based
control+verification of desktop UIs), what prior art already exists, and what's realistically
available beyond pywinauto (Windows-only) for macOS/Linux?

## 1. Existing tools/prior art

### MCP servers — Windows (UIA-based)
- **Windows-MCP** (CursorTouch) — most adopted; 6.5k★, active (v0.8.2, Jun 2026). Two modes:
  Snapshot (UIA tree, interactive element IDs) and Screenshot (visual fallback). Python, MIT.
  https://github.com/CursorTouch/Windows-MCP
- **mcp-windows** (sbroenne) — finds elements "by name, not coordinates" via UIA; actively
  released (v1.3.9), also on VS Marketplace/NuGet. https://github.com/sbroenne/mcp-windows
- **uiautomation-mcp** (locomorange) — C# native, multi-process architecture, Native AOT; small
  (23★, v0.1.2 Feb 2026) but structurally close to what we want. https://github.com/locomorange/uiautomation-mcp
- **FlaUI-MCP** (shanselman) — wraps FlaUI + UIA; less release activity than the above two.
  https://github.com/shanselman/FlaUI-MCP
- **Windows 365 for Agents MCP** (Microsoft, preview) — retrieves UIA tree for foreground window
  of a Windows 365 Cloud PC, returns clickable coords. Enterprise/cloud-PC scoped, not local-desktop.
  https://learn.microsoft.com/en-us/microsoft-agent-365/mcp-server-reference/windows-365-agents

### MCP servers — macOS (AXUIElement-based)
- **macOS-MCP** (CursorTouch) — 123★, active (v0.3.11, Jul 15 2026), PyObjC + ApplicationServices,
  explicitly "doesn't require computer vision." Closest macOS analog to Windows-MCP.
  https://github.com/CursorTouch/MacOS-MCP
- **mac-use** (entpnomad) / **macos-use.dev** — "drive any Mac app from Claude Code (no
  AppleScript)," reads AX tree directly. https://github.com/entpnomad/mac-use
- **automac-mcp** (digithree) — experimental, "full Mac UI automation." https://github.com/digithree/automac-mcp
- **macos-ui-automation-mcp** (mb-dev) — similar space, less visibility in search results.
  https://github.com/mb-dev/macos-ui-automation-mcp

### Cross-platform (closest to Cyclaudes' eventual goal)
- **Touchpoint** (Touchpoint-Labs) — Python, MIT, 42★ (Jun 2026 release). Unifies Windows UIA /
  macOS Accessibility / Linux AT-SPI2 *and* ships a built-in MCP server (vision + no-vision modes)
  for Claude/Cursor/local models. Also covers Chromium/Electron via CDP. Best single existing
  analog to "Cyclaudes as an MCP server." https://github.com/Touchpoint-Labs/touchpoint
- **agent-desktop** (lahfir) — Rust CLI, Apache-2.0, 970★, very active (v0.5.0 Jul 2026).
  Explicitly built for AI agents: deterministic element refs, progressive tree traversal for
  78-96% token reduction. macOS fully supported; Windows/Linux "planned but not implemented" as
  of this writing. No MCP server — CLI + C-ABI FFI only (would need an MCP wrapper).
  https://github.com/lahfir/agent-desktop / https://agent-desktop.dev/
- **mobile-mcp** (mobile-next) — same accessibility-first philosophy applied to iOS/Android;
  not desktop, but a useful sibling-precedent for "structural tree, screenshot only as fallback."
  https://github.com/mobile-next/mobile-mcp

### Claude Code plugins doing UI work today
- **accessibility-agents** (Community-Access) — 11 specialist agents enforcing WCAG 2.2 AA in
  *generated code*; static/lint-style review, not a runtime verification loop against a live UI.
  https://github.com/Community-Access/accessibility-agents
- No Claude Code plugin found that closes the loop Cyclaudes wants (Claude implements → Claude
  itself drives/reads the live UI via accessibility tree to verify). This looks like a genuine gap.

### Non-Claude agent frameworks (structural UI automation)
- **Microsoft UFO** — dual-agent (AppAgent/ActAgent) Windows GUI agent; uses pywinauto + UIA as
  the execution backend, GPT-Vision for perception (hybrid, not pure structural). 86% success on
  WindowsBench. Explicitly limited to what pywinauto/UIA support. arXiv:2402.07939
- **Windows-Use** (CursorTouch) — agent reads UIA tree, any LLM decides actions. Same author as
  Windows-MCP/MacOS-MCP. https://github.com/CursorTouch/Windows-Use
- **pywinassistant** — natural-language Windows GUI control, but leans on visual/spatial reasoning
  (Visualization-of-Thought) more than pure structural reads. https://github.com/a-real-ai/pywinassistant
- **FastAgent** (HKUDS) — includes platform adapters: Linux (pyatspi, xlib), Windows (pywinauto),
  plus generic "accessibility tree utilities" — a rare framework already spanning Win+Linux.
  https://github.com/HKUDS/FastAgent
- Academic: "Rethinking OS Interfaces for LLM Agents" (2026) and "COLA: A Scalable Multi-Agent
  Framework for Windows UI Task Automation" (arXiv:2503.09263) — landscape/architecture papers
  worth a skim if Cyclaudes writes up its own design rationale.

### Anthropic's own computer use tooling
Confirmed via official docs (platform.claude.com/docs/en/agents-and-tools/tool-use/computer-use-tool,
checked 2026-07-20): **screenshot + mouse/keyboard only** — "Screenshot capture: See what's
currently displayed on screen" / "Mouse control" / "Keyboard input." No mention of accessibility
tree, UIA, AXUIElement, or AT-SPI anywhere in the current tool docs. Beta header
`computer-use-2025-11-24` (Sonnet 5, Opus 4.8/4.7/4.6, Sonnet 4.6, Opus 4.5). Launched as public
beta Oct 2024, reached Claude Pro/Max desktop research-preview Mar 24 2026 (macOS). **No public
Anthropic hint of a structural/accessibility-tree-based alternative to computer use found** —
recent Claude Code accessibility work (screen-reader mode) is about Claude Code's *own* UI being
accessible to blind users, unrelated to Claude driving *other* apps structurally. This is a real
white-space: nobody has an official first-party accessibility-tree "computer use v2."

## 2. Cross-platform structural UI automation beyond pywinauto

### macOS — Accessibility API (AXUIElement / NSAccessibility)
- **Raw PyObjC + ApplicationServices** — the ground-truth binding; what everything else wraps.
  No package name, just `pyobjc-framework-ApplicationServices` + `pyobjc-framework-Cocoa`.
- **atomac / atomacos** — first Python lib built specifically for AX-based GUI testing.
  **atomacos is ARCHIVED (read-only since Mar 9 2024, last release Oct 2019, GPL-2.0, 49★)** —
  don't build on it; it looked like the "maintained fork" but is itself stale now.
  https://github.com/daveenguyen/atomacos
- **PyXA** ("Python for Automation") — wraps AppleScript/JXA scripting dictionaries + some direct
  Accessibility/UI-scripting fallback for non-scriptable apps; 160★, MIT, but modest recent
  activity (latest v0.3.0 Jan 2024). Higher-level than raw AX, less complete than hand-rolled
  PyObjC for apps with no scripting dictionary. https://github.com/SKaplanOfficial/PyXA
- **AppleScript / JXA (JavaScript for Automation) directly** — OSA-native, "UI scripting" is
  explicitly a fallback path Apple intends for apps lacking a scripting dictionary; JXA gets full
  Objective-C bridge access to AX classes since Yosemite. No Python needed but requires a
  subprocess bridge (`osascript`) from Python/other languages.
- **Permissions:** macOS Accessibility permission is granted via System Settings → Privacy &
  Security → Accessibility, and — per macOS's TCC model — it's granted to the **host process**
  (Terminal, Claude Desktop, VS Code, whatever spawns the automation code), not to the library
  itself. Confirmed across multiple 2026 MCP-server READMEs (macOS-MCP, mac-use).
- **Known limitations vs Windows UIA:** AX tree is comparatively untyped/flexible (no fixed
  control-type enum like UIA's 41 types), and lacks a batch "read whole subtree" primitive —
  cross-platform library authors (xa11y blog post) specifically flag this as harder to
  abstract cleanly than UIA.

### Linux — AT-SPI2
- **pyatspi2** (GNOME, official) — Python3 bindings for libatspi over D-Bus; hand-written wrapper,
  actively part of the GNOME accessibility stack (not agent-specific but the standard binding).
  https://github.com/GNOME/pyatspi2 — architecture: App → ATK/QAccessible → AT-SPI2 Registry →
  D-Bus → client. Known perf caveat: AT-SPI2 requires individual D-Bus calls per node (no batch
  read), which the xa11y author calls out as a performance bottleneck vs UIA/AX.
- Lower priority per the brief, but pyatspi2 is mature enough (GNOME-maintained, used for Orca
  screen reader) that it's not a blocker if/when Linux support becomes worth doing.

### Cross-platform abstraction libraries (unify UIA + AX + AT-SPI)
- **xa11y** — Rust core, Python/JS bindings + CLI, MIT, v0.11 (pre-1.0 but active). "Playwright-
  style" API, CSS-like element queries (`button[name='Submit']`), explicitly built for "end-to-end
  tests, computer-use agents, and assistive tools." Handles the three platforms' divergent
  semantics with platform-specific query evaluation + lazy/retrying locators rather than a naive
  common-denominator tree. Best-documented design rationale of anything found — worth reading its
  blog post even if not reused directly. https://xa11y.dev/ /
  https://crowecawcaw.github.io/general/2026/05/30/accessibility-for-computer-use.html
- **Touchpoint** — see above; Python, ships MCP server, all 3 platforms today (unlike agent-desktop
  which is macOS-only so far). Second-closest existing analog to "Cyclaudes, but already built."
- **agent-desktop** — see above; Rust, macOS-only currently, Windows/Linux on the roadmap.
- **pyUIauto** (PyPI) — older, smaller-scope Python lib wrapping platform automation libs into a
  common interface; didn't turn up evidence of AI-agent orientation or recent activity.
- **uia2atk** (Mono/Novell, historical) — a UIA-to-ATK bridge for Linux; dead project, useful only
  as a historical note that "bridge one platform's API onto another's" has been tried before and
  didn't stick. https://github.com/mono/uia2atk

## Build-vs-reuse takeaway (for the open question in PLAN_MAIN.md)
No first-party Anthropic or Claude-plugin solution exists yet — genuine white space for the
"verification loop tied into Claude Code" framing. Closest prior art to fork/study rather than
build fully from scratch: **Touchpoint** (Python, cross-platform, ships MCP server already) and
**xa11y** (best cross-platform abstraction design, but Rust-first / pre-1.0). Windows-MCP /
macOS-MCP (CursorTouch, same author, both actively maintained) are the best single-platform
reference implementations for tool-shape (Snapshot/Click/Type primitives) if Cyclaudes stays
Windows-first initially and wants a proven MCP tool surface to imitate.

## Touchpoint hands-on smoke test (2026-07-20, Windows 11, Notepad)

`pip install touchpoint-py` → v0.3.0, Python 3.14. Registered as a project-local Claude Code MCP
server in no-vision mode:
`claude mcp add touchpoint -s local -e TOUCHPOINT_MODE=no-vision -- <Scripts>\touchpoint-mcp.exe`

`diagnostics()` → `UiaBackend` + `SendInputProvider`, both initialized, zero errors, no config
needed on Windows. Mode env var took effect (tool surface showed `diff_snapshot`, no `screenshot`).

**What worked (round-trip verified):**
- `windows()` — enumerated all 21 open windows with title/size/app, instantly.
- `snapshot(window_id=)` — clean indented tree of Notepad: the editable document exposed as
  `document "Text editor" focused,editable value='' [id]`, plus menu bar, tabs, toolbar toggles.
  Compact enough to read directly; no vision model involved.
- `set_value(id, text, replace=True)` — wrote text, returned `OK` **and** the auto-verify flag
  `(window title changed: '*Cyclaudes smoke test... - Notepad')`.
- `read_text(id)` — independent read-back returned the exact string written. **This is the core
  Cyclaudes loop (act → structurally verify) and it works.**
- `find(query, window_id=)` — fuzzy name match; `"Bold"` correctly matched `'Bold (Ctrl+B)'`.
- `click(id)` + state assertion — after clicking, the toggle read `checked,pressed` and a new
  `'Formatted'` button appeared in the tree. State changes are observable structurally.
- **Modal dialogs are fully exposed** — Notepad's unsaved-changes prompt appeared as
  `dialog "Notepad" modal [id]` with `Save` / `Don't save` / `Cancel` children, all clickable.
  This matters: dialog-blocked states are exactly what a verification loop must detect.
- `wait_for_window(title, gone=True)` — confirmed teardown.

**Gotchas found (all relevant to Cyclaudes' design):**
1. **`close_window()` returned bare `OK` while silently failing.** A modal save-prompt blocked the
   close; the window stayed open and **no `(new window: ...)` flag was emitted**. The auto-verify
   flags are best-effort, not a guarantee — a false-positive success. Cyclaudes must re-assert
   state independently after every action rather than trusting an action's own return.
2. **Element IDs churn wholesale on tree mutation.** After the dialog opened, the document went
   `uia22` → `uia52` and every toolbar ID renumbered. IDs are per-snapshot handles, not durable
   references. Any multi-step verification must re-snapshot, not cache IDs.
3. **`wait_for_window()` does substring title matching and auto-activates the match** — it grabbed
   a *pre-existing* unrelated Notepad window (`import-...log - Notepad`) instead of the new one.
   On a busy desktop this is a real footgun: it can silently retarget input at the wrong window.
   Always resolve the window ID via `windows()` and act element-scoped.
4. `find(states=[...])` needs exact platform state vocabulary — guessing `"selected"` for a toggle
   returned nothing; the real states were `checked,pressed`. Discover states via `snapshot()` first.

**Takeaway:** Touchpoint's primitives are sound and the structural act→verify loop genuinely works
on Windows with zero setup. The gap Cyclaudes fills is *not* the driver layer — it's the
disciplined verification wrapper on top: re-assert after every action, never cache element IDs
across mutations, resolve windows explicitly. Reuse Touchpoint, build the loop. Not yet tested:
macOS/AX backend, a complex dynamic tree (Logix Designer deliberately avoided pre-Jul-24), and
Electron/CDP apps.

## Sources
All URLs above; accessed 2026-07-20 via WebSearch + WebFetch. Star counts / release dates are
point-in-time snapshots as of this date and will drift.
