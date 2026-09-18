"""Run in Blender to save the playable demo and a rendered static preview."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import bpy
from noodle_box import solver
from noodle_box.demo import create_demo

DEST = ROOT / "demo"
DEST.mkdir(exist_ok=True)
solver.register()
scene, noodles, bowl = create_demo(bpy.context)
for frame in range(1, 101):
    scene.frame_set(frame)
    noodles.evaluated_get(bpy.context.evaluated_depsgraph_get())

# A mesh snapshot guarantees that the downloaded file includes a finished
# example without relying on temporary simulation caches surviving a save.
evaluated = noodles.evaluated_get(bpy.context.evaluated_depsgraph_get())
mesh = bpy.data.meshes.new_from_object(evaluated, depsgraph=bpy.context.evaluated_depsgraph_get())
snapshot = bpy.data.objects.new("Udon - static frame 100 preview", mesh)
preview = bpy.data.scenes.new("Noodle Box - Finished Preview")
for obj in scene.objects:
    if obj != noodles:
        preview.collection.objects.link(obj)
preview.collection.objects.link(snapshot)
preview.camera = scene.camera
preview.world = scene.world
preview.unit_settings.system = "METRIC"
preview.render.engine = "CYCLES"
preview.cycles.samples = 48
preview.cycles.use_denoising = True
preview.view_settings.view_transform = "AgX"
preview.render.resolution_x, preview.render.resolution_y = 1200, 900
preview.render.resolution_percentage = 100
preview.render.filepath = str(DEST / "udon_bowl.png")
bpy.context.window.scene = preview
bpy.ops.render.render(write_still=True)

# Open the deliverable on the playable scene, ready to start at frame 1.
bpy.context.window.scene = scene
solver.reset_simulation(bpy.context, noodles)
solver.prepare_scene(bpy.context, noodles)
bpy.context.preferences.filepaths.save_version = 0
bpy.ops.wm.save_as_mainfile(filepath=str(DEST / "noodle_box_demo.blend"))
print("DEMO_SAVED", DEST / "noodle_box_demo.blend")
