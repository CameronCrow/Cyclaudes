"""Issue #5: the Notepad round-trip, as the first check driven against real UI.

This is the mechanism proof — it says nothing about any real target app, only
that the discipline layer's act->verify loop works against a live accessibility
tree. It is marked ``live`` and deselected by default (see ``addopts`` in
pyproject); the fake-driven tests in ``test_ui.py`` are what run in CI. Run it
with an interactive desktop via ``pytest -m live``.

Every assertion here is grounded in what Notepad's tree *actually* reports
(captured 2026-07-20 on Windows 11 classic Notepad), never guessed — which is
also why it is a live regression guard for both live-UI findings on this issue:

* the enum fix (states must read as ``"editable"``, never ``"State.EDITABLE"``),
* the fast-snapshot fix (a whole round-trip re-snapshots many times and must
  stay quick).

The launch/teardown fixture below is deliberately test-local scaffolding, not a
reusable one — PID-scoped ownership and a modal-surviving ``app_session``
fixture are Phase 2. All this needs is a real window and a hard promise to
leave nothing behind.
"""

from __future__ import annotations

import subprocess
import time

import pytest

from cyclaudes import ui

pytestmark = pytest.mark.live


def _window_present(pid: int) -> bool:
    import touchpoint as tp

    return any(w.pid == pid for w in tp.windows())


@pytest.fixture
def notepad():
    """Launch our own Notepad, yield a handle to it, and always tear it down.

    Resolves the window by the launched PID, so a pre-existing unrelated
    Notepad cannot be grabbed. Teardown force-kills the process: we only ever
    dismiss via *Don't Save*, so nothing is ever written to disk.
    """
    proc = subprocess.Popen(["notepad.exe"])
    try:
        # Let the window come up before resolving. A full window enumeration
        # is not cheap on a busy desktop, so we settle once here rather than
        # poll ui.window() (which would pay that cost on every miss).
        time.sleep(2.5)
        win = ui.window(app="notepad", pid=proc.pid, timeout=2.0)
        yield win
    finally:
        proc.kill()
        try:
            proc.wait(timeout=5)
        except Exception:
            pass


def test_document_text_round_trips(notepad):
    # Write, then read back INDEPENDENTLY from a fresh snapshot and assert —
    # the core act->verify loop. set_value already re-verifies internally;
    # read_text proves it again through a different path.
    notepad.set_value("Text Editor", "hello from cyclaudes", replace=True)
    assert notepad.read_text("Text Editor") == "hello from cyclaudes"
    notepad.assert_text("Text Editor", "hello from cyclaudes")

    # Overwrite, and prove the old text is truly gone, not appended.
    notepad.set_value("Text Editor", "second draft", replace=True)
    assert notepad.read_text("Text Editor") == "second draft"


def test_states_read_as_portable_values_not_enum_reprs(notepad):
    # The live guard for the enum finding: before the .value fix these came
    # back as "State.EDITABLE" and every one of these assertions failed.
    states = notepad.states("Text Editor")
    assert "editable" in states
    assert "multi_line" in states
    assert not any(s.startswith("State.") for s in states), states
    notepad.assert_state("Text Editor", "editable")
    notepad.assert_not_state("Text Editor", "checked")  # a state it does not have


def test_resolves_the_right_window_among_several(notepad):
    # Acceptance: a second, unrelated Notepad must not be grabbed. With two
    # open, app= alone is ambiguous and must refuse to guess; the PID
    # disambiguates back to ours.
    other = subprocess.Popen(["notepad.exe"])
    try:
        time.sleep(2.0)
        with pytest.raises(ui.AmbiguousWindow):
            ui.window(app="notepad", timeout=2.0)
        mine = ui.window(app="notepad", pid=notepad.pid, timeout=2.0)
        assert mine.pid == notepad.pid
    finally:
        other.kill()
        try:
            other.wait(timeout=5)
        except Exception:
            pass


def test_unsaved_changes_dialog_is_exposed_structurally(notepad):
    notepad.set_value("Text Editor", "unsaved work", replace=True)
    # No modal dialog before we try to close.
    notepad.assert_gone("Notepad", role="dialog")

    # THE footgun: close_window() returns OK while the save prompt blocks it.
    # close() must not trust that return — it re-reads the window list and
    # raises, naming the blocking dialog.
    with pytest.raises(ui.ActionNotVerified) as exc:
        notepad.close(timeout=3.0)
    msg = str(exc.value)
    assert "still exists" in msg
    assert "Notepad" in msg  # the modal dialog is named in the diagnostic

    # Assert the dialog STRUCTURALLY, not by screenshot: a modal dialog and
    # its three named choices, straight off the tree.
    notepad.assert_state("Notepad", "modal", role="dialog")
    notepad.assert_exists("Save")
    notepad.assert_exists("Don't Save")
    notepad.assert_exists("Cancel")

    # Dismiss cleanly without saving; the window must actually disappear.
    notepad.click("Don't Save")
    deadline = time.time() + 5
    while _window_present(notepad.pid) and time.time() < deadline:
        time.sleep(0.1)
    assert not _window_present(notepad.pid), "window survived Don't Save"
