"""Substeps must keep controlling fall speed.

The velocity clamp is top_speed = rest * VELOCITY_SAFETY / dt, and dt is the
frame divided by Substeps, so the cap per frame is

    2 * radius * VELOCITY_SAFETY * substeps * fps / fps
      = 2 * radius * VELOCITY_SAFETY * substeps

Lowering Substeps without raising Noodle Radius therefore does not speed the
sim up, it puts it in slow motion - which is the one failure mode that looks
like a win in a ms-per-frame benchmark. A previous version derived the substep
count from `gravity * frame_dt^2`, a from-rest estimate, which collapsed to 1
on every frame at 24 fps: Substeps stopped changing fall speed at all and the
default preset ran 9.4x slow. These tests are what catch that.
"""
import pytest

from conftest import bake_centre_of_mass, build_rod

FPS = 24.0
FRAMES = 60
TAIL = 15          # frames averaged at the end of the run


def fall_rate(noodle, substeps, radius=2.5):
    """Saturated fall speed in units per frame, for one straight rod."""
    ng, obj = build_rod(noodle, substeps, radius=radius)
    coms = bake_centre_of_mass(noodle, obj, FRAMES)
    return (coms[-TAIL] - coms[-1]) / (TAIL - 1.0)


def test_clamp_matches_the_documented_formula(noodle):
    """At Substeps 1 the rod is pinned at the cap, so the cap is measurable."""
    rate = fall_rate(noodle, 1)
    expected = 2 * 2.5 * noodle.VELOCITY_SAFETY
    assert rate == pytest.approx(expected, rel=0.02), (
        f"clamp is {rate:.3f} units/frame, formula says {expected:.3f}")


def test_substeps_scales_fall_speed(noodle):
    """Doubling Substeps must double the cap, not leave it where it was."""
    slow, fast = fall_rate(noodle, 1), fall_rate(noodle, 2)
    assert fast == pytest.approx(2 * slow, rel=0.02), (
        f"Substeps 1 -> {slow:.3f}, Substeps 2 -> {fast:.3f}; expected 2x")


def test_substeps_is_not_inert(noodle):
    """The regression guard: 8 substeps used to measure identical to 24."""
    rate = fall_rate(noodle, 8)
    assert rate > 3 * fall_rate(noodle, 1), (
        f"Substeps is inert: 8 substeps fell at {rate:.3f} units/frame")


def test_default_preset_is_not_in_slow_motion(noodle):
    """The shipped scene must out-fall the worst possible preset.

    Free fall from the default Start Height reaches the clamp only well past
    the bench window, so this asserts the cap is not what limits the default
    scene - if it were, the preset table's speed budget would be fiction.
    """
    rate = fall_rate(noodle, 8)
    cap = 2 * 2.5 * noodle.VELOCITY_SAFETY * 8
    assert rate < cap, "the default scene is clamped, not falling freely"
    assert rate > 4 * 2.5 * noodle.VELOCITY_SAFETY, (
        f"default preset fell only {rate:.3f} units/frame; it is in slow motion")
