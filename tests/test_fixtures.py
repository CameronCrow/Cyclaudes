"""Issue #3: the abstention registry, the ui->abstain seam, and the shipped
``window`` fixture.

Three things are proven here:

1. :func:`cyclaudes.abstain.register_abstention_types` refuses to register an
   ``AssertionError`` subclass — that would let an abstention be swallowed by
   ordinary ``except AssertionError`` handling, the mirror of the bug the
   whole module prevents.
2. The seam works end to end: an ``EmptyTree`` / ``WindowGone`` raised from a
   check produces the *abstained* outcome and exit code 12, not a failure.
   Run via ``pytester`` so it is asserted against the real outcome an agent
   sees. This closes the "nothing connects them" gap noted in PLAN_MAIN.
3. The ``window`` fixture, shipped via the entry point, hands a check a handle
   from one fixture argument and refuses to run without a marker.
"""

from __future__ import annotations

import pytest

from cyclaudes import EXIT_ABSTAINED
from cyclaudes import abstain, ui


# --------------------------------------------------------------------------
# 1. The registry guards the trust boundary
# --------------------------------------------------------------------------


def test_ui_conditions_are_registered_on_import():
    types = abstain.abstention_types()
    assert ui.EmptyTree in types
    assert ui.WindowGone in types


def test_registering_is_idempotent():
    before = abstain.abstention_types()
    abstain.register_abstention_types(ui.EmptyTree, ui.EmptyTree)
    assert abstain.abstention_types().count(ui.EmptyTree) == 1
    assert abstain.abstention_types() == before


def test_refuses_to_register_an_assertion_error_subclass():
    # UIAssertionError IS an AssertionError — a real failure, never an
    # abstention. Registering it would let a failed assert masquerade as
    # "could not verify", inverting the trust boundary.
    with pytest.raises(TypeError):
        abstain.register_abstention_types(ui.UIAssertionError)
    assert ui.UIAssertionError not in abstain.abstention_types()


def test_refuses_to_register_a_non_exception():
    with pytest.raises(TypeError):
        abstain.register_abstention_types(str)


# --------------------------------------------------------------------------
# 2. The seam: an unreadable tree abstains, it does not fail
# --------------------------------------------------------------------------


def test_empty_tree_from_a_check_abstains(pytester: pytest.Pytester):
    pytester.makepyfile(
        """
        import cyclaudes.ui as ui
        def test_cannot_see():
            # e.g. a missing macOS TCC grant: the tree is empty, so nothing
            # can be judged. This must abstain, never pass, never plain-fail.
            raise ui.EmptyTree("accessibility tree empty; nothing to verify")
        """
    )
    result = pytester.runpytest()
    result.assert_outcomes()  # neither passed nor failed
    assert result.parseoutcomes()["abstained"] == 1
    assert result.ret == EXIT_ABSTAINED
    result.stdout.fnmatch_lines(["*CANNOT VERIFY*", "*nothing to verify*"])


def test_window_gone_from_a_check_abstains(pytester: pytest.Pytester):
    pytester.makepyfile(
        """
        import cyclaudes.ui as ui
        def test_window_vanished():
            raise ui.WindowGone("the window closed mid-check")
        """
    )
    result = pytester.runpytest()
    assert result.parseoutcomes()["abstained"] == 1
    assert result.ret == EXIT_ABSTAINED


def test_a_ui_assertion_failure_still_fails(pytester: pytest.Pytester):
    """The mirror check: a real UI assertion failure must NOT be swallowed as
    an abstention just because it lives in the same exception hierarchy."""
    pytester.makepyfile(
        """
        import cyclaudes.ui as ui
        def test_a_real_failure():
            raise ui.UIAssertionError("expected 'Saved', tree showed 'Modified'")
        """
    )
    result = pytester.runpytest()
    result.assert_outcomes(failed=1)
    assert result.ret == int(pytest.ExitCode.TESTS_FAILED)
    assert "CANNOT VERIFY" not in result.stdout.str()


# --------------------------------------------------------------------------
# 3. The shipped `window` fixture
# --------------------------------------------------------------------------


def test_window_fixture_needs_a_marker(pytester: pytest.Pytester):
    pytester.makepyfile(
        """
        def test_no_marker(window):
            assert False, "must never run — fixture setup should error first"
        """
    )
    result = pytester.runpytest()
    assert result.ret != 0
    result.stdout.fnmatch_lines(["*needs a @pytest.mark.window*"])


def test_window_fixture_resolves_the_marked_window(pytester: pytest.Pytester):
    # Exercises the *shipped* fixture (it comes in via the entry point, not a
    # local copy) against a fake driver, proving one fixture argument yields a
    # working handle without any conftest boilerplate.
    pytester.makepyfile(
        """
        import pytest
        from cyclaudes import ui

        class _W:
            id, title, app, pid = "w:1", "Untitled - Notepad", "notepad", 4242
        class _E:
            id, name, role, raw_role = "e1", "Save", "button", "button"
            states, value = ["enabled"], None
        class _FakeTP:
            def windows(self): return [_W()]
            def elements(self, window_id=None, **k):
                return [_E()] if window_id == "w:1" else []

        @pytest.fixture(autouse=True)
        def _fake(monkeypatch):
            monkeypatch.setattr(ui, "_tp", _FakeTP())

        @pytest.mark.window(app="notepad")
        def test_gets_a_working_handle(window):
            assert window.pid == 4242
            assert window.exists("Save")
        """
    )
    result = pytester.runpytest()
    result.assert_outcomes(passed=1)
