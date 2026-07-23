"""Phase 2 acceptance test (issue #16) — the phase's payoff.

This is not a re-test of #12 (must-raise), #13 (teardown), #14 (helpers), or
#15 (scratch removal) in isolation — each already has its own unit coverage.
It is the **cohesive** proof that the whole Phase-2 machinery, run as a real
verification suite on a live-looking desktop, honours all four
``planning/PHASE_2.md`` success criteria at once:

1. The suite runs to completion **while other, unowned apps are open, and
   provably touches none of them** — asserted on **PID ownership**
   (``owned_window``/``owned_windows`` only ever resolve/return/act on our
   launched PID, even with unowned windows present), never on "no visible
   damage".
2. A deliberately **abandoned modal** in one check does not wedge the suite:
   the shipped teardown recovers non-destructively and the next check runs
   clean.
3. A run leaves **no residue** — no stray processes, no modified user files,
   no changed app config (the unowned windows are byte-identical afterwards
   and every per-check scratch dir is gone).
4. Attempting to act on an **unowned** window **raises** (``UnownedWindow``),
   naming the offender, rather than proceeding.

Two lanes, deliberately:

* **Fake-driven** (default ``python -m pytest``, green with no desktop). A
  fake desktop holds *our* owned window(s) **plus simulated "Cameron's real
  apps"** (an open log-file Notepad and a Logix Designer with unsaved
  changes — the exact smoke-test hazard). ``TestUnownedWindowsAreUntouchable``
  proves criteria 1 & 4 structurally in-process; ``test_shipped_suite_*``
  runs the **shipped** ``app_session`` fixture as a real multi-check pytest
  suite (via ``pytester``) beside those unowned windows — one check abandons a
  modal — and audits touched-none + no-residue from outside the run, so
  criteria 1, 2 & 3 are proven against the machinery users actually get.
* **Live** (``python -m pytest -m live``, needs a real desktop).
  ``test_live_acceptance_*`` launches real ``mspaint.exe`` through
  ``app_session`` and proves criteria 1 & 4 against a genuine accessibility
  tree amid every window really open on the desktop. (Notepad is unusable
  here — current Win11 Notepad is single-process tabbed and cannot be
  launch-and-owned; finding from #13, see #20 — so mspaint is the honest
  ownable target.) Criteria 2 & 3 are teardown properties proven
  deterministically by the fake suite rather than risked live.
"""

from __future__ import annotations

import json
import os
import subprocess
import textwrap
from dataclasses import dataclass, field

import pytest

from cyclaudes import ui

FAST = dict(timeout=0.25, poll=0.01)

#: PIDs standing in for Cameron's real, already-open apps. Nothing the suite
#: does may ever resolve, act on, or mutate a window belonging to these.
CAMERON_LOG_PID = 9001
CAMERON_LOGIX_PID = 9002


@pytest.fixture(autouse=True)
def _clean_ownership():
    """Isolate the module-level owned-PID set between tests (global state)."""
    ui.reset_ownership()
    yield
    ui.reset_ownership()


# ---------------------------------------------------------------------------
# In-process structural proof of criteria 1 & 4: with Cameron's real apps
# open next to ours, the ownership surface resolves/returns ONLY our PID, and
# any attempt to reach one of his windows RAISES rather than resolving.
# ---------------------------------------------------------------------------


@dataclass
class _W:
    id: str
    title: str
    app: str
    pid: int


@dataclass
class _E:
    id: str
    name: str
    role: str = ""
    raw_role: str = ""
    states: list = field(default_factory=list)
    value: str | None = None


class _Desktop:
    """A minimal touchpoint stand-in: some windows are ours, some are not."""

    def __init__(self, wins, trees):
        self.wins = wins
        self.trees = trees
        self._gen = 0
        self._live: dict[str, dict] = {}
        self.actions: list[tuple] = []

    def windows(self):
        return list(self.wins)

    def elements(self, window_id=None, **kwargs):
        if window_id not in {w.id for w in self.wins}:
            return []
        self._gen += 1
        self._live = {}
        out = []
        for i, spec in enumerate(self.trees.get(window_id, [])):
            eid = f"{window_id}:{self._gen}:{i}"
            self._live[eid] = spec
            out.append(
                _E(eid, spec["name"], spec.get("role", ""), spec.get("raw_role", ""),
                   list(spec.get("states", [])), spec.get("value"))
            )
        return out

    def get_text_content(self, el):
        return self._live[el.id].get("value")

    def click(self, el):
        self.actions.append(("click", self._live[el.id]["name"]))
        return True

    def set_value(self, el, value, replace=False):
        spec = self._live[el.id]
        self.actions.append(("set_value", spec["name"]))
        spec["value"] = value if replace else (spec.get("value") or "") + value
        return True

    def close_window(self, window_id):
        self.actions.append(("close_window", window_id))
        self.wins = [w for w in self.wins if w.id != window_id]
        self.trees.pop(window_id, None)
        return True


class TestUnownedWindowsAreUntouchable:
    """Criteria 1 & 4, structurally, with unowned windows genuinely present."""

    def _desktop(self, monkeypatch, our_pid: int = 5001):
        # Our launched Paint sits between Cameron's open log file and his
        # Logix Designer — both unowned, both must stay untouchable.
        d = _Desktop(
            [
                _W("u:log", "import-2026.log - Notepad", "Notepad", CAMERON_LOG_PID),
                _W("o:paint", "Untitled - Paint", "paint", our_pid),
                _W("u:logix", "MyProject - Logix Designer", "Logix Designer", CAMERON_LOGIX_PID),
            ],
            {
                "u:log": [{"name": "Text Editor", "value": "REAL LOG — DO NOT TOUCH"}],
                "o:paint": [{"name": "Canvas", "value": ""}],
                "u:logix": [{"name": "Ladder", "value": "unsaved ladder logic"}],
            },
        )
        monkeypatch.setattr(ui, "_tp", d)
        return d

    def test_ownership_surface_resolves_and_returns_only_our_pid(self, monkeypatch):
        d = self._desktop(monkeypatch)
        ui.own(5001)
        # Criterion 1: amid three open windows, only ours is ever surfaced.
        assert ui.owned_window(app="paint", **FAST).pid == 5001
        assert [w.pid for w in ui.owned_windows()] == [5001]
        # And nothing our surface returned even mentions his windows.
        assert all(w.pid == 5001 for w in ui.owned_windows())

    def test_reaching_a_cameron_window_raises_and_names_the_offender(self, monkeypatch):
        d = self._desktop(monkeypatch)
        ui.own(5001)
        # Criterion 4: match his window by title -> raise, naming it; never resolve.
        with pytest.raises(ui.UnownedWindow) as exc:
            ui.owned_window(title="import-2026.log - Notepad", **FAST)
        msg = str(exc.value)
        assert "import-2026.log - Notepad" in msg
        assert str(CAMERON_LOG_PID) in msg
        # By PID, and via the reusable guard, the same loud refusal.
        with pytest.raises(ui.UnownedWindow):
            ui.owned_window(pid=CAMERON_LOGIX_PID, **FAST)
        with pytest.raises(ui.UnownedWindow):
            ui.assert_owned(CAMERON_LOG_PID)
        # Not a single action ever reached one of his windows.
        assert d.actions == []

    def test_a_handle_cannot_outlive_its_claim(self, monkeypatch):
        self._desktop(monkeypatch)
        ui.own(5001)
        win = ui.owned_window(app="paint", **FAST)
        win.set_value("Canvas", "ours", replace=True)  # fine while owned
        ui.disown(5001)  # claim lapses mid-check
        # Every further read/action now refuses — criterion 4 for a live handle.
        with pytest.raises(ui.UnownedWindow):
            win.click("Canvas")
        with pytest.raises(ui.UnownedWindow):
            win.read_text("Canvas")

    def test_the_refusal_is_loud_never_an_abstention(self):
        # A refusal to touch someone else's window must fail loudly — it must
        # never be read as "cannot verify / nothing broken".
        assert ui.UnownedWindow not in ui.ABSTENTION_CONDITIONS
        assert ui.NoOwnedWindows not in ui.ABSTENTION_CONDITIONS
        assert not issubclass(ui.UnownedWindow, AssertionError)


# ---------------------------------------------------------------------------
# The shipped suite, end to end (criteria 1, 2 & 3 together): run the REAL
# app_session fixture as a real multi-check pytest suite, beside unowned
# windows, with one check abandoning a modal — then audit, from outside the
# run, that it touched none of the unowned windows and left no residue.
# ---------------------------------------------------------------------------


_SHIPPED_SUITE = textwrap.dedent(
    '''
    """A verification suite run against a fake desktop where Cameron's apps are
    already open. It uses the SHIPPED app_session fixture (launch -> own ->
    attach -> modal-safe teardown -> scratch removal); the fake driver records
    every action tagged with the PID of the window it hit, so "touched none" is
    a structural fact this module can assert, not an eyeballed impression."""
    import json, os, subprocess
    from pathlib import Path
    import pytest
    from cyclaudes import ui, pytest_ui

    LOG_PID, LOGIX_PID = 9001, 9002
    UNOWNED_PIDS = {LOG_PID, LOGIX_PID}

    EVID = Path("evidence.json")
    def record(key, value):
        data = json.loads(EVID.read_text()) if EVID.exists() else {}
        if isinstance(value, list) and isinstance(data.get(key), list):
            data[key] = data[key] + value
        else:
            data[key] = value
        EVID.write_text(json.dumps(data))

    class FakeProc:
        _n = 5000
        def __init__(self):
            FakeProc._n += 1
            self.pid = FakeProc._n
            self.returncode = None
        def poll(self): return self.returncode
        def kill(self):
            if self.returncode is None: self.returncode = -9
        def wait(self, timeout=None): return self.returncode

    class W:
        def __init__(self, id, title, app, pid):
            self.id, self.title, self.app, self.pid = id, title, app, pid
    class E:
        def __init__(self, id, name, role="", raw_role="", states=(), value=None):
            self.id, self.name, self.role, self.raw_role = id, name, role, raw_role
            self.states, self.value = list(states), value

    class FakeDesktop:
        """One driver shared across the whole suite. Cameron's windows are
        seeded open (unowned); our launched windows are added per check."""
        def __init__(self):
            self.user_files = {
                "C:/Users/ccrow/import-2026.log": "REAL LOG — DO NOT TOUCH",
                "MyProject.ACD": "unsaved ladder logic",
            }
            self._unowned = [
                W("u:log", "import-2026.log - Notepad", "Notepad", LOG_PID),
                W("u:logix", "MyProject - Logix Designer", "Logix Designer", LOGIX_PID),
            ]
            self._unowned_trees = {
                "u:log": [E("e-log", "Text Editor", value="REAL LOG — DO NOT TOUCH")],
                "u:logix": [E("e-logix", "Ladder", value="unsaved ladder logic")],
            }
            self._owned = {}
            self._owned_trees = {}
            self._blocked = set()
            self._gen = 0
            self._live = {}
            self.actions = []          # (verb, window_id, pid, name)
            self.launched_pids = []

        # a launch (the fake Popen) registers a new owned window
        def launch(self):
            proc = FakeProc()
            wid = "o:%d" % proc.pid
            self._owned[wid] = W(wid, "Untitled - Paint", "paint", proc.pid)
            self._owned_trees[wid] = [E("canvas-%d" % proc.pid, "Canvas",
                                        "document", "DocumentControl", ["editable"], "")]
            self.launched_pids.append(proc.pid)
            return proc

        def arm_modal(self, pid):
            wid = "o:%d" % pid
            self._blocked.add(wid)
            self._owned_trees[wid] = self._owned_trees.get(wid, []) + [
                E("dlg-%d" % pid, "Paint", "dialog", "", ["modal"]),
                E("dontsave-%d" % pid, "Don't Save", "button", "", ["enabled"]),
                E("save-%d" % pid, "Save", "button", "", ["enabled"]),
            ]

        def _pid_of(self, wid):
            w = self._owned.get(wid) or next((u for u in self._unowned if u.id == wid), None)
            return w.pid if w else None

        def _tree(self, wid):
            if wid in self._owned:
                return self._owned_trees.get(wid, [])
            if any(u.id == wid for u in self._unowned):
                return self._unowned_trees.get(wid, [])
            return []

        def windows(self):
            return list(self._unowned) + list(self._owned.values())

        def elements(self, window_id=None, **k):
            tree = self._tree(window_id)
            if not tree:
                return []
            self._gen += 1
            self._live = {}
            out = []
            for i, el in enumerate(tree):
                eid = "%s#%d#%d" % (window_id, self._gen, i)
                self._live[eid] = (el, window_id)
                out.append(E(eid, el.name, el.role, el.raw_role, el.states, el.value))
            return out

        def get_text_content(self, el):
            spec, wid = self._live[el.id]
            self.actions.append(("read", wid, self._pid_of(wid), spec.name))
            return spec.value

        def set_value(self, el, value, replace=False):
            spec, wid = self._live[el.id]
            self.actions.append(("set_value", wid, self._pid_of(wid), spec.name))
            spec.value = value if replace else (spec.value or "") + value
            return True

        def click(self, el):
            spec, wid = self._live[el.id]
            self.actions.append(("click", wid, self._pid_of(wid), spec.name))
            if spec.name in ("Save", "Save As"):
                raise AssertionError("teardown must never click a destructive Save")
            if spec.name in pytest_ui.NON_DESTRUCTIVE_DISMISS:
                self._blocked.discard(wid)
                self._close(wid)
            return True

        def close_window(self, window_id):
            self.actions.append(("close_window", window_id, self._pid_of(window_id), None))
            if window_id in self._blocked:
                return True          # OK while a modal silently blocks it (the footgun)
            self._close(window_id)
            return True

        def _close(self, wid):
            self._owned.pop(wid, None)
            self._owned_trees.pop(wid, None)

        # -- baselines the audit compares against --
        def unowned_values(self):
            return {wid: [e.value for e in tree]
                    for wid, tree in self._unowned_trees.items()}

    DESKTOP = FakeDesktop()
    BASELINE_UNOWNED = DESKTOP.unowned_values()
    BASELINE_FILES = dict(DESKTOP.user_files)

    @pytest.fixture(autouse=True)
    def _wire(monkeypatch):
        ui.reset_ownership()
        monkeypatch.setattr(ui, "_tp", DESKTOP)
        monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: DESKTOP.launch())
        # #36 launch-gate seam: the fake desktop's PIDs own no real visible
        # window, so None ("do the real resolve") keeps the gate a no-op here.
        monkeypatch.setattr(ui._windowing, "visible_window_pids", lambda: None)
        yield
        ui.reset_ownership()

    SESSION_KW = dict(app="paint", ready_timeout=1.0, timeout=0.25, poll=0.01)

    @pytest.mark.app_session(["paint"], **SESSION_KW)
    def test_check_one_edits_then_abandons_a_modal(app_session):
        # Criterion 1, live in the run: beside Cameron's open apps the
        # ownership surface returns ONLY our PID.
        assert {w.pid for w in ui.owned_windows()} == {app_session.pid}
        assert ui.assert_owned(app_session) == app_session.pid
        record("scratch_dirs", [app_session.scratch_dir])
        record("owned_pids_seen", [app_session.pid])
        # An edit that leaves unsaved changes, then we ABANDON the window with a
        # modal still up. The shipped teardown must recover it non-destructively
        # so the next check is not wedged (criterion 2).
        app_session.set_value("Canvas", "check-one drawing", replace=True)
        DESKTOP.arm_modal(app_session.pid)

    @pytest.mark.app_session(["paint"], **SESSION_KW)
    def test_check_two_runs_clean_proving_not_wedged(app_session):
        # If the abandoned modal above had wedged the suite, this check could
        # not have launched/attached at all.
        assert {w.pid for w in ui.owned_windows()} == {app_session.pid}
        record("scratch_dirs", [app_session.scratch_dir])
        record("owned_pids_seen", [app_session.pid])
        app_session.set_value("Canvas", "check-two", replace=True)
        app_session.assert_text("Canvas", "check-two")

    def test_zzz_audit_touched_none_and_left_no_residue():
        # This runs last: both checks (and their teardowns) are done.
        # Criterion 1/3: not one action ever landed on a window we did not own.
        offending = [a for a in DESKTOP.actions if a[2] in UNOWNED_PIDS]
        assert offending == [], "actions touched unowned windows: %r" % offending
        # Cameron's windows are all still open and byte-identical.
        assert {w.pid for w in DESKTOP.windows()} >= UNOWNED_PIDS
        assert DESKTOP.unowned_values() == BASELINE_UNOWNED
        assert DESKTOP.user_files == BASELINE_FILES
        # Criterion 3: no stray windows we launched remain open.
        assert DESKTOP._owned == {}
        # Criterion 3: every per-check scratch dir was removed.
        scratch = json.loads(EVID.read_text())["scratch_dirs"]
        assert scratch and all(not os.path.exists(d) for d in scratch)
        record("audit_ran", True)
    '''
)


def test_shipped_suite_runs_beside_unowned_apps_touches_none_no_residue(pytester):
    """Criteria 1, 2 & 3, proven end to end against the SHIPPED fixture."""
    pytester.makepyfile(_SHIPPED_SUITE)
    result = pytester.runpytest()
    # Two checks + the audit all green: the abandoned modal did not wedge the
    # suite (criterion 2), and the in-run audit passed (criteria 1 & 3).
    result.assert_outcomes(passed=3)

    ev = json.loads((pytester.path / "evidence.json").read_text())
    assert ev.get("audit_ran") is True
    # From OUTSIDE the run: our launched PIDs never collided with Cameron's,
    # and every per-check scratch workspace is gone (no residue).
    assert ev["owned_pids_seen"], "no session ever launched — suite was vacuous"
    assert all(pid not in (CAMERON_LOG_PID, CAMERON_LOGIX_PID)
               for pid in ev["owned_pids_seen"])
    for d in ev["scratch_dirs"]:
        assert not os.path.exists(d)


# ---------------------------------------------------------------------------
# Live acceptance (criteria 1 & 4 against a real desktop). Deselected by
# default; run with `pytest -m live`, with a real GUI available. mspaint is the
# ownable target — current Win11 Notepad is single-process tabbed and cannot be
# launch-and-owned (finding from #13; see #20).
# ---------------------------------------------------------------------------


@pytest.mark.live
@pytest.mark.app_session(["mspaint.exe"], app="mspaint", ready_timeout=15.0, timeout=3.0)
def test_live_acceptance_owns_only_ours_and_refuses_unowned(app_session):
    from cyclaudes import ui as live_ui

    our_pid = app_session.pid
    assert our_pid > 0 and live_ui.is_owned(our_pid)

    # Criterion 1, LIVE: amid every window really open on the desktop, the
    # ownership-scoped surface returns ONLY our launched PID.
    owned = live_ui.owned_windows()
    assert owned, "the launched window should be enumerable as owned"
    assert {w.pid for w in owned} == {our_pid}
    assert live_ui.assert_owned(app_session) == our_pid

    # Criterion 4, LIVE: pick a genuinely unowned window off the real desktop
    # and prove the layer refuses to resolve/act on it rather than proceeding.
    others = [w for w in live_ui._tp.windows() if w.pid != our_pid]
    if others:
        with pytest.raises(live_ui.UnownedWindow):
            live_ui.owned_window(pid=others[0].pid, timeout=1.0, poll=0.05)

    # Criteria 2 & 3 (modal recovery, no residue) are teardown properties: the
    # fixture finalizer closes our window and force-kills by PID. They are
    # proven deterministically by the fake suite above rather than risked live.
