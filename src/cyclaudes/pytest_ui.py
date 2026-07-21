"""Ship the discipline layer into pytest as a fixture (issue #3).

Registered via a second ``pytest11`` entry point, so installing ``cyclaudes``
is the only wiring a check needs — no conftest boilerplate. A check names an
already-open window with a marker and receives a resolved, owned
:class:`~cyclaudes.ui.WindowHandle` in one fixture argument::

    @pytest.mark.window(app="notepad")
    def test_document_round_trips(window):
        window.set_value("Text Editor", "hi", replace=True)
        window.assert_text("Text Editor", "hi")

Deliberately carries **no** app-lifecycle logic: launching, waiting-for-ready
and PID-scoped teardown are Phase 2. Phase 1 assumes the app is already open.

``touchpoint`` is imported lazily inside the fixture, not at module load, so
this plugin (which pytest imports at startup) never drags the driver onto the
startup path for a run that uses no ``window`` fixture. The abstention wiring
that makes an empty tree / vanished window *abstain* rather than fail lives in
:mod:`cyclaudes.ui`, which registers it the moment it is imported here.
"""

from __future__ import annotations

import pytest


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "window(**criteria): resolve one already-open window via ui.window() "
        "and pass the handle to the `window` fixture. Same criteria as "
        "ui.window (app=, title=, title_contains=, pid=, timeout=, poll=). "
        "Phase 1 assumes the app is already running.",
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
