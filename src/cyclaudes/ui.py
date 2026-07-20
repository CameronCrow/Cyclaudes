"""Discipline layer over touchpoint — makes four observed footguns unrepresentable.

Touchpoint reads the accessibility tree correctly; this module is *not* a
re-implementation of it. Its only job is to make the four failure modes
observed in the 2026-07-20 smoke test (see
``related-work/accessibility-tree-agent-tooling.md``) impossible to express
through the API:

1. **Action returns lie.** ``touchpoint.close_window()`` returned ``OK``
   while a modal silently blocked the close. Therefore no action here ever
   returns a success value — every action returns ``None``, and success is
   only ever established by re-reading the tree (either the action's own
   built-in re-verification, or an explicit ``assert_*`` call).
2. **Element IDs churn on tree mutation** (``uia22`` → ``uia52`` when a
   dialog opened). Therefore raw element IDs are unreachable through this
   API: every method takes a *name query* and resolves it against a fresh
   snapshot at call time. There is nothing for a caller to cache.
3. **``wait_for_window`` substring-matches and auto-activates** — it grabbed
   an unrelated pre-existing window. Therefore :func:`window` matches
   titles *exactly* (substring matching only via the explicit
   ``title_contains`` opt-in), never activates anything, and raises
   :class:`AmbiguousWindow` naming every candidate instead of guessing.
4. **State vocabulary must be discovered, not guessed** (``selected``
   matched nothing; the real states were ``checked,pressed``). Therefore
   states are opaque strings compared against whatever the tree reports,
   and every failed state assertion prints the element's *actual* states.

Portability rules (Cameron moves to macOS ~2026-08-03):

- No hardcoded state vocabulary — any string the tree reports is a valid
  state; assertions never validate against an enum.
- No hardcoded role names — the optional ``role`` filter is an opaque
  string compared against both the backend's unified role and its raw
  platform role.
- Backend element/window IDs are opaque handles. They are never parsed and
  never exposed.
- An **empty accessibility tree is a named, distinct condition**
  (:class:`EmptyTree`), because on macOS a missing TCC Accessibility grant
  yields an empty tree — which must surface as "cannot see anything", never
  as "nothing is broken". ``assert_gone`` in particular refuses to pass on
  an empty tree.

Abstention seam: this module does not define ``CannotVerify`` (issue #1
owns it). Instead, :data:`ABSTENTION_CONDITIONS` lists the exception types
that mean "the check could not be evaluated" (as opposed to "the check
failed"); the pytest integration layer maps those onto its abstention
outcome.
"""

from __future__ import annotations

import time

import touchpoint as _tp  # tests replace ui._tp with a fake; keep all calls on this alias

__all__ = [
    "ABSTENTION_CONDITIONS",
    "DEFAULT_POLL",
    "DEFAULT_TIMEOUT",
    "ActionNotVerified",
    "AmbiguousElement",
    "AmbiguousWindow",
    "ElementNotFound",
    "EmptyTree",
    "UIAssertionError",
    "UIError",
    "WindowGone",
    "WindowHandle",
    "WindowNotFound",
    "window",
]

#: Default settle window for assertions and verified actions, seconds.
DEFAULT_TIMEOUT = 5.0
#: Default re-snapshot interval while settling, seconds.
DEFAULT_POLL = 0.25


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class UIError(Exception):
    """Base for every error this layer raises."""


class WindowNotFound(UIError):
    """No open window matched the criteria. Lists what *is* open."""


class AmbiguousWindow(UIError):
    """More than one window matched. Names every candidate; never guesses."""


class WindowGone(UIError):
    """The window this handle resolved to no longer exists."""


class ElementNotFound(UIError):
    """No element in a fresh snapshot matched the name query."""


class AmbiguousElement(UIError):
    """More than one element matched the name query; never guesses."""


class EmptyTree(UIError):
    """The accessibility tree came back empty.

    This is a distinct *abstention* condition, not a pass and not an
    ordinary failure: an empty tree usually means the platform denied
    accessibility access (on macOS, a missing TCC Accessibility grant for
    the host process yields exactly this), or the app exposes no tree at
    all. Nothing can be verified — and "cannot see" must never read as
    "nothing is broken".
    """


class ActionNotVerified(UIError):
    """An action was dispatched but its effect never appeared in the tree.

    Raised instead of trusting the driver's own return value — the one
    thing this layer exists to never do.
    """


class UIAssertionError(UIError, AssertionError):
    """An explicit ``assert_*`` check failed against the observed tree."""


#: Exception types that mean "this check could not be evaluated" rather than
#: "this check failed". The pytest layer (issue #1) maps these onto its
#: abstention outcome (``CannotVerify``); this module deliberately does not
#: define that type itself.
ABSTENTION_CONDITIONS: tuple[type[UIError], ...] = (EmptyTree, WindowGone)


# ---------------------------------------------------------------------------
# Formatting helpers (never leak backend IDs)
# ---------------------------------------------------------------------------


def _states_of(el) -> tuple[str, ...]:
    """The element's states as opaque strings, exactly as the tree reports them."""
    return tuple(str(s) for s in getattr(el, "states", ()) or ())


def _describe_window(w) -> str:
    return f"title={w.title!r} app={w.app!r} pid={w.pid}"


def _describe_element(el) -> str:
    states = ", ".join(_states_of(el))
    role = str(getattr(el, "role", "") or "")
    return f"{el.name!r} (role={role}, states=[{states}])"


def _role_matches(el, role: str) -> bool:
    """Opaque role comparison against both unified and raw platform role."""
    unified = str(getattr(el, "role", "") or "")
    raw = str(getattr(el, "raw_role", "") or "")
    return role == unified or role == raw


# ---------------------------------------------------------------------------
# Window resolution
# ---------------------------------------------------------------------------


def window(
    *,
    app: str | None = None,
    title: str | None = None,
    title_contains: str | None = None,
    pid: int | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    poll: float = DEFAULT_POLL,
) -> "WindowHandle":
    """Resolve exactly one open window, or raise. Never activates, never guesses.

    Matching is deliberately strict: ``app`` is a case-insensitive *exact*
    match, ``title`` is a case-sensitive *exact* match, and substring title
    matching only happens through the explicit ``title_contains`` opt-in.
    (Touchpoint's own ``wait_for_window`` substring-matches and
    auto-activates, which grabbed an unrelated pre-existing window in the
    smoke test — that behavior is unreachable from here.)

    Args:
        app: Application name, case-insensitive exact match.
        title: Window title, exact match.
        title_contains: Case-insensitive substring of the title (explicit
            opt-in; still raises if more than one window matches).
        pid: Owning process id.
        timeout: Default settle window for this handle's assertions.
        poll: Default re-snapshot interval for this handle's assertions.

    Raises:
        WindowNotFound: nothing matched (message lists open windows).
        AmbiguousWindow: several matched (message names every candidate).
        EmptyTree: the platform reported no windows at all — likely a
            missing accessibility permission, not a healthy empty desktop.
        ValueError: no criteria given.
    """
    if app is None and title is None and title_contains is None and pid is None:
        raise ValueError(
            "window() needs at least one of app=, title=, title_contains=, pid= "
            "— refusing to pick a window arbitrarily"
        )

    wins = _tp.windows()
    if not wins:
        raise EmptyTree(
            "The platform reported zero open windows. This usually means "
            "accessibility access is denied (on macOS: System Settings > "
            "Privacy & Security > Accessibility for the host process), not "
            "that everything is fine."
        )

    def _matches(w) -> bool:
        if app is not None and str(w.app).lower() != app.lower():
            return False
        if title is not None and str(w.title) != title:
            return False
        if title_contains is not None and title_contains.lower() not in str(w.title).lower():
            return False
        if pid is not None and w.pid != pid:
            return False
        return True

    candidates = [w for w in wins if _matches(w)]

    criteria = ", ".join(
        f"{k}={v!r}"
        for k, v in (
            ("app", app),
            ("title", title),
            ("title_contains", title_contains),
            ("pid", pid),
        )
        if v is not None
    )
    if not candidates:
        listing = "\n  ".join(_describe_window(w) for w in wins[:40])
        raise WindowNotFound(
            f"No open window matches ({criteria}). Titles match exactly — "
            f"pass title_contains= for substring matching. Open windows:\n  {listing}"
        )
    if len(candidates) > 1:
        listing = "\n  ".join(_describe_window(w) for w in candidates)
        raise AmbiguousWindow(
            f"{len(candidates)} windows match ({criteria}); refusing to guess. "
            f"Candidates:\n  {listing}\n"
            f"Disambiguate with title= or pid=."
        )

    won = candidates[0]
    return WindowHandle(won.id, app=str(won.app), pid=won.pid, timeout=timeout, poll=poll)


# ---------------------------------------------------------------------------
# The handle
# ---------------------------------------------------------------------------


class WindowHandle:
    """A resolved window. All element access is by name, freshly, every time.

    Every method re-resolves its target against a brand-new snapshot at
    call time; nothing about the tree is cached between calls, so element
    ID churn cannot be observed — let alone relied on — through this class.

    Actions (:meth:`click`, :meth:`set_value`, :meth:`close`) return
    ``None`` always. Where an action has an intrinsic post-condition
    (``set_value``: the value; ``close``: the window is gone) it is
    re-verified from the tree before the call returns, and
    :class:`ActionNotVerified` is raised otherwise. ``click`` has no
    intrinsic post-condition — pair it with an ``assert_*`` call.
    """

    def __init__(self, window_id, *, app: str, pid: int, timeout: float, poll: float):
        # The backend window id is an opaque private handle: never parsed,
        # never exposed. (Element ids are never even stored.)
        self._window_id = window_id
        self.app = app
        self.pid = pid
        self._timeout = timeout
        self._poll = poll

    def __repr__(self) -> str:  # no backend ids in the repr either
        return f"WindowHandle(app={self.app!r}, pid={self.pid})"

    # -- Fresh reads -------------------------------------------------------

    def _require_window(self):
        for w in _tp.windows():
            if w.id == self._window_id:
                return w
        raise WindowGone(
            f"Window (app={self.app!r}, pid={self.pid}) no longer exists; "
            f"it may have been closed or replaced. Re-resolve with ui.window()."
        )

    def _snapshot(self):
        self._require_window()
        els = _tp.elements(window_id=self._window_id)
        if not els:
            raise EmptyTree(
                f"Accessibility tree for window (app={self.app!r}, pid={self.pid}) "
                f"is empty. This usually means accessibility access is denied "
                f"(on macOS: a missing TCC Accessibility grant yields exactly "
                f"this), or the app exposes no tree — either way, nothing can "
                f"be verified. Do not treat this as a pass."
            )
        return els

    def _resolve(self, query: str, *, role: str | None = None):
        """Resolve a name query against a fresh snapshot; raise rather than guess.

        Match rule: exact name, else unique case-insensitive exact, else
        unique case-insensitive substring. More than one candidate at the
        deciding stage raises :class:`AmbiguousElement`.
        """
        els = self._snapshot()
        pool = [el for el in els if role is None or _role_matches(el, role)]

        exact = [el for el in pool if el.name == query]
        ci = [el for el in pool if str(el.name).lower() == query.lower()]
        sub = [el for el in pool if query.lower() in str(el.name).lower()]

        for stage in (exact, ci, sub):
            if len(stage) == 1:
                return stage[0]
            if len(stage) > 1:
                listing = "\n  ".join(_describe_element(el) for el in stage)
                raise AmbiguousElement(
                    f"{len(stage)} elements match {query!r}"
                    + (f" (role={role!r})" if role else "")
                    + f"; refusing to guess. Candidates:\n  {listing}\n"
                    f"Use the full exact name or a role= filter."
                )

        named = [el.name for el in pool if str(el.name).strip()]
        listing = "\n  ".join(repr(n) for n in named[:40])
        raise ElementNotFound(
            f"No element matches {query!r}"
            + (f" (role={role!r})" if role else "")
            + f" in window (app={self.app!r}). Named elements present:\n  {listing}"
        )

    # -- Settle loop -------------------------------------------------------

    def _settle(self, check, on_fail, timeout: float | None):
        """Re-evaluate ``check`` against fresh snapshots until it holds.

        ``check`` returns ``(ok, actual)``. On timeout, ``on_fail(actual)``
        supplies the exception. An :class:`EmptyTree` (or
        :class:`WindowGone`) seen while settling is retried — trees can be
        transiently empty mid-mutation — but if it is still the standing
        condition at the deadline it is re-raised as itself, never
        converted into a pass *or* a plain assertion failure.
        """
        timeout = self._timeout if timeout is None else timeout
        deadline = time.monotonic() + timeout
        while True:
            abstain = None
            ok, actual = False, None
            try:
                ok, actual = check()
            except ABSTENTION_CONDITIONS as e:
                abstain = e
            if ok:
                return
            if time.monotonic() >= deadline:
                if abstain is not None:
                    raise abstain
                raise on_fail(actual)
            time.sleep(self._poll)

    # -- Plain reads (fresh every call, no handles returned) ---------------

    def title(self) -> str:
        """The window's current title, read fresh."""
        return str(self._require_window().title)

    def exists(self, query: str, *, role: str | None = None) -> bool:
        """Whether exactly one element currently matches ``query``.

        Ambiguity still raises — "exists ambiguously" is not a yes.
        """
        try:
            self._resolve(query, role=role)
            return True
        except ElementNotFound:
            return False

    def read_text(self, query: str, *, role: str | None = None) -> str:
        """The element's current text/value, read fresh from the tree."""
        el = self._resolve(query, role=role)
        text = _tp.get_text_content(el)
        if text is None:
            text = el.value if el.value is not None else ""
        return str(text)

    def states(self, query: str, *, role: str | None = None) -> tuple[str, ...]:
        """The element's current states as opaque strings, read fresh."""
        return _states_of(self._resolve(query, role=role))

    # -- Actions (always return None; success lives in the tree) -----------

    def click(self, query: str, *, role: str | None = None) -> None:
        """Click the element resolved fresh from ``query``. Returns ``None``.

        A click has no intrinsic post-condition, so none is invented: the
        driver's return value is discarded, and the caller states the
        expected outcome with a following ``assert_*`` call.
        """
        el = self._resolve(query, role=role)
        _tp.click(el)  # return value deliberately discarded
        return None

    def set_value(
        self,
        query: str,
        text: str,
        *,
        replace: bool = False,
        role: str | None = None,
        timeout: float | None = None,
    ) -> None:
        """Set the element's value, then verify it from a fresh snapshot.

        The driver's return value is discarded. With ``replace=True`` the
        re-read value must equal ``text``; with ``replace=False`` the
        written ``text`` must appear in the re-read value. If the tree
        never shows it within the settle window, :class:`ActionNotVerified`
        is raised with what the tree actually showed. Returns ``None``.
        """
        el = self._resolve(query, role=role)
        _tp.set_value(el, text, replace=replace)  # return value deliberately discarded

        def check():
            try:
                current = self.read_text(query, role=role)
            except ElementNotFound as e:
                return False, f"element no longer found: {e}"
            if replace:
                return current == text, current
            return (text in current if text else True), current

        def on_fail(actual):
            return ActionNotVerified(
                f"set_value({query!r}, {text!r}, replace={replace}) was "
                f"dispatched but the tree never showed it; observed value: "
                f"{actual!r}. The driver's own return value was ignored — "
                f"only the tree decides."
            )

        self._settle(check, on_fail, timeout)
        return None

    def close(self, *, timeout: float | None = None) -> None:
        """Close the window, then verify it is actually gone. Returns ``None``.

        This is the exact footgun observed live: ``close_window()``
        returned ``OK`` while a modal save-prompt silently blocked the
        close. The driver's return value is discarded; the window list is
        re-read until the window disappears, and :class:`ActionNotVerified`
        is raised — with any visible dialog-ish elements named — if it
        does not.
        """
        _tp.close_window(self._window_id)  # return value deliberately discarded
        timeout = self._timeout if timeout is None else timeout
        deadline = time.monotonic() + timeout
        while True:
            still = next((w for w in _tp.windows() if w.id == self._window_id), None)
            if still is None:
                return None
            if time.monotonic() >= deadline:
                break
            time.sleep(self._poll)

        # Best-effort diagnostic only (never a logic gate): name elements
        # whose observed role/states mention something dialog-like.
        hints = []
        try:
            for el in _tp.elements(window_id=self._window_id) or []:
                observed = " ".join(
                    (str(getattr(el, "role", "")), str(getattr(el, "raw_role", "")))
                    + _states_of(el)
                ).lower()
                if any(k in observed for k in ("modal", "dialog", "alert")):
                    hints.append(_describe_element(el))
        except Exception:
            pass
        hint_msg = (
            "\nPossibly blocking elements observed:\n  " + "\n  ".join(hints)
            if hints
            else ""
        )
        raise ActionNotVerified(
            f"close() was dispatched but window (app={self.app!r}, "
            f"pid={self.pid}, title={still.title!r}) still exists after "
            f"{timeout:.1f}s — a modal prompt may be blocking it. The "
            f"driver's own return value was ignored — only the window list "
            f"decides." + hint_msg
        )

    # -- Assertions (re-snapshot, settle, report actuals) -------------------

    def assert_text(
        self,
        query: str,
        expected: str,
        *,
        contains: bool = False,
        role: str | None = None,
        timeout: float | None = None,
    ) -> None:
        """Assert the element's text equals (or contains) ``expected``.

        Reads fresh from the tree with settle/retry; the failure message
        reports the actual observed text.
        """

        def check():
            try:
                current = self.read_text(query, role=role)
            except ElementNotFound as e:
                return False, f"<element not found: {e}>"
            ok = (expected in current) if contains else (current == expected)
            return ok, current

        def on_fail(actual):
            verb = "contain" if contains else "equal"
            return UIAssertionError(
                f"assert_text: expected {query!r} to {verb} {expected!r}, "
                f"but the tree shows {actual!r}"
            )

        self._settle(check, on_fail, timeout)

    def _assert_state(
        self,
        query: str,
        state: str,
        *,
        present: bool,
        role: str | None,
        timeout: float | None,
    ) -> None:
        def check():
            try:
                actual = self.states(query, role=role)
            except ElementNotFound as e:
                return False, (f"<element not found: {e}>",)
            return ((state in actual) is present), actual

        def on_fail(actual):
            shown = ", ".join(actual) if actual else "<none>"
            want = "to have" if present else "not to have"
            return UIAssertionError(
                f"assert_state: expected {query!r} {want} state {state!r}; "
                f"actual states: [{shown}]. State names are platform "
                f"vocabulary discovered from the tree — assert one of the "
                f"actual strings, not a guessed synonym."
            )

        self._settle(check, on_fail, timeout)

    def assert_state(
        self, query: str, state: str, *, role: str | None = None, timeout: float | None = None
    ) -> None:
        """Assert the element currently reports ``state``.

        ``state`` is an opaque string compared against whatever the tree
        reports — no vocabulary is validated or assumed. On failure the
        message lists the element's *actual* states so the correct
        platform vocabulary can be read off the error instead of guessed.
        """
        self._assert_state(query, state, present=True, role=role, timeout=timeout)

    def assert_not_state(
        self, query: str, state: str, *, role: str | None = None, timeout: float | None = None
    ) -> None:
        """Assert the element does *not* report ``state`` (actuals on failure)."""
        self._assert_state(query, state, present=False, role=role, timeout=timeout)

    def assert_exists(
        self, query: str, *, role: str | None = None, timeout: float | None = None
    ) -> None:
        """Assert exactly one element matching ``query`` is present."""

        def check():
            try:
                self._resolve(query, role=role)
                return True, None
            except ElementNotFound as e:
                return False, str(e)

        def on_fail(actual):
            return UIAssertionError(f"assert_exists: {actual}")

        self._settle(check, on_fail, timeout)

    def assert_gone(
        self, query: str, *, role: str | None = None, timeout: float | None = None
    ) -> None:
        """Assert no element matches ``query``.

        An empty tree does **not** count as gone: it raises
        :class:`EmptyTree`, because "I can't see anything" (e.g. a missing
        macOS Accessibility grant) must never satisfy an absence check.
        """

        def check():
            try:
                el = self._resolve(query, role=role)
            except ElementNotFound:
                return True, None
            except AmbiguousElement:
                return False, f"multiple elements still match {query!r}"
            return False, _describe_element(el)

        def on_fail(actual):
            return UIAssertionError(
                f"assert_gone: expected no element matching {query!r}, "
                f"but the tree still shows {actual}"
            )

        self._settle(check, on_fail, timeout)
