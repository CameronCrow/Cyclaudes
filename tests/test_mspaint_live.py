"""Issue #5/#20: the first-real-UI round trip, driven against mspaint.

This is the mechanism proof — it says nothing about any real target app, only
that the discipline layer's act->verify loop works against a live
accessibility tree. It is marked ``live`` and deselected by default (see
``addopts`` in pyproject); the fake-driven tests in ``test_ui.py`` are what
run in CI. Run it with an interactive desktop via ``pytest -m live``.

Originally this check drove Notepad (``tests/test_notepad_live.py``). Issue
#20: current Windows 11 ``notepad.exe`` is the single-process *tabbed*
Notepad, so PID-scoped ownership (#12) cannot launch-and-own it — a launched
window shares a host process with the user's own open tabs, and
``ui.window(pid=...)`` cannot tell them apart. So this check is migrated onto
``mspaint.exe``, a genuinely ownable multi-process app already used as the
live target elsewhere (``test_app_session.py``, ``test_acceptance_phase2.py``).

Every assertion here is grounded in what mspaint's tree *actually* reports
(captured live 2026-07-23 on Windows 11, the modern ribbon Paint that
``mspaint.exe`` now launches), never guessed:

* Paint's canvas is an image surface, not a text document, so it has no
  ``Text Editor``-equivalent to write-and-read-back. Its ribbon does expose
  editable text fields (e.g. the Zoom box), but that control lives inside a
  *collapsed* combo box — a live probe found it flaky to resolve/act on
  without first expanding the dropdown, and its committed value format
  (``"150"`` vs ``"300%"``) was observed to vary. It was dropped as the
  round-trip target rather than shipped on unverified behavior.
* The Pencil/Eraser toggle buttons are a clean, always-on-screen substitute:
  clicking one is a real, mutually-exclusive tool-selection act, and a fresh
  read-back proves it structurally — the same act->verify shape as Notepad's
  text write. Their observed states, live, are ``checked,pressed`` — the
  *exact* vocabulary named in ``ui.py``'s own docstring as the original
  smoke-test finding for the enum-repr fix (states must read as
  ``"checked"``, never ``"State.CHECKED"``), so this is also that fix's live
  regression guard, same as the Notepad version was.
* Drawing on the canvas (a single click on the "Using <tool> tool on Canvas"
  group) is what actually dirties the document, live-verified to raise
  Paint's real "Do you want to save your work?" modal on close — unlike a
  ribbon-only edit (e.g. Zoom), which was live-verified to close clean with
  no prompt at all.
* Paint's dialog reuses the name "Save" for both its own button and the
  ever-present ribbon Save button — a genuine live-discovered ambiguity, so
  this check never queries bare "Save" (see the comment at its one use
  below). Its non-destructive dismiss button is "Don't save" with a curly
  apostrophe (``’``, not ``'``) — also read straight off the live tree.

The launch/teardown fixture below is deliberately test-local scaffolding, not
a reusable one — PID-scoped ownership and a modal-surviving ``app_session``
fixture are Phase 2. All this needs is a real window and a hard promise to
leave nothing behind.
"""

from __future__ import annotations

import subprocess
import time

import pytest

from cyclaudes import ui

pytestmark = pytest.mark.live

#: mspaint's own curly-apostrophe dismiss label, read straight off the live
#: tree — a straight ``'`` does not match it.
DONT_SAVE = "Don’t save"


def _window_present(pid: int) -> bool:
    import touchpoint as tp

    return any(w.pid == pid for w in tp.windows())


@pytest.fixture
def mspaint():
    """Launch our own mspaint, yield a handle to it, and always tear it down.

    Resolves the window by the launched PID, so a pre-existing unrelated
    mspaint cannot be grabbed. Teardown force-kills the process regardless of
    outcome: we only ever dismiss via *Don't save*, so nothing is ever
    written to disk.
    """
    proc = subprocess.Popen(["mspaint.exe"])
    try:
        # Let the window come up before resolving. A full window enumeration
        # is not cheap on a busy desktop, so we settle once here rather than
        # poll ui.window() (which would pay that cost on every miss). Paint's
        # ribbon UI is heavier than Notepad's, so this gets more headroom
        # than the original Notepad fixture used.
        time.sleep(3.5)
        win = ui.window(app="mspaint", pid=proc.pid, timeout=5.0)
        yield win
    finally:
        proc.kill()
        try:
            proc.wait(timeout=5)
        except Exception:
            pass


def test_tool_selection_round_trips(mspaint):
    # Neither tool is active on a freshly launched canvas.
    mspaint.assert_not_state("Pencil", "checked")
    mspaint.assert_not_state("Eraser", "checked")

    # Act, then verify INDEPENDENTLY from a fresh snapshot — the core
    # act->verify loop.
    mspaint.click("Pencil")
    mspaint.assert_state("Pencil", "checked")
    mspaint.assert_state("Pencil", "pressed")

    # Switch tools, and prove the old selection is truly cleared, not merely
    # added to — the round-trip's overwrite guarantee.
    mspaint.click("Eraser")
    mspaint.assert_state("Eraser", "checked")
    mspaint.assert_not_state("Pencil", "checked")


def test_states_read_as_portable_values_not_enum_reprs(mspaint):
    # The live guard for the enum finding: before the .value fix these came
    # back as "State.CHECKED" and every one of these assertions failed. This
    # is the exact "checked,pressed" vocabulary ui.py's own docstring
    # attributes to the original smoke test.
    mspaint.click("Pencil")
    states = mspaint.states("Pencil")
    assert "checked" in states
    assert "pressed" in states
    assert not any(s.startswith("State.") for s in states), states
    mspaint.assert_state("Pencil", "checked")
    mspaint.assert_not_state("Pencil", "disabled")  # a state it does not have


def test_resolves_the_right_window_among_several(mspaint):
    # Acceptance: a second, unrelated mspaint must not be grabbed. With two
    # open, app= alone is ambiguous and must refuse to guess; the PID
    # disambiguates back to ours.
    other = subprocess.Popen(["mspaint.exe"])
    try:
        time.sleep(4.5)
        with pytest.raises(ui.AmbiguousWindow):
            ui.window(app="mspaint", timeout=2.0)
        mine = ui.window(app="mspaint", pid=mspaint.pid, timeout=2.0)
        assert mine.pid == mspaint.pid
    finally:
        other.kill()
        try:
            other.wait(timeout=5)
        except Exception:
            pass


def test_unsaved_changes_dialog_is_exposed_structurally(mspaint):
    # No modal dialog before we try to close.
    mspaint.assert_gone("Do you want to save your work?", role="dialog")

    # Draw a single mark — a click on the canvas group, live-verified to be
    # what actually dirties a Paint document (a ribbon-only edit like Zoom
    # was live-verified NOT to dirty it, so it would close clean with no
    # prompt at all — not a useful stand-in for this check).
    mspaint.click("Canvas")  # substring-resolves "Using <tool> tool on Canvas"

    # THE footgun: close_window() returns OK while the save prompt blocks it.
    # close() must not trust that return — it re-reads the window list and
    # raises, naming the blocking dialog.
    with pytest.raises(ui.ActionNotVerified) as exc:
        mspaint.close(timeout=5.0)
    msg = str(exc.value)
    assert "still exists" in msg
    assert "mspaint" in msg              # the window itself is named
    assert "save your work" in msg.lower()  # the blocking dialog is named too

    # Assert the dialog STRUCTURALLY, not by screenshot: a modal dialog and
    # named choices, straight off the tree. "Save" is deliberately not
    # queried here — Paint's ribbon has its own always-present "Save" button
    # with the identical name, so an unscoped query for bare "Save" is
    # genuinely ambiguous (live-discovered while writing this check).
    mspaint.assert_state("Do you want to save your work?", "modal", role="dialog")
    mspaint.assert_exists(DONT_SAVE)
    mspaint.assert_exists("Cancel")

    # Dismiss cleanly without saving; the window must actually disappear.
    mspaint.click(DONT_SAVE)
    deadline = time.time() + 5
    while _window_present(mspaint.pid) and time.time() < deadline:
        time.sleep(0.1)
    assert not _window_present(mspaint.pid), "window survived Don't save"
