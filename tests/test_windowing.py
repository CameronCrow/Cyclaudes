"""Tests for the enumeration-free window seam (src/cyclaudes/windowing.py) and
its two integration points in ui.py (issue #36).

Three layers:

- The ctypes probes themselves, lightly (Windows-only, and only the
  deterministic cases — an invalid handle is not live; a None handle can't be
  determined). The live desktop's exact window set isn't asserted (it's not
  deterministic in CI).
- ``ui.any_owned_window_visible`` — the launch-gate logic — against a stubbed
  seam, so True/False/None routing is proven without a real desktop.
- ``WindowHandle`` liveness — that an empty scoped read consults the cheap
  probe and maps True→EmptyTree, False→WindowGone, None→enumeration fallback.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field

import pytest

from cyclaudes import ui, windowing


# ---------------------------------------------------------------------------
# The ctypes probes (deterministic cases only)
# ---------------------------------------------------------------------------


def test_window_is_live_none_handle_is_undetermined():
    assert windowing.window_is_live(None) is None
    assert windowing.window_is_live(None, expect_pid=123) is None


@pytest.mark.skipif(sys.platform != "win32", reason="Win32 ctypes probe")
def test_window_is_live_invalid_handle_is_false():
    # 1 is never a real HWND; IsWindow says so without enumerating.
    assert windowing.window_is_live(1) is False


@pytest.mark.skipif(sys.platform != "win32", reason="Win32 ctypes sweep")
def test_visible_window_pids_shape():
    pids = windowing.visible_window_pids()
    assert pids is None or (isinstance(pids, set) and all(isinstance(p, int) and p > 0 for p in pids))


def test_visible_window_pids_off_windows_is_none(monkeypatch):
    monkeypatch.setattr(windowing.sys, "platform", "darwin")
    assert windowing.visible_window_pids() is None


# ---------------------------------------------------------------------------
# any_owned_window_visible — launch-gate routing
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_ownership():
    ui.reset_ownership()
    yield
    ui.reset_ownership()


def test_gate_none_when_sweep_undetermined(monkeypatch):
    monkeypatch.setattr(ui._windowing, "visible_window_pids", lambda: None)
    ui.own(4242)
    assert ui.any_owned_window_visible() is None  # -> caller does the real resolve


def test_gate_false_when_nothing_owned(monkeypatch):
    monkeypatch.setattr(ui._windowing, "visible_window_pids", lambda: {10, 20})
    assert ui.any_owned_window_visible() is False


def test_gate_true_when_owned_pid_is_visible(monkeypatch):
    monkeypatch.setattr(ui._windowing, "visible_window_pids", lambda: {10, 4242})
    monkeypatch.setattr(ui._ancestry, "descendant_pids", lambda pid: set())
    ui.own(4242)
    assert ui.any_owned_window_visible() is True


def test_gate_true_via_descendant(monkeypatch):
    # The re-exec case: the owned PID isn't visible, but its child is.
    monkeypatch.setattr(ui._windowing, "visible_window_pids", lambda: {9001})
    monkeypatch.setattr(ui._ancestry, "descendant_pids", lambda pid: {9001} if pid == 4242 else set())
    ui.own(4242)
    assert ui.any_owned_window_visible() is True


def test_gate_false_when_owned_not_visible(monkeypatch):
    monkeypatch.setattr(ui._windowing, "visible_window_pids", lambda: {10, 20})
    monkeypatch.setattr(ui._ancestry, "descendant_pids", lambda pid: set())
    ui.own(4242)
    assert ui.any_owned_window_visible() is False


# ---------------------------------------------------------------------------
# WindowHandle liveness on an empty scoped read
# ---------------------------------------------------------------------------


@dataclass
class FW:
    id: str
    title: str = "T"
    app: str = "App"
    pid: int = 4321


class FakeTP:
    """Empty scoped reads; windows() controls the enumeration fallback."""

    def __init__(self, wins):
        self._wins = wins

    def windows(self):
        return list(self._wins)

    def elements(self, window_id=None, **kw):
        return []  # always empty -> forces the liveness path


def _handle(monkeypatch, *, hwnd, wins):
    monkeypatch.setattr(ui, "_tp", FakeTP(wins))
    return ui.WindowHandle("w1", app="App", pid=4321, timeout=0.05, poll=0.01, hwnd=hwnd)


def test_empty_read_probe_live_is_empty_tree(monkeypatch):
    monkeypatch.setattr(ui._windowing, "window_is_live", lambda h, expect_pid=None: True)
    h = _handle(monkeypatch, hwnd=1234, wins=[])  # windows() empty, but probe says live
    with pytest.raises(ui.EmptyTree):
        h._snapshot()


def test_empty_read_probe_dead_is_window_gone(monkeypatch):
    monkeypatch.setattr(ui._windowing, "window_is_live", lambda h, expect_pid=None: False)
    h = _handle(monkeypatch, hwnd=1234, wins=[FW("w1")])  # would enumerate-live, but probe says dead
    with pytest.raises(ui.WindowGone):
        h._snapshot()


def test_empty_read_probe_undetermined_falls_back_to_enumeration(monkeypatch):
    monkeypatch.setattr(ui._windowing, "window_is_live", lambda h, expect_pid=None: None)
    # No hwnd, probe undecidable -> enumeration decides: window absent -> WindowGone.
    h = _handle(monkeypatch, hwnd=None, wins=[])
    with pytest.raises(ui.WindowGone):
        h._snapshot()
    # ...and present in the enumeration -> EmptyTree (it's live, just empty).
    h2 = _handle(monkeypatch, hwnd=None, wins=[FW("w1")])
    with pytest.raises(ui.EmptyTree):
        h2._snapshot()
