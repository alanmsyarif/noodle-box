"""Structural behaviour: the pile must fall, stay above the floor, and settle.

`--check` covers all three. These tests exist because it used to cover only two
of them badly: its fall threshold sat within 7 units of the height a healthy
pile actually settles at, and it had no bound at all on energy injection, so a
run whose top rose 70% above its spawn height still printed OK.
"""
import pytest

CHECK_FRAMES = 120


def pile_tops(noodle, count=8, frames=CHECK_FRAMES, radius=None, start=None):
    """Top z of the pile per frame."""
    ng = noodle.build_group()
    obj = noodle.build_object(ng)
    noodle.set_input(obj, ng, "Noodle Count", count)
    if radius is not None:
        noodle.set_input(obj, ng, "Noodle Radius", radius)
        noodle.set_input(obj, ng, "Start Height", start)
    history = noodle.bake(obj, frames)
    return [history[f][2] for f in range(1, frames + 1)]


@pytest.fixture(scope="module")
def default_tops(noodle):
    """One bake of the default scene, shared by the tests that read its top."""
    return pile_tops(noodle)


def test_self_check_passes(noodle, capsys):
    """The shipped self-check is the floor, not the ceiling."""
    noodle.self_check()
    out = capsys.readouterr().out
    assert "\nOK " in out or out.startswith("OK "), out


def test_pile_falls(default_tops):
    """Same criterion --check uses: the pile must fall by half its height."""
    assert default_tops[-1] < default_tops[0] * 0.5, (
        f"pile barely fell: {default_tops[0]:.1f} -> {default_tops[-1]:.1f}")


def test_pile_stays_above_the_floor(noodle):
    ng = noodle.build_group()
    obj = noodle.build_object(ng)
    noodle.set_input(obj, ng, "Noodle Count", 8)
    history = noodle.bake(obj, 60)
    low = history[60][1]
    assert low == low, "solver produced NaN"
    assert low > -1.0, f"noodles sank through the floor: {low:.3f}"


def test_default_scene_does_not_launch(default_tops):
    """Constraint resolution must not throw the pile above its spawn height.

    Tighter than --check's ceiling on purpose: the default scene measures 1.05x,
    so anything approaching --check's 1.5x on this scene is a regression that
    --check would let through.
    """
    spawn, peak = default_tops[0], max(default_tops)
    assert peak < spawn * 1.10, (
        f"pile launched: top {peak:.1f} vs spawn {spawn:.1f} "
        f"at frame {default_tops.index(peak) + 1}")


def test_check_scene_respects_the_launch_limit(noodle):
    """The fat-noodle scene --check runs must satisfy --check's own bound.

    This is the scene where the injection is visible: it measured 1.70x before
    the substep count was fixed and 1.30x after.
    """
    length = next(p[2] for p in noodle.PARAMS if p[0] == "Noodle Length")
    tops = pile_tops(noodle, radius=length / 50.0, start=length / 2.0)
    spawn, peak = tops[0], max(tops)
    assert peak < spawn * noodle.LAUNCH_LIMIT, (
        f"pile launched: top {peak:.1f} vs spawn {spawn:.1f} "
        f"at frame {tops.index(peak) + 1}")
