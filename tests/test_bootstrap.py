"""Tests for the SessionStart engine bootstrap (hooks/bootstrap.py).

Loaded by file path — it's a standalone stdlib-only hook script, like the others.
pip is never actually run: ``subprocess.run`` is stubbed, and the tests assert on
*whether and how* it would be invoked (idempotent no-op when present, correct
target/interpreter otherwise, and fail-safe on error).
"""

from __future__ import annotations

import importlib.util
import json
import pathlib

import pytest

_HOOK_PATH = pathlib.Path(__file__).resolve().parents[1] / "hooks" / "bootstrap.py"
_spec = importlib.util.spec_from_file_location("cyclaudes_bootstrap", _HOOK_PATH)
bootstrap = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bootstrap)


def _make_plugin(tmp_path, *, version="0.1.0", pyproject=True):
    (tmp_path / ".claude-plugin").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "cyclaudes", "version": version}), encoding="utf-8"
    )
    if pyproject:
        (tmp_path / "pyproject.toml").write_text("[project]\nname='cyclaudes'\n", encoding="utf-8")
    return tmp_path


# --- plugin metadata -------------------------------------------------------


def test_plugin_version_reads_manifest(tmp_path):
    root = _make_plugin(tmp_path, version="1.2.3")
    assert bootstrap.plugin_version(root) == "1.2.3"


def test_plugin_version_missing_is_none(tmp_path):
    assert bootstrap.plugin_version(tmp_path) is None  # no manifest


# --- pip target: bundled source vs published fallback ----------------------


def test_pip_target_prefers_bundled_source(tmp_path):
    root = _make_plugin(tmp_path, pyproject=True)
    assert bootstrap.pip_target(root) == str(root)


def test_pip_target_falls_back_to_repo_url(tmp_path):
    root = _make_plugin(tmp_path, pyproject=False)
    assert bootstrap.pip_target(root) == bootstrap.REPO_URL


def test_pip_command_targets_this_interpreter(tmp_path, monkeypatch):
    import sys

    root = _make_plugin(tmp_path)
    cmd = bootstrap.pip_command(root)
    assert cmd[:3] == [sys.executable, "-m", "pip"] and "install" in cmd
    assert cmd[-1] == str(root)


def test_pip_command_uses_user_site_only_outside_venv(tmp_path, monkeypatch):
    root = _make_plugin(tmp_path)
    monkeypatch.setattr(bootstrap.sys, "prefix", "/x")
    monkeypatch.setattr(bootstrap.sys, "base_prefix", "/x")  # not a venv
    assert "--user" in bootstrap.pip_command(root)
    monkeypatch.setattr(bootstrap.sys, "base_prefix", "/y")  # venv: prefix != base_prefix
    assert "--user" not in bootstrap.pip_command(root)


# --- installed_ok: import + version gate -----------------------------------


def test_installed_ok_false_when_a_package_missing(monkeypatch):
    monkeypatch.setattr(
        bootstrap.importlib.util, "find_spec",
        lambda m: None if m == "touchpoint" else object(),
    )
    assert bootstrap.installed_ok("0.1.0") is False


def test_installed_ok_true_when_present_and_version_matches(monkeypatch):
    monkeypatch.setattr(bootstrap.importlib.util, "find_spec", lambda m: object())
    import importlib.metadata as md

    monkeypatch.setattr(md, "version", lambda name: "0.1.0")
    assert bootstrap.installed_ok("0.1.0") is True


def test_installed_ok_false_on_version_mismatch(monkeypatch):
    monkeypatch.setattr(bootstrap.importlib.util, "find_spec", lambda m: object())
    import importlib.metadata as md

    monkeypatch.setattr(md, "version", lambda name: "0.0.1")
    assert bootstrap.installed_ok("0.1.0") is False  # stale -> reinstall


# --- main(): idempotency, install, fail-safe -------------------------------


def test_main_noops_when_already_installed(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(bootstrap, "plugin_root", lambda: _make_plugin(tmp_path))
    monkeypatch.setattr(bootstrap, "installed_ok", lambda v: True)
    called = []
    monkeypatch.setattr(bootstrap.subprocess, "run", lambda *a, **k: called.append(a))
    assert bootstrap.main() == 0
    assert called == []              # pip NOT invoked
    assert capsys.readouterr().out == ""  # silent fast path


def test_main_installs_when_missing(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(bootstrap, "plugin_root", lambda: _make_plugin(tmp_path))
    monkeypatch.setattr(bootstrap, "installed_ok", lambda v: False)
    calls = []
    monkeypatch.setattr(bootstrap.subprocess, "run", lambda cmd, **k: calls.append(cmd))
    assert bootstrap.main() == 0
    assert calls and calls[0][:2] == [bootstrap.sys.executable, "-m"]
    assert "installed" in capsys.readouterr().out


def test_main_is_fail_safe_on_pip_error(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(bootstrap, "plugin_root", lambda: _make_plugin(tmp_path))
    monkeypatch.setattr(bootstrap, "installed_ok", lambda v: False)

    def _boom(*a, **k):
        raise RuntimeError("pip exploded")

    monkeypatch.setattr(bootstrap.subprocess, "run", _boom)
    assert bootstrap.main() == 0                      # session never breaks
    assert "could not auto-install" in capsys.readouterr().out
