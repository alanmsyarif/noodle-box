"""Simulated noodle pile for Blender 5.2 - geometry nodes, real physics.

Two cheaper approaches exist for the same picture, and this file is the one that
gives up neither of them:

  rigid-body sim    a cylinder per segment, minutes to bake, and the geometry is
                    dead afterwards - the simulation cannot be re-tuned
  procedural curves instant, but the noodles interpenetrate: they are a shape,
                    not a pile
  this file         a position-based-dynamics rope solver written in geometry
                    nodes: noodles fall, buckle, collide with each other and
                    heap up, and every parameter stays editable

Solver, inside a Simulation Zone, run Substeps times per frame:

    remember previous position
    v += gravity * dt ;  v *= this substep's share of damping
    clamp |v| to one segment per substep
    p += v * dt
    find each point's nearest neighbour
    repeat Iterations times:
        pull consecutive points back to the rest segment length
        straighten each corner toward its neighbours (bending stiffness)
        push apart any pair closer than one noodle diameter, and pull
            together any pair just past it (cohesion)
        lift anything below the floor, then walk up the collider's
            distance field until one radius clear of it
    v = (p - previous p) / dt, then friction damps contacts sideways

That second-to-last line is what makes it position-based: constraints move
points directly, and velocity is read back out of the motion that actually
happened, so nothing fights the solver.

The one rule worth knowing: point spacing must *equal* the noodle diameter,
and a point may never travel further than that in one step. Both are enforced
rather than documented - the count is derived per noodle from its own length,
and the frame is cut into Substeps - because breaking either makes noodles
slide through each other in a way that looks like the solver is not running.
Wider than a diameter and crossings fall between samples; narrower and a
point's own chain neighbour always wins the nearest-neighbour query, so no
contact is ever resolved. See the note at the resample for the measurements.

That makes Noodle Radius the performance dial: thicker noodles need fewer
points, so they simulate faster *and* collide slightly better. Blender 5.2's
own XPBD Solver node is faster still, but it has no contact constraint - only
EdgeLength, CrossEdgeLength, RodStretchShear, RodBendTwist, PinPosition and
PinRotation - so it cannot make noodles rest on each other, which is the whole
point of a pile.

Usage
-----
In Blender:  Scripting workspace -> Open -> Run Script, then play the timeline.
             The Simulation Zone caches as it plays; scrubbing backwards past
             the cache restarts it. Frame 1 must be the first frame you play.
Headless:    blender --background --python noodle_physics.py
Self-check:  blender --background --python noodle_physics.py -- --check

Scale and gravity
-----------------
The original scene's units, where a noodle is 5 units thick and 1000 long -
inches, near enough. Gravity is therefore 386 (in/s2), not 9.81.

This is deliberately not real-world scale, and the difference is not cosmetic.
What decides how hard the solve is, is gravity measured in segment lengths: a
noodle here falls one segment in about four frames, so the solver gets four
passes to resolve every contact along the way. Real 2 mm spaghetti under 9.81
falls a segment in half a frame and needs roughly three times the Substeps to
reach the same result - at 8 it packs denser than solid pasta, which is the
giveaway that it is half interpenetrated.

So if you rebuild in metres, changing Gravity is not enough on its own; raise
Substeps to about 24 and expect it to cost more. Thicker noodles are the cheap
direction either way - fewer points, and more frames per segment fallen.

Usage contract

--------------
Run inside Blender 5.2+ with ``blender --background --python noodle_physics.py``.
Blender's arguments must follow ``--``; supported modes are ``--check``,
``--bench [preset] [count] [frames]``, and ``--realtime [preset]``. With no
arguments at all - which is what the Scripting workspace's Run Script button
does - the PRESET constant near the top of this file decides the scene.

A CLI error prints one line and exits 2. Note that Blender exits 0 on an
unhandled exception, so an exit code alone is not a pass/fail signal for this
script; tests/ drives the solver in-process instead, where exceptions raise.

The script intentionally has no external Python dependencies.
"""

import sys
from math import tau


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
    if "--check" in argv:
        return ("check",)
    if "--bench" in argv:
        i = argv.index("--bench") + 1
        rest = argv[i:]
        preset = require_preset(rest[0] if rest and not rest[0].startswith("-") else "default")
        count = positive_int(rest[1], "count") if len(rest) > 1 else 120
        frames = positive_int(rest[2], "frames") if len(rest) > 2 else 60
        if len(rest) > 3:
            raise CliError("--bench accepts at most preset, count, and frames")
        return ("bench", preset, count, frames)
    if "--realtime" in argv:
        i = argv.index("--realtime") + 1
        rest = argv[i:]
        preset = require_preset(rest[0] if rest and not rest[0].startswith("-") else "balanced")
        if len(rest) > 1:
            raise CliError("--realtime accepts one preset")
        return ("realtime", preset)
    return ("build",)


import bpy

NG_NAME = "Noodle Physics"
OBJ_NAME = "NoodlePhysics"
MAT_NAME = "Noodle"

# Preset applied when the script is run with no arguments - which is what the
# Scripting workspace's Run Script does, and is the usual way this file is used.
# None gives the reference-quality defaults in PARAMS.
#
# Speed presets (keep the file's inch-flavoured units, ~25 m noodles):
#   None            8 substeps, r2.5    reference quality
#   "quality"       4 substeps, r5.0    cheaper to settle, chunkier pasta
#   "balanced"      3 substeps, r7.0
#   "fast"          2 substeps, r9.0    blocky, but the one to scrub with
#
# Unit presets (metres, real gravity, object scale stays 1.0):
#   "metric"        0.5 m noodles, 16 mm thick   general-purpose metric scene
#   "metric_fast"   0.5 m noodles, 24 mm thick   cheap enough to scrub
#
# Use a metric preset if you have been scaling the object down to see it: that
# workaround shrinks the picture but not the physics, so the fall reads about 3x
# too slow. See the note above UNIT_PRESETS.
#
# Edit this line rather than the sliders: the settings interact, and lowering
# Substeps without raising Noodle Radius puts the sim in slow motion instead of
# speeding it up. See the note above REALTIME_PRESETS for why.
PRESET = None

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
    ("Noodle Count",    "NodeSocketInt",   120,    1,     100000, None),
    ("Seed",            "NodeSocketInt",   0,      0,     100000, None),
    ("Noodle Length",   "NodeSocketFloat", 1000.0, 0.001, 1e6,    "DISTANCE"),
    ("Length Variation","NodeSocketFloat", 0.15,   0.0,   0.9,    "FACTOR"),
    ("Spawn Coil",      "NodeSocketFloat", 1.0,    0.0,   1.0,    "FACTOR"),
    ("Noodle Radius",   "NodeSocketFloat", 2.5,    0.001, 1e6,    "DISTANCE"),
    ("Profile Faces",   "NodeSocketInt",   6,      3,     64,     None),
    ("Fill Diameter",   "NodeSocketFloat", 400.0,  0.001, 1e6,    "DISTANCE"),
    # Object sockets carry no default or range, hence the Nones.
    ("Collider",        "NodeSocketObject", None,  None,  None,   None),
    ("Collider Collection", "NodeSocketCollection", None, None, None, None),
    ("Collider Margin", "NodeSocketFloat", 0.0,    0.0,   1e6,    "DISTANCE"),
    ("Collider Friction","NodeSocketFloat", 0.5,   0.0,   1.0,    "FACTOR"),
    ("Collider Stickiness","NodeSocketFloat", 0.0,  0.0,   1.0,    "FACTOR"),
    ("Sticky Collider", "NodeSocketCollection", None, None, None,   None),
    ("Sticky Collider Grip","NodeSocketFloat", 0.8,  0.0,   1.0,    "FACTOR"),
    ("Start Height",    "NodeSocketFloat", 1250.0, 0.0,   1e6,    "DISTANCE"),
    ("Gravity",         "NodeSocketFloat", 386.0,  0.0,   1e6,    None),
    ("Damping",         "NodeSocketFloat", 0.03,   0.0,   1.0,    "FACTOR"),
    ("Stiffness",       "NodeSocketFloat", 0.5,    0.0,   1.0,    "FACTOR"),
    ("Substeps",        "NodeSocketInt",   8,      1,     128,    None),
    ("Iterations",      "NodeSocketInt",   2,      1,     64,     None),
    ("Friction",        "NodeSocketFloat", 0.9,    0.0,   1.0,    "FACTOR"),
    ("Self Collision",  "NodeSocketFloat", 1.0,    0.0,   1.0,    "FACTOR"),
    ("Cohesion",        "NodeSocketFloat", 0.15,   0.0,   1.0,    "FACTOR"),
]


DESCRIPTIONS = {
    "Noodle Count":
        "How many strands to drop. Cost is linear in this, and it is the "
        "cheapest way to make a scene affordable: halving the count halves the "
        "frame. The presets quote a suggested maximum in the console line they "
        "print when they build",
    "Seed":
        "Changes where every noodle spawns and how long it is, without touching "
        "anything else. Two scenes that differ only in Seed are the same "
        "experiment run twice, which is what makes it useful for checking that "
        "a result is the solver and not the layout",
    "Noodle Length":
        "Rest length of a strand, before Length Variation spreads it. The point "
        "count is derived from this, so doubling it doubles the cost. It is the "
        "one dimension not to shrink on its own: the solver's accuracy is set "
        "by how far a point falls per frame measured in segments, so a shorter "
        "noodle falls more segments per frame and needs more Substeps",
    "Profile Faces":
        "Sides on the tube. Six reads as round at a distance and is the "
        "cheapest; this only affects the surface, never the solve, so it is "
        "free to lower for a preview and raise for a render",
    "Fill Diameter":
        "Diameter of the disc the noodles are dropped onto. It sets how wide "
        "the pile starts, not how wide it ends up - a pile spreads to roughly "
        "its own size as it settles",
    "Start Height":
        "How far above the floor the noodles spawn. Higher means more fall "
        "before the first contact, so more speed to resolve; it is also what "
        "sets the terminal velocity the substep count has to cover, which is "
        "what substeps_for() reads",
    "Gravity":
        "Downward acceleration, in the scene's units per second squared. The "
        "defaults are inches, so 386 is real gravity for a 25 m noodle - not "
        "9.81. What makes a solve hard is gravity measured in segment lengths, "
        "so changing this without changing Substeps does not preserve the "
        "motion: see the Scale and gravity note at the top of the file",
    "Damping":
        "Per-frame air drag, applied as this substep's share of it so raising "
        "Substeps does not thicken the air. Small values are what stop a pile "
        "jittering forever; large ones make the noodles fall like they are in "
        "syrup",
    "Friction":
        "Noodle-on-noodle friction, applied to velocity at contact rather than "
        "to position. This is what stops a pile flowing sideways, and it is "
        "the knob to reach for when a heap spreads out instead of building up",
    "Noodle Radius":
        "Half the noodle thickness. Also the performance dial, twice over: the "
        "point count is derived from it, and thicker noodles fall fewer "
        "segment-lengths per frame so they tolerate fewer Substeps. It is the "
        "radius that has to move whenever Substeps does - halving one and "
        "leaving the other alone is what puts the sim in slow motion. Measured "
        "at 120 noodles on the default scene, r2.5 with 8 substeps costs about "
        "380 ms a frame; r5.0 with 4 substeps costs about 143 ms and settles in "
        "the same number of frames, and it collides slightly better too",
    "Substeps":
        "Times the frame is subdivided. This is what stops fast noodles "
        "tunnelling through each other, and it is the bulk of the cost. It is "
        "also half of what sets the fall speed, because the velocity clamp is "
        "one segment per substep - so lowering this without raising Noodle "
        "Radius makes the sim slower to watch, not faster to run. Raise it "
        "first when tunnelling appears; reduce Iterations before touching it. "
        "Values above 24 are capped to keep evaluation time predictable",
    "Iterations":
        "Constraint passes per substep. 2 is enough; below that the solve "
        "falls apart, above it costs time for very little. Values above 12 "
        "are capped to keep playback responsive",
    "Stiffness":
        "Bending resistance, 1 rigid and 0 a free chain. Solved as XPBD, so "
        "it does not drift much when Substeps or Iterations change",
    "Self Collision":
        "Strength of noodle-against-noodle contact. This is what builds a "
        "pile; at 0 the noodles fall through each other",
    "Collider":
        "Any closed mesh to collide with. It is read as a distance field, so "
        "the normals can face either way and it can be animated. A bowl or "
        "plate needs thickness - add a Solidify modifier. The floor at z=0 "
        "stays active as well",
    "Collider Collection":
        "Collide with every mesh in a collection, joined with the Collider "
        "object. Ten colliders cost the same lookups as one - they share a "
        "single distance field",
    "Collider Margin":
        "Extra clearance held against the collider only. Useful when a "
        "low-poly collider's flat faces cut inside the shape it represents",
    "Collider Friction":
        "Friction against the collider, separate from noodle-on-noodle. A "
        "plate is slipperier than wet pasta on wet pasta",
    "Sticky Collider":
        "A second set of colliders with its own stickiness, for when one "
        "number cannot cover the scene - chopsticks that lift noodles out of "
        "a bowl that lets them go. Collides exactly like the first set; only "
        "the grip differs. Costs a second distance field, about 8 ms a frame",
    "Sticky Collider Grip":
        "Stickiness for the Sticky Collider set only, leaving Collider "
        "Stickiness to the bowls and plates",
    "Collider Stickiness":
        "How strongly noodles cling to the collider just past touching - what "
        "lets strands lift with a chopstick or hang off a tilting plate. Free, "
        "like Cohesion: it reuses the distance the contact already measured. "
        "Raise Collider Friction alongside it, or they slide off sideways",
    "Length Variation":
        "Spread of noodle lengths about Noodle Length. Each noodle is "
        "resampled to its own length, so they all keep the point spacing "
        "collision needs",
    "Spawn Coil":
        "How coiled each noodle starts. 1 drops loose nests, which is what a "
        "floppy noodle actually does; 0 drops straight vertical rods",
    "Cohesion":
        "How strongly noodles cling just past touching. Cooked pasta sticks "
        "to itself; at 0 the pile behaves like dry sticks. Nearly free - it "
        "reuses the neighbour search collision already pays for",
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
    put("Subsurface Radius", (0.9, 0.55, 0.25))
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
    old = bpy.data.node_groups.get(NG_NAME)
    if old:
        bpy.data.node_groups.remove(old)
    ng = bpy.data.node_groups.new(NG_NAME, "GeometryNodeTree")
    nodes, links = ng.nodes, ng.links

    ng.interface.new_socket("Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
    for name, stype, default, lo, hi, subtype in PARAMS:
        s = ng.interface.new_socket(name, in_out="INPUT", socket_type=stype)
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
    # it also caps terminal velocity at one segment per frame - 120 units/s
    # here, a slow-motion fall. Substeps buy back the speed: the cap scales
    # with the substep count, so 8 substeps means 8x the fall speed at the
    # same collision accuracy, for 8x the solve.
    sub_in = nd("GeometryNodeRepeatInput", 20, 2)
    sub_out = nd("GeometryNodeRepeatOutput", 78, 1)
    sub_in.pair_with_output(sub_out)
    # The socket is the count. It also sets the fall-speed cap, because the
    # clamp below is rest/dt and dt is the frame divided by this number - so
    # lowering it without raising Noodle Radius puts the sim in slow motion
    # rather than speeding it up. Only the runtime ceiling is applied here.
    #
    # A previous version derived this from gravity and frame_dt instead, to
    # avoid spending the full budget on easy frames. That quantity is the
    # distance fallen in one frame *from rest*, which is not what the clamp
    # bounds: the clamp bounds the displacement actually achieved. On the
    # default scene it evaluated to 386 * (1/24)^2 / (5 * 0.85) = 0.158, whose
    # square root is 0.397, so CEIL returned 1 on every frame at 24 fps.
    # Substeps stopped changing fall speed at all - 8 and 24 measured
    # bit-identical, both 4.301 units a frame - and the default preset ran
    # 9.4x slow. Deriving the count from the achieved speed is the only form
    # that works, and the clamp below already bounds that to one segment per
    # substep, so the budget cannot run away.
    runtime_substeps = math("MINIMUM", P["Substeps"], MAX_RUNTIME_SUBSTEPS, 20, 3)
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
    # Damping is quoted per frame, so take the substep's share of it -
    # otherwise raising Substeps would silently make the air thicker.
    velocity = vmath("SCALE", velocity,
                     math("POWER", math("SUBTRACT", 1.0, P["Damping"], 23, 6),
                          math("DIVIDE", 1.0, substeps, 23, 7), 24, 6), 25, 3)
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
    # already refreshes this eight times a frame, which is where the freshness
    # that matters comes from.
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

    def collider_pass(solve, grid, stickiness, col, row):
        """Push out of one field, cling to it, and report the distance."""
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
        return solve, gated

    solve, d_plain = collider_pass(solve, grid, P["Collider Stickiness"], 55, 8)
    solve, d_stick = collider_pass(solve, sticky_grid, P["Sticky Collider Grip"], 55, 16)
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
    """Create (or replace) the object carrying the solver."""
    old = bpy.data.objects.get(OBJ_NAME)
    if old:
        data = old.data
        bpy.data.objects.remove(old)
        if data and not data.users:
            bpy.data.meshes.remove(data)
    obj = bpy.data.objects.new(OBJ_NAME, bpy.data.meshes.new(OBJ_NAME))
    bpy.context.scene.collection.objects.link(obj)
    obj.modifiers.new("Noodles", "NODES").node_group = ng
    return obj


def set_input(obj, ng, name, value):
    """Set a modifier input, with an actionable error for renamed sockets."""
    socket = next((s for s in ng.interface.items_tree
                   if getattr(s, "item_type", None) == "SOCKET" and s.name == name), None)
    if socket is None:
        available = [s.name for s in ng.interface.items_tree
                     if getattr(s, "item_type", None) == "SOCKET"]
        raise CliError(f"node-group socket {name!r} is missing; available: {available}")
    identifier = socket.identifier
    inputs = getattr(obj.modifiers[0].properties, "inputs", None)
    target = getattr(inputs, identifier, None)
    if target is None:
        raise CliError(f"modifier input for socket {name!r} is unavailable")
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
        scene.frame_set(frame)
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


# Measured realtime presets (Blender 5.2, this repo's benchmark harness).
#
# The metric that matters is wall-clock time for the pile to actually fall and
# settle, NOT ms per frame. Those are different numbers, and optimising the
# second one alone produces a simulation that is cheaper per frame and slower
# to watch - which is worse than doing nothing.
#
# The reason is the velocity clamp: top_speed = rest * VELOCITY_SAFETY / dt, and
# rest is the point spacing, which is the diameter. So, with fps the frame rate,
#
#     fall speed cap = 2 * radius * VELOCITY_SAFETY * substeps * fps
#     cost           ~ substeps * points  ~  substeps / radius
#
# Fall speed is proportional to radius*substeps; cost is proportional to
# substeps/radius. Hold the speed fixed and cost goes as substeps^2 - so the
# fast direction is FEWER substeps with proportionally FATTER noodles, and
# cutting substeps without raising the radius just puts the sim in slow motion.
# The default caps at 2*2.5*0.85*8*24 = 816 units/s; every preset here lands
# within 15% of that budget. tests/test_cli.py asserts the budget is held and
# tests/test_fall_speed.py asserts the formula itself, because both of these
# have been wrong in this file before.
#
# `substeps_for()` below is how to pick the number for a new scene: it is the
# smallest count whose clamp still lets the scene fall at the speed free fall
# would reach from its own Start Height.
#
# Measured, 120 noodles, `--bench <preset> 120 110`, Windows 11 / Blender 5.2.0
# LTS. ms/frame includes a full vertex readback the bench needs for the settle
# metric and normal playback does not pay, so treat the ratios as the signal,
# not the absolute fps. Frames-to-settle is the portable column - it is the same
# on any machine - and it is the one to compare against after a solver change:
#
#   preset    settings      ms/frame   frames   settle    vs default
#   default   8 sub, r2.5      379       87     33.0 s      1.00x
#   quality   4 sub, r5.0      143       97     13.9 s      2.64x
#   balanced  3 sub, r7.0      109       99     10.8 s      3.47x
#   fast      2 sub, r9.0       62      100      6.2 s      6.11x
#
# These absolute ms numbers move by ~2x between machines and Blender builds; the
# frame counts do not. Re-run the bench rather than trusting either.
#
# The scaling is not linear in substeps/radius: 'fast' is cheaper than the ratio
# predicts and 'balanced' beats 'quality' by 1.3x rather than landing level with
# it. The nearest-neighbour query dominates and its cost falls off a cliff once
# the point count drops far enough, so measure rather than interpolate when
# adding a preset. An earlier version of this table claimed 'balanced' was level
# with 'quality' and that 'fast' was 11x cheaper; on the same machine today it
# is 1.3x and 2.3x. Re-run the bench.
REALTIME_PRESETS = {
    # name: (overrides dict, suggested max noodle count for the fps quoted)
    # "default" is the reference scene already in PARAMS, so it overrides
    # nothing. It is a real entry rather than a special case so that the list
    # of names cannot drift from the table - the old early-return version
    # advertised this name in the README and then raised KeyError on it.
    "default":  ({}, 120),
    "quality":  ({"Substeps": 4, "Iterations": 2, "Noodle Radius": 5.0,
                  "Profile Faces": 5}, 120),
    "balanced": ({"Substeps": 3, "Iterations": 2, "Noodle Radius": 7.0,
                  "Profile Faces": 4}, 120),
    "fast":     ({"Substeps": 2, "Iterations": 2, "Noodle Radius": 9.0,
                  "Profile Faces": 4}, 120),
}


def substeps_for(radius, gravity, start_height, fps=24.0, headroom=1.0):
    """Fewest substeps whose velocity clamp still allows a full-speed fall.

    The clamp is top_speed = rest * VELOCITY_SAFETY / dt, which per frame is
    2 * radius * VELOCITY_SAFETY * substeps * fps, and free fall from
    `start_height` reaches sqrt(2*g*h). Ask for fewer substeps than this and the
    clamp bites every frame: the sim does not get faster, it goes into slow
    motion, which is the one failure mode that looks like a performance win in a
    ms-per-frame benchmark.

    Rounded rather than ceiled, because the file's own defaults sit fractionally
    under the bound. Raise `headroom` above 1.0 to insist on the full unclamped
    speed.
    """
    reach = (2.0 * gravity * max(start_height, 0.0)) ** 0.5
    need = headroom * reach / (2.0 * max(radius, 1e-9) * VELOCITY_SAFETY * fps)
    return max(1, int(round(need)))


# Unit presets. The defaults in PARAMS are inches - a noodle 1000 units long is
# 25 m, and gravity 386 in/s2 IS real gravity, so the timing is already correct
# for a 25 m noodle. Shrinking that with object scale does not shrink the
# physics with it: the solve still takes as long as a 25 m fall, so a scene
# scaled to 0.003 reads about 3x slower than reality. Rebuilding at the size you
# actually want is the fix, and it is safe - the solver is scale-invariant to
# within 0.5% over a 333x change in unit size (measured, both pile height and
# reach normalised by noodle length).
#
# Real 2 mm spaghetti at 9.81 is genuinely expensive: it falls a segment every
# half frame, so the clamp above wants ~50 substeps. Thick noodles are the way
# out, exactly as they were for the realtime presets - fewer points AND fewer
# substeps. These are udon rather than spaghetti, which is the honest cost.
_METRIC = dict(length=0.5, start=0.35, fill=0.2, gravity=9.81)

UNIT_PRESETS = {
    # ~16 mm thick, 31 points a noodle. The general-purpose metric scene.
    "metric": ({"Noodle Length": _METRIC["length"],
                "Noodle Radius": 0.008,
                "Start Height": _METRIC["start"],
                "Fill Diameter": _METRIC["fill"],
                "Gravity": _METRIC["gravity"],
                "Substeps": substeps_for(0.008, _METRIC["gravity"], _METRIC["start"]),
                "Iterations": 2,
                "Profile Faces": 5}, 120),
    # ~24 mm thick, 21 points a noodle. Chunkier, but cheap enough to scrub.
    "metric_fast": ({"Noodle Length": _METRIC["length"],
                     "Noodle Radius": 0.012,
                     "Start Height": _METRIC["start"],
                     "Fill Diameter": _METRIC["fill"],
                     "Gravity": _METRIC["gravity"],
                     "Substeps": substeps_for(0.012, _METRIC["gravity"], _METRIC["start"]),
                     "Iterations": 2,
                     "Profile Faces": 4}, 120),
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
    """Push a preset onto an already-built noodle object.

    'default' resolves to an empty override set, so it is a no-op rather than
    a branch. Returns obj for chaining.
    """
    overrides, _ = lookup_preset(preset)
    for name, value in overrides.items():
        set_input(obj, ng, name, value)
    return obj


def bench(preset="default", count=120, frames=90):
    """Time a run and report BOTH ms/frame and wall-clock time to settle.

    Reporting ms/frame alone is how you end up shipping a preset that is
    cheaper per frame and slower to watch: the velocity clamp ties fall speed
    to radius*substeps, so a cheap preset can simply be in slow motion. The
    settle time is the number that reflects what the user actually waits for.
    """
    import time
    ng = build_group()
    obj = build_object(ng)
    set_input(obj, ng, "Noodle Count", count)
    if preset and preset != "default":
        apply_realtime(obj, ng, preset)

    scene = bpy.context.scene
    dg = bpy.context.evaluated_depsgraph_get

    tops, times = [], []
    for f in range(1, frames + 1):
        t = time.perf_counter()
        scene.frame_set(f)
        evaluated = obj.evaluated_get(dg())
        mesh = evaluated.to_mesh()
        try:
            if not mesh.vertices:
                raise RuntimeError(f"frame {f} produced no evaluated vertices")
            zs = [v.co.z for v in mesh.vertices]
            times.append((time.perf_counter() - t) * 1000.0)
            tops.append(max(zs))
        finally:
            evaluated.to_mesh_clear()


    steady = sorted(times[5:] if len(times) > 6 else times)
    mean = sum(steady) / len(steady)
    # "Settled" = pile top has come down to a quarter of its spawn height.
    target = tops[0] * 0.25
    hit = next((i + 1 for i, z in enumerate(tops) if z <= target), None)
    settle = f"{hit * mean / 1000:.2f}s over {hit} frames" if hit else \
             f"NOT SETTLED in {frames} frames (slow motion?)"

    print(f"BENCH preset={preset} count={count} frames={frames}")
    print(f"  mean {mean:7.1f} ms/frame ({1000.0/mean:5.1f} fps)"
          f"  median {steady[len(steady)//2]:7.1f} ms")
    print(f"  settle: {settle}")
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


def main():
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    mode = parse_mode_args(argv)
    if mode[0] == "check":
        self_check()
        return
    if mode[0] == "bench":
        _, preset, count, frames = mode
        bench(preset, count, frames)
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
        overrides, maxn = lookup_preset(preset)
        print(f"Built '{NG_NAME}' with preset '{preset}': {overrides}. "
              f"For the quoted speed keep Noodle Count near {maxn} or below. "
              f"Play the timeline from frame 1.")
        return
    print(f"Built '{NG_NAME}' on object '{OBJ_NAME}'. Play the timeline from "
          f"frame 1. Slow? Set PRESET = \"fast\" at the top of this file.")



if __name__ == "__main__":
    try:
        main()
    except CliError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
