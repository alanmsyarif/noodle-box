"""Open a temporary GUI session, draw the sidebar, capture it, then exit."""
from pathlib import Path
import sys
import traceback

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import bpy
import noodle_box

bpy.context.preferences.view.show_splash = False
noodle_box.register()
bpy.ops.noodle.demo()


def capture():
    try:
        for area in bpy.context.screen.areas:
            if area.type == "VIEW_3D":
                area.spaces.active.show_region_ui = True
                for region in area.regions:
                    if region.type == "UI":
                        region.active_panel_category = "Noodles"
                area.tag_redraw()
        bpy.ops.wm.redraw_timer(type="DRAW_WIN_SWAP", iterations=2)
        bpy.ops.screen.screenshot(filepath=str(ROOT / "demo" / "sidebar.png"))
        print("UI_DRAW_OK", flush=True)
    except Exception:
        traceback.print_exc()
    finally:
        bpy.ops.wm.quit_blender()


bpy.app.timers.register(capture, first_interval=3.0)
