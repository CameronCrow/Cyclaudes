"""Shared test wiring.

``pytester`` lets the abstention and fixture tests run pytest-inside-pytest,
which is the only honest way to assert on the real outcomes, summaries, exit
codes and shipped-fixture behaviour an agent (or a user's own suite) would
see, rather than on our own bookkeeping.

The discipline-layer fixtures themselves are **not** here — the ``window``
fixture and its marker ship in :mod:`cyclaudes.pytest_ui` (issue #3) via a
pytest entry point, so users get them by installing cyclaudes, and pytester
runs get them the same way. Keeping them out of this conftest is what lets the
fixture tests exercise the *shipped* fixture instead of a local copy.
"""

pytest_plugins = ["pytester"]
