---
type: reference
tags: [repo/Cyclaudes, planning]
up: "[[Cyclaudes]]"
---
# React / div-soup robustness — CDP DOM-read verification

**Issue:** #37 — *robustness: React/div-soup apps expose thin a11y trees.*
**Status:** investigation done; one safe slice shipped (see [§6](#6-shipped-slice));
the rest is a scoped plan, not yet built.
**Date:** 2026-07-23

## 1. The gap, precisely

Cyclaudes verifies a live UI by reading its **accessibility (a11y) tree** through
touchpoint and asserting on element names/text/states (`ui.py`). Structural
verification can only assert on what the app *puts in that tree*.

React (and other component frameworks) routinely ship **div-soup with no ARIA
roles or names** — clickable `<div>`s, unlabeled inputs, custom widgets that
mount but expose nothing semantic. On such a UI:

- `WindowHandle._snapshot()` → `touchpoint.elements(window_id=…)` returns
  unnamed generics or a near-empty tree;
- `_resolve(query)` finds nothing to bind a name query to →
  `ElementNotFound`;
- `assert_text` / `assert_state` have nothing to bite on — *even though the app
  renders fine to a human*.

React is one of the most popular UI stacks, so "robust to React" is close to
"robust in general" for web / Electron / WebView2 targets. The 2026-07-22 LLT
dogfood (WebView2/Chromium) only worked because LLT happened to be reasonably
semantic; a role-sparse React app would give much less.

## 2. What CDP actually gives us (verified against touchpoint source)

Touchpoint ships a **Chrome DevTools Protocol (CDP) backend**
(`touchpoint/backends/cdp/cdp.py`, ~4200 lines) that connects over a WebSocket
to a Chromium/Electron `--remote-debugging-port`. Two distinct read paths:

| Path | touchpoint call | CDP domain | What it reads |
|---|---|---|---|
| **CDP AX tree** | `elements(source="cdp_ax")` or a `cdp:` window id | `Accessibility.getFullAXTree` | Chromium's *in-browser a11y projection* — still thin for div-soup |
| **CDP DOM walk** | `elements(source="dom")` | `Runtime.evaluate` injecting a JS DOM walker | The **actual live DOM** |

The **`source="dom"` path is the one that closes the #37 gap.** It injects
`_DOM_WALKER_JS` (a self-contained JS function) via `Runtime.evaluate` and walks
the real DOM from `document.body`, collecting every element that is *visible* and
either interactive or has text. Key properties, read off the walker source:

- **Role-less `<div>` with text is captured.** The walker emits any node whose
  `directText()` (its own text nodes, not inherited) is non-empty, or that is a
  leaf with `textContent`. A `<div>Total: 42</div>` with no ARIA becomes an
  element whose `name` is `"Total: 42"`. This is exactly what the a11y tree
  omits.
- **Name priority:** `aria-label` > `title` > direct text > (leaf) full
  `textContent`.
- **Inputs** report `value` (via `?? null`, so `""` is preserved).
- **Shadow DOM** is traversed (`node.shadowRoot.children`), so web-component
  custom widgets are reachable.
- **`aria-hidden` subtrees are dropped** — consistent with a11y semantics.
- **States** are inferred (`disabled`, `checked`, `expanded`, `required`,
  `readOnly`, `focused`).
- **`get_text_content(el)`** (used by `ui.read_text`) reads live
  `textContent`/`value` via `Runtime.callFunctionOn` — real DOM text, not a
  projection — for a DOM-sourced element id too (`_resolve_backend_node_id`
  handles the `dom:` id shape).

So: **for a Chromium-backed target, a role-sparse React app that abstains under
a11y-only reads exposes real, assertable text and structure through the DOM
walk.** That is a genuine, not speculative, capability.

### How touchpoint decides a target is CDP-backed

- **Discovery:** `discover_cdp_ports()` scans process command lines
  (`/proc/*/cmdline` on Linux, PowerShell `Get-CimInstance` on Windows, `ps` on
  macOS) for `--remote-debugging-port=N`, keeping the **main browser PID** (child
  renderer/GPU processes are filtered by `--type=`).
- **`_is_cdp_app(app)` / `_is_cdp_id(window_id)`** — an id starting with `cdp:`,
  or an app whose PID is in the CDP-owned PID set.
- **Window merge (critical for us):** `touchpoint.windows()` **replaces** the
  native OS window of a CDP-owned PID with the CDP page-target window
  (`[w for w in platform_wins if w.pid not in cdp_pids]` + the CDP windows). So
  for a CDP-backed app, the window Cyclaudes resolves already carries a `cdp:`
  id and the browser's main PID — no extra plumbing to "find the CDP handle".

### ID shapes (all opaque to Cyclaudes — never parsed here)

| Source | Example id |
|---|---|
| Windows UIA | `uia52` (churns on tree mutation) |
| CDP AX | `cdp:{port}:{targetId}:{nodeId}` |
| CDP DOM | `cdp:{port}:{targetId}:dom:{page_cx},{page_cy}` |

Cyclaudes' contract already treats every id as an opaque handle (`ui.py`
portability rules), so these ride through unchanged.

## 3. Does it fit the discipline? Yes — cleanly

`ui.py` enforces: name-only API, re-resolve fresh every call, owned-only via
PID, abstain honestly (never false-pass). The DOM path fits each:

- **Owned-only via PID.** A CDP window's `pid` is the browser main PID.
  `is_owned(pid)` is set-membership **plus process ancestry**
  (`_is_descendant_of_owned`), so an app Cyclaudes launched (or that re-exec'd
  from a launched process) is owned; the DOM read runs `_check_owned()` first,
  exactly like the AX read. A window we did *not* launch (e.g. attaching to
  Cameron's already-open Chrome) is correctly **not** owned → refused. No new
  ownership surface is needed.
- **Re-resolve fresh.** `get_dom_elements()` re-walks the live DOM on every call
  — nothing is cached, element ids are recomputed each walk. The
  "no cached ids" footgun stays impossible.
- **Abstain honestly.** When the target is not CDP-backed (no debugging port,
  or `websocket-client` missing), the DOM path either raises
  `touchpoint.TouchpointError` (`BackendUnavailableError` is a subclass) **or**
  returns an empty list. Both must map to an **abstention**, never a pass. This
  is the one piece of new wiring, and it is the shipped slice ([§6](#6-shipped-slice)).

## 4. What CDP does **not** solve (honest limits)

The DOM path is powerful *only for Chromium DOM that Cyclaudes owns and that was
started with remote debugging.* It does **not** help with:

1. **Non-Chromium React.** React Native, and React rendered in a WebKit/Gecko
   webview without a CDP endpoint. CDP is Chromium-only.
2. **Apps not launched with `--remote-debugging-port`.** No port ⇒ no DOM read.
   Electron/Chrome need the flag at launch; WebView2 needs it via
   `WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS`. This is an **operational
   precondition** the launcher must satisfy — see slice 4. Absent the flag, the
   target stays a native UIA window and the DOM read abstains (correctly).
3. **Canvas / WebGL-rendered UIs** (Figma-style, some charting). The DOM walker
   finds the `<canvas>` element but no text inside it. This is Phase-4 vision
   territory, not DOM.
4. **Native div-soup equivalents** — WinUI/Qt/GTK apps with unlabeled custom
   controls. Not DOM at all; no CDP.
5. **Cross-origin iframes** need extra target-grafting (touchpoint's tree path
   does some of this; the flat DOM walk does not fully). Out of scope for the
   first slices.
6. **Async render timing.** A React tree can mount content a beat after the
   window is "ready". The a11y-side answer is `wait_until_ready(signal=…)` +
   settling asserts; the DOM read needs the same settle treatment (slice 3). The
   shipped read-only primitive (slice 1) does **not** settle — a caller must pair
   it with retry or use the slice-3 asserts once they exist.

Where none of the above applies, the honest answer remains: **abstain, and tell
the dev to add ARIA roles / `data-testid`-style hooks** (issue #37 lead 3).
Never a silent pass.

## 5. Scoped plan (slices → issues)

Ordered; each is independently shippable and testable with fakes. Slice 1 is
done.

1. **Abstention-correct DOM-text read primitive** *(SHIPPED, [§6](#6-shipped-slice))*.
   `WindowHandle.read_dom_text(query)` + `DomUnavailable` abstention. Reads real
   DOM text when the owned window is CDP-backed; abstains (never false-passes)
   when it isn't. Read-only, additive, does not touch the AX hot path.
2. **DOM-aware element resolution polish.** Optional `role=` filtering and
   ambiguity messaging parity with the AX `_resolve` (already reused via
   `_match`); add `exists_dom` / `states_dom` read-only helpers if checks need
   them. Small.
3. **DOM-sourced settling assertions.** `assert_text(..., source="dom")`,
   `assert_exists`, `assert_state`, `assert_gone` that snapshot the DOM inside
   the existing `_settle` loop (so async React renders get ret/retry) and abstain
   `DomUnavailable` at the deadline when the DOM can't be read. This is where the
   real check-author ergonomics land. Medium; the settle machinery already
   exists.
4. **Launcher support for remote debugging.** Teach `app_session` (`pytest_ui.py`)
   to inject `--remote-debugging-port` (Electron/Chrome) or the WebView2 env var,
   confirm the resolved owned window is a `cdp:` window, and **abstain with
   guidance** ("could not make <app> CDP-backed; add `--remote-debugging-port` or
   add ARIA hooks") when it can't. Without this, the DOM path only works for apps
   a human already started with debugging on. Medium; touches process launch.
5. **Acceptance proof.** A deliberately role-sparse React sample where a11y-only
   verification abstains **and** the DOM path asserts real content — plus the
   mirror proof that with the debugging port *absent* the same check **abstains,
   never false-passes**. Mirrors `tests/test_acceptance_phase2.py` /
   `_phase4.py`. This is the criterion in issue #37's Acceptance section.

## 6. Shipped slice

`ui.py` now has a read-only DOM-text reader that runs through the full
discipline and abstains cleanly when the target is not CDP-backed:

- **`DomUnavailable(UIError)`** — a new abstention condition, added to
  `ABSTENTION_CONDITIONS` (so the pytest layer surfaces it as *CannotVerify*, its
  own outcome and exit code, not a pass and not a fail). It is not an
  `AssertionError` subclass, so a broad `except AssertionError` can't swallow it.
- **`WindowHandle.read_dom_text(query, *, role=None)`** — resolves the name query
  against a **fresh live-DOM walk** of the owned window and returns the element's
  DOM text/value. Ownership is re-checked first (`_check_owned`), same as every
  other read. When the window is not CDP-backed — touchpoint raises
  `TouchpointError`/`BackendUnavailableError`, *or* the DOM walk comes back empty
  (native UIA window, blank page, or the window vanished) — it raises
  `DomUnavailable` rather than inventing a result. A non-empty DOM in which the
  query simply isn't present still raises the ordinary `ElementNotFound` (a real
  "not there", consistent with the AX `read_text`), never an abstention.
- **`_resolve` refactor.** The name-matching body is extracted to
  `WindowHandle._match(els, query, role=…)` so the AX path (`_snapshot`) and the
  DOM path (`_dom_snapshot`) share identical exact→ci→substring matching and
  ambiguity behavior. The AX path's observable behavior is unchanged.

What the shipped slice deliberately does **not** do: settle/retry (slice 3),
DOM-sourced `assert_*` (slice 3), or launcher flag injection (slice 4). It is a
primitive a check pairs with a plain `assert` today; on abstention that assert's
`DomUnavailable` is caught by the abstention layer.

**Test coverage** (`tests/test_ui.py::TestDomRead`, fake-driven): reads real DOM
text a thin AX tree omits; abstains `DomUnavailable` when touchpoint raises
`TouchpointError`; abstains `DomUnavailable` on an empty DOM walk (not-CDP
window); `ElementNotFound` (not abstention) when the DOM is non-empty but the
query is absent; re-checks ownership and raises `UnownedWindow` after `disown`;
`DomUnavailable` is registered as an abstention type. The one thing fakes cannot
prove — that a *real* CDP DOM walk returns div-soup content — is deferred to the
live acceptance proof (slice 5), which needs a running Chromium React sample.
