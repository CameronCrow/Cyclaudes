#!/usr/bin/env python3
"""Stop-gate hook — refuse to finish while UI changes are unverified.

This is the load-bearing correctness of Phase 3 (see ``planning/PHASE_3.md``).
A ``Stop`` hook fires on *every* turn end. On each firing it reads the
session-scoped state the ``PostToolUse`` flagger writes and the verification
result (if any) and routes the three outcomes:

* **UI touched, no covering verify-result yet** -> *block*, naming the files to
  verify. Claude gets the reason and keeps working.
* **``outcome == "fail"``** -> *block*, reason = the expected-vs-actual
  ``detail`` so the agent self-corrects, then re-verifies.
* **``outcome == "pass"`` covering the touched set** -> *allow*.
* **``outcome == "abstain"`` covering the touched set** -> *allow, and surface
  the abstain ``detail`` to the user*. **This is the single most important
  rule.** Abstain is a legitimate stopping point that escalates -- an
  unverifiable change must not be blocked. If it were, the change would thrash
  into Claude Code's 8-consecutive-block cap and then false-pass anyway, which
  is the exact failure this whole phase exists to prevent.

The gate is satisfied only when ``covered`` covers ``ui_touched``; a later edit
to a *new* UI file re-opens it (block again, naming just the new file).

Constraints honoured from the hook contract:

* **Idempotent / never nag.** A turn with nothing to verify allows silently.
* **``stop_hook_active``-aware.** Re-entry is expected; the bounded retry below
  guarantees the block budget is never spun.
* **8-block-cap-safe.** A correct->verify cycle that cannot be satisfied is
  capped at :data:`DEFAULT_RETRY_CAP` blocks (well under 8); on exhaustion the
  gate *escalates* -- allows the stop and surfaces a clear "could not satisfy
  after N attempts" message -- rather than looping invisibly.

Deliberately **stdlib-only and self-contained**: it must run as
``python ${CLAUDE_PLUGIN_ROOT}/hooks/stop_gate.py`` with no dependency on the
``cyclaudes`` package being importable from the hook process. The core is
factored into :func:`decide` so every branch is unit-testable against
``tmp_path`` state files; ``__main__`` is a thin stdin/stdout/exit shim.
"""

from __future__ import annotations

import json
import os
import sys

#: Root of the git-ignored session state, relative to the project directory.
#: Must match the FROZEN INTERFACE in planning/PHASE_3.md and what issue A writes.
STATE_DIRNAME = ".cyclaudes"
PENDING_SUBDIR = "pending-ui"
RESULT_SUBDIR = "verify-result"
#: Our own bookkeeping (retry counters, audit counters); not part of the frozen
#: cross-issue contract -- only this hook reads and writes it.
GATE_SUBDIR = "gate-state"
COUNTERS_FILE = "counters.json"

#: Default cap on correct->verify cycles before the gate escalates instead of
#: blocking again. Kept well under Claude Code's 8-consecutive-block override so
#: an unsatisfiable check escalates *loudly* rather than being force-passed by
#: the platform. Overridable via ``CYCLAUDES_RETRY_CAP`` for tuning.
DEFAULT_RETRY_CAP = 3


# ---------------------------------------------------------------------------
# state IO (stdlib-only, defensive: a malformed/absent file reads as "nothing")
# ---------------------------------------------------------------------------


def _state_dir(project_dir: str) -> str:
    return os.path.join(project_dir, STATE_DIRNAME)


def _read_json(path: str):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _write_json(path: str, data) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)


def _norm(relpath: str) -> str:
    """Normalise a state-file relpath for set comparison (separators only).

    Both sides of the coverage check come from the same producers writing
    repo-relative paths, so normalising ``\\`` -> ``/`` and trimming is enough;
    case is left untouched to avoid guessing the filesystem's semantics.
    """
    return str(relpath).replace("\\", "/").strip()


def read_pending(project_dir: str, session_id: str) -> set[str]:
    """The de-duplicated UI-touched set for *session_id* (empty if none)."""
    if not session_id:
        return set()
    data = _read_json(
        os.path.join(_state_dir(project_dir), PENDING_SUBDIR, f"{session_id}.json")
    )
    if not isinstance(data, dict):
        return set()
    touched = data.get("ui_touched") or []
    return {_norm(p) for p in touched if str(p).strip()}


def read_verify_result(project_dir: str, session_id: str):
    """The verify-result record for *session_id*, or ``None`` if absent/foreign.

    Validates the ``session_id`` inside the file against the one keying the
    path, so a stray or mismatched record can never satisfy the wrong session's
    gate.
    """
    if not session_id:
        return None
    data = _read_json(
        os.path.join(_state_dir(project_dir), RESULT_SUBDIR, f"{session_id}.json")
    )
    if not isinstance(data, dict):
        return None
    if data.get("session_id") not in (None, session_id):
        return None
    return data


# ---------------------------------------------------------------------------
# our own gate bookkeeping (retry budget + audit counters)
# ---------------------------------------------------------------------------


def _gate_path(project_dir: str, session_id: str) -> str:
    return os.path.join(_state_dir(project_dir), GATE_SUBDIR, f"{session_id}.json")


def load_gate_state(project_dir: str, session_id: str) -> dict:
    data = _read_json(_gate_path(project_dir, session_id))
    if not isinstance(data, dict):
        return {"session_id": session_id, "block_count": 0, "last_counted_at": None}
    data.setdefault("session_id", session_id)
    data.setdefault("block_count", 0)
    data.setdefault("last_counted_at", None)
    return data


def save_gate_state(project_dir: str, session_id: str, state: dict) -> None:
    _write_json(_gate_path(project_dir, session_id), state)


def _reset_block_count(project_dir: str, session_id: str) -> None:
    state = load_gate_state(project_dir, session_id)
    if state.get("block_count"):
        state["block_count"] = 0
        save_gate_state(project_dir, session_id, state)


def bump_counter(project_dir: str, name: str) -> None:
    """Persist a pass/fail/abstain/block/escalate tally for later audit.

    The point (planning/PHASE_3.md, "Key risk") is that early runs can be
    checked for *swallowed abstentions*: if a project accrued unverifiable UI
    changes but the ``abstain`` tally is zero, something quietly reclassified
    them. Counters live at ``.cyclaudes/counters.json`` (project-wide).
    """
    path = os.path.join(_state_dir(project_dir), COUNTERS_FILE)
    data = _read_json(path)
    if not isinstance(data, dict):
        data = {}
    data[name] = int(data.get(name, 0)) + 1
    _write_json(path, data)


def _count_outcome(project_dir: str, session_id: str, outcome: str, at) -> None:
    """Tally a *verification outcome* once, de-duped by its ``at`` timestamp.

    The Stop hook re-reads the same verify-result on every turn end; without
    de-duping, a single fail blocked across three turns would inflate the audit
    counters threefold. Keyed on the result's ``at`` so each distinct
    verification is counted exactly once.
    """
    state = load_gate_state(project_dir, session_id)
    if at is not None and state.get("last_counted_at") == at:
        return
    state["last_counted_at"] = at
    save_gate_state(project_dir, session_id, state)
    bump_counter(project_dir, outcome)


# ---------------------------------------------------------------------------
# decision core
# ---------------------------------------------------------------------------


def _retry_cap() -> int:
    try:
        cap = int(os.environ.get("CYCLAUDES_RETRY_CAP", DEFAULT_RETRY_CAP))
    except (TypeError, ValueError):
        return DEFAULT_RETRY_CAP
    return cap if cap > 0 else DEFAULT_RETRY_CAP


def _allow(reason=None, system_message=None, escalated=False) -> dict:
    return {
        "decision": "allow",
        "reason": reason,
        "system_message": system_message,
        "escalated": escalated,
    }


def _unverified_reason(files: list[str], *, have_result: bool) -> str:
    listed = ", ".join(files)
    lead = (
        "UI changes have not been verified yet."
        if not have_result
        else "New UI changes are not covered by the last verification."
    )
    return (
        f"{lead} These edited UI file(s) need a verification run before you can "
        f"stop: {listed}. Run the cyclaudes UI checks that cover them (e.g. "
        f"`cyclaudes verify`), or invoke the verify-ui skill to author and run "
        f"the checks. If the change genuinely cannot be verified, record an "
        f"abstain outcome (CannotVerify) -- that is a legitimate stopping point, "
        f"not a reason to guess."
    )


def _fail_reason(detail: str) -> str:
    return (
        "UI verification FAILED. Self-correct, then re-verify. "
        f"Expected-vs-actual:\n{detail}"
    )


def _abstain_message(covered: list[str], detail: str) -> str:
    listed = ", ".join(covered) if covered else "the touched UI files"
    return (
        "UI verification ABSTAINED (could not verify) for "
        f"{listed}. This is a legitimate stopping point that needs a human's "
        f"eyes -- it is NOT a pass and NOT a failure. What the check could not "
        f"determine:\n{detail}"
    )


def _exhaustion_message(cap: int, last_reason: str) -> str:
    return (
        f"Could not satisfy the UI checks after {cap} attempts. Escalating and "
        f"allowing the stop rather than blocking further (the block budget is "
        f"bounded on purpose). Last blocking reason was:\n{last_reason}"
    )


def _gate_block(
    project_dir: str,
    session_id: str,
    *,
    reason: str,
    result_at=None,
    fail_at=None,
) -> dict:
    """Emit a block, unless the bounded-retry budget is exhausted -> escalate.

    ``fail_at`` (the verify-result ``at`` of a *fail* being blocked on) lets the
    audit counter tally the failure exactly once even while it is blocked across
    several turns.
    """
    if fail_at is not None:
        _count_outcome(project_dir, session_id, "fail", fail_at)

    cap = _retry_cap()
    state = load_gate_state(project_dir, session_id)
    count = int(state.get("block_count", 0))

    if count >= cap:
        # Exhausted. Escalate rather than burn another block -- this is what
        # keeps an unsatisfiable check from thrashing into the 8-block cap and
        # false-passing there. Reset so a genuinely new change starts fresh.
        state["block_count"] = 0
        save_gate_state(project_dir, session_id, state)
        bump_counter(project_dir, "escalate")
        return _allow(system_message=_exhaustion_message(cap, reason), escalated=True)

    state["block_count"] = count + 1
    save_gate_state(project_dir, session_id, state)
    bump_counter(project_dir, "block")
    return {
        "decision": "block",
        "reason": reason,
        "system_message": None,
        "escalated": False,
    }


def decide(payload: dict, project_dir: str) -> dict:
    """Route one Stop event. Pure w.r.t. its inputs + the ``.cyclaudes`` state.

    :param payload: the Stop hook stdin JSON -- ``session_id``,
        ``stop_hook_active`` (and ``cwd``, used by ``main`` to locate the
        project). Re-entry (``stop_hook_active``) needs no special branch: the
        bounded retry below already guarantees the block budget cannot spin.
    :param project_dir: the project root that owns ``.cyclaudes/``.
    :returns: ``{"decision": "block"|"allow", "reason", "system_message",
        "escalated"}``. ``reason`` is Claude-facing (the block channel);
        ``system_message`` is the user-facing escalation surfaced on an allow.
    """
    payload = payload or {}
    session_id = payload.get("session_id") or ""

    ui_touched = read_pending(project_dir, session_id)

    # Nothing to verify -> never nag. The idempotent, cheap-to-satisfy path that
    # keeps non-UI turns (and re-entries with nothing pending) instant.
    if not ui_touched:
        _reset_block_count(project_dir, session_id)
        return _allow()

    result = read_verify_result(project_dir, session_id)
    outcome = (result or {}).get("outcome")
    covered = {_norm(p) for p in ((result or {}).get("covered") or [])}
    uncovered = ui_touched - covered

    # A fail outranks everything: the agent must self-correct before stopping,
    # even if it has meanwhile touched further files. Reason carries the diff.
    if result and outcome == "fail":
        detail = result.get("detail") or "UI verification failed (no detail recorded)."
        return _gate_block(
            project_dir,
            session_id,
            reason=_fail_reason(detail),
            fail_at=result.get("at"),
        )

    # No result yet, or a newly-edited UI file the last verification did not
    # cover -> block, naming exactly what still needs verifying (re-opens gate).
    if uncovered:
        return _gate_block(
            project_dir,
            session_id,
            reason=_unverified_reason(sorted(uncovered), have_result=bool(result)),
        )

    # Fully covered by the latest verification.
    if outcome == "abstain":
        # THE most important rule: abstain satisfies the gate AND escalates.
        _reset_block_count(project_dir, session_id)
        _count_outcome(project_dir, session_id, "abstain", result.get("at"))
        detail = result.get("detail") or "(no reason recorded)"
        return _allow(
            system_message=_abstain_message(sorted(covered), detail), escalated=True
        )

    if outcome == "pass":
        _reset_block_count(project_dir, session_id)
        _count_outcome(project_dir, session_id, "pass", result.get("at"))
        return _allow()

    # Defensive: a result with full coverage but an outcome we don't recognise.
    # Treat as unverified rather than assuming success.
    return _gate_block(
        project_dir,
        session_id,
        reason=_unverified_reason(sorted(ui_touched), have_result=bool(result)),
    )


# ---------------------------------------------------------------------------
# __main__ -- thin stdin / stdout-JSON / exit-code shim
# ---------------------------------------------------------------------------


def main(argv=None) -> int:
    try:
        payload = json.load(sys.stdin)
    except (ValueError, OSError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}

    project_dir = payload.get("cwd") or os.getcwd()
    outcome = decide(payload, project_dir)

    if outcome["decision"] == "block":
        # JSON decision mode: Claude receives `reason` and keeps working.
        print(json.dumps({"decision": "block", "reason": outcome["reason"]}))
        return 0

    # Allow. Surface any escalation (abstain / retry-exhaustion) to the user via
    # `systemMessage` -- the Stop hook cannot inject additionalContext, so this
    # is the escalation channel.
    body: dict = {}
    if outcome.get("system_message"):
        body["systemMessage"] = outcome["system_message"]
    if body:
        print(json.dumps(body))
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised via subprocess/tests
    sys.exit(main())
