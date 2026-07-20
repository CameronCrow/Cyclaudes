"""The trust boundary: abstention must be unmistakable as anything else.

These run pytest inside pytest (via ``pytester``) so every assertion is against
the *real* outcome, summary text and process exit code an agent would see —
not against the plugin's own bookkeeping, which could agree with itself while
being wrong about all three.

The property under test throughout: **an abstention can never be read as
success.** Not by counting, not by exit code, not by parsing output, not by
reading the JUnit XML.
"""

from __future__ import annotations

import re

import pytest

from cyclaudes import EXIT_ABSTAINED

# One file exercising all three outcomes, used by the "distinctness" tests.
THREE_OUTCOMES = """
    from cyclaudes import CannotVerify

    def test_it_passes():
        assert True

    def test_it_fails():
        assert False, "the button was enabled"

    def test_it_abstains():
        raise CannotVerify("Save button absent from the tree")
"""

ONLY_ABSTENTIONS = """
    from cyclaudes import CannotVerify, cannot_verify

    def test_abstains_one():
        raise CannotVerify("no accessibility permission; tree came back empty")

    def test_abstains_two():
        cannot_verify("modal occluded the surface under test")
"""


def _summary_line(result) -> str:
    """The final counts line, e.g. ``== 2 abstained in 0.01s ==``."""
    for line in reversed(result.outlines):
        if re.search(r"=+ .* in [\d.]+s.* =+", line):
            return line
    raise AssertionError(f"no summary line found in:\n{result.stdout.str()}")


# --------------------------------------------------------------------------
# The three outcomes are genuinely distinct
# --------------------------------------------------------------------------


def test_three_outcomes_are_counted_separately(pytester: pytest.Pytester):
    pytester.makepyfile(THREE_OUTCOMES)
    result = pytester.runpytest()

    # Not folded into passed, not folded into failed.
    result.assert_outcomes(passed=1, failed=1)
    assert result.parseoutcomes()["abstained"] == 1
    result.stdout.fnmatch_lines(["*1 failed*1 passed*1 abstained*"])


def test_abstention_has_its_own_progress_letter(pytester: pytest.Pytester):
    pytester.makepyfile(ONLY_ABSTENTIONS)
    result = pytester.runpytest()
    # 'A', not '.' (pass), 'F' (fail) or 's' (skip).
    result.stdout.fnmatch_lines(["*AA*"])


def test_abstention_has_its_own_verbose_word(pytester: pytest.Pytester):
    pytester.makepyfile(THREE_OUTCOMES)
    result = pytester.runpytest("-v")
    result.stdout.fnmatch_lines(["*test_it_abstains ABSTAINED*"])
    # And it is not also announced as any other outcome.
    assert "test_it_abstains PASSED" not in result.stdout.str()
    assert "test_it_abstains FAILED" not in result.stdout.str()
    assert "test_it_abstains SKIPPED" not in result.stdout.str()


def test_abstention_is_not_reported_as_skipped(pytester: pytest.Pytester):
    """Skipping reads as benign. Abstention must never borrow that framing."""
    pytester.makepyfile(ONLY_ABSTENTIONS)
    result = pytester.runpytest("-rs")
    assert "skipped" not in _summary_line(result)


# --------------------------------------------------------------------------
# An all-abstain run is not reportable as success
# --------------------------------------------------------------------------


def test_all_abstain_run_does_not_exit_zero(pytester: pytest.Pytester):
    """The single most important assertion in the suite."""
    pytester.makepyfile(ONLY_ABSTENTIONS)
    result = pytester.runpytest()
    assert result.ret != 0
    assert result.ret == EXIT_ABSTAINED


def test_all_abstain_exit_code_is_distinct_from_failure(pytester: pytest.Pytester):
    """Abstained ('nothing was checked') != failed ('something is broken')."""
    # Distinct filenames and subprocess runs: three independent sessions, so
    # module caching cannot leak one outcome into the next.
    pytester.makepyfile(
        test_only_abstains=ONLY_ABSTENTIONS,
        test_only_fails="def test_fails(): assert False",
        test_only_passes="def test_passes(): assert True",
    )
    abstained = pytester.runpytest_subprocess("test_only_abstains.py")
    failed = pytester.runpytest_subprocess("test_only_fails.py")
    passed = pytester.runpytest_subprocess("test_only_passes.py")

    assert passed.ret == int(pytest.ExitCode.OK)
    assert failed.ret == int(pytest.ExitCode.TESTS_FAILED)
    assert abstained.ret == EXIT_ABSTAINED
    assert len({passed.ret, failed.ret, abstained.ret}) == 3


def test_all_abstain_summary_line_claims_no_passes(pytester: pytest.Pytester):
    """Nothing in the counts line an output-parser could read as green."""
    pytester.makepyfile(ONLY_ABSTENTIONS)
    result = pytester.runpytest()
    line = _summary_line(result)
    assert "2 abstained" in line
    assert "passed" not in line
    assert "no tests ran" not in line


def test_all_abstain_run_prints_a_loud_section(pytester: pytest.Pytester):
    pytester.makepyfile(ONLY_ABSTENTIONS)
    result = pytester.runpytest()
    result.stdout.fnmatch_lines(
        [
            "*CANNOT VERIFY (2)*",
            "*ABSTAINED*test_abstains_one*",
            "*2 checks COULD NOT BE VERIFIED*",
        ]
    )
    assert "This run is NOT a pass" in result.stdout.str()
    # ASCII only, so a cp1252 Windows console cannot mangle the key line.
    section = [line for line in result.outlines if "COULD NOT BE VERIFIED" in line]
    assert section and all(line.isascii() for line in section)


def test_reason_is_surfaced_in_the_summary(pytester: pytest.Pytester):
    pytester.makepyfile(ONLY_ABSTENTIONS)
    result = pytester.runpytest()
    result.stdout.fnmatch_lines(
        [
            "*reason: no accessibility permission; tree came back empty*",
            "*reason: modal occluded the surface under test*",
        ]
    )


def test_summary_section_appears_without_r_flags(pytester: pytest.Pytester):
    """A quiet abstention is the exact failure mode this plugin prevents."""
    pytester.makepyfile(ONLY_ABSTENTIONS)
    for extra in ([], ["-q"], ["-p", "no:cacheprovider"]):
        result = pytester.runpytest(*extra)
        assert "CANNOT VERIFY" in result.stdout.str(), f"missing with {extra}"
        assert result.ret == EXIT_ABSTAINED


# --------------------------------------------------------------------------
# Machine-readable surfaces
# --------------------------------------------------------------------------


def test_junit_xml_marks_abstention_as_non_passing(pytester: pytest.Pytester):
    """A parser that knows nothing about Cyclaudes must still not see a pass."""
    pytester.makepyfile(ONLY_ABSTENTIONS)
    xml_path = pytester.path / "junit.xml"
    result = pytester.runpytest(f"--junit-xml={xml_path}")
    assert result.ret == EXIT_ABSTAINED

    xml = xml_path.read_text(encoding="utf-8")
    assert 'failures="2"' in xml
    assert 'errors="0"' in xml
    assert 'skipped="0"' in xml  # never the benign-looking bucket
    assert xml.count("<failure") == 2
    # The reason travels with it, so the XML is actionable on its own.
    assert "CannotVerify" in xml
    assert "no accessibility permission" in xml


def test_junit_xunit1_carries_the_abstention_marker(pytester: pytest.Pytester):
    """A parser that *does* know Cyclaudes gets the finer distinction.

    ``xunit2`` (pytest's default family) drops per-testcase properties
    entirely, so the marker is only assertable under ``xunit1``. The
    fail-safe ``<failure>`` above is what carries the signal by default.
    """
    pytester.makepyfile(ONLY_ABSTENTIONS)
    xml_path = pytester.path / "junit.xml"
    pytester.runpytest(f"--junit-xml={xml_path}", "-o", "junit_family=xunit1")

    xml = xml_path.read_text(encoding="utf-8")
    assert 'name="cyclaudes_outcome" value="abstained"' in xml
    assert 'name="cyclaudes_reason_call"' in xml
    assert "no accessibility permission" in xml


def test_report_outcome_is_never_passed(pytester: pytest.Pytester):
    """Guard the raw report attribute other plugins and tools key off."""
    pytester.makepyfile(ONLY_ABSTENTIONS)
    reports = []

    class Collector:
        def pytest_runtest_logreport(self, report):
            reports.append(report)

    pytester.runpytest(plugins=[Collector()])
    call_reports = [r for r in reports if r.when == "call"]
    assert call_reports
    for report in call_reports:
        assert report.outcome == "failed"
        assert report.passed is False
        assert report.skipped is False
        assert getattr(report, "cyclaudes_abstained", False) is True
        # Machine-readable marker for any report consumer (JSON reporters,
        # CI plugins) that reads user_properties.
        assert ("cyclaudes_outcome", "abstained") in report.user_properties
        assert any(
            key.startswith("cyclaudes_reason") for key, _ in report.user_properties
        )


# --------------------------------------------------------------------------
# Interaction with real outcomes
# --------------------------------------------------------------------------


def test_real_failures_outrank_abstentions_for_the_exit_code(
    pytester: pytest.Pytester,
):
    """Broken beats unchecked — a known failure is the more urgent signal."""
    pytester.makepyfile(THREE_OUTCOMES)
    result = pytester.runpytest()
    assert result.ret == int(pytest.ExitCode.TESTS_FAILED)
    # The abstention is still reported, just not the headline.
    assert "CANNOT VERIFY" in result.stdout.str()


def test_a_clean_run_is_unaffected(pytester: pytest.Pytester):
    """The plugin must be invisible when nothing abstains."""
    pytester.makepyfile("def test_passes(): assert True")
    result = pytester.runpytest()
    assert result.ret == int(pytest.ExitCode.OK)
    result.assert_outcomes(passed=1)
    assert "CANNOT VERIFY" not in result.stdout.str()


def test_abstention_from_a_fixture_is_still_an_abstention(
    pytester: pytest.Pytester,
):
    """Setup-phase abstention is the common case: the app never got ready."""
    pytester.makepyfile(
        """
        import pytest
        from cyclaudes import CannotVerify

        @pytest.fixture
        def app_window():
            raise CannotVerify("app under test never opened a window")

        def test_needs_the_app(app_window):
            assert False, "must never run"
        """
    )
    result = pytester.runpytest()
    assert result.ret == EXIT_ABSTAINED
    assert result.parseoutcomes()["abstained"] == 1
    # Crucially: not counted as an 'error', which tooling treats as
    # infrastructure noise rather than an unverified result.
    assert "error" not in _summary_line(result)
    result.stdout.fnmatch_lines(
        ["*ABSTAINED*test_needs_the_app*during setup*", "*app under test never opened a window*"]
    )


def test_abstention_does_not_hide_a_failing_sibling(pytester: pytest.Pytester):
    """Abstaining in one check must not suppress a real failure in another."""
    pytester.makepyfile(THREE_OUTCOMES)
    result = pytester.runpytest()
    result.stdout.fnmatch_lines(["*the button was enabled*"])
