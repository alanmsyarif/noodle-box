"""Load the compatibility launcher and isolate each Blender test scene."""
import importlib.util
import os
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
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


@pytest.fixture(autouse=True)
def isolated_scene():
    """Independent test scenes, since building no longer deletes prior piles."""
    import bpy
    bpy.ops.wm.read_factory_settings(use_empty=True)
    yield


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
    # These regressions measure the original large-unit speed cap. Keep the
    # fixture explicit now that the interactive defaults are metres.
    for name, value in {"Noodle Length": 1000.0, "Start Height": 1250.0,
                        "Fill Diameter": 400.0, "Gravity": 386.0,
                        "Adaptive Substeps": False}.items():
        noodle.set_input(obj, ng, name, value)
    noodle.set_input(obj, ng, "Noodle Count", count)
    noodle.set_input(obj, ng, "Spawn Coil", 0.0)
    noodle.set_input(obj, ng, "Stiffness", 1.0)
    noodle.set_input(obj, ng, "Noodle Radius", radius)
    noodle.set_input(obj, ng, "Substeps", substeps)
    return ng, obj
