"""Extension lifecycle, noodle types and the non-destructive demo scene."""
import math

import bpy
import pytest

import noodle_box
from noodle_box import presets
from noodle_box.demo import create_demo


def test_extension_registration_does_not_create_objects():
    before = set(bpy.data.objects)
    noodle_box.register()
    noodle_box.register()
    assert set(bpy.data.objects) == before
    assert hasattr(bpy.types.Scene, "noodle_box_type")
    noodle_box.unregister()
    noodle_box.unregister()
    assert not hasattr(bpy.types.Scene, "noodle_box_type")
    assert not hasattr(bpy.types, "NOODLE_PT_controls")


@pytest.mark.parametrize("key", presets.NOODLE_TYPES)
def test_noodle_type_simulates(noodle, key):
    ng = noodle.build_group()
    obj = noodle.build_object(ng)
    needed = presets.apply_type(obj, key, bpy.context.scene)
    settings = presets.NOODLE_TYPES[key]
    assert noodle.get_input(obj, ng, "Noodle Radius") == pytest.approx(settings["radius"])
    assert noodle.get_input(obj, ng, "Profile Aspect") == pytest.approx(settings["aspect"])
    assert noodle.get_input(obj, ng, "Substeps") == min(24, needed)
    assert noodle.get_input(obj, ng, "Noodle Material")["noodle_type"] == key
    noodle.set_input(obj, ng, "Noodle Count", 3)
    history = noodle.bake(obj, 20)
    assert all(math.isfinite(v) for row in history.values() for v in row)
    assert history[20][2] < history[1][2] * 0.9
    assert history[20][1] > -settings["radius"], "smoothed surface sank through floor"


def test_type_switch_restores_round_profile_and_preserves_colliders(noodle):
    ng = noodle.build_group()
    obj = noodle.build_object(ng)
    bpy.ops.mesh.primitive_cube_add()
    collider = bpy.context.object
    noodle.set_input(obj, ng, "Collider", collider)
    noodle.set_input(obj, ng, "Noodle Count", 7)
    presets.apply_type(obj, "rice_ribbon", bpy.context.scene, False)
    ribbon_material = noodle.get_input(obj, ng, "Noodle Material")
    presets.apply_type(obj, "soba", bpy.context.scene, False)
    assert noodle.get_input(obj, ng, "Profile Aspect") == 1
    assert noodle.get_input(obj, ng, "Noodle Count") == 7
    assert noodle.get_input(obj, ng, "Collider") == collider
    assert noodle.get_input(obj, ng, "Noodle Material") != ribbon_material
    assert ribbon_material["noodle_type"] == "rice_ribbon"


def test_demo_is_separate_and_bowl_is_closed(noodle):
    import bmesh
    old_scene = bpy.context.scene
    bpy.ops.mesh.primitive_cube_add()
    existing = bpy.context.object
    scene, noodles, bowl = create_demo(bpy.context)
    assert scene != old_scene
    assert existing.name in old_scene.objects
    assert bpy.context.scene == scene
    assert scene.camera is not None
    assert sum(obj.type == "LIGHT" for obj in scene.objects) == 3
    assert noodle.get_input(noodles, noodle.noodle_modifier(noodles).node_group, "Collider") == bowl
    bm = bmesh.new()
    bm.from_mesh(bowl.data)
    try:
        assert all(edge.is_manifold for edge in bm.edges)
        assert bm.calc_volume(signed=True) > 0
    finally:
        bm.free()
    history = noodle.bake(noodles, 60)
    assert history[60][2] < history[1][2]
    assert history[60][1] > -0.005


def test_type_operator_uses_scene_selection(noodle):
    noodle_box.register()
    try:
        bpy.context.scene.noodle_box_type = "rice_ribbon"
        assert bpy.ops.noodle.add(noodle_type="rice_ribbon") == {"FINISHED"}
        obj = bpy.context.object
        assert obj["noodle_type"] == "rice_ribbon"
        bpy.context.scene.noodle_box_type = "udon"
        assert bpy.ops.noodle.apply_type() == {"FINISHED"}
        assert obj["noodle_type"] == "udon"
        assert bpy.ops.noodle.demo() == {"FINISHED"}
        assert bpy.context.scene.get("noodle_demo")
    finally:
        noodle_box.unregister()
