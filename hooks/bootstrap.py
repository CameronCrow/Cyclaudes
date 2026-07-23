#!/usr/bin/env python3
"""SessionStart bootstrap — auto-install the cyclaudes engine, so installing the
plugin is the only step a user takes.

The plugin ships config + scripts: the ``verify-ui`` skill, the stdlib-only hook
scripts, and the touchpoint MCP *declaration*. The verification **engine** —
the ``cyclaudes`` package, its ``cyclaudes verify`` CLI, and the third-party
``touchpoint-py`` / ``pytest`` deps — is a real Python package that must live in
the interpreter the MCP server and the checks run under. Claude Code plugins
can't declare pip dependencies, so this hook installs the engine on session
start:

- **into THIS interpreter** (``sys.executable`` — the same bare ``python`` the
  MCP command and the ``python ${CLAUDE_PLUGIN_ROOT}/hooks/*`` hooks resolve to),
- **idempotently** — a fast import+version check no-ops once it is present at the
  plugin's version, so it only actually installs on the first session (or after a
  plugin version bump),
- **fail-safe** — any problem here prints a one-line note telling the user the
  manual command and exits 0; a bootstrap issue must never break the session.

Stdlib-only and loaded by file path, exactly like the other hooks — it cannot
assume ``cyclaudes`` is importable, since making it importable is its whole job.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

#: Everything the engine side needs present in this interpreter.
REQUIRED = ("cyclaudes", "touchpoint", "pytest")
#: Published fallback when the installed plugin dir isn't a pip project.
REPO_URL = "git+https://github.com/CameronCrow/Cyclaudes.git"


def plugin_root() -> Path:
    """The installed plugin directory (``${CLAUDE_PLUGIN_ROOT}``), best-effort."""
    root = os.environ.get("CLAUDE_PLUGIN_ROOT")
    if root:
        return Path(root)
    return Path(__file__).resolve().parent.parent  # <plugin>/hooks/bootstrap.py -> <plugin>


def plugin_version(root: Path) -> str | None:
    """The plugin's declared version from ``.claude-plugin/plugin.json``, or None."""
    try:
        data = json.loads((root / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
        v = data.get("version")
        return str(v) if v is not None else None
    except Exception:
        return None


def installed_ok(want_version: str | None) -> bool:
    """Whether all required packages are importable and cyclaudes matches ``want_version``.

    A version mismatch counts as "not ok" so a plugin update reinstalls the
    matching engine rather than running checks against stale code. If the version
    can't be read (e.g. cyclaudes is on the path but not a pip distribution), we
    err toward reinstalling — safe, since pip is a no-op when already satisfied.
    """
    if any(importlib.util.find_spec(m) is None for m in REQUIRED):
        return False
    if not want_version:
        return True
    try:
        import importlib.metadata as md

        return md.version("cyclaudes") == want_version
    except Exception:
        return False


def pip_target(root: Path) -> str:
    """What to hand pip: the bundled plugin source (exact version) or the repo URL."""
    return str(root) if (root / "pyproject.toml").is_file() else REPO_URL


def pip_command(root: Path) -> list[str]:
    """The pip install argv, targeting this interpreter (user site if not a venv)."""
    cmd = [sys.executable, "-m", "pip", "install", "--quiet", "--disable-pip-version-check"]
    # A bare system interpreter (not a virtualenv) needs --user to avoid writing
    # to a protected site-packages; inside a venv --user is invalid, so skip it.
    if sys.prefix == sys.base_prefix:
        cmd.append("--user")
    cmd.append(pip_target(root))
    return cmd


def main() -> int:
    root = plugin_root()
    if installed_ok(plugin_version(root)):
        return 0  # already present at the right version — silent fast path
    try:
        subprocess.run(pip_command(root), check=True, capture_output=True, timeout=900)
        print("[cyclaudes] verification engine installed — checks are ready.")
    except Exception as exc:  # never break the session
        print(
            f"[cyclaudes] could not auto-install the engine ({type(exc).__name__}). "
            f"Run manually: {sys.executable} -m pip install {pip_target(root)}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
