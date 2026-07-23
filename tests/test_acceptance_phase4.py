"""Phase 4 acceptance — the vision fallback earns its place.

The success criterion: vision catches a defect the *structural* check passes
(an element present and enabled in the tree, but blank / occluded / clipped),
and does **not** fail a good layout — while still abstaining, never passing,
when it cannot actually see.

Proven deterministically against a fake touchpoint (same discipline as the
Phase 2 acceptance suite, which proved teardown properties by construction
rather than risking them live): each scenario builds one tree where the
structural read succeeds, then shows the vision assertion diverging from it.
A live dogfood against a real UI (e.g. LLT) is the field confirmation to run
on top of this, not a substitute for it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from PIL import Image

from cyclaudes import abstain, ui, vision


@dataclass
class FakeWindow:
    id: str
    title: str
    app: str
    pid: int
    position: tuple = (0, 0)
    size: tuple = (800, 600)


@dataclass
class FakeElement:
    id: str
    name: str
    role: str = "unknown"
    raw_role: str = ""
    states: list = field(default_factory=list)
    value: str | None = None
    position: tuple = (100, 100)
    size: tuple = (50, 20)
    window_id: str = "w1"
    pid: int = 4321


class FakeTP:
    def __init__(self, *, image=None, elements=None, hit=None):
        self.win = FakeWindow("w1", "Editor", "Editor", 4321)
        self._image, self._elements, self._hit = image, elements or [], hit

    def windows(self):
        return [self.win]

    def elements(self, window_id=None, **kw):
        return list(self._elements) if window_id == self.win.id else []

    def screenshot(self, *, window_id=None, element=None, padding=0):
        return self._image

    def element_at(self, x, y):
        return self._hit


@pytest.fixture
def handle_for():
    def _make(tp):
        ui._tp = tp
        vision._tp = tp
        ui.own(tp.win.pid)
        return ui.WindowHandle(
            tp.win.id, app=tp.win.app, pid=tp.win.pid, timeout=0.1, poll=0.01, owned=True
        )

    yield _make
    ui.reset_ownership()


def _painted(size=(50, 20)):
    img = Image.new("RGB", size, (255, 255, 255))
    for x in range(size[0]):
        img.putpixel((x, size[1] // 2), (0, 0, 0))
    return img


# --- Criterion 1: catches a defect the structural check passes -------------


def test_structural_passes_but_region_is_blank(handle_for):
    """Present in the tree, enabled — but painted blank. Structural: pass. Vision: fail."""
    el = FakeElement("e1", "Preview", states=["enabled"])
    h = handle_for(FakeTP(image=Image.new("RGB", (50, 20), (255, 255, 255)), elements=[el]))
    h.assert_exists("Preview")  # structural is happy
    h.assert_state("Preview", "enabled")  # structural is happy
    with pytest.raises(ui.UIAssertionError):  # vision is not
        vision.assert_rendered(h, "Preview")


def test_structural_passes_but_element_is_occluded(handle_for):
    """Present and enabled, but another window covers it. Structural: pass. Vision: fail.

    The reliably-catchable occlusion is a *foreign process* painted over our
    element (an OS dialog, another app) — exactly what structural can't see.
    """
    el = FakeElement("e1", "Save", position=(100, 100), size=(50, 20))  # centre (125,110)
    modal = FakeElement(
        "m1", "Overwrite?", position=(0, 0), size=(800, 600), window_id="w2", pid=9999
    )
    h = handle_for(FakeTP(elements=[el], hit=modal))
    h.assert_exists("Save")  # structural is happy
    with pytest.raises(ui.UIAssertionError):  # vision sees the foreign window on top
        vision.assert_not_occluded(h, "Save")


def test_structural_passes_but_element_is_clipped(handle_for):
    """Present with a position, but that position is off the window edge."""
    el = FakeElement("e1", "Toolbar", position=(790, 100), size=(50, 20))  # spills past x=800
    h = handle_for(FakeTP(elements=[el]))
    h.assert_exists("Toolbar")  # structural is happy
    with pytest.raises(ui.UIAssertionError):  # vision sees it clipped
        vision.assert_within_viewport(h, "Toolbar")


# --- Criterion 2: does NOT fail a good layout ------------------------------


def test_good_layout_passes_every_vision_check(handle_for):
    el = FakeElement("e1", "Save", position=(100, 100), size=(50, 20))
    h = handle_for(FakeTP(image=_painted(), elements=[el], hit=el))
    vision.assert_visible(h, "Save")  # present + on-screen + unobscured + painted


# --- Criterion 3: abstains (never passes) when it cannot see ----------------


def test_abstains_when_it_cannot_capture(handle_for):
    el = FakeElement("e1", "Preview")
    h = handle_for(FakeTP(image=None, elements=[el]))
    try:
        vision.assert_rendered(h, "Preview")
    except Exception as e:  # noqa: BLE001 - asserting on the type
        assert isinstance(e, abstain.abstention_types())  # cannot-verify
        assert not isinstance(e, AssertionError)  # not a failure, not a pass
    else:
        pytest.fail("expected an abstention when pixels cannot be captured")
