"""Art-directed noodle types in metres, independent of performance shortcuts.

Physical fields: length and radius in metres; modulus is the cooked noodle's
Young's modulus in pascals (it sets how far a strand droops under its own
weight, see solver.elastogravity_length); friction is the wet noodle-on-noodle
Coulomb coefficient; cohesion is starch stickiness. Density is
solver.NOODLE_DENSITY for every type.
"""

NOODLE_TYPES = {
    "udon": dict(label="Udon", description="Thick, pale and soft; best for live playback",
                 length=0.24, radius=0.005, count=32, modulus=100e3, friction=0.3, cohesion=0.16,
                 aspect=1.0, colour=(0.92, 0.82, 0.60, 1.0), roughness=0.3),
    "ramen": dict(label="Ramen", description="Medium egg noodles with a firmer bend",
                  length=0.22, radius=0.0025, count=24, modulus=250e3, friction=0.3, cohesion=0.12,
                  aspect=1.0, colour=(0.82, 0.56, 0.20, 1.0), roughness=0.28),
    "spaghetti": dict(label="Spaghetti", description="Fine round pasta; detailed, more expensive to solve",
                      length=0.24, radius=0.0012, count=12, modulus=2.0e6, friction=0.25, cohesion=0.08,
                      aspect=1.0, colour=(0.90, 0.64, 0.25, 1.0), roughness=0.32),
    "soba": dict(label="Soba", description="Slender buckwheat noodles with a darker, matte surface",
                 length=0.20, radius=0.0018, count=18, modulus=300e3, friction=0.35, cohesion=0.07,
                 aspect=1.0, colour=(0.27, 0.17, 0.10, 1.0), roughness=0.48),
    "rice_ribbon": dict(label="Rice Ribbon", description="Wide, pale, flexible ribbons; approximate round collision envelope",
                        length=0.18, radius=0.005, count=20, modulus=150e3, friction=0.25, cohesion=0.22,
                        aspect=0.20, colour=(0.94, 0.95, 0.86, 1.0), roughness=0.22),
    "fettuccine": dict(label="Fettuccine", description="Golden flat pasta; approximate round collision envelope",
                       length=0.24, radius=0.0035, count=20, modulus=1.0e6, friction=0.3, cohesion=0.12,
                       aspect=0.28, colour=(0.86, 0.61, 0.28, 1.0), roughness=0.35),
}


def bending_modulus(key):
    """Young's modulus the round-section solver needs to bend like this type.

    The solver models every strand as round. A flat ribbon of the same width
    is much lighter and much easier to bend across its thickness: bending
    stiffness per unit mass goes as I/A, which for a rectangle of thickness
    t is t^2/12 against r^2/4 for the round section. Scaling the modulus by
    that ratio, 4/3 * aspect^2, makes a ribbon droop like a ribbon.
    """
    preset = NOODLE_TYPES[key]
    aspect = preset["aspect"]
    return preset["modulus"] * (1.0 if aspect >= 1.0 else 4.0 / 3.0 * aspect * aspect)

TYPE_ITEMS = tuple((key, value["label"], value["description"])
                   for key, value in NOODLE_TYPES.items())


def type_material(key):
    import bpy
    from . import solver
    preset = NOODLE_TYPES[key]
    name = "Noodle Box - " + preset["label"]
    material = bpy.data.materials.get(name)
    if material is not None and material.get("noodle_type") == key:
        return material
    material = solver.ensure_material().copy()
    material.name = name
    material["noodle_type"] = key
    material.diffuse_color = preset["colour"]
    for node in material.node_tree.nodes:
        if node.type == "VALTORGB":
            for item, factor in zip(node.color_ramp.elements, (0.88, 1.0)):
                item.color = tuple(c * factor for c in preset["colour"][:3]) + (1.0,)
        elif node.type == "MAP_RANGE":
            node.inputs["To Min"].default_value = preset["roughness"] * 0.8
            node.inputs["To Max"].default_value = preset["roughness"] * 1.2
    return material


def physics_inputs(key):
    """Solver inputs that carry this type's material behaviour."""
    from . import solver
    preset = NOODLE_TYPES[key]
    return {
        "Damping": 0.0, "Contact Damping": solver.DEFAULT_CONTACT_DAMPING,
        "Stiffness": solver.stiffness_for_modulus(bending_modulus(key)),
        "Friction": preset["friction"], "Cohesion": preset["cohesion"],
    }


def apply_type(obj, key, scene, use_suggested_count=True):
    from . import solver
    preset = NOODLE_TYPES[key]  # Validate before changing the object.
    modifier = solver.noodle_modifier(obj)
    ng = modifier.node_group
    values = {
        "Noodle Length": preset["length"], "Noodle Radius": preset["radius"],
        "Length Variation": 0.12, "Spawn Coil": 1.0, "Fill Diameter": 0.14,
        "Start Height": 0.12, "Gravity": 9.81, "Self Collision": 1.0,
        "Profile Faces": 8, "Profile Aspect": preset["aspect"],
        "Smooth Surface": True,
        "Noodle Material": type_material(key), "Adaptive Substeps": True, "Iterations": 2,
    }
    values.update(physics_inputs(key))
    if use_suggested_count:
        values["Noodle Count"] = preset["count"]
    for name, value in values.items():
        solver.set_input(obj, ng, name, value)
    needed = solver.recommended_substeps(obj, ng, scene)
    solver.set_input(obj, ng, "Substeps", min(needed, solver.MAX_RUNTIME_SUBSTEPS))
    obj["noodle_type"] = key
    obj["noodle_preset"] = "custom"
    return needed
