"""Tests for the discipline layer (src/cyclaudes/ui.py).

These are discipline tests, not driver tests: they run against a fake
touchpoint that reproduces the four footguns observed in the 2026-07-20
smoke test (lying action returns, element-ID churn, substring window
grabs, guessed state vocabulary) and prove the layer neutralizes each
one. Every test here fails if the corresponding discipline is removed.

The fake renumbers element IDs on *every* snapshot and hard-errors if an
action is attempted with an ID from any earlier snapshot — so any code
path that caches an element across calls blows up loudly.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from cyclaudes import ui


# ---------------------------------------------------------------------------
# Fake touchpoint
# ---------------------------------------------------------------------------


@dataclass
class FakeWindow:
    id: str
    title: str
    app: str
    pid: int
    position: tuple = (0, 0)
    size: tuple = (800, 600)
    is_active: bool = True
    is_visible: bool = True


@dataclass
class FakeElement:
    id: str
    name: str
    role: str = "unknown"
    raw_role: str = ""
    states: list = field(default_factory=list)
    value: str | None = None


class FakeTouchpoint:
    """Stands in for the touchpoint module, footguns included.

    - ``elements()`` renumbers every element ID on every call (the churn
      observed live: ``uia22`` -> ``uia52`` when a dialog opened).
    - Actions raise ``RuntimeError`` if handed an ID that is not from the
      *latest* snapshot — a stale cached ID cannot be silently reused.
    - ``set_value`` / ``close_window`` can be configured to *lie*: return
      truthy success while changing nothing (the observed
      ``close_window: OK``-while-blocked failure).
    """

    def __init__(self):
        self.wins: list[FakeWindow] = []
        # window_id -> list of element spec dicts (name/role/raw_role/states/value)
        self.trees: dict[str, list[dict]] = {}
        self.generation = 0
        self.issued_ids: set[str] = set()
        self._live: dict[str, dict] = {}  # latest-snapshot id -> spec
        self.windows_calls = 0
        self.set_value_lies = False
        self.close_lies = False
        self.set_value_apply_after: int = 0  # extra snapshots before a write shows up
        self._pending: list[tuple[dict, str, bool, int]] = []
        self.actions: list[tuple] = []

    # -- driver surface used by ui.py --

    def windows(self):
        self.windows_calls += 1
        return list(self.wins)

    def elements(self, window_id=None, **kwargs):
        # Real touchpoint: a scoped read on a window that no longer exists
        # comes back empty (verified live 2026-07-20). The fake must match, or
        # it would report a live tree for a dead window and mask WindowGone.
        if window_id not in {w.id for w in self.wins}:
            return []
        self.generation += 1
        # apply delayed writes whose time has come
        still_pending = []
        for spec, value, replace, due in self._pending:
            if self.generation >= due:
                spec["value"] = value if replace else (spec.get("value") or "") + value
            else:
                still_pending.append((spec, value, replace, due))
        self._pending = still_pending

        self._live = {}
        out = []
        for i, spec in enumerate(self.trees.get(window_id, [])):
            el_id = f"uia{self.generation * 100 + i}"
            self.issued_ids.add(el_id)
            self._live[el_id] = spec
            out.append(
                FakeElement(
                    id=el_id,
                    name=spec.get("name", ""),
                    role=spec.get("role", "unknown"),
                    raw_role=spec.get("raw_role", ""),
                    states=list(spec.get("states", [])),
                    value=spec.get("value"),
                )
            )
        return out

    def _spec_for(self, element):
        el_id = element.id if hasattr(element, "id") else element
        if el_id not in self._live:
            raise RuntimeError(
                f"stale element id {el_id!r} used — not from the latest snapshot"
            )
        return self._live[el_id]

    def get_text_content(self, element):
        return self._spec_for(element).get("value")

    def set_value(self, element, value, replace=False):
        spec = self._spec_for(element)
        self.actions.append(("set_value", spec.get("name"), value, replace))
        if self.set_value_lies:
            return True  # claims success, changes nothing
        if self.set_value_apply_after > 0:
            self._pending.append(
                (spec, value, replace, self.generation + self.set_value_apply_after)
            )
            return True
        spec["value"] = value if replace else (spec.get("value") or "") + value
        return True

    def click(self, element):
        spec = self._spec_for(element)
        self.actions.append(("click", spec.get("name")))
        return True

    def close_window(self, window_id):
        self.actions.append(("close_window", window_id))
        if self.close_lies:
            return True  # claims success, window stays (modal blocked it)
        self.wins = [w for w in self.wins if w.id != window_id]
        self.trees.pop(window_id, None)
        return True


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

FAST = dict(timeout=0.25, poll=0.01)


@pytest.fixture()
def fake(monkeypatch):
    tp = FakeTouchpoint()
    monkeypatch.setattr(ui, "_tp", tp)
    return tp


@pytest.fixture()
def notepad(fake):
    """One Notepad window with a document, a toggle, and a Save button."""
    fake.wins = [FakeWindow(id="w:1", title="Untitled - Notepad", app="Notepad", pid=4242)]
    fake.trees["w:1"] = [
        {
            "name": "Text editor",
            "role": "document",
            "raw_role": "DocumentControl",
            "states": ["focused", "editable"],
            "value": "",
        },
        {
            "name": "Bold (Ctrl+B)",
            "role": "toggle_button",
            "raw_role": "ButtonControl",
            "states": ["checked", "pressed"],
        },
        {"name": "Save", "role": "button", "states": ["enabled"]},
        {"name": "Save As", "role": "button", "states": ["enabled"]},
    ]
    return fake


# ---------------------------------------------------------------------------
# Footgun 3: explicit window resolution — raise on ambiguity, never guess
# ---------------------------------------------------------------------------


class TestWindowResolution:
    def test_ambiguous_window_raises_naming_both_candidates(self, fake):
        fake.wins = [
            FakeWindow(id="w:1", title="Untitled - Notepad", app="Notepad", pid=1),
            FakeWindow(id="w:2", title="import-2026.log - Notepad", app="Notepad", pid=2),
        ]
        with pytest.raises(ui.AmbiguousWindow) as exc:
            ui.window(app="Notepad")
        msg = str(exc.value)
        assert "Untitled - Notepad" in msg
        assert "import-2026.log - Notepad" in msg

    def test_title_matches_exactly_never_substring(self, fake):
        # touchpoint's wait_for_window would substring-grab this window;
        # the discipline layer must not.
        fake.wins = [
            FakeWindow(id="w:2", title="import-2026.log - Notepad", app="Notepad", pid=2)
        ]
        with pytest.raises(ui.WindowNotFound):
            ui.window(title="Notepad")

    def test_substring_matching_is_an_explicit_opt_in(self, fake):
        fake.wins = [
            FakeWindow(id="w:2", title="import-2026.log - Notepad", app="Notepad", pid=2)
        ]
        win = ui.window(title_contains="notepad", **FAST)
        assert win.pid == 2

    def test_not_found_lists_open_windows(self, fake):
        fake.wins = [FakeWindow(id="w:9", title="Inbox", app="Mail", pid=9)]
        with pytest.raises(ui.WindowNotFound) as exc:
            ui.window(app="Notepad")
        assert "Inbox" in str(exc.value)

    def test_no_criteria_is_refused(self, fake):
        with pytest.raises(ValueError):
            ui.window()

    def test_disambiguation_by_pid_and_title_works(self, fake):
        fake.wins = [
            FakeWindow(id="w:1", title="Untitled - Notepad", app="Notepad", pid=1),
            FakeWindow(id="w:2", title="import-2026.log - Notepad", app="Notepad", pid=2),
        ]
        assert ui.window(app="Notepad", pid=1, **FAST).pid == 1
        assert ui.window(app="Notepad", title="import-2026.log - Notepad", **FAST).pid == 2


# ---------------------------------------------------------------------------
# Footgun 2: element IDs are unreachable and uncacheable
# ---------------------------------------------------------------------------


class TestNoRawIDs:
    def test_public_surface_never_exposes_a_backend_id(self, notepad):
        win = ui.window(app="Notepad", **FAST)
        # exercise every public read path
        results = [
            win.title(),
            win.read_text("Text editor"),
            win.states("Bold (Ctrl+B)"),
            win.exists("Save"),
            repr(win),
            win.app,
            win.pid,
        ]
        win.click("Save")
        public_attrs = [getattr(win, a) for a in dir(win) if not a.startswith("_")]
        blob = " ".join(str(v) for v in results + public_attrs)
        assert notepad.issued_ids, "fake never issued ids — test is vacuous"
        for issued in notepad.issued_ids:
            assert issued not in blob
        # and the backend *window* id stays private too
        assert "w:1" not in blob
        assert not hasattr(win, "id")

    def test_every_action_resolves_against_a_fresh_snapshot(self, notepad):
        # The fake renumbers ids on every snapshot and raises RuntimeError on
        # any stale id. Two sequential actions plus reads must all survive —
        # possible only if the layer re-resolves by name each time.
        win = ui.window(app="Notepad", **FAST)
        win.set_value("Text editor", "hello", replace=True)
        win.click("Bold (Ctrl+B)")
        assert win.read_text("Text editor") == "hello"
        gens = {int(i.removeprefix("uia")) // 100 for i in notepad.issued_ids}
        assert len(gens) > 3, "expected many distinct snapshot generations"


# ---------------------------------------------------------------------------
# Footgun 1: action returns are never trusted
# ---------------------------------------------------------------------------


class TestActionsNeverTrustTheirReturn:
    def test_actions_return_none(self, notepad):
        win = ui.window(app="Notepad", **FAST)
        assert win.set_value("Text editor", "x", replace=True) is None
        assert win.click("Save") is None
        assert win.close() is None

    def test_lying_set_value_raises_action_not_verified(self, notepad):
        notepad.set_value_lies = True
        win = ui.window(app="Notepad", **FAST)
        with pytest.raises(ui.ActionNotVerified) as exc:
            win.set_value("Text editor", "hello", replace=True)
        # the message reports what the tree actually showed
        assert "hello" in str(exc.value)
        assert "''" in str(exc.value)

    def test_close_blocked_by_modal_raises_instead_of_ok(self, notepad):
        # The live smoke-test failure: close_window() returned OK while a
        # modal save prompt kept the window open.
        notepad.close_lies = True
        notepad.trees["w:1"].append(
            {"name": "Save changes?", "role": "dialog", "states": ["modal"]}
        )
        win = ui.window(app="Notepad", **FAST)
        with pytest.raises(ui.ActionNotVerified) as exc:
            win.close()
        msg = str(exc.value)
        assert "still exists" in msg
        assert "Save changes?" in msg  # the blocking dialog is named

    def test_honest_close_verifies_from_the_window_list(self, notepad):
        win = ui.window(app="Notepad", **FAST)
        win.close()
        assert notepad.wins == []


# ---------------------------------------------------------------------------
# Footgun 4: opaque state vocabulary, actuals on failure
# ---------------------------------------------------------------------------


class TestStates:
    def test_failed_state_assertion_reports_expected_and_actual(self, notepad):
        win = ui.window(app="Notepad", **FAST)
        with pytest.raises(ui.UIAssertionError) as exc:
            win.assert_state("Bold (Ctrl+B)", "selected")
        msg = str(exc.value)
        assert "selected" in msg  # what was expected
        assert "checked" in msg and "pressed" in msg  # what is actually there

    def test_state_strings_are_opaque_no_vocabulary_enforced(self, fake):
        # A state name no enum knows about must work end-to-end, because
        # macOS AX vocabulary differs from UIA and is discovered, not known.
        fake.wins = [FakeWindow(id="w:1", title="X", app="Ax", pid=7)]
        fake.trees["w:1"] = [
            {"name": "Thing", "states": ["AXFrobnicated", "weird-platform-state"]}
        ]
        win = ui.window(app="Ax", **FAST)
        win.assert_state("Thing", "AXFrobnicated")
        win.assert_not_state("Thing", "AXPressed")
        assert win.states("Thing") == ("AXFrobnicated", "weird-platform-state")

    def test_roles_are_opaque_and_match_raw_platform_role(self, notepad):
        win = ui.window(app="Notepad", **FAST)
        # matches the unified role string
        assert win.exists("Save", role="button")
        # matches the raw platform role string equally
        assert win.exists("Text editor", role="DocumentControl")
        # a wrong role is simply no match, not an enum error
        assert not win.exists("Save", role="AXNonsense")

    def test_enum_valued_roles_and_states_compare_by_value_not_repr(self, fake):
        # The live bug (issue #5): touchpoint returns roles/states as Enum
        # members, and str(State.CHECKED) is "State.CHECKED", not "checked".
        # The layer must compare against .value, or every real-driver state
        # and unified-role assertion silently never matches. The existing
        # fakes use plain strings, so this uses real enum members on purpose.
        import touchpoint as tp

        fake.wins = [FakeWindow(id="w:1", title="X", app="App", pid=1)]
        fake.trees["w:1"] = [
            {
                "name": "Bold",
                "role": tp.Role.BUTTON,
                "raw_role": "button",
                "states": [tp.State.CHECKED, tp.State.ENABLED],
            }
        ]
        win = ui.window(app="App", **FAST)
        # states surface as their portable .value, not "State.CHECKED"
        assert win.states("Bold") == ("checked", "enabled")
        win.assert_state("Bold", "checked")
        win.assert_not_state("Bold", "State.CHECKED")
        # the unified role's .value matches, not its repr
        assert win.exists("Bold", role=tp.Role.BUTTON.value)
        assert not win.exists("Bold", role="Role.BUTTON")


# ---------------------------------------------------------------------------
# Element resolution: raise rather than guess
# ---------------------------------------------------------------------------


class TestElementResolution:
    def test_ambiguous_element_raises_naming_candidates(self, notepad):
        with pytest.raises(ui.AmbiguousElement) as exc:
            ui.window(app="Notepad", **FAST).click("Sav")
        msg = str(exc.value)
        assert "'Save'" in msg and "'Save As'" in msg

    def test_exact_match_beats_substring_ambiguity(self, notepad):
        win = ui.window(app="Notepad", **FAST)
        win.click("Save")  # exact match wins even though "Save As" contains it
        assert ("click", "Save") in notepad.actions

    def test_unique_substring_resolves(self, notepad):
        win = ui.window(app="Notepad", **FAST)
        win.click("Bold")
        assert ("click", "Bold (Ctrl+B)") in notepad.actions

    def test_not_found_lists_available_names(self, notepad):
        with pytest.raises(ui.ElementNotFound) as exc:
            ui.window(app="Notepad", **FAST).click("Italic")
        assert "Bold (Ctrl+B)" in str(exc.value)


# ---------------------------------------------------------------------------
# EmptyTree: the permission trap is a named condition, never a pass
# ---------------------------------------------------------------------------


class TestEmptyTree:
    def test_empty_tree_is_a_named_distinct_condition(self, fake):
        fake.wins = [FakeWindow(id="w:1", title="X", app="App", pid=1)]
        fake.trees["w:1"] = []
        win = ui.window(app="App", **FAST)
        with pytest.raises(ui.EmptyTree) as exc:
            win.read_text("anything")
        assert "Accessibility" in str(exc.value)

    def test_assert_gone_refuses_to_pass_on_an_empty_tree(self, fake):
        # THE silent-false-pass trap: with a missing macOS TCC grant the
        # tree is empty, so every "is it gone?" check would vacuously pass.
        fake.wins = [FakeWindow(id="w:1", title="X", app="App", pid=1)]
        fake.trees["w:1"] = []
        win = ui.window(app="App", **FAST)
        with pytest.raises(ui.EmptyTree):
            win.assert_gone("Error banner")

    def test_zero_windows_is_empty_tree_not_not_found(self, fake):
        fake.wins = []
        with pytest.raises(ui.EmptyTree):
            ui.window(app="Anything")

    def test_empty_tree_is_an_abstention_not_an_assertion_failure(self):
        assert ui.EmptyTree in ui.ABSTENTION_CONDITIONS
        assert not issubclass(ui.EmptyTree, AssertionError)

    def test_window_gone_raises_after_the_window_disappears(self, notepad):
        win = ui.window(app="Notepad", **FAST)
        notepad.wins = []
        with pytest.raises(ui.WindowGone):
            win.read_text("Text editor")


# ---------------------------------------------------------------------------
# Settle/retry: racy updates don't fail, but the deadline is honest
# ---------------------------------------------------------------------------


class TestSnapshotCost:
    def test_reads_and_asserts_never_re_enumerate_all_windows(self, notepad):
        # The second live-UI finding: _require_window() called _tp.windows()
        # on every snapshot, and windows() costs seconds when many apps are
        # open (~8s live with 19 windows). A live re-snapshot must be a single
        # window-scoped elements() read, never a full-desktop walk.
        win = ui.window(app="Notepad", **FAST)
        notepad.windows_calls = 0  # count only post-resolution work
        win.read_text("Text editor")
        win.states("Bold (Ctrl+B)")
        win.assert_state("Bold (Ctrl+B)", "checked")
        win.set_value("Text editor", "hi", replace=True)
        win.assert_text("Text editor", "hi")
        assert notepad.windows_calls == 0, (
            f"re-snapshotting a live window enumerated all windows "
            f"{notepad.windows_calls} times; it must stay scoped to elements()"
        )

    def test_empty_scoped_read_still_distinguishes_gone_from_empty(self, fake):
        # The cheap path must not lose the WindowGone/EmptyTree distinction:
        # window present but tree empty -> EmptyTree; window absent -> WindowGone.
        fake.wins = [FakeWindow(id="w:1", title="X", app="App", pid=1)]
        fake.trees["w:1"] = []
        win = ui.window(app="App", **FAST)
        with pytest.raises(ui.EmptyTree):
            win.read_text("anything")
        fake.wins = []
        with pytest.raises(ui.WindowGone):
            win.read_text("anything")


class TestSettle:
    def test_assert_text_settles_over_a_racy_update(self, notepad):
        notepad.set_value_apply_after = 3  # value shows up 3 snapshots later
        win = ui.window(app="Notepad", timeout=1.0, poll=0.01)
        win.set_value("Text editor", "eventually", replace=True)
        win.assert_text("Text editor", "eventually")

    def test_assertion_still_fails_when_the_tree_never_agrees(self, notepad):
        win = ui.window(app="Notepad", **FAST)
        with pytest.raises(ui.UIAssertionError) as exc:
            win.assert_text("Text editor", "never appears")
        assert "''" in str(exc.value)  # actual observed text is reported
