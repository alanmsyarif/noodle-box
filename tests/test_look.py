"""Noodle look: materials, surface-only inputs and the display surface."""
import bpy
import numpy as np
import pytest

from noodle_box import materials, presets

SURFACE_SOCKETS = ("Profile Squareness", "Waviness", "Thickness Variation",
                   "End Softness", "Render Faces")
ZONE_NODES = {"GeometryNodeSimulationInput", "GeometryNodeSimulationOutput",
              "GeometryNodeRepeatInput", "GeometryNodeRepeatOutput"}


def principled(material):
    return next(n for n in material.node_tree.nodes if n.bl_idname == "ShaderNodeBsdfPrincipled")


@pytest.mark.parametrize("key", presets.NOODLE_TYPES)
def test_type_material_is_translucent_wet_and_surface_anchored(key):
    material = presets.type_material(key)
    assert material["noodle_type"] == key
    assert material["noodle_look_version"] == materials.LOOK_VERSION
    bsdf = principled(material)
    assert bsdf.subsurface_method == "RANDOM_WALK"
    assert bsdf.inputs["Subsurface Weight"].default_value > 0.3
    assert bsdf.inputs["Coat IOR"].default_value == pytest.approx(1.33)
    for name in ("Base Color", "Roughness", "Coat Weight", "Normal", "Coat Normal"):
        assert bsdf.inputs[name].is_linked, name
    used = {n.attribute_name for n in material.node_tree.nodes if n.bl_idname == "ShaderNodeAttribute"}
    assert {"nb_prof", "nb_along", "nb_end", "np_id"} <= used
    # No world/object-space coordinates: those would swim as strands move.
    assert not any(n.bl_idname == "ShaderNodeTexCoord" for n in material.node_tree.nodes)
    assert presets.type_material(key) == material  # reused while current


def test_outdated_material_is_rebuilt_in_place(noodle):
    material = noodle.ensure_material()
    material["noodle_look_version"] = 0
    material.node_tree.nodes.clear()
    assert noodle.ensure_material() == material
    assert material["noodle_look_version"] == materials.LOOK_VERSION
    assert principled(material).inputs["Normal"].is_linked


def test_surface_sockets_exist_in_noodles_panel(noodle):
    ng = noodle.build_group()
    panel = {title: names for title, closed, names in noodle.CONTROL_GROUPS}["Noodles"]
    sockets = {s.name: s for s in ng.interface.items_tree if s.item_type == "SOCKET"}
    for name in SURFACE_SOCKETS:
        assert name in panel and name in sockets and name in noodle.DESCRIPTIONS
        assert sockets[name].parent.name == "Noodles"


def test_surface_inputs_never_reach_the_simulation(noodle):
    ng = noodle.build_group()
    gin = next(n for n in ng.nodes if n.bl_idname == "NodeGroupInput")
    for name in SURFACE_SOCKETS + ("Profile Aspect", "Noodle Material"):
        seen, todo = set(), [gin.outputs[name]]
        while todo:
            for link in todo.pop().links:
                node = link.to_node
                if node.name not in seen:
                    seen.add(node.name)
                    todo.extend(node.outputs)
        kinds = {ng.nodes[n].bl_idname for n in seen}
        assert "NodeGroupOutput" in kinds, name
        assert not kinds & ZONE_NODES, f"{name} feeds the simulation"


def test_apply_type_sets_surface_shape(noodle):
    ng = noodle.build_group()
    obj = noodle.build_object(ng)
    presets.apply_type(obj, "ramen", bpy.context.scene)
    look = presets.LOOKS["ramen"]
    assert noodle.get_input(obj, ng, "Waviness") == pytest.approx(look["waviness"])
    assert noodle.get_input(obj, ng, "Profile Squareness") == pytest.approx(look["squareness"])


def evaluated_arrays(obj, *names):
    evaluated = obj.evaluated_get(bpy.context.evaluated_depsgraph_get())
    mesh = evaluated.to_mesh()
    try:
        out = {}
        co = np.empty(len(mesh.vertices) * 3, dtype=np.float32)
        mesh.vertices.foreach_get("co", co)
        out["co"] = co.reshape(-1, 3)
        for name in names:
            attribute = mesh.attributes[name]
            width = 3 if attribute.data_type == "FLOAT_VECTOR" else 1
            values = np.empty(len(attribute.data) * width, dtype=np.float32)
            attribute.data.foreach_get("vector" if width == 3 else "value", values)
            out[name] = values.reshape(-1, width) if width == 3 else values
        return out
    finally:
        evaluated.to_mesh_clear()


def test_surface_attributes_and_softened_ends(noodle):
    ng = noodle.build_group()
    obj = noodle.build_object(ng)
    presets.apply_type(obj, "udon", bpy.context.scene)
    noodle.set_input(obj, ng, "Noodle Count", 2)
    noodle.set_input(obj, ng, "Thickness Variation", 0.0)
    bpy.context.scene.frame_set(1)
    data = evaluated_arrays(obj, "nb_prof", "nb_along", "nb_end", "np_id")
    radius = presets.NOODLE_TYPES["udon"]["radius"]
    # Rounded square: width and thickness stay within the radius per axis.
    assert np.abs(data["nb_prof"][:, :2]).max() <= radius * 1.0001
    assert data["nb_along"].max() > 0.1
    assert data["nb_end"].min() == pytest.approx(0.0, abs=1e-7)
    # Cut-end rings are pulled in (rounded), mid-strand rings are full size.
    faces = 8  # viewport profile
    rings = data["co"].reshape(-1, faces, 3)
    ring_r = np.linalg.norm(rings - rings.mean(axis=1, keepdims=True), axis=2).max(axis=1)
    ring_end = data["nb_end"].reshape(-1, faces)[:, 0]
    assert ring_r[ring_end == 0].max() < radius * 0.85
    assert ring_r[ring_end > 2 * radius].min() > radius * 0.95


def test_waviness_moves_only_the_display_surface(noodle):
    """Same seed with and without crinkle: identical simulation, different tube."""
    results = []
    for waviness in (0.0, 1.0):
        bpy.ops.wm.read_factory_settings(use_empty=True)
        ng = noodle.build_group()
        obj = noodle.build_object(ng)
        noodle.set_input(obj, ng, "Noodle Count", 3)
        noodle.set_input(obj, ng, "Waviness", waviness)
        noodle.set_input(obj, ng, "Smooth Surface", True)
        for frame in range(1, 13):
            bpy.context.scene.frame_set(frame)
        results.append(evaluated_arrays(obj, "nb_along", "np_vel"))
    flat, wavy = results
    # np_vel is simulation state carried onto the mesh: it must not change.
    assert np.allclose(flat["np_vel"], wavy["np_vel"])
    assert np.allclose(flat["nb_along"], wavy["nb_along"])
    assert np.abs(flat["co"] - wavy["co"]).max() > 0.001
