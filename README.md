# Noodle Physics for Blender

A geometry-nodes, position-based-dynamics solver for piling cooked noodles. It creates a procedural noodle field, simulates gravity, bending, self-collision, cohesion, floor contact, and closed-mesh colliders, then converts the curves to a shaded mesh.

## Requirements

- Blender **5.2 or newer** (the script uses Blender 5.x Geometry Nodes interfaces).
- No external Python packages.
- A closed collider mesh for bowls, plates, or other surfaces. Add **Solidify** to thin/open shells.

## Quick start

1. Open Blender's **Scripting** workspace.
2. Open `noodle_physics.py` and click **Run Script**.
3. Select the generated `NoodlePhysics` object and play the timeline from **frame 1**.
4. Adjust inputs on the **Noodles** Geometry Nodes modifier.

The Simulation Zone advances only as frames play forward. Scrubbing backward past the cached range, changing solver inputs, or changing collider geometry can invalidate the cache; return to frame 1 and play forward again.

## Headless commands

Arguments for the script go after Blender's `--` separator:

```sh
blender --background --python noodle_physics.py
blender --background --python noodle_physics.py -- --check
blender --background --python noodle_physics.py -- --bench default 120 60
blender --background --python noodle_physics.py -- --bench fast 120 90
blender --background --python noodle_physics.py -- --realtime balanced
```

`--check` builds the node group, runs a small simulation, and validates that the pile falls, stays above the floor, and does not explode sideways. `--bench` reports mean and median milliseconds per frame plus wall-clock time to settle. Valid presets are `default`, `quality`, `balanced`, and `fast`; count and frame arguments must be positive integers.

## Scale and solver behavior

The defaults use a deliberately large scene: noodle radius `2.5`, length `1000`, and gravity `386` units/s², approximately inches. Gravity is evaluated relative to segment length, not by its numeric value alone. If you rebuild in metres, change the noodle dimensions and raise `Substeps` accordingly; changing Gravity by itself does not preserve the same motion or collision quality.

Point spacing is derived from each noodle's length and diameter. Keep this invariant: it is what lets the nearest-neighbour contact query detect crossing noodles. `Noodle Radius` is the main performance control: thicker noodles require fewer points and usually need fewer substeps.

## Important controls

- **Noodle Count**: number of strands.
- **Noodle Length / Length Variation**: strand lengths while preserving collision spacing.
- **Noodle Radius**: half thickness and the primary performance dial.
- **Fill Diameter / Start Height**: spawn area and drop height.
- **Gravity / Damping**: fall speed and air damping.
- **Substeps**: maximum collision-accuracy budget and fall speed; the solver adapts downward on easy frames and caps the effective value at `24`. Increase this first when tunnelling occurs.
- **Iterations**: constraint convergence per substep; `2` is a useful default and the effective value is capped at `12`.
- **Stiffness**: bending resistance.
- **Self Collision / Cohesion**: pile separation and cooked-pasta sticking.
- **Friction**: noodle-on-noodle contact friction.
- **Collider / Collider Collection**: primary closed-mesh collision sources.
- **Collider Margin / Collider Friction / Collider Stickiness**: surface clearance and contact response.
- **Sticky Collider / Sticky Collider Grip**: a second collider set with independent grip.
- **Profile Faces**: tube resolution; lower it for faster previews.

The floor at `z = 0` remains active even without a collider. Collider collections are joined into one distance field per frame. Animated or deforming colliders are supported, but a collider moving farther than its narrow-band distance in one frame can tunnel through the noodles; use smaller motion per frame or more suitable animation sampling.

## Realtime presets

These presets preserve approximately the default fall-speed budget while trading detail for speed:

| Preset | Substeps | Radius | Profile faces | Intended use |
| --- | ---: | ---: | ---: | --- |
| `default` | 8 | 2.5 | 6 | Highest detail |
| `quality` | 4 | 5.0 | 5 | Better live playback |
| `balanced` | 3 | 7.0 | 4 | General preview |
| `fast` | 2 | 9.0 | 4 | Blocking and iteration |

Preset performance depends heavily on Blender version, CPU, noodle count, and collider complexity. Measure with `--bench` rather than treating the historical benchmark numbers as guarantees.

## Troubleshooting

- **Nothing moves:** reset to frame 1, ensure the modifier is enabled, and play forward rather than jumping to a later frame.
- **Noodles pass through each other:** increase `Substeps`; avoid reducing radius while keeping the same substep count.
- **Noodles pass through a bowl:** make the collider watertight and thick, then check its scale and `Collider Margin`.
- **The pile explodes:** lower `Noodle Count`, raise `Noodle Radius`, reduce `Substeps` only with a matching radius increase, or lower `Start Height`.
- **Playback is slow:** apply `--realtime fast`, reduce `Profile Faces`, reduce `Iterations`, or benchmark a smaller count.
- **A benchmark says `NOT SETTLED`:** the run did not reach the chosen settle threshold within the requested frames; increase frames or treat the result as a fall-time comparison rather than a settled result.
- **A scripted socket assignment fails:** use the current script and confirm the generated node group was rebuilt; the CLI now reports the missing socket name and available inputs.

## License and modification

The solver is intentionally self-contained so it can be inspected and modified in Blender's Text Editor. Keep the public parameter names and the point-spacing invariant when extending the node graph; downstream materials and presets rely on them.
