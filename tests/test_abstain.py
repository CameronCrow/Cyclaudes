"""Unit tests for the abstention vocabulary itself.

The pytest-outcome behaviour is tested separately in ``test_abstain_outcome.py``.
"""

from __future__ import annotations

import pytest

import cyclaudes
from cyclaudes import EXIT_ABSTAINED, CannotVerify, abstain_on, cannot_verify


def test_exported_from_package_root():
    assert cyclaudes.CannotVerify is CannotVerify
    assert cyclaudes.cannot_verify is cannot_verify
    assert cyclaudes.abstain_on is abstain_on


def test_reason_is_preserved_and_stringifies():
    exc = CannotVerify("Save button absent from the tree")
    assert exc.reason == "Save button absent from the tree"
    assert str(exc) == "Save button absent from the tree"
    assert exc.cause is None


def test_blank_reason_still_says_something():
    assert CannotVerify("   ").reason == "no reason given"


def test_is_not_an_assertion_error():
    """An abstention must never be caught by code handling failed assertions."""
    assert not issubclass(CannotVerify, AssertionError)
    with pytest.raises(CannotVerify):
        try:
            raise CannotVerify("nope")
        except AssertionError:  # pragma: no cover - must not be taken
            pytest.fail("CannotVerify was caught as an AssertionError")


def test_cannot_verify_helper_raises():
    with pytest.raises(CannotVerify) as excinfo:
        cannot_verify("tree was empty")
    assert excinfo.value.reason == "tree was empty"


def test_abstain_on_converts_listed_exception():
    with pytest.raises(CannotVerify) as excinfo:
        with abstain_on(PermissionError, reason="no accessibility permission"):
            raise PermissionError("TCC denied")
    exc = excinfo.value
    assert "no accessibility permission" in exc.reason
    assert "PermissionError" in exc.reason
    assert "TCC denied" in exc.reason
    assert isinstance(exc.cause, PermissionError)


def test_abstain_on_leaves_other_exceptions_alone():
    """A real failure inside the block must stay a real failure."""
    with pytest.raises(AssertionError):
        with abstain_on(PermissionError, reason="no permission"):
            raise AssertionError("the button was enabled")


def test_abstain_on_does_not_rewrap_an_abstention():
    with pytest.raises(CannotVerify) as excinfo:
        with abstain_on(Exception, reason="outer"):
            cannot_verify("inner reason")
    assert excinfo.value.reason == "inner reason"


def test_abstain_on_refuses_to_catch_everything():
    """Blanket conversion would let real failures hide as abstentions."""
    with pytest.raises(TypeError):
        with abstain_on(reason="anything at all"):
            pass


def test_abstain_on_passes_through_when_nothing_raises():
    with abstain_on(PermissionError, reason="unused"):
        value = 1
    assert value == 1


def test_exit_code_is_distinct_from_pytest_conventions():
    """Not 0 (passed), not 1 (failed), and outside pytest's reserved 0-5."""
    assert EXIT_ABSTAINED not in {int(code) for code in pytest.ExitCode}
    assert EXIT_ABSTAINED != 0
