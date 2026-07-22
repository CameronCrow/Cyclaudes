"""Write ``verify-result/<session_id>.json`` when UI checks run.

The Stop gate (``hooks/stop_gate.py``) can only route the three outcomes if
*something* records them per session, against the FROZEN INTERFACE in
``planning/PHASE_3.md``::

    { "session_id": "...", "outcome": "pass|fail|abstain",
      "covered": ["relpath/one.tsx"],
      "detail": "expected-vs-actual, or the abstain reason",
      "at": "<iso8601>" }

This module is that writer, delivered as a thin ``cyclaudes verify`` CLI
(chosen over a pytest-``sessionfinish`` hook so an *ordinary* pytest run never
writes a result -- only an explicit verification does, which keeps the gate's
input unambiguous). It runs the project's cyclaudes checks with ``pytest`` and
maps the run to an outcome.

The outcome mapping is **exactly** the three-outcome semantics already defined
in :mod:`cyclaudes.abstain` / :mod:`cyclaudes.pytest_plugin` -- it reuses their
process exit codes rather than re-deriving them, so a real failure can never be
reclassified as an abstention or vice versa:

* any UI-check failure -> ``fail``       (pytest exit ``1``)
* any abstention, no failure -> ``abstain`` (pytest exit ``EXIT_ABSTAINED`` = 12)
* all checks pass -> ``pass``            (pytest exit ``0``)

The writing and outcome-mapping are factored into importable, side-effect-free
functions (:func:`map_exit_code`, :func:`classify`, :func:`write_result`) so
they are testable without a subprocess or an editable install; the CLI is a
thin driver over them.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

from .abstain import EXIT_ABSTAINED

STATE_DIRNAME = ".cyclaudes"
PENDING_SUBDIR = "pending-ui"
RESULT_SUBDIR = "verify-result"

#: The only valid outcomes, matching the frozen schema and the three-outcome
#: discipline the rest of the package enforces.
OUTCOMES = ("pass", "fail", "abstain")


# ---------------------------------------------------------------------------
# pure outcome mapping (mirrors cyclaudes.pytest_plugin's exit-code decision)
# ---------------------------------------------------------------------------


def map_exit_code(exit_code: int) -> str:
    """Map a cyclaudes pytest run's exit code to a frozen-schema outcome.

    Reuses the exact codes :mod:`cyclaudes.pytest_plugin` produces: ``0`` is an
    all-pass, :data:`~cyclaudes.abstain.EXIT_ABSTAINED` (12) is "only
    abstentions, nothing broken", and everything else (a real failure ``1``, a
    collection/usage error, ...) is a ``fail``. Erring toward ``fail`` for the
    odd exit codes is the safe direction: a verification that did not cleanly
    pass or cleanly abstain has not verified anything.
    """
    if exit_code == 0:
        return "pass"
    if exit_code == EXIT_ABSTAINED:
        return "abstain"
    return "fail"


def classify(failures, abstentions) -> tuple[str, str]:
    """Derive ``(outcome, detail)`` from collected failures and abstentions.

    Failure outranks abstention (a known-broken result is the more urgent
    signal -- the same precedence :mod:`cyclaudes.pytest_plugin` uses for the
    exit code), so the mapping is: any *failure* -> ``fail`` with the
    expected-vs-actual text; else any *abstention* -> ``abstain`` with the
    reasons; else ``pass``.

    :param failures: iterable of ``(nodeid, detail_text)``.
    :param abstentions: iterable of ``(nodeid, reason)``.
    """
    failures = list(failures)
    abstentions = list(abstentions)
    if failures:
        detail = "\n\n".join(
            f"{nodeid}\n{text}".rstrip() for nodeid, text in failures
        )
        return "fail", detail
    if abstentions:
        detail = "; ".join(f"{nodeid}: {reason}" for nodeid, reason in abstentions)
        return "abstain", detail
    return "pass", ""


# ---------------------------------------------------------------------------
# the writer
# ---------------------------------------------------------------------------


def _state_dir(project_dir: str) -> str:
    return os.path.join(project_dir, STATE_DIRNAME)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def write_result(
    project_dir: str,
    session_id: str,
    outcome: str,
    covered,
    detail: str,
    *,
    at: str | None = None,
) -> str:
    """Write the frozen-schema ``verify-result/<session_id>.json``; return path.

    :raises ValueError: if *outcome* is not one of :data:`OUTCOMES`. Refusing an
        unknown outcome here keeps a typo from writing a record the gate would
        then have to guess about.
    """
    if outcome not in OUTCOMES:
        raise ValueError(f"outcome must be one of {OUTCOMES}, got {outcome!r}")
    record = {
        "session_id": session_id,
        "outcome": outcome,
        "covered": list(covered),
        "detail": detail,
        "at": at or _now_iso(),
    }
    path = os.path.join(_state_dir(project_dir), RESULT_SUBDIR, f"{session_id}.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(record, fh, indent=2, sort_keys=True)
    return path


def read_covered_from_pending(project_dir: str, session_id: str) -> list[str]:
    """The UI files this session touched -- the default ``covered`` set.

    A verification run addresses whatever UI files are pending for the session,
    so absent an explicit ``--covered`` the touched set is exactly what the run
    covers. Returns ``[]`` if there is no pending-ui record.
    """
    path = os.path.join(
        _state_dir(project_dir), PENDING_SUBDIR, f"{session_id}.json"
    )
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return []
    if not isinstance(data, dict):
        return []
    return [str(p) for p in (data.get("ui_touched") or []) if str(p).strip()]


# ---------------------------------------------------------------------------
# CLI: `cyclaudes verify [-- <pytest args>]`
# ---------------------------------------------------------------------------


class _ResultCollector:
    """Pytest plugin that captures failure detail and abstention reasons.

    Reads the markers :mod:`cyclaudes.pytest_plugin` stamps on each report so an
    abstention (which that plugin represents as ``report.outcome == "failed"``
    with ``cyclaudes_abstained = True``) is never miscounted as a failure. The
    *outcome* itself is taken from the process exit code (:func:`map_exit_code`)
    -- authoritative and already correct -- and this collector supplies only the
    human-readable ``detail`` for whichever outcome that turns out to be.
    """

    def __init__(self) -> None:
        self.failures: list[tuple[str, str]] = []
        self.abstentions: list[tuple[str, str]] = []

    def pytest_runtest_logreport(self, report) -> None:
        if getattr(report, "cyclaudes_abstained", False):
            reason = getattr(report, "cyclaudes_abstain_reason", None)
            if not reason:
                reason = _reason_from_properties(report) or "(no reason recorded)"
            self.abstentions.append((report.nodeid, reason))
        elif report.failed:
            where = "" if report.when == "call" else f" [{report.when}]"
            self.failures.append((report.nodeid + where, report.longreprtext))


def _reason_from_properties(report) -> str | None:
    for key, value in getattr(report, "user_properties", []):
        if key.startswith("cyclaudes_reason"):
            return value
    return None


def _resolve_session(project_dir: str, explicit: str | None) -> str:
    """The session id: explicit flag, else env, else the sole pending session."""
    if explicit:
        return explicit
    env = os.environ.get("CLAUDE_SESSION_ID")
    if env:
        return env
    pending_dir = os.path.join(_state_dir(project_dir), PENDING_SUBDIR)
    try:
        candidates = [f for f in os.listdir(pending_dir) if f.endswith(".json")]
    except OSError:
        candidates = []
    if len(candidates) == 1:
        return candidates[0][: -len(".json")]
    raise SystemExit(
        "cyclaudes verify: could not determine the session id. Pass --session, "
        "set CLAUDE_SESSION_ID, or ensure exactly one pending-ui record exists "
        f"(found {len(candidates)})."
    )


def _cmd_verify(args) -> int:
    import pytest

    project_dir = args.project_dir or os.getcwd()
    session_id = _resolve_session(project_dir, args.session)

    collector = _ResultCollector()
    ret = pytest.main(list(args.pytest_args), plugins=[collector])
    ret = int(ret)

    outcome = map_exit_code(ret)
    # The exit code is authoritative for the outcome; the collector supplies the
    # detail. classify() is the fallback when the collector saw the reports
    # (they always agree), but map_exit_code wins so the CLI stays consistent
    # with pytest's own abstain-vs-fail decision even on odd exit codes.
    _, detail = classify(collector.failures, collector.abstentions)
    if outcome == "fail" and not detail:
        detail = f"pytest exited {ret} with no per-test detail collected."

    covered = args.covered or read_covered_from_pending(project_dir, session_id)
    write_result(project_dir, session_id, outcome, covered, detail)
    return ret


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cyclaudes")
    sub = parser.add_subparsers(dest="command", required=True)

    verify = sub.add_parser(
        "verify",
        help="run the cyclaudes UI checks and record verify-result/<session>.json",
    )
    verify.add_argument(
        "--session", help="session id (default: $CLAUDE_SESSION_ID or the sole pending)"
    )
    verify.add_argument(
        "--project-dir", help="project root owning .cyclaudes/ (default: cwd)"
    )
    verify.add_argument(
        "--covered",
        nargs="*",
        help="UI files this run covers (default: the session's pending ui_touched)",
    )
    verify.add_argument(
        "pytest_args",
        nargs=argparse.REMAINDER,
        help="args passed through to pytest (put them after `--`)",
    )
    verify.set_defaults(func=_cmd_verify)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    # argparse REMAINDER keeps a leading `--`; drop it so pytest sees clean args.
    if getattr(args, "pytest_args", None) and args.pytest_args[:1] == ["--"]:
        args.pytest_args = args.pytest_args[1:]
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover - exercised via the console script
    sys.exit(main())
