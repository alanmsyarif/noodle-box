"""Cooked-noodle shader networks.

Real cooked noodles are a translucent starch gel under a thin film of
starchy water. What makes them read as food rather than plastic is mostly:

* light that soaks in and bleeds back out (random-walk subsurface), so thin
  edges and backlit strands glow and contact shadows are coloured, not grey;
* a separate glossy water film (Coat, IOR 1.33) whose strength wanders over
  the surface - some places are wet and sharp, others have dried to satin;
* low-frequency tone changes within and between strands;
* micro relief: extrusion striations running along the strand, pits and
  dimples, tiny trapped bubbles, flour/starch blotches, husk specks (soba).

All texture coordinates come from attributes the solver's surface section
writes, so detail is glued to the noodle and does not swim as it moves:

``nb_prof``  position in the strand's cross-section, metres (x across, y up);
``nb_along`` arc length from the strand's start, metres;
``nb_end``   distance to the nearer cut end, metres (0 on the cut faces);
``np_id``    per-strand integer identity.

Everything is set through guarded helpers: socket and property names move
between Blender releases, so missing ones are skipped rather than fatal. The
network uses only nodes EEVEE also supports; there random-walk subsurface
falls back to EEVEE's own subsurface and everything else is shared.
"""

import bpy

# Stamped on every material so a file saved by an older add-on rebuilds its
# noodle materials instead of silently reusing the old shader.
LOOK_VERSION = 3

ATTR_PROF = "nb_prof"
ATTR_ALONG = "nb_along"
ATTR_END = "nb_end"
ATTR_ID = "np_id"

# Fallback look used for anything a type does not override.
DEFAULT_LOOK = dict(
    colour=(0.80, 0.66, 0.40, 1.0),      # main body albedo, linear
    colour_b=(0.86, 0.72, 0.46, 1.0),    # second tone mixed in at low frequency
    tone=0.5,              # amount of low-frequency tone variation
    roughness=0.40,        # base (under the water film) roughness
    coat=(0.35, 0.9),      # water-film weight range, driest to wettest
    coat_roughness=0.08,
    sss=0.9,               # subsurface weight
    sss_radius=(1.0, 0.7, 0.4),
    sss_scale=0.0015,      # metres: mean free path scale
    sss_anisotropy=0.3,
    transmission=0.0,
    blotch=0.4,            # flour/starch patches: paler, rougher, less wet
    striation=0.4,         # longitudinal extrusion/knife marks (bump)
    dimples=0.3,           # shallow pits (bump)
    bubbles=0.2,           # tiny trapped air bubbles (bump + paler dots)
    grit=0.15,             # fine isotropic roughness, e.g. bronze-die pasta
    lumps=0.3,             # broad, low undulation of the dough (5 mm scale)
    micro=0.5,             # fine pores/skin texture under the film (bump)
    sparkle=0.5,           # tiny sharp ripples in the water film (coat bump)
    wrinkles=0.0,          # creases across the strand, as in rice sheets
    speck_size=0.3,        # largest speck radius, in speck-cell units
    specks=0.0,            # dark husk specks (soba), density 0..1
    speck_colour=(0.045, 0.032, 0.022, 1.0),
    bump_distance=0.00006,  # metres of relief at full height
    cap_dry=0.6,           # how much rougher and paler cut faces are
)


def look_for(look=None):
    merged = dict(DEFAULT_LOOK)
    merged.update(look or {})
    return merged


class _Builder:
    """Small, guarded node-wiring vocabulary for one shader tree."""

    def __init__(self, tree):
        self.tree = tree
        self.nodes = tree.nodes
        self.links = tree.links
        self.col = 0

    def node(self, idname, x, y, **props):
        n = self.nodes.new(idname)
        n.location = (x * 200, -y * 160)
        for key, value in props.items():
            if hasattr(n, key):
                try:
                    setattr(n, key, value)
                except (TypeError, ValueError):
                    pass
        return n

    @staticmethod
    def sock(collection, name):
        """By name or index; None when this Blender has no such socket."""
        if isinstance(name, int):
            return collection[name] if name < len(collection) else None
        for s in collection:
            if s.name == name and getattr(s, "enabled", True):
                return s
        for s in collection:
            if s.identifier == name:
                return s
        return None

    def put(self, node, name, value):
        s = self.sock(node.inputs, name)
        if s is None:
            return None
        if isinstance(value, bpy.types.NodeSocket):
            self.links.new(value, s)
        else:
            try:
                s.default_value = value
            except (TypeError, ValueError):
                pass
        return s

    def out(self, node, name):
        return self.sock(node.outputs, name)

    def math(self, op, a, b=None, x=0, y=0, clamp=False):
        n = self.node("ShaderNodeMath", x, y, operation=op, use_clamp=clamp)
        self.put(n, 0, a)
        if b is not None:
            self.put(n, 1, b)
        return n.outputs[0]

    def vmath(self, op, a, b=None, x=0, y=0):
        n = self.node("ShaderNodeVectorMath", x, y, operation=op)
        self.put(n, 0, a)
        if b is not None:
            if op == "SCALE":
                self.put(n, "Scale", b)
            else:
                self.put(n, 1, b)
        return self.out(n, "Value" if op in ("LENGTH", "DOT_PRODUCT", "DISTANCE") else "Vector")

    def remap(self, value, a, b, c, d, x=0, y=0, kind="SMOOTHSTEP"):
        n = self.node("ShaderNodeMapRange", x, y, interpolation_type=kind, clamp=True)
        self.put(n, "Value", value)
        self.put(n, "From Min", a)
        self.put(n, "From Max", b)
        self.put(n, "To Min", c)
        self.put(n, "To Max", d)
        return self.out(n, "Result")

    def mix(self, factor, a, b, x=0, y=0, data_type="RGBA", blend="MIX"):
        n = self.node("ShaderNodeMix", x, y, data_type=data_type, blend_type=blend,
                      clamp_factor=True)
        self.put(n, "Factor", factor)
        a_in = [s for s in n.inputs if s.name == "A" and s.enabled]
        b_in = [s for s in n.inputs if s.name == "B" and s.enabled]
        for sock, value in ((a_in, a), (b_in, b)):
            if not sock:
                continue
            if isinstance(value, bpy.types.NodeSocket):
                self.links.new(value, sock[0])
            else:
                sock[0].default_value = value
        return [s for s in n.outputs if s.name == "Result" and s.enabled][0]

    def attribute(self, name, x, y):
        n = self.node("ShaderNodeAttribute", x, y, attribute_type="GEOMETRY",
                      attribute_name=name)
        return n

    def noise(self, vector, scale, detail, x, y, roughness=0.5, w=None, dims="3D",
              distortion=0.0):
        n = self.node("ShaderNodeTexNoise", x, y, noise_dimensions=dims)
        self.put(n, "Vector", vector)
        self.put(n, "Scale", scale)
        self.put(n, "Detail", detail)
        self.put(n, "Roughness", roughness)
        self.put(n, "Distortion", distortion)
        if w is not None:
            self.put(n, "W", w)
        return n

    def voronoi(self, vector, scale, x, y, randomness=1.0, feature="F1"):
        n = self.node("ShaderNodeTexVoronoi", x, y, voronoi_dimensions="3D",
                      feature=feature, distance="EUCLIDEAN")
        self.put(n, "Vector", vector)
        self.put(n, "Scale", scale)
        self.put(n, "Randomness", randomness)
        return n


def build(material, look=None):
    """(Re)build *material*'s node tree as a cooked noodle with *look*."""
    look = look_for(look)
    if material.node_tree is None and hasattr(material, "use_nodes"):
        material.use_nodes = True  # pre-5.0 materials start without a tree
    tree = material.node_tree
    tree.nodes.clear()
    b = _Builder(tree)
    col3 = lambda c: tuple(c[:3]) + (1.0,)

    # ------------------------------------------------------------ inputs
    # Attribute outputs: Color, Vector, Factor, Alpha (names vary by release).
    prof = b.attribute(ATTR_PROF, 0, 0).outputs[1]
    along = b.attribute(ATTR_ALONG, 0, 2).outputs[2]
    end = b.attribute(ATTR_END, 0, 3).outputs[2]
    sid = b.attribute(ATTR_ID, 0, 4).outputs[2]

    # One uncorrelated random triple per strand.
    wn = b.node("ShaderNodeTexWhiteNoise", 1, 4, noise_dimensions="1D")
    b.put(wn, "W", sid)
    strand_rand = b.out(wn, "Value")
    strand_vec = b.out(wn, "Color")

    # Surface-anchored coordinates in millimetres; each strand is offset into
    # its own region of noise space so neighbours do not repeat each other.
    split = _sep(b, prof, 1, 0)
    mm = b.vmath("SCALE", _comb(b, split.outputs[0], split.outputs[1], along, 2, 1),
                 1000.0, 3, 1)
    uvw = b.vmath("ADD", mm, b.vmath("SCALE", strand_vec, 173.0, 2, 3), 4, 1)
    # Stretched along the strand for striations; the strand axis is z.
    streak = b.vmath("MULTIPLY", uvw, (1.0, 1.0, 0.06), 5, 0)

    # ------------------------------------------------------------ fields
    # Low-frequency tone, roughly 2-3 cm features.
    tone = b.out(b.noise(uvw, 0.035, 1.5, 6, 0), "Fac")
    # Wetness: patches about a centimetre across, plus a per-strand bias.
    wet_n = b.out(b.noise(uvw, 0.09, 2.0, 6, 1, roughness=0.55), "Fac")
    wet = b.math("ADD", b.remap(wet_n, 0.38, 0.62, 0.0, 1.0, 7, 1),
                 b.math("MULTIPLY", b.math("SUBTRACT", strand_rand, 0.5, 7, 2), 0.5, 8, 2),
                 8, 1, clamp=True)
    # Flour/starch blotches: a few millimetres, thresholded into patches.
    blotch_n = b.out(b.noise(uvw, 0.32, 3.0, 6, 2, roughness=0.6, distortion=0.3), "Fac")
    blotch = b.math("MULTIPLY", b.remap(blotch_n, 0.52, 0.68, 0.0, 1.0, 7, 3),
                    look["blotch"], 8, 3)
    # Longitudinal striations.
    stri = b.out(b.noise(streak, 2.2, 4.0, 6, 3, roughness=0.55), "Fac")
    # Shallow dimples: Voronoi cells about 1 mm, pits where F1 is small.
    vd = b.voronoi(uvw, 0.9, 6, 4)
    dimple = b.remap(b.out(vd, "Distance"), 0.0, 0.45, 1.0, 0.0, 7, 4)
    # Only some cells carry a pit, chosen by the cell's own random colour.
    dimple = b.math("MULTIPLY", dimple,
                    b.remap(_sep(b, b.out(vd, "Color"), 7, 5).outputs[0],
                            0.55, 0.7, 0.0, 1.0, 8, 5, kind="LINEAR"), 9, 4)
    # Tiny trapped bubbles, sparse.
    vb = b.voronoi(uvw, 2.6, 6, 6)
    bub = b.remap(b.out(vb, "Distance"), 0.0, 0.16, 1.0, 0.0, 7, 6)
    bub = b.math("MULTIPLY", bub,
                 b.remap(_sep(b, b.out(vb, "Color"), 7, 7).outputs[1],
                         0.8, 0.86, 0.0, 1.0, 8, 7, kind="LINEAR"), 9, 6)
    # Soft lumps a couple of millimetres across: uneven dough, swelling.
    lumps = b.out(b.noise(uvw, 0.18, 1.0, 6, 10, roughness=0.4), "Fac")
    # Fine skin texture (~0.2 mm) and the film's own tiny ripples, which give
    # the sharp micro-specular sparkle of a wet starch surface.
    micro = b.out(b.noise(uvw, 5.0, 2.0, 6, 12, roughness=0.5), "Fac")
    ripple = b.out(b.noise(b.vmath("ADD", uvw, (7.1, 3.3, 9.7), 5, 13), 2.2, 3.0, 6, 13,
                           roughness=0.6), "Fac")
    # Creases running mostly across the strand (folded rice sheet).
    crease_n = b.out(b.noise(b.vmath("MULTIPLY", uvw, (0.25, 0.25, 1.0), 5, 14), 0.35, 2.0,
                             6, 14, roughness=0.5, distortion=0.4), "Fac")
    crease = b.math("SUBTRACT", 1.0, b.math("ABSOLUTE", b.math("MULTIPLY", b.math(
        "SUBTRACT", crease_n, 0.5, 7, 14), 9.0, 8, 14), None, 9, 14), 10, 14, clamp=True)
    # Only here and there, where the sheet happened to fold.
    crease = b.math("MULTIPLY", crease, b.remap(lumps, 0.5, 0.62, 0.0, 1.0, 9, 15), 11, 15)
    # Fine grit, isotropic.
    grit = b.out(b.noise(uvw, 9.0, 6.0, 6, 8, roughness=0.7), "Fac")
    # Husk specks: small dark flecks of varying size and density.
    vs = b.voronoi(b.vmath("ADD", uvw, (31.7, 11.3, 5.9), 5, 9), 2.6, 6, 9)
    vs_rand = _sep(b, b.out(vs, "Color"), 7, 9)
    speck_size = b.math("MULTIPLY", b.math("POWER", vs_rand.outputs[0], 2.0, 7, 10), look["speck_size"], 8, 9)
    speck_keep = b.math("LESS_THAN", vs_rand.outputs[1], look["specks"], 8, 10)
    speck = b.math("MULTIPLY",
                   b.math("LESS_THAN", b.out(vs, "Distance"), speck_size, 9, 9),
                   speck_keep, 10, 9)
    # Cut faces: drier, rougher, more opaque-looking starch.
    cap = b.remap(end, 0.0, 0.00025, 1.0, 0.0, 6, 11, kind="LINEAR")

    # ------------------------------------------------------------ colour
    base = b.mix(b.math("ADD", b.math("MULTIPLY",
                                      b.math("SUBTRACT", tone, 0.5, 11, 0), 2.5 * look["tone"], 12, 0),
                        b.math("MULTIPLY", strand_rand, look["tone"], 12, 1), 13, 0, clamp=True),
                 col3(look["colour"]), col3(look["colour_b"]), 14, 0)
    pale = b.mix(0.5, col3(look["colour_b"]), (0.95, 0.93, 0.88, 1.0), 13, 2)
    # Starch blotches and bubbles read as paler, slightly chalky spots.
    base = b.mix(b.math("MAXIMUM", b.math("MULTIPLY", blotch, 0.55, 12, 3),
                        b.math("MULTIPLY", bub, look["bubbles"], 12, 4), 13, 3),
                 base, pale, 15, 1)
    base = b.mix(b.math("MULTIPLY", cap, look["cap_dry"] * 0.35, 14, 4), base, pale, 16, 1)
    base = b.mix(speck, base, col3(look["speck_colour"]), 17, 1)

    # ------------------------------------------------------------ roughness
    rough = b.math("ADD", look["roughness"] * 0.85,
                   b.math("MULTIPLY", b.math("SUBTRACT", 1.0, wet, 12, 6),
                          look["roughness"] * 0.3, 13, 6), 14, 6)
    rough = b.math("ADD", rough, b.math("MULTIPLY", blotch, 0.15, 13, 7), 15, 6)
    rough = b.math("ADD", rough, b.math("MULTIPLY", cap, look["cap_dry"] * 0.3, 14, 8), 16, 6)
    rough = b.math("ADD", rough, b.math("MULTIPLY", b.math("SUBTRACT", grit, 0.5, 14, 9),
                                        look["grit"] * 0.25, 15, 9), 17, 6, clamp=True)

    coat_lo, coat_hi = look["coat"]
    coat_w = b.math("MULTIPLY",
                    b.remap(wet, 0.0, 1.0, coat_lo, coat_hi, 13, 10, kind="LINEAR"),
                    b.math("SUBTRACT", 1.0, b.math("MAXIMUM",
                                                   b.math("MULTIPLY", blotch, 0.5, 13, 12),
                                                   b.math("MULTIPLY", cap, 0.8, 13, 13), 14, 12),
                           15, 12), 16, 10)
    coat_r = b.remap(wet, 0.0, 1.0, look["coat_roughness"] * 1.8,
                     look["coat_roughness"] * 0.6, 14, 11, kind="LINEAR")

    # ------------------------------------------------------------ relief
    def term(signal, weight, x, y, centre=0.0):
        s = signal if not centre else b.math("SUBTRACT", signal, centre, x - 1, y)
        return b.math("MULTIPLY", s, weight, x, y)
    height = b.math("ADD", term(stri, -look["striation"], 11, 14, 0.5),
                    term(dimple, -look["dimples"], 11, 15), 12, 14)
    height = b.math("ADD", height, term(bub, look["bubbles"] * 0.7, 11, 16), 13, 14)
    height = b.math("ADD", height, term(grit, look["grit"], 11, 17, 0.5), 14, 14)
    height = b.math("ADD", height, term(blotch, 0.25, 11, 18), 15, 14)
    height = b.math("ADD", height, term(speck, 0.3 * look["specks"], 11, 19), 16, 14)
    height = b.math("ADD", height, term(lumps, 1.2 * look["lumps"], 11, 20, 0.5), 17, 14)
    height = b.math("ADD", height, term(micro, 0.6 * look["micro"], 11, 21, 0.5), 18, 14)
    height = b.math("ADD", height, term(crease, -1.5 * look["wrinkles"], 11, 22), 19, 14)
    bump = b.node("ShaderNodeBump", 21, 14)
    b.put(bump, "Height", height)
    b.put(bump, "Strength", 1.0)
    b.put(bump, "Distance", look["bump_distance"])
    # The water film smooths micro relief; it follows only broad undulation.
    coat_height = b.math("ADD", b.math("MULTIPLY", stri, 0.25 * look["striation"], 15, 16),
                         b.math("MULTIPLY", lumps, 1.2 * look["lumps"], 15, 17), 16, 16)
    coat_height = b.math("ADD", coat_height,
                         b.math("MULTIPLY", ripple, 0.8 * look["sparkle"], 15, 18), 17, 16)
    coat_height = b.math("ADD", coat_height,
                         b.math("MULTIPLY", crease, -1.2 * look["wrinkles"], 15, 19), 18, 16)
    coat_bump = b.node("ShaderNodeBump", 21, 16)
    b.put(coat_bump, "Height", coat_height)
    b.put(coat_bump, "Strength", 1.0)
    b.put(coat_bump, "Distance", look["bump_distance"])

    # ------------------------------------------------------------ BSDF
    bsdf = b.node("ShaderNodeBsdfPrincipled", 23, 2)
    for prop, value in (("subsurface_method", "RANDOM_WALK"), ("distribution", "MULTI_GGX")):
        try:
            setattr(bsdf, prop, value)
        except (AttributeError, TypeError, ValueError):
            pass
    b.put(bsdf, "Base Color", base)
    b.put(bsdf, "Roughness", rough)
    b.put(bsdf, "IOR", 1.40)
    b.put(bsdf, "Subsurface Weight", look["sss"])
    b.put(bsdf, "Subsurface Radius", tuple(look["sss_radius"]))
    b.put(bsdf, "Subsurface Scale", look["sss_scale"])
    b.put(bsdf, "Subsurface IOR", 1.38)
    b.put(bsdf, "Subsurface Anisotropy", look["sss_anisotropy"])
    b.put(bsdf, "Transmission Weight", look["transmission"])
    b.put(bsdf, "Coat Weight", coat_w)
    b.put(bsdf, "Coat Roughness", coat_r)
    b.put(bsdf, "Coat IOR", 1.33)
    b.put(bsdf, "Normal", b.out(bump, "Normal"))
    b.put(bsdf, "Coat Normal", b.out(coat_bump, "Normal"))

    output = b.node("ShaderNodeOutputMaterial", 26, 2)
    b.links.new(bsdf.outputs[0], output.inputs["Surface"])

    material.diffuse_color = col3(look["colour"])
    material.roughness = float(look["roughness"])
    for prop, value in (("displacement_method", "BUMP"),):
        if hasattr(material, prop):
            setattr(material, prop, value)
    material["noodle_look_version"] = LOOK_VERSION
    return material


def _sep(b, vector, x, y):
    n = b.node("ShaderNodeSeparateXYZ", x, y)
    b.put(n, 0, vector)
    return n


def _comb(b, x_, y_, z_, x, y):
    n = b.node("ShaderNodeCombineXYZ", x, y)
    b.put(n, 0, x_)
    b.put(n, 1, y_)
    b.put(n, 2, z_)
    return n.outputs[0]


def ensure(name, look=None, tag=None):
    """Return the named noodle material, rebuilding it if it is out of date."""
    material = bpy.data.materials.get(name)
    if (material is not None and material.get("noodle_look_version") == LOOK_VERSION
            and material.get("noodle_type") == tag):
        return material
    if material is None:
        material = bpy.data.materials.new(name)
    build(material, look)
    if tag is not None:
        material["noodle_type"] = tag
    return material
