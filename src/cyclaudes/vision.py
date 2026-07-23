"""Phase 4 — vision fallback: assert on what the accessibility tree cannot encode.

The tree will happily report a button as *present and enabled* while it renders
behind a modal, off-screen, clipped out of the viewport, or painted blank. Those
are real defect classes structural checks pass silently (see
``planning/PHASE_4.md``). This module is the disciplined pixel/geometry path for
exactly those gaps — and nothing else. Structural verification stays the default;
vision is opt-in, per assertion.

Discipline carried over from :mod:`cyclaudes.ui`:

1. **Owned-only.** Everything goes through a :class:`~cyclaudes.ui.WindowHandle`,
   which re-checks PID ownership on every read. We never screenshot or hit-test a
   window we did not launch. A lapsed claim raises
   :class:`~cyclaudes.ui.UnownedWindow` (a loud safety error), never an abstention.
2. **Abstain, never false-pass.** "Could not get the pixels / could not measure
   the geometry / have no baseline yet" is a *distinct abstention*
   (:class:`VisionAbstention` and its subclasses), wired into the same abstention
   seam as an empty tree — because "I couldn't see it" must never read as "it
   looks fine". Only a genuinely observed defect (a region that *is* blank, an
   element that *is* occluded / clipped / changed) fails as an assertion.
3. **Deterministic over model judgment.** Every assertion here answers a single
   pre-declared, deterministic question decided by pixels or geometry — did this
   region paint anything? is it inside its window? is something on top of it?
   does it still match its approved baseline? — never open-ended model judgment.
   Per the phase's key decision, model judgment is reserved for genuinely novel
   states and defaults to abstain; it is deliberately absent here.

Routing rule (structural → vision). Structural checks stay the cheap default.
Escalate to a vision assertion *only* for a property the tree structurally cannot
encode (blank render, occlusion, clipping, pixel regression), and do it explicitly
per assertion — vision is slower and costlier, so it is never the default path.
:func:`assert_visible` is that rule made concrete: it runs the cheap structural
gate first and only pays for each more-expensive vision check if the cheaper one
passed.

The pixels come from ``touchpoint.screenshot`` (returns a ``PIL.Image``) and the
geometry from each element's/window's ``position``/``size`` plus
``touchpoint.element_at`` (a hit-test); this module's only job is the wrapper that
makes the footguns above unrepresentable, mirroring what :mod:`cyclaudes.ui` does
for the tree.
"""

from __future__ import annotations

import os
from pathlib import Path

import touchpoint as _tp  # tests replace vision._tp with a fake; keep all calls on this alias
from PIL import Image, ImageChops

from . import abstain as _abstain
from . import ui as _ui

__all__ = [
    "DEFAULT_BASELINE_TOLERANCE",
    "DEFAULT_FLAT_TOLERANCE",
    "DEFAULT_PER_PIXEL_TOLERANCE",
    "REBASELINE_ENV",
    "BaselineUnavailable",
    "CaptureUnavailable",
    "GeometryUnavailable",
    "VisionAbstention",
    "assert_matches_baseline",
    "assert_not_occluded",
    "assert_rendered",
    "assert_visible",
    "assert_within_viewport",
    "capture",
    "is_flat",
]

#: Per-channel span (max − min, on a 0–255 scale) at or below which a region is
#: judged flat/unpainted. Not zero: real captures carry a few levels of noise at
#: sub-pixel/anti-aliased edges even over a "solid" fill, and a hard ``== 0``
#: would make the check flap. A handful of levels is safely below any region
#: that actually rendered text, an icon, or a gradient.
DEFAULT_FLAT_TOLERANCE = 6

#: Fraction of pixels (0–1) allowed to differ from a baseline before
#: :func:`assert_matches_baseline` fails. Small but non-zero: cursor blink,
#: sub-pixel AA and font hinting jitter a handful of pixels between otherwise
#: identical renders.
DEFAULT_BASELINE_TOLERANCE = 0.005

#: A single pixel counts as "changed" only if some channel differs from the
#: baseline by more than this (0–255). Filters imperceptible noise so it doesn't
#: inflate the changed-pixel fraction.
DEFAULT_PER_PIXEL_TOLERANCE = 16

#: Set this env var (to anything non-empty) to *write* baselines instead of
#: comparing against them — the explicit, opt-in re-baseline step. A run with it
#: set never passes: it abstains, because nothing was verified.
REBASELINE_ENV = "CYCLAUDES_REBASELINE"


class VisionAbstention(_ui.UIError):
    """Base for every "could not verify visually" condition — an **abstention**.

    Distinct from an ordinary assertion *failure* (the region is blank / the
    element is occluded / clipped / changed): these mean the check could not be
    *evaluated* at all. Nothing was observed, so nothing can be asserted, and
    "could not see" must never be reported as a pass. Every subclass is
    registered into the abstention seam below so the pytest layer surfaces it as
    "cannot verify", exactly like :class:`~cyclaudes.ui.EmptyTree`.
    """


class CaptureUnavailable(VisionAbstention):
    """Pixels could not be captured — no screenshot backend, or a zero-area region."""


class GeometryUnavailable(VisionAbstention):
    """An element's/window's geometry could not be measured for a spatial check.

    Missing ``position``/``size`` on the tree node, or a hit-test that landed on
    nothing — either way there is no measurement to assert on, so it abstains
    rather than guessing the element is fine (or broken).
    """


class BaselineUnavailable(VisionAbstention):
    """No baseline to compare against — one was just written; re-run to verify.

    Raised both when no baseline existed yet (a first run creates it) and when
    :data:`REBASELINE_ENV` is set (an explicit re-baseline). Neither actually
    verified anything, so both abstain — a freshly written baseline must never
    be reported as a pass against itself.
    """


# Wire the abstention seam, same pattern ui.py uses for EmptyTree/WindowGone:
# a visual check we couldn't evaluate is "cannot verify", not "verified fine".
_abstain.register_abstention_types(
    CaptureUnavailable, GeometryUnavailable, BaselineUnavailable
)


# ---------------------------------------------------------------------------
# Deterministic pixel primitives
# ---------------------------------------------------------------------------


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


def _changed_fraction(base, current, per_pixel: int) -> tuple[float, int]:
    """Fraction of pixels that differ beyond ``per_pixel``, and the max deviation.

    Deterministic and numpy-free: per-pixel max-channel difference (via
    ``ImageChops.lighter`` folding the three channels) thresholded to a 0/255
    mask, then counted from the histogram. Returns ``(fraction, max_deviation)``
    over two same-size RGB images.
    """
    diff = ImageChops.difference(base, current)
    r, g, b = diff.split()
    max_channel = ImageChops.lighter(ImageChops.lighter(r, g), b)  # mode "L"
    mask = max_channel.point(lambda p: 255 if p > per_pixel else 0)
    changed = mask.histogram()[255]
    total = base.width * base.height or 1
    max_dev = max_channel.getextrema()[1]
    return changed / total, max_dev


# ---------------------------------------------------------------------------
# Geometry primitives
# ---------------------------------------------------------------------------


def _rect(obj):
    """``(x, y, w, h)`` for a tree node/window, or ``None`` if unmeasurable."""
    pos = getattr(obj, "position", None)
    size = getattr(obj, "size", None)
    if not pos or not size:
        return None
    (x, y), (w, h) = pos, size
    if w <= 0 or h <= 0:
        return None
    return (int(x), int(y), int(w), int(h))


def _contains(outer, inner, tol: int) -> bool:
    """Whether ``inner`` rect sits within ``outer`` rect, allowing ``tol`` px slack."""
    ox, oy, ow, oh = outer
    ix, iy, iw, ih = inner
    return (
        ix >= ox - tol
        and iy >= oy - tol
        and ix + iw <= ox + ow + tol
        and iy + ih <= oy + oh + tol
    )


def _point_in(rect, x: int, y: int) -> bool:
    """Whether screen point ``(x, y)`` falls inside ``rect`` (x, y, w, h)."""
    rx, ry, rw, rh = rect
    return rx <= x <= rx + rw and ry <= y <= ry + rh


# ---------------------------------------------------------------------------
# Capture
# ---------------------------------------------------------------------------


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
    :class:`AmbiguousElement`) propagate unchanged.

    Returns a ``PIL.Image``. Raises :class:`CaptureUnavailable` (an abstention)
    if the screenshot backend yields nothing or the region has zero area —
    never a blank placeholder image that a downstream check might read as real.
    """
    if query is None:
        handle._require_window()  # re-checks ownership + WindowGone
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


# ---------------------------------------------------------------------------
# Assertions
# ---------------------------------------------------------------------------


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
    rather than failing: "couldn't see it" is not "it's broken".

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


def assert_within_viewport(
    handle: "_ui.WindowHandle",
    query: str,
    *,
    role: str | None = None,
    tolerance: int = 0,
) -> None:
    """Assert an element's box lies within its window — not clipped/off-screen.

    The structural gap: the tree reports an element present with a position,
    but that position is partly or wholly outside the window's bounds (scrolled
    off, clipped by an overflow container, laid out past the edge). Pure
    geometry from the tree — no capture. Fails if the element's rect is not
    contained in the window's rect (with ``tolerance`` px of slack for borders).

    Abstains (:class:`GeometryUnavailable`) if either rect can't be measured —
    a node without ``position``/``size`` tells us nothing, so we don't guess.

    Raises:
        UIAssertionError: the element extends outside its window (an observed defect).
        GeometryUnavailable | WindowGone: geometry unavailable (abstention).
        UnownedWindow: the handle's PID is no longer owned (safety error).
    """
    win = handle._require_window()  # re-checks ownership + WindowGone
    el = handle._resolve(query, role=role)
    win_rect, el_rect = _rect(win), _rect(el)
    if win_rect is None or el_rect is None:
        raise GeometryUnavailable(
            f"assert_within_viewport: cannot measure geometry for {query!r} "
            f"(element rect={el_rect}, window rect={win_rect}) in window "
            f"(app={handle.app!r}). The tree reported no usable position/size, "
            f"so containment can't be decided — abstaining, not passing."
        )
    if not _contains(win_rect, el_rect, tolerance):
        raise _ui.UIAssertionError(
            f"assert_within_viewport: {query!r} (app={handle.app!r}) has box "
            f"{el_rect} which is not contained within its window {win_rect} "
            f"(tolerance {tolerance}px). It is clipped or off-screen — present "
            f"in the tree but not actually visible."
        )


def assert_not_occluded(
    handle: "_ui.WindowHandle",
    query: str,
    *,
    role: str | None = None,
) -> None:
    """Assert nothing is painted on top of an element's centre (a hit-test).

    The structural gap: the tree reports an element present and enabled while a
    modal, an overlay, a tooltip, or another **window** covers it — so a user
    (or a click) can't actually reach it. Reads the element's rect, hit-tests
    its centre with ``touchpoint.element_at``, and classifies what is topmost.

    What it reliably decides, and how — established by the 2026-07-23 LLT
    (WebView2, high-DPI) dogfood, which showed ``element_at`` is far less
    trustworthy than it looks on embedded-web/high-DPI surfaces:

    1. **Trust guard.** ``element_at`` is supposed to return the element *at*
       the point, but on the WebView2 dogfood it returned a node from another
       Chromium process whose bounds did **not** even contain the queried point
       (a coordinate/DPI mismatch). So if the hit's rect does not contain the
       centre, the hit-test is untrustworthy here → **abstain**, never decide.
    2. **Topmost is our element or a child inside it** → not occluded (the
       healthy case). Decided by geometry, since element IDs churn across
       touchpoint reads and can't be chained.
    3. **Topmost belongs to a process we do not own** (and really is on the
       point) → **occluded**, fail loudly. This is the high-value, unambiguous
       case: another app's window or an OS dialog drawn over ours — exactly what
       structural verification cannot see.
    4. **Topmost is an owned/same-process node that isn't inside our element**
       (a wrapper, a CDP-namespace node, a same-app overlay) → **abstain**. On a
       nested DOM this is usually a benign container, but it is indistinguishable
       *by geometry alone* from a real same-window overlay, so abstaining is the
       only choice that neither false-fails nor false-passes.

    ponytail: single centre-point hit-test; partial-edge occlusion that spares
    the centre isn't caught, and same-process/DOM overlays abstain rather than
    fail. A CDP/DOM z-order query (issue #37) is the robust upgrade for
    same-window web overlays; multi-point sampling for edge occlusion. The one
    thing it asserts hard — a *foreign process* painted over the element — is the
    part that's actually reliable.

    Raises:
        UIAssertionError: a foreign-process window is on top of the element (an observed defect).
        GeometryUnavailable | WindowGone: cannot measure / trust the hit-test / decide (abstention).
        UnownedWindow: the handle's PID is no longer owned (safety error).
    """
    el = handle._resolve(query, role=role)  # fresh resolve; re-checks ownership
    el_rect = _rect(el)
    if el_rect is None:
        raise GeometryUnavailable(
            f"assert_not_occluded: {query!r} (app={handle.app!r}) has no usable "
            f"position/size in the tree, so its centre can't be hit-tested — "
            f"abstaining, not passing."
        )
    x, y, w, h = el_rect
    cx, cy = x + w // 2, y + h // 2
    hit = _tp.element_at(cx, cy)
    if hit is None:
        raise GeometryUnavailable(
            f"assert_not_occluded: hit-test at the centre ({cx}, {cy}) of "
            f"{query!r} (app={handle.app!r}) returned nothing. Cannot decide "
            f"occlusion — abstaining, not passing."
        )

    # (1) Trust guard: a trustworthy hit-test returns an element whose bounds
    # contain the queried point. On WebView2/high-DPI it can return an unrelated
    # node whose rect is nowhere near the point (observed live) — don't trust it.
    hit_rect = _rect(hit)
    if hit_rect is None or not _point_in(hit_rect, cx, cy):
        raise GeometryUnavailable(
            f"assert_not_occluded: the hit-test for {query!r} (app={handle.app!r}) "
            f"at ({cx}, {cy}) returned {_ui._describe_element(hit)} whose bounds "
            f"{hit_rect} do not contain that point — the hit-test is unreliable "
            f"here (a coordinate/DPI mismatch, as seen on WebView2). Cannot decide "
            f"occlusion; abstaining, not passing."
        )

    # (2) Our element or a child inside it is topmost -> not occluded.
    if getattr(hit, "id", None) == el.id or _contains(el_rect, hit_rect, 0):
        return

    # (3) A process we don't own is painted on the point -> real occlusion.
    hit_pid = getattr(hit, "pid", None)
    if hit_pid is not None and not _ui.is_owned(hit_pid):
        raise _ui.UIAssertionError(
            f"assert_not_occluded: {query!r} (app={handle.app!r}) is covered at "
            f"its centre ({cx}, {cy}) by another process's window "
            f"{_ui._describe_element(hit)} (pid={hit_pid}, not owned by this run, "
            f"rect={hit_rect}). Something is drawn on top of it."
        )

    # (4) Owned/same-process node that isn't inside our element: a wrapper, a
    # CDP-namespace node, or a same-app overlay — undecidable by geometry with
    # churning IDs. Abstain rather than false-fail or false-pass. (#37.)
    raise GeometryUnavailable(
        f"assert_not_occluded: the centre hit-test for {query!r} "
        f"(app={handle.app!r}) resolved to an owned/same-process element that "
        f"isn't inside it: {_ui._describe_element(hit)} (rect={hit_rect}). On a "
        f"nested DOM this is usually a wrapper, not a real overlay, but the two "
        f"can't be told apart by geometry — abstaining, not passing. (A DOM "
        f"z-order query, issue #37, is the robust check for same-window overlays.)"
    )


def assert_matches_baseline(
    handle: "_ui.WindowHandle",
    name: str,
    query: str | None = None,
    *,
    role: str | None = None,
    padding: int = 0,
    tolerance: float = DEFAULT_BASELINE_TOLERANCE,
    per_pixel: int = DEFAULT_PER_PIXEL_TOLERANCE,
    baseline_dir: str | os.PathLike | None = None,
) -> None:
    """Assert a region still matches its approved baseline (deterministic diff).

    The most reliable vision check per the phase plan: capture the region and
    compare pixel-for-pixel against a stored PNG baseline. Fails
    (:class:`~cyclaudes.ui.UIAssertionError`) if the region's size changed or
    more than ``tolerance`` (fraction) of pixels differ by more than
    ``per_pixel`` levels; passes otherwise. No model — a diff, not a judgment.

    Re-baselining is explicit and opt-in: set the :data:`REBASELINE_ENV`
    environment variable to write the current capture as the new baseline. A
    first run with no baseline yet also writes one. **Both cases abstain**
    (:class:`BaselineUnavailable`) — a freshly written baseline verified nothing
    against itself, so it must never count as a pass.

    ``name`` keys the baseline file (``<baseline_dir>/<name>.png``);
    ``baseline_dir`` defaults to ``.cyclaudes/baselines`` under the cwd.

    Raises:
        UIAssertionError: the region diverged from its baseline (an observed defect).
        BaselineUnavailable: baseline missing or re-baselined — nothing compared (abstention).
        CaptureUnavailable | WindowGone: could not capture (abstention).
        UnownedWindow: the handle's PID is no longer owned (safety error).
    """
    current = capture(handle, query, role=role, padding=padding).convert("RGB")
    root = Path(baseline_dir) if baseline_dir is not None else Path(".cyclaudes") / "baselines"
    path = root / f"{name}.png"

    rebaseline = bool(os.environ.get(REBASELINE_ENV))
    if rebaseline or not path.exists():
        root.mkdir(parents=True, exist_ok=True)
        current.save(path)
        why = (
            f"{REBASELINE_ENV} is set — wrote a new baseline"
            if rebaseline
            else "no baseline existed — created one"
        )
        raise BaselineUnavailable(
            f"assert_matches_baseline({name!r}): {why} at {path}. Nothing was "
            f"compared, so this is an abstention, not a pass — re-run without "
            f"{REBASELINE_ENV} to verify against the baseline."
        )

    base = Image.open(path).convert("RGB")
    if base.size != current.size:
        raise _ui.UIAssertionError(
            f"assert_matches_baseline({name!r}, app={handle.app!r}): region size "
            f"changed from baseline {base.size} to {current.size}. The layout "
            f"moved or resized — a real visual regression against {path}."
        )
    frac, max_dev = _changed_fraction(base, current, per_pixel)
    if frac > tolerance:
        raise _ui.UIAssertionError(
            f"assert_matches_baseline({name!r}, app={handle.app!r}): "
            f"{frac:.4f} of pixels differ from baseline {path} (>{tolerance:.4f} "
            f"allowed; max channel deviation {max_dev}). A visual regression the "
            f"structural check cannot see. Re-baseline with {REBASELINE_ENV}=1 "
            f"only if this change is intended."
        )


def assert_visible(
    handle: "_ui.WindowHandle",
    query: str,
    *,
    role: str | None = None,
) -> None:
    """Assert an element is actually usable — present, on-screen, unobscured, painted.

    The routing rule made concrete: run the cheapest structural gate first and
    only escalate to each more-expensive vision check if the cheaper one passed.
    Order is deliberate — structural existence (cheap tree read) → within
    viewport (geometry, no capture) → not occluded (one hit-test) → rendered
    (a capture). The first failure/abstention short-circuits, so a missing
    element never pays for a screenshot.

    Each step keeps its own semantics: a real defect at any step fails, an
    unevaluable step abstains, a lapsed ownership claim raises. This is the
    single call a check reaches for when it means "is this thing genuinely
    visible to a user", not just "is it in the tree".
    """
    handle.assert_exists(query, role=role)  # structural, cheapest
    assert_within_viewport(handle, query, role=role)  # geometry, no capture
    assert_not_occluded(handle, query, role=role)  # one hit-test
    assert_rendered(handle, query, role=role)  # capture, most expensive
