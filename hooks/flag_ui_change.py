"""PostToolUse relevance detector — the cheap, deterministic half of the Phase 3 trigger.

Fires on every ``Edit``/``Write`` tool call. Checks whether the touched file matches
this repo's UI globs and, if so, records it in session-scoped state under
``<project>/.cyclaudes/pending-ui/<session_id>.json`` for the Stop hook (Phase 3,
deliverable B, issue #32) to read. Free — no filesystem write at all — on the common
case of a non-UI change, which is the whole point: verifying every edit would be slow
enough that the trigger gets disabled.

This module is import-safe: :func:`flag` is the entire implementation, and the
``__main__`` block below it is a thin stdin/stdout adapter, so tests call ``flag()``
directly against a tmp project dir rather than spawning a subprocess.

FROZEN INTERFACE (see ``planning/PHASE_3.md`` → Implementation design →
FROZEN INTERFACE) — do not change this shape without updating that section first;
the Stop hook (issue #32) is built against it in parallel::

    {"session_id": "...", "ui_touched": ["relpath/one.tsx", "relpath/two.xaml"]}

Per-repo glob override: drop a ``.cyclaudes/ui-globs.txt`` in the project root, one
glob pattern per line (``#`` comments and blank lines ignored). Its presence
*replaces* :data:`DEFAULT_UI_GLOBS` entirely — an override that wants to keep some of
the defaults must repeat them.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any

__all__ = ["DEFAULT_UI_GLOBS", "flag"]

#: Default UI-glob set, matched against the repo-relative, forward-slash-normalized
#: path. Overridable per-repo via ``.cyclaudes/ui-globs.txt`` (see module docstring).
DEFAULT_UI_GLOBS = [
    "ui/**",
    "**/*.tsx",
    "**/*.jsx",
    "**/*.xaml",
    "**/*.css",
    "frontend/**",
]

_OVERRIDE_PATH = Path(".cyclaudes") / "ui-globs.txt"


def _load_globs(project_dir: Path) -> list[str]:
    """The glob set in effect for *project_dir*: the override file if present, else the default."""
    override = project_dir / _OVERRIDE_PATH
    if not override.is_file():
        return DEFAULT_UI_GLOBS
    globs = [
        line.strip()
        for line in override.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    return globs or DEFAULT_UI_GLOBS


def _glob_to_regex(pattern: str) -> re.Pattern[str]:
    """Compile *pattern* (posix-style, ``**`` allowed) to a regex matching a full relpath.

    :mod:`fnmatch` and :meth:`pathlib.PurePath.match` don't give ``**`` "any number
    of path segments" semantics portably across the Python versions this project
    supports, so this is a small hand-rolled translator: ``**/`` and ``**`` cross
    directory boundaries; ``*`` and ``?`` stay within a single segment.
    """
    pattern = pattern.replace("\\", "/")
    parts: list[str] = []
    i, n = 0, len(pattern)
    while i < n:
        if pattern[i : i + 3] == "**/":
            parts.append("(?:.*/)?")
            i += 3
        elif pattern[i : i + 2] == "**":
            parts.append(".*")
            i += 2
        elif pattern[i] == "*":
            parts.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            parts.append("[^/]")
            i += 1
        else:
            parts.append(re.escape(pattern[i]))
            i += 1
    return re.compile("^" + "".join(parts) + "$")


def _matches_ui_glob(relpath: str, globs: list[str]) -> bool:
    relpath = relpath.replace("\\", "/")
    return any(_glob_to_regex(g).match(relpath) for g in globs)


def _repo_relative(file_path: str, project_dir: Path) -> str:
    """*file_path* relative to *project_dir*, forward-slash-normalized.

    ``tool_input.file_path`` is normally absolute; a relative path (already
    repo-relative) is passed through rather than re-resolved against the process's
    current working directory, which may not be *project_dir*.
    """
    path = Path(file_path)
    if path.is_absolute():
        try:
            relpath = os.path.relpath(path, project_dir)
        except ValueError:
            # Different drive on Windows — no relative path exists. Fall back to the
            # absolute path so matching degrades sanely (misses, never crashes).
            relpath = str(path)
    else:
        relpath = str(path)
    return relpath.replace("\\", "/")


def flag(payload: dict[str, Any], project_dir: str | Path) -> None:
    """Core relevance test.

    Reads *payload* — a ``PostToolUse`` hook stdin JSON — and, if
    ``tool_input.file_path`` matches a UI glob for *project_dir*, appends its
    repo-relative path (de-duplicated) to
    ``<project_dir>/.cyclaudes/pending-ui/<session_id>.json`` per the frozen schema.
    Does nothing on a non-UI path or an incomplete payload.

    Never raises. A ``PostToolUse`` hook cannot block the tool call anyway, so the
    only sane failure mode for a malformed payload is silence, not a crash the agent
    has to work around.
    """
    session_id = payload.get("session_id")
    tool_input = payload.get("tool_input") or {}
    file_path = tool_input.get("file_path")
    if not session_id or not file_path:
        return

    project_dir = Path(project_dir)
    relpath = _repo_relative(file_path, project_dir)
    if not _matches_ui_glob(relpath, _load_globs(project_dir)):
        return

    state_dir = project_dir / ".cyclaudes" / "pending-ui"
    state_dir.mkdir(parents=True, exist_ok=True)
    state_path = state_dir / f"{session_id}.json"

    ui_touched: list[str] = []
    if state_path.is_file():
        try:
            existing = json.loads(state_path.read_text(encoding="utf-8"))
            ui_touched = list(existing.get("ui_touched", []))
        except (json.JSONDecodeError, AttributeError):
            ui_touched = []

    if relpath not in ui_touched:
        ui_touched.append(relpath)

    state_path.write_text(
        json.dumps({"session_id": session_id, "ui_touched": ui_touched}, indent=2),
        encoding="utf-8",
    )


def _main() -> int:
    """Stdin/stdout adapter Claude Code actually invokes. Always exits 0."""
    try:
        payload = json.load(sys.stdin)
        project_dir = payload.get("cwd") or os.getcwd()
        flag(payload, project_dir)
    except Exception:
        # PostToolUse cannot block the tool call; a broken hook must stay silent
        # rather than surface as a tool-call error the agent has to work around.
        pass
    return 0


if __name__ == "__main__":
    sys.exit(_main())
