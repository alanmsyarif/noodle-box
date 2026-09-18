"""Create a separate, lit bowl scene without deleting any existing scene."""
from math import cos, sin, tau

import bpy
from mathutils import Vector

from . import solver
from .presets import apply_type


def material(name, colour, roughness):
    mat = bpy.data.materials.new(name)
    mat.diffuse_color = colour
    bsdf = mat.node_tree.nodes.get("Principled BSDF")
    bsdf.inputs["Base Color"].default_value = colour
    bsdf.inputs["Roughness"].default_value = roughness
    return mat


def bowl_mesh(collection):
    # Closed ceramic shell, including the underside. No collision modifier
    # required; an open single-surface bowl would leak through the SDF.
    profile = [(0, .014), (.055, .014), (.078, .017), (.097, .025),
               (.115, .040), (.129, .059), (.136, .078), (.138, .082),
               (.143, .082), (.144, .077), (.137, .056), (.122, .032),
               (.099, .014), (.073, .004), (.052, .003), (0, .003)]
    vertices, rings, faces = [], [], []
    sides = 96
    for radius, height in profile:
        ring = []
        for i in range(sides if radius else 1):
            ring.append(len(vertices))
            angle = tau * i / sides
            vertices.append((radius * cos(angle), radius * sin(angle), height))
        rings.append(ring)
    for a, b in zip(rings, rings[1:]):
        for i in range(sides):
            j = (i + 1) % sides
            if len(a) == 1:
                faces.append((a[0], b[i], b[j]))
            elif len(b) == 1:
                faces.append((a[i], b[0], a[j]))
            else:
                faces.append((a[i], b[i], b[j], a[j]))
    mesh = bpy.data.meshes.new("Closed ceramic bowl")
    mesh.from_pydata(vertices, [], faces)
    mesh.update()
    obj = bpy.data.objects.new("Bowl - closed collider", mesh)
    collection.objects.link(obj)
    for face in mesh.polygons:
        face.use_smooth = True
    mesh.materials.append(material("Glazed teal ceramic", (.018, .16, .17, 1), .24))
    return obj


def point_at(obj, target):
    obj.rotation_euler = (Vector(target) - obj.location).to_track_quat('-Z', 'Y').to_euler()


def create_demo(context):
    scene = bpy.data.scenes.new("Noodle Box - Udon Bowl")
    # Switch the window's scene, leaving the previous scene and its data intact.
    context.window.scene = scene
    collection = bpy.data.collections.new("Noodle Box Demo")
    scene.collection.children.link(collection)
    scene.frame_start, scene.frame_end = 1, 160
    scene.render.fps = 24
    scene.render.engine = "CYCLES"
    scene.cycles.samples = 32
    scene.cycles.use_denoising = True
    scene.render.resolution_x, scene.render.resolution_y = 1200, 900
    scene.render.resolution_percentage = 100
    scene.world = bpy.data.worlds.new("Noodle Box Studio")
    scene.world.node_tree.nodes["Background"].inputs["Color"].default_value = (.3, .36, .42, 1)
    scene.world.node_tree.nodes["Background"].inputs["Strength"].default_value = .3
    scene.view_settings.view_transform = "AgX"

    bowl = bowl_mesh(collection)
    ng = solver.build_group()
    noodles = solver.build_object(ng)
    # Keep all demo objects together, including the procedural carrier.
    for owner in list(noodles.users_collection):
        owner.objects.unlink(noodles)
    collection.objects.link(noodles)
    noodles.name = "Udon - select for controls"
    apply_type(noodles, "udon", scene)
    solver.set_input(noodles, ng, "Noodle Count", 24)
    solver.set_input(noodles, ng, "Fill Diameter", .09)
    solver.set_input(noodles, ng, "Collider", bowl)
    solver.set_input(noodles, ng, "Collider Friction", .65)

    mesh = bpy.data.meshes.new("Studio ground")
    mesh.from_pydata([(-200, -200, 0), (200, -200, 0), (200, 200, 0), (-200, 200, 0)],
                     [], [(0, 1, 2, 3)])
    ground = bpy.data.objects.new("Warm studio surface", mesh)
    collection.objects.link(ground)
    mesh.materials.append(material("Warm limestone", (.33, .25, .17, 1), .7))

    camera = bpy.data.objects.new("Demo camera", bpy.data.cameras.new("Demo camera"))
    collection.objects.link(camera)
    camera.location = (.43, -.55, .52)
    point_at(camera, (0, 0, .055))
    camera.data.type = "ORTHO"
    camera.data.ortho_scale = .43
    scene.camera = camera
    for name, location, power, size, colour in (
        ("Large softbox", (.1, -.25, .65), 20, .45, (1, .88, .72)),
        ("Cool rim", (-.35, .18, .4), 12, .30, (.72, .85, 1)),
        ("Front fill", (.35, -.1, .25), 5, .25, (1, 1, 1)),
    ):
        data = bpy.data.lights.new(name, "AREA")
        data.energy, data.shape, data.size, data.color = power, "DISK", size, colour
        light = bpy.data.objects.new(name, data)
        collection.objects.link(light)
        light.location = location
        point_at(light, (0, 0, .04))

    scene.timeline_markers.new("START - play forward", frame=1)
    scene.timeline_markers.new("Pile preview", frame=100)
    text = bpy.data.texts.new("Noodle Box - Read Me")
    text.write("NOODLE BOX / UDON BOWL\n\n"
               "Select 'Udon - select for controls'. Open the 3D Viewport sidebar (N), Noodles tab.\n"
               "Play from frame 1. Restart clears the simulation so you can change settings.\n"
               "Choose a noodle type, then Apply Type; thin noodles can need more steps than the budget.\n"
               "This bowl is a closed mesh collider. The camera and lighting are ready to render.\n"
               "The generated scene is separate from your previous work.\n"
               "The downloadable demo also contains an optional static frame-100 preview scene.\n")
    scene["noodle_demo"] = True
    solver.prepare_scene(context, noodles)
    for screen in bpy.data.screens:
        for area in screen.areas:
            if area.type == "VIEW_3D":
                area.spaces.active.region_3d.view_perspective = "CAMERA"
                area.spaces.active.show_region_ui = True
                area.spaces.active.shading.color_type = "MATERIAL"
    return scene, noodles, bowl
