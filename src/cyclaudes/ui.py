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

PID-scoped window ownership (Phase 2A, issue #12):

An unattended run on Cameron's live desktop is the normal case, not the
exception. The smoke test proved the danger: ``wait_for_window("Notepad")``
substring-grabbed his real open log file, one ``type_text`` from corrupting
his work. So the layer tracks the PIDs of processes *it* launched (an
"owned set", :func:`own` / :func:`owning`) and offers a strictly safer
surface — :func:`owned_window` and :func:`owned_windows` — that will only
ever resolve, return, or enumerate windows in that set. A criteria match on
an *unowned* window raises :class:`UnownedWindow` (naming it) rather than
resolving to it, an empty owned set raises :class:`NoOwnedWindows` rather
than falling back to the whole desktop, and an owned :class:`WindowHandle`
re-checks ownership on every read and action so it cannot outlive its claim.
Ownership is pure PID-set membership — no titles, roles, or platform state
are involved — so it carries over unchanged to macOS PID semantics (Phase 5).

Abstention seam: this module does not define ``CannotVerify`` (issue #1
owns it). Instead, :data:`ABSTENTION_CONDITIONS` lists the exception types
that mean "the check could not be evaluated" (as opposed to "the check
failed"); the pytest integration layer maps those onto its abstention
outcome. Ownership refusals (:class:`UnownedWindow`, :class:`NoOwnedWindows`)
are deliberately *not* abstentions — they are safety errors that must fail
loudly, never be read as "nothing to verify".
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Callable, Iterator
from typing import NamedTuple

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
    "NoOwnedWindows",
    "OwnedWindow",
    "UIAssertionError",
    "UIError",
    "UnownedWindow",
    "WindowGone",
    "WindowHandle",
    "WindowNotFound",
    "assert_owned",
    "disown",
    "is_owned",
    "own",
    "owned_pids",
    "owned_window",
    "owned_windows",
    "owning",
    "reset_ownership",
    "reset_to_known_state",
    "wait_until_ready",
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


class UnownedWindow(UIError):
    """A targeted or matched window belongs to a PID this layer did not launch.

    The core Phase-2 safety property: an unattended run must never act on,
    return, or enumerate a window it does not own. Rather than resolve to the
    unowned window (the smoke-test footgun — grabbing Cameron's real log
    file), the ownership-scoped surface raises this and names the offender.
    Deliberately a hard error, not an abstention: "that isn't mine to touch"
    must fail loudly, never read as "nothing to verify".
    """


class NoOwnedWindows(UIError):
    """Ownership-scoped resolution/enumeration was asked for with nothing owned.

    Refusing to fall back to the full desktop is the point: with an empty
    owned set there is, by definition, no window this layer is allowed to
    touch. :func:`own` a launched process's PID first.
    """


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

# Wire the seam (issue #3): tell the abstention plugin to treat these as
# abstentions, so an empty tree or a vanished window surfaces as "cannot
# verify" rather than a plain failure. Importing abstain here is fine — the
# rule ui.py observes is that it must not *define* CannotVerify, not that it
# can't hand its conditions to the layer that owns it. abstain never imports
# ui back, so there is no cycle.
from . import abstain as _abstain  # noqa: E402  (kept next to what it wires)

_abstain.register_abstention_types(*ABSTENTION_CONDITIONS)


# ---------------------------------------------------------------------------
# Formatting helpers (never leak backend IDs)
# ---------------------------------------------------------------------------


def _val(x) -> str:
    """Canonical string for a role or state token.

    Touchpoint reports roles and states as ``enum.Enum`` members whose
    ``.value`` holds the portable vocabulary string (``State.CHECKED.value
    == "checked"``). Plain ``str(member)`` yields ``"State.CHECKED"`` — the
    wrong string, which silently breaks every state/role comparison against
    the real driver (the tests use plain-string fakes, so they never caught
    it). ``.value`` is also the *portable* choice: macOS AX reports these as
    plain strings, and ``getattr(x, "value", x)`` returns them unchanged.
    """
    return str(getattr(x, "value", x))


def _states_of(el) -> tuple[str, ...]:
    """The element's states as opaque strings, exactly as the tree reports them."""
    return tuple(_val(s) for s in getattr(el, "states", ()) or ())


def _describe_window(w) -> str:
    return f"title={w.title!r} app={w.app!r} pid={w.pid}"


def _describe_element(el) -> str:
    states = ", ".join(_states_of(el))
    role = _val(getattr(el, "role", "") or "")
    return f"{el.name!r} (role={role}, states=[{states}])"


def _role_matches(el, role: str) -> bool:
    """Opaque role comparison against both unified and raw platform role."""
    unified = _val(getattr(el, "role", "") or "")
    raw = _val(getattr(el, "raw_role", "") or "")
    return role == unified or role == raw


def _matches_window(w, app, title, title_contains, pid) -> bool:
    """Whether window ``w`` satisfies every supplied criterion (strict)."""
    if app is not None and str(w.app).lower() != app.lower():
        return False
    if title is not None and str(w.title) != title:
        return False
    if title_contains is not None and title_contains.lower() not in str(w.title).lower():
        return False
    if pid is not None and w.pid != pid:
        return False
    return True


def _criteria_str(app, title, title_contains, pid) -> str:
    return ", ".join(
        f"{k}={v!r}"
        for k, v in (
            ("app", app),
            ("title", title),
            ("title_contains", title_contains),
            ("pid", pid),
        )
        if v is not None
    )


# ---------------------------------------------------------------------------
# PID-scoped window ownership (Phase 2A, issue #12)
# ---------------------------------------------------------------------------

#: PIDs of processes this layer launched. The single source of truth for what
#: the ownership-scoped surface (:func:`owned_window`, :func:`owned_windows`,
#: and owned :class:`WindowHandle` re-checks) is permitted to touch. Managed
#: through :func:`own` / :func:`disown` / :func:`owning`; a launcher fixture
#: (issue #13) owns a PID right after spawning and disowns it at teardown.
_owned_pids: set[int] = set()


class OwnedWindow(NamedTuple):
    """A read-only descriptor for one owned window (no actions on it).

    Returned by :func:`owned_windows`. Actionable access is via
    :func:`owned_window`, which returns a live :class:`WindowHandle`.
    """

    app: str
    title: str
    pid: int


def own(pid: int) -> int:
    """Register ``pid`` as owned — a process this layer launched. Idempotent.

    Returns the PID for convenient inline use. From this point the
    ownership-scoped surface will resolve, return and enumerate that
    process's windows; nothing else becomes touchable through it.
    """
    pid = int(pid)
    _owned_pids.add(pid)
    return pid


def disown(pid: int) -> None:
    """Drop ``pid`` from the owned set. Idempotent.

    Any owned :class:`WindowHandle` for it immediately refuses further reads
    and actions (raising :class:`UnownedWindow`), so a handle cannot outlive
    the claim that justified touching it.
    """
    _owned_pids.discard(int(pid))


def is_owned(pid: int) -> bool:
    """Whether ``pid`` is currently in the owned set."""
    return int(pid) in _owned_pids


def owned_pids() -> frozenset[int]:
    """An immutable snapshot of the currently-owned PIDs."""
    return frozenset(_owned_pids)


def reset_ownership() -> None:
    """Forget every owned PID. A test/teardown seam; use :func:`owning` in code."""
    _owned_pids.clear()


@contextlib.contextmanager
def owning(pid: int) -> Iterator[int]:
    """Own ``pid`` for the duration of the block, disowning it on exit.

    The natural shape for a launcher fixture: own right after spawn, and
    guarantee the claim is dropped even if the check fails or errors::

        proc = launch(...)
        with ui.owning(proc.pid):
            win = ui.owned_window(app=...)
            ...
    """
    already = is_owned(pid)
    own(pid)
    try:
        yield int(pid)
    finally:
        if not already:
            disown(pid)


def owned_windows() -> list[OwnedWindow]:
    """Enumerate **only** owned windows; never lists one we did not launch.

    Raises:
        NoOwnedWindows: nothing is owned, so there is nothing this layer may
            enumerate (it refuses to fall back to the whole desktop).
    """
    if not _owned_pids:
        raise NoOwnedWindows(
            "owned_windows() called with an empty owned set — refusing to "
            "enumerate windows this layer did not launch. own() a launched "
            "process's PID first."
        )
    return [
        OwnedWindow(str(w.app), str(w.title), w.pid)
        for w in _tp.windows()
        if w.pid in _owned_pids
    ]


def owned_window(
    *,
    app: str | None = None,
    title: str | None = None,
    title_contains: str | None = None,
    pid: int | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    poll: float = DEFAULT_POLL,
) -> "WindowHandle":
    """Resolve exactly one **owned** window, or raise. The safe Phase-2 surface.

    Same strict matching as :func:`window`, but the candidate pool is scoped
    to owned PIDs *before* anything is chosen, so an unowned window can never
    be resolved. A criteria match that only hits an unowned window raises
    :class:`UnownedWindow` (naming it) instead of silently missing — that is
    the exact smoke-test footgun made loud.

    Raises:
        NoOwnedWindows: the owned set is empty.
        UnownedWindow: the criteria match only window(s) we did not launch.
        WindowNotFound: nothing matched among owned windows.
        AmbiguousWindow: several owned windows matched.
        EmptyTree: the platform reported no windows at all.
        ValueError: no criteria given.
    """
    if app is None and title is None and title_contains is None and pid is None:
        raise ValueError(
            "owned_window() needs at least one of app=, title=, title_contains=, "
            "pid= — refusing to pick a window arbitrarily"
        )
    if not _owned_pids:
        raise NoOwnedWindows(
            "owned_window() called with an empty owned set — refusing to "
            "resolve against windows this layer did not launch. own() a "
            "launched process's PID first."
        )

    wins = _tp.windows()
    if not wins:
        raise EmptyTree(
            "The platform reported zero open windows. This usually means "
            "accessibility access is denied (on macOS: System Settings > "
            "Privacy & Security > Accessibility for the host process), not "
            "that everything is fine."
        )

    criteria = _criteria_str(app, title, title_contains, pid)
    matched = [w for w in wins if _matches_window(w, app, title, title_contains, pid)]
    owned_matched = [w for w in matched if w.pid in _owned_pids]

    if not owned_matched:
        unowned = [w for w in matched if w.pid not in _owned_pids]
        if unowned:
            listing = "\n  ".join(_describe_window(w) for w in unowned)
            raise UnownedWindow(
                f"{len(unowned)} window(s) match ({criteria}) but belong to "
                f"PIDs this layer did not launch (owned={sorted(_owned_pids)}); "
                f"refusing to touch a window we did not open:\n  {listing}\n"
                f"If this process really is ours, own() its PID first."
            )
        owned_open = [w for w in wins if w.pid in _owned_pids]
        listing = "\n  ".join(_describe_window(w) for w in owned_open) or "<none open>"
        raise WindowNotFound(
            f"No owned window matches ({criteria}). Owned windows currently "
            f"open:\n  {listing}"
        )

    if len(owned_matched) > 1:
        listing = "\n  ".join(_describe_window(w) for w in owned_matched)
        raise AmbiguousWindow(
            f"{len(owned_matched)} owned windows match ({criteria}); refusing "
            f"to guess. Candidates:\n  {listing}\nDisambiguate with title= or pid=."
        )

    won = owned_matched[0]
    return WindowHandle(
        won.id, app=str(won.app), pid=won.pid, owned=True, timeout=timeout, poll=poll
    )


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

    candidates = [w for w in wins if _matches_window(w, app, title, title_contains, pid)]

    criteria = _criteria_str(app, title, title_contains, pid)
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

    def __init__(
        self,
        window_id,
        *,
        app: str,
        pid: int,
        timeout: float,
        poll: float,
        owned: bool = False,
    ):
        # The backend window id is an opaque private handle: never parsed,
        # never exposed. (Element ids are never even stored.)
        self._window_id = window_id
        self.app = app
        self.pid = pid
        self._timeout = timeout
        self._poll = poll
        # An owned handle re-checks PID ownership before every read and action
        # (see _check_owned), so it cannot be used after its process is
        # disowned. Handles from the unscoped window() are not owned and skip
        # the check — Phase 1's "app already open, manual precondition" path.
        self._owned = owned

    def __repr__(self) -> str:  # no backend ids in the repr either
        kind = "owned " if self._owned else ""
        return f"{kind}WindowHandle(app={self.app!r}, pid={self.pid})"

    # -- Fresh reads -------------------------------------------------------

    def _check_owned(self) -> None:
        """Refuse to touch the window if this owned handle's PID is no longer owned.

        A no-op for unscoped handles. For owned handles this is the guarantee
        that a handle can never outlive its claim: once :func:`disown` (or a
        fixture teardown) drops the PID, every further read and action raises
        rather than acting on a window the layer no longer owns. Pure set
        membership — no enumeration, no titles, portable to macOS PIDs.
        """
        if self._owned and self.pid not in _owned_pids:
            raise UnownedWindow(
                f"window (app={self.app!r}, pid={self.pid}) is no longer owned "
                f"(owned={sorted(_owned_pids)}); refusing to act on it. The "
                f"process was disowned or torn down — re-resolve via "
                f"owned_window() after own()-ing it again."
            )

    def _require_window(self):
        self._check_owned()
        for w in _tp.windows():
            if w.id == self._window_id:
                return w
        raise WindowGone(
            f"Window (app={self.app!r}, pid={self.pid}) no longer exists; "
            f"it may have been closed or replaced. Re-resolve with ui.window()."
        )

    def _snapshot(self):
        # Hot path: one window-scoped elements() read (~50ms). We deliberately
        # do NOT call _require_window() here — that enumerates every top-level
        # window (_tp.windows()), which costs seconds when many apps are open
        # and was making a single re-snapshot ~8-30s (second live-UI finding on
        # issue #5). Since ui.py re-snapshots after every action, that tax hit
        # every assertion. A scoped read is cheap and already returns empty
        # when the window is gone; only then do we pay for windows() to tell
        # WindowGone from a genuinely empty tree.
        self._check_owned()
        els = _tp.elements(window_id=self._window_id)
        if els:
            return els
        self._require_window()  # raises WindowGone if the window has vanished
        raise EmptyTree(
            f"Accessibility tree for window (app={self.app!r}, pid={self.pid}) "
            f"is empty. This usually means accessibility access is denied "
            f"(on macOS: a missing TCC Accessibility grant yields exactly "
            f"this), or the app exposes no tree — either way, nothing can "
            f"be verified. Do not treat this as a pass."
        )

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
        close. The driver's return value is discarded; the window is
        re-checked until it disappears, and :class:`ActionNotVerified`
        is raised — with any visible dialog-ish elements named — if it
        does not.

        Liveness here uses a **scoped** per-window element read, never a full
        ``_tp.windows()`` enumeration (issue #11). A busy desktop makes
        ``windows()`` cost seconds, so the old poll loop (one enumeration per
        ``poll`` interval) made the interval meaningless and a blocked-close
        diagnosis take ~8s. A window that has closed returns an *empty* scoped
        read; a window a modal is still blocking keeps that dialog in its tree,
        so the read stays *non-empty* — which is exactly the signal that
        distinguishes "closed" from "blocked" without walking the whole
        desktop. (Perms-denied "empty tree" is not a concern here: that would
        have failed every earlier read on this handle, long before close.)
        """
        self._check_owned()
        _tp.close_window(self._window_id)  # return value deliberately discarded
        timeout = self._timeout if timeout is None else timeout
        deadline = time.monotonic() + timeout
        while True:
            if not _tp.elements(window_id=self._window_id):
                return None  # scoped read empty -> the window is gone
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
            f"pid={self.pid}) still exists after {timeout:.1f}s — a modal "
            f"prompt may be blocking it. The driver's own return value was "
            f"ignored — only the tree decides." + hint_msg
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


# ---------------------------------------------------------------------------
# Precondition helpers (Phase 2D, issue #14)
# ---------------------------------------------------------------------------
#
# Small, boring building blocks that fixtures compose from. Each one is
# careful about *which side of the trust boundary a failure lands on*:
#
# - ``assert_owned`` is a **safety error**. "That isn't ours to touch" must
#   fail loudly (:class:`UnownedWindow`, a :class:`UIError`), never abstain and
#   never masquerade as an ordinary assertion failure — exactly the discipline
#   #12 established for the ownership-scoped surface.
# - ``wait_until_ready`` **abstains**. A window that never becomes interactive
#   means "could not verify", not "verified broken", so it re-raises the
#   standing abstention condition (:class:`EmptyTree` / :class:`WindowGone`)
#   rather than failing as an assertion.
#
# Both stay portable: ownership is pure PID-set membership and "ready" is the
# vocabulary-free "the tree has something in it", so no state/role strings are
# hardcoded (macOS AX and Windows UIA differ) — see the module portability rules.


def assert_owned(handle_or_pid) -> int:
    """Hard-assert the target belongs to a PID this layer launched.

    Accepts either a :class:`WindowHandle` (or any object exposing ``.pid``,
    e.g. an :class:`OwnedWindow`) or a raw integer PID, and is built directly
    on :func:`is_owned`, so it is pure PID-set membership — no titles, roles,
    or platform state — and carries over unchanged to macOS PID semantics.

    This is the reusable guard behind Phase-2 success criterion 4 ("attempting
    to act on an unowned window **raises**"). A fixture calls it right before
    handing a window to a check, or a check calls it before acting.

    On an unowned target it raises :class:`UnownedWindow` — deliberately the
    *same* loud failure mode as the ownership-scoped surface: a
    :class:`UIError`, **not** in :data:`ABSTENTION_CONDITIONS` and **not** an
    :class:`AssertionError` subclass. "That isn't ours to touch" must fail
    loudly, never be swallowed by a broad ``except AssertionError`` nor read as
    "nothing to verify". Returns the resolved PID for convenient inline use.

    Raises:
        UnownedWindow: the target's PID is not in the owned set.
    """
    pid = getattr(handle_or_pid, "pid", None)
    if pid is None:
        pid = int(handle_or_pid)
    if not is_owned(pid):
        raise UnownedWindow(
            f"assert_owned: pid {pid} is not owned "
            f"(owned={sorted(_owned_pids)}); refusing to treat a window this "
            f"layer did not launch as ours. own() its PID first if it really "
            f"is a process we launched. This is a safety error, not an "
            f"abstention — it must fail loudly."
        )
    return pid


def wait_until_ready(
    handle: "WindowHandle",
    *,
    signal: str | Callable[["WindowHandle"], bool] | None = None,
    timeout: float | None = None,
    poll: float | None = None,
) -> "WindowHandle":
    """Block until *handle*'s window is present and interactive, else **abstain**.

    "Ready" is defined portably, with no state/role vocabulary: the window
    still exists and its accessibility tree is *non-empty* — i.e. the app has
    rendered something to read or act on. (On macOS, an owned window whose tree
    is still empty is not "ready" either.)

    That default notion of "non-empty" is deliberately weak, and some
    embedded-web surfaces (WebView2/Chromium in particular) exploit the gap:
    the accessibility tree is lazy, so the very first read back returns only
    empty ``landmark`` wrappers and window chrome — no DOM — and stays that
    way until a deeper read engages Chromium accessibility. That tree is
    "non-empty" by the default check yet has nothing assertable in it, so the
    first assertion after launch would false-abstain. Pass ``signal`` to gate
    on real content instead: either the name of an element that must be
    present (checked via ``handle.exists(signal)``), or a predicate
    ``(handle) -> bool`` for anything the name-query vocabulary can't express.
    The predicate is re-evaluated against a fresh snapshot every poll — it is
    never cached, since readiness can only be observed by re-reading the tree.
    With no ``signal`` (the default), behaviour is unchanged from before this
    parameter existed: ready means only "non-empty, owned, live tree".

    This never fails-as-assertion. A window that never becomes ready is "could
    not verify", not "verified broken", so at the deadline it re-raises the
    standing abstention condition — :class:`EmptyTree` (nothing in the tree yet,
    accessibility access denied, or — with a ``signal`` — the tree stayed
    non-empty but the expected content never appeared) or :class:`WindowGone`
    (the window vanished before it settled). Both are in
    :data:`ABSTENTION_CONDITIONS`, so the pytest layer surfaces them as
    "cannot verify".

    Ownership is re-checked on every poll: the settle uses the handle's own
    fresh read, so a handle whose PID was disowned raises :class:`UnownedWindow`
    — a hard safety error that is *not* caught here (it is not an abstention),
    so it propagates immediately rather than being retried or swallowed. The
    same holds if a caller-supplied predicate itself raises
    :class:`UnownedWindow` — it is not one of :data:`ABSTENTION_CONDITIONS`, so
    it is never swallowed into a retry.

    ``timeout`` / ``poll`` default to the handle's own configured settle
    window. Returns the same handle on success, so it chains::

        win = ui.wait_until_ready(ui.owned_window(app="Notepad"))
        win = ui.wait_until_ready(ui.owned_window(app="LLT"), signal="Import")

    Raises:
        EmptyTree | WindowGone: the window never became ready (abstention).
        UnownedWindow: the handle's PID is no longer owned (safety error).
    """
    timeout = handle._timeout if timeout is None else timeout
    poll = handle._poll if poll is None else poll
    deadline = time.monotonic() + timeout

    def _content_ready() -> bool:
        # handle._snapshot() above already established a non-empty, owned,
        # live tree; this only decides whether *this* non-empty tree counts
        # as ready. Re-checked fresh every call (via handle.exists() /
        # handle._snapshot(), never cached) since a landmark-only cold tree
        # and a populated one are both "non-empty".
        if signal is None:
            return True
        if callable(signal):
            return bool(signal(handle))
        return handle.exists(signal)

    while True:
        abstain = None
        ready = False
        try:
            # A non-empty scoped read is the base readiness signal; _snapshot()
            # raises EmptyTree/WindowGone when not ready and UnownedWindow
            # (which we deliberately do NOT catch) when the claim has lapsed.
            handle._snapshot()
            ready = _content_ready()
        except ABSTENTION_CONDITIONS as e:
            abstain = e
        if ready:
            return handle
        if time.monotonic() >= deadline:
            if abstain is not None:
                raise abstain  # still not ready at the deadline -> abstain, never fail
            raise EmptyTree(
                f"Window (app={handle.app!r}, pid={handle.pid}) has a "
                f"non-empty accessibility tree but the readiness signal "
                f"{signal!r} never appeared before the {timeout}s deadline. "
                f"This usually means the app's content is still loading (a "
                f"lazy WebView2/Chromium tree, for instance) or the signal "
                f"names the wrong element. Do not treat this as a pass."
            )
        time.sleep(poll)


def reset_to_known_state(
    handle: "WindowHandle",
    reset,
    *,
    timeout: float | None = None,
    poll: float | None = None,
) -> "WindowHandle":
    """Run an app-specific *reset*, returning the handle once it is ready again.

    A **convention, not a framework**. Navigation and clearing are inherently
    app-specific (Phase 2 keeps navigation in per-check code), so the reset
    step itself is the caller's callable, invoked as ``reset(handle)``. This
    wrapper only supplies the surrounding discipline — run the reset, then
    block until the window is interactive again — so one check's leftovers
    cannot bleed into the next.

    Ownership needs no separate gate here: any action *reset* performs goes
    through the handle, which re-checks ownership on every call, and the
    trailing :func:`wait_until_ready` re-checks it once more. Readiness after
    reset therefore abstains (never fails-as-assertion) if the window does not
    settle, and raises :class:`UnownedWindow` if the claim has lapsed.

    Returns the handle, so a fixture can ``return reset_to_known_state(win, ...)``.
    """
    reset(handle)
    return wait_until_ready(handle, timeout=timeout, poll=poll)
