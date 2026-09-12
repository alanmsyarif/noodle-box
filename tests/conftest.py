"""Load the single-file solver as a module.

The solver deliberately stays one file that a user can open in Blender's Text
Editor and run, so there is no package to import. This fixture loads the script
by path instead, which keeps the repo layout exactly as shipped.
"""
import importlib.util
import os
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
# NOODLE_SCRIPT lets the suite run against another revision of the solver, which
# is how the regression tests were shown to fail on the code they describe:
#   NOODLE_SCRIPT=../old/noodle_physics.py pytest tests/
SCRIPT = pathlib.Path(os.environ.get("NOODLE_SCRIPT", ROOT / "noodle_physics.py"))
if not SCRIPT.is_absolute():
    SCRIPT = (ROOT / SCRIPT).resolve()

# bpy is only available under Blender's own interpreter or the bpy wheel, which
# is version-locked to one Blender release and to one CPython. Skip loudly
# rather than failing the suite on a machine that has neither.
pytest.importorskip(
    "bpy",
    reason="needs Blender's Python: pip install bpy==5.2.1 (CPython 3.13)",
)


@pytest.fixture(scope="session")
def noodle():
    """The solver module, loaded from the file at the repo root."""
    spec = importlib.util.spec_from_file_location("noodle_physics", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["noodle_physics"] = module
    spec.loader.exec_module(module)
    return module


def bake_centre_of_mass(noodle, obj, frames):
    """Mean vertex z per frame.

    A single straight rod translates rather than rotates, so its centre of mass
    is what the velocity clamp actually bounds. Reading the lowest vertex
    instead measures the tip, which swings once the rod tilts, and made an
    earlier probe disagree with itself.
    """
    import bpy

    coms = []
    for frame in range(1, frames + 1):
        bpy.context.scene.frame_set(frame)
        evaluated = obj.evaluated_get(bpy.context.evaluated_depsgraph_get())
        mesh = evaluated.to_mesh()
        try:
            coms.append(sum(v.co.z for v in mesh.vertices) / len(mesh.vertices))
        finally:
            evaluated.to_mesh_clear()
    return coms


def build_rod(noodle, substeps, radius=2.5, count=1):
    """One straight, stiff rod: the simplest thing the clamp can be measured on.

    Spawn Coil 0 gives the vertical rod instead of the default nest, so the
    geometry is rigid and its motion is free fall until the clamp bites.
    """
    ng = noodle.build_group()
    obj = noodle.build_object(ng)
    noodle.set_input(obj, ng, "Noodle Count", count)
    noodle.set_input(obj, ng, "Spawn Coil", 0.0)
    noodle.set_input(obj, ng, "Stiffness", 1.0)
    noodle.set_input(obj, ng, "Noodle Radius", radius)
    noodle.set_input(obj, ng, "Substeps", substeps)
    return ng, obj
