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
from touchpoint.core.exceptions import BackendUnavailableError, TouchpointError

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
    - ``elements(source="dom")`` models touchpoint's CDP DOM-walk path
      (issue #37): it serves ``dom_trees`` (the *actual DOM*, distinct from the
      a11y ``trees``), or — when ``dom_raises`` is set — raises the real
      ``TouchpointError``/``BackendUnavailableError`` a non-CDP target produces.
    """

    #: The real touchpoint base exception, so ``ui._dom_snapshot`` can do a
    #: precise ``except _tp.TouchpointError`` against the fake standing in for
    #: the module. (Matches how the real module exposes it as ``tp.*``.)
    TouchpointError = TouchpointError
    BackendUnavailableError = BackendUnavailableError

    def __init__(self):
        self.wins: list[FakeWindow] = []
        # window_id -> list of element spec dicts (name/role/raw_role/states/value)
        self.trees: dict[str, list[dict]] = {}
        # window_id -> DOM-walk spec dicts (the *real DOM*, source="dom").
        self.dom_trees: dict[str, list[dict]] = {}
        # When set, a source="dom" read raises this instead of returning — the
        # not-CDP-backed / no-CDP-backend case touchpoint raises for.
        self.dom_raises: Exception | None = None
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

    def elements(self, window_id=None, source="full", **kwargs):
        # DOM-walk path (issue #37): source="dom" reads the *actual DOM*, a
        # different tree from the a11y projection, and on a non-CDP target
        # touchpoint raises TouchpointError rather than returning.
        if source == "dom":
            if self.dom_raises is not None:
                raise self.dom_raises
            if window_id not in {w.id for w in self.wins}:
                return []
            self.generation += 1
            self._live = {}
            out = []
            for i, spec in enumerate(self.dom_trees.get(window_id, [])):
                el_id = f"cdp:9222:t1:dom:{self.generation * 100 + i},0"
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


@pytest.fixture(autouse=True)
def _clean_ownership():
    """Isolate the module-level owned-PID set between tests (it is global state)."""
    ui.reset_ownership()
    yield
    ui.reset_ownership()


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


# ---------------------------------------------------------------------------
# PID-scoped window ownership (Phase 2A, issue #12): never touch a window we
# did not launch. Ambiguity or an unowned target raises — never a guess.
# ---------------------------------------------------------------------------


class TestOwnership:
    def test_owned_window_refuses_a_window_we_did_not_launch(self, fake):
        # THE smoke-test scenario: our launched Notepad (pid 100) sits next to
        # Cameron's real open log file (pid 200), another Notepad. Resolving by
        # app must pick OURS; targeting HIS window must raise, never resolve.
        fake.wins = [
            FakeWindow(id="w:mine", title="Untitled - Notepad", app="Notepad", pid=100),
            FakeWindow(id="w:his", title="import-2026.log - Notepad", app="Notepad", pid=200),
        ]
        ui.own(100)

        win = ui.owned_window(app="Notepad", **FAST)
        assert win.pid == 100  # our window, not his

        with pytest.raises(ui.UnownedWindow) as exc:
            ui.owned_window(title="import-2026.log - Notepad", **FAST)
        msg = str(exc.value)
        assert "import-2026.log - Notepad" in msg  # names the offender
        assert "200" in msg

    def test_owned_windows_enumeration_excludes_unowned(self, fake):
        fake.wins = [
            FakeWindow(id="w:mine", title="Mine", app="App", pid=100),
            FakeWindow(id="w:his", title="His", app="App", pid=200),
        ]
        ui.own(100)
        listed = ui.owned_windows()
        assert {w.pid for w in listed} == {100}
        assert [w.title for w in listed] == ["Mine"]

    def test_enumeration_with_nothing_owned_raises(self, fake):
        fake.wins = [FakeWindow(id="w:his", title="His", app="App", pid=200)]
        with pytest.raises(ui.NoOwnedWindows):
            ui.owned_windows()

    def test_owned_window_with_nothing_owned_raises(self, fake):
        fake.wins = [FakeWindow(id="w:his", title="His", app="App", pid=200)]
        with pytest.raises(ui.NoOwnedWindows):
            ui.owned_window(app="App", **FAST)

    def test_owned_window_not_found_among_owned_lists_owned(self, fake):
        # Criteria that match no window at all (owned or not) -> WindowNotFound,
        # and the listing is scoped to OUR windows, never the whole desktop.
        fake.wins = [
            FakeWindow(id="w:mine", title="Mine", app="App", pid=100),
            FakeWindow(id="w:his", title="Other", app="Other", pid=200),
        ]
        ui.own(100)
        with pytest.raises(ui.WindowNotFound) as exc:
            ui.owned_window(app="Nonexistent", **FAST)
        msg = str(exc.value)
        assert "Mine" in msg  # our windows are what it lists
        assert "Other" not in msg  # the unowned window is never disclosed

    def test_ambiguous_among_owned_still_raises(self, fake):
        fake.wins = [
            FakeWindow(id="w:1", title="Untitled - Notepad", app="Notepad", pid=1),
            FakeWindow(id="w:2", title="Other - Notepad", app="Notepad", pid=2),
        ]
        ui.own(1)
        ui.own(2)
        with pytest.raises(ui.AmbiguousWindow):
            ui.owned_window(app="Notepad", **FAST)

    def test_acting_on_a_disowned_handle_raises(self, notepad):
        # notepad fixture uses pid 4242.
        ui.own(4242)
        win = ui.owned_window(app="Notepad", **FAST)
        win.assert_text("Text editor", "")  # works while owned

        ui.disown(4242)
        # every read and action now refuses — a handle can't outlive its claim
        with pytest.raises(ui.UnownedWindow):
            win.read_text("Text editor")
        with pytest.raises(ui.UnownedWindow):
            win.click("Save")
        with pytest.raises(ui.UnownedWindow):
            win.title()
        with pytest.raises(ui.UnownedWindow):
            win.close()

    def test_owning_context_manager_scopes_and_releases(self, fake):
        fake.wins = [FakeWindow(id="w:1", title="X", app="App", pid=55)]
        assert not ui.is_owned(55)
        with ui.owning(55):
            assert ui.is_owned(55)
            assert ui.owned_window(app="App", **FAST).pid == 55
        assert not ui.is_owned(55)  # released on exit
        with pytest.raises(ui.NoOwnedWindows):
            ui.owned_window(app="App", **FAST)

    def test_unowned_window_is_not_an_abstention(self):
        # A refusal to touch someone else's window must fail loudly, never be
        # read as "cannot verify / nothing broken".
        assert ui.UnownedWindow not in ui.ABSTENTION_CONDITIONS
        assert ui.NoOwnedWindows not in ui.ABSTENTION_CONDITIONS
        assert not issubclass(ui.UnownedWindow, AssertionError)

    def test_unscoped_window_handle_is_not_ownership_checked(self, notepad):
        # Phase 1's window() path stays usable with nothing owned — it never
        # claimed ownership, so it must not start refusing now.
        win = ui.window(app="Notepad", **FAST)
        assert not ui.owned_pids()
        win.read_text("Text editor")  # no UnownedWindow


# ---------------------------------------------------------------------------
# Subtree-aware ownership (Phase 2F, issue #23): a re-exec'ing launcher (the
# Windows App Execution Alias shim being the concrete case — `python` re-execs
# the real interpreter as a CHILD process) means the window we need to attach
# to often does not carry the PID we launched. Owning a launched PID must also
# own its descendants' windows — but ancestry only ever WIDENS what counts as
# ours; a PID that is not actually a descendant of anything we own must still
# be refused exactly as before. That negative case is the one that matters.
# ---------------------------------------------------------------------------


class TestSubtreeOwnership:
    def _fake_tree(self, monkeypatch, parents: dict):
        """Stub ancestry.parent_pid against a fake {pid: parent_pid} process tree.

        Only the pids named in *parents* have a known parent; anything else
        (an unrelated process, or the root of a chain) reports ``None`` —
        exactly like the real Windows implementation when a PID has no
        further ancestry to report.
        """
        monkeypatch.setattr(ui._ancestry, "parent_pid", lambda pid: parents.get(pid))

    def test_direct_child_is_owned(self, monkeypatch):
        # owned parent (100) -> re-exec'd child (101): the exact shim shape.
        self._fake_tree(monkeypatch, {101: 100})
        ui.own(100)
        assert ui.is_owned(101)

    def test_grandchild_is_owned(self, monkeypatch):
        # owned parent (100) -> child (101) -> grandchild (102): proves the
        # walk goes more than one hop, not just the immediate child.
        self._fake_tree(monkeypatch, {101: 100, 102: 101})
        ui.own(100)
        assert ui.is_owned(102)

    def test_descendant_window_resolves_through_owned_window(self, fake, monkeypatch):
        self._fake_tree(monkeypatch, {101: 100, 102: 101})
        fake.wins = [FakeWindow(id="w:grandchild", title="App", app="App", pid=102)]
        ui.own(100)  # we only ever launched the parent PID

        win = ui.owned_window(app="App", **FAST)
        assert win.pid == 102

    def test_unrelated_pid_is_still_refused(self, fake, monkeypatch):
        # 200 has NO ancestry link to anything owned at all.
        self._fake_tree(monkeypatch, {})
        fake.wins = [
            FakeWindow(id="w:mine", title="Mine", app="App", pid=100),
            FakeWindow(id="w:unrelated", title="Unrelated", app="App", pid=200),
        ]
        ui.own(100)

        assert not ui.is_owned(200)
        with pytest.raises(ui.UnownedWindow) as exc:
            ui.owned_window(title="Unrelated", **FAST)
        assert "Unrelated" in str(exc.value)
        assert "200" in str(exc.value)

    def test_sibling_subtree_is_still_refused(self, fake, monkeypatch):
        # 201 has a REAL ancestry chain, but it roots at a DIFFERENT,
        # un-owned PID (999) — a sibling subtree. Having *some* ancestry must
        # not be enough; it has to chain up to something we actually own.
        self._fake_tree(monkeypatch, {201: 999})
        fake.wins = [
            FakeWindow(id="w:mine", title="Mine", app="App", pid=100),
            FakeWindow(id="w:sibling", title="Sibling", app="App", pid=201),
        ]
        ui.own(100)

        assert not ui.is_owned(201)
        with pytest.raises(ui.UnownedWindow) as exc:
            ui.owned_window(title="Sibling", **FAST)
        assert "Sibling" in str(exc.value)

    def test_ancestry_lookup_failure_refuses_rather_than_owns(self, monkeypatch):
        # A raising lookup must read as "can't tell", never "assume owned".
        def _boom(pid):
            raise OSError("process ancestry unavailable")

        monkeypatch.setattr(ui._ancestry, "parent_pid", _boom)
        ui.own(100)
        assert not ui.is_owned(101)

    def test_unbounded_chain_does_not_hang_or_resolve_to_owned(self, monkeypatch):
        # A chain that never reaches an owned pid (and never repeats) must
        # stop at _MAX_ANCESTRY_DEPTH and refuse, not spin forever.
        monkeypatch.setattr(ui._ancestry, "parent_pid", lambda pid: pid + 1)
        ui.own(100)
        assert not ui.is_owned(1)

    def test_owned_window_resolves_by_ownership_alone_no_criteria(self, fake, monkeypatch):
        # Mirrors what pytest_ui._wait_for_first_window does post-#23: own the
        # launched PID, then resolve with no app/title/pid criterion at all —
        # ownership scope alone still finds the re-exec'd child's window.
        self._fake_tree(monkeypatch, {2222: 1111})
        fake.wins = [FakeWindow(id="w:1", title="App", app="App", pid=2222)]
        with ui.owning(1111):
            win = ui.owned_window(**FAST)
        assert win.pid == 2222

    def test_exact_pid_ownership_still_works_unchanged(self, monkeypatch):
        # No ancestry at all needed for the direct-match case (#12 baseline).
        self._fake_tree(monkeypatch, {})
        ui.own(100)
        assert ui.is_owned(100)


class TestOwnedLiveness:
    """Issue #11: owned-window liveness must not pay a full windows() walk."""

    def test_successful_close_uses_scoped_reads_not_full_enumeration(self, notepad):
        ui.own(4242)
        win = ui.owned_window(app="Notepad", **FAST)
        notepad.windows_calls = 0  # count only the close() work
        win.close()
        assert notepad.wins == []
        assert notepad.windows_calls == 0, (
            f"close() enumerated all windows {notepad.windows_calls} times; "
            f"owned-window liveness must stay a scoped per-window read (#11)"
        )

    def test_modal_blocked_close_still_raises_without_full_enumeration(self, notepad):
        ui.own(4242)
        notepad.close_lies = True  # window stays; a modal blocks the close
        notepad.trees["w:1"].append(
            {"name": "Save changes?", "role": "dialog", "states": ["modal"]}
        )
        win = ui.owned_window(app="Notepad", **FAST)
        notepad.windows_calls = 0
        with pytest.raises(ui.ActionNotVerified) as exc:
            win.close()
        msg = str(exc.value)
        assert "still exists" in msg
        assert "Save changes?" in msg  # blocking dialog named from a scoped read
        assert notepad.windows_calls == 0, (
            "a blocked-close diagnosis must not walk every top-level window (#11)"
        )


# ---------------------------------------------------------------------------
# Precondition helpers (Phase 2D, issue #14): the reusable building blocks
# fixtures compose from. assert_owned fails LOUDLY; wait_until_ready ABSTAINS.
# ---------------------------------------------------------------------------


class TestAssertOwned:
    def test_passes_on_owned_raises_on_unowned_for_a_handle(self, notepad):
        # notepad fixture uses pid 4242.
        ui.own(4242)
        win = ui.owned_window(app="Notepad", **FAST)
        assert ui.assert_owned(win) == 4242  # owned handle -> passes, returns pid

        ui.disown(4242)
        with pytest.raises(ui.UnownedWindow) as exc:
            ui.assert_owned(win)
        assert "4242" in str(exc.value)  # names the offending pid

    def test_accepts_a_raw_pid_too(self, fake):
        fake.wins = [FakeWindow(id="w:1", title="X", app="App", pid=55)]
        ui.own(55)
        assert ui.assert_owned(55) == 55
        with pytest.raises(ui.UnownedWindow):
            ui.assert_owned(999)  # never owned

    def test_failure_is_loud_never_an_abstention_or_assertion(self):
        # THE point of assert_owned: "not ours to touch" must fail loudly. The
        # raised instance must not be swallowable as an abstention, nor by a
        # broad `except AssertionError`.
        with pytest.raises(ui.UnownedWindow) as exc:
            ui.assert_owned(12345)  # nothing owned
        assert type(exc.value) not in ui.ABSTENTION_CONDITIONS
        assert not isinstance(exc.value, AssertionError)


class TestWaitUntilReady:
    def test_returns_handle_when_already_interactive(self, notepad):
        ui.own(4242)
        win = ui.owned_window(app="Notepad", **FAST)
        assert ui.wait_until_ready(win) is win  # ready now -> returns immediately

    def test_blocks_then_returns_once_the_tree_fills(self, notepad):
        # The core promise: block while the window is not yet interactive, then
        # succeed once it renders — without failing in the meantime.
        ui.own(4242)
        win = ui.owned_window(app="Notepad", timeout=1.0, poll=0.01)
        real_tree = notepad.trees["w:1"]
        notepad.trees["w:1"] = []  # not interactive yet
        orig_elements = notepad.elements
        calls = {"n": 0}

        def delayed(window_id=None, **kwargs):
            calls["n"] += 1
            if calls["n"] >= 3 and window_id == "w:1":
                notepad.trees["w:1"] = real_tree  # becomes interactive mid-wait
            return orig_elements(window_id=window_id, **kwargs)

        notepad.elements = delayed
        assert ui.wait_until_ready(win) is win
        assert calls["n"] >= 3  # it actually had to wait, not pass on the first look

    def test_abstains_when_tree_stays_empty(self, fake):
        # Never-ready must be "cannot verify" (EmptyTree), not a fail.
        fake.wins = [FakeWindow(id="w:1", title="X", app="App", pid=77)]
        fake.trees["w:1"] = []
        ui.own(77)
        win = ui.owned_window(app="App", **FAST)
        with pytest.raises(ui.EmptyTree) as exc:
            ui.wait_until_ready(win)
        # abstention, not an assertion failure
        assert type(exc.value) in ui.ABSTENTION_CONDITIONS
        assert not isinstance(exc.value, AssertionError)

    def test_abstains_when_the_window_vanishes(self, notepad):
        ui.own(4242)
        win = ui.owned_window(app="Notepad", **FAST)
        notepad.wins = []  # closed before it ever became ready
        with pytest.raises(ui.WindowGone) as exc:
            ui.wait_until_ready(win)
        assert type(exc.value) in ui.ABSTENTION_CONDITIONS

    def test_disowned_handle_raises_unowned_not_abstention(self, notepad):
        # The ownership re-check must win: a lapsed claim is a hard safety
        # error that propagates immediately, never retried or abstained.
        ui.own(4242)
        win = ui.owned_window(app="Notepad", **FAST)
        ui.disown(4242)
        with pytest.raises(ui.UnownedWindow) as exc:
            ui.wait_until_ready(win)
        assert type(exc.value) not in ui.ABSTENTION_CONDITIONS

    # -- issue #24: content-aware readiness (lazy WebView2/Chromium tree) --

    def test_no_signal_treats_landmark_only_tree_as_ready_unchanged(self, fake):
        # Backward compatibility: with no signal, "ready" is exactly what it
        # was before this parameter existed — non-empty, even if the only
        # thing in the tree is a content-less landmark wrapper.
        fake.wins = [FakeWindow(id="w:1", title="X", app="App", pid=99)]
        fake.trees["w:1"] = [{"name": "", "role": "landmark", "states": []}]
        ui.own(99)
        win = ui.owned_window(app="App", **FAST)
        assert ui.wait_until_ready(win) is win

    def test_signal_blocks_past_landmark_only_tree_until_content_appears(self, notepad):
        # The real defect: WebView2/Chromium's a11y tree is lazy. The first
        # reads return only landmark wrappers + chrome (non-empty, but
        # nothing assertable); only after several reads does the real DOM
        # populate. A bare non-empty check would return on the first look and
        # be wrong; a content signal must keep polling until it actually
        # shows up.
        ui.own(4242)
        win = ui.owned_window(app="Notepad", timeout=1.0, poll=0.01)
        real_tree = notepad.trees["w:1"]
        notepad.trees["w:1"] = [{"name": "", "role": "landmark", "states": []}]
        orig_elements = notepad.elements
        calls = {"n": 0}

        def delayed(window_id=None, **kwargs):
            calls["n"] += 1
            if calls["n"] >= 5 and window_id == "w:1":
                notepad.trees["w:1"] = real_tree  # content finally renders
            return orig_elements(window_id=window_id, **kwargs)

        notepad.elements = delayed
        assert ui.wait_until_ready(win, signal="Save") is win
        assert calls["n"] >= 5  # blocked past the cold, landmark-only polls

    def test_signal_abstains_at_deadline_when_content_never_appears(self, notepad):
        # Never-ready-with-content must abstain (EmptyTree), not fail, and
        # not be confused with the tree-was-actually-empty case.
        ui.own(4242)
        win = ui.owned_window(app="Notepad", **FAST)
        notepad.trees["w:1"] = [{"name": "", "role": "landmark", "states": []}]
        with pytest.raises(ui.EmptyTree) as exc:
            ui.wait_until_ready(win, signal="Save")
        assert type(exc.value) in ui.ABSTENTION_CONDITIONS
        assert not isinstance(exc.value, AssertionError)
        assert "Save" in str(exc.value)

    def test_signal_accepts_a_callable_predicate_evaluated_fresh_each_poll(self, notepad):
        ui.own(4242)
        win = ui.owned_window(app="Notepad", timeout=1.0, poll=0.01)
        seen = {"n": 0}

        def ready_after_a_few_looks(handle):
            seen["n"] += 1  # only ever incremented by a *fresh* poll, never cached
            return seen["n"] >= 3

        assert ui.wait_until_ready(win, signal=ready_after_a_few_looks) is win
        assert seen["n"] >= 3

    def test_signal_predicate_raising_unowned_window_propagates_immediately(self, notepad):
        # Same safety/abstention split as the base loop: if the caller's own
        # predicate raises UnownedWindow, it must never be swallowed into a
        # retry or read as "not ready yet".
        ui.own(4242)
        win = ui.owned_window(app="Notepad", **FAST)

        def boom(handle):
            raise ui.UnownedWindow("predicate says no")

        with pytest.raises(ui.UnownedWindow):
            ui.wait_until_ready(win, signal=boom)


class TestResetToKnownState:
    def test_runs_reset_then_returns_a_ready_handle(self, notepad):
        ui.own(4242)
        win = ui.owned_window(app="Notepad", **FAST)
        win.set_value("Text editor", "leftover from a prior check", replace=True)

        def clear(w):
            w.set_value("Text editor", "", replace=True)

        result = ui.reset_to_known_state(win, clear)
        assert result is win
        assert win.read_text("Text editor") == ""  # prior check's residue is gone

    def test_abstains_if_the_window_is_not_ready_after_reset(self, notepad):
        # Inherits wait_until_ready's abstention: a reset that leaves the
        # window unready is "cannot verify", not a fail.
        ui.own(4242)
        win = ui.owned_window(app="Notepad", **FAST)

        def wipe(w):
            notepad.trees["w:1"] = []  # reset left nothing interactive

        with pytest.raises(ui.EmptyTree):
            ui.reset_to_known_state(win, wipe)


# ---------------------------------------------------------------------------
# Issue #37: DOM-read path (read_dom_text) — reads the actual DOM, abstains
# cleanly (never false-passes) when the target isn't a readable CDP-backed DOM
# ---------------------------------------------------------------------------


@pytest.fixture()
def react_app(fake):
    """A Chromium/Electron-style app with a *thin a11y tree* but a rich DOM.

    Mirrors the #37 gap: the a11y projection (``trees``) is role-less div-soup
    with no names to assert on, while the real DOM (``dom_trees``) carries the
    rendered text. The window id is a ``cdp:`` id, the shape touchpoint's window
    merge hands out for a CDP-backed app.
    """
    fake.wins = [FakeWindow(id="cdp:9222:t1", title="My React App", app="Electron", pid=7000)]
    # a11y tree: unnamed generic containers — nothing a name query can bind to.
    fake.trees["cdp:9222:t1"] = [
        {"name": "", "role": "section", "raw_role": "generic", "states": ["visible"]},
        {"name": "", "role": "section", "raw_role": "generic", "states": ["visible"]},
    ]
    # actual DOM: role-less <div>s whose text the a11y tree omits.
    fake.dom_trees["cdp:9222:t1"] = [
        {"name": "Total: 42", "role": "section", "raw_role": "div",
         "states": ["visible"], "value": "Total: 42"},
        {"name": "Checkout", "role": "button", "raw_role": "div",
         "states": ["visible", "enabled"], "value": "Checkout"},
    ]
    return fake


class TestDomRead:
    def test_reads_dom_text_a_thin_ax_tree_omits(self, react_app):
        ui.own(7000)
        win = ui.owned_window(app="Electron", **FAST)
        # The a11y path cannot see it — this is exactly the #37 gap.
        with pytest.raises(ui.ElementNotFound):
            win.read_text("Total")
        # The DOM path reads the real rendered content.
        assert win.read_dom_text("Total") == "Total: 42"
        assert win.read_dom_text("Checkout") == "Checkout"

    def test_abstains_when_touchpoint_raises_touchpointerror(self, react_app):
        # Not a CDP app: touchpoint raises its own base error; we abstain.
        react_app.dom_raises = TouchpointError("source='dom' is only supported for CDP apps")
        ui.own(7000)
        win = ui.owned_window(app="Electron", **FAST)
        with pytest.raises(ui.DomUnavailable):
            win.read_dom_text("Total")

    def test_abstains_when_cdp_backend_unavailable(self, react_app):
        # websocket-client missing entirely — BackendUnavailableError (a
        # TouchpointError subclass). Still an abstention, never a pass.
        react_app.dom_raises = BackendUnavailableError(
            backend="cdp", reason="source='dom' requires a CDP backend"
        )
        ui.own(7000)
        win = ui.owned_window(app="Electron", **FAST)
        with pytest.raises(ui.DomUnavailable):
            win.read_dom_text("Total")

    def test_abstains_on_empty_dom_walk(self, fake):
        # A native (non-CDP) window: the DOM walk returns nothing. Must abstain,
        # not silently succeed or read something misleading.
        fake.wins = [FakeWindow(id="w:1", title="Native", app="Notepad", pid=4242)]
        fake.trees["w:1"] = [{"name": "Save", "role": "button", "states": ["enabled"]}]
        # no dom_trees entry -> empty DOM walk
        ui.own(4242)
        win = ui.owned_window(app="Notepad", **FAST)
        with pytest.raises(ui.DomUnavailable):
            win.read_dom_text("Save")

    def test_missing_query_in_readable_dom_is_not_found_not_abstention(self, react_app):
        # DOM is readable but the queried element is genuinely absent: that is a
        # real "not there" (ElementNotFound), NOT an abstention — same as the
        # a11y read_text. A false abstention would hide a real missing element.
        ui.own(7000)
        win = ui.owned_window(app="Electron", **FAST)
        with pytest.raises(ui.ElementNotFound) as exc:
            win.read_dom_text("Nonexistent widget")
        assert not isinstance(exc.value, ui.DomUnavailable)

    def test_ambiguous_dom_match_raises_never_guesses(self, react_app):
        react_app.dom_trees["cdp:9222:t1"] = [
            {"name": "Item", "role": "section", "value": "Item one"},
            {"name": "Item", "role": "section", "value": "Item two"},
        ]
        ui.own(7000)
        win = ui.owned_window(app="Electron", **FAST)
        with pytest.raises(ui.AmbiguousElement):
            win.read_dom_text("Item")

    def test_reads_fresh_every_call(self, react_app):
        ui.own(7000)
        win = ui.owned_window(app="Electron", **FAST)
        assert win.read_dom_text("Total") == "Total: 42"
        # The live DOM changed (React re-rendered); a fresh walk must reflect it,
        # nothing cached across calls.
        react_app.dom_trees["cdp:9222:t1"][0]["value"] = "Total: 99"
        react_app.dom_trees["cdp:9222:t1"][0]["name"] = "Total: 99"
        assert win.read_dom_text("Total") == "Total: 99"

    def test_rechecks_ownership_and_refuses_after_disown(self, react_app):
        ui.own(7000)
        win = ui.owned_window(app="Electron", **FAST)
        assert win.read_dom_text("Total") == "Total: 42"
        ui.disown(7000)
        # Ownership is a safety error, NOT an abstention — must fail loudly.
        with pytest.raises(ui.UnownedWindow):
            win.read_dom_text("Total")

    def test_domunavailable_is_registered_as_an_abstention(self):
        from cyclaudes import abstain

        assert ui.DomUnavailable in ui.ABSTENTION_CONDITIONS
        assert ui.DomUnavailable in abstain.abstention_types()
        # Never catchable as an ordinary assertion failure.
        assert not issubclass(ui.DomUnavailable, AssertionError)
