"""Tests for the vision fallback (src/cyclaudes/vision.py).

Two layers, matching the module's split:

- The deterministic *decision* (``is_flat`` / span) against real PIL images —
  a blank fill is flat, painted content is not. No driver, no mocking.
- The *discipline* around capture against a fake touchpoint: owned-only,
  abstain (not false-pass) when pixels can't be had, fail (not abstain) when a
  region is observed blank. Each test fails if its discipline is removed.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from PIL import Image

from cyclaudes import ui, vision


# ---------------------------------------------------------------------------
# Real images for the deterministic core
# ---------------------------------------------------------------------------


def _flat(color=(255, 255, 255), size=(40, 20)):
    return Image.new("RGB", size, color)


def _painted(size=(40, 20)):
    img = Image.new("RGB", size, (255, 255, 255))
    # a black bar through the middle — unmistakably "rendered content"
    for x in range(size[0]):
        img.putpixel((x, size[1] // 2), (0, 0, 0))
    return img


def test_flat_white_is_flat():
    assert vision.is_flat(_flat((255, 255, 255)))


def test_flat_any_solid_colour_is_flat():
    assert vision.is_flat(_flat((17, 200, 120)))


def test_painted_region_is_not_flat():
    assert not vision.is_flat(_painted())


def test_tolerance_ignores_sub_threshold_noise():
    img = _flat((100, 100, 100))
    img.putpixel((0, 0), (100 + vision.DEFAULT_FLAT_TOLERANCE, 100, 100))  # within tol
    assert vision.is_flat(img)
    img.putpixel((0, 0), (100 + vision.DEFAULT_FLAT_TOLERANCE + 5, 100, 100))  # over tol
    assert not vision.is_flat(img)


# ---------------------------------------------------------------------------
# Fake touchpoint (only what vision.capture touches)
# ---------------------------------------------------------------------------


@dataclass
class FakeWindow:
    id: str
    title: str
    app: str
    pid: int


@dataclass
class FakeElement:
    id: str
    name: str
    role: str = "unknown"
    raw_role: str = ""
    states: list = field(default_factory=list)
    value: str | None = None


class FakeTP:
    def __init__(self, *, image=None, elements=None):
        self.win = FakeWindow("w1", "App", "App", 4321)
        self._image = image
        self._elements = elements or []
        self.shots: list = []

    def windows(self):
        return [self.win]

    def elements(self, window_id=None, **kw):
        return list(self._elements) if window_id == self.win.id else []

    def screenshot(self, *, window_id=None, element=None, padding=0):
        self.shots.append((window_id, getattr(element, "id", element), padding))
        return self._image


@pytest.fixture
def owned_handle():
    """A real owned WindowHandle wired to a fake touchpoint, cleaned up after."""

    def _make(tp):
        ui._tp = tp
        vision._tp = tp
        ui.own(tp.win.pid)
        return ui.WindowHandle(
            tp.win.id, app=tp.win.app, pid=tp.win.pid, timeout=0.1, poll=0.01, owned=True
        )

    yield _make
    ui.reset_ownership()


# ---------------------------------------------------------------------------
# capture() discipline
# ---------------------------------------------------------------------------


def test_capture_whole_window_returns_image(owned_handle):
    tp = FakeTP(image=_painted())
    h = owned_handle(tp)
    img = vision.capture(h)
    assert img.size == (40, 20)
    assert tp.shots == [("w1", None, 0)]  # scoped to the owned window


def test_capture_element_resolves_fresh_and_scopes(owned_handle):
    tp = FakeTP(image=_painted(), elements=[FakeElement("e1", "Import")])
    h = owned_handle(tp)
    vision.capture(h, "Import", padding=3)
    assert tp.shots == [(None, "e1", 3)]  # by element, not whole window


def test_capture_none_image_abstains(owned_handle):
    tp = FakeTP(image=None)
    h = owned_handle(tp)
    with pytest.raises(vision.CaptureUnavailable):
        vision.capture(h)


def test_capture_zero_area_abstains(owned_handle):
    tp = FakeTP(image=Image.new("RGB", (0, 0)))
    h = owned_handle(tp)
    with pytest.raises(vision.CaptureUnavailable):
        vision.capture(h)


def test_capture_unowned_is_safety_error_not_abstention(owned_handle):
    tp = FakeTP(image=_painted())
    h = owned_handle(tp)
    ui.disown(tp.win.pid)  # claim lapses
    with pytest.raises(ui.UnownedWindow):
        vision.capture(h)


def test_capture_unavailable_is_registered_abstention():
    # The seam the pytest layer keys on: capture-failure => "cannot verify".
    assert issubclass(vision.CaptureUnavailable, ui.UIError)
    assert not issubclass(vision.CaptureUnavailable, AssertionError)
    from cyclaudes import abstain

    assert vision.CaptureUnavailable in abstain.abstention_types()


# ---------------------------------------------------------------------------
# assert_rendered: blank => fail, painted => pass, no-capture => abstain
# ---------------------------------------------------------------------------


def test_assert_rendered_passes_on_painted(owned_handle):
    tp = FakeTP(image=_painted())
    h = owned_handle(tp)
    vision.assert_rendered(h)  # no raise


def test_assert_rendered_fails_on_blank(owned_handle):
    tp = FakeTP(image=_flat((255, 255, 255)))
    h = owned_handle(tp)
    with pytest.raises(ui.UIAssertionError):
        vision.assert_rendered(h)


def test_assert_rendered_blank_is_failure_not_abstention(owned_handle):
    # The core safety line: a blank render is a real defect (fail), never
    # something to abstain on — the opposite of a capture we couldn't take.
    tp = FakeTP(image=_flat((10, 10, 10)))
    h = owned_handle(tp)
    from cyclaudes import abstain

    try:
        vision.assert_rendered(h)
    except Exception as e:  # noqa: BLE001 - asserting on the type
        assert isinstance(e, AssertionError)
        assert not isinstance(e, abstain.abstention_types())
    else:
        pytest.fail("expected assert_rendered to fail on a blank region")


def test_assert_rendered_abstains_when_capture_unavailable(owned_handle):
    tp = FakeTP(image=None)
    h = owned_handle(tp)
    with pytest.raises(vision.CaptureUnavailable):
        vision.assert_rendered(h)
