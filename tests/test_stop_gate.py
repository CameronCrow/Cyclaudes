"""The Stop gate must route the three outcomes deterministically.

The load-bearing correctness of Phase 3 (planning/PHASE_3.md). Every branch of
:func:`stop_gate.decide` is exercised against real ``tmp_path`` state files --
the same ``.cyclaudes/`` layout the PostToolUse flagger and the verify-result
writer produce -- so the assertions are against the actual routing an agent
would get, not the hook's own bookkeeping.

The single most important test here is
:func:`test_abstain_allows_and_escalates_does_not_block`: if abstain ever
blocked, an unverifiable change would thrash into Claude Code's 8-block cap and
false-pass there, which is the exact failure the whole phase exists to prevent.

The hook ships as a standalone ``python ${CLAUDE_PLUGIN_ROOT}/hooks/stop_gate.py``
script (stdlib-only, no dependency on cyclaudes being importable), so it is
loaded here by file path rather than imported as a package module.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib

import pytest

_HOOK_PATH = pathlib.Path(__file__).resolve().parents[1] / "hooks" / "stop_gate.py"
_spec = importlib.util.spec_from_file_location("cyclaudes_stop_gate", _HOOK_PATH)
stop_gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(stop_gate)

SID = "sess-123"


# ---------------------------------------------------------------------------
# helpers: write the frozen-schema state files under <project>/.cyclaudes/
# ---------------------------------------------------------------------------


def _write_pending(project_dir: pathlib.Path, session_id: str, ui_touched: list[str]):
    path = project_dir / ".cyclaudes" / "pending-ui" / f"{session_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"session_id": session_id, "ui_touched": ui_touched}),
        encoding="utf-8",
    )


def _write_result(
    project_dir: pathlib.Path,
    session_id: str,
    outcome: str,
    covered: list[str],
    detail: str,
    at: str = "2026-07-22T12:00:00+00:00",
):
    path = project_dir / ".cyclaudes" / "verify-result" / f"{session_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "session_id": session_id,
                "outcome": outcome,
                "covered": covered,
                "detail": detail,
                "at": at,
            }
        ),
        encoding="utf-8",
    )


def _payload(session_id: str = SID, *, stop_hook_active: bool = False) -> dict:
    return {"session_id": session_id, "stop_hook_active": stop_hook_active}


def _counters(project_dir: pathlib.Path) -> dict:
    path = project_dir / ".cyclaudes" / "counters.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# nothing to verify -> never nag
# ---------------------------------------------------------------------------


def test_no_pending_state_allows_silently(tmp_path):
    """A turn with no pending-ui record must not block (non-UI turns stay fast)."""
    decision = stop_gate.decide(_payload(), str(tmp_path))
    assert decision["decision"] == "allow"
    assert decision["system_message"] is None
    # No verification happened -> no audit counters touched.
    assert _counters(tmp_path) == {}


def test_empty_ui_touched_allows(tmp_path):
    _write_pending(tmp_path, SID, [])
    assert stop_gate.decide(_payload(), str(tmp_path))["decision"] == "allow"


# ---------------------------------------------------------------------------
# block-when-unverified: reason names the files
# ---------------------------------------------------------------------------


def test_ui_touched_without_result_blocks_naming_files(tmp_path):
    _write_pending(tmp_path, SID, ["src/App.tsx", "src/Panel.xaml"])
    decision = stop_gate.decide(_payload(), str(tmp_path))
    assert decision["decision"] == "block"
    assert "src/App.tsx" in decision["reason"]
    assert "src/Panel.xaml" in decision["reason"]
    assert _counters(tmp_path).get("block") == 1


# ---------------------------------------------------------------------------
# block-on-fail: reason carries the expected-vs-actual detail
# ---------------------------------------------------------------------------


def test_fail_blocks_and_reason_carries_the_diff(tmp_path):
    diff = "expected Save button ENABLED, actual DISABLED"
    _write_pending(tmp_path, SID, ["src/App.tsx"])
    _write_result(tmp_path, SID, "fail", ["src/App.tsx"], diff)
    decision = stop_gate.decide(_payload(), str(tmp_path))
    assert decision["decision"] == "block"
    assert diff in decision["reason"]
    assert _counters(tmp_path).get("fail") == 1


# ---------------------------------------------------------------------------
# allow-on-pass (covered)
# ---------------------------------------------------------------------------


def test_pass_covering_touched_set_allows(tmp_path):
    _write_pending(tmp_path, SID, ["src/App.tsx"])
    _write_result(tmp_path, SID, "pass", ["src/App.tsx"], "")
    decision = stop_gate.decide(_payload(), str(tmp_path))
    assert decision["decision"] == "allow"
    assert decision["system_message"] is None
    assert _counters(tmp_path).get("pass") == 1


def test_pass_not_covering_touched_set_still_blocks(tmp_path):
    """A pass that covers only some touched files does not satisfy the gate."""
    _write_pending(tmp_path, SID, ["src/App.tsx", "src/New.tsx"])
    _write_result(tmp_path, SID, "pass", ["src/App.tsx"], "")
    decision = stop_gate.decide(_payload(), str(tmp_path))
    assert decision["decision"] == "block"
    assert "src/New.tsx" in decision["reason"]
    assert "src/App.tsx" not in decision["reason"]  # already covered


# ---------------------------------------------------------------------------
# THE most important rule: abstain allows AND escalates, never blocks
# ---------------------------------------------------------------------------


def test_abstain_allows_and_escalates_does_not_block(tmp_path):
    reason = "Save button absent from the tree; window may not have loaded"
    _write_pending(tmp_path, SID, ["src/App.tsx"])
    _write_result(tmp_path, SID, "abstain", ["src/App.tsx"], reason)

    decision = stop_gate.decide(_payload(), str(tmp_path))

    # Must NOT block -- this is the whole point.
    assert decision["decision"] == "allow"
    # Must escalate: the abstain reason is surfaced to the user.
    assert decision["escalated"] is True
    assert decision["system_message"] is not None
    assert reason in decision["system_message"]
    # And it is tallied as an abstention for audit (never swallowed into pass).
    assert _counters(tmp_path).get("abstain") == 1
    assert _counters(tmp_path).get("pass") is None


def test_abstain_stays_allow_across_reentry(tmp_path):
    """Re-firing on the same abstain must keep allowing, never start blocking."""
    _write_pending(tmp_path, SID, ["src/App.tsx"])
    _write_result(tmp_path, SID, "abstain", ["src/App.tsx"], "cannot see it")
    for _ in range(5):
        decision = stop_gate.decide(
            _payload(stop_hook_active=True), str(tmp_path)
        )
        assert decision["decision"] == "allow"
    # Counted once, not once per re-entry (de-duped by the result's `at`).
    assert _counters(tmp_path).get("abstain") == 1


# ---------------------------------------------------------------------------
# coverage: a new UI file after a pass re-opens the gate
# ---------------------------------------------------------------------------


def test_new_ui_file_after_pass_reopens_the_gate(tmp_path):
    _write_pending(tmp_path, SID, ["src/App.tsx"])
    _write_result(tmp_path, SID, "pass", ["src/App.tsx"], "")
    assert stop_gate.decide(_payload(), str(tmp_path))["decision"] == "allow"

    # A later edit touches a new UI file the pass did not cover.
    _write_pending(tmp_path, SID, ["src/App.tsx", "src/Extra.tsx"])
    decision = stop_gate.decide(_payload(), str(tmp_path))
    assert decision["decision"] == "block"
    assert "src/Extra.tsx" in decision["reason"]


# ---------------------------------------------------------------------------
# stop_hook_active re-entry: does not thrash / does not re-block spuriously
# ---------------------------------------------------------------------------


def test_reentry_on_pass_does_not_block(tmp_path):
    _write_pending(tmp_path, SID, ["src/App.tsx"])
    _write_result(tmp_path, SID, "pass", ["src/App.tsx"], "")
    for _ in range(4):
        decision = stop_gate.decide(
            _payload(stop_hook_active=True), str(tmp_path)
        )
        assert decision["decision"] == "allow"


# ---------------------------------------------------------------------------
# bounded retry: exhaustion escalates (allow + message), never infinite block
# ---------------------------------------------------------------------------


def test_retry_exhaustion_escalates_instead_of_blocking_forever(tmp_path, monkeypatch):
    monkeypatch.setenv("CYCLAUDES_RETRY_CAP", "3")
    _write_pending(tmp_path, SID, ["src/App.tsx"])
    _write_result(tmp_path, SID, "fail", ["src/App.tsx"], "still broken")

    # First `cap` re-verifies still fail -> block each time.
    for _ in range(3):
        decision = stop_gate.decide(
            _payload(stop_hook_active=True), str(tmp_path)
        )
        assert decision["decision"] == "block"

    # The next attempt exhausts the budget: escalate (allow) rather than
    # blocking a 4th time toward the 8-block cap.
    final = stop_gate.decide(_payload(stop_hook_active=True), str(tmp_path))
    assert final["decision"] == "allow"
    assert final["escalated"] is True
    assert "after 3 attempts" in final["system_message"]
    assert _counters(tmp_path).get("escalate") == 1


def test_unverified_retry_also_bounded(tmp_path, monkeypatch):
    """Even if verification never runs, blocks are bounded, not infinite."""
    monkeypatch.setenv("CYCLAUDES_RETRY_CAP", "2")
    _write_pending(tmp_path, SID, ["src/App.tsx"])
    for _ in range(2):
        assert stop_gate.decide(_payload(), str(tmp_path))["decision"] == "block"
    final = stop_gate.decide(_payload(), str(tmp_path))
    assert final["decision"] == "allow"
    assert final["escalated"] is True


def test_block_budget_resets_after_a_pass(tmp_path, monkeypatch):
    """A satisfied gate clears the retry budget so a later change starts fresh."""
    monkeypatch.setenv("CYCLAUDES_RETRY_CAP", "3")
    _write_pending(tmp_path, SID, ["src/App.tsx"])
    stop_gate.decide(_payload(), str(tmp_path))  # block 1
    stop_gate.decide(_payload(), str(tmp_path))  # block 2

    _write_result(tmp_path, SID, "pass", ["src/App.tsx"], "")
    assert stop_gate.decide(_payload(), str(tmp_path))["decision"] == "allow"

    # New unverified file: the budget was reset, so we get full blocks again.
    _write_pending(tmp_path, SID, ["src/App.tsx", "src/Two.tsx"])
    for _ in range(3):
        assert stop_gate.decide(_payload(), str(tmp_path))["decision"] == "block"


# ---------------------------------------------------------------------------
# foreign / malformed state is treated as "no result", never as a pass
# ---------------------------------------------------------------------------


def test_result_for_a_different_session_is_ignored(tmp_path):
    _write_pending(tmp_path, SID, ["src/App.tsx"])
    # A verify-result whose inner session_id does not match the keyed one.
    _write_result(tmp_path, SID, "pass", ["src/App.tsx"], "")
    path = tmp_path / ".cyclaudes" / "verify-result" / f"{SID}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["session_id"] = "someone-else"
    path.write_text(json.dumps(data), encoding="utf-8")

    decision = stop_gate.decide(_payload(), str(tmp_path))
    assert decision["decision"] == "block"  # not satisfied by a foreign record


# ---------------------------------------------------------------------------
# the __main__ shim emits the right JSON / exit code for each decision
# ---------------------------------------------------------------------------


def test_main_emits_block_json(tmp_path, monkeypatch, capsys):
    _write_pending(tmp_path, SID, ["src/App.tsx"])
    monkeypatch.setattr(
        "sys.stdin",
        _FakeStdin(json.dumps({"session_id": SID, "cwd": str(tmp_path)})),
    )
    ret = stop_gate.main()
    out = json.loads(capsys.readouterr().out)
    assert ret == 0
    assert out["decision"] == "block"
    assert "src/App.tsx" in out["reason"]


def test_main_emits_system_message_on_abstain(tmp_path, monkeypatch, capsys):
    _write_pending(tmp_path, SID, ["src/App.tsx"])
    _write_result(tmp_path, SID, "abstain", ["src/App.tsx"], "cannot see it")
    monkeypatch.setattr(
        "sys.stdin",
        _FakeStdin(json.dumps({"session_id": SID, "cwd": str(tmp_path)})),
    )
    ret = stop_gate.main()
    out = json.loads(capsys.readouterr().out)
    assert ret == 0
    assert "decision" not in out  # an allow, not a block
    assert "cannot see it" in out["systemMessage"]


def test_main_allows_silently_with_nothing_to_verify(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(
        "sys.stdin",
        _FakeStdin(json.dumps({"session_id": SID, "cwd": str(tmp_path)})),
    )
    ret = stop_gate.main()
    assert ret == 0
    assert capsys.readouterr().out.strip() == ""  # no nag, no output


class _FakeStdin:
    def __init__(self, text: str) -> None:
        self._text = text

    def read(self) -> str:
        return self._text
