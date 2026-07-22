"""Ship the discipline layer into pytest as fixtures (issues #3, #13).

Registered via a second ``pytest11`` entry point, so installing ``cyclaudes``
is the only wiring a check needs — no conftest boilerplate. Two fixtures ship
here:

``window`` (issue #3) resolves an **already-open** window a check names with a
marker and hands back a :class:`~cyclaudes.ui.WindowHandle` in one fixture
argument::

    @pytest.mark.window(app="notepad")
    def test_document_round_trips(window):
        window.set_value("Text Editor", "hi", replace=True)
        window.assert_text("Text Editor", "hi")

``app_session`` (issue #13, Phase 2B) owns the **lifecycle** the ``window``
fixture deliberately leaves out: it launches the marked process, claims its PID
(:func:`cyclaudes.ui.owning`, so the claim is always dropped), waits for the
process's first window, and yields an *owned* handle::

    @pytest.mark.app_session(["notepad.exe"], app="notepad")
    def test_round_trips(app_session):
        app_session.set_value("Text Editor", "hi", replace=True)

Getting from "app is open" to the specific screen under test is ordinary
per-check fixture code, **not** a navigation DSL (explicitly rejected in
``planning/PHASE_2.md``). This fixture only owns launch → attach → teardown.

Teardown is the hard part and the reason this fixture exists. In the live smoke
test ``close_window()`` returned ``OK`` while a save prompt silently blocked the
close (see ``related-work/accessibility-tree-agent-tooling.md``). So teardown
does not trust a close: it builds on :meth:`WindowHandle.close`, which re-reads
the tree and raises :class:`~cyclaudes.ui.ActionNotVerified` when a modal is
still holding the window open; on that signal it dismisses the modal
**non-destructively** (a *Don't Save* / *Discard* choice — never *Save*, which
could write a file) and retries; and as a last resort it **force-kills by PID**.
Because it runs from the fixture's finalizer, it fires even when the check fails
or errors — a wedged run cannot poison later ones.

``app_session`` also owns **scratch workspace isolation** (issue #15, Phase 2C):
the launched process's ``cwd`` is always a fresh directory from
``tempfile.mkdtemp()``, never Cameron's real working directory, and an
app-specific ``scratch_arg=`` marker option can point an app's own profile /
data-dir flag at that same directory for apps that don't just honor ``cwd``.
The scratch directory is removed in the fixture's outermost finalizer — after
the process is confirmed dead, so nothing still has it open — regardless of
whether the check passed, failed, or errored. This is deliberately app-agnostic
(a directory plus optional opaque command-line templates), not a Notepad- or
mspaint-specific mechanism.

``touchpoint`` is imported lazily inside the fixtures, not at module load, so
this plugin (which pytest imports at startup) never drags the driver onto the
startup path for a run that uses no cyclaudes fixture. The abstention wiring
that makes an empty tree / vanished window *abstain* rather than fail lives in
:mod:`cyclaudes.ui`, which registers it the moment it is imported here.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import time

import pytest

#: How long to wait for the launched process's first window before giving up.
#: Apps can be slow to paint their first frame, and a full window enumeration
#: is not cheap on a busy desktop, so this is generous.
DEFAULT_READY_TIMEOUT = 10.0
#: Re-check interval while waiting for that first window, seconds.
DEFAULT_READY_POLL = 0.25
#: How long to wait for a force-killed process to actually die, seconds.
DEFAULT_KILL_TIMEOUT = 5.0
#: Prefix for the per-session scratch directory (issue #15), so a stray one
#: left behind by a hard crash is easy to spot and sweep in a temp dir.
SCRATCH_DIR_PREFIX = "cyclaudes-app_session-"

#: Modal buttons that dismiss a save/discard prompt **without writing anything**
#: to disk. Teardown will only ever click one of these to get a blocking modal
#: out of the way — it must never click *Save* / *Save As*, which could persist
#: junk or clobber a real file. Ordered most- to least-common across platforms.
NON_DESTRUCTIVE_DISMISS = (
    "Don't Save",
    "Don't save",
    "Do&n't Save",
    "Discard",
    "Discard Changes",
    "Delete anyway",
)


class AppSessionError(RuntimeError):
    """A launched session could not be established (never an abstention).

    Raised when the target process dies before a window appears, or no window
    shows up within the ready timeout. Deliberately **not** a ``ui.UIError``
    and **not** registered as an abstention: a launch that never came up is a
    hard setup failure, not a "cannot verify". The fixture's force-kill
    finalizer still runs, so a failed launch leaves no stray process.
    """


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "window(**criteria): resolve one already-open window via ui.window() "
        "and pass the handle to the `window` fixture. Same criteria as "
        "ui.window (app=, title=, title_contains=, pid=, timeout=, poll=). "
        "Phase 1 assumes the app is already running.",
    )
    config.addinivalue_line(
        "markers",
        "app_session(cmd, **opts): launch cmd (str or list, as subprocess.Popen "
        "takes it), own its PID, wait for its first window, and pass the owned "
        "handle to the `app_session` fixture. opts: app=, title=, title_contains= "
        "narrow the owned-window resolve; timeout=, poll= tune the handle; "
        "ready_timeout=, ready_poll= tune the wait for the first window; "
        "scratch_arg= (str or sequence of str, each with a `{scratch}` "
        "placeholder) appends app-specific profile/data-dir flags pointing at "
        "the scratch workspace, beyond the cwd= isolation every session gets.",
    )


@pytest.fixture
def window(request: pytest.FixtureRequest):
    """A resolved :class:`~cyclaudes.ui.WindowHandle` for the marked window.

    Reads the closest ``@pytest.mark.window(...)`` marker and resolves it with
    :func:`cyclaudes.ui.window`. Resolution failures (``WindowNotFound``,
    ``AmbiguousWindow``) propagate unchanged — the layer's refusal to guess is
    the point — and ``EmptyTree`` still surfaces as an abstention, not a pass.
    """
    from . import ui  # lazy: keep touchpoint off the pytest-startup path

    marker = request.node.get_closest_marker("window")
    if marker is None:
        raise pytest.UsageError(
            "the `window` fixture needs a @pytest.mark.window(app=...) marker "
            "naming which already-open window to resolve"
        )
    return ui.window(*marker.args, **marker.kwargs)


# ---------------------------------------------------------------------------
# app_session — launch / attach / modal-safe teardown (issue #13, Phase 2B)
# ---------------------------------------------------------------------------


def _wait_for_first_window(proc, ui, *, criteria, ready_timeout, ready_poll,
                           handle_timeout, handle_poll):
    """Poll until the launched process shows an owned window; resolve and return it.

    Resolution is ownership-scoped (``ui.owned_window(...)`` against the PID
    :func:`ui.owning` just claimed), so a pre-existing unrelated window can
    never be grabbed — the exact smoke-test footgun. Deliberately does **not**
    pass ``pid=proc.pid`` as a match criterion (issue #23): on Windows,
    ``python`` (and any other re-exec'ing launcher — ``.cmd``/``.bat``,
    ``npx``, Java, Electron helpers) resolves through a shim that re-execs the
    real process as a *child*, so the window that appears belongs to a PID
    that is never equal to ``proc.pid`` — an exact ``pid=`` match would refuse
    it forever. Ownership scoping alone is enough: :func:`ui.is_owned` now
    also accepts any PID descending from an owned one via process ancestry, so
    the re-exec'd child's window resolves as ours without naming its PID up
    front. ``WindowNotFound`` / ``EmptyTree`` just mean "not up yet" and are
    retried until the deadline; a process that exits before painting a window,
    or a window that never appears, raises :class:`AppSessionError` so the
    failure is loud (and the fixture's force-kill finalizer still fires).
    """
    deadline = time.monotonic() + ready_timeout
    while True:
        code = proc.poll()
        if code is not None:
            raise AppSessionError(
                f"launched process (pid={proc.pid}) exited (code {code}) before "
                f"a window appeared — nothing to attach to."
            )
        try:
            return ui.owned_window(timeout=handle_timeout, poll=handle_poll, **criteria)
        except (ui.WindowNotFound, ui.EmptyTree):
            pass  # not up yet — keep waiting
        if time.monotonic() >= deadline:
            raise AppSessionError(
                f"launched process (pid={proc.pid}) showed no owned window within "
                f"{ready_timeout:.1f}s (criteria={criteria or 'none — ownership alone'})."
            )
        time.sleep(ready_poll)


def _dismiss_blocking_modal(win, ui) -> bool:
    """Click a non-destructive dismissal on a blocking modal; report if one fired.

    Tries each :data:`NON_DESTRUCTIVE_DISMISS` label in turn against a fresh
    snapshot. Never clicks *Save* — a modal we cannot dismiss without writing is
    left for the force-kill last resort rather than risking a file write. A
    label that is not present raises ``ElementNotFound`` and we move on; any
    other ``UIError`` (window gone, ambiguity, unowned) stops the attempt.
    """
    for label in NON_DESTRUCTIVE_DISMISS:
        try:
            win.click(label)
            return True
        except ui.ElementNotFound:
            continue
        except ui.UIError:
            return False
    return False


def _modal_safe_close(win, ui) -> None:
    """Close the window, surviving a modal that silently blocks the close.

    Builds directly on :meth:`WindowHandle.close`, which raises
    :class:`~cyclaudes.ui.ActionNotVerified` when the window is still there
    after the close (the ``close_window: OK``-while-blocked footgun). On that
    signal — and only then — dismiss the modal non-destructively and retry
    once. Any other ``UIError`` means there is nothing to close (already gone,
    disowned, empty tree); return quietly and let the force-kill guarantee run.
    """
    try:
        win.close()
        return
    except ui.ActionNotVerified:
        pass  # a modal is likely holding the window open — dismiss it below
    except ui.UIError:
        return  # WindowGone / UnownedWindow / EmptyTree: nothing to close
    if _dismiss_blocking_modal(win, ui):
        try:
            win.close()
        except ui.UIError:
            pass  # still stuck — the force-kill last resort will handle it


def _ensure_process_dead(proc, *, kill_timeout=DEFAULT_KILL_TIMEOUT) -> None:
    """Force-kill the process if it is still alive. The no-residue guarantee.

    The last resort behind every teardown path: whatever happened with the
    window, we launched this process so we make certain it is gone. Purely
    defensive — it never raises, so it can run from a finalizer no matter how
    the check or the graceful close ended.
    """
    try:
        if proc.poll() is None:
            proc.kill()
            try:
                proc.wait(timeout=kill_timeout)
            except Exception:
                pass
    except Exception:
        pass


def _teardown_session(proc, win, ui) -> None:
    """Full teardown: modal-safe graceful close, then a hard force-kill guarantee.

    Ordering matters and is deliberate. The graceful close runs *first*, while
    the PID is still owned, so a save prompt is dismissed non-destructively
    rather than lost to a kill. Then the force-kill guarantees no stray process
    regardless of what the graceful path managed. The graceful step is wrapped
    so nothing it does can ever prevent the force-kill from running.
    """
    try:
        if win is not None:
            _modal_safe_close(win, ui)
    except Exception:
        pass  # graceful cleanup is best-effort; the kill below is the guarantee
    _ensure_process_dead(proc)


def _scratch_command(cmd, scratch_arg, scratch_dir: str):
    """Return *cmd* with any ``scratch_arg`` template(s) appended, path filled in.

    ``scratch_arg`` is the escape hatch for apps that don't honor ``cwd=``
    alone — a profile directory switch, a ``--user-data-dir=``-style flag,
    etc. It stays completely app-agnostic: this function knows nothing about
    any particular app, only that it may have been handed a format-string
    template (or several) containing a ``{scratch}`` placeholder. ``None``
    (the default) leaves *cmd* untouched, since ``cwd=`` isolation alone is
    enough for most apps.
    """
    if scratch_arg is None:
        return cmd
    templates = [scratch_arg] if isinstance(scratch_arg, str) else list(scratch_arg)
    extra = [t.format(scratch=scratch_dir) for t in templates]
    if isinstance(cmd, str):
        return " ".join([cmd, *extra])
    return [*cmd, *extra]


@pytest.fixture
def app_session(request: pytest.FixtureRequest):
    """Launch the marked process, yield an owned handle, always tear it down.

    Reads the closest ``@pytest.mark.app_session(cmd, ...)`` marker, launches
    ``cmd``, owns its PID for the lifetime of the check (:func:`ui.owning`, so
    the claim is dropped on teardown even on failure), waits for its first
    window, and yields the owned :class:`~cyclaudes.ui.WindowHandle`.

    Teardown runs from the fixture finalizer, so it fires even when the check
    fails or errors: it closes the window surviving a blocking modal, and
    force-kills the process by PID as a last resort (see module docstring).

    **Scratch workspace isolation** (issue #15): the process is always
    launched with ``cwd=`` a fresh :func:`tempfile.mkdtemp` directory, never
    Cameron's real working directory, so nothing a check does can land on his
    real files. Apps that need more than ``cwd=`` (a profile/data-dir switch)
    can be pointed at the same directory via the marker's ``scratch_arg=``
    option. The scratch directory is removed in the outermost finalizer —
    after the process is confirmed dead, so nothing still has it open —
    whether the check passed, failed, or errored.
    """
    from . import ui  # lazy: keep touchpoint off the pytest-startup path

    marker = request.node.get_closest_marker("app_session")
    if marker is None:
        raise pytest.UsageError(
            "the `app_session` fixture needs a @pytest.mark.app_session(cmd, ...) "
            "marker naming the process to launch"
        )
    if not marker.args:
        raise pytest.UsageError(
            "@pytest.mark.app_session(cmd, ...) needs the command to launch as "
            "its first positional argument (str or list, as subprocess.Popen takes)"
        )

    cmd = marker.args[0]
    opts = dict(marker.kwargs)
    ready_timeout = opts.pop("ready_timeout", DEFAULT_READY_TIMEOUT)
    ready_poll = opts.pop("ready_poll", DEFAULT_READY_POLL)
    handle_timeout = opts.pop("timeout", ui.DEFAULT_TIMEOUT)
    handle_poll = opts.pop("poll", ui.DEFAULT_POLL)
    scratch_arg = opts.pop("scratch_arg", None)
    # Whatever is left narrows the owned-window resolve (app=, title=,
    # title_contains=). pid is always our launched process, never taken here.
    criteria = {k: opts[k] for k in ("app", "title", "title_contains") if k in opts}

    # Never Cameron's real cwd/profile: a fresh scratch directory per session,
    # removed in the outermost finally below no matter how the check ends.
    scratch_dir = tempfile.mkdtemp(prefix=SCRATCH_DIR_PREFIX)
    try:
        launch_cmd = _scratch_command(cmd, scratch_arg, scratch_dir)
        proc = subprocess.Popen(launch_cmd, cwd=scratch_dir)
        try:
            with ui.owning(proc.pid):
                win = _wait_for_first_window(
                    proc,
                    ui,
                    criteria=criteria,
                    ready_timeout=ready_timeout,
                    ready_poll=ready_poll,
                    handle_timeout=handle_timeout,
                    handle_poll=handle_poll,
                )
                win.scratch_dir = scratch_dir  # so a check can target it explicitly
                try:
                    yield win
                finally:
                    # Graceful, non-destructive close while still owned. Any modal
                    # (e.g. an unsaved-changes prompt from edits the check made) is
                    # dismissed here rather than killed through.
                    _modal_safe_close(win, ui)
        finally:
            # The no-residue guarantee, PID-based so it needs no ownership and
            # runs even if the wait above raised before we ever had a window.
            _ensure_process_dead(proc)
    finally:
        # Removed last, after the process is confirmed dead, so nothing still
        # has a file open under it. ignore_errors: best-effort — a locked or
        # already-gone directory must never mask the check's real outcome.
        shutil.rmtree(scratch_dir, ignore_errors=True)
