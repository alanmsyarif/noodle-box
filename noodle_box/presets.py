"""Art-directed noodle types in metres, independent of performance shortcuts."""

NOODLE_TYPES = {
    "udon": dict(label="Udon", description="Thick, pale and soft; best for live playback",
                 length=0.24, radius=0.005, count=32, stiffness=0.3, cohesion=0.16,
                 aspect=1.0, colour=(0.92, 0.82, 0.60, 1.0), roughness=0.3),
    "ramen": dict(label="Ramen", description="Medium egg noodles with a firmer bend",
                  length=0.22, radius=0.0025, count=24, stiffness=0.5, cohesion=0.12,
                  aspect=1.0, colour=(0.82, 0.56, 0.20, 1.0), roughness=0.28),
    "spaghetti": dict(label="Spaghetti", description="Fine round pasta; detailed, more expensive to solve",
                      length=0.24, radius=0.0012, count=12, stiffness=0.65, cohesion=0.08,
                      aspect=1.0, colour=(0.90, 0.64, 0.25, 1.0), roughness=0.32),
    "soba": dict(label="Soba", description="Slender buckwheat noodles with a darker, matte surface",
                 length=0.20, radius=0.0018, count=18, stiffness=0.55, cohesion=0.07,
                 aspect=1.0, colour=(0.27, 0.17, 0.10, 1.0), roughness=0.48),
    "rice_ribbon": dict(label="Rice Ribbon", description="Wide, pale, flexible ribbons; approximate round collision envelope",
                        length=0.18, radius=0.005, count=20, stiffness=0.18, cohesion=0.22,
                        aspect=0.20, colour=(0.94, 0.95, 0.86, 1.0), roughness=0.22),
    "fettuccine": dict(label="Fettuccine", description="Golden flat pasta; approximate round collision envelope",
                       length=0.24, radius=0.0035, count=20, stiffness=0.42, cohesion=0.12,
                       aspect=0.28, colour=(0.86, 0.61, 0.28, 1.0), roughness=0.35),
}

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
    needed = solver.recommended_substeps(obj, ng, scene)
    solver.set_input(obj, ng, "Substeps", min(needed, solver.MAX_RUNTIME_SUBSTEPS))
    obj["noodle_type"] = key
    obj["noodle_preset"] = "custom"
    return needed
