"""Metre-scale motion, adaptive stepping, colliders and interactive lifecycle."""
import math

import bpy
import pytest

from conftest import bake_centre_of_mass


def test_metric_defaults(noodle):
    defaults = {p[0]: p[2] for p in noodle.PARAMS}
    assert defaults["Noodle Length"] == 0.25
    assert defaults["Gravity"] == 9.81
    assert defaults["Noodle Radius"] < 0.01
    assert defaults["Adaptive Substeps"] is True


def test_budget_never_rounds_down(noodle):
    radius, height, fps = 0.004, 0.3, 24
    required = noodle.substeps_for(radius, 9.81, height, fps)
    speed = math.sqrt(2 * 9.81 * height)
    per_step = 2 * radius * noodle.VELOCITY_SAFETY * fps
    assert required * per_step >= speed
    assert (required - 1) * per_step < speed
    assert noodle.substeps_for(0.0001, 9.81, 1) > noodle.MAX_RUNTIME_SUBSTEPS


@pytest.mark.parametrize("kwargs", [dict(radius=0), dict(fps=0),
                                    dict(gravity=-1), dict(start_height=float("nan"))])
def test_budget_rejects_invalid_inputs(noodle, kwargs):
    values = dict(radius=0.006, gravity=9.81, start_height=0.2, fps=24)
    values.update(kwargs)
    with pytest.raises(ValueError):
        noodle.substeps_for(**values)


def falling_rod(noodle, adaptive, fps=24, frames=10, unit_scale=1):
    ng = noodle.build_group()
    obj = noodle.build_object(ng)
    bpy.context.scene.render.fps = fps
    values = {"Noodle Count": 1, "Spawn Coil": 0.0, "Stiffness": 1.0,
              "Noodle Length": 0.25 * unit_scale, "Noodle Radius": 0.006 * unit_scale,
              "Start Height": 2.0 * unit_scale, "Fill Diameter": 0.2 * unit_scale,
              "Gravity": 9.81 * unit_scale, "Substeps": 24,
              "Adaptive Substeps": adaptive, "Self Collision": 0.0}
    for name, value in values.items():
        noodle.set_input(obj, ng, name, value)
    coms = bake_centre_of_mass(noodle, obj, frames)
    bpy.data.objects.remove(obj, do_unlink=True)
    return (coms[0] - coms[-1]) / unit_scale


def test_adaptive_fall_tracks_fixed_step(noodle):
    fixed = falling_rod(noodle, False)
    adaptive = falling_rod(noodle, True)
    assert fixed > 0.5  # Catches a fast benchmark obtained by slowing gravity.
    assert adaptive == pytest.approx(fixed, rel=0.04)


def test_world_unit_normalization_preserves_motion(noodle):
    metric = falling_rod(noodle, True)
    large = falling_rod(noodle, True, unit_scale=100)
    assert metric == pytest.approx(large, rel=0.01)


def test_same_elapsed_time_at_different_fps(noodle):
    # Nine intervals at 24 fps = eighteen at 48 fps. Drag uses elapsed seconds.
    at_24 = falling_rod(noodle, False, fps=24, frames=10)
    at_48 = falling_rod(noodle, False, fps=48, frames=19)
    assert at_24 == pytest.approx(at_48, rel=0.025)


@pytest.mark.parametrize("source", ["Collider", "Collider Collection", "Sticky Collider"])
def test_closed_collider_holds_noodles(noodle, source):
    bpy.ops.mesh.primitive_cube_add(size=1, location=(0, 0, 0.03))
    slab = bpy.context.object
    slab.scale = (0.8, 0.8, 0.06)  # Closed slab; surface at 0.06 m.
    ng = noodle.build_group()
    obj = noodle.build_object(ng)
    noodle.set_input(obj, ng, "Noodle Count", 2)
    if source == "Collider":
        collider = slab
    else:
        collider = bpy.data.collections.new("Test collider")
        bpy.context.scene.collection.children.link(collider)
        collider.objects.link(slab)
    noodle.set_input(obj, ng, source, collider)
    history = noodle.bake(obj, 40)
    assert history[40][1] > 0.05, "assigned collider was skipped or penetrated"
    assert history[40][2] < history[1][2], "noodles did not fall onto the collider"


def test_default_restores_preset_and_keeps_colliders(noodle):
    ng = noodle.build_group()
    obj = noodle.build_object(ng)
    bpy.ops.mesh.primitive_cube_add()
    collider = bpy.context.object
    noodle.set_input(obj, ng, "Collider", collider)
    noodle.set_input(obj, ng, "Noodle Count", 7)
    noodle.apply_realtime(obj, ng, "metric_fast")
    noodle.apply_realtime(obj, ng, "default")
    assert noodle.get_input(obj, ng, "Noodle Length") == pytest.approx(0.25)
    assert noodle.get_input(obj, ng, "Noodle Radius") == pytest.approx(0.006)
    assert noodle.get_input(obj, ng, "Collider") == collider
    assert noodle.get_input(obj, ng, "Noodle Count") == 7


def test_build_preserves_existing_objects_and_groups(noodle):
    first_ng = noodle.build_group()
    first = noodle.build_object(first_ng)
    first.location = (1, 2, 3)
    second_ng = noodle.build_group()
    second = noodle.build_object(second_ng)
    assert first != second
    assert first.modifiers[0].node_group == first_ng
    assert first_ng != second_ng
    assert tuple(first.location) == (1, 2, 3)


def test_contacts_build_a_pile(noodle):
    tops = []
    for contact in (0.0, 1.0):
        ng = noodle.build_group()
        obj = noodle.build_object(ng)
        # Twelve strands can legitimately spread into a single floor layer;
        # use enough material to require a pile in the contact-enabled case.
        for name, value in {"Noodle Count": 48, "Fill Diameter": 0.08,
                            "Self Collision": contact}.items():
            noodle.set_input(obj, ng, name, value)
        tops.append(noodle.bake(obj, 80)[80][2])
        bpy.data.objects.remove(obj, do_unlink=True)
    assert tops[1] > 1.5 * tops[0], "self-contact did not build a separated pile"


def test_main_rerun_keeps_settings(noodle, monkeypatch):
    monkeypatch.setattr("sys.argv", ["blender"])
    try:
        noodle.main()
        obj = bpy.context.object
        ng = noodle.noodle_modifier(obj).node_group
        noodle.set_input(obj, ng, "Noodle Count", 7)
        before = len(bpy.data.objects)
        noodle.main()
        assert len(bpy.data.objects) == before
        assert bpy.context.object == obj
        assert noodle.get_input(obj, ng, "Noodle Count") == 7
    finally:
        noodle.unregister()


def test_input_access_with_another_modifier_first(noodle):
    ng = noodle.build_group()
    obj = noodle.build_object(ng)
    obj.modifiers.new("Before noodles", "SUBSURF")
    obj.modifiers.move(1, 0)
    noodle.set_input(obj, ng, "Noodle Count", 3)
    assert noodle.get_input(obj, ng, "Noodle Count") == 3


def test_sidebar_registration_and_operators(noodle):
    noodle.register()
    noodle.register()  # Run Script twice should not raise a duplicate-class error.
    try:
        assert bpy.ops.noodle.add() == {"FINISHED"}
        obj = bpy.context.object
        ng = noodle.noodle_modifier(obj).node_group
        assert tuple(obj.scale) == (1, 1, 1)
        assert bpy.context.scene.unit_settings.scale_length == 1
        noodle.set_input(obj, ng, "Noodle Count", 2)
        initial = noodle.bake(obj, 8)[1]
        assert bpy.ops.noodle.reset() == {"FINISHED"}
        assert bpy.context.scene.frame_current == bpy.context.scene.frame_start
        reset = noodle.bake(obj, 1)[1]
        assert reset == pytest.approx(initial)
        assert bpy.ops.noodle.preset(preset="fast") == {"FINISHED"}
        assert noodle.get_input(obj, ng, "Noodle Radius") == pytest.approx(0.009)
        assert bpy.ops.noodle.fit_steps() == {"FINISHED"}
        assert noodle.get_input(obj, ng, "Substeps") == noodle.recommended_substeps(obj, ng, bpy.context.scene)
    finally:
        noodle.unregister()


@pytest.mark.parametrize("argv", [["--unknown"], ["--check", "extra"],
                                  ["--check", "--bench"], ["junk", "--check"]])
def test_cli_rejects_unknown_or_mixed_modes(noodle, argv):
    with pytest.raises(noodle.CliError):
        noodle.parse_mode_args(argv)
