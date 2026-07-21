"""Tests for the ``app_session`` fixture (issue #13, Phase 2B).

The fixture owns the lifecycle the ``window`` fixture leaves out: launch → own
PID → wait for the first window → yield an owned handle → **modal-safe teardown**.
Teardown is the whole point, so it is what these tests hammer:

* fake-driven unit tests drive the teardown helpers against a fake touchpoint +
  fake process and prove the escalation — clean close, dismiss a blocking modal
  non-destructively (never clicking *Save*), and force-kill by PID as the last
  resort;
* ``pytester`` runs the *shipped* fixture end to end and proves its finalizer
  fires whether the check passes, fails, or errors — the property that keeps a
  wedged run from poisoning the next one.

The live test at the bottom (deselected by default, like ``test_notepad_live``)
launches real Notepad through ``app_session`` and leans on teardown to clean up.
"""

from __future__ import annotations

import subprocess
import textwrap
from dataclasses import dataclass, field

import pytest

from cyclaudes import pytest_ui, ui

FAST = dict(timeout=0.25, poll=0.01)


# ---------------------------------------------------------------------------
# Fakes: a process and a touchpoint whose window is tied to that process, so
# closing the window exits the process (as a single-window app does).
# ---------------------------------------------------------------------------


class FakeProc:
    """A subprocess.Popen stand-in: pid, poll/kill/wait, plus a natural exit."""

    def __init__(self, pid: int = 4242):
        self.pid = pid
        self.returncode: int | None = None
        self.killed = False

    def poll(self):
        return self.returncode

    def kill(self):
        self.killed = True
        if self.returncode is None:
            self.returncode = -9

    def wait(self, timeout=None):
        return self.returncode

    def _exit(self, code: int = 0):
        if self.returncode is None:
            self.returncode = code


@dataclass
class FW:
    id: str
    title: str
    app: str
    pid: int


@dataclass
class FE:
    id: str
    name: str
    role: str = ""
    raw_role: str = ""
    states: list = field(default_factory=list)
    value: str | None = None


class FakeDriver:
    """Touchpoint stand-in with one window bound to ``proc``.

    ``close_window`` can be made to *lie* (return OK while a modal keeps the
    window open) — the live footgun. A non-destructive dismissal click clears
    the modal and closes the app; clicking a *Save* button hard-errors, because
    teardown must never risk writing a file.
    """

    def __init__(self, proc: FakeProc, *, app: str = "notepad",
                 title: str = "Untitled - Notepad"):
        self.proc = proc
        self.wins = [FW(id="w:1", title=title, app=app, pid=proc.pid)]
        self.trees: dict[str, list[dict]] = {
            "w:1": [
                {"name": "Text Editor", "role": "document",
                 "raw_role": "DocumentControl", "states": ["editable"], "value": ""},
            ]
        }
        self.close_blocked_by_modal = False
        self.actions: list[tuple] = []
        self._gen = 0
        self._live: dict[str, dict] = {}

    # -- driver surface used by ui.py --

    def windows(self):
        return list(self.wins)

    def elements(self, window_id=None, **kwargs):
        if window_id not in {w.id for w in self.wins}:
            return []  # a scoped read on a gone window is empty (matches live)
        self._gen += 1
        self._live = {}
        out = []
        for i, spec in enumerate(self.trees.get(window_id, [])):
            eid = f"e{self._gen * 100 + i}"
            self._live[eid] = spec
            out.append(FE(id=eid, name=spec.get("name", ""), role=spec.get("role", ""),
                          raw_role=spec.get("raw_role", ""),
                          states=list(spec.get("states", [])), value=spec.get("value")))
        return out

    def get_text_content(self, el):
        return self._live[el.id].get("value")

    def set_value(self, el, value, replace=False):
        spec = self._live[el.id]
        self.actions.append(("set_value", spec.get("name"), value))
        spec["value"] = value if replace else (spec.get("value") or "") + value
        return True

    def click(self, el):
        spec = self._live[el.id]
        name = spec.get("name")
        self.actions.append(("click", name))
        if name in ("Save", "Save As"):
            raise AssertionError(
                "teardown clicked a destructive Save button — it must only ever "
                "dismiss a modal non-destructively"
            )
        if name in pytest_ui.NON_DESTRUCTIVE_DISMISS:
            self.close_blocked_by_modal = False
            self._close_all()
        return True

    def close_window(self, window_id):
        self.actions.append(("close_window", window_id))
        if self.close_blocked_by_modal:
            return True  # OK while a modal silently blocks it (the footgun)
        self._close_all()
        return True

    def _close_all(self):
        self.wins = []
        self.trees.clear()
        self.proc._exit(0)  # closing the sole window exits the app process

    # -- test helper --

    def add_save_modal(self, *, dismissable: bool = True):
        """Make close lie, and put a Notepad-style save prompt in the tree."""
        self.close_blocked_by_modal = True
        dlg = [{"name": "Notepad", "role": "dialog", "states": ["modal"]}]
        if dismissable:
            dlg.append({"name": "Don't Save", "role": "button", "states": ["enabled"]})
        dlg += [
            {"name": "Save", "role": "button", "states": ["enabled"]},
            {"name": "Cancel", "role": "button", "states": ["enabled"]},
        ]
        self.trees["w:1"] = self.trees.get("w:1", []) + dlg


@pytest.fixture(autouse=True)
def _clean_ownership():
    """Isolate the module-level owned-PID set between tests (global state)."""
    ui.reset_ownership()
    yield
    ui.reset_ownership()


@pytest.fixture()
def session(monkeypatch):
    """An owned handle to a fake single-window app, plus its proc and driver."""
    proc = FakeProc()
    drv = FakeDriver(proc)
    monkeypatch.setattr(ui, "_tp", drv)
    ui.own(proc.pid)
    win = ui.owned_window(pid=proc.pid, **FAST)
    return proc, drv, win


# ---------------------------------------------------------------------------
# Teardown escalation: close cleanly, else dismiss non-destructively, else kill
# ---------------------------------------------------------------------------


class TestTeardown:
    def test_clean_close_leaves_no_window_and_never_force_kills(self, session):
        proc, drv, win = session
        pytest_ui._teardown_session(proc, win, ui)
        assert drv.wins == []            # window really gone
        assert proc.returncode == 0      # process exited on its own
        assert not proc.killed           # force-kill was not needed

    def test_blocking_modal_is_dismissed_non_destructively_then_closes(self, session):
        proc, drv, win = session
        drv.add_save_modal(dismissable=True)  # close_window will lie until dismissed

        pytest_ui._teardown_session(proc, win, ui)

        assert ("click", "Don't Save") in drv.actions       # dismissed the modal
        assert not any(a == ("click", "Save") for a in drv.actions)  # never Save
        assert drv.wins == []                               # window then closed
        assert not proc.killed                              # no force-kill needed

    def test_undismissable_modal_escalates_to_force_kill(self, session):
        proc, drv, win = session
        drv.add_save_modal(dismissable=False)  # only Save/Cancel — no clean way out

        pytest_ui._teardown_session(proc, win, ui)

        assert not any(a == ("click", "Save") for a in drv.actions)  # never Save
        assert drv.wins != []          # window never closed (we refused to Save)
        assert proc.killed             # so teardown force-killed by PID
        assert proc.returncode == -9

    def test_force_kill_runs_even_with_no_window(self):
        # The wait-for-window step can raise before a handle ever exists; the
        # kill guarantee must still hold with win=None and the proc alive.
        proc = FakeProc()
        pytest_ui._teardown_session(proc, None, ui)
        assert proc.killed

    def test_modal_safe_close_ignores_an_already_gone_window(self, session):
        proc, drv, win = session
        drv._close_all()  # window vanished before teardown
        pytest_ui._modal_safe_close(win, ui)  # must not raise
        assert drv.wins == []


# ---------------------------------------------------------------------------
# Waiting for the first window: resolve ours by PID, or fail loudly
# ---------------------------------------------------------------------------


class TestWaitForFirstWindow:
    def _wait(self, proc, **kw):
        kw.setdefault("criteria", {})
        kw.setdefault("ready_timeout", 0.3)
        kw.setdefault("ready_poll", 0.01)
        kw.setdefault("handle_timeout", 0.25)
        kw.setdefault("handle_poll", 0.01)
        return pytest_ui._wait_for_first_window(proc, ui, **kw)

    def test_returns_the_owned_handle_for_our_pid(self, monkeypatch):
        proc = FakeProc()
        drv = FakeDriver(proc)
        monkeypatch.setattr(ui, "_tp", drv)
        with ui.owning(proc.pid):
            win = self._wait(proc)
            assert win.pid == proc.pid

    def test_raises_if_process_dies_before_a_window_appears(self, monkeypatch):
        proc = FakeProc()
        drv = FakeDriver(proc)
        drv.wins = []          # nothing ever opens
        proc._exit(1)          # and the process is already dead
        monkeypatch.setattr(ui, "_tp", drv)
        with ui.owning(proc.pid):
            with pytest.raises(pytest_ui.AppSessionError) as exc:
                self._wait(proc)
        assert "exited" in str(exc.value)

    def test_raises_if_no_window_appears_within_the_timeout(self, monkeypatch):
        proc = FakeProc()
        drv = FakeDriver(proc)
        drv.wins = []          # process stays alive but never paints a window
        monkeypatch.setattr(ui, "_tp", drv)
        with ui.owning(proc.pid):
            with pytest.raises(pytest_ui.AppSessionError) as exc:
                self._wait(proc)
        assert "no owned window" in str(exc.value)

    def test_appsession_error_is_not_an_abstention(self):
        # A launch that never came up is a hard setup failure, never a
        # "cannot verify" that could read as a benign pass.
        assert pytest_ui.AppSessionError not in ui.ABSTENTION_CONDITIONS
        assert not issubclass(pytest_ui.AppSessionError, AssertionError)


# ---------------------------------------------------------------------------
# The shipped fixture, end to end via pytester: teardown fires on pass/fail/error
# ---------------------------------------------------------------------------


def _inner_session_module(body: str) -> str:
    """A pytester test module that runs the shipped app_session against fakes.

    The fake process records a teardown event to ``evidence.json`` the moment
    its window is closed or it is killed, so the outer test can prove — from
    outside the run — that the fixture's finalizer executed regardless of how
    the check body ended.
    """
    return textwrap.dedent(
        '''
        import json, subprocess
        from pathlib import Path
        import pytest
        from cyclaudes import ui

        EVID = Path("evidence.json")
        def _record(ev):
            data = json.loads(EVID.read_text()) if EVID.exists() else []
            data.append(ev)
            EVID.write_text(json.dumps(data))

        class FakeProc:
            def __init__(self): self.pid = 4242; self.returncode = None
            def poll(self): return self.returncode
            def kill(self):
                _record("kill")
                if self.returncode is None: self.returncode = -9
            def wait(self, timeout=None): return self.returncode

        class W:
            def __init__(self, pid):
                self.id, self.title, self.app, self.pid = "w:1", "Untitled - Notepad", "notepad", pid
        class E:
            id, name, role, raw_role = "e1", "Text Editor", "document", "DocumentControl"
            states, value = ["editable"], ""

        class FakeDriver:
            def __init__(self, proc): self.proc = proc; self.wins = [W(proc.pid)]
            def windows(self): return list(self.wins)
            def elements(self, window_id=None, **k):
                return [E()] if any(w.id == window_id for w in self.wins) else []
            def get_text_content(self, el): return el.value
            def close_window(self, wid):
                _record("close")
                self.wins = []
                if self.proc.returncode is None: self.proc.returncode = 0

        @pytest.fixture(autouse=True)
        def _fake(monkeypatch):
            ui.reset_ownership()
            proc = FakeProc()
            monkeypatch.setattr(ui, "_tp", FakeDriver(proc))
            monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: proc)
            yield
            ui.reset_ownership()

        @pytest.mark.app_session(["fake"], app="notepad", ready_timeout=1.0, timeout=0.25, poll=0.01)
        def test_body(app_session):
            assert app_session.pid == 4242
        BODY
        '''
    ).replace("BODY", textwrap.indent(textwrap.dedent(body).strip("\n"), "    "))


class TestFixtureLifecycle:
    def test_teardown_fires_on_a_clean_pass(self, pytester):
        pytester.makepyfile(_inner_session_module("pass"))
        result = pytester.runpytest()
        result.assert_outcomes(passed=1)
        assert "close" in (pytester.path / "evidence.json").read_text()

    def test_teardown_fires_when_the_check_fails(self, pytester):
        pytester.makepyfile(
            _inner_session_module('assert False, "intentional check failure"')
        )
        result = pytester.runpytest()
        result.assert_outcomes(failed=1)
        # The finalizer ran despite the failing assertion — no wedged residue.
        assert "close" in (pytester.path / "evidence.json").read_text()

    def test_teardown_fires_when_the_check_errors(self, pytester):
        pytester.makepyfile(
            _inner_session_module('raise RuntimeError("boom mid-check")')
        )
        result = pytester.runpytest()
        result.assert_outcomes(failed=1)
        assert "close" in (pytester.path / "evidence.json").read_text()

    def test_fixture_needs_a_marker(self, pytester):
        pytester.makepyfile(
            """
            def test_no_marker(app_session):
                assert False, "must never run — setup should error first"
            """
        )
        result = pytester.runpytest()
        assert result.ret != 0
        result.stdout.fnmatch_lines(["*needs a @pytest.mark.app_session*"])

    def test_marker_needs_a_command(self, pytester):
        pytester.makepyfile(
            """
            import pytest
            @pytest.mark.app_session()
            def test_no_cmd(app_session):
                assert False, "must never run — setup should error first"
            """
        )
        result = pytester.runpytest()
        assert result.ret != 0
        result.stdout.fnmatch_lines(["*needs the command to launch*"])


# ---------------------------------------------------------------------------
# Live: launch a real app through the fixture and let its teardown clean up.
# Deselected by default (see addopts in pyproject). Run with `pytest -m live`.
#
# Target choice — a Phase-2B finding worth recording. The obvious target,
# `notepad.exe`, does NOT work with PID-scoped ownership on current Windows 11:
# it is the single-process *tabbed* Notepad, so a launched window runs under a
# shared host process, never the PID subprocess.Popen returns. Worse, Cameron's
# own open Notepad tabs live under that same host PID — so ownership genuinely
# cannot tell our tab from his (exactly the "single-instance enforcement" open
# question in planning/PHASE_2.md). The fixture *fails safe* there: it raises
# AppSessionError rather than attaching to a window it cannot prove it launched.
# `mspaint.exe` is a true multi-process app whose window carries the launcher
# PID, so it is the honest live proof that launch -> own -> attach -> teardown
# works against a real accessibility tree. The modal-dismiss / force-kill
# escalation is proven deterministically by the fake-driven tests above.
# ---------------------------------------------------------------------------


@pytest.mark.live
@pytest.mark.app_session(["mspaint.exe"], app="mspaint", ready_timeout=15.0, timeout=3.0)
def test_app_session_launches_owns_and_tears_down(app_session):
    # We were handed an OWNED handle to the process the fixture just launched —
    # PID ownership resolved end to end against a real desktop, not a fake.
    assert app_session.pid > 0
    from cyclaudes import ui

    assert ui.is_owned(app_session.pid)
    # A real read off the live tree proves this is a genuine, attached window.
    assert "paint" in app_session.title().lower()
    # No assertion about teardown here: the fixture finalizer closes the window
    # and force-kills by PID as the guaranteed last resort, leaving no residue.
