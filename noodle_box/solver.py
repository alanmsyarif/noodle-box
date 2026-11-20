"""Noodle Box solver and sidebar for Blender 5.2+.

A position-based noodle solver built entirely in Geometry Nodes. Coordinates
are metres, time is seconds, and gravity defaults to 9.81 m/s squared. Enable
the Noodle Box extension, then open Viewport > Sidebar > Noodles. Existing
objects are preserved; Add Noodles creates another independent simulation.

Point spacing is derived from each strand length and diameter. Each substep
limits travel to 0.85 segments, then solves stretch, XPBD bending, nearest-point
contact, cohesion, floor and signed-distance-field collider contact. Contact
is approximate; this is a preview rope solver, not a continuous collision solver.

Adaptive substeps use the fastest stored point velocity plus this frame of
gravity, bounded by the Substeps budget. Disable adaptation for fixed-step
comparisons. The budget must still cover the intended fall speed; the sidebar
can calculate it and warns when the runtime ceiling is insufficient.

CLI arguments follow Blender's -- separator. Use --python-exit-code 1 before
--python to make exceptions fail headless jobs. No external packages required.
"""

import sys
from math import ceil, isfinite, sqrt, tau


class CliError(ValueError):
    """A user-facing command-line validation error."""


def positive_int(value, label, minimum=1):
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise CliError(f"{label} must be an integer, got {value!r}") from exc
    if parsed < minimum:
        raise CliError(f"{label} must be >= {minimum}, got {parsed}")
    return parsed


def require_preset(preset):
    """Validate a preset name against the tables, which are the single source."""
    if preset not in preset_names():
        raise CliError(f"unknown preset {preset!r}; choose from "
                       f"{', '.join(preset_names())}")
    return preset


def parse_mode_args(argv):
    """Parse the small Blender-side CLI without hiding malformed input."""
    if not argv:
        return ("build",)
    if argv[0] == "--check" and len(argv) == 1:
        return ("check",)
    if argv[0] == "--bench":
        i = argv.index("--bench") + 1
        rest = argv[i:]
        preset = require_preset(rest[0] if rest else "default")
        count = positive_int(rest[1], "count") if len(rest) > 1 else 120
        frames = positive_int(rest[2], "frames") if len(rest) > 2 else 60
        if len(rest) > 3:
            raise CliError("--bench accepts at most preset, count, and frames")
        return ("bench", preset, count, frames)
    if argv[0] == "--realtime":
        i = argv.index("--realtime") + 1
        rest = argv[i:]
        preset = require_preset(rest[0] if rest else "balanced")
        if len(rest) > 1:
            raise CliError("--realtime accepts one preset")
        return ("realtime", preset)
    raise CliError("expected --check, --bench [preset count frames], or --realtime [preset]")


import bpy
from . import materials
from .presets import NOODLE_TYPES, TYPE_ITEMS

NG_NAME = "Noodle Physics"
OBJ_NAME = "NoodlePhysics"
MAT_NAME = "Noodle"
SOLVER_REVISION = 5

# Interactive startup preset. All presets use metres; None uses PARAMS.
PRESET = "balanced"

ATTR_ID = "np_id"      # per-noodle identity
ATTR_VEL = "np_vel"    # carried between frames inside the simulation state
ATTR_PREV = "np_prev"  # position at the top of this substep
ATTR_NEAR = "np_near"      # index of this point's nearest neighbour
ATTR_BLAM = "np_blam"      # XPBD multiplier for the bend centred on this point
ATTR_BDLAM = "np_bdlam"    # its change this iteration, shared with the neighbours
ATTR_LEN = "np_len"        # this noodle's own length, once Length Variation applies
ATTR_ROOT = "np_root"      # where this noodle was spawned on the disc
ATTR_CDIST = "np_cdist"    # signed distance to the collider, from the SDF
ATTR_CNRM = "np_cnrm"      # outward normal of the nearest collider surface
ATTR_SPUSH = "np_spush"    # this substep's contact push from the floor and colliders
ATTR_NPUSH = "np_npush"    # this substep's contact push from other noodles
ATTR_NEAR2 = "np_near2"    # a second neighbour, from the other half of the query
ATTR_DRIFT = "np_drift"    # running mean of the displacement per substep
ATTR_HOLD = "np_hold"      # 1 where a resting point is also held in place
ATTR_REST = "np_rest"      # 1 where a point is held at rest by static friction this substep
ATTR_AGE = "np_age"        # seconds since the spawn
ATTR_BREST = "np_brest"    # plastically set rest bend (offset from the neighbours' midpoint)
ATTR_SFRIC = "np_sfric"    # this substep's floor/collider friction, as a displacement
ATTR_SDIR = "np_sdir"      # unit direction of the segment ending at this point
ATTR_SABC = "np_sabc"      # tridiagonal row (a, b, c) of the stretch solve
ATTR_SD = "np_sd"          # right-hand side, then multiplier, of the stretch solve
# Bending solve: tridiagonal rows, unit solves, capacitance and work vectors.
ATTR_BEND_AC = "np_bac"
ATTR_BEND_B = "np_bb"
ATTR_BEND_E = "np_be"
ATTR_BEND_Y = "np_by"
ATTR_BEND_S = "np_bs"
ATTR_BEND_DRE = "np_bdre"
ATTR_BEND_DIM = "np_bdim"
ATTR_BEND_TMP_E = "np_btmpe"
ATTR_BEND_TMP_AC = "np_btmpac"
ATTR_BEND_TMP_B = "np_btmpb"
ATTR_BEND_LEVEL = "np_bk"  # + round number: that round's elimination factors
ATTR_BEND_KEY = "np_bkey"  # the compliance the stored factorisation was built for

# The collider is read as a signed distance field, voxelised once a frame.
# Voxel size is a fraction of the noodle radius; Band Width is how many voxels
# of narrow band OpenVDB keeps either side of the surface, which is also how
# deep a buried point can still be pushed back out - 6 voxels here is three
# noodle diameters. Finer grids buy nothing measurable and cost memory fast: a
# 1200-unit bowl at a quarter of the radius ran past 3 GB.
SDF_VOXEL = 1.0
SDF_BAND = 6

# Solver scratch. All of it used to ride out through Curve to Mesh onto every
# vertex of the tube, interpolated, for data nothing downstream reads. Stripped
# before the surface; np_id and np_vel are deliberately kept, because they are
# genuinely useful in a shader.
SCRATCH_ATTRS = (ATTR_HOLD, ATTR_DRIFT, ATTR_NEAR2, ATTR_REST, ATTR_AGE, ATTR_BREST, ATTR_PREV, ATTR_NEAR, ATTR_CDIST, ATTR_CNRM, ATTR_SPUSH, ATTR_NPUSH, ATTR_SFRIC,
                 ATTR_SDIR, ATTR_SABC, ATTR_SD,
                 ATTR_BEND_AC, ATTR_BEND_B, ATTR_BEND_E, ATTR_BEND_Y, ATTR_BEND_S,
                 ATTR_BEND_DRE, ATTR_BEND_DIM, ATTR_BEND_TMP_E, ATTR_BEND_TMP_AC,
                 ATTR_BEND_TMP_B, ATTR_BEND_KEY,
                 *(ATTR_BEND_LEVEL + str(level) for level in range(12)),
                 ATTR_BLAM, ATTR_BDLAM, ATTR_LEN, ATTR_ROOT)

# How far past touching cooked pasta still clings, as a multiple of diameter.
COHESION_REACH = 1.8

# Ceiling on one contact pass, as a fraction of a segment. Keeps a spawn-time
# overlap from converting into launch velocity.
PUSH_LIMIT = 0.25

# Leave a little clearance under the one-segment displacement bound. The
# strict bound is enough for static samples, but the margin prevents a point
# landing exactly on the next sample from tunnelling after rounding and
# makes the limit robust when a collider moves during a frame.
VELOCITY_SAFETY = 0.85

# Hard runtime ceilings keep a mistaken socket value from multiplying the
# Geometry Nodes graph into an unusable frame. The exposed sockets remain wide
# for experimentation, but values beyond these limits no longer create runaway
# evaluation times or make the simulation appear frozen.
MAX_RUNTIME_SUBSTEPS = 24
MAX_RUNTIME_ITERATIONS = 12

# How far above its spawn height the pile top may rise before --check calls it
# a launch. A coiled noodle uncoils as it falls and lifts the top a little even
# when the solver is healthy, so this is a ceiling on the failure rather than a
# promise of no rise. It is set above the 1.30x this scene measures today and
# below the 1.70x it measured before the substep count was fixed, so it fails on
# the behaviour it is there to catch.
LAUNCH_LIMIT = 1.5


# Spawn shape: each strand starts as a broad loop in a loose coil (see build_group).
SPAWN_WAVE = 0.07          # m of strand over which the waviness varies
SPAWN_WAVE_RADIAL = 0.3    # in/out waviness, in diameters per unit of noise, at Spawn Coil 1
SPAWN_WAVE_HEIGHT = 0.3    # up/down waviness, in diameters per unit of noise, at Spawn Coil 1
SPAWN_RINGS = 3            # concentric loops per level of the coil
SPAWN_SETTLE = 0.1        # seconds the spawned strands untangle before they fall
SPAWN_LOWER_TIME = 1.5    # seconds the portion is lowered in rather than dropped
SPAWN_LOWER_SPEED = 0.4   # m/s, the lowering speed

# ------------------------------------------------------------ material model
# Cooked wheat noodles are about as dense as water with a little starch in it.
NOODLE_DENSITY = 1050.0  # kg/m^3

# Stiffness 0..1 maps logarithmically onto Young's modulus between these, so
# the slider reads evenly from overcooked to barely cooked. Measured cooked
# noodles sit around 50-300 kPa (udon, ramen) up to a few MPa (al dente
# spaghetti). Each noodle type stores its modulus and converts with
# stiffness_for_modulus().
YOUNG_MIN = 10.0e3   # Pa, Stiffness 0
YOUNG_MAX = 10.0e6   # Pa, Stiffness 1

# Quadratic air drag across a strand, a = AIR_DRAG / radius * |v|^2, from
# 0.5 * rho_air * Cd * (2 r) / (rho * pi * r^2) with rho_air 1.2 and Cd 1.2.
AIR_DRAG = 0.5 * 1.2 * 1.2 * 2.0 / (NOODLE_DENSITY * 3.141592653589793)

# Viscoelastic relaxation time of bending, in seconds: cooked dough is lossy,
# so a bent strand eases into shape instead of springing back and ringing.
BEND_RELAXATION = 0.03

# Plasticity: a held bend sets. Rest bends creep towards PLASTIC_SET of the
# current bend with time constant PLASTIC_TIME seconds.
PLASTIC_SET = 0.6
PLASTIC_TIME = 0.4


# Adaptive substeps keep a resting point's per-step gravity sag below this
# fraction of the radius. 0.05 measured the same settled heap for 20% more time.
SAG_FRACTION = 0.1

# Share of a noodle contact's overlap a point resolves itself: CONTACT_SHARE,
# plus STACK_SHARE times the upward component of the contact normal, so the
# strand on top moves more (shock propagation).
CONTACT_SHARE = 0.5
STACK_SHARE = 0.45

# Sockets whose meaning changed in solver revision 5; upgrades reset them.
REVISION_5_PHYSICS = ("Damping", "Contact Damping", "Stiffness", "Friction")

# Default for the Contact Damping socket.
DEFAULT_CONTACT_DAMPING = 1.0

# Coming to rest: a point in contact whose mean drift over SETTLE_TIME
# seconds is slower than SETTLE_FRACTION * sqrt(g r) (44 mm/s for udon) is
# held still, unless it is sliding on a surface faster than friction allows.
SETTLE_FRACTION = 0.2
SETTLE_TIME = 0.3

# Contact skin, as a fraction of the radius. Anything a point comes this close
# to counts as touching, for friction purposes.
CONTACT_SKIN = 1.2


def modulus_for_stiffness(stiffness):
    """Young's modulus in pascals for a Stiffness value."""
    stiffness = min(max(stiffness, 0.0), 1.0)
    return YOUNG_MIN * (YOUNG_MAX / YOUNG_MIN) ** stiffness


def stiffness_for_modulus(modulus):
    """Stiffness value for a Young's modulus in pascals (clamped to 0..1)."""
    from math import log
    return min(max(log(modulus / YOUNG_MIN) / log(YOUNG_MAX / YOUNG_MIN), 0.0), 1.0)


def elastogravity_length(modulus, radius, gravity=9.81, density=NOODLE_DENSITY):
    """L_g = (E I / (rho g A))^(1/3) for a round strand: the bend radius of a drape."""
    return (modulus * radius * radius / (4.0 * density * gravity)) ** (1.0 / 3.0)


NOODLE_SPREAD = 51.7   # noise-space gap between noodles, for the start jitter

# name, socket type, default, min, max, subtype
PARAMS = [
    ("Noodle Count",    "NodeSocketInt",   48,     1,     100000, None),
    ("Seed",            "NodeSocketInt",   0,      0,     100000, None),
    ("Noodle Length",   "NodeSocketFloat", 0.25,   0.001, 100.0,  "DISTANCE"),
    ("Length Variation","NodeSocketFloat", 0.15,   0.0,   0.9,    "FACTOR"),
    ("Spawn Coil",      "NodeSocketFloat", 1.0,    0.0,   1.0,    "FACTOR"),
    ("Noodle Radius",   "NodeSocketFloat", 0.006,  0.0001, 10.0,  "DISTANCE"),
    ("Profile Faces",   "NodeSocketInt",   6,      3,     64,     None),
    ("Profile Aspect",  "NodeSocketFloat", 1.0,    0.1,   1.0,    "FACTOR"),
    ("Smooth Surface",  "NodeSocketBool", False,  None,  None,   None),
    ("Noodle Material", "NodeSocketMaterial", None, None, None, None),
    ("Fill Diameter",   "NodeSocketFloat", 0.2,    0.001, 100.0,  "DISTANCE"),
    # Object sockets carry no default or range, hence the Nones.
    ("Collider",        "NodeSocketObject", None,  None,  None,   None),
    ("Collider Collection", "NodeSocketCollection", None, None, None, None),
    ("Collider Margin", "NodeSocketFloat", 0.0,    0.0,   1e6,    "DISTANCE"),
    ("Collider Friction","NodeSocketFloat", 0.3,   0.0,   1.0,    "FACTOR"),
    ("Collider Stickiness","NodeSocketFloat", 0.0,  0.0,   1.0,    "FACTOR"),
    ("Sticky Collider", "NodeSocketCollection", None, None, None,   None),
    ("Sticky Collider Grip","NodeSocketFloat", 0.8,  0.0,   1.0,    "FACTOR"),
    ("Start Height",    "NodeSocketFloat", 0.12,   0.0,   100.0,  "DISTANCE"),
    ("Gravity",         "NodeSocketFloat", 9.81,   0.0,   1e6,    None),
    ("Damping",         "NodeSocketFloat", 0.0,    0.0,   1.0,    "FACTOR"),
    ("Contact Damping", "NodeSocketFloat", DEFAULT_CONTACT_DAMPING, 0.0, 1.0, "FACTOR"),
    ("Stiffness",       "NodeSocketFloat", 0.333,  0.0,   1.0,    "FACTOR"),
    ("Substeps",        "NodeSocketInt",   14,     1,     MAX_RUNTIME_SUBSTEPS, None),
    ("Adaptive Substeps", "NodeSocketBool", True,  None,  None,   None),
    ("Iterations",      "NodeSocketInt",   2,      1,     MAX_RUNTIME_ITERATIONS, None),
    ("Friction",        "NodeSocketFloat", 0.3,    0.0,   1.0,    "FACTOR"),
    ("Self Collision",  "NodeSocketFloat", 1.0,    0.0,   1.0,    "FACTOR"),
    ("Cohesion",        "NodeSocketFloat", 0.15,   0.0,   1.0,    "FACTOR"),
    # Display-only surface shape. None of these reach the simulation zone.
    ("Profile Squareness", "NodeSocketFloat", 0.0, 0.0,   1.0,    "FACTOR"),
    ("Waviness",        "NodeSocketFloat", 0.0,    0.0,   1.0,    "FACTOR"),
    ("Thickness Variation", "NodeSocketFloat", 0.03, 0.0, 0.3,    "FACTOR"),
    ("End Softness",    "NodeSocketFloat", 0.35,   0.0,   0.9,    "FACTOR"),
    ("Render Faces",    "NodeSocketInt",   24,     3,     64,     None),
    ("Edge Irregularity", "NodeSocketFloat", 0.0,  0.0,   1.0,    "FACTOR"),
]


# Shared ordering for the modifier and sidebar; collider details start folded.
CONTROL_GROUPS = (
    ("Noodles", False, ("Noodle Count", "Noodle Length", "Noodle Radius",
                        "Length Variation", "Profile Faces", "Profile Aspect", "Smooth Surface", "Noodle Material",
                        "Profile Squareness", "Waviness", "Thickness Variation", "End Softness",
                        "Render Faces", "Edge Irregularity")),
    ("Spawn", False, ("Fill Diameter", "Start Height", "Spawn Coil", "Seed")),
    ("Solver", False, ("Adaptive Substeps", "Substeps", "Iterations", "Gravity",
                       "Damping", "Contact Damping", "Stiffness", "Self Collision", "Friction", "Cohesion")),
    ("Colliders", True, ("Collider", "Collider Collection", "Collider Margin",
                         "Collider Friction", "Collider Stickiness")),
    ("Sticky colliders", True, ("Sticky Collider", "Sticky Collider Grip")),
)

DESCRIPTIONS = {
    "Noodle Count": "Number of strands. More strands increase simulation and surface cost",
    "Noodle Length": "Strand length in metres. Longer strands need more simulation points",
    "Noodle Radius": "Half thickness in metres. Smaller radii need more points and substeps",
    "Length Variation": "Random fraction above and below the strand length",
    "Profile Faces": "Tube sides: 4 for preview, 6 to 8 for a smoother surface. Does not change physics",
    "Profile Aspect": "Thickness divided by width: 1 round, smaller values flatten into ribbons. Collisions use the full width",
    "Smooth Surface": "Round the displayed curve between simulation points; adds surface detail without increasing solver points",
    "Noodle Material": "Surface material for this pile; noodle type presets supply their own colours",
    "Profile Squareness": "Cross-section from round (0) towards a rounded square (1), as knife-cut udon and soba. Display only",
    "Waviness": "Crinkle along the strand, as in wavy ramen. Display only; the simulated centreline is unchanged",
    "Thickness Variation": "Slow, uneven thickness along each strand, as in hand-cut noodles. Display only",
    "End Softness": "Rounds the cut ends instead of a sharp flat cap; fraction of the radius. Display only",
    "Render Faces": "Tube sides used for final renders; the viewport keeps Profile Faces. Display only",
    "Edge Irregularity": "Wandering cut edges and uneven thickness across the width, for ribbons. Display only",
    "Fill Diameter": "Outer diameter of the spawned coil of loops in metres; keep it inside the bowl",
    "Start Height": "Spawn origin above the local floor in metres; coils can extend below this",
    "Spawn Coil": "Spawn as a portion lowered in as a coil of broad loops (1 waviest); 0 drops upright straight rods",
    "Seed": "Repeatable random layout and strand lengths",
    "Gravity": "Downward acceleration in metres per second squared; Earth gravity is 9.81",
    "Damping": "Extra air drag per 1/24 second on top of the physical drag every noodle feels; 0 falls at true gravity",
    "Contact Damping": "Let a resting heap come to rest: a point in contact drifting slower than about 4 cm/s (udon) is held still; a strand sliding faster than its friction allows never is. 0 disables",
    "Stiffness": "Bending stiffness as Young's modulus on a log scale: 0 = 10 kPa (overcooked), 0.33 = 100 kPa (udon), 0.77 = 2 MPa (al dente), 1 = 10 MPa",
    "Adaptive Substeps": "Use current maximum speed plus gravity to spend fewer substeps on slow frames",
    "Substeps": "Maximum steps per frame (1 to 24). Too few clamp fall speed. Use Fit Step Budget after changing scale or FPS",
    "Iterations": "Contact passes per substep (1 to 12). Stretch is solved exactly in each pass and bending exactly after them; 2 is plenty",
    "Self Collision": "Noodle contact strength; 0 allows strands to pass through one another",
    "Friction": "Coulomb friction coefficient between noodles and on the floor; a strand holds on slopes below atan(Friction). Wet pasta is about 0.2-0.4",
    "Cohesion": "Starch stickiness: pull towards a strand within 1.8 diameters, as a fraction of gravity; 0 is dry",
    "Collider": "Closed mesh collider. Add thickness to open bowls with Solidify. The local floor remains active",
    "Collider Collection": "Additional closed meshes, joined into one distance field per frame",
    "Collider Margin": "Extra clearance from collider surfaces in metres",
    "Collider Friction": "Coulomb friction coefficient against colliders; wet glazed ceramic is about 0.2-0.3",
    "Collider Stickiness": "Attraction to nearby collider surfaces",
    "Sticky Collider": "Second collider collection with independent grip; adds a second distance field",
    "Sticky Collider Grip": "Attraction to the sticky collection surfaces",
}


def ensure_material():
    """One shared noodle material, reused while it is current.

    The shader itself lives in materials.py: translucent random-walk
    subsurface, a patchy starch-water coat, per-strand tone from np_id and
    micro relief anchored to the surface attributes the surface section
    writes. A material saved by an older add-on is rebuilt in place.
    """
    return materials.ensure(MAT_NAME)


def build_group():
    """Build (or rebuild) the Noodle Physics node group and return it."""
    ng = bpy.data.node_groups.new(NG_NAME, "GeometryNodeTree")
    ng["noodle_physics_version"] = 2
    ng["noodle_solver_revision"] = SOLVER_REVISION
    nodes, links = ng.nodes, ng.links

    ng.interface.new_socket("Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
    panels = {title: ng.interface.new_panel(title, default_closed=closed)
              for title, closed, names in CONTROL_GROUPS}
    parents = {name: panels[title] for title, closed, names in CONTROL_GROUPS
               for name in names}
    for name, stype, default, lo, hi, subtype in PARAMS:
        s = ng.interface.new_socket(name, in_out="INPUT", socket_type=stype,
                                    parent=parents[name])
        if default is not None:
            s.default_value = default
            if lo is not None:          # booleans carry no range
                s.min_value, s.max_value = lo, hi
        if subtype:
            s.subtype = subtype
        if name == "Noodle Material":
            s.default_value = ensure_material()
        if name in DESCRIPTIONS:
            s.description = DESCRIPTIONS[name]

    def nd(idname, col, row, **props):
        n = nodes.new(idname)
        n.location = (col * 230, -row * 190)
        for k, v in props.items():
            setattr(n, k, v)
        return n

    def plug(inp, value):
        if isinstance(value, bpy.types.NodeSocket):
            links.new(value, inp)
        else:
            inp.default_value = value

    def pick_in(node, name):
        return next(s for s in node.inputs if s.name == name and s.enabled)

    def pick_out(node, name):
        return next(s for s in node.outputs if s.name == name and s.enabled)

    def math(op, a, b=None, col=0, row=0):
        n = nd("ShaderNodeMath", col, row, operation=op)
        plug(n.inputs[0], a)
        if b is not None:
            plug(n.inputs[1], b)
        return n.outputs[0]

    def vmath(op, a, b=None, col=0, row=0):
        n = nd("ShaderNodeVectorMath", col, row, operation=op)
        plug(n.inputs[0], a)
        if b is not None:
            # SCALE takes a float on "Scale", every other op a second Vector.
            plug(pick_in(n, "Scale") if op == "SCALE" else n.inputs[1], b)
        scalar = op in ("LENGTH", "DISTANCE", "DOT_PRODUCT")
        return pick_out(n, "Value" if scalar else "Vector")

    def xyz(x, y, z, col=0, row=0):
        n = nd("ShaderNodeCombineXYZ", col, row)
        plug(n.inputs["X"], x)
        plug(n.inputs["Y"], y)
        plug(n.inputs["Z"], z)
        return n.outputs["Vector"]

    def sep(v, col=0, row=0):
        n = nd("ShaderNodeSeparateXYZ", col, row)
        plug(n.inputs["Vector"], v)
        return n.outputs["X"], n.outputs["Y"], n.outputs["Z"]

    def mix_vec(factor, a, b, col=0, row=0):
        n = nd("ShaderNodeMix", col, row, data_type="VECTOR", factor_mode="UNIFORM")
        plug(pick_in(n, "Factor"), factor)
        plug(pick_in(n, "A"), a)
        plug(pick_in(n, "B"), b)
        return pick_out(n, "Result")

    def store(geo, name, value, data_type, col, row):
        n = nd("GeometryNodeStoreNamedAttribute", col, row,
               data_type=data_type, domain="POINT")
        plug(n.inputs["Geometry"], geo)
        plug(n.inputs["Name"], name)
        plug(n.inputs["Value"], value)
        return n.outputs["Geometry"]

    def named(name, data_type, col, row):
        n = nd("GeometryNodeInputNamedAttribute", col, row, data_type=data_type)
        plug(n.inputs["Name"], name)
        return n.outputs["Attribute"]

    def at_index(value, index, data_type, col, row):
        n = nd("GeometryNodeFieldAtIndex", col, row,
               data_type=data_type, domain="POINT")
        plug(n.inputs["Value"], value)
        plug(n.inputs["Index"], index)
        return n.outputs["Value"]

    def set_position(geo, position, col, row):
        n = nd("GeometryNodeSetPosition", col, row)
        plug(n.inputs["Geometry"], geo)
        plug(n.inputs["Position"], position)
        return n.outputs["Geometry"]

    def bend_offset(scale, col, row):
        """scale * (p - midpoint of neighbours) on interior points, else 0."""
        sp = nd("GeometryNodeSplineParameter", col, row).outputs["Index"]
        count = nd("GeometryNodeSplineLength", col, row + 1).outputs["Point Count"]
        inner = math("MULTIPLY", math("GREATER_THAN", sp, 0.5, col + 1, row),
                     math("LESS_THAN", sp, math("SUBTRACT", count, 1.5, col + 1, row + 1), col + 2, row + 1),
                     col + 2, row)
        pos = nd("GeometryNodeInputPosition", col, row + 2).outputs["Position"]
        idx = nd("GeometryNodeInputIndex", col, row + 3).outputs["Index"]
        mid = vmath("SCALE",
                    vmath("ADD", at_index(pos, math("SUBTRACT", idx, 1, col + 1, row + 2), "FLOAT_VECTOR", col + 2, row + 2),
                          at_index(pos, math("ADD", idx, 1, col + 1, row + 3), "FLOAT_VECTOR", col + 2, row + 3),
                          col + 3, row + 2), 0.5, col + 4, row + 2)
        return vmath("SCALE", vmath("SUBTRACT", pos, mid, col + 5, row + 2),
                     math("MULTIPLY", inner, scale, col + 3, row), col + 6, row + 2)

    gin = nd("NodeGroupInput", 0, 4)
    P = {name: gin.outputs[name] for name, *_ in PARAMS}
    position = nd("GeometryNodeInputPosition", 0, 0).outputs["Position"]
    index = nd("GeometryNodeInputIndex", 0, 1).outputs["Index"]
    fill_r = math("MULTIPLY", P["Fill Diameter"], 0.5, 1, 6)
    diameter = math("MULTIPLY", P["Noodle Radius"], 2.0, 1, 7)
    # How far clear of a collider surface a noodle's centreline is held.
    # Margin buys back the gap a thin or low-poly collider loses to its own
    # faceting, without fattening the noodle itself.
    collider_gap = math("ADD", P["Noodle Radius"], P["Collider Margin"], 2, 7)
    # Point spacing must equal the diameter. Not "at most" - exactly.
    #
    # Too far apart and crossing noodles have no sample pair within a diameter,
    # so they slide through each other. Too close and it breaks the other way,
    # which is much less obvious: Index of Nearest returns a single neighbour,
    # so once spacing drops below a diameter a point's own chain neighbour is
    # always nearer than any foreign noodle touching it. The query returns the
    # chain neighbour every time, the chain filter rejects it, and the point
    # resolves no contact at all. Measured: at spacing 3.33 against a diameter
    # of 5, median overlap went from 2% to 16%.
    #
    # So the count is not a parameter. It is derived per noodle, from that
    # noodle's own length, which is what lets Length Variation work at all.

    # ---------------------------------------------------------------- setup
    # One root per noodle, uniform over the spawn disc; sqrt(u) is what makes
    # it uniform by area rather than clumped at the centre.
    wn = nd("ShaderNodeTexWhiteNoise", 3, 1, noise_dimensions="4D")
    plug(wn.inputs["Vector"], xyz(P["Seed"], 0.0, 0.0, 2, 1))
    plug(wn.inputs["W"], index)
    u, v, w = sep(wn.outputs["Color"], 4, 1)
    disc_r = math("MULTIPLY", math("SQRT", u, None, 5, 0), fill_r, 6, 0)
    theta = math("MULTIPLY", v, tau, 5, 2)
    root = xyz(math("MULTIPLY", math("COSINE", theta, None, 6, 2), disc_r, 7, 2),
               math("MULTIPLY", math("SINE", theta, None, 6, 3), disc_r, 7, 3),
               0.0, 8, 2)
    # Third channel of the same noise, so length variation costs no extra
    # random. Spread symmetrically about Noodle Length.
    noodle_len = math("MULTIPLY", P["Noodle Length"],
                      math("ADD", 1.0,
                           math("MULTIPLY", math("SUBTRACT", w, 0.5, 5, 4),
                                math("MULTIPLY", P["Length Variation"], 2.0, 5, 5),
                                6, 4), 7, 4), 8, 4)

    pts = nd("GeometryNodePoints", 9, 1)
    plug(pts.inputs["Count"], P["Noodle Count"])
    plug(pts.inputs["Position"], root)
    geo = store(pts.outputs["Points"], ATTR_ID, index, "INT", 10, 1)
    geo = store(geo, ATTR_LEN, noodle_len, "FLOAT", 10, 2)
    geo = store(geo, ATTR_ROOT, root, "FLOAT_VECTOR", 10, 5)

    # Unit line, stretched per instance, so each noodle gets its own length
    # while they all keep the same point count.
    line = nd("GeometryNodeCurvePrimitiveLine", 10, 3)
    plug(line.inputs["Start"], (0.0, 0.0, 0.0))
    plug(line.inputs["End"], (0.0, 0.0, 1.0))

    iop = nd("GeometryNodeInstanceOnPoints", 11, 1)
    plug(iop.inputs["Points"], geo)
    plug(iop.inputs["Instance"], line.outputs["Curve"])
    plug(iop.inputs["Scale"], xyz(1.0, 1.0, noodle_len, 10, 4))
    real = nd("GeometryNodeRealizeInstances", 12, 1)
    plug(real.inputs["Geometry"], iop.outputs["Instances"])
    # Count is a per-curve field, so each noodle is resampled to its own
    # length divided by the diameter - every strand lands on the spacing the
    # collision query needs, whatever length it drew.
    resample = nd("GeometryNodeResampleCurve", 13, 1)  # Mode defaults to Count
    plug(resample.inputs["Curve"], real.outputs["Geometry"])
    plug(resample.inputs["Count"],
         math("MAXIMUM",
              math("ADD",
                   math("DIVIDE", nd("GeometryNodeSplineLength", 11, 4).outputs["Length"],
                        diameter, 12, 4),
                   1.0, 12, 5),
              2.0, 12, 6))

    # Spawn shape: a portion lowered into the bowl. A rigid vertical rod is
    # not what a cooked noodle does, and nor is a long straight strand: it
    # drapes over the rim of any bowl smaller than itself. Cooked noodles
    # lowered into a bowl fold over into broad loops, so each strand starts as
    # one wide, gently waving loop around the bowl's axis, and the loops are
    # stacked like a loose coil: SPAWN_RINGS concentric rings per level, a
    # 1.3-diameter gap between rings, each loop rising one diameter per turn so
    # it clears itself, levels one rise apart. Nothing touches at the start.
    # That matters: a spawn with strands crossing through each other could
    # only be pulled apart along whichever side the contact query happened to
    # see, so some pairs ended up threaded through one another for good and
    # the heap trembled forever around them. Fill Diameter is the outer
    # extent of the loops.
    #
    # Spawn Coil scales the waviness of the loops; 0 gives back the old
    # straight upright rod, which still needs a nudge to break its symmetry
    # or it falls perfectly vertically and stacks like a pole.
    nid_setup = named(ATTR_ID, "INT", 13, 3)
    len_setup = named(ATTR_LEN, "FLOAT", 13, 5)
    rest_setup = math("DIVIDE", len_setup,
                      math("MAXIMUM",
                           math("SUBTRACT",
                                nd("GeometryNodeSplineLength", 13, 6).outputs["Point Count"],
                                1.0, 14, 6), 1.0, 15, 6), 14, 5)
    spline_i_setup = nd("GeometryNodeSplineParameter", 13, 4).outputs["Index"]
    arc = math("MULTIPLY", spline_i_setup, rest_setup, 14, 2)
    coil_noise = nd("ShaderNodeTexNoise", 15, 3, noise_dimensions="3D")
    plug(coil_noise.inputs["Vector"], xyz(
        math("DIVIDE", arc, SPAWN_WAVE, 14, 3),
        math("MULTIPLY", nid_setup, NOODLE_SPREAD, 14, 4),
        P["Seed"], 14, 6))
    plug(coil_noise.inputs["Scale"], 1.0)
    plug(coil_noise.inputs["Detail"], 0.0)
    cx, cy, cz = sep(vmath("SUBTRACT", coil_noise.outputs["Color"], (0.5, 0.5, 0.5), 16, 3), 17, 3)
    strand_noise = nd("ShaderNodeTexWhiteNoise", 15, 7, noise_dimensions="2D")
    plug(strand_noise.inputs["Vector"], xyz(nid_setup, P["Seed"], 0.0, 14, 7))
    s_head, s_lift, s_size = sep(strand_noise.outputs["Color"], 16, 7)
    gap_ring = math("MULTIPLY", diameter, 1.5, 16, 20)
    reach = math("MAXIMUM", math("SUBTRACT", fill_r, P["Noodle Radius"], 17, 20),
                 math("MULTIPLY", diameter, 1.5, 16, 21), 18, 20)
    # As many rings as fit in the outer 60% of the disc, at most SPAWN_RINGS.
    rings = math("MAXIMUM",
                 math("MINIMUM", math("ADD", math("FLOOR", math("DIVIDE", math("MULTIPLY", reach, 0.6, 17, 22),
                                                                  gap_ring, 18, 22), None, 19, 22),
                                       1.0, 20, 22), SPAWN_RINGS, 21, 22), 1.0, 22, 22)
    ring = math("FLOORED_MODULO", nid_setup, rings, 17, 23)
    level = math("FLOOR", math("DIVIDE", nid_setup, rings, 17, 24), None, 18, 24)
    loop_r = math("SUBTRACT", reach,
                  math("MULTIPLY", math("ADD", ring, 0.5, 18, 23), gap_ring, 19, 23), 20, 23)
    inner_r = math("SUBTRACT", reach,
                   math("MULTIPLY", math("SUBTRACT", rings, 0.5, 18, 25), gap_ring, 19, 25), 20, 25)
    rise_turn = math("MULTIPLY", diameter, 1.15, 18, 26)
    longest = math("MULTIPLY", P["Noodle Length"],
                   math("ADD", 1.0, P["Length Variation"], 18, 27), 19, 27)
    level_h = math("ADD",
                   math("DIVIDE", math("MULTIPLY", rise_turn, longest, 19, 28),
                        math("MULTIPLY", inner_r, tau, 19, 29), 20, 28),
                   math("ADD", rise_turn, math("MULTIPLY", P["Spawn Coil"],
                                               math("MULTIPLY", diameter, SPAWN_WAVE_HEIGHT * 2.0, 19, 31),
                                               20, 31), 21, 31), 21, 28)
    turn_dir = math("SUBTRACT", math("MULTIPLY", math("FLOORED_MODULO", level, 2.0, 19, 32), 2.0, 20, 32),
                    1.0, 21, 32)
    angle = math("ADD", math("MULTIPLY", s_head, tau, 19, 33),
                 math("MULTIPLY", turn_dir, math("DIVIDE", arc, loop_r, 20, 34), 21, 34), 22, 33)
    wave = math("MULTIPLY", P["Spawn Coil"], diameter, 19, 35)
    radius_now = math("ADD", loop_r, math("MULTIPLY", math("MULTIPLY", cy, SPAWN_WAVE_RADIAL * 2.0, 20, 36),
                                          wave, 21, 36), 22, 36)
    height_now = math("ADD",
                      math("ADD", math("MULTIPLY", level, level_h, 20, 37),
                           math("DIVIDE", math("MULTIPLY", rise_turn, arc, 20, 38),
                                math("MULTIPLY", loop_r, tau, 20, 39), 21, 38), 22, 37),
                      math("MULTIPLY", math("MULTIPLY", cz, SPAWN_WAVE_HEIGHT * 2.0, 21, 40), wave, 22, 40),
                      23, 37)
    poured = vmath("SUBTRACT",
                   xyz(math("MULTIPLY", math("COSINE", angle, None, 23, 33), radius_now, 24, 33),
                       math("MULTIPLY", math("SINE", angle, None, 23, 34), radius_now, 24, 34),
                       height_now, 25, 33),
                   named(ATTR_ROOT, "FLOAT_VECTOR", 24, 35), 26, 33)

    # The rod is straight but leans by up to 1 in 50, so a symmetric drop
    # still topples. A straight lean, not a wiggle: with the bending solved
    # stiffly, a wiggled rod was kicked straight on its first step.
    rod = xyz(math("MULTIPLY", arc, math("MULTIPLY", math("SUBTRACT", s_head, 0.5, 16, 10), 0.04, 16, 11),
                   17, 10),
              math("MULTIPLY", arc, math("MULTIPLY", math("SUBTRACT", s_lift, 0.5, 16, 12), 0.04, 16, 13),
                   17, 11),
              arc, 18, 10)
    spawn = mix_vec(math("MINIMUM", math("MULTIPLY", P["Spawn Coil"], 1e3, 20, 11), 1.0, 21, 11),
                    rod, poured, 22, 10)

    # Position is rebuilt from scratch rather than offset, so the instanced
    # line's own shape does not survive into the spawn.
    start = nd("GeometryNodeSetPosition", 24, 1)
    # Named so measurement fixtures can replace the spawn shape (the drape
    # test lays a single straight strand across a rod).
    start.name = start.label = "Spawn Position"
    plug(start.inputs["Geometry"], resample.outputs["Curve"])
    plug(start.inputs["Position"],
         vmath("ADD",
               vmath("ADD", named(ATTR_ROOT, "FLOAT_VECTOR", 22, 6), spawn, 23, 5),
               xyz(0.0, 0.0, P["Start Height"], 22, 7), 25, 4))
    geo = store(start.outputs["Geometry"], ATTR_VEL, (0.0, 0.0, 0.0), "FLOAT_VECTOR", 26, 1)
    # The poured loops start partly set (see "plasticity" below), as a
    # noodle out of the pot already holds the curl it cooked in.
    geo = store(geo, ATTR_BREST, bend_offset(PLASTIC_SET, 26, 3), "FLOAT_VECTOR", 27, 1)

    # ----------------------------------------------------------- simulation
    sim_out = nd("GeometryNodeSimulationOutput", 40, 1)
    sim_in = nd("GeometryNodeSimulationInput", 20, 1)
    sim_in.pair_with_output(sim_out)
    plug(sim_in.inputs["Geometry"], geo)
    frame_dt = sim_in.outputs["Delta Time"]
    state = sim_in.outputs["Geometry"]
    # Seconds since the spawn, for the settling-in period below.
    state = store(state, ATTR_AGE, math("ADD", named(ATTR_AGE, "FLOAT", 20, 30), frame_dt, 21, 30),
                  "FLOAT", 22, 30)

    # ------------------------------------------------------------- substeps
    # Collision is sampled at points, so a point that travels more than a
    # noodle is thick in one step passes clean through whatever it should
    # have hit. The step size therefore has to stay small - but the frame
    # length is fixed by the frame rate, so the frame is subdivided instead.
    #
    # The alternative, letting a point cross several segments per step, was
    # measured: it put 65-82% of points inside another noodle. Holding the
    # step to one segment instead fixes that, but applied to the whole frame
    # it also caps terminal velocity at one segment per frame, a slow-motion
    # fall. Substeps buy back the speed: the cap scales
    # with the substep count, so 8 substeps means 8x the fall speed at the
    # same collision accuracy, for 8x the solve.
    # Rounds of cyclic reduction the stretch solve needs: log2 of the longest
    # strand's point count. Constant for a run, so measured once a frame.
    longest = nd("GeometryNodeAttributeStatistic", 17, 15, data_type="FLOAT", domain="POINT")
    plug(longest.inputs["Geometry"], state)
    plug(longest.inputs["Attribute"], nd("GeometryNodeSplineLength", 16, 15).outputs["Point Count"])
    pcr_rounds = math("CEIL", math("LOGARITHM", math("MAXIMUM", longest.outputs["Max"], 2.0, 18, 15),
                                   2.0, 19, 15), None, 20, 15)
    sub_in = nd("GeometryNodeRepeatInput", 20, 2)
    sub_out = nd("GeometryNodeRepeatOutput", 78, 1)
    sub_in.pair_with_output(sub_out)
    # Substeps is the hard user budget; adaptation spends only what is needed.
    runtime_substeps = math("MINIMUM", P["Substeps"], MAX_RUNTIME_SUBSTEPS, 20, 3)
    runtime_substeps = math("MAXIMUM", runtime_substeps, 1.0, 19, 8)
    # Predict this frame's fastest displacement from the *stored velocity*,
    # including one full frame of gravity. A from-rest estimate alone makes
    # accelerating noodles hit the clamp and silently run in slow motion.
    speeds = nd("GeometryNodeAttributeStatistic", 17, 10,
                data_type="FLOAT", domain="POINT")
    plug(speeds.inputs["Geometry"], state)
    plug(speeds.inputs["Attribute"],
         vmath("LENGTH", named(ATTR_VEL, "FLOAT_VECTOR", 15, 10), None, 16, 10))
    predicted = math("ADD", speeds.outputs["Max"],
                     math("MULTIPLY", P["Gravity"], frame_dt, 17, 11), 18, 10)
    needed = math("CEIL", math("DIVIDE",
                  math("MULTIPLY", predicted, frame_dt, 18, 11),
                  math("MULTIPLY", diameter, VELOCITY_SAFETY, 18, 12), 19, 10),
                  None, 19, 11)
    # A resting point still accelerates under gravity before contact projects
    # it out. Limit that per-step sag to 5% of a radius; the speed criterion
    # alone drops to two steps and repeatedly compresses/rebounds a whole pile.
    sag_steps = math("CEIL", math("SQRT", math("DIVIDE",
                    math("MULTIPLY", P["Gravity"],
                         math("MULTIPLY", frame_dt, frame_dt, 16, 13), 17, 13),
                    math("MULTIPLY", P["Noodle Radius"], SAG_FRACTION, 17, 14), 18, 13),
                    None, 18, 14), None, 19, 14)
    adaptive = math("MINIMUM", runtime_substeps,
                    math("MAXIMUM", needed,
                         math("MAXIMUM", sag_steps, 2.0, 19, 13), 19, 12), 20, 10)
    step_switch = nd("GeometryNodeSwitch", 20, 11, input_type="INT")
    plug(step_switch.inputs["Switch"], P["Adaptive Substeps"])
    plug(step_switch.inputs["False"], runtime_substeps)
    plug(step_switch.inputs["True"], adaptive)
    runtime_substeps = step_switch.outputs["Output"]
    plug(sub_in.inputs["Iterations"], runtime_substeps)
    substeps = math("MAXIMUM", runtime_substeps, 1.0, 20, 8)
    dt = math("DIVIDE", frame_dt, substeps, 21, 8)
    # Per noodle, not global: Length Variation means every strand has its own
    # rest length, and the speed clamp and contact rules all key off it. Taken
    # from the actual resampled point count, so it stays exact after the
    # count is rounded to an integer.
    rest = math("DIVIDE", named(ATTR_LEN, "FLOAT", 21, 6),
                math("MAXIMUM",
                     math("SUBTRACT",
                          nd("GeometryNodeSplineLength", 21, 7).outputs["Point Count"],
                          1.0, 22, 7), 1.0, 23, 7), 22, 6)

    spline_i = nd("GeometryNodeSplineParameter", 35, 4).outputs["Index"]
    point_count = nd("GeometryNodeSplineLength", 35, 5).outputs["Point Count"]
    has_prev = math("GREATER_THAN", spline_i, 0.5, 36, 4)
    has_prev2_row = math("GREATER_THAN", spline_i, 1.5, 36, 3)
    has_next = math("LESS_THAN", spline_i, math("SUBTRACT", point_count, 1.5, 36, 5), 37, 5)
    prev_p = at_index(position, math("SUBTRACT", index, 1, 36, 6), "FLOAT_VECTOR", 37, 6)
    next_p = at_index(position, math("ADD", index, 1, 36, 9), "FLOAT_VECTOR", 37, 9)
    interior = math("MULTIPLY", has_prev, has_next, 38, 3)

    # ------------------------------------------- bending solve, set-up per frame
    # Bending is solved directly, not iterated - see "bending stiffness" in
    # the constraint loop for why. The matrix it needs depends only on each
    # strand's point count, rest length and compliance, so its factorisation
    # is done once a frame, here, and the loop only replays it.
    #
    # Each interior point carries one vector constraint, C = p - midpoint of
    # its neighbours. XPBD needs (J J^T + a I) dlambda = r, with a = alpha /
    # dt^2; J J^T is the truncated pentadiagonal Toeplitz matrix
    # (1, -4, 6, -4, 1) / 4, a fourth difference. Away from the two end rows
    # that is exactly L^2 / 4 for the tridiagonal second difference L, and
    # a I + L^2 / 4 = a (I + i g L)(I - i g L) with g = 1 / (2 sqrt a), so its
    # inverse applied to a real vector is Re[(I + i g L)^-1 r] / a: one complex
    # tridiagonal solve. Parallel cyclic reduction does that solve with every
    # point working at once in ceil(log2(points)) rounds. The two end rows,
    # where J J^T and L^2 / 4 differ by 1/4 on the diagonal, are corrected
    # exactly with the Sherman-Morrison-Woodbury identity, using two more
    # solves (for unit vectors at the first and last row) that also depend
    # only on the strand, so they are done here too.
    def cmul(ar, ai, br, bi, col, row):
        """(ar + i ai)(br + i bi) for scalar fields."""
        return (math("SUBTRACT", math("MULTIPLY", ar, br, col, row),
                     math("MULTIPLY", ai, bi, col, row + 1), col + 1, row),
                math("ADD", math("MULTIPLY", ar, bi, col, row + 2),
                     math("MULTIPLY", ai, br, col, row + 3), col + 1, row + 2))

    def cdiv(ar, ai, br, bi, col, row):
        """(ar + i ai) / (br + i bi) for scalar fields."""
        den = math("ADD", math("MULTIPLY", br, br, col, row + 4),
                   math("MULTIPLY", bi, bi, col, row + 5), col + 1, row + 4)
        re, im = cmul(ar, ai, br, math("MULTIPLY", bi, -1.0, col, row + 6), col + 1, row)
        return (math("DIVIDE", re, den, col + 2, row), math("DIVIDE", im, den, col + 2, row + 1))

    def colour_store(geo, name, r, g, b, a, col, row):
        n = nd("FunctionNodeCombineColor", col, row, mode="RGB")
        for socket, value in zip(("Red", "Green", "Blue", "Alpha"), (r, g, b, a)):
            plug(n.inputs[socket], value)
        return store(geo, name, n.outputs["Color"], "FLOAT_COLOR", col + 1, row)

    def colour_read(source, col, row):
        n = nd("FunctionNodeSeparateColor", col, row, mode="RGB")
        plug(n.inputs["Color"], source)
        return n.outputs["Red"], n.outputs["Green"], n.outputs["Blue"], n.outputs["Alpha"]

    def gated_colour(ok, source, col, row):
        """A neighbour's four values, or zeros where it is off the strand."""
        return tuple(math("MULTIPLY", v, ok, col + 1, row + k)
                     for k, v in enumerate(colour_read(source, col, row)))

    def level_name(level, col, row):
        text = nd("FunctionNodeValueToString", col, row)
        plug(text.inputs["Value"], level)
        join = nd("GeometryNodeStringJoin", col + 1, row)
        prefix = nd("FunctionNodeInputString", col, row + 1)
        prefix.string = ATTR_BEND_LEVEL
        # Multi-input sockets list the newest link first.
        links.new(text.outputs["String"], join.inputs["Strings"])
        links.new(prefix.outputs["String"], join.inputs["Strings"])
        return join.outputs["String"]

    # Compliance alpha = rho l^4 / (E r^2); see the constraint loop.
    modulus = math("MULTIPLY", YOUNG_MIN,
                   math("POWER", YOUNG_MAX / YOUNG_MIN,
                        math("MINIMUM", math("MAXIMUM", P["Stiffness"], 0.0, 2, 20),
                             1.0, 3, 20), 4, 20), 5, 20)
    rest2 = math("MULTIPLY", rest, rest, 2, 21)
    alpha = math("DIVIDE",
                 math("MULTIPLY", NOODLE_DENSITY, math("MULTIPLY", rest2, rest2, 3, 21), 4, 21),
                 math("MULTIPLY", modulus,
                      math("MULTIPLY", P["Noodle Radius"], P["Noodle Radius"], 3, 22),
                      4, 22), 5, 21)
    # A floor on a keeps g finite; 1e-6 is already far stiffer than any
    # cooked noodle at any substep count this solver allows.
    alpha_dt = math("MAXIMUM",
                    math("DIVIDE", alpha, math("MAXIMUM", math("MULTIPLY", dt, dt, 5, 22),
                                               1e-12, 6, 22), 6, 21), 1e-6, 7, 21)
    # Viscoelastic damping of bending (Kelvin-Voigt: stress follows strain
    # plus BEND_RELAXATION times its rate), XPBD's constraint damping with
    # gamma = tau / dt. It only resists changes of shape, so a falling or
    # sliding noodle is untouched, while a bent one eases back instead of
    # ringing like a wire. It folds into the same matrix with a scaled
    # compliance: ((1 + gamma) J J^T + a) = (1 + gamma)(J J^T + a / (1 + gamma)).
    bend_gamma = math("DIVIDE", BEND_RELAXATION, math("MAXIMUM", dt, 1e-9, 6, 25), 7, 25)
    bend_scale = math("DIVIDE", 1.0, math("ADD", 1.0, bend_gamma, 8, 25), 9, 25)
    alpha_hat = math("MAXIMUM", math("MULTIPLY", alpha_dt, bend_scale, 9, 26), 1e-6, 10, 26)
    g_c = math("DIVIDE", 0.5, math("SQRT", alpha_hat, None, 7, 22), 8, 21)
    first_row = math("COMPARE", spline_i, 1.0, 6, 23)
    last_row = math("COMPARE", spline_i, math("SUBTRACT", point_count, 2.0, 5, 24), 6, 24)
    lo_row = math("MULTIPLY", interior, math("GREATER_THAN", spline_i, 1.5, 7, 23), 8, 23)
    hi_row = math("MULTIPLY", interior,
                  math("LESS_THAN", spline_i, math("SUBTRACT", point_count, 2.5, 7, 24), 8, 24), 9, 24)
    off_im = math("MULTIPLY", g_c, -1.0, 9, 21)
    # The factorisation only changes when the step length or a strand's
    # compliance does - with adaptive substeps, now and then - so it is kept in
    # the simulation state and redone only when its key no longer matches.
    # The switch is lazy: on a matching frame none of it runs.
    cached = state
    key_field = named(ATTR_BEND_KEY, "FLOAT", 9, 30)
    stale_stat = nd("GeometryNodeAttributeStatistic", 11, 30, data_type="FLOAT", domain="POINT")
    plug(stale_stat.inputs["Geometry"], state)
    plug(stale_stat.inputs["Attribute"],
         math("GREATER_THAN",
              math("ABSOLUTE", math("SUBTRACT", alpha_hat, key_field, 10, 31), None, 10, 32),
              math("MULTIPLY", alpha_hat, 1e-5, 10, 33), 11, 31))
    stale = math("GREATER_THAN", stale_stat.outputs["Max"], 0.5, 12, 30)
    # Rows: a and c (coupling down and up the strand) in one colour, b in a
    # vector, the two unit right-hand sides in another colour.
    state = colour_store(state, ATTR_BEND_AC, 0.0, math("MULTIPLY", off_im, lo_row, 10, 22),
                         0.0, math("MULTIPLY", off_im, hi_row, 10, 23), 10, 21)
    state = store(state, ATTR_BEND_B,
                  xyz(1.0, math("MULTIPLY", math("MULTIPLY", g_c, 2.0, 11, 22), interior, 12, 22),
                      0.0, 12, 21), "FLOAT_VECTOR", 13, 21)
    state = colour_store(state, ATTR_BEND_E, math("MULTIPLY", first_row, interior, 13, 22), 0.0,
                         math("MULTIPLY", last_row, interior, 13, 23), 0.0, 14, 21)

    fac_in = nd("GeometryNodeRepeatInput", 15, 20)
    fac_out = nd("GeometryNodeRepeatOutput", 30, 20)
    fac_in.pair_with_output(fac_out)
    plug(fac_in.inputs["Iterations"], pcr_rounds)
    plug(fac_in.inputs["Geometry"], state)
    stride = math("POWER", 2.0, fac_in.outputs["Iteration"], 15, 21)
    lo_ok = math("GREATER_THAN", math("SUBTRACT", spline_i, stride, 16, 21), -0.5, 17, 21)
    hi_ok = math("LESS_THAN", math("ADD", spline_i, stride, 16, 22),
                 math("SUBTRACT", point_count, 0.5, 16, 23), 17, 22)
    lo_i = math("SUBTRACT", index, stride, 16, 24)
    hi_i = math("ADD", index, stride, 16, 25)
    ac = named(ATTR_BEND_AC, "FLOAT_COLOR", 15, 26)
    bb = named(ATTR_BEND_B, "FLOAT_VECTOR", 15, 27)
    ee = named(ATTR_BEND_E, "FLOAT_COLOR", 15, 28)
    ar, ai, cr, ci = colour_read(ac, 16, 26)
    br, bi, _ = sep(bb, 16, 27)
    e0r, e0i, e1r, e1i = colour_read(ee, 16, 28)
    alr, ali, clr, cli = gated_colour(lo_ok, at_index(ac, lo_i, "FLOAT_COLOR", 17, 26), 18, 26)
    ahr, ahi, chr_, chi = gated_colour(hi_ok, at_index(ac, hi_i, "FLOAT_COLOR", 17, 27), 18, 27)
    blr, bli, _ = sep(mix_vec(lo_ok, (1.0, 0.0, 0.0), at_index(bb, lo_i, "FLOAT_VECTOR", 17, 28), 18, 28), 19, 28)
    bhr, bhi, _ = sep(mix_vec(hi_ok, (1.0, 0.0, 0.0), at_index(bb, hi_i, "FLOAT_VECTOR", 17, 29), 18, 29), 19, 29)
    k1r, k1i = cdiv(ar, ai, blr, bli, 20, 30)
    k2r, k2i = cdiv(cr, ci, bhr, bhi, 20, 38)
    # The multipliers for this round, kept for the per-iteration replay.
    level_attr = level_name(fac_in.outputs["Iteration"], 20, 46)
    kc = nd("FunctionNodeCombineColor", 23, 30, mode="RGB")
    for socket, value in zip(("Red", "Green", "Blue", "Alpha"), (k1r, k1i, k2r, k2i)):
        plug(kc.inputs[socket], value)
    fac = store(fac_in.outputs["Geometry"], level_attr, kc.outputs["Color"], "FLOAT_COLOR", 24, 20)
    # Same elimination applied to the two unit right-hand sides.
    lo_e = gated_colour(lo_ok, at_index(ee, lo_i, "FLOAT_COLOR", 17, 30), 18, 30)
    hi_e = gated_colour(hi_ok, at_index(ee, hi_i, "FLOAT_COLOR", 17, 31), 18, 31)
    new_e = []
    for j, (dr, di) in enumerate(((e0r, e0i), (e1r, e1i))):
        lr, li = cmul(k1r, k1i, lo_e[2 * j], lo_e[2 * j + 1], 24, 32 + 8 * j)
        hr, hi_ = cmul(k2r, k2i, hi_e[2 * j], hi_e[2 * j + 1], 24, 36 + 8 * j)
        new_e += [math("SUBTRACT", dr, math("ADD", lr, hr, 26, 32 + 8 * j), 27, 32 + 8 * j),
                  math("SUBTRACT", di, math("ADD", li, hi_, 26, 34 + 8 * j), 27, 34 + 8 * j)]
    # New rows. Every value here was read before any of these stores.
    t1r, t1i = cmul(clr, cli, k1r, k1i, 24, 50)
    t2r, t2i = cmul(ahr, ahi, k2r, k2i, 24, 54)
    new_b = xyz(math("SUBTRACT", br, math("ADD", t1r, t2r, 26, 50), 27, 50),
                math("SUBTRACT", bi, math("ADD", t1i, t2i, 26, 51), 27, 51), 0.0, 28, 50)
    nar, nai = cmul(alr, ali, k1r, k1i, 24, 58)
    ncr, nci = cmul(chr_, chi, k2r, k2i, 24, 62)
    neg = lambda v, r: math("MULTIPLY", v, -1.0, 26, r)
    # Colours must be built from the old values, so all three are combined
    # before the first of the three stores below runs.
    e_col = nd("FunctionNodeCombineColor", 28, 32, mode="RGB")
    for socket, value in zip(("Red", "Green", "Blue", "Alpha"), new_e):
        plug(e_col.inputs[socket], value)
    ac_col = nd("FunctionNodeCombineColor", 28, 58, mode="RGB")
    for socket, value in zip(("Red", "Green", "Blue", "Alpha"),
                             (neg(nar, 58), neg(nai, 59), neg(ncr, 62), neg(nci, 63))):
        plug(ac_col.inputs[socket], value)
    # Stores are sequential, so each one is written to a staging name first
    # and the staged values moved over afterwards.
    fac = store(fac, ATTR_BEND_TMP_E, e_col.outputs["Color"], "FLOAT_COLOR", 25, 20)
    fac = store(fac, ATTR_BEND_TMP_AC, ac_col.outputs["Color"], "FLOAT_COLOR", 26, 20)
    fac = store(fac, ATTR_BEND_TMP_B, new_b, "FLOAT_VECTOR", 27, 20)
    fac = store(fac, ATTR_BEND_E, named(ATTR_BEND_TMP_E, "FLOAT_COLOR", 27, 22), "FLOAT_COLOR", 28, 20)
    fac = store(fac, ATTR_BEND_AC, named(ATTR_BEND_TMP_AC, "FLOAT_COLOR", 28, 22), "FLOAT_COLOR", 29, 20)
    fac = store(fac, ATTR_BEND_B, named(ATTR_BEND_TMP_B, "FLOAT_VECTOR", 29, 22), "FLOAT_VECTOR", 29, 21)
    plug(fac_out.inputs["Geometry"], fac)
    state = fac_out.outputs["Geometry"]

    # Finish the two unit solves: y = Re[e / b] / a.
    fbr, fbi, _ = sep(named(ATTR_BEND_B, "FLOAT_VECTOR", 31, 20), 32, 20)
    f0r, f0i, f1r, f1i = colour_read(named(ATTR_BEND_E, "FLOAT_COLOR", 31, 21), 32, 21)
    y0, _ = cdiv(f0r, f0i, fbr, fbi, 33, 20)
    y1, _ = cdiv(f1r, f1i, fbr, fbi, 33, 28)
    y0 = math("DIVIDE", y0, alpha_hat, 36, 20)
    y1 = math("DIVIDE", y1, alpha_hat, 36, 21)
    state = store(state, ATTR_BEND_Y, xyz(y0, y1, 0.0, 37, 20), "FLOAT_VECTOR", 38, 20)
    # The 2x2 capacitance matrix 4 I + U^T Y of each strand, inverted.
    yy = named(ATTR_BEND_Y, "FLOAT_VECTOR", 38, 22)
    first_i = math("ADD", math("SUBTRACT", index, spline_i, 38, 23), 1.0, 39, 23)
    last_i = math("ADD", math("SUBTRACT", index, spline_i, 38, 24),
                  math("SUBTRACT", point_count, 2.0, 38, 25), 39, 24)
    y0f, y1f, _ = sep(at_index(yy, first_i, "FLOAT_VECTOR", 40, 23), 41, 23)
    y0l, y1l, _ = sep(at_index(yy, last_i, "FLOAT_VECTOR", 40, 24), 41, 24)
    s00 = math("ADD", 4.0, y0f, 42, 23)
    s11 = math("ADD", 4.0, y1l, 42, 24)
    det = math("SUBTRACT", math("MULTIPLY", s00, s11, 43, 23),
               math("MULTIPLY", y1f, y0l, 43, 24), 44, 23)
    inv = math("DIVIDE", 1.0, det, 45, 23)
    state = colour_store(state, ATTR_BEND_S,
                         math("MULTIPLY", s11, inv, 46, 22),
                         math("MULTIPLY", math("MULTIPLY", y1f, -1.0, 45, 24), inv, 46, 23),
                         math("MULTIPLY", math("MULTIPLY", y0l, -1.0, 45, 25), inv, 46, 24),
                         math("MULTIPLY", s00, inv, 46, 25), 47, 22)
    state = store(state, ATTR_BEND_KEY, alpha_hat, "FLOAT", 48, 21)
    refactor = nd("GeometryNodeSwitch", 49, 21, input_type="GEOMETRY")
    plug(refactor.inputs["Switch"], stale)
    plug(refactor.inputs["False"], cached)
    plug(refactor.inputs["True"], state)
    state = refactor.outputs["Output"]

    plug(sub_in.inputs["Geometry"], state)
    state = sub_in.outputs["Geometry"]

    # Keep this substep's starting positions; velocity is read back out of
    # them after the constraints have had their say.
    state = store(state, ATTR_PREV, position, "FLOAT_VECTOR", 21, 1)

    velocity = named(ATTR_VEL, "FLOAT_VECTOR", 21, 3)
    velocity = vmath("ADD", velocity,
                     xyz(0.0, 0.0, math("MULTIPLY", math("MULTIPLY", P["Gravity"], -1.0, 22, 4), dt, 23, 4), 23, 5),
                     24, 3)
    # Air drag on a cylinder moving sideways through air, quadratic in speed:
    # a = 0.5 rho_air Cd (2r) |v_n|^2 / (rho pi r^2). It acts only across the
    # strand - sliding along its own axis is almost free - and it is a few
    # percent of g at the speeds a dropped noodle reaches, so the fall is
    # essentially free fall. Solved implicitly, v_n / (1 + k |v_n| dt), so no
    # step size can reverse it.
    tangent = vmath("NORMALIZE", vmath("SUBTRACT", next_p, prev_p, 22, 9), None, 23, 9)
    v_axial = vmath("SCALE", tangent, vmath("DOT_PRODUCT", velocity, tangent, 24, 9), 25, 9)
    v_cross = vmath("SUBTRACT", velocity, v_axial, 26, 9)
    drag_k = math("DIVIDE", AIR_DRAG,
                  math("MAXIMUM", P["Noodle Radius"], 1e-6, 24, 10), 25, 10)
    v_cross = vmath("SCALE", v_cross,
                    math("DIVIDE", 1.0,
                         math("ADD", 1.0,
                              math("MULTIPLY", math("MULTIPLY", drag_k, dt, 26, 10),
                                   vmath("LENGTH", v_cross, None, 26, 11), 27, 10),
                              28, 10), 29, 10), 30, 9)
    velocity = vmath("ADD", v_axial, v_cross, 31, 9)
    # Optional extra drag, calibrated at 24 fps and integrated over seconds at
    # any scene FPS. 0 by default: the physical drag above is all a noodle sees.
    velocity = vmath("SCALE", velocity,
                     math("POWER", math("SUBTRACT", 1.0, P["Damping"], 23, 6),
                          math("MULTIPLY", dt, 24.0, 23, 7), 24, 6), 25, 3)
    # Catch a constraint spike before it throws noodles to infinity and ruins
    # the cache. One segment per substep is also exactly the bound collision
    # sampling needs, so the guard and the accuracy limit are the same number.
    speed = vmath("LENGTH", velocity, None, 26, 3)
    top_speed = math("DIVIDE",
                     math("MULTIPLY", rest, VELOCITY_SAFETY, 26, 4),
                     math("MAXIMUM", dt, 1e-9, 26, 5), 27, 4)
    velocity = vmath("SCALE", velocity,
                     math("MINIMUM", math("DIVIDE", top_speed,
                                          math("MAXIMUM", speed, 1e-6, 27, 5), 28, 5), 1.0, 29, 5),
                     30, 3)
    state = set_position(state, vmath("ADD", position, vmath("SCALE", velocity, dt, 31, 3), 32, 3), 33, 1)
    # Contact pushes are summed over the iterations of each substep: they are
    # the normal impulses Coulomb friction is proportional to, and because
    # they include the weight a contact passes on - the tension in a strand
    # hanging off a rod, the strands piled on top - friction grows with load.
    state = store(state, ATTR_SPUSH, 0.0, "FLOAT", 33, 2)
    state = store(state, ATTR_NPUSH, 0.0, "FLOAT", 33, 3)


    # ------------------------------------------------------------- collider
    # An object socket and a collection socket, joined into one geometry and
    # voxelised into a single distance field. The solver never sees how many
    # colliders there are: ten in a collection cost one grid and one lookup
    # per sample, the same as one object.
    #
    # Both are read in RELATIVE space, which means this object's local space
    # and the evaluated collider at the current frame - so a collider that
    # moves, rotates or deforms is re-voxelised every frame and pushes noodles
    # as it goes. That also sets the speed limit: a collider crossing more
    # than its own narrow band in one frame steps over the noodles instead of
    # hitting them.
    obj_info = nd("GeometryNodeObjectInfo", 32, 5, transform_space="RELATIVE")
    plug(obj_info.inputs["Object"], P["Collider"])

    coll_info = nd("GeometryNodeCollectionInfo", 32, 6, transform_space="RELATIVE")
    plug(coll_info.inputs["Collection"], P["Collider Collection"])
    plug(coll_info.inputs["Separate Children"], False)
    coll_real = nd("GeometryNodeRealizeInstances", 33, 6)
    plug(coll_real.inputs["Geometry"], coll_info.outputs["Instances"])

    joined = nd("GeometryNodeJoinGeometry", 33, 5)
    plug(joined.inputs["Geometry"], obj_info.outputs["Geometry"])
    plug(joined.inputs["Geometry"], coll_real.outputs["Geometry"])
    collider = joined.outputs["Geometry"]

    # The sticky set. Everything inside one field shares one stickiness, which
    # is the price of joining them, so a scene that needs two answers - a bowl
    # that lets go, chopsticks that do not - needs two fields. Collision is
    # identical either way; only the grip differs.
    sticky_info = nd("GeometryNodeCollectionInfo", 32, 8, transform_space="RELATIVE")
    plug(sticky_info.inputs["Collection"], P["Sticky Collider"])
    plug(sticky_info.inputs["Separate Children"], False)
    sticky_real = nd("GeometryNodeRealizeInstances", 33, 9)
    plug(sticky_real.inputs["Geometry"], sticky_info.outputs["Instances"])

    # Voxelise once per frame, not once per substep: these nodes depend only on
    # the collider objects and never on solver state, so they sit outside both
    # zones and every Sample Grid inside them reads the same grids.
    voxel = math("MULTIPLY", P["Noodle Radius"], SDF_VOXEL, 33, 7)

    def make_grid(geo, col, row):
        n = nd("GeometryNodeMeshToSDFGrid", col, row)
        plug(n.inputs["Mesh"], geo)
        plug(n.inputs["Voxel Size"], voxel)
        plug(n.inputs["Band Width"], SDF_BAND)
        return n.outputs["SDF Grid"]

    grid = make_grid(collider, 34, 8)
    sticky_grid = make_grid(sticky_real.outputs["Geometry"], 34, 10)

    def has_faces(geometry, row):
        size = nd("GeometryNodeAttributeDomainSize", 30, row, component="MESH")
        plug(size.inputs["Geometry"], geometry)
        return math("GREATER_THAN", size.outputs["Face Count"], 0.0, 31, row)

    has_plain = has_faces(collider, 12)
    has_sticky = has_faces(sticky_real.outputs["Geometry"], 13)

    def sdf_at(grid, p, col, row):
        n = nd("GeometryNodeSampleGrid", col, row, data_type="FLOAT")
        plug(n.inputs["Grid"], grid)
        plug(n.inputs["Position"], p)
        return n.outputs["Value"]

    def sdf_probe(grid, p, col, row):
        """Distance to the collider and the gradient of that distance.

        Six samples for the gradient, central differences one voxel either
        side. They are grid lookups rather than BVH descents, which is what
        makes asking seven times per constraint iteration affordable.
        """
        d = sdf_at(grid, p, col, row)
        axes = (xyz(voxel, 0.0, 0.0, col, row + 1),
                xyz(0.0, voxel, 0.0, col, row + 2),
                xyz(0.0, 0.0, voxel, col, row + 3))
        comps = [math("SUBTRACT",
                      sdf_at(grid, vmath("ADD", p, a, col + 1, row + 1 + i), col + 2, row + 1 + i),
                      sdf_at(grid, vmath("SUBTRACT", p, a, col + 1, row + 4 + i), col + 2, row + 4 + i),
                      col + 3, row + 1 + i)
                 for i, a in enumerate(axes)]
        return d, xyz(comps[0], comps[1], comps[2], col + 4, row + 1)


    # XPBD accumulates its multiplier across the iterations of one step, so it
    # has to start each substep at zero or the bend would keep stiffening.
    state = store(state, ATTR_BLAM, (0.0, 0.0, 0.0), "FLOAT_VECTOR", 38, 1)

    # ----------------------------------------------------- constraint solve
    rep_in = nd("GeometryNodeRepeatInput", 35, 1)
    rep_out = nd("GeometryNodeRepeatOutput", 39, 1)
    rep_in.pair_with_output(rep_out)
    # Constraint iterations are the other major runtime multiplier. A bounded
    # effective value keeps playback responsive while retaining the editable
    # socket for normal-quality tuning.
    runtime_iterations = math("MINIMUM", P["Iterations"], MAX_RUNTIME_ITERATIONS, 35, 0)
    plug(rep_in.inputs["Iterations"], runtime_iterations)
    plug(rep_in.inputs["Geometry"], state)
    solve = rep_in.outputs["Geometry"]
    # Nearest neighbour once per constraint iteration. Each point resolves
    # only one contact at a time, so in a dense pile a point touching three
    # strands needs the query refreshed to get to the others; once per substep
    # left the overlaps it skipped to be pushed out a substep later, and the
    # settled pile in the bowl jittered 30% more. For a few thousand points
    # the query costs about as much as one stretch solve.
    #
    # The query is split into two halves by the parity of (strand id + point
    # number). A point's own neighbours along the strand are in the other
    # half, so they can no longer win the query: before, they sat exactly one
    # diameter away and tied with a touching strand, which was then only seen
    # once it had sunk in. Points on the other strands in the same half are
    # two diameters apart, and the segment test below finds the true closest
    # point between them. A second query, split by point number alone, finds a
    # second touching strand for many points, which a dense heap needs.
    sp_it = nd("GeometryNodeSplineParameter", 36, 7).outputs["Index"]
    id_it = named(ATTR_ID, "INT", 36, 8)
    for k, (attr, group) in enumerate(((ATTR_NEAR, math("FLOORED_MODULO", math("ADD", sp_it, id_it, 37, 7),
                                                         2.0, 38, 7)),
                                       (ATTR_NEAR2, math("FLOORED_MODULO", sp_it, 2.0, 38, 8)))):
        near_it = nd("GeometryNodeIndexOfNearest", 39, 6 + k)
        plug(near_it.inputs["Position"], position)
        plug(near_it.inputs["Group ID"], group)
        solve = store(solve, attr, near_it.outputs["Index"], "INT", 36 + k, 0)

    # -- bending stiffness, from the material.
    # Distance constraints alone make a *chain*, not a noodle: consecutive
    # segments may meet at any angle, so the curve kinks hard. Real cooked
    # pasta resists bending with a Young's modulus E, and with its weight
    # that sets one length scale, the elasto-gravity length
    # L_g = (E I / (rho g A))^(1/3): roughly the radius a draped strand
    # curves over.
    #
    # The constraint at each interior point is the vector C = p - midpoint of
    # its neighbours. Curvature is about 2|C| / l^2, so a beam's bending
    # energy E I kappa^2 l / 2 per joint is k |C|^2 / 2 with k = 4 E I / l^3.
    # Inverse masses in this solver are 1, not 1/kg, so the compliance that
    # reproduces the beam is alpha = m / k = rho l^4 / (E r^2): physical,
    # independent of the substep count, and scaled correctly with radius and
    # segment length - a thin strand is much floppier than a thick one of
    # the same material, as it should be.
    #
    # It is solved exactly for the whole strand at once (the factorisation
    # is set up once a frame, before the substeps). An iterated solve could
    # not reach these stiffnesses: the previous Jacobi version needed its
    # steps scaled down to stay stable, and with only a couple of passes per
    # substep that left every noodle many times floppier than its Stiffness
    # said, with hairpin kinks wherever a strand hung over an edge - the
    # bent-drinking-straw look - and raising Stiffness barely changed it.
    # The constraint is linear in position, so one solve is exact.
    def bending(solve):
        lam = named(ATTR_BLAM, "FLOAT_VECTOR", 47, 18)
        own = vmath("SUBTRACT", position,
                    vmath("SCALE", vmath("ADD", prev_p, next_p, 47, 19), 0.5, 48, 19), 49, 19)
        then = named(ATTR_PREV, "FLOAT_VECTOR", 46, 20)
        own_then = vmath("SUBTRACT", then,
                         vmath("SCALE",
                               vmath("ADD", at_index(then, math("SUBTRACT", index, 1, 46, 21), "FLOAT_VECTOR", 47, 21),
                                     at_index(then, math("ADD", index, 1, 46, 22), "FLOAT_VECTOR", 47, 22),
                                     48, 21), 0.5, 49, 21), 50, 21)
        # -(C + a lambda + gamma (C - C at the start of the substep)) / (1 + gamma)
        # Measured from the set (plastic) bend, not from straight.
        own_set = vmath("SUBTRACT", own, named(ATTR_BREST, "FLOAT_VECTOR", 48, 24), 49, 24)
        rhs = vmath("SCALE",
                    vmath("ADD",
                          vmath("ADD", own_set, vmath("SCALE", lam, alpha_dt, 49, 20), 50, 19),
                          vmath("SCALE", vmath("SUBTRACT", own, own_then, 51, 21), bend_gamma, 52, 21),
                          51, 20),
                    math("MULTIPLY", math("MULTIPLY", interior, -1.0, 49, 23), bend_scale, 50, 23), 51, 19)
        solve = store(solve, ATTR_BEND_DRE, rhs, "FLOAT_VECTOR", 52, 1)
        solve = store(solve, ATTR_BEND_DIM, (0.0, 0.0, 0.0), "FLOAT_VECTOR", 53, 1)

        rep_b_in = nd("GeometryNodeRepeatInput", 54, 2)
        rep_b_out = nd("GeometryNodeRepeatOutput", 62, 2)
        rep_b_in.pair_with_output(rep_b_out)
        plug(rep_b_in.inputs["Iterations"], pcr_rounds)
        plug(rep_b_in.inputs["Geometry"], solve)
        b_stride = math("POWER", 2.0, rep_b_in.outputs["Iteration"], 54, 4)
        b_lo_ok = math("GREATER_THAN", math("SUBTRACT", spline_i, b_stride, 55, 4), -0.5, 56, 4)
        b_hi_ok = math("LESS_THAN", math("ADD", spline_i, b_stride, 55, 5),
                       math("SUBTRACT", point_count, 0.5, 55, 6), 56, 5)
        b_lo_i = math("SUBTRACT", index, b_stride, 55, 7)
        b_hi_i = math("ADD", index, b_stride, 55, 8)
        kk = named(level_name(rep_b_in.outputs["Iteration"], 54, 9), "FLOAT_COLOR", 56, 9)
        q1r, q1i, q2r, q2i = colour_read(kk, 57, 9)
        dre = named(ATTR_BEND_DRE, "FLOAT_VECTOR", 56, 11)
        dim = named(ATTR_BEND_DIM, "FLOAT_VECTOR", 56, 12)

        def neighbour(field, idx, ok, col, row):
            return vmath("SCALE", at_index(field, idx, "FLOAT_VECTOR", col, row), ok, col + 1, row)

        lre, lim = neighbour(dre, b_lo_i, b_lo_ok, 57, 11), neighbour(dim, b_lo_i, b_lo_ok, 57, 12)
        hre, him = neighbour(dre, b_hi_i, b_hi_ok, 57, 13), neighbour(dim, b_hi_i, b_hi_ok, 57, 14)

        def ctimes(kr, ki, vr, vi, col, row):
            """Complex scalar times complex vector."""
            return (vmath("SUBTRACT", vmath("SCALE", vr, kr, col, row), vmath("SCALE", vi, ki, col, row + 1),
                          col + 1, row),
                    vmath("ADD", vmath("SCALE", vi, kr, col, row + 2), vmath("SCALE", vr, ki, col, row + 3),
                          col + 1, row + 2))

        xr1, xi1 = ctimes(q1r, q1i, lre, lim, 59, 11)
        xr2, xi2 = ctimes(q2r, q2i, hre, him, 59, 15)
        new_re = vmath("SUBTRACT", dre, vmath("ADD", xr1, xr2, 61, 11), 62, 11)
        new_im = vmath("SUBTRACT", dim, vmath("ADD", xi1, xi2, 61, 13), 62, 13)
        # Both halves are computed from the old values: stage one, then move it.
        rb = store(rep_b_in.outputs["Geometry"], ATTR_BEND_TMP_B, new_im, "FLOAT_VECTOR", 58, 2)
        rb = store(rb, ATTR_BEND_DRE, new_re, "FLOAT_VECTOR", 59, 2)
        rb = store(rb, ATTR_BEND_DIM, named(ATTR_BEND_TMP_B, "FLOAT_VECTOR", 60, 3), "FLOAT_VECTOR", 60, 2)
        plug(rep_b_out.inputs["Geometry"], rb)
        solve = rep_b_out.outputs["Geometry"]

        # z = Re[d / b] / a, then the end-row correction.
        zbr, zbi, _ = sep(named(ATTR_BEND_B, "FLOAT_VECTOR", 63, 4), 64, 4)
        zden = math("MULTIPLY",
                    math("ADD", math("MULTIPLY", zbr, zbr, 65, 4), math("MULTIPLY", zbi, zbi, 65, 5), 66, 4),
                    alpha_hat, 67, 4)
        z = vmath("SCALE",
                  vmath("ADD", vmath("SCALE", named(ATTR_BEND_DRE, "FLOAT_VECTOR", 63, 6), zbr, 64, 6),
                        vmath("SCALE", named(ATTR_BEND_DIM, "FLOAT_VECTOR", 63, 7), zbi, 64, 7), 65, 6),
                  math("DIVIDE", 1.0, zden, 66, 6), 67, 6)
        solve = store(solve, ATTR_BEND_DRE, z, "FLOAT_VECTOR", 64, 1)
        zz = named(ATTR_BEND_DRE, "FLOAT_VECTOR", 65, 8)
        first_iz = math("ADD", math("SUBTRACT", index, spline_i, 65, 9), 1.0, 66, 9)
        last_iz = math("ADD", math("SUBTRACT", index, spline_i, 65, 10),
                       math("SUBTRACT", point_count, 2.0, 65, 11), 66, 10)
        t0 = at_index(zz, first_iz, "FLOAT_VECTOR", 67, 9)
        t1 = at_index(zz, last_iz, "FLOAT_VECTOR", 67, 10)
        s00, s01, s10, s11 = colour_read(named(ATTR_BEND_S, "FLOAT_COLOR", 66, 12), 67, 12)
        w0 = vmath("ADD", vmath("SCALE", t0, s00, 68, 9), vmath("SCALE", t1, s01, 68, 10), 69, 9)
        w1 = vmath("ADD", vmath("SCALE", t0, s10, 68, 11), vmath("SCALE", t1, s11, 68, 12), 69, 11)
        yb0, yb1, _ = sep(named(ATTR_BEND_Y, "FLOAT_VECTOR", 68, 13), 69, 13)
        dlam_v = vmath("SCALE",
                       vmath("SUBTRACT", zz,
                             vmath("ADD", vmath("SCALE", w0, yb0, 70, 9), vmath("SCALE", w1, yb1, 70, 10),
                                   71, 9), 72, 9),
                       interior, 73, 9)
        solve = store(solve, ATTR_BDLAM, dlam_v, "FLOAT_VECTOR", 65, 1)
        dl = named(ATTR_BDLAM, "FLOAT_VECTOR", 66, 14)
        # Dummy end rows carry zero, so neighbours need no strand check.
        bend = vmath("SUBTRACT", dl,
                     vmath("SCALE",
                           vmath("ADD",
                                 vmath("SCALE", at_index(dl, math("SUBTRACT", index, 1, 67, 15),
                                                         "FLOAT_VECTOR", 68, 15), has_prev, 69, 15),
                                 vmath("SCALE", at_index(dl, math("ADD", index, 1, 67, 16),
                                                         "FLOAT_VECTOR", 68, 16), has_next, 69, 16),
                                 70, 15), 0.5, 71, 15), 72, 15)
        solve = set_position(solve, vmath("ADD", position, bend, 73, 15), 66, 1)
        solve = store(solve, ATTR_BLAM, vmath("ADD", lam, dl, 67, 17), "FLOAT_VECTOR", 67, 1)
        return solve


    # -- inextensibility, solved exactly along each strand.
    # A noodle does not stretch measurably under its own weight. Solved as
    # local distance constraints (Jacobi, two iterations) the length error
    # could only diffuse one segment per pass, and a strand hanging over a
    # rod came out 13% longer than it is - a rubber band, and a pile whose
    # upper strands never really bore down on the lower ones.
    #
    # Instead all of a strand's distance constraints are solved together, as
    # one linear system: XPBD's (J W J^T) dlambda = -C, which for a chain is
    # tridiagonal - 2 on the diagonal, minus the cosine between neighbouring
    # segments off it. Parallel cyclic reduction solves a tridiagonal system
    # with every point working at once: each round, every row eliminates the
    # rows `stride` away and the stride doubles, so ceil(log2(points)) rounds
    # decouple it completely. One solve projects the whole strand to its rest
    # length the way a mass-weighted projection should, conserving momentum,
    # and the tension it carries is the weight of everything hanging below.
    def stretching(solve):
        u = vmath("NORMALIZE", vmath("SUBTRACT", position, prev_p, 70, 7), None, 71, 7)
        seg_len = vmath("LENGTH", vmath("SUBTRACT", position, prev_p, 70, 8), None, 71, 8)
        solve = store(solve, ATTR_SDIR, vmath("SCALE", u, has_prev, 72, 7), "FLOAT_VECTOR", 72, 1)
        u_s = named(ATTR_SDIR, "FLOAT_VECTOR", 73, 6)
        u_prev = at_index(u_s, math("SUBTRACT", index, 1, 73, 7), "FLOAT_VECTOR", 74, 7)
        u_next = at_index(u_s, math("ADD", index, 1, 73, 8), "FLOAT_VECTOR", 74, 8)
        row_a = math("MULTIPLY", math("MULTIPLY", vmath("DOT_PRODUCT", u_prev, u_s, 75, 7), -1.0, 76, 7),
                     has_prev2_row, 77, 7)
        row_c = math("MULTIPLY", math("MULTIPLY", vmath("DOT_PRODUCT", u_next, u_s, 75, 8), -1.0, 76, 8),
                     math("MULTIPLY", has_prev, has_next, 76, 9), 77, 8)
        row_b = math("ADD", 1.0, has_prev, 75, 9)  # 2 for a real row, 1 for the dummy at spline start
        row_d = math("MULTIPLY", math("SUBTRACT", rest, seg_len, 75, 10), has_prev, 76, 10)
        solve = store(solve, ATTR_SD, row_d, "FLOAT", 78, 1)
        solve = store(solve, ATTR_SABC, xyz(row_a, row_b, row_c, 78, 8), "FLOAT_VECTOR", 79, 1)

        pcr_in = nd("GeometryNodeRepeatInput", 80, 2)
        pcr_out = nd("GeometryNodeRepeatOutput", 86, 2)
        pcr_in.pair_with_output(pcr_out)
        plug(pcr_in.inputs["Iterations"], pcr_rounds)
        plug(pcr_in.inputs["Geometry"], solve)
        stride = math("POWER", 2.0, pcr_in.outputs["Iteration"], 80, 4)
        abc = named(ATTR_SABC, "FLOAT_VECTOR", 80, 5)
        dd = named(ATTR_SD, "FLOAT", 80, 6)
        lo_ok = math("GREATER_THAN", math("SUBTRACT", spline_i, stride, 81, 7), -0.5, 82, 7)
        hi_ok = math("LESS_THAN", math("ADD", spline_i, stride, 81, 8),
                     math("SUBTRACT", point_count, 0.5, 81, 9), 82, 8)
        lo_i = math("SUBTRACT", index, stride, 81, 10)
        hi_i = math("ADD", index, stride, 81, 11)
        # Rows outside the strand read as the identity row (0, 1, 0 | 0).
        identity = (0.0, 1.0, 0.0)
        abc_lo = mix_vec(lo_ok, identity, at_index(abc, lo_i, "FLOAT_VECTOR", 82, 10), 83, 10)
        abc_hi = mix_vec(hi_ok, identity, at_index(abc, hi_i, "FLOAT_VECTOR", 82, 11), 83, 11)
        d_lo = math("MULTIPLY", at_index(dd, lo_i, "FLOAT", 82, 12), lo_ok, 83, 12)
        d_hi = math("MULTIPLY", at_index(dd, hi_i, "FLOAT", 82, 13), hi_ok, 83, 13)
        a0, b0, c0 = sep(abc, 81, 5)
        al, bl, cl = sep(abc_lo, 84, 10)
        ah, bh, ch = sep(abc_hi, 84, 11)
        k1 = math("DIVIDE", a0, bl, 85, 10)
        k2 = math("DIVIDE", c0, bh, 85, 11)
        d_new = math("SUBTRACT", dd,
                     math("ADD", math("MULTIPLY", d_lo, k1, 86, 12),
                          math("MULTIPLY", d_hi, k2, 86, 13), 87, 12), 88, 12)
        abc_new = xyz(math("MULTIPLY", math("MULTIPLY", al, k1, 86, 7), -1.0, 87, 7),
                      math("SUBTRACT", b0,
                           math("ADD", math("MULTIPLY", cl, k1, 86, 8),
                                math("MULTIPLY", ah, k2, 86, 9), 87, 8), 88, 8),
                      math("MULTIPLY", math("MULTIPLY", ch, k2, 86, 10), -1.0, 87, 10), 89, 8)
        # d first: it reads the old coefficients, which the second store replaces.
        reduced = store(pcr_in.outputs["Geometry"], ATTR_SD, d_new, "FLOAT", 84, 2)
        reduced = store(reduced, ATTR_SABC, abc_new, "FLOAT_VECTOR", 85, 2)
        plug(pcr_out.inputs["Geometry"], reduced)
        solve = pcr_out.outputs["Geometry"]

        _, b_fin, _ = sep(named(ATTR_SABC, "FLOAT_VECTOR", 87, 4), 88, 4)
        lam_s = math("DIVIDE", named(ATTR_SD, "FLOAT", 87, 5), b_fin, 89, 4)
        solve = store(solve, ATTR_SD, lam_s, "FLOAT", 90, 1)
        lam_st = named(ATTR_SD, "FLOAT", 90, 5)
        stretch_fix = vmath("SUBTRACT",
                            vmath("SCALE", u_s, lam_st, 91, 5),
                            vmath("SCALE", u_next,
                                  math("MULTIPLY",
                                       at_index(lam_st, math("ADD", index, 1, 90, 6), "FLOAT", 91, 6),
                                       has_next, 92, 6), 92, 7), 93, 5)
        solve = set_position(solve, vmath("ADD", position, stretch_fix, 94, 5), 95, 1)
        return solve

    solve = stretching(solve)

    # -- push apart noodles that overlap.
    # Index of Nearest excludes only the point itself, so a point's own chain
    # neighbours would otherwise win the query and drown out real contacts.
    #
    # Rejecting them by a fixed index distance only works while points sit
    # further apart than a noodle is thick. They must not: with spacing at
    # 2.1x the diameter, two crossing noodles have no pair of sample points
    # within the diameter at all and slide straight through each other -
    # measured at 68% of points embedded in another noodle. Collision
    # sampling needs spacing no larger than the diameter, and at that density
    # a point's own strand is inside the contact radius for several points
    # either side. So the test is by identity, with the index window scaled
    # to however many points now span one diameter.
    def contact_allowed(near_socket, col, row):
        """True where a contact is real: another noodle, or far enough along
        this one that it is not just this point's own neighbourhood."""
        my_id = named(ATTR_ID, "INT", col, row)
        # Integer ids, so any difference is at least 1; MINIMUM turns that
        # into a plain 0/1 flag without needing a compare epsilon.
        other_noodle = math("MINIMUM",
                            math("ABSOLUTE",
                                 math("SUBTRACT", at_index(my_id, near_socket, "INT", col + 1, row),
                                      my_id, col + 2, row), col + 3, row),
                            1.0, col + 4, row)
        far_along = math("GREATER_THAN",
                         math("ABSOLUTE",
                              math("SUBTRACT", near_socket, index, col + 1, row + 1),
                              col + 2, row + 1),
                         # One more than the samples within a diameter: the
                         # segment test reaches one sample past the nearest.
                         math("ADD", math("DIVIDE", diameter, rest, col + 1, row + 2),
                              2.0, col + 2, row + 2),
                         col + 3, row + 1)
        return math("MAXIMUM", other_noodle, far_along, col + 5, row)

    def closest_on_strand(near_socket, col, row):
        """Closest point to this one on the other strand's two segments either
        side of the nearest sample. Point-to-point contact pushed along the
        line between samples, which at a crossing is tilted whenever the
        samples do not line up: an upper strand then shoved the one below it
        sideways on every substep and a settled pile crawled and buzzed. The
        segment gives the true normal of the strand surface."""
        j_pos = at_index(position, near_socket, "FLOAT_VECTOR", col, row)
        ends = []
        for k, (step, gate_field) in enumerate(((-1, has_prev), (1, has_next))):
            other = at_index(position, math("ADD", near_socket, float(step), col, row + 1 + k),
                             "FLOAT_VECTOR", col + 1, row + 1 + k)
            gate = at_index(gate_field, near_socket, "FLOAT", col + 1, row + 3 + k)
            seg = vmath("SCALE", vmath("SUBTRACT", other, j_pos, col + 2, row + 1 + k), gate,
                        col + 3, row + 1 + k)
            t = math("MINIMUM",
                     math("MAXIMUM",
                          math("DIVIDE", vmath("DOT_PRODUCT", vmath("SUBTRACT", position, j_pos,
                                                                     col + 2, row + 5 + k),
                                               seg, col + 3, row + 5 + k),
                               math("MAXIMUM", vmath("DOT_PRODUCT", seg, seg, col + 3, row + 7 + k),
                                    1e-12, col + 4, row + 7 + k), col + 4, row + 5 + k),
                          0.0, col + 5, row + 5 + k), 1.0, col + 6, row + 5 + k)
            q = vmath("ADD", j_pos, vmath("SCALE", seg, t, col + 7, row + 5 + k), col + 8, row + 5 + k)
            ends.append(q)
        d0 = vmath("DISTANCE", position, ends[0], col + 9, row + 5)
        d1 = vmath("DISTANCE", position, ends[1], col + 9, row + 6)
        return mix_vec(math("LESS_THAN", d1, d0, col + 10, row + 6), ends[0], ends[1], col + 11, row + 5)

    def self_push(solve, near_attr):
        near_index = named(near_attr, "INT", 46, 4)
        near_pos = closest_on_strand(near_index, 30, 40)
        away = vmath("SUBTRACT", position, near_pos, 48, 4)
        gap = vmath("LENGTH", away, None, 49, 4)
        off_chain = contact_allowed(near_index, 46, 6)
        # Shock propagation: the strand on top takes the larger share of a
        # contact's correction, so a heap's weight is carried down through it
        # within a substep instead of squeezing the lower layers together - with
        # an even split the lowest strands of a four-layer heap sank a third
        # of a diameter into each other.
        push_share = math("ADD", CONTACT_SHARE,
                          math("MULTIPLY", sep(vmath("NORMALIZE", away, None, 45, 12), 46, 12)[2],
                               STACK_SHARE, 47, 12), 48, 12)
        overlap = math("MAXIMUM", math("SUBTRACT", diameter, gap, 50, 4), 0.0, 51, 4)
        # Cap how far one pass may move a point. A coiled noodle crosses itself,
        # so noodles spawn already overlapped, and an unclamped push turns that
        # first-frame overlap into launch velocity - the pile rose instead of
        # falling. Bounded to a fraction of a segment, the same overlap resolves
        # over a few substeps instead of exploding.
        limit = math("MULTIPLY", rest, PUSH_LIMIT, 51, 5)
        amount = math("MINIMUM",
                      math("MAXIMUM",
                           math("MULTIPLY", overlap, push_share, 53, 4),
                           math("MULTIPLY", limit, -1.0, 52, 5), 53, 5),
                      limit, 54, 5)
        push = vmath("SCALE", vmath("NORMALIZE", away, None, 50, 5),
                     math("MULTIPLY", amount,
                          math("MULTIPLY", off_chain, P["Self Collision"], 52, 6), 54, 4),
                     55, 4)
        solve = store(solve, ATTR_NPUSH,
                      math("ADD", named(ATTR_NPUSH, "FLOAT", 55, 8),
                           math("MULTIPLY", math("MAXIMUM", amount, 0.0, 55, 9),
                                math("MULTIPLY", off_chain, P["Self Collision"], 55, 10), 56, 9),
                           57, 8), "FLOAT", 79, 1)
        solve = set_position(solve, vmath("ADD", position, push, 56, 4), 80, 1)
        return solve

    solve = self_push(solve, ATTR_NEAR)
    solve = self_push(solve, ATTR_NEAR2)

    # -- floor. A hard constraint, so it belongs inside the loop.
    px, py, pz = sep(position, 56, 6)
    spush = named(ATTR_SPUSH, "FLOAT", 57, 10)
    solve = store(solve, ATTR_SPUSH,
                  math("ADD", spush, math("MAXIMUM", math("SUBTRACT", P["Noodle Radius"], pz, 57, 11),
                                          0.0, 58, 11), 59, 10), "FLOAT", 80, 2)
    solve = set_position(solve,
                         xyz(px, py, math("MAXIMUM", pz, P["Noodle Radius"], 57, 6), 58, 6),
                         81, 1)

    # -- collider, as a distance field.
    #
    # The field knows which side of the mesh a point is on and how far in it
    # got, so the whole rule is "walk up the gradient until you are one radius
    # clear". Nothing about it depends on which way the normals were drawn, on
    # where the point was last substep, or on how deep it ended up - the three
    # things a nearest-face query cannot answer.
    #
    # The nearest-face version this replaces treated the closest face as an
    # infinite plane, because that is the only side test available from a
    # single surface sample. A box parked 470 units clear of the pile still
    # pinned points to the plane of its side face and stood the heap up 187
    # tall against 63 for no collider at all - on a collider nothing ever
    # touched. A field is local: the same box measures identical to no
    # collider, to the last digit.
    #
    # The cost of that is that the mesh has to be closed. An open shell
    # voxelises to an unsigned field, where both faces read positive and a
    # point that gets pushed through is then pushed further out rather than
    # back - a thin bowl leaks half the pile. Give it thickness; a Solidify
    # modifier on the same bowl holds everything.
    # Stickiness is the same expression with the sign the other way up, exactly
    # as Cohesion is for noodle-on-noodle: inside the gap the point is pushed
    # out, just outside it is pulled back in. Wet pasta clings to a chopstick,
    # and the distance this needs has already been measured.
    #
    # The pull is capped at a fraction of a segment; the push deliberately is
    # not, because a buried point has to come out however deep it is. An
    # uncapped pull would snap a passing noodle onto the surface hard enough to
    # become launch velocity when it let go.
    stick_reach = math("ADD", collider_gap,
                       math("MULTIPLY", diameter, COHESION_REACH - 1.0, 54, 8), 54, 9)
    pull_cap = math("MULTIPLY", math("MULTIPLY", rest, PUSH_LIMIT, 54, 10), -1.0, 54, 11)

    def collider_pass(solve, grid, stickiness, present, col, row, accumulate=True):
        """Push out of one field, cling to it, and report distance and normal."""
        incoming = solve
        d_i, grad_i = sdf_probe(grid, position, col, row)
        # Outside the narrow band the grid is a constant, so the gradient is
        # exactly zero. That doubles as the "is there a collider at all" test:
        # with none assigned the grid is empty, the gradient is zero, and both
        # the push and the friction below multiply out to nothing.
        in_band = math("GREATER_THAN", vmath("LENGTH", grad_i, None, col + 5, row + 2),
                       voxel, col + 6, row + 2)
        depth = math("SUBTRACT", collider_gap, d_i, col + 5, row)
        push_side = math("LESS_THAN", d_i, collider_gap, col + 5, row + 1)
        stick = math("MAXIMUM",
                     math("MULTIPLY",
                          math("MULTIPLY", depth, stickiness, col + 6, row + 3),
                          math("MULTIPLY",
                               math("LESS_THAN", d_i, stick_reach, col + 6, row + 4),
                               math("SUBTRACT", 1.0, push_side, col + 7, row + 4),
                               col + 7, row + 5), col + 8, row + 3),
                     pull_cap, col + 8, row + 4)
        clear = math("MULTIPLY",
                     math("ADD", math("MULTIPLY", depth, push_side, col + 6, row),
                          stick, col + 7, row), in_band, col + 8, row)
        normal = vmath("NORMALIZE", grad_i, None, col + 7, row + 2)
        if accumulate:
            solve = store(solve, ATTR_SPUSH,
                          math("ADD", spush,
                               math("MULTIPLY", math("MULTIPLY", depth, push_side, col + 9, row + 7),
                                    in_band, col + 10, row + 7), col + 11, row + 7),
                          "FLOAT", col + 9, 0)
        solve = set_position(
            solve, vmath("ADD", position,
                         vmath("SCALE", normal, clear, col + 8, row + 2), col + 9, row + 2), col + 10, 1)
        # What the friction stage needs, taken from the probe already paid for.
        # Parked far away outside the band, so points nowhere near a collider
        # read as "not touching" rather than as "sitting on the surface".
        gated = math("ADD", d_i,
                     math("MULTIPLY", 1e9,
                          math("SUBTRACT", 1.0, in_band, col + 6, row + 6),
                          col + 7, row + 6), col + 8, row + 6)
        # Geometry switches are lazy: an unassigned collider should not run
        # seven SDF samples and a Set Position on every constraint iteration.
        geometry_switch = nd("GeometryNodeSwitch", col + 11, row, input_type="GEOMETRY")
        plug(geometry_switch.inputs["Switch"], present)
        plug(geometry_switch.inputs["False"], incoming)
        plug(geometry_switch.inputs["True"], solve)
        distance_switch = nd("GeometryNodeSwitch", col + 11, row + 1, input_type="FLOAT")
        plug(distance_switch.inputs["Switch"], present)
        plug(distance_switch.inputs["False"], 1e9)
        plug(distance_switch.inputs["True"], gated)
        return (geometry_switch.outputs["Output"], distance_switch.outputs["Output"],
                normal)

    solve, d_plain, n_plain = collider_pass(solve, grid, P["Collider Stickiness"], has_plain, 82, 8)
    solve, d_stick, n_stick = collider_pass(solve, sticky_grid, P["Sticky Collider Grip"], has_sticky, 82, 16)
    # Friction only cares which surface is nearest, not which set it came from.
    solve = store(solve, ATTR_CDIST, math("MINIMUM", d_plain, d_stick, 93, 12),
                  "FLOAT", 94, 12)
    solve = store(solve, ATTR_CNRM,
                  mix_vec(math("LESS_THAN", d_stick, d_plain, 93, 13), n_plain, n_stick, 94, 13),
                  "FLOAT_VECTOR", 95, 12)

    plug(rep_out.inputs["Geometry"], solve)
    state = rep_out.outputs["Geometry"]
    # Bending once per substep, after the contact passes, so that supports
    # never leave a kink in a strand's shape. Solving it inside every pass
    # measured the same settled heap for 10% more time per frame.
    state = bending(state)

    # ---------------------------------------------- velocity, then friction
    # Velocity is whatever motion survived the constraints. Friction acts
    # here, on velocity, not on position: a positional version has to fight
    # the constraints to do its job.
    #
    # It is Coulomb friction. The normal impulse of a contact is how far the
    # contact constraints pushed a point out this substep, summed over the
    # iterations; the tangential velocity it may remove is Friction times
    # that, divided by dt. A noodle at rest on a slope therefore holds while the slope is
    # shallower than atan(Friction) and slides when it is steeper, landing
    # kills sliding in proportion to how hard it lands, and the result does
    # not change with the substep count. The previous rule removed a fixed
    # 90% of all horizontal velocity on every substep of any contact, which
    # made wet noodles behave like velcro: strands froze into loops and
    # nests in mid-slump and never settled into the bowl.
    #
    # It has to cover noodle-against-noodle contact, not just the ground: only
    # the bottom layer is ever "on the floor", so a ground-only rule lets
    # everything above it flow like a liquid. Noodle contacts use the
    # velocity relative to the strand underneath, so a pile sliding as one
    # mass keeps sliding.
    prev_pos = named(ATTR_PREV, "FLOAT_VECTOR", 64, 3)
    inv_dt = math("DIVIDE", 1.0, math("MAXIMUM", dt, 1e-6, 65, 4), 66, 4)
    velocity_out = vmath("SCALE", vmath("SUBTRACT", position, prev_pos, 65, 3), inv_dt, 67, 3)


    _, _, fz = sep(position, 64, 6)
    # Contact skins are a fraction of the radius, never a fixed distance. A
    # constant here silently stops meaning anything when the scene is rescaled:
    # half a unit is a rounding error next to a 2.5-unit noodle and twenty-five
    # times the whole radius of a 1 mm one.
    on_floor = math("LESS_THAN", fz, math("MULTIPLY", P["Noodle Radius"], CONTACT_SKIN, 65, 7), 66, 7)
    # Fresh nodes rather than reusing the ones inside the zone: a field node
    # feeding both sides of a zone boundary is asking for trouble. The push
    # has already separated the pair by now, so this asks "still touching",
    # not "was overlapping".
    near_after = named(ATTR_NEAR, "INT", 64, 9)
    near_vec = vmath("SUBTRACT", position, closest_on_strand(near_after, 30, 60), 66, 9)
    touch_gap = vmath("LENGTH", near_vec, None, 67, 9)
    touching = math("MULTIPLY",
                    math("LESS_THAN", touch_gap,
                         math("MULTIPLY", diameter, 1.1, 65, 10), 68, 10),
                    contact_allowed(near_after, 65, 11),
                    69, 10)
    touching = math("MULTIPLY", touching, P["Self Collision"], 69, 11)
    # Resting on the collider counts as contact too, or noodles would keep
    # sliding around the inside of a bowl instead of settling in it.
    on_collider = math("LESS_THAN", named(ATTR_CDIST, "FLOAT", 64, 11),
                       math("MULTIPLY", P["Noodle Radius"], CONTACT_SKIN, 65, 12), 67, 11)

    def coulomb(v, normal, mu, gate, push_attr, col, row, share=1.0):
        """Remove up to mu * (normal impulse) of `share` of the sliding part of v."""
        vn = vmath("SCALE", normal, vmath("DOT_PRODUCT", v, normal, col, row), col + 1, row)
        vt = vmath("SUBTRACT", v, vn, col + 2, row)
        push = math("MULTIPLY", named(push_attr, "FLOAT", col, row + 1), inv_dt, col + 1, row + 1)
        cut = math("MINIMUM",
                   math("DIVIDE", math("MULTIPLY", math("MULTIPLY", push, mu, col + 2, row + 1),
                                       gate, col + 3, row + 1),
                        math("MAXIMUM", vmath("LENGTH", vt, None, col + 2, row + 2), 1e-9,
                             col + 3, row + 2), col + 4, row + 1),
                   1.0, col + 5, row + 1)
        return vmath("SCALE", vt, math("MULTIPLY", cut, share, col + 5, row + 2), col + 6, row)

    # Surfaces that do not move with the noodle: floor and colliders. A point
    # on the collider uses its normal and Collider Friction; otherwise the
    # floor's, with Friction.
    surface_normal = mix_vec(on_collider, (0.0, 0.0, 1.0), named(ATTR_CNRM, "FLOAT_VECTOR", 66, 16),
                             67, 16)
    mu_mix = nd("ShaderNodeMix", 66, 17, data_type="FLOAT", factor_mode="UNIFORM")
    plug(pick_in(mu_mix, "Factor"), on_collider)
    plug(pick_in(mu_mix, "A"), P["Friction"])
    plug(pick_in(mu_mix, "B"), P["Collider Friction"])
    surface_mu = pick_out(mu_mix, "Result")
    surface_gate = math("MAXIMUM", on_floor, on_collider, 67, 17)
    surface_cut = coulomb(velocity_out, surface_normal, surface_mu, surface_gate, ATTR_SPUSH, 68, 16)
    # Sliding on a floor or collider: friction could not stop it this substep.
    surface_slips = math("MULTIPLY", surface_gate,
                         math("GREATER_THAN",
                              vmath("LENGTH", vmath("SUBTRACT", velocity_out,
                                                    vmath("SCALE", surface_normal,
                                                          vmath("DOT_PRODUCT", velocity_out, surface_normal,
                                                                66, 52), 67, 52), 68, 52), None, 69, 52),
                              math("MULTIPLY", math("MULTIPLY", named(ATTR_SPUSH, "FLOAT", 66, 53), inv_dt, 67, 53),
                                   surface_mu, 68, 53), 70, 52), 71, 52)
    velocity_out = vmath("SUBTRACT", velocity_out, surface_cut, 75, 16)
    # Surface friction acts on this substep's displacement too, not only on
    # the velocity carried into the next one: otherwise every substep's
    # gravity still slides a resting noodle a little before friction catches
    # it, and a strand on a 15 degree slope with Friction 0.5 crept at
    # 12 mm/s. Only the tangential part changes, so the contact stays
    # resolved. Noodle-on-noodle friction stays on velocity alone: moving
    # those points shifted them against their own strand's constraints, and
    # the next substep's correction made a dense pile tremble.
    state = store(state, ATTR_SFRIC, vmath("SCALE", surface_cut, dt, 76, 17), "FLOAT_VECTOR", 76, 2)
    # Noodle on noodle: friction on the velocity relative to the touching strand.
    near_vel = at_index(vmath("SCALE", vmath("SUBTRACT", position, prev_pos, 68, 19), inv_dt, 69, 19),
                        near_after, "FLOAT_VECTOR", 70, 19)
    relative = vmath("SUBTRACT", velocity_out, near_vel, 71, 19)
    contact_normal = vmath("NORMALIZE", near_vec, None, 71, 20)
    # Both points of a touching pair apply this, each to itself, so a pair
    # side by side meets halfway; but a strand lying on another takes on the
    # lower one's motion, which is what is carrying it. Removing all of the
    # relative velocity on both sides instead swapped their velocities every
    # substep, and the pile never stopped trading motion back and forth.
    upper_share = math("ADD", 0.5,
                       math("MULTIPLY", sep(contact_normal, 71, 21)[2], 0.5, 72, 21), 73, 21)
    velocity_out = vmath("SUBTRACT", velocity_out,
                         coulomb(relative, contact_normal, P["Friction"], touching, ATTR_NPUSH, 72, 19,
                                 share=upper_share), 79, 19)
    # Wet noodles do not bounce off each other: the approaching or separating
    # speed of a touching pair is shared out, as in a perfectly inelastic
    # collision. Contacts here only ever see one neighbour each, so without
    # this the position pushes rattled a settled pile at a few cm/s.
    rel_n = vmath("DOT_PRODUCT", relative, contact_normal, 72, 23)
    velocity_out = vmath("SUBTRACT", velocity_out,
                         vmath("SCALE", contact_normal,
                               math("MULTIPLY", math("MULTIPLY", rel_n, touching, 73, 23),
                                    0.5, 74, 23), 75, 23), 79, 23)

    # Cohesion: cooked pasta clings to itself, and a pile of it is not a pile
    # of dry sticks. A strand within COHESION_REACH diameters of another is
    # drawn to it with an acceleration of Cohesion times gravity - a force, so
    # it does not depend on the step count, unlike the per-pass position pull
    # it replaces, which also fought the contact push and kept the pile
    # trembling.
    reach = math("MULTIPLY", diameter, COHESION_REACH, 70, 25)
    clinging = math("MULTIPLY",
                    math("MULTIPLY", math("LESS_THAN", touch_gap, reach, 71, 25),
                         contact_allowed(near_after, 65, 26), 72, 25),
                    P["Self Collision"], 73, 25)
    velocity_out = vmath("SUBTRACT", velocity_out,
                         vmath("SCALE", contact_normal,
                               math("MULTIPLY", math("MULTIPLY", P["Cohesion"], P["Gravity"], 74, 26),
                                    math("MULTIPLY", dt, clinging, 75, 26), 76, 26), 77, 25), 80, 25)

    # Settling in. A poured portion spawns with strands crossing through one
    # another; for the first SPAWN_SETTLE seconds the contact solve untangles
    # them with velocity held at zero, so the overlap is pushed apart instead
    # of being turned into speed that flung strands out of the bowl.
    #
    # Then the portion is lowered in, as with tongs, rather than dropped:
    # for SPAWN_LOWER_TIME seconds no point moves faster than
    # SPAWN_LOWER_SPEED. Dropped from the top of its coil, a full serving hit
    # the bowl at over 2 m/s and splashed strands over the rim. Both apply
    # only to the poured spawn (Spawn Coil above 0); upright rods drop freely.
    age = named(ATTR_AGE, "FLOAT", 72, 30)
    poured_spawn = math("GREATER_THAN", P["Spawn Coil"], 0.0, 72, 31)
    held = math("MULTIPLY", poured_spawn, math("LESS_THAN", age, SPAWN_SETTLE, 73, 31), 74, 31)
    lowering = math("MULTIPLY", poured_spawn, math("LESS_THAN", age, SPAWN_LOWER_TIME, 73, 32), 74, 32)
    cap = math("MINIMUM", 1.0,
               math("DIVIDE", SPAWN_LOWER_SPEED,
                    math("MAXIMUM", vmath("LENGTH", velocity_out, None, 73, 33), 1e-6, 74, 33), 75, 33),
               76, 33)
    velocity_out = vmath("SCALE", velocity_out,
                         math("MULTIPLY", math("SUBTRACT", 1.0, held, 75, 31),
                              math("ADD", math("SUBTRACT", 1.0, lowering, 76, 32),
                                   math("MULTIPLY", lowering, cap, 77, 32), 78, 32), 79, 31),
                         74, 30)
    state = store(state, ATTR_VEL, velocity_out, "FLOAT_VECTOR", 74, 1)
    damped = named(ATTR_VEL, "FLOAT_VECTOR", 75, 21)

    # Coming to rest. A position-based contact solve never balances forces
    # exactly, so a heap in which every strand is held still trades a few
    # mm/s back and forth between substeps and never settles. That residual
    # has no direction; real motion does. So each point keeps a running mean
    # of its displacement per substep over about SETTLE_TIME seconds, and a
    # point in contact whose mean drift is slower than SETTLE_FRACTION *
    # sqrt(g r) is at rest: Contact Damping keeps it where it was and clears
    # its velocity. A strand that should slide - a slope steeper than its
    # friction angle, a strand dragged by another - drifts steadily, far above
    # that speed within a few substeps, and is never held. The old version
    # damped every slow contact and froze strands in mid-slump.
    step_now = vmath("SUBTRACT",
                     vmath("SUBTRACT", position, named(ATTR_SFRIC, "FLOAT_VECTOR", 70, 46), 71, 46),
                     prev_pos, 72, 46)
    drift_mix = math("SUBTRACT", 1.0,
                     math("EXPONENT", math("DIVIDE", math("MULTIPLY", dt, -1.0, 70, 47), SETTLE_TIME, 71, 47),
                          None, 72, 47), 73, 47)
    drift_old = named(ATTR_DRIFT, "FLOAT_VECTOR", 70, 48)
    state = store(state, ATTR_DRIFT,
                  vmath("ADD", drift_old, vmath("SCALE", vmath("SUBTRACT", step_now, drift_old, 73, 48),
                                                drift_mix, 74, 48), 75, 48), "FLOAT_VECTOR", 76, 0)
    in_contact = touching
    at_rest = math("MULTIPLY", math("MULTIPLY", in_contact, math("SUBTRACT", 1.0, surface_slips, 70, 53), 71, 53),
                   math("LESS_THAN",
                        math("MULTIPLY", vmath("LENGTH", named(ATTR_DRIFT, "FLOAT_VECTOR", 72, 50), None, 73, 50),
                             inv_dt, 74, 50),
                        math("MULTIPLY",
                             math("SQRT", math("MULTIPLY", P["Gravity"], P["Noodle Radius"], 72, 51),
                                  None, 73, 51), SETTLE_FRACTION, 74, 51), 75, 50), 78, 40)
    state = store(state, ATTR_REST, math("MULTIPLY", at_rest, P["Contact Damping"], 79, 41),
                  "FLOAT", 80, 0)
    # Only points resting on other noodles are also held in place; on a floor
    # or collider only their speed is cleared. Holding a strand's points on a
    # rod while its tails moved broke its length constraints, and a strand
    # hanging over a chopstick slid off it.
    state = store(state, ATTR_HOLD, math("MULTIPLY", named(ATTR_REST, "FLOAT", 80, 44),
                                         math("SUBTRACT", 1.0, surface_gate, 79, 45), 80, 45),
                  "FLOAT", 81, 0)
    resting = named(ATTR_REST, "FLOAT", 80, 42)
    state = store(state, ATTR_VEL, vmath("SCALE", damped, math("SUBTRACT", 1.0, resting, 80, 43), 78, 14),
                  "FLOAT_VECTOR", 81, 1)
    state = set_position(state, vmath("SUBTRACT", position, named(ATTR_SFRIC, "FLOAT_VECTOR", 79, 15),
                                      81, 15), 82, 1)

    # Post-stabilisation. Bending is solved after the contact passes so the
    # strand's shape is never left kinked by a support, but that can leave a
    # point a fraction of a millimetre inside a contact. Resolving it here,
    # after velocity has been read back, removes the overlap without turning
    # it into speed - otherwise the next substep's push-out became a bounce
    # and the settled pile buzzed at a millimetre or two.
    post_near = named(ATTR_NEAR, "INT", 82, 20)
    post_away = vmath("SUBTRACT", position, closest_on_strand(post_near, 30, 50), 84, 20)
    post_overlap = math("MINIMUM",
                        math("MULTIPLY",
                             math("MAXIMUM", math("SUBTRACT", diameter,
                                                  vmath("LENGTH", post_away, None, 85, 21), 85, 20),
                                  0.0, 86, 20),
                             math("ADD", CONTACT_SHARE,
                                  math("MULTIPLY", sep(vmath("NORMALIZE", post_away, None, 84, 26), 85, 26)[2],
                                       STACK_SHARE, 86, 26), 87, 26), 87, 20),
                        math("MULTIPLY", rest, PUSH_LIMIT, 86, 21), 88, 20)
    state = set_position(
        state, vmath("ADD", position,
                     vmath("SCALE", vmath("NORMALIZE", post_away, None, 85, 22),
                           math("MULTIPLY", post_overlap,
                                math("MULTIPLY", contact_allowed(post_near, 82, 23),
                                     P["Self Collision"], 88, 23), 89, 22), 90, 22), 91, 22), 83, 2)
    ppx, ppy, ppz = sep(position, 84, 25)
    state = set_position(state, xyz(ppx, ppy, math("MAXIMUM", ppz, P["Noodle Radius"], 85, 25), 86, 25),
                         84, 2)
    state, _, _ = collider_pass(state, grid, 0.0, has_plain, 85, 28, accumulate=False)
    state, _, _ = collider_pass(state, sticky_grid, 0.0, has_sticky, 85, 36, accumulate=False)

    # A point at rest also keeps its place, after every correction of this
    # substep: what the solve and the overlap pass moved it by is residual,
    # not motion, and left in it made the heap tremble.
    state = set_position(state, mix_vec(named(ATTR_HOLD, "FLOAT", 84, 44), position,
                                        named(ATTR_PREV, "FLOAT_VECTOR", 84, 45), 85, 44), 86, 44)

    # -- plasticity. Cooked dough is viscoplastic: a bend that is held sets.
    # Each joint's rest bend creeps towards PLASTIC_SET of its current bend
    # with time constant PLASTIC_TIME, so a strand that has lain curled in
    # the pile for a moment keeps a softened version of that curl instead of
    # springing back straight, while PLASTIC_SET < 1 keeps an elastic part
    # that always resists, so held bends never creep into kinks. This is why
    # a served portion lies in soft folds rather than as straight sticks, and
    # why the pile comes to rest: bending stress relaxes instead of pushing
    # strands against their friction forever.
    creep = math("SUBTRACT", 1.0,
                 math("EXPONENT", math("DIVIDE", math("MULTIPLY", dt, -1.0, 86, 40), PLASTIC_TIME, 87, 40),
                      None, 88, 40), 89, 40)
    set_now = named(ATTR_BREST, "FLOAT_VECTOR", 86, 41)
    state = store(state, ATTR_BREST,
                  vmath("ADD", set_now,
                        vmath("SCALE", vmath("SUBTRACT", bend_offset(PLASTIC_SET, 80, 42), set_now, 88, 41),
                              creep, 89, 41), 90, 41), "FLOAT_VECTOR", 91, 40)

    plug(sub_out.inputs["Geometry"], state)
    plug(sim_out.inputs["Geometry"], sub_out.outputs["Geometry"])

    # -------------------------------------------------------------- surface
    # Display only. Everything below reads the simulated curves after the
    # simulation zone and never feeds back into it, so none of these inputs
    # can change the physics. Columns from 80 on keep it clear of the solver.
    #
    # Attributes written for the shader (see materials.py), all anchored to
    # the noodle so textures do not swim as strands move:
    #   nb_prof   cross-section position in metres (x across, y thickness)
    #   nb_along  arc length from the strand start in metres
    #   nb_end    distance to the nearer cut end in metres (0 on the caps)
    is_view = nd("GeometryNodeIsViewport", 80, 0).outputs["Is Viewport"]

    def choose(switch, if_true, if_false, kind, col, row):
        n = nd("GeometryNodeSwitch", col, row, input_type=kind)
        plug(n.inputs["Switch"], switch)
        plug(n.inputs["True"], if_true)
        plug(n.inputs["False"], if_false)
        return n.outputs["Output"]

    radius = P["Noodle Radius"]

    # Profile: a unit superellipse, |x|^n + |y|^n = 1. Squareness 0 is the
    # old circle; towards 1 it becomes the rounded square of knife-cut udon
    # and soba, or with Aspect < 1 a flat ribbon with softened edges. Final
    # renders use Render Faces so close-up silhouettes stay round; the
    # viewport keeps the cheaper Profile Faces.
    faces = choose(is_view, P["Profile Faces"],
                   math("MAXIMUM", P["Profile Faces"], P["Render Faces"], 80, 1),
                   "INT", 81, 1)
    circle = nd("GeometryNodeCurvePrimitiveCircle", 82, 1, mode="RADIUS")
    plug(circle.inputs["Resolution"], faces)
    plug(circle.inputs["Radius"], 1.0)
    exponent = math("SUBTRACT", 1.0, math("MULTIPLY", P["Profile Squareness"], 0.75, 80, 3),
                    81, 3)
    ux, uy, _ = sep(nd("GeometryNodeInputPosition", 80, 4).outputs["Position"], 81, 4)

    def superellipse(c, col, row):
        return math("MULTIPLY", math("SIGN", c, None, col, row),
                    math("POWER", math("ABSOLUTE", c, None, col, row + 1), exponent,
                         col + 1, row + 1), col + 1, row)

    prof_pos = xyz(superellipse(ux, 82, 3),
                   math("MULTIPLY", superellipse(uy, 82, 5), P["Profile Aspect"], 84, 5),
                   0.0, 85, 4)
    profile = set_position(circle.outputs["Curve"], prof_pos, 86, 1)
    profile = store(profile, "nb_prof",
                    vmath("SCALE", nd("GeometryNodeInputPosition", 86, 3).outputs["Position"],
                          radius, 87, 3), "FLOAT_VECTOR", 87, 1)

    # Drop the solver's working data before the surface. Curve to Mesh
    # interpolates every attribute it is handed onto every vertex of every
    # tube, so leaving nine scratch attributes attached meant carrying and
    # rebuilding them across ~145k vertices a frame for nothing. np_id and
    # np_vel survive on purpose - see the shader notes in the README.
    clean = sim_out.outputs["Geometry"]
    for i, attr in enumerate(SCRATCH_ATTRS):
        strip = nd("GeometryNodeRemoveAttribute", 80 + i, 7)
        plug(strip.inputs["Geometry"], clean)
        plug(strip.inputs["Name"], attr)
        clean = strip.outputs["Geometry"]
    bezier = nd("GeometryNodeCurveSplineType", 88, 8, spline_type="BEZIER")
    plug(bezier.inputs["Curve"], clean)
    handles = nd("GeometryNodeCurveSetHandles", 89, 8, handle_type="AUTO")
    plug(handles.inputs["Curve"], bezier.outputs["Curve"])
    resolution = nd("GeometryNodeSetSplineResolution", 90, 8)
    plug(resolution.inputs["Geometry"], handles.outputs["Curve"])
    plug(resolution.inputs["Resolution"], choose(is_view, 3, 6, "INT", 89, 9))
    surface = nd("GeometryNodeSwitch", 91, 7, input_type="GEOMETRY")
    plug(surface.inputs["Switch"], P["Smooth Surface"])
    plug(surface.inputs["False"], clean)
    plug(surface.inputs["True"], resolution.outputs["Geometry"])

    # Make the evaluated (smoothed) points real, so the per-point shape
    # fields below have the full display resolution, then add extra points
    # on the first and last segment only, for the rounded cut ends.
    evaluated = nd("GeometryNodeResampleCurve", 92, 7)
    plug(evaluated.inputs["Curve"], surface.outputs["Output"])
    pick_in(evaluated, "Mode").default_value = "Evaluated"
    sp_index = nd("GeometryNodeSplineParameter", 92, 9).outputs["Index"]
    sp_count = nd("GeometryNodeSplineLength", 92, 10).outputs["Point Count"]
    end_segment = math("MAXIMUM", math("LESS_THAN", sp_index, 0.5, 93, 9),
                       math("GREATER_THAN", sp_index,
                            math("SUBTRACT", sp_count, 2.5, 93, 10), 94, 10), 95, 9)
    soft_on = math("GREATER_THAN", P["End Softness"], 0.0, 94, 11)
    ends = nd("GeometryNodeSubdivideCurve", 96, 7)
    plug(ends.inputs["Curve"], evaluated.outputs["Curve"])
    plug(ends.inputs["Cuts"], math("MULTIPLY", math("MULTIPLY", end_segment, soft_on, 95, 10),
                                   choose(is_view, 4, 10, "INT", 95, 11), 96, 9))

    along = nd("GeometryNodeSplineParameter", 97, 9).outputs["Length"]
    total = nd("GeometryNodeSplineLength", 97, 10).outputs["Length"]
    to_end = math("MINIMUM", along, math("SUBTRACT", total, along, 98, 10), 99, 9)
    shaped = store(ends.outputs["Curve"], "nb_along", along, "FLOAT", 98, 7)
    shaped = store(shaped, "nb_end", to_end, "FLOAT", 99, 7)
    strand = named(ATTR_ID, "INT", 100, 9)
    along_n = named("nb_along", "FLOAT", 100, 10)
    end_n = named("nb_end", "FLOAT", 100, 11)

    # Crinkle: a wandering wave across the strand. Its wavelength follows
    # the diameter, its phase drifts with a slow noise so it is not a sine.
    # It moves only the displayed tube; the simulated centreline is intact.
    wavelength = math("MULTIPLY", radius, 7.0, 101, 12)
    cycles = math("DIVIDE", along_n, math("MAXIMUM", wavelength, 1e-9, 102, 12), 103, 12)
    drift = nd("ShaderNodeTexNoise", 104, 12, noise_dimensions="2D")
    plug(drift.inputs["Vector"], xyz(math("MULTIPLY", cycles, 0.3, 104, 13),
                                     math("MULTIPLY", strand, NOODLE_SPREAD, 104, 14), 0.0, 105, 13))
    phase = math("ADD", math("MULTIPLY", cycles, tau, 105, 12),
                 math("ADD", math("MULTIPLY", drift.outputs["Factor"], 9.0, 106, 13),
                      math("MULTIPLY", strand, 2.399, 106, 14), 107, 13), 107, 12)
    amplitude = math("MULTIPLY", P["Waviness"], math("MULTIPLY", radius, 0.9, 101, 14), 102, 14)
    normal = nd("GeometryNodeInputNormal", 101, 15).outputs["Normal"]
    tangent = nd("GeometryNodeInputTangent", 101, 16).outputs["Tangent"]
    binormal = vmath("CROSS_PRODUCT", tangent, normal, 102, 16)
    wave = vmath("ADD",
                 vmath("SCALE", normal, math("SINE", phase, None, 108, 12), 103, 15),
                 vmath("SCALE", binormal,
                       math("MULTIPLY", math("SINE", math("MULTIPLY", phase, 0.5, 108, 14),
                                             None, 109, 14), 0.25, 110, 14), 103, 16),
                 104, 15)
    shaped = set_position(shaped,
                          vmath("ADD", nd("GeometryNodeInputPosition", 105, 15).outputs["Position"],
                                vmath("SCALE", wave, amplitude, 106, 15), 107, 15), 101, 7)

    # Thickness: slow unevenness along the strand (hand/knife cut), then a
    # quarter-circle fillet at each cut end so caps are softened, not sharp.
    swell = nd("ShaderNodeTexNoise", 102, 17, noise_dimensions="2D")
    plug(swell.inputs["Vector"], xyz(math("DIVIDE", along_n,
                                          math("MAXIMUM", math("MULTIPLY", radius, 9.0, 102, 18),
                                               1e-9, 103, 18), 104, 18),
                                     math("MULTIPLY", strand, NOODLE_SPREAD, 104, 19), 0.0, 105, 18))
    plug(swell.inputs["Detail"], 2.0)
    thickness = math("ADD", 1.0,
                     math("MULTIPLY", math("SUBTRACT", swell.outputs["Factor"], 0.5, 106, 17),
                          math("MULTIPLY", P["Thickness Variation"], 4.0, 106, 18), 107, 17),
                     108, 17)
    # Each end gets its own softness, so the two cuts of a strand differ.
    end_noise = nd("ShaderNodeTexWhiteNoise", 102, 20, noise_dimensions="2D")
    plug(end_noise.inputs["Vector"],
         xyz(strand, math("GREATER_THAN", along_n, math("MULTIPLY", total, 0.5, 103, 21), 104, 21),
             0.0, 105, 20))
    soft = math("MULTIPLY", P["End Softness"],
                math("ADD", 0.7, math("MULTIPLY", end_noise.outputs["Value"], 0.6, 106, 20), 107, 20),
                108, 20)
    t = math("MINIMUM", math("DIVIDE", end_n,
                             math("MAXIMUM", math("MULTIPLY", soft, radius, 109, 21), 1e-9, 110, 21),
                             111, 21), 1.0, 112, 21)
    u = math("SUBTRACT", 1.0, t, 113, 21)
    arc = math("SQRT", math("MAXIMUM", math("SUBTRACT", 1.0, math("MULTIPLY", u, u, 114, 21), 115, 21),
                            0.0, 116, 21), None, 117, 21)
    fillet = math("SUBTRACT", 1.0, math("MULTIPLY", soft, math("SUBTRACT", 1.0, arc, 118, 21), 119, 20),
                  120, 20)
    scale = math("MULTIPLY", radius, math("MULTIPLY", thickness, fillet, 121, 18), 122, 17)

    # The centreline rides onto each ring so the mesh pass below can push
    # vertices radially; it is removed again afterwards.
    shaped = store(shaped, "nb_axis", nd("GeometryNodeInputPosition", 108, 8).outputs["Position"],
                   "FLOAT_VECTOR", 109, 7)
    to_mesh = nd("GeometryNodeCurveToMesh", 110, 7)
    plug(to_mesh.inputs["Curve"], shaped)
    plug(to_mesh.inputs["Profile Curve"], profile)
    plug(pick_in(to_mesh, "Scale"), scale)
    plug(to_mesh.inputs["Fill Caps"], True)

    # Edge irregularity, for ribbons: the cut edges wander in and out along
    # the strand and the sheet is unevenly thick across its width, instead
    # of a perfect extrusion. Vertices move radially from their own ring
    # centre: near the edges (|x| close to the half-width) by an edge noise,
    # elsewhere by a thickness noise that varies across the width.
    centre = named("nb_axis", "FLOAT_VECTOR", 110, 23)
    radial = vmath("SUBTRACT", nd("GeometryNodeInputPosition", 110, 24).outputs["Position"],
                   centre, 111, 23)
    prof_x, _, _ = sep(named("nb_prof", "FLOAT_VECTOR", 110, 25), 111, 25)
    safe_r = math("MAXIMUM", radius, 1e-9, 111, 26)
    across = math("DIVIDE", math("ABSOLUTE", prof_x, None, 112, 25), safe_r, 113, 25)
    edge_w = math("MINIMUM", math("MAXIMUM", math("DIVIDE", math("SUBTRACT", across, 0.55, 114, 25),
                                                  0.45, 115, 25), 0.0, 116, 25), 1.0, 117, 25)
    edge_w = math("MULTIPLY", edge_w, edge_w, 118, 25)
    mesh_along = named("nb_along", "FLOAT", 110, 27)
    mesh_id = named(ATTR_ID, "INT", 110, 28)
    fray = nd("ShaderNodeTexNoise", 112, 27, noise_dimensions="2D")
    plug(fray.inputs["Vector"], xyz(math("DIVIDE", mesh_along, math("MULTIPLY", safe_r, 1.3, 112, 29),
                                         113, 28),
                                    math("MULTIPLY", mesh_id, NOODLE_SPREAD, 113, 29), 0.0, 114, 28))
    plug(fray.inputs["Detail"], 2.0)
    plug(fray.inputs["Roughness"], 0.55)
    uneven = nd("ShaderNodeTexNoise", 112, 30, noise_dimensions="3D")
    plug(uneven.inputs["Vector"], xyz(math("DIVIDE", prof_x, math("MULTIPLY", safe_r, 0.45, 113, 31),
                                           114, 31),
                                      math("DIVIDE", mesh_along, math("MULTIPLY", safe_r, 1.6, 113, 32),
                                           114, 32),
                                      math("MULTIPLY", mesh_id, NOODLE_SPREAD, 113, 33), 115, 31))
    plug(uneven.inputs["Detail"], 2.0)
    wobble = math("ADD",
                  math("MULTIPLY", math("MULTIPLY", math("SUBTRACT", fray.outputs["Factor"], 0.5, 115, 27),
                                        0.22, 116, 27), edge_w, 117, 27),
                  math("MULTIPLY", math("MULTIPLY", math("SUBTRACT", uneven.outputs["Factor"], 0.5, 116, 30),
                                        0.6, 117, 30),
                       math("SUBTRACT", 1.0, edge_w, 117, 31), 118, 30), 119, 27)
    rough_edges = set_position(
        to_mesh.outputs["Mesh"],
        vmath("ADD", centre,
              vmath("SCALE", radial,
                    math("ADD", 1.0, math("MULTIPLY", wobble, P["Edge Irregularity"], 120, 27),
                         121, 27), 120, 23), 121, 23), 122, 22)
    drop_axis = nd("GeometryNodeRemoveAttribute", 123, 22)
    plug(drop_axis.inputs["Geometry"], rough_edges)
    plug(drop_axis.inputs["Name"], "nb_axis")
    smooth = nd("GeometryNodeSetShadeSmooth", 111, 7, domain="FACE")
    plug(smooth.inputs["Mesh"], drop_axis.outputs["Geometry"])
    plug(smooth.inputs["Shade Smooth"], True)
    set_mat = nd("GeometryNodeSetMaterial", 112, 7)
    plug(set_mat.inputs["Geometry"], smooth.outputs["Mesh"])
    plug(set_mat.inputs["Material"], P["Noodle Material"])
    plug(nd("NodeGroupOutput", 113, 7).inputs["Geometry"], set_mat.outputs["Geometry"])
    return ng


def build_object(ng):
    """Create an independent solver without deleting existing scene content."""
    obj = bpy.data.objects.new(OBJ_NAME, bpy.data.meshes.new(OBJ_NAME))
    bpy.context.scene.collection.objects.link(obj)
    obj.modifiers.new("Noodles", "NODES").node_group = ng
    obj["noodle_physics_version"] = 2
    return obj


def noodle_modifier(obj):
    if obj is None:
        return None
    return next((m for m in obj.modifiers if m.type == "NODES" and m.node_group
                 and m.node_group.get("noodle_physics_version") == 2), None)


def input_property(obj, ng, name):
    socket = next((s for s in ng.interface.items_tree
                   if s.item_type == "SOCKET" and s.in_out == "INPUT"
                   and s.name == name), None)
    modifier = next((m for m in obj.modifiers if m.type == "NODES"
                     and m.node_group == ng), None)
    if socket is None or modifier is None:
        raise CliError(f"noodle input {name!r} is unavailable on {obj.name!r}")
    return getattr(modifier.properties.inputs, socket.identifier)


def get_input(obj, ng, name):
    return input_property(obj, ng, name).value


def set_input(obj, ng, name, value):
    """Set a modifier input, with an actionable error for renamed sockets."""
    socket = next((s for s in ng.interface.items_tree
                   if getattr(s, "item_type", None) == "SOCKET" and s.name == name), None)
    if socket is None:
        available = [s.name for s in ng.interface.items_tree
                     if getattr(s, "item_type", None) == "SOCKET"]
        raise CliError(f"node-group socket {name!r} is missing; available: {available}")
    target = input_property(obj, ng, name)
    target.value = value

    # Writing an Object or Collection socket this way does not rebuild the
    # modifier's dependency relations on its own, and without them Object Info
    # hands the solver an empty mesh - a collider assigned from a script simply
    # does nothing, silently. Setting it in the modifier panel tags this for
    # you; from Python it has to be asked for.
    obj.update_tag()


def bake(obj, frames, report=None):
    """Step the timeline so the Simulation Zone advances, returning per-frame stats.

    A simulation zone only advances when the scene frame does, one frame at a
    time and forwards. Evaluating frame 200 directly gets you frame 1's state.

    Stats are read out while each mesh is still alive - an evaluated mesh is
    invalidated by the next depsgraph update, so it cannot be returned.
    """
    scene = bpy.context.scene
    history = {}
    for frame in range(1, frames + 1):
        scene.frame_set(scene.frame_start + frame - 1)
        evaluated = obj.evaluated_get(bpy.context.evaluated_depsgraph_get())
        mesh = evaluated.to_mesh()
        try:
            if not mesh.vertices:
                raise RuntimeError(f"frame {frame} produced no evaluated vertices")
            zs = [v.co.z for v in mesh.vertices]
            reach = max(max(abs(v.co.x), abs(v.co.y)) for v in mesh.vertices)
            history[frame] = (len(mesh.vertices), min(zs), max(zs), reach)
            if report and frame in report:
                print(f"  frame {frame:4}  z {min(zs):8.1f} .. {max(zs):8.1f}  reach {reach:7.1f}")
        finally:
            evaluated.to_mesh_clear()

    return history


def substeps_for(radius, gravity, start_height, fps=24.0, headroom=1.0):
    """Conservative, uncapped step budget for a fall from rest.

    Return the ceiling, never round down. Callers can then report when the
    required budget exceeds MAX_RUNTIME_SUBSTEPS instead of hiding the deficit.
    """
    values = (radius, gravity, start_height, fps, headroom)
    if not all(isfinite(v) for v in values):
        raise ValueError("step-budget inputs must be finite")
    if radius <= 0 or fps <= 0 or headroom < 1 or gravity < 0 or start_height < 0:
        raise ValueError("positive radius/fps, nonnegative gravity/height and headroom >= 1 required")
    speed = sqrt(2.0 * gravity * start_height)
    return max(1, ceil(headroom * speed / (2.0 * radius * VELOCITY_SAFETY * fps)))


# All presets describe tabletop noodles in metres. Count limits are starting
# points for preview workloads, not an FPS guarantee. Quality changes thickness.
REALTIME_PRESETS = {
    "default": ({}, 48),
    "quality": ({"Noodle Radius": 0.004, "Substeps": 20,
                 "Profile Faces": 8}, 48),
    "balanced": ({"Noodle Radius": 0.006, "Substeps": 14,
                  "Profile Faces": 5}, 48),
    "fast": ({"Noodle Radius": 0.009, "Substeps": 10,
              "Profile Faces": 4}, 32),
}

# Keep the existing CLI names as explicit larger metric scenes.
UNIT_PRESETS = {
    "metric": ({"Noodle Length": 0.5, "Noodle Radius": 0.008,
                "Start Height": 0.35, "Fill Diameter": 0.2,
                "Substeps": 17, "Profile Faces": 5}, 48),
    "metric_fast": ({"Noodle Length": 0.5, "Noodle Radius": 0.012,
                     "Start Height": 0.35, "Fill Diameter": 0.2,
                     "Substeps": 12, "Profile Faces": 4}, 48),
}


def preset_names():
    """Every name the CLI and the README may offer, derived from the tables."""
    return tuple(REALTIME_PRESETS) + tuple(UNIT_PRESETS)


def lookup_preset(name):
    """Resolve one preset name to its (overrides, suggested max count)."""
    for table in (REALTIME_PRESETS, UNIT_PRESETS):
        if name in table:
            return table[name]
    raise CliError(f"unknown preset {name!r}; choose from "
                   f"{', '.join(preset_names())}")


def apply_realtime(obj, ng, preset="balanced"):
    """Apply a complete preset; preserve count, seed, and collider assignments."""
    overrides, _ = lookup_preset(preset)
    controlled = {"Noodle Length", "Noodle Radius", "Start Height", "Fill Diameter",
                  "Gravity", "Substeps", "Iterations", "Profile Faces", "Adaptive Substeps"}
    values = {name: default for name, stype, default, lo, hi, subtype in PARAMS
              if name in controlled}
    values.update(overrides)
    for name, value in values.items():
        set_input(obj, ng, name, value)
    obj["noodle_preset"] = preset
    return obj


def bench(preset="default", count=120, frames=90):
    """Report evaluation cost, readback cost and a fall-progress threshold.

    A height threshold measures progress, not physical rest. Timings exclude
    viewport drawing and include surface generation; do not label them live FPS.
    """
    import time
    require_preset(preset)
    count = positive_int(count, "count")
    frames = positive_int(frames, "frames")
    ng = build_group()
    obj = build_object(ng)
    set_input(obj, ng, "Noodle Count", count)
    if preset and preset != "default":
        apply_realtime(obj, ng, preset)

    scene = bpy.context.scene
    dg = bpy.context.evaluated_depsgraph_get

    tops, times, evaluation_times = [], [], []
    for f in range(1, frames + 1):
        t = time.perf_counter()
        scene.frame_set(scene.frame_start + f - 1)
        evaluated = obj.evaluated_get(dg())
        mesh = evaluated.to_mesh()
        evaluation_times.append((time.perf_counter() - t) * 1000.0)
        try:
            if not mesh.vertices:
                raise RuntimeError(f"frame {f} produced no evaluated vertices")
            zs = [v.co.z for v in mesh.vertices]
            tops.append(max(zs))
        finally:
            evaluated.to_mesh_clear()
        times.append((time.perf_counter() - t) * 1000.0)


    steady = sorted(times[5:] if len(times) > 6 else times)
    mean = sum(steady) / len(steady)
    # Drop progress only: a pile can cross this threshold while still moving,
    # and a tall, stable pile may never cross it at all.
    target = tops[0] * 0.25
    hit = next((i + 1 for i, z in enumerate(tops) if z <= target), None)
    progress = f"{sum(times[:hit]) / 1000:.2f}s wall time, frame {hit}" if hit else \
               f"not reached in {frames} frames (tall piles may stay above it)"
    evaluation = evaluation_times[5:] if len(times) > 6 else evaluation_times
    eval_mean = sum(evaluation) / len(evaluation)
    p95 = steady[max(0, ceil(0.95 * len(steady)) - 1)]
    frame_budget = 1000 * scene.render.fps_base / scene.render.fps

    print(f"BENCH preset={preset} count={count} frames={frames}")
    print(f"  total mean {mean:7.1f} ms/frame ({1000.0/mean:5.1f} frames/s headless)"
          f"  median {steady[len(steady)//2]:7.1f} ms  p95 {p95:.1f} ms")
    print(f"  evaluation {eval_mean:.1f} ms; timeline budget {frame_budget:.1f} ms; "
          f"{'WITHIN' if p95 <= frame_budget else 'OVER'} budget at p95 (viewport excluded)")
    print(f"  25% spawn-height threshold: {progress}")
    return mean, hit


def self_check():
    """Smallest thing that fails if the solver is wrong."""
    ng = build_group()
    obj = build_object(ng)
    # Deliberately fat noodles: the point count is derived from the radius, so
    # this is what keeps the check small and quick. Everything below is stated
    # relative to the noodle, not in absolute units, so the check survives a
    # rescale of the scene.
    defaults = {p[0]: p[2] for p in PARAMS}
    length, fill = defaults["Noodle Length"], defaults["Fill Diameter"]
    # length/50 keeps the point count small without pushing segment spacing
    # past the diameter, which is the one configuration the solver is not
    # meant to handle.
    radius, start = length / 50.0, length / 2.0
    set_input(obj, ng, "Noodle Count", 8)
    set_input(obj, ng, "Noodle Radius", radius)
    set_input(obj, ng, "Start Height", start)

    frames = 120
    history = bake(obj, frames, report={1, 40, 80, frames})
    verts, low, high, reach = history[frames]
    top_first = history[1][2]
    peak = max(history[f][2] for f in range(1, frames + 1))
    peak_frame = max(range(1, frames + 1), key=lambda f: history[f][2])

    assert low == low and high == high, "solver produced NaN - it blew up"
    assert low > -1.5 * radius, f"noodles sank through the floor: {low:.4f}"
    # The whole point of the exercise: they must have actually fallen. Stated
    # as a fraction of the spawn height rather than as "half a noodle", which
    # is what this used to say: that bound sat within 7 units of the height a
    # healthy pile actually settles at, so the check failed on a solver that
    # was working. A pile that has not moved still fails here by a mile.
    assert high < top_first * 0.5, f"nothing fell: {top_first:.4f} -> {high:.4f}"
    # Energy injection. Constraint passes move points directly, so a spawn-time
    # overlap resolved over a frame arrives at the velocity readback as motion
    # that was never a legal speed, and the pile rises above where it spawned.
    # A healthy run still rises a little (the coiled spawn uncoils), so this is
    # a ceiling on the failure, not a promise of no rise: measured 1.30x on
    # this scene against 1.70x before the substep count was fixed.
    assert peak < top_first * LAUNCH_LIMIT, (
        f"pile launched: top {peak:.1f} at frame {peak_frame} against a spawn "
        f"height of {top_first:.1f} ({peak / top_first:.2f}x)")
    # An unstable solver flings points to the horizon rather than settling. A
    # noodle landing straight already spans its own length, so allow for that.
    limit = fill / 2 + length * 1.5
    assert reach < limit, f"solver exploded sideways: reach {reach:.4f} > {limit:.4f}"

    print(f"OK  verts={verts}  top {top_first:.1f} -> {high:.1f}"
          f"  peak {peak:.1f} ({peak / top_first:.2f}x at frame {peak_frame})"
          f"  floor {low:.2f}  reach {reach:.1f}")


def recommended_substeps(obj, ng, scene):
    # Include the full possible strand height, including length variation.
    height = get_input(obj, ng, "Start Height") + get_input(obj, ng, "Noodle Length") * (
        1.0 + get_input(obj, ng, "Length Variation"))
    return substeps_for(get_input(obj, ng, "Noodle Radius"),
                        get_input(obj, ng, "Gravity"), height,
                        scene.render.fps / scene.render.fps_base)


def upgrade_solver(context, obj):
    """Replace an older generated graph, retaining inputs and the carrier object.

    Keep the old group datablock intact: other piles may share it. New sockets
    use their defaults. Updating a simulation necessarily clears its old cache.
    """
    modifier = noodle_modifier(obj)
    old = modifier.node_group
    if old.get("noodle_solver_revision", 0) >= SOLVER_REVISION:
        return False
    saved = {s.name: get_input(obj, old, s.name) for s in old.interface.items_tree
             if s.item_type == "SOCKET" and s.in_out == "INPUT"
             and s.name in {p[0] for p in PARAMS}}
    if old.get("noodle_solver_revision", 0) < 5:
        # Revision 5 gave these sockets physical meanings: Friction became a
        # Coulomb coefficient (it was a fraction of sliding speed removed per
        # substep), Stiffness a Young's modulus, Damping extra drag on top of
        # the physical air drag. Old values would mean something else now, so
        # they take the new defaults, or the pile's noodle type's values.
        for name in REVISION_5_PHYSICS:
            saved.pop(name, None)
    ng = build_group()
    reset_simulation(context, obj)
    modifier.node_group = ng
    for name, value in saved.items():
        set_input(obj, ng, name, value)
    if old.get("noodle_solver_revision", 0) < 5:
        # The modifier keeps an old value under a matching socket, so these
        # have to be written explicitly, not just left out of `saved`.
        kind = obj.get("noodle_type")
        if kind in NOODLE_TYPES:
            from .presets import physics_inputs
            fresh = physics_inputs(kind)
        else:
            fresh = {p[0]: p[2] for p in PARAMS}
        for name in REVISION_5_PHYSICS:
            set_input(obj, ng, name, fresh[name])
    obj.update_tag()
    context.scene.frame_set(context.scene.frame_start)
    return True


def reset_simulation(context, obj):
    if context.screen and context.screen.is_animation_playing:
        bpy.ops.screen.animation_cancel(restore_frame=False)
    with context.temp_override(object=obj, active_object=obj):
        bpy.ops.object.simulation_nodes_cache_delete(selected=False)
    obj.update_tag()
    context.scene.frame_set(context.scene.frame_start)


def prepare_scene(context, obj):
    scene = context.scene
    scene.unit_settings.system = "METRIC"
    scene.unit_settings.scale_length = 1.0
    scene.unit_settings.length_unit = "METERS"
    # Frame skipping is inappropriate for a simulation that needs every step.
    scene.sync_mode = "NONE"
    for selected in context.selected_objects:
        selected.select_set(False)
    obj.select_set(True)
    context.view_layer.objects.active = obj
    scene.frame_set(scene.frame_start)


class NOODLE_OT_add(bpy.types.Operator):
    bl_idname = "noodle.add"
    bl_label = "Add Noodles"
    bl_description = "Create an independent noodle simulation at metre scale"
    bl_options = {"REGISTER", "UNDO"}
    noodle_type: bpy.props.EnumProperty(name="Noodle type", items=TYPE_ITEMS, default="udon")

    def execute(self, context):
        from .presets import apply_type
        ng = build_group()
        obj = build_object(ng)
        apply_type(obj, self.noodle_type, context.scene)
        obj.name = NOODLE_TYPES[self.noodle_type]["label"] + " Noodles"
        prepare_scene(context, obj)
        return {"FINISHED"}


class NOODLE_OT_apply_type(bpy.types.Operator):
    bl_idname = "noodle.apply_type"
    bl_label = "Apply Type"
    bl_description = "Apply the selected noodle's dimensions, material and behaviour, then restart; keep colliders"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return noodle_modifier(context.object) is not None

    def execute(self, context):
        from .presets import apply_type
        obj = context.object
        upgrade_solver(context, obj)
        needed = apply_type(obj, context.scene.noodle_box_type, context.scene,
                            context.scene.noodle_box_suggested_count)
        reset_simulation(context, obj)
        if needed > MAX_RUNTIME_SUBSTEPS:
            self.report({"WARNING"}, f"Fine noodles may fall slowly: full-height budget is {needed}, limit is 24")
        return {"FINISHED"}


class NOODLE_OT_demo(bpy.types.Operator):
    bl_idname = "noodle.demo"
    bl_label = "Create Bowl Demo"
    bl_description = "Create a separate Udon bowl scene with a closed collider, camera and studio lights"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        from .demo import create_demo
        create_demo(context)
        self.report({"INFO"}, "Demo ready in a new scene. Play from frame 1")
        return {"FINISHED"}


class NOODLE_OT_update_solver(bpy.types.Operator):
    bl_idname = "noodle.update_solver"
    bl_label = "Update Solver"
    bl_description = "Upgrade this pile's node graph, preserve its settings and restart the simulation"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return noodle_modifier(context.object) is not None

    def execute(self, context):
        upgrade_solver(context, context.object)
        return {"FINISHED"}


class NOODLE_OT_preset(bpy.types.Operator):
    bl_idname = "noodle.preset"
    bl_label = "Apply Noodle Preset"
    bl_description = "Change thickness and solver quality, then restart; preserve count and colliders"
    bl_options = {"REGISTER", "UNDO"}
    preset: bpy.props.StringProperty(default="balanced")

    @classmethod
    def poll(cls, context):
        return noodle_modifier(context.object) is not None

    def execute(self, context):
        obj = context.object
        apply_realtime(obj, noodle_modifier(obj).node_group, self.preset)
        reset_simulation(context, obj)
        self.report({"INFO"}, f"Applied {self.preset}; thickness changed, count preserved")
        return {"FINISHED"}


class NOODLE_OT_reset(bpy.types.Operator):
    bl_idname = "noodle.reset"
    bl_label = "Restart"
    bl_description = "Stop playback, clear this object's simulation cache and return to the start"
    bl_options = {"UNDO"}

    @classmethod
    def poll(cls, context):
        return noodle_modifier(context.object) is not None

    def execute(self, context):
        reset_simulation(context, context.object)
        return {"FINISHED"}


class NOODLE_OT_fit_steps(bpy.types.Operator):
    bl_idname = "noodle.fit_steps"
    bl_label = "Fit Step Budget"
    bl_description = "Calculate substeps from size, drop height, gravity and scene FPS, then restart"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return noodle_modifier(context.object) is not None

    def execute(self, context):
        obj = context.object
        ng = noodle_modifier(obj).node_group
        needed = recommended_substeps(obj, ng, context.scene)
        set_input(obj, ng, "Substeps", min(needed, MAX_RUNTIME_SUBSTEPS))
        reset_simulation(context, obj)
        if needed > MAX_RUNTIME_SUBSTEPS:
            self.report({"WARNING"}, f"Needs {needed} steps; limit is 24. Increase radius or lower drop height")
        else:
            self.report({"INFO"}, f"Step budget set to {needed}")
        return {"FINISHED"}


class NOODLE_OT_frame(bpy.types.Operator):
    bl_idname = "noodle.frame"
    bl_label = "Frame Noodles"
    bl_description = "Fit the selected noodle pile in the viewport"

    @classmethod
    def poll(cls, context):
        return noodle_modifier(context.object) is not None and context.area.type == "VIEW_3D"

    def execute(self, context):
        region = next(r for r in context.area.regions if r.type == "WINDOW")
        with context.temp_override(region=region):
            bpy.ops.view3d.view_selected(use_all_regions=False)
        return {"FINISHED"}


class NOODLE_PT_controls(bpy.types.Panel):
    bl_label = "Noodle Box"
    bl_idname = "NOODLE_PT_controls"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Noodles"

    def draw(self, context):
        layout = self.layout
        obj = context.object
        modifier = noodle_modifier(obj)
        library = layout.box()
        library.label(text="Noodle library", icon="PRESET")
        library.prop(context.scene, "noodle_box_type", text="Type")
        style = NOODLE_TYPES[context.scene.noodle_box_type]
        library.label(text=f"{style['radius'] * 2000:g} mm wide / {style['length'] * 100:g} cm long")
        if modifier is None:
            library.operator("noodle.add", icon="ADD").noodle_type = context.scene.noodle_box_type
            layout.operator("noodle.demo", icon="SCENE_DATA")
            layout.label(text="Select a noodle object to edit it")
            return
        ng = modifier.node_group
        if ng.get("noodle_solver_revision", 0) < SOLVER_REVISION:
            layout.label(text="This pile uses an older solver", icon="INFO")
            layout.operator("noodle.update_solver", icon="FILE_REFRESH")
            return
        library.prop(context.scene, "noodle_box_suggested_count")
        library.operator("noodle.apply_type", icon="CHECKMARK")
        row = layout.row(align=True)
        row.operator("noodle.reset", icon="FILE_REFRESH")
        playing = context.screen and context.screen.is_animation_playing
        row.operator("screen.animation_play", text="Pause" if playing else "Play",
                     icon="PAUSE" if playing else "PLAY")
        layout.operator("noodle.frame", icon="VIEWZOOM")
        layout.label(text="Restart after changing settings", icon="INFO")
        layout.label(text="Metres / local floor at Z = 0")

        row = layout.row(align=True)
        for name in ("fast", "balanced", "quality"):
            row.operator("noodle.preset", text=name.title()).preset = name
        layout.label(text="Speed presets change thickness")
        count = get_input(obj, ng, "Noodle Count")
        radius = get_input(obj, ng, "Noodle Radius")
        length = get_input(obj, ng, "Noodle Length")
        estimate = int(count * max(2, length / (2 * radius) + 1))
        layout.label(text=f"~{estimate:,} points / {2 * radius * 1000:.1f} mm wide")
        layout.label(text=f"{context.scene.render.fps / context.scene.render.fps_base:g} fps timeline")

        if any(abs(v - 1.0) > 1e-5 for v in obj.scale):
            box = layout.box()
            box.alert = True
            box.label(text="Object scale changes the apparent physics", icon="ERROR")
            box.label(text="Use scale 1; edit Length and Radius")
        if context.scene.unit_settings.scale_length != 1.0:
            layout.label(text="Use scene Unit Scale 1 for metre dimensions", icon="ERROR")
        needed = recommended_substeps(obj, ng, context.scene)
        budget = get_input(obj, ng, "Substeps")
        if needed > budget:
            box = layout.box()
            box.label(text=f"Full-height fall needs up to {needed} steps", icon="INFO")
            box.label(text="Current budget may limit fall speed")
        layout.operator("noodle.fit_steps", icon="SETTINGS")

        for title, closed, names in CONTROL_GROUPS:
            header, body = layout.panel("noodle_" + title, default_closed=closed)
            header.label(text=title)
            if body:
                body.use_property_split = True
                body.use_property_decorate = False
                for name in names:
                    label = {"Noodle Count": "Strands", "Noodle Length": "Length",
                             "Noodle Radius": "Radius", "Length Variation": "Variation",
                             "Profile Faces": "Tube sides", "Profile Aspect": "Aspect",
                             "Noodle Material": "Material"}.get(name, name)
                    body.prop(input_property(obj, ng, name), "value", text=label)
        layout.separator()
        layout.operator("noodle.add", text="Add Another Pile", icon="ADD").noodle_type = context.scene.noodle_box_type
        layout.operator("noodle.demo", icon="SCENE_DATA")


UI_CLASSES = (NOODLE_OT_add, NOODLE_OT_apply_type, NOODLE_OT_demo, NOODLE_OT_update_solver,
              NOODLE_OT_preset, NOODLE_OT_reset,
              NOODLE_OT_fit_steps, NOODLE_OT_frame, NOODLE_PT_controls)


def register():
    if bpy.app.version < (5, 2, 0):
        raise RuntimeError("Noodle Box requires Blender 5.2 or newer")
    # Run Script can be used repeatedly in the same Blender session.
    for cls in reversed(UI_CLASSES):
        old = getattr(bpy.types, cls.__name__, None)
        if old is not None:
            bpy.utils.unregister_class(old)
    for cls in UI_CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.Scene.noodle_box_type = bpy.props.EnumProperty(
        name="Noodle type", items=TYPE_ITEMS, default="udon")
    bpy.types.Scene.noodle_box_suggested_count = bpy.props.BoolProperty(
        name="Use suggested strand count", default=True,
        description="Use the type's preview count; disable to keep the current count")


def unregister():
    for cls in reversed(UI_CLASSES):
        old = getattr(bpy.types, cls.__name__, None)
        if old is not None:
            bpy.utils.unregister_class(old)
    for name in ("noodle_box_type", "noodle_box_suggested_count"):
        if hasattr(bpy.types.Scene, name):
            delattr(bpy.types.Scene, name)


def main():
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    mode = parse_mode_args(argv)
    if bpy.app.version < (5, 2, 0):
        raise CliError("Noodle Physics requires Blender 5.2 or newer")
    if mode[0] == "check":
        self_check()
        return
    if mode[0] == "bench":
        _, preset, count, frames = mode
        bench(preset, count, frames)
        return

    register()
    if mode[0] == "build":
        existing = bpy.context.object if noodle_modifier(bpy.context.object) else next(
            (obj for obj in bpy.context.scene.objects if noodle_modifier(obj)), None)
        if existing:
            prepare_scene(bpy.context, existing)
            if upgrade_solver(bpy.context, existing):
                print("Updated noodle solver; settings preserved and simulation restarted.")
            print("Noodle Physics ready. Open Viewport > Sidebar > Noodles.")
            return
    ng = build_group()
    obj = build_object(ng)
    # --realtime [preset] on the command line, or the PRESET constant at the top
    # of this file when there is no command line - which is the case for the
    # Scripting workspace's Run Script button, and the usual way this is used.
    preset = PRESET
    if mode[0] == "realtime":
        _, preset = mode
    if preset:
        apply_realtime(obj, ng, preset)
        _, maxn = lookup_preset(preset)
        set_input(obj, ng, "Noodle Count", maxn)
        prepare_scene(bpy.context, obj)
        overrides, maxn = lookup_preset(preset)
        print(f"Built '{NG_NAME}' with preset '{preset}': {overrides}. "
              f"Open Viewport > Sidebar > Noodles. Play from the start frame.")
        return
    prepare_scene(bpy.context, obj)
    print(f"Built '{NG_NAME}'. Open Viewport > Sidebar > Noodles.")



if __name__ == "__main__":
    try:
        main()
    except CliError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
