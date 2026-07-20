"""Give :class:`~cyclaudes.abstain.CannotVerify` its own pytest outcome.

Registered automatically via the ``pytest11`` entry point — installing
``cyclaudes`` is the only wiring required. No conftest changes needed.

The design goal is narrow and absolute: **an abstention must be impossible to
misread as success**, on every surface an agent might read.

=====================  ====================================================
Surface                What an abstention looks like
=====================  ====================================================
Progress output        ``A`` (not ``.``, not ``F``, not ``s``)
Verbose output         ``ABSTAINED`` in yellow
Counts line            ``1 abstained`` — its own bucket, never folded into
                       ``passed``; the line renders yellow, never green
Terminal summary       A dedicated ``CANNOT VERIFY`` section listing every
                       abstention with its reason, printed unconditionally
Exit code              ``EXIT_ABSTAINED`` (12) — not 0, not 1
JUnit XML              ``<failure>`` carrying the reason — never
                       ``<skipped>``. Under ``junit_family=xunit1`` also a
                       ``cyclaudes_outcome=abstained`` property (pytest's
                       default ``xunit2`` drops per-testcase properties)
``report.outcome``     ``"failed"``, plus ``cyclaudes_abstained = True`` and
                       ``cyclaudes_outcome`` in ``user_properties``
=====================  ====================================================

Two of those choices are load-bearing and deliberate:

**The underlying ``report.outcome`` is "failed", never "skipped".** Skipping is
conventionally benign — every legacy parser, CI dashboard and summary line
treats a skipped test as "fine, moving on". An abstention is the opposite: it
is the thing that must stop an agent from declaring victory. So the wire
representation fails *safe*: any tool that does not know about Cyclaudes sees a
non-passing test, and only tools that do know about it get the finer
distinction. The cost is that ``--exitfirst``/``--maxfail`` count abstentions
toward their budget; that is the correct direction to err.

**The exit code is its own value.** Non-zero alone would be enough to prevent a
false "verified", but collapsing abstention into ``1`` would tell an agent that
something is *broken* when in fact nothing was *checked* — a different problem
demanding a different response (go look, or make the check verifiable). Real
failures win when both are present: a run with failures exits ``1`` even if it
also abstained, because a known-broken result is the more urgent signal.
"""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING

import pytest

from .abstain import EXIT_ABSTAINED, CannotVerify

if TYPE_CHECKING:  # pragma: no cover - typing only
    from _pytest.terminal import TerminalReporter

#: Stats key / status category. Distinct from "passed", "failed", "skipped",
#: "error", "xfailed" and "xpassed", so it lands in its own bucket everywhere
#: the terminal reporter groups by category.
ABSTAINED = "abstained"

_IS_ABSTENTION = "cyclaudes_abstained"
_REASON = "cyclaudes_abstain_reason"


@dataclasses.dataclass(frozen=True)
class Abstention:
    """One recorded "I could not verify this"."""

    nodeid: str
    when: str  # "setup" | "call" | "teardown"
    reason: str


class AbstentionPlugin:
    """Tracks abstentions for one pytest session and reports them loudly."""

    def __init__(self) -> None:
        self.abstentions: list[Abstention] = []
        #: Failures that are *not* abstentions. Kept separately so a real
        #: failure can outrank an abstention when choosing the exit code.
        self.real_failures = 0

    # -- classification ---------------------------------------------------

    @pytest.hookimpl(wrapper=True)
    def pytest_runtest_makereport(self, item, call):
        report = yield
        excinfo = call.excinfo
        if excinfo is None or not excinfo.errisinstance(CannotVerify):
            return report

        reason = getattr(excinfo.value, "reason", None) or str(excinfo.value)

        # Not "skipped" — see the module docstring. This must remain a
        # non-passing outcome for tools that know nothing about Cyclaudes.
        report.outcome = "failed"
        setattr(report, _IS_ABSTENTION, True)
        setattr(report, _REASON, reason)

        # Machine-readable marker for report consumers. Recorded on the item
        # (the canonical route, which the JUnit writer reads at teardown) and
        # on this report, which is what in-process report consumers see.
        # Guarded because setup/call/teardown can each abstain.
        for target in (item.user_properties, report.user_properties):
            if ("cyclaudes_outcome", ABSTAINED) not in target:
                target.append(("cyclaudes_outcome", ABSTAINED))
            if (f"cyclaudes_reason_{report.when}", reason) not in target:
                target.append((f"cyclaudes_reason_{report.when}", reason))
        return report

    def pytest_report_teststatus(self, report, config):
        if getattr(report, _IS_ABSTENTION, False):
            return ABSTAINED, "A", ("ABSTAINED", {"yellow": True, "bold": True})
        return None

    def pytest_runtest_logreport(self, report) -> None:
        if getattr(report, _IS_ABSTENTION, False):
            self.abstentions.append(
                Abstention(report.nodeid, report.when, getattr(report, _REASON, ""))
            )
        elif report.failed:
            self.real_failures += 1

    # -- reporting --------------------------------------------------------

    def pytest_terminal_summary(self, terminalreporter: TerminalReporter) -> None:
        """Print the abstention section.

        Unconditional — not gated behind ``-r``. A quiet abstention is exactly
        the failure mode this plugin exists to prevent.
        """
        if not self.abstentions:
            return

        count = len(self.abstentions)
        terminalreporter.write_sep(
            "=", f"CANNOT VERIFY ({count})", yellow=True, bold=True
        )
        for item in self.abstentions:
            where = "" if item.when == "call" else f" [during {item.when}]"
            terminalreporter.write_line(
                f"ABSTAINED {item.nodeid}{where}", yellow=True, bold=True
            )
            terminalreporter.write_line(f"    reason: {item.reason}")

        checks = "check" if count == 1 else "checks"
        terminalreporter.write_line("")
        # ASCII only: this is the line that must survive a cp1252 Windows
        # console without turning into mojibake.
        terminalreporter.write_line(
            f"{count} {checks} COULD NOT BE VERIFIED. This run is NOT a pass. "
            "Nothing was confirmed about the behaviour under test.",
            yellow=True,
            bold=True,
        )
        if not self.real_failures:
            terminalreporter.write_line(
                f"Exit code will be {EXIT_ABSTAINED} (abstained), not 0 (verified).",
                yellow=True,
                bold=True,
            )

    # -- exit code --------------------------------------------------------

    @pytest.hookimpl(trylast=True)
    def pytest_sessionfinish(self, session, exitstatus) -> None:
        if not self.abstentions or self.real_failures:
            return
        # Only override the ordinary outcomes. Usage errors, internal errors
        # and user interrupts carry information this must not clobber.
        if int(exitstatus) in (int(pytest.ExitCode.OK), int(pytest.ExitCode.TESTS_FAILED)):
            session.exitstatus = EXIT_ABSTAINED


def pytest_configure(config: pytest.Config) -> None:
    config.pluginmanager.register(AbstentionPlugin(), "cyclaudes-abstention")
