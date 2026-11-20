"""Photographic set for the bowl demo: ceramic bowl, oak table, window light.

Everything is procedural and metre scale, so the add-on stays a pure-Python
extension with no image files. The bowl is a closed, watertight mesh because
the solver voxelises it into a signed distance field.
"""
from math import cos, pi, radians, sin, tau

import bpy
from mathutils import Euler, Matrix, Vector, noise

# --------------------------------------------------------------------- bowl
# A thrown donburi: 19.9 cm across, 8.3 cm tall, 6-9 mm walls, rounded lip
# and a trimmed 8 mm foot ring. (radius, height) in metres, from the inside
# centre up the inner wall, over the lip, down the outside, round the foot and
# back in under the base. The order sets the face winding (outward normals).
BOWL_PROFILE = (
    # inner surface, glazed, what the noodles touch
    (0.0, 0.0150), (0.012, 0.0151), (0.024, 0.0156), (0.035, 0.0167),
    (0.045, 0.0186), (0.054, 0.0214), (0.062, 0.0252), (0.069, 0.0300),
    (0.075, 0.0357), (0.080, 0.0421), (0.0842, 0.0490), (0.0876, 0.0562),
    (0.0902, 0.0634), (0.0920, 0.0703), (0.0931, 0.0763), (0.0937, 0.0800),
    # rounded lip, ~5.5 mm thick
    (0.0941, 0.0819), (0.0952, 0.0831), (0.0966, 0.0833), (0.0980, 0.0827),
    (0.0990, 0.0813), (0.0994, 0.0792),
    # outer wall: the inner curve offset by 9 mm at the base tapering to 6 mm
    # below the lip. Thinner than ~1.2 noodle radii and the voxelised SDF
    # loses the wall and noodles leak through.
    (0.0991, 0.0753), (0.0981, 0.0690), (0.0965, 0.0615), (0.0941, 0.0535),
    (0.0908, 0.0455), (0.0865, 0.0376), (0.0812, 0.0302), (0.0745, 0.0233),
    (0.0665, 0.0176), (0.0590, 0.0138), (0.0520, 0.0113), (0.0470, 0.0102),
    # trimmed foot ring: outer bevel, 2.4 mm flat bearing face, inner bevel
    (0.0440, 0.0095), (0.0428, 0.0072), (0.0422, 0.0030), (0.0416, 0.0007),
    (0.0407, 0.0), (0.0383, 0.0), (0.0374, 0.0008), (0.0368, 0.0032),
    # recessed underside inside the foot
    (0.0360, 0.0052), (0.0338, 0.0060), (0.0250, 0.0064), (0.0120, 0.0066),
    (0.0, 0.0067),
)
BOWL_SIDES = 192
BOWL_RIM_RADIUS = 0.0994
GLAZE_LINE = 0.0135      # height where the outside glaze stops above the foot


def _profile_lengths(profile):
    lengths, total = [0.0], 0.0
    for (r0, z0), (r1, z1) in zip(profile, profile[1:]):
        total += ((r1 - r0) ** 2 + (z1 - z0) ** 2) ** 0.5
        lengths.append(total)
    return lengths


def _thrown(angle, r, z):
    """Throwing irregularity: slight oval, rim undulation, radial ripple."""
    c, s = cos(angle), sin(angle)
    ripple = noise.noise(Vector((c * 1.6, s * 1.6, z * 18.0 + 3.1)))
    oval = 0.0035 * cos(2 * angle + 0.7) + 0.0015 * cos(3 * angle + 2.0)
    r *= 1.0 + oval + 0.0025 * ripple
    lift = max(0.0, (z - 0.05) / 0.033)
    z += lift * lift * 0.0007 * noise.noise(Vector((c * 1.3, s * 1.3, 7.7)))
    return (r * c, r * s, z)


def _revolve(profile, name, sides=BOWL_SIDES, thrown=True):
    """Closed surface of revolution; profile ends must sit on the axis."""
    vertices, rings = [], []
    for radius, height in profile:
        ring = []
        for i in range(sides if radius else 1):
            ring.append(len(vertices))
            angle = tau * i / sides
            if not radius:
                vertices.append((0.0, 0.0, height))
            elif thrown:
                vertices.append(_thrown(angle, radius, height))
            else:
                vertices.append((radius * cos(angle), radius * sin(angle), height))
        rings.append(ring)
    faces = []
    for a, b in zip(rings, rings[1:]):
        for i in range(sides):
            j = (i + 1) % sides
            if len(a) == 1:
                faces.append((a[0], b[i], b[j]))
            elif len(b) == 1:
                faces.append((a[i], b[0], a[j]))
            else:
                faces.append((a[i], b[i], b[j], a[j]))
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata(vertices, [], faces)
    mesh.update()
    for face in mesh.polygons:
        face.use_smooth = True
    return mesh


def bowl_mesh(collection, material=None):
    """Closed thrown bowl with gentle hand-made wobble.

    The wobble is a slight oval, a rim that rises and falls by under a
    millimetre and a low-frequency radial ripple. It is applied to the inner
    and outer skins alike so the wall keeps its thickness and the mesh stays
    manifold. Per-vertex 'profile_t' (metres along the profile) and 'outside'
    drive the glaze shader: pooling, rim break and the raw clay foot.
    """
    profile = BOWL_PROFILE
    lengths = _profile_lengths(profile)
    rim_index = max(range(len(profile)), key=lambda i: profile[i][1])
    t_rim = lengths[rim_index]
    t_values, outside = [], []
    for k, (radius, _) in enumerate(profile):
        n = BOWL_SIDES if radius else 1
        t_values += [lengths[k]] * n
        outside += [1.0 if k > rim_index else 0.0] * n
    mesh = _revolve(profile, "Thrown donburi")
    for name, values in (("profile_t", t_values), ("outside", outside)):
        attr = mesh.attributes.new(name, "FLOAT", "POINT")
        attr.data.foreach_set("value", values)
    mesh["rim_t"] = t_rim
    obj = bpy.data.objects.new("Bowl - closed collider", mesh)
    collection.objects.link(obj)
    mesh.materials.append(material or glaze_material(t_rim))
    return obj


# ----------------------------------------------------------- node helpers
class Nodes:
    """Tiny wrapper that keeps shader construction readable."""

    def __init__(self, tree):
        self.tree = tree
        self.nodes = tree.nodes
        self.links = tree.links
        self.x = 0

    def new(self, kind, **props):
        node = self.nodes.new(kind)
        node.location = (self.x, 0)
        self.x += 20
        for key, value in props.items():
            setattr(node, key, value)
        return node

    def link(self, socket, target):
        if isinstance(socket, (int, float)):
            target.default_value = socket
        elif isinstance(socket, tuple):
            target.default_value = socket
        else:
            self.links.new(socket, target)

    def math(self, op, a, b=None, clamp=False):
        node = self.new("ShaderNodeMath", operation=op, use_clamp=clamp)
        self.link(a, node.inputs[0])
        if b is not None:
            self.link(b, node.inputs[1])
        return node.outputs[0]

    def vmath(self, op, a, b=None):
        node = self.new("ShaderNodeVectorMath", operation=op)
        self.link(a, node.inputs[0])
        if b is not None:
            self.link(b, node.inputs[1])
        return node.outputs[1] if op in {"LENGTH", "DOT_PRODUCT", "DISTANCE"} else node.outputs[0]

    def map_range(self, value, a, b, c=0.0, d=1.0, interp="SMOOTHSTEP"):
        node = self.new("ShaderNodeMapRange", interpolation_type=interp)
        self.link(value, node.inputs["Value"])
        for name, v in zip(("From Min", "From Max", "To Min", "To Max"), (a, b, c, d)):
            self.link(v, node.inputs[name])
        return node.outputs["Result"]

    def mix(self, fac, a, b):
        node = self.new("ShaderNodeMix", data_type="RGBA")
        self.link(fac, node.inputs[0])
        self.link(a, node.inputs[6])
        self.link(b, node.inputs[7])
        return node.outputs[2]

    def mixf(self, fac, a, b):
        node = self.new("ShaderNodeMix", data_type="FLOAT")
        self.link(fac, node.inputs[0])
        self.link(a, node.inputs[2])
        self.link(b, node.inputs[3])
        return node.outputs[0]

    def noise(self, vector, scale, detail=2.0, rough=0.5, distortion=0.0, dims="3D", w=None):
        node = self.new("ShaderNodeTexNoise", noise_dimensions=dims)
        self.link(vector, node.inputs["Vector"])
        if w is not None:
            self.link(w, node.inputs["W"])
        node.inputs["Scale"].default_value = scale
        node.inputs["Detail"].default_value = detail
        node.inputs["Roughness"].default_value = rough
        node.inputs["Distortion"].default_value = distortion
        return node

    def voronoi(self, vector, scale, feature="F1", randomness=1.0):
        node = self.new("ShaderNodeTexVoronoi", feature=feature)
        self.link(vector, node.inputs["Vector"])
        node.inputs["Scale"].default_value = scale
        node.inputs["Randomness"].default_value = randomness
        return node

    def scale(self, vector, xyz):
        node = self.new("ShaderNodeVectorMath", operation="MULTIPLY")
        self.link(vector, node.inputs[0])
        node.inputs[1].default_value = xyz
        return node.outputs[0]

    def attr(self, name):
        return self.new("ShaderNodeAttribute", attribute_name=name).outputs["Fac"]

    def ramp(self, value, stops):
        node = self.new("ShaderNodeValToRGB")
        self.link(value, node.inputs["Fac"])
        elements = node.color_ramp.elements
        while len(elements) < len(stops):
            elements.new(0.5)
        for element, (pos, colour) in zip(elements, stops):
            element.position = pos
            element.color = tuple(colour) + ((1.0,) if len(colour) == 3 else ())
        return node.outputs["Color"]

    def bump(self, height, strength, distance, normal=None):
        node = self.new("ShaderNodeBump")
        self.link(height, node.inputs["Height"])
        node.inputs["Strength"].default_value = strength
        node.inputs["Distance"].default_value = distance
        if normal is not None:
            self.link(normal, node.inputs["Normal"])
        return node.outputs["Normal"]


def _new_material(name):
    mat = bpy.data.materials.new(name)
    tree = mat.node_tree
    for node in list(tree.nodes):
        tree.nodes.remove(node)
    out = tree.nodes.new("ShaderNodeOutputMaterial")
    bsdf = tree.nodes.new("ShaderNodeBsdfPrincipled")
    tree.links.new(bsdf.outputs["BSDF"], out.inputs["Surface"])
    return mat, Nodes(tree), bsdf


# ------------------------------------------------------------------ glaze
def glaze_material(t_rim):
    """Iron-saturate 'kaki/tenmoku' glaze over a buff stoneware body.

    Dark where the glaze pools in the well, breaking to rust over the lip and
    the throwing ridges, faint hare's-fur streaks running down the outside, a
    scatter of iron spots, an orange-peel surface that ripples reflections,
    and a raw, toasted clay foot below a wavy glaze line.
    """
    mat, n, bsdf = _new_material("Kaki tenmoku glaze over stoneware")
    mat.diffuse_color = (0.05, 0.025, 0.015, 1)
    obj = n.new("ShaderNodeTexCoord").outputs["Object"]
    t = n.attr("profile_t")
    outside = n.attr("outside")
    sep = n.new("ShaderNodeSeparateXYZ")
    n.link(obj, sep.inputs[0])
    z = sep.outputs["Z"]

    # Distance along the profile from the lip; the glaze thins where it runs
    # off the edge.
    rim_dist = n.math("ABSOLUTE", n.math("SUBTRACT", t, t_rim))
    wobble = n.noise(obj, 22.0, 3.0, 0.55).outputs["Fac"]
    rim_break = n.map_range(n.math("ADD", rim_dist, n.math("MULTIPLY", wobble, 0.009)),
                            0.0075, 0.0028)
    rim_break = n.math("MULTIPLY", rim_break, n.map_range(n.noise(obj, 60.0, 3.0, 0.6).outputs["Fac"], 0.3, 0.6, 0.55, 1.0))
    # Throwing ridges: thinner glaze over each crest.
    ridge_phase = n.math("ADD", n.math("MULTIPLY", z, tau / 0.0072),
                         n.math("MULTIPLY", n.noise(obj, 9.0, 1.0).outputs["Fac"], 5.0))
    ridges = n.math("MULTIPLY", n.math("SINE", ridge_phase), 0.5)
    ridge_thin = n.map_range(ridges, 0.3, 0.5, 0.0, 0.10)
    # Hare's-fur streaks: stretched noise, long in Z, tight around the wall.
    streak_vec = n.scale(obj, (110.0, 110.0, 5.0))
    streaks = n.map_range(n.noise(streak_vec, 1.0, 6.0, 0.62).outputs["Fac"], 0.52, 0.8, 0.0, 0.4)
    thin = n.math("MAXIMUM", rim_break,
                  n.math("ADD", n.math("MULTIPLY", streaks, outside),
                         n.math("MULTIPLY", ridge_thin, n.map_range(z, 0.03, 0.07))))
    # Pooling in the well: deeper, darker, glassier.
    pool = n.map_range(t, 0.045, 0.012)
    tone = n.noise(obj, 6.0, 4.0, 0.6).outputs["Fac"]
    body = n.mix(n.map_range(tone, 0.3, 0.7), (0.016, 0.0085, 0.0050, 1), (0.045, 0.020, 0.0095, 1))
    body = n.mix(pool, body, (0.010, 0.0055, 0.0035, 1))
    kaki = n.mix(n.map_range(tone, 0.3, 0.7), (0.17, 0.062, 0.022, 1), (0.29, 0.125, 0.05, 1))
    glaze = n.mix(thin, body, kaki)
    # Iron spots: sparse, small, a touch raised; rust with dark cores.
    spots_v = n.voronoi(obj, 260.0)
    spot = n.map_range(spots_v.outputs["Distance"], 0.16, 0.06)
    spot_sel = n.map_range(n.noise(obj, 140.0, 0.0).outputs["Fac"], 0.58, 0.66)
    spot = n.math("MULTIPLY", spot, spot_sel)
    glaze = n.mix(n.math("MULTIPLY", spot, 0.85), glaze, (0.30, 0.13, 0.05, 1))

    # Unglazed foot: the glaze stops on a wavy line just above the ring and
    # rolls into a slightly thicker, darker bead.
    edge_noise = n.math("MULTIPLY", n.math("SUBTRACT", n.noise(obj, 30.0, 3.0).outputs["Fac"], 0.5), 0.004)
    above = n.math("SUBTRACT", n.math("ADD", z, edge_noise), GLAZE_LINE)
    glazed = n.math("MAXIMUM", n.map_range(above, -0.0004, 0.0004),
                    n.math("SUBTRACT", 1.0, outside))
    bead = n.math("MULTIPLY", n.map_range(above, 0.0, 0.0018, 1.0, 0.0), glazed)
    glaze = n.mix(n.math("MULTIPLY", bead, 0.7), glaze, (0.012, 0.006, 0.004, 1))
    grog = n.map_range(n.noise(obj, 900.0, 0.0).outputs["Fac"], 0.66, 0.74)
    clay_tone = n.noise(obj, 40.0, 3.0).outputs["Fac"]
    clay = n.mix(n.map_range(clay_tone, 0.3, 0.7), (0.30, 0.19, 0.115, 1), (0.40, 0.27, 0.17, 1))
    # Kiln flashing: the clay toasts darker toward the glaze line.
    clay = n.mix(n.map_range(z, 0.006, 0.0125, 0.0, 0.55), clay, (0.17, 0.085, 0.045, 1))
    clay = n.mix(n.math("MULTIPLY", grog, 0.8), clay, (0.06, 0.04, 0.03, 1))
    n.link(n.mix(glazed, clay, glaze), bsdf.inputs["Base Color"])

    # Glossy glaze with slight variation; the thin kaki areas go satin.
    gloss = n.map_range(n.noise(obj, 14.0, 2.0).outputs["Fac"], 0.3, 0.7, 0.035, 0.075)
    gloss = n.math("ADD", gloss, n.math("MULTIPLY", thin, 0.10))
    n.link(n.mixf(glazed, 0.82, gloss), bsdf.inputs["Roughness"])
    bsdf.inputs["IOR"].default_value = 1.53
    bsdf.inputs["Specular IOR Level"].default_value = 0.5

    # Surface: orange peel and long-wave waviness on the glaze, pinholes,
    # rough grog on the clay. Throwing ridges show faintly through the glaze.
    peel = n.noise(obj, 260.0, 2.0, 0.5).outputs["Fac"]
    wave = n.noise(obj, 38.0, 2.0, 0.5).outputs["Fac"]
    pin_v = n.voronoi(obj, 420.0)
    pin_sel = n.map_range(n.noise(obj, 90.0, 0.0).outputs["Fac"], 0.66, 0.72)
    pins = n.math("MULTIPLY", n.map_range(pin_v.outputs["Distance"], 0.08, 0.0), pin_sel)
    glaze_h = n.math("ADD", n.math("MULTIPLY", peel, 0.25), n.math("MULTIPLY", wave, 1.0))
    glaze_h = n.math("ADD", glaze_h, n.math("MULTIPLY", ridges, 0.35))
    glaze_h = n.math("SUBTRACT", glaze_h, n.math("MULTIPLY", pins, 0.6))
    glaze_h = n.math("ADD", glaze_h, n.math("MULTIPLY", bead, 0.8))
    clay_h = n.math("ADD", n.noise(obj, 600.0, 3.0, 0.7).outputs["Fac"], n.math("MULTIPLY", grog, 0.8))
    normal = n.bump(n.mixf(glazed, clay_h, glaze_h), 1.0, 0.0002)
    n.link(normal, bsdf.inputs["Normal"])
    return mat


# ------------------------------------------------------------------ table
def oak_material():
    """Oiled white-oak planks: growth rings, medullary flecks, open pores,
    seams between boards, per-board tone and a little wear."""
    mat, n, bsdf = _new_material("Oiled oak planks")
    mat.diffuse_color = (0.25, 0.15, 0.08, 1)
    obj = n.new("ShaderNodeTexCoord").outputs["Object"]
    sep = n.new("ShaderNodeSeparateXYZ")
    n.link(obj, sep.inputs[0])
    x, y = sep.outputs["X"], sep.outputs["Y"]
    width = 0.19
    # Boards run along Y. Each board gets an id, a lengthwise offset and its
    # own end joint.
    # Offset so the bowl sits mid-board, not on a seam.
    xb = n.math("DIVIDE", n.math("ADD", x, 0.056), width)
    board = n.math("FLOOR", xb)
    local_x = n.math("SUBTRACT", xb, board)
    board_rand = n.new("ShaderNodeTexWhiteNoise", noise_dimensions="1D")
    n.link(board, board_rand.inputs["W"])
    rnd = board_rand.outputs["Value"]
    rnd_col = board_rand.outputs["Color"]
    # Grain coordinates: offset per board so neighbours don't line up.
    gx = n.math("ADD", x, n.math("MULTIPLY", rnd, 3.7))
    gy = n.math("ADD", y, n.math("MULTIPLY", rnd, 11.3))
    comb = n.new("ShaderNodeCombineXYZ")
    n.link(gx, comb.inputs[0])
    n.link(gy, comb.inputs[1])
    n.link(n.math("MULTIPLY", rnd, 5.0), comb.inputs[2])
    g = comb.outputs[0]
    # Growth rings of a flat-sawn board: distance from each surface point to
    # the (slightly tilted) log axis the board was cut from. Where the cut
    # plane approaches the pith the rings open into cathedral arches.
    centre = n.math("SUBTRACT", n.math("MULTIPLY", rnd, 0.16), 0.08)
    dx = n.math("SUBTRACT", n.math("MULTIPLY", n.math("SUBTRACT", local_x, 0.5), width), centre)
    depth = n.math("ADD", n.math("MULTIPLY", rnd, 0.08), 0.07)
    slope = n.math("MULTIPLY", n.math("SUBTRACT", n.math("FRACT", n.math("MULTIPLY", rnd, 7.13)), 0.5), 0.07)
    dz = n.math("ADD", depth, n.math("MULTIPLY", y, slope))
    warp = n.noise(n.scale(g, (3.0, 0.5, 1.0)), 1.0, 2.0, 0.5).outputs["Fac"]
    dz = n.math("ADD", dz, n.math("MULTIPLY", n.math("SUBTRACT", warp, 0.5), 0.006))
    radius = n.math("SQRT", n.math("ADD", n.math("MULTIPLY", dx, dx), n.math("MULTIPLY", dz, dz)))
    jitter = n.noise(n.scale(g, (5.0, 0.7, 1.0)), 1.0, 3.0, 0.5).outputs["Fac"]
    fine = n.noise(n.scale(g, (160.0, 6.0, 1.0)), 1.0, 2.0, 0.5).outputs["Fac"]
    radius = n.math("ADD", radius, n.math("MULTIPLY", jitter, 0.0025))
    radius = n.math("ADD", radius, n.math("MULTIPLY", fine, 0.0004))
    mid = n.noise(n.scale(g, (22.0, 2.2, 1.0)), 1.0, 3.0, 0.5).outputs["Fac"]
    radius = n.math("ADD", radius, n.math("MULTIPLY", mid, 0.0014))
    # Uneven ring widths: a 1D warp of the radius itself stays monotonic.
    uneven = n.new("ShaderNodeTexNoise", noise_dimensions="1D")
    n.link(n.math("MULTIPLY", radius, 140.0), uneven.inputs["W"])
    uneven.inputs["Detail"].default_value = 3.0
    radius = n.math("ADD", radius, n.math("MULTIPLY", uneven.outputs["Fac"], 0.0035))
    # Ring spacing is per board: a varying divisor would scramble the rings.
    spacing = n.math("ADD", 0.0032, n.math("MULTIPLY", n.math("FRACT", n.math("MULTIPLY", rnd, 3.71)), 0.0022))
    ring_f = n.math("FRACT", n.math("DIVIDE", radius, spacing))
    # Earlywood is a narrow porous band; latewood dense and darker.
    ring_v = n.map_range(ring_f, 0.62, 0.93)
    ring_v = n.math("MULTIPLY", ring_v, n.map_range(ring_f, 1.0, 0.94))
    # Fine streaks along the board.
    streak = n.noise(n.scale(g, (220.0, 6.0, 1.0)), 1.0, 5.0, 0.6).outputs["Fac"]
    streak = n.map_range(streak, 0.35, 0.8, 0.0, 0.6)
    # Pores: tiny dark dashes, long in Y.
    pores = n.noise(n.scale(g, (1400.0, 60.0, 1.0)), 1.0, 1.0, 0.5).outputs["Fac"]
    pores = n.map_range(pores, 0.68, 0.78)
    # Pores cluster in the earlywood.
    pores = n.math("MULTIPLY", pores, n.map_range(ring_f, 0.35, 0.6, 1.0, 0.3))
    # Medullary ray flecks (quarter-sawn oak): short lighter lenses.
    fleck = n.voronoi(n.scale(g, (90.0, 14.0, 1.0)), 1.0, "F1")
    fleck = n.math("MULTIPLY", n.map_range(fleck.outputs["Distance"], 0.12, 0.02),
                   n.map_range(n.noise(n.scale(g, (8.0, 2.0, 1.0)), 1.0, 1.0).outputs["Fac"], 0.55, 0.65))
    early = n.mix(n.math("MULTIPLY", rnd, 1.0), (0.17, 0.10, 0.043, 1), (0.135, 0.078, 0.032, 1))
    late = n.mix(n.math("MULTIPLY", rnd, 1.0), (0.080, 0.042, 0.016, 1), (0.060, 0.032, 0.012, 1))
    col = n.mix(n.math("MULTIPLY", ring_v, 0.65), early, late)
    col = n.mix(n.math("MULTIPLY", streak, 0.35), col, late)
    col = n.mix(n.math("MULTIPLY", pores, 0.5), col, (0.05, 0.026, 0.012, 1))
    col = n.mix(n.math("MULTIPLY", fleck, 0.25), col, (0.22, 0.12, 0.055, 1))
    # Board-to-board tint shift.
    tint = n.math("ADD", 0.82, n.math("MULTIPLY", rnd, 0.36))
    hsv = n.new("ShaderNodeHueSaturation")
    n.link(col, hsv.inputs["Color"])
    n.link(tint, hsv.inputs["Value"])
    n.link(n.math("ADD", 0.95, n.math("MULTIPLY", rnd, 0.15)), hsv.inputs["Saturation"])
    col = hsv.outputs["Color"]
    # Seams: dark grooves where boards meet.
    edge = n.math("MINIMUM", local_x, n.math("SUBTRACT", 1.0, local_x))
    seam = n.map_range(n.math("MULTIPLY", edge, width), 0.0009, 0.0001)
    col = n.mix(seam, col, (0.008, 0.005, 0.003, 1))
    # Wear: lighter, drier patches.
    wear = n.map_range(n.noise(obj, 3.5, 4.0, 0.6).outputs["Fac"], 0.55, 0.75)
    col = n.mix(n.math("MULTIPLY", wear, 0.2), col, (0.20, 0.12, 0.06, 1))
    n.link(col, bsdf.inputs["Base Color"])
    # Oiled finish: satin, rougher in pores and worn patches, smudges.
    smudge = n.noise(obj, 9.0, 3.0, 0.6).outputs["Fac"]
    rough = n.map_range(smudge, 0.3, 0.7, 0.34, 0.5)
    rough = n.math("ADD", rough, n.math("MULTIPLY", pores, 0.25))
    rough = n.math("ADD", rough, n.math("MULTIPLY", wear, 0.12))
    rough = n.math("ADD", rough, n.math("MULTIPLY", seam, 0.5))
    n.link(rough, bsdf.inputs["Roughness"])
    bsdf.inputs["Specular IOR Level"].default_value = 0.45
    height = n.math("ADD", n.math("MULTIPLY", ring_v, 0.06), n.math("MULTIPLY", pores, -0.12))
    height = n.math("ADD", height, n.math("MULTIPLY", streak, 0.2))
    height = n.math("SUBTRACT", height, n.math("MULTIPLY", seam, 0.4))
    n.link(n.bump(height, 1.0, 0.00025), bsdf.inputs["Normal"])
    return mat


def table(collection):
    """A 1.8 x 1.4 m slab of planks, thick enough to have real edges that the
    long lens never sees."""
    mesh = bpy.data.meshes.new("Oak table")
    sx, sy = 0.9, 0.7
    verts = [(-sx, -sy, -0.03), (sx, -sy, -0.03), (sx, sy, -0.03), (-sx, sy, -0.03),
             (-sx, -sy, 0.0), (sx, -sy, 0.0), (sx, sy, 0.0), (-sx, sy, 0.0)]
    faces = [(0, 3, 2, 1), (4, 5, 6, 7), (0, 1, 5, 4), (1, 2, 6, 5), (2, 3, 7, 6), (3, 0, 4, 7)]
    mesh.from_pydata(verts, [], faces)
    mesh.update()
    obj = bpy.data.objects.new("Oak table", mesh)
    collection.objects.link(obj)
    obj.rotation_euler = (0.0, 0.0, radians(-9.0))
    obj.location = (0.0, 0.25, 0.0)
    mesh.materials.append(oak_material())
    return obj


# ------------------------------------------------------------- chopsticks
def chopstick_material():
    mat, n, bsdf = _new_material("Lacquered bamboo chopsticks")
    obj = n.new("ShaderNodeTexCoord").outputs["Object"]
    streak = n.noise(n.scale(obj, (5.0, 400.0, 400.0)), 1.0, 4.0, 0.6).outputs["Fac"]
    col = n.mix(n.map_range(streak, 0.3, 0.75), (0.075, 0.036, 0.016, 1), (0.040, 0.018, 0.008, 1))
    node = n.new("ShaderNodeSeparateXYZ")
    n.link(obj, node.inputs[0])
    # Bare, paler tips (the last 6 cm), as on real lacquered hashi.
    tip = n.map_range(node.outputs["X"], 0.180, 0.184)
    col = n.mix(tip, col, (0.22, 0.13, 0.065, 1))
    n.link(col, bsdf.inputs["Base Color"])
    n.link(n.mixf(tip, 0.22, 0.6), bsdf.inputs["Roughness"])
    n.link(n.bump(streak, 0.4, 0.0001), bsdf.inputs["Normal"])
    return mat


def chopstick_mesh(name, length=0.225, butt=0.0034, tip=0.0011, sides=16):
    """Tapered round chopstick along +X from its butt; rounded ends, closed."""
    verts, faces = [], []
    stations = 24
    xs = [length * i / stations for i in range(stations + 1)]
    rings = []
    verts.append((-0.0003, 0.0, 0.0))
    for x in xs:
        u = x / length
        r = butt + (tip - butt) * (u ** 1.15)
        if x == 0.0:
            r *= 0.85
        ring = []
        for k in range(sides):
            a = tau * k / sides
            ring.append(len(verts))
            verts.append((x, r * cos(a), r * sin(a)))
        rings.append(ring)
    verts.append((length + tip * 0.6, 0.0, 0.0))
    first, last = 0, len(verts) - 1
    for k in range(sides):
        faces.append((first, rings[0][(k + 1) % sides], rings[0][k]))
    for a, b in zip(rings, rings[1:]):
        for k in range(sides):
            j = (k + 1) % sides
            faces.append((a[k], a[j], b[j], b[k]))
    for k in range(sides):
        faces.append((last, rings[-1][k], rings[-1][(k + 1) % sides]))
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata(verts, [], faces)
    mesh.update()
    for face in mesh.polygons:
        face.use_smooth = True
    return mesh


def rest_material():
    mat, n, bsdf = _new_material("Celadon chopstick rest")
    obj = n.new("ShaderNodeTexCoord").outputs["Object"]
    tone = n.noise(obj, 80.0, 3.0).outputs["Fac"]
    n.link(n.mix(n.map_range(tone, 0.3, 0.7), (0.020, 0.014, 0.011, 1), (0.045, 0.026, 0.016, 1)),
           bsdf.inputs["Base Color"])
    bsdf.inputs["Roughness"].default_value = 0.12
    bsdf.inputs["Subsurface Weight"].default_value = 0.15
    bsdf.inputs["Subsurface Radius"].default_value = (0.002, 0.002, 0.0015)
    n.link(n.bump(n.noise(obj, 300.0, 2.0).outputs["Fac"], 0.3, 0.0001), bsdf.inputs["Normal"])
    return mat


def chopstick_rest(collection, location, angle):
    """Small pillow-shaped hashioki: a squashed, waisted capsule."""
    bm_mesh = bpy.data.meshes.new("Hashioki")
    verts, faces = [], []
    seg_u, seg_v = 32, 20
    length, rad = 0.042, 0.0065
    for i in range(seg_u + 1):
        u = i / seg_u
        x = (u - 0.5) * length
        waist = 1.0 - 0.28 * sin(pi * u) ** 2
        end = sin(pi * u) ** 0.35
        for j in range(seg_v):
            a = tau * j / seg_v
            r = rad * waist * end
            verts.append((x, r * cos(a), rad * 0.95 + r * 0.82 * sin(a)))
    for i in range(seg_u):
        for j in range(seg_v):
            a, b = i * seg_v + j, i * seg_v + (j + 1) % seg_v
            faces.append((a, b, b + seg_v, a + seg_v))
    bm_mesh.from_pydata(verts, [], faces)
    bm_mesh.update()
    for face in bm_mesh.polygons:
        face.use_smooth = True
    obj = bpy.data.objects.new("Chopstick rest", bm_mesh)
    collection.objects.link(obj)
    obj.location = location
    obj.rotation_euler = (0, 0, angle)
    # Seat it on the table: its lowest point is ~0.0007 below zero after the
    # ends close, so lift it.
    obj.location.z += 0.0001
    bm_mesh.materials.append(rest_material())
    return obj


def chopsticks(collection, rest_loc=(0.138, -0.052, 0.0), yaw_deg=103.0):
    """A pair laid on a hashioki: tips on the rest, butts on the oak, pointing
    toward the bowl as a leading line."""
    mat = chopstick_material()
    rest_loc = Vector(rest_loc)
    yaw = radians(yaw_deg)          # direction from butt to tip
    rest = chopstick_rest(collection, rest_loc, yaw + pi / 2)
    rest_top = 0.0065 * 0.95 + 0.0065 * 0.72 * 0.82
    objs = [rest]
    direction = Vector((cos(yaw), sin(yaw), 0.0))
    side = Vector((-direction.y, direction.x, 0.0))
    length, contact = 0.225, 0.17
    for k, offset in enumerate((-0.0045, 0.0045)):
        mesh = chopstick_mesh(f"Chopstick {k + 1}", length=length)
        mesh.materials.append(mat)
        obj = bpy.data.objects.new(f"Chopstick {k + 1}", mesh)
        collection.objects.link(obj)
        u = contact / length
        r_contact = 0.0034 + (0.0011 - 0.0034) * u ** 1.15
        point = rest_loc + side * offset * (1.0 + 0.4 * k)
        point.z = rest_top + r_contact
        # Splay the pair a little, as if put down by hand.
        d = (direction + side * (0.02 if k else -0.015)).normalized()
        flat = contact * (1.0 - ((point.z - 0.0034) / contact) ** 2) ** 0.5
        butt = point - d * flat
        butt.z = 0.0034
        obj.location = butt
        obj.rotation_euler = (point - butt).normalized().to_track_quat("X", "Z").to_euler()
        objs.append(obj)
    return objs


def shino_material():
    """Pale, fatty shino-style glaze with orange flashing and iron specks."""
    mat, n, bsdf = _new_material("Shino glaze")
    obj = n.new("ShaderNodeTexCoord").outputs["Object"]
    tone = n.noise(obj, 35.0, 4.0, 0.6).outputs["Fac"]
    col = n.mix(n.map_range(tone, 0.35, 0.7), (0.62, 0.58, 0.50, 1), (0.55, 0.40, 0.28, 1))
    speck = n.map_range(n.voronoi(obj, 300.0).outputs["Distance"], 0.12, 0.04)
    col = n.mix(n.math("MULTIPLY", speck, 0.8), col, (0.12, 0.07, 0.04, 1))
    n.link(col, bsdf.inputs["Base Color"])
    n.link(n.map_range(tone, 0.3, 0.7, 0.18, 0.32), bsdf.inputs["Roughness"])
    n.link(n.bump(n.noise(obj, 150.0, 3.0).outputs["Fac"], 0.5, 0.0003), bsdf.inputs["Normal"])
    return mat


def teacup(collection, location=(-0.150, 0.175, 0.0)):
    """Yunomi in the background, well out of focus, for depth."""
    profile = ((0.0, 0.0080), (0.020, 0.0084), (0.029, 0.0105), (0.0315, 0.020),
               (0.0330, 0.060), (0.0337, 0.0760), (0.0345, 0.0776), (0.0356, 0.0774),
               (0.0362, 0.0755), (0.0358, 0.060), (0.0345, 0.020), (0.0322, 0.0075),
               (0.0290, 0.0040), (0.0282, 0.0), (0.0228, 0.0), (0.0220, 0.0032),
               (0.0, 0.0042))
    mesh = _revolve(profile, "Yunomi", sides=96)
    cup = bpy.data.objects.new("Teacup (background)", mesh)
    collection.objects.link(cup)
    cup.location = location
    mesh.materials.append(shino_material())
    return cup


# ----------------------------------------------------------------- lights
def point_at(obj, target):
    obj.rotation_euler = (Vector(target) - obj.location).to_track_quat("-Z", "Y").to_euler()


def matte_card(collection, name, location, target, size, colour, emission=0.0):
    """Camera-invisible card: black flag (negative fill) or white bounce."""
    mesh = bpy.data.meshes.new(name)
    w, h = size
    mesh.from_pydata([(-w / 2, -h / 2, 0), (w / 2, -h / 2, 0), (w / 2, h / 2, 0), (-w / 2, h / 2, 0)],
                     [], [(0, 1, 2, 3)])
    obj = bpy.data.objects.new(name, mesh)
    collection.objects.link(obj)
    obj.location = location
    point_at(obj, target)
    obj.visible_camera = False
    obj.visible_shadow = False
    mat, n, bsdf = _new_material(name)
    bsdf.inputs["Base Color"].default_value = colour
    bsdf.inputs["Roughness"].default_value = 0.9
    if emission:
        bsdf.inputs["Emission Color"].default_value = colour
        bsdf.inputs["Emission Strength"].default_value = emission
    mesh.materials.append(mat)
    return obj


def window_world(scene):
    """Dim room with a brighter window side: a soft gradient that gives the
    glaze something room-like to reflect, never a visible backdrop."""
    world = bpy.data.worlds.new("Window-lit room")
    scene.world = world
    tree = world.node_tree
    for node in list(tree.nodes):
        tree.nodes.remove(node)
    n = Nodes(tree)
    out = n.new("ShaderNodeOutputWorld")
    bg = n.new("ShaderNodeBackground")
    n.links.new(bg.outputs[0], out.inputs["Surface"])
    coords = n.new("ShaderNodeTexCoord").outputs["Generated"]
    sep = n.new("ShaderNodeSeparateXYZ")
    n.link(coords, sep.inputs[0])
    # Generated coordinates on a world are the view direction.
    up = sep.outputs["Z"]
    # Window direction: back-left (+Y, -X), slightly raised.
    win_dir = Vector((-0.62, 0.70, 0.35)).normalized()
    facing = n.vmath("DOT_PRODUCT", coords, tuple(win_dir))
    sky = n.ramp(n.math("ADD", n.math("MULTIPLY", up, 0.5), 0.5),
                 [(0.0, (0.05, 0.04, 0.032)), (0.5, (0.16, 0.14, 0.12)), (1.0, (0.30, 0.30, 0.30))])
    glow = n.map_range(facing, 0.35, 0.95, 0.0, 1.0)
    colour = n.mix(n.math("MULTIPLY", glow, 0.85), sky, (1.6, 1.62, 1.66, 1.0))
    n.link(colour, bg.inputs["Color"])
    bg.inputs["Strength"].default_value = 0.22
    return world


def window_pane(collection, key):
    """What the glaze reflects instead of the bare area light: the same
    window, but with mullions, a sheer curtain's soft folds and a brighter
    sky at the top. Seen only by glossy/transmission rays, so it neither
    blocks nor doubles the key's illumination."""
    w, h = key.data.size, key.data.size_y
    mesh = bpy.data.meshes.new("Window reflection")
    mesh.from_pydata([(-w / 2, -h / 2, 0), (w / 2, -h / 2, 0), (w / 2, h / 2, 0), (-w / 2, h / 2, 0)],
                     [], [(0, 1, 2, 3)])
    pane = bpy.data.objects.new("Window reflection", mesh)
    collection.objects.link(pane)
    pane.matrix_world = key.matrix_basis @ Matrix.Translation((0.0, 0.0, 0.001))
    for flag in ("visible_camera", "visible_diffuse", "visible_shadow", "visible_volume_scatter"):
        setattr(pane, flag, False)
    key.visible_glossy = False
    key.visible_transmission = False
    mat, n, bsdf = _new_material("Window reflection")
    tree = mat.node_tree
    tree.nodes.remove(bsdf)
    emit = n.new("ShaderNodeEmission")
    out = next(node for node in n.nodes if node.type == "OUTPUT_MATERIAL")
    n.links.new(emit.outputs[0], out.inputs["Surface"])
    obj = n.new("ShaderNodeTexCoord").outputs["Object"]
    sep = n.new("ShaderNodeSeparateXYZ")
    n.link(obj, sep.inputs[0])
    x, y = sep.outputs["X"], sep.outputs["Y"]
    # Sky brighter toward the top of the window.
    grad = n.map_range(y, -h / 2, h / 2, 0.7, 1.15, "LINEAR")
    # Sheer curtain folds: soft vertical bands.
    folds = n.noise(n.scale(obj, (9.0, 0.4, 1.0)), 1.0, 2.0, 0.5).outputs["Fac"]
    grad = n.math("MULTIPLY", grad, n.map_range(folds, 0.3, 0.7, 0.8, 1.15))
    # Mullions: one vertical, one horizontal bar, 3.5 cm.
    bar_v = n.map_range(n.math("ABSOLUTE", x), 0.016, 0.019, 0.0, 1.0)
    bar_h = n.map_range(n.math("ABSOLUTE", n.math("SUBTRACT", y, 0.08)), 0.016, 0.019, 0.0, 1.0)
    frame = n.math("MULTIPLY", bar_v, bar_h)
    edge_x = n.map_range(n.math("ABSOLUTE", x), w / 2 - 0.03, w / 2 - 0.025, 1.0, 0.0)
    edge_y = n.map_range(n.math("ABSOLUTE", y), h / 2 - 0.03, h / 2 - 0.025, 1.0, 0.0)
    frame = n.math("MULTIPLY", frame, n.math("MULTIPLY", edge_x, edge_y))
    strength = n.math("MULTIPLY", n.math("MULTIPLY", grad, frame), key.data.energy / (w * h))
    n.link(strength, emit.inputs["Strength"])
    emit.inputs["Color"].default_value = tuple(key.data.color) + (1.0,)
    mesh.materials.append(mat)
    return pane


def lights(collection):
    """Window key from behind-left, a warm bounce card front-right and a
    gentle overhead room fill. Area light power is in watts at metre scale."""
    made = []
    for name, location, power, size, colour, target in (
        # 1.1 x 0.8 m window, ~1.1 m behind-left of the bowl, a little above.
        ("Window key", (-0.52, 0.80, 0.62), 120.0, (1.1, 0.8), (1.0, 0.97, 0.93), (0.0, 0.0, 0.04)),
        # Reflector card bouncing the window back into the front of the bowl.
        ("Bounce card", (0.42, -0.36, 0.14), 5.0, (0.45, 0.35), (1.0, 0.93, 0.84), (0.0, 0.0, 0.05)),
        # Ceiling bounce of a room: very large, very soft, weak.
        ("Room fill", (0.15, 0.10, 1.6), 4.0, (1.6, 1.6), (0.95, 0.96, 1.0), (0.0, 0.0, 0.0)),
    ):
        data = bpy.data.lights.new(name, "AREA")
        data.shape = "RECTANGLE"
        data.size, data.size_y = size
        data.energy = power
        data.color = colour
        obj = bpy.data.objects.new(name, data)
        collection.objects.link(obj)
        obj.location = location
        point_at(obj, target)
        obj.visible_camera = False
        if name == "Room fill":
            # The room reads through the world gradient, not a ceiling panel.
            obj.visible_glossy = False
        made.append(obj)
    made.append(window_pane(collection, made[0]))
    # Black flag on the shadow side (negative fill) for shape.
    made.append(matte_card(collection, "Negative fill flag", (0.46, 0.22, 0.16), (0.0, 0.0, 0.05),
                           (0.5, 0.4), (0.01, 0.01, 0.01, 1)))
    return made


# ----------------------------------------------------------------- camera
def camera(collection, scene, focus):
    data = bpy.data.cameras.new("85mm f/4")
    cam = bpy.data.objects.new("Food camera 85mm", data)
    collection.objects.link(cam)
    data.lens = 85.0
    data.sensor_fit = "HORIZONTAL"
    data.sensor_width = 36.0
    data.clip_start = 0.05
    data.clip_end = 20.0
    target = Vector((0.0, 0.004, 0.045))
    # ~34 degrees above the table, slightly from the right.
    elevation, azimuth, distance = radians(34.0), radians(-100.0), 0.78
    cam.location = target + distance * Vector((cos(elevation) * cos(azimuth),
                                               cos(elevation) * sin(azimuth), sin(elevation)))
    point_at(cam, target)
    focus_obj = bpy.data.objects.new("Focus - front of noodles", None)
    focus_obj.empty_display_size = 0.02
    collection.objects.link(focus_obj)
    focus_obj.location = focus
    data.dof.use_dof = True
    data.dof.focus_object = focus_obj
    data.dof.aperture_fstop = 2.8
    data.dof.aperture_blades = 7
    data.dof.aperture_rotation = radians(10.0)
    scene.camera = cam
    return cam


# ------------------------------------------------------------------ render
def render_settings(scene, samples=256, size=(1500, 1000)):
    scene.render.engine = "CYCLES"
    cy = scene.cycles
    cy.samples = samples
    cy.preview_samples = 32
    cy.use_adaptive_sampling = True
    cy.adaptive_threshold = 0.012
    cy.use_denoising = True
    cy.use_preview_denoising = True
    cy.max_bounces = 12
    cy.diffuse_bounces = 4
    cy.glossy_bounces = 6
    cy.transmission_bounces = 12
    cy.transparent_max_bounces = 16
    cy.volume_bounces = 2
    cy.sample_clamp_indirect = 4.0
    cy.caustics_reflective = False
    cy.caustics_refractive = False
    cy.blur_glossy = 0.5
    scene.render.resolution_x, scene.render.resolution_y = size
    scene.render.resolution_percentage = 100
    scene.render.film_transparent = False
    scene.view_settings.view_transform = "AgX"
    scene.view_settings.look = "AgX - Medium High Contrast"
    scene.view_settings.exposure = -0.45
    scene.view_settings.gamma = 1.0
    scene.render.image_settings.file_format = "PNG"
    scene.render.image_settings.color_depth = "8"


def lens_finish(scene, vignette=0.38, grain=0.035):
    """Compositor finish of a real lens and sensor: soft optical vignetting
    and fine luminance grain. Applied in scene-linear before AgX."""
    tree = bpy.data.node_groups.new("Lens and grain - " + scene.name, "CompositorNodeTree")
    tree.interface.new_socket("Image", in_out="OUTPUT", socket_type="NodeSocketColor")
    n = Nodes(tree)
    layers = n.new("CompositorNodeRLayers")
    # Render Layers otherwise points at whichever scene was active.
    layers.scene = scene
    out = n.new("NodeGroupOutput")
    coords = n.new("CompositorNodeImageCoordinates")
    n.link(layers.outputs["Image"], coords.inputs["Image"])
    # Uniform coordinates are centred and aspect-correct.
    radius = n.vmath("LENGTH", coords.outputs["Uniform"])
    falloff = n.map_range(radius, 0.45, 1.9, 1.0, 1.0 - vignette)
    grain_node = n.new("ShaderNodeTexWhiteNoise", noise_dimensions="2D")
    n.link(coords.outputs["Pixel"], grain_node.inputs["Vector"])
    jitter = n.math("ADD", 1.0 - grain / 2, n.math("MULTIPLY", grain_node.outputs["Value"], grain))
    gain = n.math("MULTIPLY", falloff, jitter)
    mix = n.new("ShaderNodeMix", data_type="RGBA", blend_type="MULTIPLY")
    n.link(1.0, mix.inputs[0])
    n.link(layers.outputs["Image"], mix.inputs[6])
    comb = n.new("ShaderNodeCombineXYZ")
    for i in range(3):
        n.link(gain, comb.inputs[i])
    n.link(comb.outputs[0], mix.inputs[7])
    n.link(mix.outputs[2], out.inputs[0])
    scene.compositing_node_group = tree
    scene.render.use_compositing = True
    # CPU works everywhere, including headless builds without a GPU context.
    scene.render.compositor_device = "CPU"
    return tree


def build_set(scene, collection):
    """Table, lights, cards, world, camera and props around a bowl at origin."""
    window_world(scene)
    table(collection)
    lights(collection)
    chopsticks(collection)
    teacup(collection)
    camera(collection, scene, focus=(0.0, -0.045, 0.06))
    render_settings(scene)
    lens_finish(scene)


# ------------------------------------------------------------- garnish
def scallion_material():
    mat, n, bsdf = _new_material("Scallion")
    obj = n.new("ShaderNodeTexCoord").outputs["Object"]
    attr = n.attr("scallion_layer")
    tone = n.noise(obj, 900.0, 2.0).outputs["Fac"]
    green = n.mix(n.map_range(tone, 0.3, 0.7), (0.035, 0.12, 0.012, 1), (0.065, 0.18, 0.02, 1))
    pale = (0.42, 0.50, 0.20, 1)
    n.link(n.mix(n.map_range(attr, 0.3, 0.85, 0.0, 0.8, "LINEAR"), green, pale), bsdf.inputs["Base Color"])
    bsdf.inputs["Roughness"].default_value = 0.25
    bsdf.inputs["Subsurface Weight"].default_value = 0.5
    bsdf.inputs["Subsurface Radius"].default_value = (0.0006, 0.0012, 0.0004)
    bsdf.inputs["Subsurface Scale"].default_value = 1.0
    return mat


def _scallion_ring(verts, faces, layer, matrix, r_out, r_in, depth, rng,
                   sweep=tau, squash=1.0, tag=0.0, sides=18):
    """One closed scallion layer: an annulus (or a broken arc with end caps),
    elliptic where the knife cut on a slant. 'tag' tints inner layers paler."""
    base = len(verts)
    full = sweep >= tau - 1e-6
    count = sides if full else max(4, int(sides * sweep / tau)) + 1
    phase = rng.random() * tau
    for z in (-depth / 2, depth / 2):
        for r in (r_out, r_in):
            for k in range(count):
                a = phase + sweep * k / (count if full else count - 1)
                wob = 1.0 + 0.06 * sin(3 * a + r * 1200.0)
                verts.append(matrix @ Vector((r * wob * cos(a) * squash, r * wob * sin(a), z)))
                layer.append(tag + (0.35 if r == r_in else 0.0))
    bo, bi, to, ti = (base + i * count for i in range(4))
    spans = count if full else count - 1
    for k in range(spans):
        j = (k + 1) % count
        faces.append((bo + k, bo + j, to + j, to + k))      # outer skin
        faces.append((bi + j, bi + k, ti + k, ti + j))      # inner skin
        faces.append((to + k, to + j, ti + j, ti + k))      # cut face, top
        faces.append((bo + j, bo + k, bi + k, bi + j))      # cut face, bottom
    if not full:
        e = count - 1
        faces.append((bo, to, ti, bi))
        faces.append((bo + e, bi + e, ti + e, to + e))


def _scallion_slice(verts, faces, layer, matrix, rng, size=None, depth=None):
    """A slice of spring onion: thin green outer layer, sometimes a nested
    paler inner layer, now and then broken into an arc."""
    size = size or rng.uniform(0.0030, 0.0045)
    depth = depth or rng.uniform(0.0008, 0.0016)
    squash = rng.uniform(1.0, 1.3)
    sweep = tau if rng.random() > 0.18 else rng.uniform(radians(150), radians(290))
    wall = size * rng.uniform(0.2, 0.3)
    _scallion_ring(verts, faces, layer, matrix, size, size - wall, depth, rng, sweep, squash, 0.0)
    if rng.random() < 0.5 and sweep >= tau - 1e-6:
        inner = size - wall * rng.uniform(1.4, 1.9)
        _scallion_ring(verts, faces, layer, matrix, inner, inner - wall * 0.8, depth * 0.95,
                       rng, tau, squash, 0.5)


def scatter_scallions(collection, target, count=38, seed=3, table_count=3):
    """Drop sliced scallion rings onto the baked pile (static geometry)."""
    import random
    from mathutils.bvhtree import BVHTree
    rng = random.Random(seed)
    depsgraph = bpy.context.evaluated_depsgraph_get()
    bvh = BVHTree.FromObject(target, depsgraph)
    world = target.matrix_world
    inv = world.inverted()
    verts, faces, layer = [], [], []
    placed = 0
    tries = 0
    while placed < count and tries < count * 40:
        tries += 1
        r = 0.058 * rng.random() ** 0.6
        a = rng.random() * tau
        # Favour the front-right of the pile, where the camera looks.
        if rng.random() < 0.45:
            a = rng.uniform(radians(-150), radians(-20))
        origin = inv @ Vector((r * cos(a), r * sin(a), 0.2))
        hit, normal, _, _ = bvh.ray_cast(origin, Vector((0, 0, -1)))
        if hit is None or normal.z < 0.25:
            continue
        hit, normal = world @ hit, (world.to_3x3() @ normal).normalized()
        size = rng.uniform(0.0030, 0.0045)
        depth = rng.uniform(0.0010, 0.0018)
        tilt = normal.lerp(Vector((rng.uniform(-1, 1), rng.uniform(-1, 1), 1)).normalized(), 0.35)
        rot = tilt.to_track_quat("Z", "Y").to_matrix().to_4x4() @ Matrix.Rotation(rng.random() * tau, 4, "Z")
        m = Matrix.Translation(hit + normal * depth * 0.35) @ rot
        _scallion_slice(verts, faces, layer, m, rng, size, depth)
        placed += 1
    # A couple fallen onto the table in front of the bowl.
    for _ in range(table_count):
        a = rng.uniform(radians(-140), radians(-50))
        r = rng.uniform(0.115, 0.16)
        size = rng.uniform(0.003, 0.004)
        depth = 0.0012
        m = Matrix.Translation((r * cos(a), r * sin(a), depth / 2)) @ Matrix.Rotation(rng.random() * tau, 4, "Z")
        _scallion_slice(verts, faces, layer, m, rng, size, depth)
    mesh = bpy.data.meshes.new("Scallion slices")
    mesh.from_pydata([tuple(v) for v in verts], [], faces)
    attr = mesh.attributes.new("scallion_layer", "FLOAT", "POINT")
    attr.data.foreach_set("value", layer)
    mesh.update()
    obj = bpy.data.objects.new("Garnish - scallion slices", mesh)
    collection.objects.link(obj)
    mesh.materials.append(scallion_material())
    return obj


# ------------------------------------------------------- finished preview
def make_preview_scene(scene, noodles, snapshot_mesh, samples=256, size=(1500, 1000)):
    """Static 'Finished Preview' scene: every set object plus a baked noodle
    snapshot and the scallion garnish scattered over it."""
    snapshot = bpy.data.objects.new("Udon - static frame 100 preview", snapshot_mesh)
    preview = bpy.data.scenes.new("Noodle Box - Finished Preview")
    for obj in scene.objects:
        if obj != noodles:
            preview.collection.objects.link(obj)
    preview.collection.objects.link(snapshot)
    preview.camera = scene.camera
    preview.world = scene.world
    preview.unit_settings.system = "METRIC"
    render_settings(preview, samples, size)
    lens_finish(preview)
    garnish = bpy.data.collections.new("Garnish")
    preview.collection.children.link(garnish)
    window = bpy.context.window
    previous = window.scene if window else None
    if window:
        window.scene = preview
    scatter_scallions(garnish, snapshot)
    if window and previous:
        window.scene = previous
    return preview, snapshot
