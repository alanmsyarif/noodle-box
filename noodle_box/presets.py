"""Art-directed noodle types in metres, independent of performance shortcuts."""

# How each cooked noodle looks. Colours are linear albedo. Everything here is
# surface-only: shader values for materials.build() plus the display-shape
# inputs in SURFACE_INPUTS. None of it changes the simulation.
#
#   squareness   cross-section from round (0) towards a rounded square (1)
#   waviness     crinkle amplitude, as a fraction of the diameter
#   thickness_var  slow thickness variation along the strand (fraction)
#   end_softness   fillet on the cut ends, as a fraction of the radius
LOOKS = {
    # Plump, off-white, faintly warm; glossy-wet starch gel, strongly
    # translucent; knife-cut rounded-square section with uneven thickness.
    "udon": dict(colour=(0.70, 0.60, 0.42, 1.0), colour_b=(0.78, 0.69, 0.52, 1.0),
                 tone=0.45, roughness=0.34, coat=(0.55, 1.0), coat_roughness=0.05,
                 sss=1.0, sss_radius=(1.0, 0.86, 0.66), sss_scale=0.003,
                 sss_anisotropy=0.3, blotch=0.45, striation=0.30, dimples=0.45,
                 bubbles=0.35, grit=0.10, bump_distance=0.00016,
                 squareness=0.55, waviness=0.0, thickness_var=0.05, end_softness=0.4),
    # Kansui-yellow alkaline noodle: springy, satin rather than wet-glossy,
    # gently crinkled.
    "ramen": dict(colour=(0.62, 0.40, 0.10, 1.0), colour_b=(0.70, 0.48, 0.15, 1.0),
                  tone=0.4, roughness=0.36, coat=(0.30, 0.65), coat_roughness=0.16,
                  sss=0.85, sss_radius=(1.0, 0.62, 0.22), sss_scale=0.0016,
                  sss_anisotropy=0.2, blotch=0.25, striation=0.35, dimples=0.15,
                  bubbles=0.12, grit=0.12, bump_distance=0.00004, lumps=0.2,
                  squareness=0.3, waviness=0.22, thickness_var=0.03, end_softness=0.35),
    # Durum pasta: golden, semi-translucent, bronze-die micro-roughness.
    "spaghetti": dict(colour=(0.70, 0.48, 0.17, 1.0), colour_b=(0.78, 0.56, 0.22, 1.0),
                      tone=0.35, roughness=0.46, coat=(0.25, 0.7), coat_roughness=0.12,
                      sss=0.85, sss_radius=(1.0, 0.6, 0.22), sss_scale=0.0010,
                      sss_anisotropy=0.2, blotch=0.2, striation=0.45, dimples=0.1,
                      bubbles=0.05, grit=0.7, bump_distance=0.00006,
                      squareness=0.0, waviness=0.0, thickness_var=0.02, end_softness=0.3),
    # Buckwheat: grey-brown, more matte, dark husk specks; knife-cut square.
    "soba": dict(colour=(0.14, 0.112, 0.085, 1.0), colour_b=(0.175, 0.142, 0.11, 1.0),
                 tone=0.5, roughness=0.5, coat=(0.2, 0.55), coat_roughness=0.16,
                 sss=0.35, sss_radius=(1.0, 0.8, 0.62), sss_scale=0.0008,
                 sss_anisotropy=0.1, blotch=0.3, striation=0.35, dimples=0.2,
                 bubbles=0.05, grit=0.35, specks=0.55,
                 speck_colour=(0.03, 0.022, 0.016, 1.0), bump_distance=0.00003, lumps=0.25,
                 squareness=0.7, waviness=0.0, thickness_var=0.04, end_softness=0.25),
    # Fresh rice noodle sheet: milky, very translucent, slick.
    "rice_ribbon": dict(colour=(0.86, 0.86, 0.82, 1.0), colour_b=(0.90, 0.90, 0.86, 1.0),
                        tone=0.3, roughness=0.24, coat=(0.7, 1.0), coat_roughness=0.03,
                        sss=1.0, sss_radius=(1.0, 0.92, 0.8), sss_scale=0.0025,
                        sss_anisotropy=0.4, transmission=0.12, blotch=0.35, striation=0.15,
                        dimples=0.3, bubbles=0.4, grit=0.05, bump_distance=0.00004,
                        squareness=0.65, waviness=0.0, thickness_var=0.06, end_softness=0.18),
    # Egg pasta ribbon: egg-yellow, slightly rough, less translucent.
    "fettuccine": dict(colour=(0.70, 0.45, 0.12, 1.0), colour_b=(0.77, 0.53, 0.17, 1.0),
                       tone=0.35, roughness=0.5, coat=(0.15, 0.5), coat_roughness=0.2,
                       sss=0.8, sss_radius=(1.0, 0.58, 0.2), sss_scale=0.0012,
                       sss_anisotropy=0.2, blotch=0.25, striation=0.3, dimples=0.15,
                       bubbles=0.06, grit=0.6, bump_distance=0.00006,
                       squareness=0.6, waviness=0.0, thickness_var=0.04, end_softness=0.2),
}

# Display-only solver inputs driven by a look (socket name -> look key).
SURFACE_INPUTS = {"Profile Squareness": "squareness", "Waviness": "waviness",
                  "Thickness Variation": "thickness_var", "End Softness": "end_softness"}

NOODLE_TYPES = {
    "udon": dict(label="Udon", description="Thick, pale and soft; best for live playback",
                 length=0.24, radius=0.005, count=32, stiffness=0.3, cohesion=0.16,
                 aspect=1.0, **LOOKS["udon"]),
    "ramen": dict(label="Ramen", description="Medium egg noodles with a firmer bend",
                  length=0.22, radius=0.0025, count=24, stiffness=0.5, cohesion=0.12,
                  aspect=1.0, **LOOKS["ramen"]),
    "spaghetti": dict(label="Spaghetti", description="Fine round pasta; detailed, more expensive to solve",
                      length=0.24, radius=0.0012, count=12, stiffness=0.65, cohesion=0.08,
                      aspect=1.0, **LOOKS["spaghetti"]),
    "soba": dict(label="Soba", description="Slender buckwheat noodles with a darker, matte surface",
                 length=0.20, radius=0.0018, count=18, stiffness=0.55, cohesion=0.07,
                 aspect=1.0, **LOOKS["soba"]),
    "rice_ribbon": dict(label="Rice Ribbon", description="Wide, pale, flexible ribbons; approximate round collision envelope",
                        length=0.18, radius=0.005, count=20, stiffness=0.18, cohesion=0.22,
                        aspect=0.20, **LOOKS["rice_ribbon"]),
    "fettuccine": dict(label="Fettuccine", description="Golden flat pasta; approximate round collision envelope",
                       length=0.24, radius=0.0035, count=20, stiffness=0.42, cohesion=0.12,
                       aspect=0.28, **LOOKS["fettuccine"]),
}

TYPE_ITEMS = tuple((key, value["label"], value["description"])
                   for key, value in NOODLE_TYPES.items())


def type_material(key):
    """The type's cooked-noodle material, rebuilt if made by an older add-on."""
    from . import materials
    preset = NOODLE_TYPES[key]
    return materials.ensure("Noodle Box - " + preset["label"], LOOKS[key], tag=key)


def surface_values(key):
    """Display-shape inputs for a type; they never affect the simulation."""
    return {name: LOOKS[key][field] for name, field in SURFACE_INPUTS.items()}


def apply_type(obj, key, scene, use_suggested_count=True):
    from . import solver
    preset = NOODLE_TYPES[key]  # Validate before changing the object.
    modifier = solver.noodle_modifier(obj)
    ng = modifier.node_group
    values = {
        "Noodle Length": preset["length"], "Noodle Radius": preset["radius"],
        "Length Variation": 0.12, "Spawn Coil": 1.0, "Fill Diameter": 0.14,
        "Start Height": 0.12, "Gravity": 9.81, "Damping": 0.03,
        "Contact Damping": 1.0, "Stiffness": preset["stiffness"],
        "Cohesion": preset["cohesion"], "Self Collision": 1.0, "Friction": 0.9,
        "Profile Faces": 8, "Profile Aspect": preset["aspect"],
        "Smooth Surface": True,
        "Noodle Material": type_material(key), "Adaptive Substeps": True, "Iterations": 2,
    }
    if use_suggested_count:
        values["Noodle Count"] = preset["count"]
    for name, value in values.items():
        solver.set_input(obj, ng, name, value)
    sockets = {s.name for s in ng.interface.items_tree if s.item_type == "SOCKET"}
    for name, value in surface_values(key).items():
        if name in sockets:  # absent on graphs built before these inputs existed
            solver.set_input(obj, ng, name, value)
    needed = solver.recommended_substeps(obj, ng, scene)
    solver.set_input(obj, ng, "Substeps", min(needed, solver.MAX_RUNTIME_SUBSTEPS))
    obj["noodle_type"] = key
    obj["noodle_preset"] = "custom"
    return needed
