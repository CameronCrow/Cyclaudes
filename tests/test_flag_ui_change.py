"""Tests for ``hooks/flag_ui_change.py`` — the PostToolUse relevance detector.

Loaded by file path rather than package import: the hook script deliberately lives
outside the ``cyclaudes`` package (at ``hooks/flag_ui_change.py``) so the bare
``python ${CLAUDE_PLUGIN_ROOT}/hooks/flag_ui_change.py`` invocation Claude Code
actually runs needs no install step. See the module docstring there for the frozen
``pending-ui/<session_id>.json`` schema this exercises.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

HOOK_PATH = Path(__file__).resolve().parents[1] / "hooks" / "flag_ui_change.py"


def _load_hook():
    spec = importlib.util.spec_from_file_location("flag_ui_change", HOOK_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


flag_ui_change = _load_hook()
flag = flag_ui_change.flag


def _payload(session_id: str, file_path: str) -> dict:
    """A minimal PostToolUse stdin payload shape, as Claude Code would send it."""
    return {
        "session_id": session_id,
        "tool_name": "Edit",
        "tool_input": {"file_path": file_path},
    }


def _state_path(project_dir: Path, session_id: str) -> Path:
    return project_dir / ".cyclaudes" / "pending-ui" / f"{session_id}.json"


def _touch(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("// placeholder", encoding="utf-8")


def test_ui_path_is_recorded_with_repo_relative_path(tmp_path):
    file_path = tmp_path / "src" / "components" / "Button.tsx"
    _touch(file_path)

    flag(_payload("sess-1", str(file_path)), tmp_path)

    state = json.loads(_state_path(tmp_path, "sess-1").read_text(encoding="utf-8"))
    assert state == {
        "session_id": "sess-1",
        "ui_touched": ["src/components/Button.tsx"],
    }


def test_non_ui_path_is_a_noop(tmp_path):
    file_path = tmp_path / "src" / "cyclaudes" / "ui.py"
    _touch(file_path)

    flag(_payload("sess-2", str(file_path)), tmp_path)

    assert not (tmp_path / ".cyclaudes").exists()


def test_dedup_same_file_twice_yields_one_entry(tmp_path):
    file_path = tmp_path / "frontend" / "App.jsx"
    _touch(file_path)

    flag(_payload("sess-3", str(file_path)), tmp_path)
    flag(_payload("sess-3", str(file_path)), tmp_path)

    state = json.loads(_state_path(tmp_path, "sess-3").read_text(encoding="utf-8"))
    assert state["ui_touched"] == ["frontend/App.jsx"]


def test_distinct_ui_files_accumulate(tmp_path):
    one = tmp_path / "frontend" / "App.jsx"
    two = tmp_path / "frontend" / "Nav.tsx"
    _touch(one)
    _touch(two)

    flag(_payload("sess-4", str(one)), tmp_path)
    flag(_payload("sess-4", str(two)), tmp_path)

    state = json.loads(_state_path(tmp_path, "sess-4").read_text(encoding="utf-8"))
    assert state["ui_touched"] == ["frontend/App.jsx", "frontend/Nav.tsx"]


def test_separate_sessions_do_not_cross_contaminate(tmp_path):
    one = tmp_path / "frontend" / "App.jsx"
    two = tmp_path / "frontend" / "Nav.tsx"
    _touch(one)
    _touch(two)

    flag(_payload("sess-a", str(one)), tmp_path)
    flag(_payload("sess-b", str(two)), tmp_path)

    state_a = json.loads(_state_path(tmp_path, "sess-a").read_text(encoding="utf-8"))
    state_b = json.loads(_state_path(tmp_path, "sess-b").read_text(encoding="utf-8"))
    assert state_a["ui_touched"] == ["frontend/App.jsx"]
    assert state_b["ui_touched"] == ["frontend/Nav.tsx"]
    assert set(_state_path(tmp_path, "sess-a").parent.iterdir()) == {
        _state_path(tmp_path, "sess-a"),
        _state_path(tmp_path, "sess-b"),
    }


def test_glob_override_changes_what_matches(tmp_path):
    (tmp_path / ".cyclaudes").mkdir()
    (tmp_path / ".cyclaudes" / "ui-globs.txt").write_text(
        "# only python under widgets/ counts as UI in this repo\nwidgets/**/*.py\n",
        encoding="utf-8",
    )

    no_longer_ui = tmp_path / "frontend" / "App.jsx"
    now_ui = tmp_path / "widgets" / "panel" / "view.py"
    _touch(no_longer_ui)
    _touch(now_ui)

    flag(_payload("sess-5", str(no_longer_ui)), tmp_path)
    flag(_payload("sess-5", str(now_ui)), tmp_path)

    state = json.loads(_state_path(tmp_path, "sess-5").read_text(encoding="utf-8"))
    assert state["ui_touched"] == ["widgets/panel/view.py"]


def test_missing_session_id_or_file_path_is_a_noop(tmp_path):
    flag({"tool_input": {"file_path": str(tmp_path / "a.tsx")}}, tmp_path)
    flag({"session_id": "sess-6", "tool_input": {}}, tmp_path)

    assert not (tmp_path / ".cyclaudes").exists()
