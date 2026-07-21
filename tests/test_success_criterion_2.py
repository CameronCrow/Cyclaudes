"""Issue #6 / Phase 1 success criterion 2: a broken change is CAUGHT.

A verification tool that cannot fail is worse than none. This proves, through
the real discipline layer, that each way a UI change can silently break
produces a pytest **failure** — not a pass, and not an abstention — and that
the failure message names expected vs actual clearly enough to self-correct.

Run pytest-inside-pytest (``pytester``) against a small fake driver so every
assertion is on the real outcome and exit code an agent would read, while
staying deterministic and CI-safe. The fake can be told to *lie* the way the
live smoke test's driver did — return OK while changing nothing — which is the
headline case here.
"""

from __future__ import annotations

import pytest

# A fake touchpoint that can simulate each broken behaviour, plus a `win`
# fixture, shared by every inner test via the generated conftest.
FAKE_CONFTEST = '''
import pytest
from cyclaudes import ui

class _El:
    id = "e"
    def __init__(self, name, role="button", raw_role="", states=(), value=None):
        self.name, self.role, self.raw_role = name, role, raw_role
        self.states, self.value = list(states), value

class _Win:
    id, title, app, pid = "w:1", "App", "app", 1

class FakeTP:
    def __init__(self):
        self.wins = [_Win()]
        self.els = []
        self.set_value_lies = False
        self.close_lies = False
    def add(self, name, **kw):
        self.els.append(_El(name, **kw)); return self.els[-1]
    def windows(self):
        return list(self.wins)
    def elements(self, window_id=None, **k):
        return list(self.els) if window_id in {w.id for w in self.wins} else []
    def get_text_content(self, el):
        return el.value
    def set_value(self, el, value, replace=False):
        if self.set_value_lies:
            return  # claims success, changes nothing
        el.value = value if replace else (el.value or "") + value
    def click(self, el):
        pass
    def close_window(self, wid):
        if self.close_lies:
            return  # OK returned, window stays (a modal blocked it)
        self.wins = [w for w in self.wins if w.id != wid]

@pytest.fixture
def tp(monkeypatch):
    fake = FakeTP()
    monkeypatch.setattr(ui, "_tp", fake)
    return fake

@pytest.fixture
def win(tp):
    return ui.window(app="app", timeout=0.3, poll=0.02)
'''

BROKEN_CHECKS = '''
import pytest
from cyclaudes import ui

def test_close_returns_ok_but_modal_silently_blocks_it(tp, win):
    # THE case that bit us: close_window() returned OK while a modal blocked
    # the close. The check must fail, not pass.
    tp.add("Text Editor", role="text_field", value="unsaved")
    tp.add("Notepad", role="dialog", states=["modal"])
    tp.close_lies = True
    win.close()  # must raise -> the test fails

def test_action_returning_ok_while_value_unchanged_fails(tp, win):
    tp.add("Text Editor", role="text_field", value="")
    tp.set_value_lies = True
    win.set_value("Text Editor", "hello", replace=True)  # lie -> must raise

def test_expected_text_that_never_appears_fails(tp, win):
    tp.add("Text Editor", role="text_field", value="Modified")
    win.assert_text("Text Editor", "Saved")  # never becomes "Saved" -> fail

def test_expected_element_absent_fails(tp, win):
    tp.add("Cancel", role="button")
    win.assert_exists("Save")  # not in the tree -> fail

def test_state_that_does_not_change_fails(tp, win):
    tp.add("Bold", role="button", states=["enabled"])  # never "checked"
    win.assert_state("Bold", "checked")  # -> fail, actuals reported
'''


def test_every_broken_case_fails_and_none_abstains(pytester: pytest.Pytester):
    pytester.makeconftest(FAKE_CONFTEST)
    pytester.makepyfile(BROKEN_CHECKS)
    result = pytester.runpytest()

    # Every broken case is a FAILURE — not a pass, and crucially not an
    # abstention (which would say "couldn't check" when we very much could).
    result.assert_outcomes(failed=5)
    assert result.ret == int(pytest.ExitCode.TESTS_FAILED)
    assert "abstained" not in result.stdout.str()
    assert "CANNOT VERIFY" not in result.stdout.str()


def test_failure_messages_name_expected_versus_actual(pytester: pytest.Pytester):
    pytester.makeconftest(FAKE_CONFTEST)
    pytester.makepyfile(BROKEN_CHECKS)
    result = pytester.runpytest("-v")
    out = result.stdout.str()
    # The OK-but-blocked close names the standing state...
    assert "still exists" in out
    # ...the text mismatch shows both expected and what the tree actually held...
    assert "Saved" in out and "Modified" in out
    # ...and the wrong-state failure prints the element's real states.
    assert "checked" in out and "enabled" in out
