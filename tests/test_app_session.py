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
  wedged run from poisoning the next one;
* the same ``pytester`` harness proves scratch workspace isolation (issue #15,
  Phase 2C): the launched process's ``cwd`` is the scratch directory, a write
  during the check lands *inside* it (path containment, not eyeballing), and
  the directory is gone after teardown on pass, fail, **and** error.

The live test at the bottom (deselected by default, like ``test_notepad_live``)
launches real Notepad through ``app_session`` and leans on teardown to clean up.
"""

from __future__ import annotations

import json
import subprocess
import textwrap
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from cyclaudes import ancestry, pytest_ui, ui

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
# Subtree-aware force-kill: reap the re-exec'd child + helper swarm (issue #29)
# ---------------------------------------------------------------------------


class TestDescendantWalk:
    """The pure ``ancestry._descendants_from_table`` walk the force-kill leans on.

    Exercised directly against a fake ``{pid: parent_pid}`` table (no real
    snapshot), so the "who counts as a descendant" logic that decides which
    PIDs the teardown may kill is pinned down in isolation.
    """

    def test_gathers_children_and_grandchildren(self):
        # 100 -> 101 -> 102 -> 103: the re-exec'd child and its helper depth.
        table = {101: 100, 102: 101, 103: 102}
        assert ancestry._descendants_from_table(100, table) == {101, 102, 103}

    def test_excludes_the_root_itself(self):
        # The launched PID is killed through its Popen handle, not this set.
        assert 100 not in ancestry._descendants_from_table(100, {101: 100})

    def test_a_sibling_subtree_is_excluded(self):
        # 200 -> 201 is a separate tree; walking from 100 must never reach it.
        assert ancestry._descendants_from_table(100, {101: 100, 201: 200}) == {101}

    def test_a_cyclic_table_does_not_hang_or_over_collect(self):
        # PID reuse can make a table look cyclic (100 <-> 101); the seen-set
        # must break the loop and still return a finite descendant set.
        assert ancestry._descendants_from_table(100, {101: 100, 100: 101}) == {101}


class TestSubtreeForceKill:
    """Issue #29: the force-kill last resort must reap the whole subtree.

    Mirrors ``test_ui.TestSubtreeOwnership``'s fake-process-tree style. The
    per-PID kill and the descendant lookup are both stubbed, so no real process
    is ever launched or signalled. Where it matters (the negative test), the
    descendant lookup runs the *real* walk over a fake table, so "an unrelated
    PID is not a descendant" is decided by production logic, not the stub.
    """

    def _fake_process_tree(self, monkeypatch, table, killed):
        monkeypatch.setattr(
            pytest_ui.ancestry, "descendant_pids",
            lambda pid: ancestry._descendants_from_table(pid, table),
        )
        monkeypatch.setattr(pytest_ui.os, "kill", lambda pid, sig: killed.add(pid))

    def test_kills_the_launched_pid_and_every_descendant(self, monkeypatch):
        # launched 100 -> re-exec'd child 101 -> grandchild helper 102.
        killed = set()
        self._fake_process_tree(monkeypatch, {101: 100, 102: 101}, killed)
        proc = FakeProc(pid=100)

        pytest_ui._ensure_process_dead(proc)

        assert proc.killed           # the launched PID, via its Popen handle
        assert killed == {101, 102}  # child + grandchild, terminated by PID

    def test_an_unrelated_pid_is_not_killed(self, monkeypatch):
        # 999 is present in the table but roots at un-owned 500, not our 100.
        # This is the most important guarantee: the reach never spills over.
        killed = set()
        self._fake_process_tree(monkeypatch, {101: 100, 999: 500}, killed)
        proc = FakeProc(pid=100)

        pytest_ui._ensure_process_dead(proc)

        assert 101 in killed      # our genuine descendant reaped
        assert 999 not in killed  # the unrelated PID left untouched

    def test_one_kill_that_raises_does_not_stop_the_others(self, monkeypatch):
        # Best-effort per PID: a permission/race failure on one descendant must
        # neither propagate out of the finalizer nor skip the remaining PIDs.
        killed = set()

        def _kill(pid, sig):
            if pid == 101:
                raise PermissionError("access denied")
            killed.add(pid)

        monkeypatch.setattr(
            pytest_ui.ancestry, "descendant_pids", lambda pid: {101, 102, 103})
        monkeypatch.setattr(pytest_ui.os, "kill", _kill)
        proc = FakeProc(pid=100)

        pytest_ui._ensure_process_dead(proc)  # must not raise

        assert proc.killed          # the launched PID still reaped
        assert killed == {102, 103}  # 101 raised; the rest still died

    def test_never_raises_even_if_the_subtree_lookup_raises(self, monkeypatch):
        # If the snapshot itself blows up we still kill the root and stay silent
        # — the finalizer contract is absolute.
        def _boom(pid):
            raise OSError("snapshot failed")

        monkeypatch.setattr(pytest_ui.ancestry, "descendant_pids", _boom)
        proc = FakeProc(pid=100)

        pytest_ui._ensure_process_dead(proc)  # must not raise

        assert proc.killed  # falls back to killing the launched PID alone


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
# _scratch_command: the app-agnostic {scratch} template substitution helper
# behind the marker's scratch_arg= option (issue #15).
# ---------------------------------------------------------------------------


class TestScratchCommand:
    def test_none_leaves_a_list_cmd_untouched(self):
        assert pytest_ui._scratch_command(["fake.exe"], None, "C:/scratch/abc") == ["fake.exe"]

    def test_single_template_is_appended_to_a_list_cmd(self):
        cmd = pytest_ui._scratch_command(
            ["fake.exe"], "--profile-dir={scratch}", "C:/scratch/abc"
        )
        assert cmd == ["fake.exe", "--profile-dir=C:/scratch/abc"]

    def test_multiple_templates_are_all_appended_in_order(self):
        cmd = pytest_ui._scratch_command(
            ["fake.exe"],
            ["--profile-dir={scratch}", "--cache-dir={scratch}/cache"],
            "C:/scratch/abc",
        )
        assert cmd == [
            "fake.exe",
            "--profile-dir=C:/scratch/abc",
            "--cache-dir=C:/scratch/abc/cache",
        ]

    def test_string_cmd_gets_the_template_appended_as_text(self):
        cmd = pytest_ui._scratch_command("fake.exe", "--profile-dir={scratch}", "C:/scratch/abc")
        assert cmd == "fake.exe --profile-dir=C:/scratch/abc"

    def test_caller_supplied_list_is_not_mutated(self):
        cmd = ["fake.exe"]
        pytest_ui._scratch_command(cmd, "--profile-dir={scratch}", "C:/scratch/abc")
        assert cmd == ["fake.exe"]  # the marker's own list must stay untouched


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
# Scratch workspace isolation (issue #15, Phase 2C): a run must be incapable
# of mutating real user data. Proven end to end via the shipped fixture,
# fake Popen/driver, and path-containment assertions — never eyeballing.
# ---------------------------------------------------------------------------


def _inner_scratch_module(outcome: str, *, scratch_arg: str | list[str] | None = None) -> str:
    """A pytester test module that runs the shipped app_session and records
    everything needed to prove scratch isolation from *outside* the run:
    where the fake process was launched (cwd + argv), what ``app_session``
    handed the check as ``.scratch_dir``, and where a file the check writes
    actually lands — all via ``evidence.json``, since the scratch directory
    itself is removed by the finalizer before the outer test gets to look.
    """
    marker_kw = f", scratch_arg={scratch_arg!r}" if scratch_arg is not None else ""
    return textwrap.dedent(
        f'''
        import json, os, subprocess
        from pathlib import Path
        import pytest
        from cyclaudes import ui

        EVID = Path("evidence.json")
        def _record(key, value):
            data = json.loads(EVID.read_text()) if EVID.exists() else {{}}
            data[key] = value
            EVID.write_text(json.dumps(data))

        class FakeProc:
            def __init__(self): self.pid = 4242; self.returncode = None
            def poll(self): return self.returncode
            def kill(self):
                if self.returncode is None: self.returncode = -9
            def wait(self, timeout=None): return self.returncode

        class W:
            def __init__(self, pid):
                self.id, self.title, self.app, self.pid = "w:1", "Untitled", "fakeapp", pid
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
                self.wins = []
                if self.proc.returncode is None: self.proc.returncode = 0

        @pytest.fixture(autouse=True)
        def _fake(monkeypatch):
            ui.reset_ownership()
            proc = FakeProc()
            monkeypatch.setattr(ui, "_tp", FakeDriver(proc))

            def fake_popen(cmd, cwd=None, **kw):
                _record("popen_cmd", cmd)
                _record("popen_cwd", cwd)
                _record("cwd_existed_at_launch", bool(cwd and os.path.isdir(cwd)))
                return proc

            monkeypatch.setattr(subprocess, "Popen", fake_popen)
            yield
            ui.reset_ownership()

        @pytest.mark.app_session(["fake"], app="fakeapp", ready_timeout=1.0, timeout=0.25, poll=0.01{marker_kw})
        def test_body(app_session):
            _record("scratch_dir", app_session.scratch_dir)
            _record("scratch_dir_exists_during_check", os.path.isdir(app_session.scratch_dir))
            target = os.path.join(app_session.scratch_dir, "written-by-check.txt")
            with open(target, "w") as f:
                f.write("scratch data")
            _record("written_file", target)
            {outcome}
        '''
    )


class TestScratchWorkspaceIsolation:
    def _run(self, pytester, outcome, **kw):
        pytester.makepyfile(_inner_scratch_module(outcome, **kw))
        return pytester.runpytest()

    def _evidence(self, pytester) -> dict:
        return json.loads((pytester.path / "evidence.json").read_text())

    def test_process_is_launched_with_cwd_pointed_at_the_scratch_dir(self, pytester):
        result = self._run(pytester, "pass")
        result.assert_outcomes(passed=1)
        ev = self._evidence(pytester)
        # Containment, not eyeballing: the exact cwd Popen received IS the
        # exact path app_session handed the check as .scratch_dir.
        assert ev["popen_cwd"] == ev["scratch_dir"]
        assert ev["cwd_existed_at_launch"] is True

    def test_a_write_during_the_check_lands_under_the_scratch_dir(self, pytester):
        result = self._run(pytester, "pass")
        result.assert_outcomes(passed=1)
        ev = self._evidence(pytester)
        scratch = Path(ev["scratch_dir"]).resolve()
        written = Path(ev["written_file"]).resolve()
        assert written.is_relative_to(scratch)  # containment assertion, not eyeballing
        assert ev["scratch_dir_exists_during_check"] is True

    @pytest.mark.parametrize(
        "outcome",
        ["pass", 'assert False, "intentional check failure"', 'raise RuntimeError("boom")'],
        ids=["pass", "fail", "error"],
    )
    def test_scratch_dir_is_removed_on_teardown_regardless_of_outcome(self, pytester, outcome):
        self._run(pytester, outcome)  # outcome may fail/error the inner test — that's the point
        ev = self._evidence(pytester)
        assert not Path(ev["scratch_dir"]).exists()  # no residue, no matter how the check ended

    def test_scratch_dirs_differ_across_sessions(self, pytester):
        result1 = self._run(pytester, "pass")
        result1.assert_outcomes(passed=1)
        first = self._evidence(pytester)["scratch_dir"]

        (pytester.path / "evidence.json").unlink()
        result2 = self._run(pytester, "pass")
        result2.assert_outcomes(passed=1)
        second = self._evidence(pytester)["scratch_dir"]

        assert first != second  # each session gets its own throwaway directory

    def test_scratch_arg_template_is_substituted_into_the_launch_command(self, pytester):
        result = self._run(pytester, "pass", scratch_arg="--profile-dir={scratch}")
        result.assert_outcomes(passed=1)
        ev = self._evidence(pytester)
        assert ev["popen_cmd"] == ["fake", f"--profile-dir={ev['scratch_dir']}"]

    def test_multiple_scratch_arg_templates_are_all_substituted(self, pytester):
        result = self._run(
            pytester,
            "pass",
            scratch_arg=["--profile-dir={scratch}", "--cache-dir={scratch}"],
        )
        result.assert_outcomes(passed=1)
        ev = self._evidence(pytester)
        scratch = ev["scratch_dir"]
        assert ev["popen_cmd"] == [
            "fake",
            f"--profile-dir={scratch}",
            f"--cache-dir={scratch}",
        ]


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
