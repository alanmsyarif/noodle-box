"""Create a separate, photographically lit bowl scene without deleting any
existing scene. The set itself (bowl, table, light, camera) lives in
scene_assets so the demo stays quick to create: nothing is baked here."""
import bpy

from . import scene_assets, solver
from .presets import apply_type


def bowl_mesh(collection):
    """Closed, watertight thrown bowl used as the noodle collider."""
    return scene_assets.bowl_mesh(collection)


def create_demo(context):
    scene = bpy.data.scenes.new("Noodle Box - Udon Bowl")
    # Switch the window's scene, leaving the previous scene and its data intact.
    context.window.scene = scene
    collection = bpy.data.collections.new("Noodle Box Demo")
    scene.collection.children.link(collection)
    scene.frame_start, scene.frame_end = 1, 160
    scene.render.fps = 24

    bowl = bowl_mesh(collection)
    ng = solver.build_group()
    noodles = solver.build_object(ng)
    # Keep all demo objects together, including the procedural carrier.
    for owner in list(noodles.users_collection):
        owner.objects.unlink(noodles)
    collection.objects.link(noodles)
    noodles.name = "Udon - select for controls"
    apply_type(noodles, "udon", scene)
    # A generous serving: a full mound in the well (36 strands, about 0.7 L
    # of noodle); measured ~0.2 s/frame headless on a shared 4-core box.
    solver.set_input(noodles, ng, "Noodle Count", 36)
    solver.set_input(noodles, ng, "Fill Diameter", .09)
    # A loose, mostly hanging spawn instead of wide flat coils: full coils
    # reach ~17 cm from the axis and drape strands over a 20 cm bowl's rim.
    # At 0.3 every strand lands inside (checked over several seeds).
    solver.set_input(noodles, ng, "Spawn Coil", .3)
    solver.set_input(noodles, ng, "Collider", bowl)
    solver.set_input(noodles, ng, "Collider Friction", .65)

    # Oak table, window light, bounce and flag cards, room world, 85 mm camera
    # with shallow depth of field, Cycles + AgX.
    scene_assets.build_set(scene, collection)

    scene.timeline_markers.new("START - play forward", frame=1)
    scene.timeline_markers.new("Pile preview", frame=100)
    text = bpy.data.texts.new("Noodle Box - Read Me")
    text.write("NOODLE BOX / UDON BOWL\n\n"
               "Select 'Udon - select for controls'. Open the 3D Viewport sidebar (N), Noodles tab.\n"
               "Play from frame 1. Restart clears the simulation so you can change settings.\n"
               "Choose a noodle type, then Apply Type; thin noodles can need more steps than the budget.\n"
               "The thrown stoneware bowl is a closed mesh collider (19.5 cm x 8.3 cm).\n"
               "The set is lit like a food photograph: a window key behind-left, a bounce\n"
               "card front-right, a black flag and a soft room fill. The 85 mm camera\n"
               "focuses on the 'Focus - front of noodles' empty at f/4.\n"
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
