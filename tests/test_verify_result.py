"""The verify-result writer must emit the frozen schema for each outcome.

Phase 3, deliverable B (planning/PHASE_3.md, FROZEN INTERFACE). The Stop gate
routes on ``verify-result/<session_id>.json``; this is the module that writes
it. Two things must hold and are proven here:

* the record it writes matches the frozen schema exactly, for pass/fail/abstain;
* the outcome mapping is the *same* three-outcome semantics the rest of the
  package enforces -- a real failure is never reclassified as an abstention, nor
  the reverse (:mod:`cyclaudes.abstain`, :mod:`cyclaudes.pytest_plugin`).

The pure functions are driven directly (no subprocess, no editable install);
one ``pytester`` run drives the full collector path end-to-end for each outcome.
"""

from __future__ import annotations

import json

import pytest

from cyclaudes import EXIT_ABSTAINED
from cyclaudes.verify_result import (
    OUTCOMES,
    classify,
    map_exit_code,
    read_covered_from_pending,
    write_result,
)


# ---------------------------------------------------------------------------
# outcome mapping mirrors the existing exit-code semantics exactly
# ---------------------------------------------------------------------------


def test_map_exit_code_matches_three_outcome_semantics():
    assert map_exit_code(0) == "pass"
    assert map_exit_code(EXIT_ABSTAINED) == "abstain"  # 12, not 1
    assert map_exit_code(1) == "fail"


def test_map_exit_code_errs_toward_fail_for_odd_codes():
    # A collection error (5) or usage error (4) has verified nothing -> fail,
    # never a silent pass and never miscounted as an abstention.
    for code in (2, 3, 4, 5):
        assert map_exit_code(code) == "fail"
    assert map_exit_code(EXIT_ABSTAINED) == "abstain"


def test_classify_failure_outranks_abstention():
    outcome, detail = classify(
        failures=[("t::a", "expected X, got Y")],
        abstentions=[("t::b", "could not see it")],
    )
    assert outcome == "fail"
    assert "expected X, got Y" in detail


def test_classify_abstention_when_no_failure():
    outcome, detail = classify(failures=[], abstentions=[("t::b", "no a11y grant")])
    assert outcome == "abstain"
    assert "no a11y grant" in detail


def test_classify_pass_when_clean():
    assert classify(failures=[], abstentions=[]) == ("pass", "")


# ---------------------------------------------------------------------------
# the writer produces the frozen schema for each of pass / fail / abstain
# ---------------------------------------------------------------------------

FROZEN_KEYS = {"session_id", "outcome", "covered", "detail", "at"}


@pytest.mark.parametrize(
    "outcome,detail",
    [
        ("pass", ""),
        ("fail", "expected Save ENABLED, actual DISABLED"),
        ("abstain", "Save button absent from the tree"),
    ],
)
def test_write_result_matches_frozen_schema(tmp_path, outcome, detail):
    path = write_result(
        str(tmp_path), "sess-9", outcome, ["src/App.tsx"], detail
    )
    record = json.loads(open(path, encoding="utf-8").read())

    assert set(record) == FROZEN_KEYS
    assert record["session_id"] == "sess-9"
    assert record["outcome"] == outcome
    assert record["covered"] == ["src/App.tsx"]
    assert record["detail"] == detail
    assert record["at"]  # an iso8601 stamp is present
    # Written where the Stop gate reads it.
    assert path.endswith(f"verify-result{__import__('os').sep}sess-9.json")


def test_write_result_rejects_unknown_outcome(tmp_path):
    with pytest.raises(ValueError):
        write_result(str(tmp_path), "sess-9", "maybe", [], "")
    assert set(OUTCOMES) == {"pass", "fail", "abstain"}


def test_read_covered_defaults_to_pending_touched_set(tmp_path):
    pending = tmp_path / ".cyclaudes" / "pending-ui" / "sess-9.json"
    pending.parent.mkdir(parents=True, exist_ok=True)
    pending.write_text(
        json.dumps({"session_id": "sess-9", "ui_touched": ["a.tsx", "b.xaml"]}),
        encoding="utf-8",
    )
    assert read_covered_from_pending(str(tmp_path), "sess-9") == ["a.tsx", "b.xaml"]
    assert read_covered_from_pending(str(tmp_path), "absent") == []


# ---------------------------------------------------------------------------
# end-to-end: a real (subprocess) pytest run maps each outcome via its exit code
# ---------------------------------------------------------------------------

_PASS = "def test_ok(): assert True\n"
_FAIL = "def test_bad(): assert False, 'the button was enabled'\n"
_ABSTAIN = (
    "from cyclaudes import CannotVerify\n"
    "def test_cannot(): raise CannotVerify('Save button absent from the tree')\n"
)


@pytest.mark.parametrize(
    "body,expected_outcome",
    [(_PASS, "pass"), (_FAIL, "fail"), (_ABSTAIN, "abstain")],
)
def test_exit_code_maps_through_a_real_run(pytester, body, expected_outcome):
    """A real pytest run's exit code maps to the right outcome.

    The abstain body must exit :data:`EXIT_ABSTAINED` (12) and map to
    ``abstain``, never ``fail`` -- proving the mapping rides the same
    exit-code contract the abstention plugin already guarantees. Run in a
    subprocess (this repo's isolation pattern) so nested in-process pytest
    state cannot bleed.
    """
    pytester.makepyfile(body)
    result = pytester.runpytest_subprocess()
    assert map_exit_code(result.ret) == expected_outcome


def test_collector_reads_abstain_and_fail_markers():
    """The collector must classify an abstention as such, not as a failure.

    The abstention plugin represents an abstain as ``report.outcome ==
    "failed"`` with ``cyclaudes_abstained = True``; the collector keys off that
    marker, so a fake pair of reports is enough to prove it never miscounts an
    abstention as a failure.
    """
    import types

    import cyclaudes.verify_result as vr

    abstain_report = types.SimpleNamespace(
        nodeid="t::abstains",
        when="call",
        failed=True,
        longreprtext="irrelevant",
        cyclaudes_abstained=True,
        cyclaudes_abstain_reason="Save button absent from the tree",
        user_properties=[],
    )
    fail_report = types.SimpleNamespace(
        nodeid="t::fails",
        when="call",
        failed=True,
        longreprtext="expected ENABLED, got DISABLED",
        user_properties=[],
    )

    collector = vr._ResultCollector()
    collector.pytest_runtest_logreport(abstain_report)
    collector.pytest_runtest_logreport(fail_report)

    assert collector.abstentions == [("t::abstains", "Save button absent from the tree")]
    assert collector.failures == [("t::fails", "expected ENABLED, got DISABLED")]
    # And classify puts the failure first (broken outranks unchecked).
    outcome, _ = vr.classify(collector.failures, collector.abstentions)
    assert outcome == "fail"
