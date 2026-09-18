"""Test the distributable through Blender's actual extension installer.

Run in a dedicated --factory-startup Blender with BLENDER_USER_RESOURCES set
to a temporary directory. Never installs into the normal user profile.
"""
import os
import sys
from pathlib import Path

import bpy
import addon_utils

ROOT = Path(__file__).resolve().parents[1]
profile = os.environ.get("BLENDER_USER_RESOURCES")
assert profile, "Set an isolated BLENDER_USER_RESOURCES before running this test"
destination = Path(profile) / "extensions" / "noodle_test"
destination.mkdir(parents=True, exist_ok=True)
repos = bpy.context.preferences.extensions.repos
repo = repos.new(name="Noodle install test", module="noodle_test", custom_directory=str(destination))
repo.use_custom_directory = True
result = bpy.ops.extensions.package_install_files(
    filepath=str(ROOT / "dist" / "noodle_box-1.0.0.zip"),
    repo=repo.module, enable_on_install=True)
assert result == {"FINISHED"}, result
module_name = "bl_ext.noodle_test.noodle_box"
assert addon_utils.check(module_name)[1], "extension was not enabled"
assert bpy.ops.noodle.add(noodle_type="ramen") == {"FINISHED"}
assert bpy.context.object["noodle_type"] == "ramen"
assert bpy.ops.noodle.demo() == {"FINISHED"}
assert bpy.context.scene.get("noodle_demo")
addon_utils.disable(module_name, default_set=True)
assert not hasattr(bpy.types.Scene, "noodle_box_type")
addon_utils.enable(module_name, default_set=True)
assert addon_utils.check(module_name)[1]
print("INSTALL_OK: ZIP installed, enabled, created noodles and demo, disabled, re-enabled")
