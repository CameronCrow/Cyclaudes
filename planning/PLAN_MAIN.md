---
type: reference
tags: [repo/PROJECT_TEMPLATE]
up: "[[PROJECT_TEMPLATE]]"
---
# Planning

**Status:** Unscoped brief, written for Fable to pick up (2026-07-17). Not a
design — the first real task here is turning this into a `PHASE_1.md` with
committed decisions, not writing code.

## Problem

Claude Code can modify a UI but can't cheaply *verify* one on its own today.
The only two options are: ask Cameron to eyeball it, or take a screenshot and
have a multi-modal pass visually inspect pixels — slow, imprecise, and
expensive to run in a tight verification loop.

## Seed

The `Ladder-Logic-Translator-LLT` repo already solved a narrow version of
this: it drives Rockwell's Logix Designer via Windows UI Automation, through
`pywinauto`'s `Desktop` backend (gated behind LLT's optional `[importer]`
extra) — see `src/llt/importer/driver.py` and `tools/inspect_studio_uia.py` in
that repo (local path: `C:\Users\ccrow\Projects\Ladder-Logic-Translator-LLT`).
That gives *structural* access to a UI — the control tree, element
properties, text values — instead of pixels.

## Vision (Cameron's framing)

> Compile and make easily usable those kinds of tools for Claude, so it can
> run/test UI and multi-modal applications itself, rather than being blocked
> on manual verification or frame-capture-and-analyze approaches.

Generalize LLT's app-specific UIA driver into a reusable toolkit any Claude
Code session can reach for — not just for Logix Designer.

## Open questions (the actual scope of this brief)

- **Delivery shape.** A Claude Code plugin? An MCP server exposing UIA
  primitives (find element, read property, click, read text) as tools? A
  plain Python library other repos import? Cameron described it as "a Claude
  plugin we custom build" — start there, but confirm what a Claude Code
  plugin can actually expose (tool definitions, permissions) before
  committing to that shape.
- **Scope of "multi-modal."** Does this replace frame-capture/vision analysis
  entirely, or keep it as a fallback where structural access isn't available
  (web UIs, games, anything without a native accessibility tree)? Windows UIA
  is Windows-only and desktop-only — decide early whether this is
  Windows-first or needs a cross-platform story.
- **Relationship to LLT.** Does LLT's driver get extracted/generalized into
  this repo (LLT then depends on Cyclaudes), or does Cyclaudes start fresh and
  only borrow the pattern? LLT is Cameron's active work project with a
  near-term deadline (last Simplex day ~Jul 24) — don't disrupt it to
  refactor prematurely.
- **Verification-loop design.** How does a Claude session actually use this
  mid-task — a tool it calls directly, a subprocess, a queried service? It
  should tie into how Claude Code already verifies its own work (test
  runners, build checks) rather than being a bolt-on side channel.

## Current State

Just scaffolded (2026-07-17) from PROJECT_TEMPLATE. No code yet.

## Related

- `Ladder-Logic-Translator-LLT` — `src/llt/importer/driver.py`,
  `tools/inspect_studio_uia.py` — the seed UIA implementation.
- `workforce` repo Projects registry — "Multi-modal Claude Verification"
  entry (Mode: repo) points here.
- [[Repos/PROJECT_TEMPLATE/planning/PHASE_1|PHASE_1]]
- [[Repos/PROJECT_TEMPLATE/planning/TODO|TODO]]
- [[PROJECT_TEMPLATE]]
