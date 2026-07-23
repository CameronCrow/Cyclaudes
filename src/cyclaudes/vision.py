"""Phase 4 — vision fallback: assert on what the accessibility tree cannot encode.

The tree will happily report a button as *present and enabled* while it renders
behind a modal, off-screen, clipped out of the viewport, or painted blank. Those
are real defect classes structural checks pass silently (see
``planning/PHASE_4.md``). This module is the disciplined pixel path for exactly
those gaps — and nothing else. Structural verification stays the default; vision
is opt-in, per assertion.

Discipline carried over from :mod:`cyclaudes.ui`:

1. **Owned-only.** Capture goes through a :class:`~cyclaudes.ui.WindowHandle`,
   which re-checks PID ownership on every read. We never screenshot a window we
   did not launch. A lapsed claim raises :class:`~cyclaudes.ui.UnownedWindow`
   (a loud safety error), never an abstention.
2. **Abstain, never false-pass.** "Could not get the pixels" (no capture backend,
   a zero-area region, a headless host) is a *distinct abstention*
   (:class:`CaptureUnavailable`), wired into the same abstention seam as an empty
   tree — because "I couldn't see it" must never read as "it looks fine". Only a
   genuinely observed defect (a region that *is* blank) fails as an assertion.
3. **Deterministic over model judgment.** This first slice asks a single
   pre-declared, deterministic question — "did this region paint anything, or is
   it a flat blank?" — decided by pixel statistics, not a vision model. Per the
   phase's key decision, model judgment is reserved for genuinely novel states
   and defaults to abstain; it is deliberately absent here.

The pixels come from ``touchpoint.screenshot`` (returns a ``PIL.Image``); this
module's only job is the wrapper that makes the three footguns above
unrepresentable, mirroring what :mod:`cyclaudes.ui` does for the tree.
"""

from __future__ import annotations

import touchpoint as _tp  # tests replace vision._tp with a fake; keep all calls on this alias

from . import abstain as _abstain
from . import ui as _ui

__all__ = [
    "DEFAULT_FLAT_TOLERANCE",
    "CaptureUnavailable",
    "assert_rendered",
    "capture",
    "is_flat",
]

#: Per-channel span (max − min, on a 0–255 scale) at or below which a region is
#: judged flat/unpainted. Not zero: real captures carry a few levels of noise at
#: sub-pixel/anti-aliased edges even over a "solid" fill, and a hard ``== 0``
#: would make the check flap. A handful of levels is safely below any region
#: that actually rendered text, an icon, or a gradient.
DEFAULT_FLAT_TOLERANCE = 6


class CaptureUnavailable(_ui.UIError):
    """The pixels for a region could not be captured — an **abstention**.

    Distinct from "the region is blank" (which is an ordinary assertion
    *failure*): this means the capture itself did not happen — no screenshot
    backend, a headless/no-display host, or a resolved region with zero area.
    Nothing was observed, so nothing can be asserted, and "could not see" must
    never be reported as a pass. Registered into
    :data:`cyclaudes.ui.ABSTENTION_CONDITIONS`' seam below so the pytest layer
    surfaces it as "cannot verify", exactly like :class:`~cyclaudes.ui.EmptyTree`.
    """


# Wire the abstention seam, same pattern ui.py uses for EmptyTree/WindowGone:
# a capture we couldn't take is "cannot verify", not "verified fine".
_abstain.register_abstention_types(CaptureUnavailable)


def _extrema_span(img) -> int:
    """Largest per-channel span (max − min) across the region, 0–255.

    A flat/unpainted region has near-identical pixels, so every channel's span
    is ~0; anything that rendered content (text, an icon, a gradient, a border)
    pushes at least one channel's span well up. Uses ``getextrema`` — exact on
    the lossless PNG touchpoint returns, and O(pixels) in C, not Python.
    """
    rgb = img.convert("RGB")
    extrema = rgb.getextrema()  # ((rmin,rmax),(gmin,gmax),(bmin,bmax))
    return max(hi - lo for lo, hi in extrema)


def is_flat(img, *, tolerance: int = DEFAULT_FLAT_TOLERANCE) -> bool:
    """Whether *img* is a flat, essentially single-colour region (deterministic).

    ``True`` means no channel varies by more than ``tolerance`` levels across
    the whole image — a blank/white/unpainted region. Pure pixel statistics; no
    model, no baseline. Separated out (and exported) so the decision is unit-
    testable without a live capture.
    """
    return _extrema_span(img) <= tolerance


def capture(
    handle: "_ui.WindowHandle",
    query: str | None = None,
    *,
    role: str | None = None,
    padding: int = 0,
):
    """Capture pixels for an owned window, or one element within it.

    With no ``query``, captures the whole owned window. With a ``query``, the
    element is resolved *fresh* against the current tree (same name-query
    discipline and ambiguity rules as :class:`~cyclaudes.ui.WindowHandle` — an
    ID is never cached or exposed) and only its bounding box is captured, with
    optional ``padding`` pixels around it.

    Ownership is enforced before any pixels are read: the handle's own fresh
    read (``_resolve`` / ``_require_window``) re-checks the PID claim and raises
    :class:`~cyclaudes.ui.UnownedWindow` if it has lapsed — a hard safety error,
    not caught here. Element resolution failures (:class:`ElementNotFound`,
    :class:`AmbiguousElement`) propagate unchanged: "capture *that* element"
    with no unambiguous target is a caller error, not something to guess past.

    Returns a ``PIL.Image``. Raises :class:`CaptureUnavailable` (an abstention)
    if the screenshot backend yields nothing or the region has zero area —
    never a blank placeholder image that a downstream check might read as real.

    Raises:
        UnownedWindow: the handle's PID is no longer owned (safety error).
        ElementNotFound | AmbiguousElement: ``query`` did not resolve to one element.
        CaptureUnavailable: pixels could not be obtained (abstention).
    """
    if query is None:
        # _require_window re-checks ownership and that the window still exists
        # (raising WindowGone — itself an abstention — if it vanished).
        handle._require_window()
        img = _tp.screenshot(window_id=handle._window_id)
        scope = f"window (app={handle.app!r}, pid={handle.pid})"
    else:
        el = handle._resolve(query, role=role)  # fresh resolve; re-checks ownership
        img = _tp.screenshot(element=el, padding=padding)
        scope = f"element {query!r} in window (app={handle.app!r}, pid={handle.pid})"

    if img is None:
        raise CaptureUnavailable(
            f"No pixels captured for {scope}: the screenshot backend returned "
            f"nothing. This usually means there is no display/capture backend "
            f"available (a headless host), not that the region is fine. Do not "
            f"treat this as a pass."
        )
    if not (getattr(img, "width", 0) and getattr(img, "height", 0)):
        raise CaptureUnavailable(
            f"Captured a zero-area region for {scope} (size "
            f"{getattr(img, 'size', None)!r}) — the element is likely off-screen, "
            f"collapsed, or not laid out yet. Nothing to look at; cannot verify."
        )
    return img


def assert_rendered(
    handle: "_ui.WindowHandle",
    query: str | None = None,
    *,
    role: str | None = None,
    padding: int = 0,
    tolerance: int = DEFAULT_FLAT_TOLERANCE,
) -> None:
    """Assert a region actually painted content — not a flat blank/white fill.

    The structural gap this closes: the tree reports an element present and
    enabled, but it rendered blank (a failed web view, an unpainted canvas, a
    component that mounted but never drew). Captures the region and fails
    (:class:`~cyclaudes.ui.UIAssertionError`) if it is essentially a single
    flat colour; passes if it varies. Deterministic — no model, no baseline.

    A capture that cannot be taken **abstains** (:class:`CaptureUnavailable`)
    rather than failing: "couldn't see it" is not "it's broken". This keeps the
    phase's safety property — a false "rendered" is worse than the stall it
    replaces.

    ``tolerance`` is the per-channel span below which the region counts as flat;
    raise it for regions expected to be near-uniform, lower it to be stricter.

    Raises:
        UIAssertionError: the region is flat/blank (an observed defect).
        CaptureUnavailable | WindowGone: could not capture (abstention).
        UnownedWindow: the handle's PID is no longer owned (safety error).
    """
    img = capture(handle, query, role=role, padding=padding)
    span = _extrema_span(img)
    if span <= tolerance:
        target = f"{query!r}" if query is not None else "the window"
        raise _ui.UIAssertionError(
            f"assert_rendered: {target} (app={handle.app!r}) captured as a flat "
            f"{img.width}x{img.height} region — max per-channel span {span} ≤ "
            f"tolerance {tolerance}, i.e. essentially a single colour. The "
            f"accessibility tree reports it present, but it painted blank "
            f"(white/unrendered). This is a real defect the structural check "
            f"cannot see."
        )
