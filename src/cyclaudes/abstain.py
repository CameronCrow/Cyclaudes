"""Abstention — the trust boundary.

A check that *cannot be evaluated* must never look like a check that *passed*.
This module supplies the vocabulary for saying "I could not verify this"; the
pytest integration in :mod:`cyclaudes.pytest_plugin` gives that statement its
own outcome, its own line in the terminal summary, and its own exit code.

Abstention is a **normal path**, not an error path. Reach for it freely::

    from cyclaudes import CannotVerify, abstain_on, cannot_verify

    def test_save_button_disabled_on_empty_form():
        win = ...
        if win.is_occluded():
            raise CannotVerify("save button is behind a modal; state is unreadable")
        assert win.state("Save") == "disabled"

Three equivalent spellings, pick whichever reads best at the call site:

* ``raise CannotVerify(reason)`` — explicit.
* ``cannot_verify(reason)`` — reads as prose inside an ``if``/``else``.
* ``with abstain_on(PermissionError, reason=...)`` — converts a lower-level
  failure that means "I couldn't look" into an abstention rather than a fail.

The last one matters for portability: on macOS a missing TCC Accessibility
grant yields an *empty* accessibility tree, which would otherwise read as
"nothing is broken."
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from typing import NoReturn

__all__ = ["EXIT_ABSTAINED", "CannotVerify", "abstain_on", "cannot_verify"]


#: Process exit code for a run whose only non-passing outcomes were abstentions.
#:
#: Deliberately distinct from *both* pytest's ``0`` (all passed) and ``1``
#: (tests failed), and outside pytest's reserved ``0``–``5`` range. An agent
#: checking ``returncode == 0`` will not see success; an agent checking
#: ``returncode == 1`` will not mistake abstention for a real failure.
EXIT_ABSTAINED = 12


class CannotVerify(Exception):
    """Raised by a check that genuinely could not be evaluated.

    This is *not* a failure and *not* a pass. It means the check ran but the
    evidence needed to decide was unavailable — the element was not in the
    tree, the accessibility permission was missing, a modal occluded the
    surface under test, the app never reached the state under test.

    Deliberately **not** a subclass of :class:`AssertionError`: an abstention
    must never be swallowed by code that broadly catches assertion failures,
    nor be counted as one by tooling that keys off that type.

    :param reason: Why the check could not be evaluated. Surfaced verbatim in
        the terminal summary, so write it for a reader who has no other
        context. "could not verify" is useless; "Save button not present in
        the tree — window may not have finished loading" is actionable.
    :param cause: The lower-level exception that triggered the abstention, if
        one did. Set automatically by :func:`abstain_on`.
    """

    def __init__(self, reason: str, *, cause: BaseException | None = None) -> None:
        reason = str(reason).strip() or "no reason given"
        super().__init__(reason)
        self.reason = reason
        self.cause = cause

    def __str__(self) -> str:
        return self.reason


def cannot_verify(reason: str) -> NoReturn:
    """Abstain from the current check. Never returns.

    Sugar for ``raise CannotVerify(reason)``, so abstaining is as cheap to
    write as asserting::

        if "Save" not in win.names():
            cannot_verify("Save button absent from the tree; nothing to assert on")
    """
    raise CannotVerify(reason)


@contextlib.contextmanager
def abstain_on(*exc_types: type[BaseException], reason: str) -> Iterator[None]:
    """Convert *exc_types* raised inside the block into a :class:`CannotVerify`.

    For the common shape where a low-level error means "I could not look",
    not "the thing under test is broken"::

        with abstain_on(PermissionError, reason="no accessibility permission"):
            tree = backend.snapshot(window)

    An assertion failure inside the block still fails normally — only the
    listed types are converted — and a :class:`CannotVerify` raised inside
    propagates unchanged rather than being re-wrapped.

    :raises TypeError: if no exception types are given. Catching everything
        would let a genuine failure masquerade as an abstention, which is the
        mirror image of the bug this whole module exists to prevent.
    """
    if not exc_types:
        raise TypeError(
            "abstain_on() requires at least one exception type; blanket "
            "conversion would let real failures hide as abstentions"
        )
    try:
        yield
    except CannotVerify:
        raise
    except exc_types as exc:
        raise CannotVerify(
            f"{reason} ({type(exc).__name__}: {exc})", cause=exc
        ) from exc
