"""Phase 3 acceptance test (issue #33) — the autonomous trigger's payoff.

This is not a re-test of #35 (the PostToolUse relevance detector) or #32 (the
Stop-gate routing + bounded retry) in isolation — each already has its own unit
coverage (``tests/test_flag_ui_change.py``, ``tests/test_stop_gate.py``,
``tests/test_verify_result.py``). It is the **cohesive** proof that the three
real pieces, wired together exactly as Claude Code drives them, close one full
**unattended** UI issue-resolution cycle with zero human input — and that the
anti-footgun guards from ``planning/PHASE_3.md`` hold when they are chained.

The cycle it proves, end to end, using the *actual* hook cores (no fakes for the
logic under test — only synthetic Claude Code payloads and a ``tmp_path``
project, since a live Claude Code runtime is not needed to exercise the
deterministic contract):

    edit a UI file            -> PostToolUse `flag()` records it in pending-ui
    agent tries to stop       -> Stop `decide()` BLOCKS (nothing verified yet)
    `cyclaudes verify` runs   -> verify_result.write_result() records the outcome
    agent tries to stop again -> Stop `decide()` routes on that outcome:
        pass    -> ALLOW                       (the happy unattended close)
        fail    -> BLOCK with the diff, agent self-corrects, re-verify -> pass
        abstain -> ALLOW + escalate, and NEVER thrash toward the 8-block cap

The four ``planning/PHASE_3.md`` success criteria, one per guard:

1. A full resolution — edit -> trigger -> verify -> (self-correct ->) allow —
   completes with **zero** Cameron input (``test_happy_unattended_cycle`` and
   ``test_self_correct_loop_fail_then_fix_then_pass``).
2. An unverifiable change **escalates promptly** rather than looping or guessing
   — abstain allows-and-escalates and provably never consumes the block budget
   (``test_abstain_escalates_and_never_thrashes_the_block_cap``). This is the
   phase's single most important correctness rule: a *blocking* abstain would
   thrash into Claude Code's 8-consecutive-block cap and then false-pass there.
3. A change that **breaks the UI is caught by the trigger**, not by Cameron
   later — a ``fail`` outcome blocks with the actionable expected-vs-actual diff
   (``test_self_correct_loop_*`` and ``test_fail_bounded_retry_*``).
4. **Non-UI changes are not slowed down** — a non-UI edit does not flag and the
   Stop gate does not block on it (``test_non_ui_change_does_not_flag_or_block``).

Fake-driven and deterministic: green under the default ``python -m pytest`` with
no desktop and no live Claude Code runtime.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib

import pytest

from cyclaudes import verify_result

# ---------------------------------------------------------------------------
# Load the two hook scripts by file path — they deliberately live OUTSIDE the
# cyclaudes package (at hooks/*.py) so `python ${CLAUDE_PLUGIN_ROOT}/hooks/…`
# needs no install step. The acceptance test drives the same cores Claude Code
# runs, not a copy. verify_result is the shipped `cyclaudes verify` writer.
# ---------------------------------------------------------------------------

_HOOKS = pathlib.Path(__file__).resolve().parents[1] / "hooks"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, _HOOKS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


flag_ui_change = _load("flag_ui_change")
stop_gate = _load("stop_gate")

SID = "sess-phase3-acceptance"


# ---------------------------------------------------------------------------
# The unattended harness: the three moves Claude Code makes each turn, driven
# through the REAL cores. Nothing here simulates the logic under test.
# ---------------------------------------------------------------------------


def _edit_ui_file(project_dir: pathlib.Path, relpath: str, session_id: str = SID):
    """Simulate an Edit tool call: write the file, then fire the PostToolUse hook.

    Returns the pending-ui set the flagger recorded (via the Stop hook's own
    reader), so a caller can assert the relevance test actually fired.
    """
    target = project_dir / relpath
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("// edited by the agent", encoding="utf-8")
    payload = {
        "session_id": session_id,
        "tool_name": "Edit",
        "tool_input": {"file_path": str(target)},
    }
    flag_ui_change.flag(payload, project_dir)
    return stop_gate.read_pending(str(project_dir), session_id)


def _try_to_stop(project_dir: pathlib.Path, session_id: str = SID, *, reentry=False):
    """Simulate the agent trying to end its turn: fire the Stop hook core."""
    payload = {"session_id": session_id, "stop_hook_active": reentry}
    return stop_gate.decide(payload, str(project_dir))


def _run_cyclaudes_verify(
    project_dir: pathlib.Path,
    outcome: str,
    detail: str,
    *,
    session_id: str = SID,
    at: str,
    covered=None,
):
    """Simulate a `cyclaudes verify` run recording its outcome.

    Uses the shipped writer, and — when ``covered`` is not given — the shipped
    ``read_covered_from_pending`` so the result covers exactly what the flagger
    recorded. That the writer reads the flagger's pending file is itself part of
    the end-to-end wiring being proven. Distinct verifications MUST pass distinct
    ``at`` values (the gate de-dupes audit counters on ``at``).
    """
    if covered is None:
        covered = verify_result.read_covered_from_pending(str(project_dir), session_id)
    verify_result.write_result(
        str(project_dir), session_id, outcome, covered, detail, at=at
    )


def _counters(project_dir: pathlib.Path) -> dict:
    path = project_dir / ".cyclaudes" / "counters.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def _block_count(project_dir: pathlib.Path, session_id: str = SID) -> int:
    return int(
        stop_gate.load_gate_state(str(project_dir), session_id).get("block_count", 0)
    )


# ---------------------------------------------------------------------------
# Criterion 1 — the happy unattended cycle: edit -> block -> verify pass -> allow
# ---------------------------------------------------------------------------


def test_happy_unattended_cycle(tmp_path):
    """A UI edit is flagged, the first stop is blocked, and a covering pass
    lets the agent finish — the whole loop with zero human input."""
    # 1. The agent edits a UI file. The relevance test fires and records it.
    pending = _edit_ui_file(tmp_path, "frontend/App.tsx")
    assert pending == {"frontend/App.tsx"}

    # 2. The agent tries to stop. UI touched, nothing verified -> BLOCK, and the
    #    reason names exactly the file that needs verifying (the agent's cue).
    first = _try_to_stop(tmp_path)
    assert first["decision"] == "block"
    assert "frontend/App.tsx" in first["reason"]

    # 3. Prompted, the agent runs `cyclaudes verify`; the checks pass, covering
    #    the touched file (default-covered read from the flagger's pending file).
    _run_cyclaudes_verify(tmp_path, "pass", "", at="2026-07-23T10:00:00+00:00")

    # 4. The agent tries to stop again -> ALLOW, silently. No human was involved.
    second = _try_to_stop(tmp_path, reentry=True)
    assert second["decision"] == "allow"
    assert second["system_message"] is None
    assert second["escalated"] is False

    # Audit trail is honest: one block while unverified, then one pass.
    assert _counters(tmp_path) == {"block": 1, "pass": 1}


# ---------------------------------------------------------------------------
# Criterion 3 — a UI break is caught, and the agent self-corrects unattended:
# fail blocks WITH the diff -> agent fixes -> re-verify pass -> allow.
# ---------------------------------------------------------------------------


def test_self_correct_loop_fail_then_fix_then_pass(tmp_path):
    """A change that breaks the UI is caught by the trigger (fail -> block with
    the expected-vs-actual diff), the agent self-corrects and re-verifies, and
    only the passing re-verify is allowed to finish."""
    _edit_ui_file(tmp_path, "frontend/App.tsx")

    # The first verify catches a real break. The gate blocks and hands back the
    # actionable diff on the block `reason` (its only channel to the model).
    diff = "expected Save button ENABLED, actual DISABLED"
    _run_cyclaudes_verify(tmp_path, "fail", diff, at="2026-07-23T11:00:00+00:00")
    blocked = _try_to_stop(tmp_path)
    assert blocked["decision"] == "block"
    assert diff in blocked["reason"]

    # The agent self-corrects and re-verifies; this run passes.
    _run_cyclaudes_verify(tmp_path, "pass", "", at="2026-07-23T11:05:00+00:00")
    allowed = _try_to_stop(tmp_path, reentry=True)
    assert allowed["decision"] == "allow"
    assert allowed["escalated"] is False

    # The break was recorded as a genuine fail and never reclassified as a pass.
    counters = _counters(tmp_path)
    assert counters.get("fail") == 1
    assert counters.get("pass") == 1


# ---------------------------------------------------------------------------
# Criterion 4 — a non-UI change does not fire the gate (no measurable slowdown).
# ---------------------------------------------------------------------------


def test_non_ui_change_does_not_flag_or_block(tmp_path):
    """A non-UI edit records nothing and the Stop gate allows instantly — the
    cheap relevance test is what keeps the trigger from being disabled."""
    pending = _edit_ui_file(tmp_path, "src/cyclaudes/verify_result.py")

    # The relevance test did not fire: no pending-ui state at all.
    assert pending == set()
    assert not (tmp_path / ".cyclaudes" / "pending-ui").exists()

    # So the agent may stop with no block, no verification, no bookkeeping.
    decision = _try_to_stop(tmp_path)
    assert decision["decision"] == "allow"
    assert decision["system_message"] is None
    assert _counters(tmp_path) == {}


# ---------------------------------------------------------------------------
# Criterion 2 — THE load-bearing guard: an abstain escalates and ALLOWS, and
# provably never consumes the block-retry budget toward the 8-block cap. A
# blocking abstain would thrash into that cap and then false-pass there — the
# exact failure this whole phase exists to prevent.
# ---------------------------------------------------------------------------


def test_abstain_escalates_and_never_thrashes_the_block_cap(tmp_path, monkeypatch):
    # Cap the retries low so that IF abstain wrongly blocked, this test's many
    # re-entries would exhaust it and trip the exhaustion-escalate path — which
    # we then assert never happens.
    monkeypatch.setenv("CYCLAUDES_RETRY_CAP", "3")

    _edit_ui_file(tmp_path, "frontend/App.tsx")

    # Realistically the agent gets one "verify this" block before it runs the
    # check, so account for exactly that single block.
    assert _try_to_stop(tmp_path)["decision"] == "block"
    assert _block_count(tmp_path) == 1

    # The verify run cannot tell (the window never loaded, say) -> abstain.
    reason = "Save button absent from the tree; window may not have loaded"
    _run_cyclaudes_verify(tmp_path, "abstain", reason, at="2026-07-23T12:00:00+00:00")

    # Now the agent re-fires Stop far more times than the 8-block cap. Every
    # single one must ALLOW and escalate — never block.
    for _ in range(12):
        decision = _try_to_stop(tmp_path, reentry=True)
        assert decision["decision"] == "allow"
        assert decision["escalated"] is True
        assert reason in decision["system_message"]

    # The proof it never thrashed:
    #  * the block budget was RESET by the abstain and stayed at 0 throughout,
    #    so 12 re-entries got nowhere near the 8-consecutive-block override;
    assert _block_count(tmp_path) == 0
    counters = _counters(tmp_path)
    #  * the abstain was tallied exactly once (de-duped across re-entries), never
    #    swallowed into a pass;
    assert counters.get("abstain") == 1
    assert counters.get("pass") is None
    #  * only the single legitimate pre-verify block was ever counted — abstain
    #    added no blocks;
    assert counters.get("block") == 1
    #  * and the exhaustion-escalate path was never taken (that would mean abstain
    #    HAD been blocking and burned the whole budget).
    assert counters.get("escalate") is None


# ---------------------------------------------------------------------------
# Criterion 3 (bounded) — fail blocks with the diff, but the retry cycle is
# capped: on exhaustion the gate escalates (allow + message) rather than
# blocking forever toward the 8-block cap.
# ---------------------------------------------------------------------------


def test_fail_bounded_retry_caps_and_escalates_on_exhaustion(tmp_path, monkeypatch):
    monkeypatch.setenv("CYCLAUDES_RETRY_CAP", "3")

    _edit_ui_file(tmp_path, "frontend/App.tsx")
    diff = "expected list to have 3 rows, actual 0"
    _run_cyclaudes_verify(tmp_path, "fail", diff, at="2026-07-23T13:00:00+00:00")

    # The agent keeps failing to satisfy the check. It blocks up to the cap,
    # each block carrying the actionable diff so a real fix is possible.
    for _ in range(3):
        decision = _try_to_stop(tmp_path, reentry=True)
        assert decision["decision"] == "block"
        assert diff in decision["reason"]

    # The next attempt exhausts the budget: rather than block a 4th time toward
    # the 8-block cap (and false-pass there), it escalates — allows the stop and
    # surfaces a clear "could not satisfy after N attempts" to the human.
    final = _try_to_stop(tmp_path, reentry=True)
    assert final["decision"] == "allow"
    assert final["escalated"] is True
    assert "after 3 attempts" in final["system_message"]

    counters = _counters(tmp_path)
    assert counters.get("block") == 3
    assert counters.get("escalate") == 1
    # The fail itself is counted exactly once despite being blocked across turns.
    assert counters.get("fail") == 1


# ---------------------------------------------------------------------------
# Wiring honesty — the outcome mapping the gate routes on cannot silently
# reclassify a real failure as a pass or an abstention. This is what makes
# "a UI break is caught, not missed" a property of the plumbing, not luck.
# ---------------------------------------------------------------------------


def test_verify_outcome_mapping_is_honest():
    # An all-pass exit is a pass; a real failure exit (1) is a fail; only the
    # dedicated abstain exit code is an abstain. Odd exit codes err toward fail —
    # a run that did not cleanly pass has verified nothing.
    assert verify_result.map_exit_code(0) == "pass"
    assert verify_result.map_exit_code(1) == "fail"
    assert verify_result.map_exit_code(verify_result.EXIT_ABSTAINED) == "abstain"
    assert verify_result.map_exit_code(2) == "fail"

    # The writer refuses an out-of-contract outcome outright, so a typo can never
    # write a record the gate would then have to guess about.
    with pytest.raises(ValueError):
        verify_result.write_result("x", SID, "passed", [], "")
