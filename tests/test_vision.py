"""Tests for the vision fallback (src/cyclaudes/vision.py).

Two layers, matching the module's split:

- The deterministic *decisions* (flatness, pixel diff, geometry containment)
  against real PIL images / plain rects — no driver, no mocking.
- The *discipline* around each assertion against a fake touchpoint: owned-only,
  abstain (not false-pass) when pixels/geometry/baseline can't be had, fail (not
  abstain) when a real defect is observed. Each test fails if its discipline is
  removed.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from PIL import Image

from cyclaudes import abstain, ui, vision


# ---------------------------------------------------------------------------
# Real images for the deterministic core
# ---------------------------------------------------------------------------


def _flat(color=(255, 255, 255), size=(40, 20)):
    return Image.new("RGB", size, color)


def _painted(size=(40, 20)):
    img = Image.new("RGB", size, (255, 255, 255))
    for x in range(size[0]):  # a black bar — unmistakably "rendered content"
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


def test_changed_fraction_identical_is_zero():
    a = _painted()
    frac, dev = vision._changed_fraction(a.convert("RGB"), a.convert("RGB"), 16)
    assert frac == 0 and dev == 0


def test_changed_fraction_counts_real_change():
    a = _flat((0, 0, 0)).convert("RGB")
    b = _flat((0, 0, 0)).convert("RGB")
    b.putpixel((0, 0), (255, 255, 255))  # one pixel of 800
    frac, dev = vision._changed_fraction(a, b, 16)
    assert dev == 255 and 0 < frac < 0.01


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
        self.win = FakeWindow("w1", "App", "App", 4321)
        self._image = image
        self._elements = elements or []
        self._hit = hit
        self.shots: list = []

    def windows(self):
        return [self.win]

    def elements(self, window_id=None, **kw):
        return list(self._elements) if window_id == self.win.id else []

    def screenshot(self, *, window_id=None, element=None, padding=0):
        self.shots.append((window_id, getattr(element, "id", element), padding))
        return self._image

    def element_at(self, x, y):
        return self._hit


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
    img = vision.capture(owned_handle(tp))
    assert img.size == (40, 20)
    assert tp.shots == [("w1", None, 0)]  # scoped to the owned window


def test_capture_element_resolves_fresh_and_scopes(owned_handle):
    tp = FakeTP(image=_painted(), elements=[FakeElement("e1", "Import")])
    vision.capture(owned_handle(tp), "Import", padding=3)
    assert tp.shots == [(None, "e1", 3)]  # by element, not whole window


def test_capture_none_image_abstains(owned_handle):
    with pytest.raises(vision.CaptureUnavailable):
        vision.capture(owned_handle(FakeTP(image=None)))


def test_capture_zero_area_abstains(owned_handle):
    with pytest.raises(vision.CaptureUnavailable):
        vision.capture(owned_handle(FakeTP(image=Image.new("RGB", (0, 0)))))


def test_capture_unowned_is_safety_error_not_abstention(owned_handle):
    tp = FakeTP(image=_painted())
    h = owned_handle(tp)
    ui.disown(tp.win.pid)  # claim lapses
    with pytest.raises(ui.UnownedWindow):
        vision.capture(h)


def test_vision_abstentions_are_registered_and_not_assertion_errors():
    for exc in (
        vision.CaptureUnavailable,
        vision.GeometryUnavailable,
        vision.BaselineUnavailable,
    ):
        assert issubclass(exc, vision.VisionAbstention)
        assert not issubclass(exc, AssertionError)
        assert isinstance(exc(), abstain.abstention_types())


# ---------------------------------------------------------------------------
# assert_rendered
# ---------------------------------------------------------------------------


def test_assert_rendered_passes_on_painted(owned_handle):
    vision.assert_rendered(owned_handle(FakeTP(image=_painted())))


def test_assert_rendered_fails_on_blank(owned_handle):
    with pytest.raises(ui.UIAssertionError):
        vision.assert_rendered(owned_handle(FakeTP(image=_flat((255, 255, 255)))))


def test_assert_rendered_blank_is_failure_not_abstention(owned_handle):
    h = owned_handle(FakeTP(image=_flat((10, 10, 10))))
    try:
        vision.assert_rendered(h)
    except Exception as e:  # noqa: BLE001 - asserting on the type
        assert isinstance(e, AssertionError)
        assert not isinstance(e, abstain.abstention_types())
    else:
        pytest.fail("expected assert_rendered to fail on a blank region")


def test_assert_rendered_abstains_when_capture_unavailable(owned_handle):
    with pytest.raises(vision.CaptureUnavailable):
        vision.assert_rendered(owned_handle(FakeTP(image=None)))


# ---------------------------------------------------------------------------
# assert_within_viewport (geometry, no capture)
# ---------------------------------------------------------------------------


def test_within_viewport_passes_when_contained(owned_handle):
    el = FakeElement("e1", "Btn", position=(100, 100), size=(50, 20))  # inside 800x600
    vision.assert_within_viewport(owned_handle(FakeTP(elements=[el])), "Btn")


def test_within_viewport_fails_when_clipped(owned_handle):
    el = FakeElement("e1", "Btn", position=(790, 100), size=(50, 20))  # spills past x=800
    with pytest.raises(ui.UIAssertionError):
        vision.assert_within_viewport(owned_handle(FakeTP(elements=[el])), "Btn")


def test_within_viewport_abstains_without_geometry(owned_handle):
    el = FakeElement("e1", "Btn", position=None, size=None)
    with pytest.raises(vision.GeometryUnavailable):
        vision.assert_within_viewport(owned_handle(FakeTP(elements=[el])), "Btn")


# ---------------------------------------------------------------------------
# assert_not_occluded (hit-test)
# ---------------------------------------------------------------------------


def test_not_occluded_passes_when_hit_is_self(owned_handle):
    el = FakeElement("e1", "Btn")
    vision.assert_not_occluded(owned_handle(FakeTP(elements=[el], hit=el)), "Btn")


def test_not_occluded_passes_when_hit_is_child(owned_handle):
    el = FakeElement("e1", "Btn", position=(100, 100), size=(50, 20))  # centre (125,110)
    child = FakeElement("e2", "label", position=(115, 102), size=(20, 16), window_id="w1")
    vision.assert_not_occluded(owned_handle(FakeTP(elements=[el], hit=child)), "Btn")


def test_not_occluded_fails_when_covered_by_foreign_process(owned_handle):
    # The reliable hard case: another (unowned) process is painted on the point.
    el = FakeElement("e1", "Btn", position=(100, 100), size=(50, 20))
    overlay = FakeElement(
        "m1", "Modal", position=(0, 0), size=(800, 600), window_id="w2", pid=9999
    )
    with pytest.raises(ui.UIAssertionError):
        vision.assert_not_occluded(owned_handle(FakeTP(elements=[el], hit=overlay)), "Btn")


def test_not_occluded_abstains_when_hittest_empty(owned_handle):
    el = FakeElement("e1", "Btn")
    with pytest.raises(vision.GeometryUnavailable):
        vision.assert_not_occluded(owned_handle(FakeTP(elements=[el], hit=None)), "Btn")


def test_not_occluded_abstains_when_hit_misses_the_point(owned_handle):
    # The LLT/WebView2 finding: element_at returned a node whose bounds don't
    # contain the queried point (coordinate/DPI mismatch). Untrustworthy -> abstain.
    el = FakeElement("e1", "Btn", position=(100, 100), size=(50, 20))  # centre (125,110)
    stray = FakeElement("g1", "", role="group", position=(900, 900), size=(50, 50), pid=9999)
    with pytest.raises(vision.GeometryUnavailable):
        vision.assert_not_occluded(owned_handle(FakeTP(elements=[el], hit=stray)), "Btn")


def test_not_occluded_abstains_on_owned_enclosing_wrapper(owned_handle):
    # An owned/same-process node that encloses the button (a DOM wrapper) and
    # really is on the point: benign-wrapper vs real-overlay is undecidable by
    # geometry -> abstain, never false-fail or false-pass.
    el = FakeElement("e1", "Btn", position=(100, 100), size=(50, 20))  # centre (125,110)
    wrapper = FakeElement("g1", "", role="group", position=(0, 0), size=(800, 600), window_id="w1")
    with pytest.raises(vision.GeometryUnavailable):
        vision.assert_not_occluded(owned_handle(FakeTP(elements=[el], hit=wrapper)), "Btn")


# ---------------------------------------------------------------------------
# assert_matches_baseline (deterministic diff + explicit re-baseline)
# ---------------------------------------------------------------------------


def test_baseline_first_run_creates_and_abstains(owned_handle, tmp_path):
    h = owned_handle(FakeTP(image=_painted()))
    with pytest.raises(vision.BaselineUnavailable):
        vision.assert_matches_baseline(h, "btn", baseline_dir=tmp_path)
    assert (tmp_path / "btn.png").exists()


def test_baseline_matching_passes(owned_handle, tmp_path):
    h = owned_handle(FakeTP(image=_painted()))
    with pytest.raises(vision.BaselineUnavailable):
        vision.assert_matches_baseline(h, "btn", baseline_dir=tmp_path)  # seed
    vision.assert_matches_baseline(h, "btn", baseline_dir=tmp_path)  # identical -> pass


def test_baseline_regression_fails(owned_handle, tmp_path):
    seed = owned_handle(FakeTP(image=_painted()))
    with pytest.raises(vision.BaselineUnavailable):
        vision.assert_matches_baseline(seed, "btn", baseline_dir=tmp_path)
    changed = owned_handle(FakeTP(image=_flat((255, 255, 255))))  # totally different
    with pytest.raises(ui.UIAssertionError):
        vision.assert_matches_baseline(changed, "btn", baseline_dir=tmp_path)


def test_baseline_size_change_fails(owned_handle, tmp_path):
    seed = owned_handle(FakeTP(image=_painted(size=(40, 20))))
    with pytest.raises(vision.BaselineUnavailable):
        vision.assert_matches_baseline(seed, "btn", baseline_dir=tmp_path)
    resized = owned_handle(FakeTP(image=_painted(size=(60, 20))))
    with pytest.raises(ui.UIAssertionError):
        vision.assert_matches_baseline(resized, "btn", baseline_dir=tmp_path)


def test_baseline_rebaseline_env_rewrites_and_abstains(owned_handle, tmp_path, monkeypatch):
    seed = owned_handle(FakeTP(image=_painted()))
    with pytest.raises(vision.BaselineUnavailable):
        vision.assert_matches_baseline(seed, "btn", baseline_dir=tmp_path)
    # Now a DIFFERENT render, but re-baseline env set: must rewrite + abstain,
    # never pass on the new pixels.
    monkeypatch.setenv(vision.REBASELINE_ENV, "1")
    changed = owned_handle(FakeTP(image=_flat((255, 255, 255))))
    with pytest.raises(vision.BaselineUnavailable):
        vision.assert_matches_baseline(changed, "btn", baseline_dir=tmp_path)
    # Baseline was overwritten: without the env, the new pixels now match.
    monkeypatch.delenv(vision.REBASELINE_ENV)
    vision.assert_matches_baseline(changed, "btn", baseline_dir=tmp_path)


# ---------------------------------------------------------------------------
# assert_visible (routing rule: cheap structural gate -> escalate)
# ---------------------------------------------------------------------------


def test_assert_visible_passes_when_all_hold(owned_handle):
    el = FakeElement("e1", "Btn", position=(100, 100), size=(50, 20))
    tp = FakeTP(image=_painted(), elements=[el], hit=el)
    vision.assert_visible(owned_handle(tp), "Btn")


def test_assert_visible_short_circuits_on_occlusion(owned_handle):
    el = FakeElement("e1", "Btn", position=(100, 100), size=(50, 20))
    overlay = FakeElement("m1", "Modal", position=(0, 0), size=(800, 600), window_id="w2", pid=9999)
    # Present + within viewport, but a foreign window is on top -> fails before capture.
    tp = FakeTP(image=_painted(), elements=[el], hit=overlay)
    with pytest.raises(ui.UIAssertionError):
        vision.assert_visible(owned_handle(tp), "Btn")
