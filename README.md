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

Run Script passes no arguments, so the scene it builds is decided by the `PRESET` constant near the top of the file. It ships as `None`, the reference-quality defaults. If playback is too slow to work with, set it to `"fast"` and run the script again.

## Headless commands

Arguments for the script go after Blender's `--` separator:

```sh
blender --background --python noodle_physics.py
blender --background --python noodle_physics.py -- --check
blender --background --python noodle_physics.py -- --bench default 120 60
blender --background --python noodle_physics.py -- --bench fast 120 90
blender --background --python noodle_physics.py -- --realtime balanced
```

`--check` builds the node group, runs a small simulation, and validates that the pile falls, stays above the floor, does not launch above its spawn height, and does not explode sideways. `--bench` reports mean and median milliseconds per frame plus wall-clock time to settle.

Preset names are `default`, `quality`, `balanced`, `fast`, `metric`, and `metric_fast`; count and frame arguments must be positive integers. A CLI error prints one line and exits `2`.

One caveat when scripting against this file: Blender exits `0` even when the script raises, so an exit code on its own is not a pass/fail signal. Grep the output for `Traceback`, or drive the solver in-process the way `tests/` does.

## Scale and solver behavior

The defaults use a deliberately large scene: noodle radius `2.5`, length `1000`, and gravity `386` units/s², approximately inches. Gravity is evaluated relative to segment length, not by its numeric value alone. If you rebuild in metres, change the noodle dimensions and raise `Substeps` accordingly; changing Gravity by itself does not preserve the same motion or collision quality. The `metric` and `metric_fast` presets are the worked example — half-metre noodles at real gravity, with the substep count derived from the scene rather than guessed.

Scaling the object down to see a 25 m noodle does **not** scale the physics with it: the solve still takes as long as a 25 m fall, so a scene scaled to 0.003 reads about 3× slower than reality. Rebuild at the size you want instead. The solver itself is scale-invariant to within 0.5% over a 333× change in unit size.

Point spacing is derived from each noodle's length and diameter. Keep this invariant: it is what lets the nearest-neighbour contact query detect crossing noodles. `Noodle Radius` is the main performance control: thicker noodles require fewer points and usually need fewer substeps.

## Important controls

- **Noodle Count**: number of strands; cost is linear in this.
- **Noodle Length / Length Variation**: strand lengths while preserving collision spacing.
- **Noodle Radius**: half thickness and the primary performance dial.
- **Fill Diameter / Start Height**: spawn area and drop height.
- **Gravity / Damping**: fall speed and air damping.
- **Substeps**: collision-accuracy budget **and fall speed**; the effective value is capped at `24`. Increase this first when tunnelling occurs.
- **Iterations**: constraint convergence per substep; `2` is a useful default and the effective value is capped at `12`.
- **Stiffness**: bending resistance.
- **Self Collision / Cohesion**: pile separation and cooked-pasta sticking.
- **Friction**: noodle-on-noodle contact friction.
- **Collider / Collider Collection**: primary closed-mesh collision sources.
- **Collider Margin / Collider Friction / Collider Stickiness**: surface clearance and contact response.
- **Sticky Collider / Sticky Collider Grip**: a second collider set with independent grip.
- **Profile Faces**: tube resolution; lower it for faster previews.

Every socket carries a tooltip describing its unit, its practical range, and what changing it costs. Those are worth reading before the list above.

The floor at `z = 0` remains active even without a collider. Collider collections are joined into one distance field per frame. Animated or deforming colliders are supported, but a collider moving farther than its narrow-band distance in one frame can tunnel through the noodles; use smaller motion per frame or more suitable animation sampling.

## Realtime presets

These presets trade detail for speed while holding the same fall-speed budget. That budget is the thing to understand before changing any of them:

```
fall speed cap = 2 * radius * 0.85 * substeps * fps
```

`Substeps` and `Noodle Radius` are two halves of one number. Lowering `Substeps` without raising `Noodle Radius` does not make the simulation faster to watch — it clamps the fall speed and puts the sim into slow motion, which is worse than leaving it alone. `substeps_for()` in the file is how to pick the count for a new scene.

| Preset | Substeps | Radius | Profile faces | Intended use |
| --- | ---: | ---: | ---: | --- |
| `default` | 8 | 2.5 | 6 | Highest detail |
| `quality` | 4 | 5.0 | 5 | Better live playback |
| `balanced` | 3 | 7.0 | 4 | General preview |
| `fast` | 2 | 9.0 | 4 | Blocking and iteration |

Measured at 120 noodles with `--bench <preset> 120 110` (Windows 11, Blender 5.2.0 LTS). The millisecond column moves by roughly 2× between machines and Blender builds; the frame count does not, so compare that one after a solver change:

| Preset | ms/frame | fps | frames to settle | settle (wall clock) |
| --- | ---: | ---: | ---: | ---: |
| `default` | 379 | 2.6 | 87 | 33.0 s |
| `quality` | 143 | 7.0 | 97 | 13.9 s |
| `balanced` | 109 | 9.2 | 99 | 10.8 s |
| `fast` | 62 | 16.1 | 100 | 6.2 s |

`ms/frame` includes a full vertex readback the benchmark needs for the settle metric and normal playback does not pay, so live fps is higher than this column.

## Unit presets

`metric` and `metric_fast` rebuild the same scene in metres at real gravity, with `Substeps` derived by `substeps_for()`. They are udon rather than spaghetti — 16 mm and 24 mm thick — because real 2 mm pasta at 9.81 falls a segment every half frame and would need roughly 50 substeps. Thick noodles are the cheap direction: fewer points *and* fewer substeps.

## Tests

The suite runs the solver in-process through the `bpy` PyPI wheel, which is version-locked to one Blender release and to one CPython (`bpy==5.2.1` is CPython 3.13 only):

```sh
python3.13 -m venv .venv-test
.venv-test/bin/pip install "bpy==5.2.1" pytest
.venv-test/bin/python -m pytest tests/ -v
```

Set `NOODLE_SCRIPT=<path>` to run the same suite against another revision of the solver, which is how the regression tests were checked against the code they describe.

## Troubleshooting

- **Nothing moves:** reset to frame 1, ensure the modifier is enabled, and play forward rather than jumping to a later frame.
- **Noodles pass through each other:** increase `Substeps`; avoid reducing radius while keeping the same substep count.
- **Noodles pass through a bowl:** make the collider watertight and thick, then check its scale and `Collider Margin`.
- **The pile explodes:** lower `Noodle Count`, raise `Noodle Radius`, reduce `Substeps` only with a matching radius increase, or lower `Start Height`.
- **The pile falls in slow motion:** `Substeps` is too low for the scene's `Start Height` and `Noodle Radius`. Raise `Substeps`, or raise `Noodle Radius` instead if the frame is already too slow — see the fall-speed formula above.
- **Playback is slow:** apply `--realtime fast`, reduce `Profile Faces`, reduce `Iterations`, or benchmark a smaller count.
- **A benchmark says `NOT SETTLED`:** the run did not reach the chosen settle threshold within the requested frames; increase frames or treat the result as a fall-time comparison rather than a settled result.
- **A scripted socket assignment fails:** use the current script and confirm the generated node group was rebuilt; the CLI now reports the missing socket name and available inputs.

## License and modification

Apache-2.0; see `LICENSE`.

The solver is intentionally self-contained so it can be inspected and modified in Blender's Text Editor. Keep the public parameter names and the point-spacing invariant when extending the node graph; downstream materials and presets rely on them. `tests/` is the guard rail — `test_fall_speed.py` in particular exists because the fall-speed coupling above has been broken once already.
