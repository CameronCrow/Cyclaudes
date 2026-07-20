"""Shared test wiring.

Deliberately minimal — the discipline-layer fixtures land here separately
(see issue #3). The abstention plugin itself needs *no* conftest wiring; it
registers through the ``pytest11`` entry point in ``pyproject.toml``.

``pytester`` lets the abstention tests run pytest-inside-pytest, which is the
only honest way to assert on real outcomes, summaries and exit codes rather
than on our own bookkeeping.
"""

pytest_plugins = ["pytester"]
