"""Issue #7 / Phase 1 success criterion 3: abstention works and is never read
as success — the property the whole project rests on.

If a check reports "verified" on work nobody could actually check, the tool
does not merely fail to help, it does harm: it manufactures false confidence.
So each genuinely-unevaluable situation must produce an **abstention** — not a
pass, and not a plain failure — and that abstention must be impossible to
mistake for success on any surface an agent reads (its own letter, its own
count, its own exit code 12, a loud summary section naming what could not be
judged and why).

Three unevaluable situations, run through the real discipline layer via
``pytester`` so the assertions are on the real outcome an agent sees:

* the accessibility tree is empty/unavailable — **the macOS TCC case**: a
  missing Accessibility grant yields an empty tree that must never read as
  "nothing is broken";
* the target element does not exist / never appears, so a property of it
  cannot be judged;
* the app is in a state the check cannot interpret.
"""

from __future__ import annotations

import pytest

from cyclaudes import EXIT_ABSTAINED

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
        self.els = []          # empty tree by default: the denied-a11y case
    def add(self, name, **kw):
        self.els.append(_El(name, **kw)); return self.els[-1]
    def windows(self):
        return list(self.wins)
    def elements(self, window_id=None, **k):
        return list(self.els) if window_id in {w.id for w in self.wins} else []
    def get_text_content(self, el):
        return el.value
    def set_value(self, el, value, replace=False):
        el.value = value if replace else (el.value or "") + value
    def click(self, el):
        pass
    def close_window(self, wid):
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

UNVERIFIABLE_CHECKS = '''
from cyclaudes import cannot_verify

def test_empty_accessibility_tree_abstains(tp, win):
    # tp.els is empty — e.g. a missing macOS TCC Accessibility grant. There is
    # nothing to read, so "is the banner gone?" must NOT vacuously pass.
    win.assert_gone("Error banner")  # -> EmptyTree -> abstain, never pass

def test_absent_target_element_abstains(tp, win):
    tp.add("Cancel", role="button")  # tree is readable, but no Save button
    if not win.exists("Save"):
        cannot_verify(
            "Save button never appeared in the tree; cannot judge whether it "
            "is disabled. The app may not have finished loading."
        )
    win.assert_state("Save", "disabled")  # unreachable

def test_unexpected_app_state_abstains(tp, win):
    tp.add("Text Editor", role="text_field", value="")
    title = win.title()
    if title != "Editor - Ready":
        cannot_verify(
            f"app is in an unexpected state (window title {title!r}); the "
            "check cannot interpret it. A prior step may have left it dirty."
        )
'''


def test_each_unevaluable_case_abstains_never_passes_or_fails(
    pytester: pytest.Pytester,
):
    pytester.makeconftest(FAKE_CONFTEST)
    pytester.makepyfile(UNVERIFIABLE_CHECKS)
    result = pytester.runpytest()

    # Abstention is its own bucket — not folded into passed, not into failed.
    outcomes = result.parseoutcomes()
    assert outcomes.get("abstained") == 3
    assert "passed" not in outcomes
    assert "failed" not in outcomes


def test_an_all_abstain_run_is_not_reportable_as_verified(
    pytester: pytest.Pytester,
):
    """The single most important assertion: nothing an agent reads says success."""
    pytester.makeconftest(FAKE_CONFTEST)
    pytester.makepyfile(UNVERIFIABLE_CHECKS)
    result = pytester.runpytest()

    assert result.ret != int(pytest.ExitCode.OK)  # not 0 / "verified"
    assert result.ret == EXIT_ABSTAINED  # its own code, distinct from failure's 1
    out = result.stdout.str()
    assert "CANNOT VERIFY" in out  # the loud, unconditional section
    assert "This run is NOT a pass" in out


def test_the_abstention_letter_and_reasons_are_visible(pytester: pytest.Pytester):
    pytester.makeconftest(FAKE_CONFTEST)
    pytester.makepyfile(UNVERIFIABLE_CHECKS)
    result = pytester.runpytest()
    # 'A', not '.'/'F'/'s'.
    result.stdout.fnmatch_lines(["*AAA*"])
    # Every abstention explains what was attempted and why it could not be judged.
    out = result.stdout.str()
    assert "Save button never appeared" in out
    assert "unexpected state" in out


def test_the_macos_tcc_empty_tree_is_the_reason_shown(pytester: pytest.Pytester):
    """The denied-accessibility case must read as 'cannot see', not 'all good'."""
    pytester.makeconftest(FAKE_CONFTEST)
    pytester.makepyfile(
        """
        def test_denied_a11y(tp, win):
            win.assert_gone("Error banner")
        """
    )
    result = pytester.runpytest()
    assert result.ret == EXIT_ABSTAINED
    out = result.stdout.str()
    assert "empty" in out.lower()
    assert "do not treat this as a pass" in out.lower()
