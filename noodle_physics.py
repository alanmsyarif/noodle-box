"""Noodle Physics for Blender 5.2+.

A position-based noodle solver built entirely in Geometry Nodes. Coordinates
are metres, time is seconds, and gravity defaults to 9.81 m/s squared. Run this
file in Blender, then open Viewport > Sidebar > Noodles. Existing objects are
preserved; Add Noodles creates another independent simulation.

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

NG_NAME = "Noodle Physics"
OBJ_NAME = "NoodlePhysics"
MAT_NAME = "Noodle"

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
SCRATCH_ATTRS = (ATTR_PREV, ATTR_NEAR, ATTR_CDIST,
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


# Turn rate of the spawn coil. A noodle held upright would buckle instantly, so
# it is spawned already coiled rather than as a rigid vertical rod.
COIL_SCALE = 0.18

# Compliance at Stiffness 0.5. Inverse masses here are 1 rather than 1/kg, so
# alpha/dt^2 comes out dimensionless and this number does not depend on the
# scene's length unit - only on how large the noodle is relative to gravity.
# Lowering it "for scale" once made the whole Stiffness range rigid, which
# showed up as the knob having no measurable effect at all.
BEND_COMPLIANCE = 4.0e-5

# Contact skin, as a fraction of the radius. Anything a point comes this close
# to counts as touching, for friction purposes.
CONTACT_SKIN = 1.2


# Under-relaxation for the bend solve. Points are solved in parallel, so each
# one receives corrections from its own triple and from both neighbouring
# triples at once; at full strength that overshoots and diverges - at
# Stiffness 0.9 the mean turn angle came out at 89 degrees, near-random, and
# raising stiffness made it worse rather than better. Scaling the multiplier
# step slows convergence without moving the solution it converges to.
JACOBI_RELAX = 0.3

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
    ("Fill Diameter",   "NodeSocketFloat", 0.2,    0.001, 100.0,  "DISTANCE"),
    # Object sockets carry no default or range, hence the Nones.
    ("Collider",        "NodeSocketObject", None,  None,  None,   None),
    ("Collider Collection", "NodeSocketCollection", None, None, None, None),
    ("Collider Margin", "NodeSocketFloat", 0.0,    0.0,   1e6,    "DISTANCE"),
    ("Collider Friction","NodeSocketFloat", 0.5,   0.0,   1.0,    "FACTOR"),
    ("Collider Stickiness","NodeSocketFloat", 0.0,  0.0,   1.0,    "FACTOR"),
    ("Sticky Collider", "NodeSocketCollection", None, None, None,   None),
    ("Sticky Collider Grip","NodeSocketFloat", 0.8,  0.0,   1.0,    "FACTOR"),
    ("Start Height",    "NodeSocketFloat", 0.12,   0.0,   100.0,  "DISTANCE"),
    ("Gravity",         "NodeSocketFloat", 9.81,   0.0,   1e6,    None),
    ("Damping",         "NodeSocketFloat", 0.03,   0.0,   1.0,    "FACTOR"),
    ("Stiffness",       "NodeSocketFloat", 0.5,    0.0,   1.0,    "FACTOR"),
    ("Substeps",        "NodeSocketInt",   14,     1,     MAX_RUNTIME_SUBSTEPS, None),
    ("Adaptive Substeps", "NodeSocketBool", True,  None,  None,   None),
    ("Iterations",      "NodeSocketInt",   2,      1,     MAX_RUNTIME_ITERATIONS, None),
    ("Friction",        "NodeSocketFloat", 0.9,    0.0,   1.0,    "FACTOR"),
    ("Self Collision",  "NodeSocketFloat", 1.0,    0.0,   1.0,    "FACTOR"),
    ("Cohesion",        "NodeSocketFloat", 0.15,   0.0,   1.0,    "FACTOR"),
]


# Shared ordering for the modifier and sidebar; collider details start folded.
CONTROL_GROUPS = (
    ("Noodles", False, ("Noodle Count", "Noodle Length", "Noodle Radius",
                        "Length Variation", "Profile Faces")),
    ("Spawn", False, ("Fill Diameter", "Start Height", "Spawn Coil", "Seed")),
    ("Solver", False, ("Adaptive Substeps", "Substeps", "Iterations", "Gravity",
                       "Damping", "Stiffness", "Self Collision", "Friction", "Cohesion")),
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
    "Fill Diameter": "Diameter of the spawn disc in metres",
    "Start Height": "Spawn origin above the local floor in metres; coils can extend below this",
    "Spawn Coil": "1 starts loose coils; 0 starts upright strands",
    "Seed": "Repeatable random layout and strand lengths",
    "Gravity": "Downward acceleration in metres per second squared; Earth gravity is 9.81",
    "Damping": "Air drag fraction per 1/24 second, adjusted for scene FPS and substeps",
    "Stiffness": "XPBD bending resistance: 0 flexible, 1 rigid",
    "Adaptive Substeps": "Use current maximum speed plus gravity to spend fewer substeps on slow frames",
    "Substeps": "Maximum steps per frame (1 to 24). Too few clamp fall speed. Use Fit Step Budget after changing scale or FPS",
    "Iterations": "Constraint passes per substep (1 to 12); start with 2",
    "Self Collision": "Noodle contact strength; 0 allows strands to pass through one another",
    "Friction": "Sideways contact friction against noodles and the floor",
    "Cohesion": "Attraction between nearby strands; 0 is dry, higher values clump",
    "Collider": "Closed mesh collider. Add thickness to open bowls with Solidify. The local floor remains active",
    "Collider Collection": "Additional closed meshes, joined into one distance field per frame",
    "Collider Margin": "Extra clearance from collider surfaces in metres",
    "Collider Friction": "Sideways friction against collider surfaces",
    "Collider Stickiness": "Attraction to nearby collider surfaces",
    "Sticky Collider": "Second collider collection with independent grip; adds a second distance field",
    "Sticky Collider Grip": "Attraction to the sticky collection surfaces",
}


def ensure_material():
    """One shared noodle material, reused if it already exists.

    Every strand is shaded slightly differently, driven by the np_id attribute
    the solver leaves on the mesh. Uniform pasta reads as plastic: real cooked
    spaghetti varies in tone and in how wet each strand is, and with a hundred
    of them touching, that variation is most of what sells it.
    """
    mat = bpy.data.materials.get(MAT_NAME)
    if mat:
        return mat
    mat = bpy.data.materials.new(MAT_NAME)  # already node-backed in 5.x
    tree = mat.node_tree
    bsdf = tree.nodes.get("Principled BSDF")
    if not bsdf:
        return mat

    def put(name, value):
        """Socket names move between releases; set only what exists."""
        if name in bsdf.inputs:
            bsdf.inputs[name].default_value = value

    put("Base Color", (0.94, 0.80, 0.44, 1.0))
    put("Roughness", 0.35)
    put("Subsurface Weight", 0.22)
    put("Subsurface Radius", (0.0009, 0.00055, 0.00025))
    put("Subsurface Scale", 1.0)
    put("IOR", 1.42)

    # np_id -> white noise -> one uncorrelated random per strand.
    attr = tree.nodes.new("ShaderNodeAttribute")
    attr.attribute_type = "GEOMETRY"
    attr.attribute_name = ATTR_ID
    attr.location = (-900, 0)
    noise = tree.nodes.new("ShaderNodeTexWhiteNoise")
    noise.noise_dimensions = "1D"
    noise.location = (-700, 0)
    tree.links.new(attr.outputs["Fac"], noise.inputs["W"])

    ramp = tree.nodes.new("ShaderNodeValToRGB")
    ramp.location = (-500, 120)
    ramp.color_ramp.elements[0].color = (0.86, 0.70, 0.38, 1.0)   # paler, drier
    ramp.color_ramp.elements[1].color = (0.98, 0.87, 0.55, 1.0)   # richer, wetter
    tree.links.new(noise.outputs["Value"], ramp.inputs["Fac"])
    if "Base Color" in bsdf.inputs:
        tree.links.new(ramp.outputs["Color"], bsdf.inputs["Base Color"])

    # Wetter strands are glossier. Same random, remapped.
    rough = tree.nodes.new("ShaderNodeMapRange")
    rough.location = (-500, -160)
    rough.inputs["From Min"].default_value = 0.0
    rough.inputs["From Max"].default_value = 1.0
    rough.inputs["To Min"].default_value = 0.22
    rough.inputs["To Max"].default_value = 0.45
    tree.links.new(noise.outputs["Value"], rough.inputs["Value"])
    if "Roughness" in bsdf.inputs:
        tree.links.new(rough.outputs["Result"], bsdf.inputs["Roughness"])
    return mat


def build_group():
    """Build (or rebuild) the Noodle Physics node group and return it."""
    ng = bpy.data.node_groups.new(NG_NAME, "GeometryNodeTree")
    ng["noodle_physics_version"] = 2
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

    # Spawn shape. A rigid vertical rod is not what a cooked noodle does: held
    # up by one end it buckles immediately. So the default is an already-coiled
    # noodle, built by the same accumulated-noise walk the procedural generator
    # uses - correlated turns rather than jitter. Spawn Coil 0 gives back the
    # old straight rod, which still needs a nudge to break its symmetry or it
    # falls perfectly vertically and stacks like a pole.
    nid_setup = named(ATTR_ID, "INT", 13, 3)
    len_setup = named(ATTR_LEN, "FLOAT", 13, 5)
    rest_setup = math("DIVIDE", len_setup,
                      math("MAXIMUM",
                           math("SUBTRACT",
                                nd("GeometryNodeSplineLength", 13, 6).outputs["Point Count"],
                                1.0, 14, 6), 1.0, 15, 6), 14, 5)
    spline_i_setup = nd("GeometryNodeSplineParameter", 13, 4).outputs["Index"]
    coil_noise = nd("ShaderNodeTexNoise", 15, 3, noise_dimensions="3D")
    plug(coil_noise.inputs["Vector"], xyz(
        math("MULTIPLY", spline_i_setup, COIL_SCALE, 14, 3),
        math("MULTIPLY", nid_setup, NOODLE_SPREAD, 14, 4),
        0.0, 14, 6))
    plug(coil_noise.inputs["Scale"], 1.0)
    cx, cy, cz = sep(vmath("SUBTRACT", coil_noise.outputs["Color"], (0.5, 0.5, 0.5), 16, 3), 17, 3)
    # Flatten the walk so a spawned noodle is a loose nest, not a tangled ball.
    coil_step = vmath("SCALE",
                      vmath("NORMALIZE", xyz(cx, cy, math("MULTIPLY", cz, 0.35, 18, 4), 18, 3),
                            None, 19, 3),
                      rest_setup, 20, 3)
    coil_acc = nd("GeometryNodeAccumulateField", 21, 3,
                  data_type="FLOAT_VECTOR", domain="POINT")
    plug(coil_acc.inputs["Value"], coil_step)
    plug(coil_acc.inputs["Group ID"], nid_setup)

    rod = xyz(math("MULTIPLY", cx, P["Noodle Radius"], 17, 6),
              math("MULTIPLY", cy, P["Noodle Radius"], 17, 7),
              math("MULTIPLY", spline_i_setup, rest_setup, 17, 8), 18, 7)
    spawn = mix_vec(P["Spawn Coil"], rod, coil_acc.outputs["Trailing"], 22, 4)

    # Position is rebuilt from scratch rather than offset, so the instanced
    # line's own shape does not survive into the spawn.
    start = nd("GeometryNodeSetPosition", 24, 1)
    plug(start.inputs["Geometry"], resample.outputs["Curve"])
    plug(start.inputs["Position"],
         vmath("ADD",
               vmath("ADD", named(ATTR_ROOT, "FLOAT_VECTOR", 22, 6), spawn, 23, 5),
               xyz(0.0, 0.0, P["Start Height"], 22, 7), 25, 4))
    geo = store(start.outputs["Geometry"], ATTR_VEL, (0.0, 0.0, 0.0), "FLOAT_VECTOR", 26, 1)

    # ----------------------------------------------------------- simulation
    sim_out = nd("GeometryNodeSimulationOutput", 40, 1)
    sim_in = nd("GeometryNodeSimulationInput", 20, 1)
    sim_in.pair_with_output(sim_out)
    plug(sim_in.inputs["Geometry"], geo)
    frame_dt = sim_in.outputs["Delta Time"]
    state = sim_in.outputs["Geometry"]

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
    adaptive = math("MINIMUM", runtime_substeps,
                    math("MAXIMUM", needed, 2.0, 19, 12), 20, 10)
    step_switch = nd("GeometryNodeSwitch", 20, 11, input_type="INT")
    plug(step_switch.inputs["Switch"], P["Adaptive Substeps"])
    plug(step_switch.inputs["False"], runtime_substeps)
    plug(step_switch.inputs["True"], adaptive)
    runtime_substeps = step_switch.outputs["Output"]
    plug(sub_in.inputs["Iterations"], runtime_substeps)
    plug(sub_in.inputs["Geometry"], state)
    state = sub_in.outputs["Geometry"]
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

    # Keep this substep's starting positions; velocity is read back out of
    # them after the constraints have had their say.
    state = store(state, ATTR_PREV, position, "FLOAT_VECTOR", 21, 1)

    velocity = named(ATTR_VEL, "FLOAT_VECTOR", 21, 3)
    velocity = vmath("ADD", velocity,
                     xyz(0.0, 0.0, math("MULTIPLY", math("MULTIPLY", P["Gravity"], -1.0, 22, 4), dt, 23, 4), 23, 5),
                     24, 3)
    # Drag is calibrated at 24 fps and integrated over seconds at any scene FPS.
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

    # Nearest neighbour once per substep, not once per constraint iteration.
    # Re-querying every iteration sounds better and measured identically -
    # 37.9% and 47.7% of points overlapping, against 37.9% and 47.8% - for
    # 12.5x the time (186s against 15s per 150 frames). The substep loop
    # refreshes the query on every substep.
    nearest = nd("GeometryNodeIndexOfNearest", 33, 3)
    plug(nearest.inputs["Position"], position)
    state = store(state, ATTR_NEAR, nearest.outputs["Index"], "INT", 34, 1)

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
    state = store(state, ATTR_BLAM, 0.0, "FLOAT", 38, 1)

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

    # -- keep consecutive points one rest length apart.
    # Jacobi style: both ends of a segment move half the error. Gauss-Seidel
    # would converge faster but the points are solved in parallel, so it is
    # not available; more Iterations is the knob instead.
    spline_i = nd("GeometryNodeSplineParameter", 35, 4).outputs["Index"]
    point_count = nd("GeometryNodeSplineLength", 35, 5).outputs["Point Count"]
    has_prev = math("GREATER_THAN", spline_i, 0.5, 36, 4)
    has_next = math("LESS_THAN", spline_i, math("SUBTRACT", point_count, 1.5, 36, 5), 37, 5)

    prev_p = at_index(position, math("SUBTRACT", index, 1, 36, 6), "FLOAT_VECTOR", 37, 6)
    next_p = at_index(position, math("ADD", index, 1, 36, 9), "FLOAT_VECTOR", 37, 9)

    def pull(neighbour_pos, gate, col, row):
        """Half of the correction that puts this point at rest length."""
        delta = vmath("SUBTRACT", position, neighbour_pos, col, row)
        length = vmath("LENGTH", delta, None, col + 1, row)
        scale = math("MULTIPLY",
                     math("DIVIDE", math("SUBTRACT", rest, length, col + 2, row),
                          math("MAXIMUM", length, 1e-6, col + 2, row + 1), col + 3, row),
                     math("MULTIPLY", 0.5, gate, col + 3, row + 1), col + 4, row)
        return vmath("SCALE", delta, scale, col + 5, row)

    correction = vmath("ADD", pull(prev_p, has_prev, 38, 6),
                       pull(next_p, has_next, 38, 9), 44, 6)
    solve = set_position(solve, vmath("ADD", position, correction, 45, 6), 46, 1)

    # -- bending stiffness.
    # Distance constraints alone make a *chain*, not a noodle: consecutive
    # segments may meet at any angle, so the curve kinks hard and Curve to
    # Mesh pinches the tube shut at every reversal, which renders as a heap of
    # short stubs. Real spaghetti resists bending.
    #
    # It is solved as XPBD. The constraint is C = |p - midpoint of neighbours|,
    # whose gradients are n at the centre and -n/2 at each neighbour, summing
    # to zero - so the correction conserves momentum. That matters: a plain
    # drag toward the midpoint moves mass with nothing moving back and injects
    # energy, which sent the pile to a height of 929 and a radius of 1689,
    # worse the stiffer it got.
    #
    # What XPBD adds over scaling that correction by a stiffness factor is the
    # compliance term. A plain factor is applied once per iteration, so the
    # rigidity you actually get depends on Iterations and Substeps - retune the
    # solver and the noodles change stiffness. Here compliance alpha carries
    # the physical units, alpha/dt^2 makes it step-size independent, and the
    # multiplier lambda accumulates across iterations so the constraint
    # converges to the same equilibrium whatever the counts are.
    #
    # The rope's own length constraint needs none of this: it is already XPBD
    # at zero compliance, which is exactly the rigid half-correction above.
    prev2_p = at_index(position, math("SUBTRACT", index, 2, 47, 7), "FLOAT_VECTOR", 48, 7)
    next2_p = at_index(position, math("ADD", index, 2, 47, 11), "FLOAT_VECTOR", 48, 11)

    def offset(left, middle, right, col, row):
        """How far `middle` sits off the line between its two neighbours."""
        return vmath("SUBTRACT", middle,
                     vmath("SCALE", vmath("ADD", left, right, col, row), 0.5, col + 1, row),
                     col + 2, row)

    # Each triple only exists where both of its outer points do.
    has_prev2 = math("GREATER_THAN", spline_i, 1.5, 49, 7)
    has_next2 = math("LESS_THAN", spline_i, math("SUBTRACT", point_count, 2.5, 49, 11), 50, 11)
    interior = math("MULTIPLY", has_prev, has_next, 49, 9)

    own = offset(prev_p, position, next_p, 50, 9)
    own_dir = vmath("NORMALIZE", own, None, 53, 9)

    # Stiffness 1 means zero compliance, i.e. as rigid as the solver gets.
    alpha = math("MULTIPLY", BEND_COMPLIANCE,
                 math("SUBTRACT",
                      math("DIVIDE", 1.0, math("MAXIMUM", P["Stiffness"], 1e-4, 47, 13), 48, 13),
                      1.0, 49, 13), 50, 13)
    alpha_dt = math("DIVIDE", alpha, math("MAXIMUM", math("MULTIPLY", dt, dt, 48, 14),
                                          1e-12, 49, 14), 51, 13)
    lam = named(ATTR_BLAM, "FLOAT", 47, 15)
    # Sum of w|grad C|^2 over the three points: 1 + 1/4 + 1/4.
    delta_lam = math("MULTIPLY",
                     math("DIVIDE",
                          math("SUBTRACT",
                               math("MULTIPLY", vmath("LENGTH", own, None, 53, 10), -1.0, 54, 10),
                               math("MULTIPLY", alpha_dt, lam, 52, 15), 55, 10),
                          math("ADD", 1.5, alpha_dt, 52, 14), 56, 10),
                     math("MULTIPLY", interior, JACOBI_RELAX, 57, 10), 57, 11)
    # Parked on the geometry so the position update and the lambda update
    # below both read the same value, computed from pre-update positions.
    solve = store(solve, ATTR_BDLAM, delta_lam, "FLOAT", 58, 1)

    dlam = named(ATTR_BDLAM, "FLOAT", 59, 9)
    bend = vmath("ADD",
                 vmath("SCALE", own_dir, dlam, 60, 9),
                 vmath("ADD",
                       vmath("SCALE",
                             vmath("NORMALIZE", offset(prev2_p, prev_p, position, 59, 7),
                                   None, 62, 7),
                             math("MULTIPLY", math("MULTIPLY", -0.5, has_prev2, 60, 7),
                                  at_index(dlam, math("SUBTRACT", index, 1, 60, 6),
                                           "FLOAT", 61, 6), 63, 7), 64, 7),
                       vmath("SCALE",
                             vmath("NORMALIZE", offset(position, next_p, next2_p, 59, 11),
                                   None, 62, 11),
                             math("MULTIPLY", math("MULTIPLY", -0.5, has_next2, 60, 11),
                                  at_index(dlam, math("ADD", index, 1, 60, 12),
                                           "FLOAT", 61, 12), 63, 11), 64, 11),
                       65, 10),
                 66, 9)
    solve = set_position(solve, vmath("ADD", position, bend, 67, 9), 68, 1)
    solve = store(solve, ATTR_BLAM, math("ADD", lam, dlam, 69, 9), "FLOAT", 70, 1)

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
                         math("ADD", math("DIVIDE", diameter, rest, col + 1, row + 2),
                              1.0, col + 2, row + 2),
                         col + 3, row + 1)
        return math("MAXIMUM", other_noodle, far_along, col + 5, row)

    near_index = named(ATTR_NEAR, "INT", 46, 4)
    near_pos = at_index(position, near_index, "FLOAT_VECTOR", 47, 4)
    away = vmath("SUBTRACT", position, near_pos, 48, 4)
    gap = vmath("LENGTH", away, None, 49, 4)
    off_chain = contact_allowed(near_index, 46, 6)
    overlap = math("MAXIMUM", math("SUBTRACT", diameter, gap, 50, 4), 0.0, 51, 4)
    # Cohesion: cooked pasta clings to itself, and a pile of it is not a pile
    # of dry sticks. Just past contact the same pair is pulled back together
    # instead of ignored, which costs almost nothing - the neighbour query it
    # needs is already the most expensive thing in the frame and is paid for
    # by the push above. Negative overlap, so one expression covers both.
    reach = math("MULTIPLY", diameter, COHESION_REACH, 50, 7)
    cling = math("MULTIPLY",
                 math("MULTIPLY",
                      math("SUBTRACT", diameter, gap, 51, 7),   # negative outside contact
                      math("LESS_THAN", gap, reach, 52, 7), 53, 7),
                 P["Cohesion"], 54, 7)
    # Only the separating half is cohesion; the overlapping half is the push.
    separation = math("MINIMUM", cling, 0.0, 55, 7)
    # Cap how far one pass may move a point. A coiled noodle crosses itself,
    # so noodles spawn already overlapped, and an unclamped push turns that
    # first-frame overlap into launch velocity - the pile rose instead of
    # falling. Bounded to a fraction of a segment, the same overlap resolves
    # over a few substeps instead of exploding.
    limit = math("MULTIPLY", rest, PUSH_LIMIT, 51, 5)
    amount = math("MINIMUM",
                  math("MAXIMUM",
                       math("MULTIPLY", math("ADD", overlap, separation, 52, 4), 0.5, 53, 4),
                       math("MULTIPLY", limit, -1.0, 52, 5), 53, 5),
                  limit, 54, 5)
    push = vmath("SCALE", vmath("NORMALIZE", away, None, 50, 5),
                 math("MULTIPLY", amount,
                      math("MULTIPLY", off_chain, P["Self Collision"], 52, 6), 54, 4),
                 55, 4)
    solve = set_position(solve, vmath("ADD", position, push, 56, 4), 57, 1)

    # -- floor. A hard constraint, so it belongs inside the loop.
    px, py, pz = sep(position, 56, 6)
    solve = set_position(solve,
                         xyz(px, py, math("MAXIMUM", pz, P["Noodle Radius"], 57, 6), 58, 6),
                         59, 1)

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

    def collider_pass(solve, grid, stickiness, present, col, row):
        """Push out of one field, cling to it, and report the distance."""
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
        solve = set_position(
            solve, vmath("ADD", position,
                         vmath("SCALE", vmath("NORMALIZE", grad_i, None, col + 7, row + 2),
                               clear, col + 8, row + 2), col + 9, row + 2), col + 10, 1)
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
        return geometry_switch.outputs["Output"], distance_switch.outputs["Output"]

    solve, d_plain = collider_pass(solve, grid, P["Collider Stickiness"], has_plain, 55, 8)
    solve, d_stick = collider_pass(solve, sticky_grid, P["Sticky Collider Grip"], has_sticky, 55, 16)
    # Friction only cares which surface is nearest, not which set it came from.
    solve = store(solve, ATTR_CDIST, math("MINIMUM", d_plain, d_stick, 66, 12),
                  "FLOAT", 67, 12)


    plug(rep_out.inputs["Geometry"], solve)
    state = rep_out.outputs["Geometry"]

    # ---------------------------------------------- velocity, then friction
    # Velocity is whatever motion survived the constraints. That is also where
    # the energy problem lives: resolving a penetration moves a point, and
    # that motion becomes speed on the next frame, so a position-based solver
    # quietly pumps energy into the pile. Left alone it spread the heap to a
    # radius of 743 from a 200-unit spawn disc.
    #
    # Friction therefore acts here, on velocity, not on position. A positional
    # version has to fight the constraints to do its job; damping the velocity
    # of contacting points removes the injected energy instead, and cannot
    # stretch a noodle no matter how strong it is set.
    #
    # It has to cover noodle-against-noodle contact, not just the ground: only
    # the bottom layer is ever "on the floor", so a ground-only rule lets
    # everything above it flow like a liquid.
    prev_pos = named(ATTR_PREV, "FLOAT_VECTOR", 64, 3)
    velocity_out = vmath("SCALE", vmath("SUBTRACT", position, prev_pos, 65, 3),
                         math("DIVIDE", 1.0, math("MAXIMUM", dt, 1e-6, 65, 4), 66, 4), 67, 3)

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
    touch_gap = vmath("LENGTH",
                      vmath("SUBTRACT", position,
                            at_index(position, near_after, "FLOAT_VECTOR", 65, 9), 66, 9),
                      None, 67, 9)
    touching = math("MULTIPLY",
                    math("LESS_THAN", touch_gap,
                         math("MULTIPLY", diameter, 1.1, 65, 10), 68, 10),
                    contact_allowed(near_after, 65, 11),
                    69, 10)
    # Resting on the collider counts as contact too, or noodles would keep
    # sliding around the inside of a bowl instead of settling in it.
    on_collider = math("LESS_THAN", named(ATTR_CDIST, "FLOAT", 64, 11),
                       math("MULTIPLY", P["Noodle Radius"], CONTACT_SKIN, 65, 12), 67, 11)
    # Collider surfaces get their own friction: a ceramic plate is not as
    # grippy as wet pasta on wet pasta, and wanting the two to differ is the
    # usual reason a pile slides off a bowl or refuses to spread on one.
    keep = math("SUBTRACT", 1.0,
                math("MAXIMUM",
                     math("MULTIPLY", math("MAXIMUM", on_floor, touching, 70, 9),
                          P["Friction"], 71, 9),
                     math("MULTIPLY", on_collider, P["Collider Friction"], 71, 10),
                     72, 9),
                73, 9)
    # Sideways only. Damping the vertical too would stop the heap settling
    # into its own gaps, and gravity is already resisted by the contact.
    vx, vy, vz = sep(velocity_out, 68, 3)
    state = store(state, ATTR_VEL,
                  xyz(math("MULTIPLY", vx, keep, 73, 3),
                      math("MULTIPLY", vy, keep, 73, 4), vz, 74, 3),
                  "FLOAT_VECTOR", 75, 1)

    plug(sub_out.inputs["Geometry"], state)
    plug(sim_out.inputs["Geometry"], sub_out.outputs["Geometry"])

    # -------------------------------------------------------------- surface
    circle = nd("GeometryNodeCurvePrimitiveCircle", 69, 3, mode="RADIUS")
    plug(circle.inputs["Resolution"], P["Profile Faces"])
    plug(circle.inputs["Radius"], P["Noodle Radius"])
    to_mesh = nd("GeometryNodeCurveToMesh", 70, 1)
    # Drop the solver's working data before the surface. Curve to Mesh
    # interpolates every attribute it is handed onto every vertex of every
    # tube, so leaving nine scratch attributes attached meant carrying and
    # rebuilding them across ~145k vertices a frame for nothing. np_id and
    # np_vel survive on purpose - see the shader notes in the README.
    clean = sim_out.outputs["Geometry"]
    for i, attr in enumerate(SCRATCH_ATTRS):
        strip = nd("GeometryNodeRemoveAttribute", 69 + i, 1)
        plug(strip.inputs["Geometry"], clean)
        plug(strip.inputs["Name"], attr)
        clean = strip.outputs["Geometry"]
    plug(to_mesh.inputs["Curve"], clean)
    plug(to_mesh.inputs["Profile Curve"], circle.outputs["Curve"])
    plug(to_mesh.inputs["Fill Caps"], True)
    smooth = nd("GeometryNodeSetShadeSmooth", 71, 1, domain="FACE")
    plug(smooth.inputs["Mesh"], to_mesh.outputs["Mesh"])
    plug(smooth.inputs["Shade Smooth"], True)
    set_mat = nd("GeometryNodeSetMaterial", 72, 1)
    plug(set_mat.inputs["Geometry"], smooth.outputs["Mesh"])
    plug(set_mat.inputs["Material"], ensure_material())
    plug(nd("NodeGroupOutput", 73, 1).inputs["Geometry"], set_mat.outputs["Geometry"])
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

    def execute(self, context):
        ng = build_group()
        obj = build_object(ng)
        apply_realtime(obj, ng, "balanced")
        prepare_scene(context, obj)
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
    bl_label = "Noodle Physics"
    bl_idname = "NOODLE_PT_controls"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Noodles"

    def draw(self, context):
        layout = self.layout
        obj = context.object
        modifier = noodle_modifier(obj)
        if modifier is None:
            layout.label(text="Tabletop noodle simulation", icon="PHYSICS")
            layout.label(text="Metres · real gravity · floor at Z = 0")
            layout.operator("noodle.add", icon="ADD")
            layout.label(text="Select a noodle object to edit it")
            return
        ng = modifier.node_group
        row = layout.row(align=True)
        row.operator("noodle.reset", icon="FILE_REFRESH")
        playing = context.screen and context.screen.is_animation_playing
        row.operator("screen.animation_play", text="Pause" if playing else "Play",
                     icon="PAUSE" if playing else "PLAY")
        layout.operator("noodle.frame", icon="VIEWZOOM")
        layout.label(text="Restart after editing simulation settings", icon="INFO")
        layout.label(text="Lengths in metres · local floor at Z = 0")

        row = layout.row(align=True)
        for name in ("fast", "balanced", "quality"):
            row.operator("noodle.preset", text=name.title()).preset = name
        layout.label(text="Presets change thickness; keep strand count")
        count = get_input(obj, ng, "Noodle Count")
        radius = get_input(obj, ng, "Noodle Radius")
        length = get_input(obj, ng, "Noodle Length")
        estimate = int(count * max(2, length / (2 * radius) + 1))
        layout.label(text=f"~{estimate:,} points · {2 * radius * 1000:.1f} mm thick")
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
                    body.prop(input_property(obj, ng, name), "value", text=name)
        layout.separator()
        layout.operator("noodle.add", text="Add Another Pile", icon="ADD")


UI_CLASSES = (NOODLE_OT_add, NOODLE_OT_preset, NOODLE_OT_reset,
              NOODLE_OT_fit_steps, NOODLE_OT_frame, NOODLE_PT_controls)


def register():
    # Run Script can be used repeatedly in the same Blender session.
    for cls in reversed(UI_CLASSES):
        old = getattr(bpy.types, cls.__name__, None)
        if old is not None:
            bpy.utils.unregister_class(old)
    for cls in UI_CLASSES:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(UI_CLASSES):
        old = getattr(bpy.types, cls.__name__, None)
        if old is not None:
            bpy.utils.unregister_class(old)


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
