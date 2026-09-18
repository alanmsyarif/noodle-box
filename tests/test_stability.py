"""Settled piles must not shake, and damping must not freeze moving contacts."""
import bpy
import numpy as np
import pytest


def centres(obj, sides=6):
    evaluated = obj.evaluated_get(bpy.context.evaluated_depsgraph_get())
    mesh = evaluated.to_mesh()
    try:
        coords = np.empty(len(mesh.vertices) * 3, dtype=np.float32)
        mesh.vertices.foreach_get("co", coords)
        # Curve to Mesh emits one profile ring per simulated point.
        return coords.reshape(-1, sides, 3).mean(axis=1)
    finally:
        evaluated.to_mesh_clear()


def test_settled_dense_pile_has_low_jitter(noodle):
    ng = noodle.build_group()
    obj = noodle.build_object(ng)
    noodle.set_input(obj, ng, "Noodle Count", 48)
    noodle.set_input(obj, ng, "Fill Diameter", 0.08)
    positions = []
    for frame in range(1, 201):
        bpy.context.scene.frame_set(frame)
        points = centres(obj)
        if frame >= 140:
            positions.append(points)
    positions = np.asarray(positions)
    # Second differences reject steady drift and measure frame-to-frame shake.
    # Before the fix RMS shake was 2.80 mm, with p95 steps of 4.84 mm.
    acceleration = np.diff(positions, n=2, axis=0)
    steps = np.linalg.norm(np.diff(positions, axis=0), axis=-1)
    assert np.sqrt(np.mean(acceleration ** 2)) < 0.0008
    assert np.percentile(steps, 95) < 0.0018
    assert positions[-1, :, 2].max() > 0.012, "pile collapsed into the floor layer"


def test_damped_contacts_follow_animated_collider(noodle):
    bpy.ops.mesh.primitive_cube_add(size=1, location=(0, 0, 0.03))
    slab = bpy.context.object
    slab.scale = (0.8, 0.8, 0.06)
    ng = noodle.build_group()
    obj = noodle.build_object(ng)
    noodle.set_input(obj, ng, "Noodle Count", 4)
    noodle.set_input(obj, ng, "Collider", slab)
    before = None
    for frame in range(1, 161):
        # After resting, lift the surface 4 cm over 40 frames.
        slab.location.z = 0.03 + max(0, frame - 120) * 0.001
        bpy.context.scene.frame_set(frame)
        points = centres(obj)
        if frame == 120:
            before = points[:, 2].mean()
        if frame > 120:
            assert points[:, 2].min() >= slab.location.z + 0.03, "lost collider contact"
    assert points[:, 2].mean() > before + 0.035, "damping froze the lifted pile"


def test_old_graph_upgrade_preserves_inputs_and_other_users(noodle):
    ng = noodle.build_group()
    obj = noodle.build_object(ng)
    other = noodle.build_object(ng)
    ng["noodle_solver_revision"] = 2
    noodle.set_input(obj, ng, "Noodle Count", 7)
    noodle.set_input(obj, ng, "Noodle Radius", 0.008)
    obj.location = (1, 2, 3)
    # Match an old graph's interface, which lacks the new damping control.
    socket = next(s for s in ng.interface.items_tree if s.name == "Contact Damping")
    ng.interface.remove(socket)
    assert noodle.upgrade_solver(bpy.context, obj)
    current = noodle.noodle_modifier(obj).node_group
    assert current != ng
    assert noodle.noodle_modifier(other).node_group == ng
    assert tuple(obj.location) == (1, 2, 3)
    assert noodle.get_input(obj, current, "Noodle Count") == 7
    assert noodle.get_input(obj, current, "Noodle Radius") == pytest.approx(0.008)
    assert noodle.get_input(obj, current, "Contact Damping") == 1
    assert not noodle.upgrade_solver(bpy.context, obj)
