"""Run in Blender to save the playable demo and a rendered static preview.

Environment overrides for quick checks: NOODLE_DEMO_SAMPLES, NOODLE_DEMO_SIZE
(e.g. "750x500"), NOODLE_DEMO_THREADS.
"""
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import bpy
from noodle_box import scene_assets, solver
from noodle_box.demo import create_demo

DEST = ROOT / "demo"
DEST.mkdir(exist_ok=True)
SAMPLES = int(os.environ.get("NOODLE_DEMO_SAMPLES", 384))
SIZE = tuple(int(v) for v in os.environ.get("NOODLE_DEMO_SIZE", "1600x1067").split("x"))
THREADS = int(os.environ.get("NOODLE_DEMO_THREADS", 0))

solver.register()
scene, noodles, bowl = create_demo(bpy.context)
for frame in range(1, 101):
    scene.frame_set(frame)
    noodles.evaluated_get(bpy.context.evaluated_depsgraph_get())

# A mesh snapshot guarantees that the downloaded file includes a finished
# example without relying on temporary simulation caches surviving a save.
depsgraph = bpy.context.evaluated_depsgraph_get()
evaluated = noodles.evaluated_get(depsgraph)
mesh = bpy.data.meshes.new_from_object(evaluated, depsgraph=depsgraph)
# The static preview adds scallion garnish scattered onto the baked pile.
preview, snapshot = scene_assets.make_preview_scene(scene, noodles, mesh, SAMPLES, SIZE)
if THREADS:
    preview.render.threads_mode = "FIXED"
    preview.render.threads = THREADS
preview.render.filepath = str(DEST / "udon_bowl.png")
bpy.context.window.scene = preview
started = time.time()
bpy.ops.render.render(write_still=True)
print("RENDER_SECONDS", round(time.time() - started, 1))

# Open the deliverable on the playable scene, ready to start at frame 1.
bpy.context.window.scene = scene
solver.reset_simulation(bpy.context, noodles)
solver.prepare_scene(bpy.context, noodles)
bpy.context.preferences.filepaths.save_version = 0
bpy.ops.wm.save_as_mainfile(filepath=str(DEST / "noodle_box_demo.blend"))
print("DEMO_SAVED", DEST / "noodle_box_demo.blend")
