## Git

Commit milestone work **one phase per commit** (the phases in `planning/TODO.md`,
e.g. "Phase 1 / Phase 2 / …"). Each commit carries that phase's real artifacts
plus the matching `TODO.md` checklist edit — and *only* that phase's TODO edit. When
several phases were done together so `TODO.md` holds all their updates at once, stage
the file in **intermediate states** to split it: revert the other phases' TODO lines,
commit phase N with its line, then restore the next phase's line and commit it, and so
on. Don't lump multiple phases' TODO edits into one commit, and don't commit a phase's
code without its TODO line. (Commit on `main`)
