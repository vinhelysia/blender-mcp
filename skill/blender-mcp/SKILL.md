---
name: blender-mcp
description: Expert operation of the Blender MCP connector (26 tools bridging to a Blender addon on localhost:9876). Use this skill WHENEVER the user mentions Blender, bpy, 3D modeling, meshes, materials, GLB/glTF export, .blend files, rendering, viewport, rigging, physics simulation, game assets, or asks to create/edit/inspect anything 3D — even if they don't say "Blender MCP" explicitly. Also use when exporting assets for Godot (Cogito framework).
---

# Blender MCP Mastery

You control a live Blender 5.x instance through MCP tools. `blender_execute_python`
runs arbitrary `bpy` code — everything else is a convenience wrapper. Work like a
careful technical artist: inspect first, act in small verified steps, never claim
success without numeric proof.

## Golden loop (follow every time)

1. **Inspect** — `blender_get_objects_summary` before assuming anything about the scene.
2. **Act** — one focused `blender_execute_python` call per logical step.
3. **Verify numerically** — return dimensions, vert/tri counts, bounding boxes,
   material slot names in `result`. "The code ran" is not verification.
4. **Verify visually** — `blender_get_area_screenshot` (VIEW_3D) or
   `blender_get_window_screenshot` for a final check. If screenshots misbehave,
   fall back to numeric checks; do not block on pixels.

## Core conventions

- Interactive tools: executed code assigns a JSON-serializable dict to `result`.
  CLI tools (`*_cli`): assign to `summary` instead.
- Import everything yourself (`import bpy, bmesh, math, mathutils`) in every call —
  namespaces do not persist between calls.
- Convert Blender types before returning: `[round(v,4) for v in obj.dimensions]`,
  never raw Vector/Euler/Matrix.
- `print()` is captured and returned as `stdout`; errors return a full traceback
  in `message`.
- Long operations (physics bakes, renders, heavy booleans): pass `timeout` (e.g. 300).
  On timeout the op may STILL be running inside Blender — wait and re-inspect,
  never re-fire a heavy op blindly.
- CLI variants spawn a headless `blender --background <file>`; changes are DISCARDED
  unless the code calls `bpy.ops.wm.save_mainfile()`. Blender's exe is auto-detected
  from the running instance; if Blender is closed, `BLENDER_BINARY` env var is needed.

## Context overrides (most bpy.ops failures come from this)

GUI operators need the right window/area/region. Pattern:

```python
area = next((a for a in bpy.context.screen.areas if a.type == 'VIEW_3D'), None)
region = next(r for r in area.regions if r.type == 'WINDOW')
with bpy.context.temp_override(area=area, region=region):
    bpy.ops.view3d.view_selected()          # or render.opengl(view_context=True)
```

- `bpy.ops.screen.screenshot` needs `temp_override(window=win)` with
  `win = bpy.context.window_manager.windows[0]`.
- Always ensure OBJECT mode before selection/transform ops:
  `if bpy.context.mode != 'OBJECT': bpy.ops.object.mode_set(mode='OBJECT')`.
- Wrap state-mutating render/viewport ops in try/finally and restore prior
  render settings (filepath, resolution, file_format).

## Hard-won gotchas

| Symptom | Cause → Fix |
|---|---|
| `.material` is None crash after boolean | Boolean apply leaves None slots → filter `[s for s in obj.material_slots if s.material]` |
| GLB looks gray/untextured in engine | Procedural node materials DON'T export → bake to image textures first |
| Baked texture looks like a mirror | Roughness pixels wrote as 0.0 → write via `img.pixels.foreach_set(buf)`, then `img.update()` and `img.pack()`; verify sampled pixel values before wiring |
| Mesh stats look wrong | You read the base mesh → use evaluated depsgraph: `obj.evaluated_get(bpy.context.evaluated_depsgraph_get()).to_mesh()` (and `to_mesh_clear()` after) |
| Object won't select/focus | It's hidden or in another collection → `obj.hide_set(False)` first; deselect all others explicitly |
| Export contains stray cameras/lights | Use `use_selection=True` on export and select exactly the asset + its collision helpers |
| Scale wrong in engine | Unapplied transforms → `bpy.ops.object.transform_apply(location=False, rotation=True, scale=True)` before export |

## Physics scatter recipe (rubble, debris, drops)

1. Create chunks + invisible container walls; add rigid body (convex hull,
   friction 0.9, restitution ~0.01).
2. Step frames manually to bake: `for f in range(1, 101): scene.frame_set(f)`.
3. Freeze results via evaluated world matrices:
   `mw = obj.evaluated_get(dg).matrix_world.copy(); obj.matrix_world = mw`.
4. Remove rigid body from all, delete container walls, join chunks, set
   origin bottom-center, add a fitted box collision helper.
5. Trim outliers farther than ~2 m from the median center for a tight footprint.

## Texture bake-for-export recipe (procedural → GLB)

1. Generate albedo/roughness/normal pixel buffers (e.g. `mathutils.noise` fractal)
   at 256–512 px; write with `foreach_set`, then `update()` + `pack()`.
2. Build nodes: Image Texture → Principled (BaseColor, Roughness, Normal via
   Normal Map node). Matte concrete: roughness 0.85–0.95, Specular IOR ~0.25.
3. UV: cube project with sensible world-scale tiling (e.g. 2 m).
4. Verify by sampling `img.pixels[:8]` — never trust the bake blind.

## Godot / Cogito export conventions (user's FPS project)

- Grid: 1 unit = 1 m, 4 m modular pieces. Light concrete albedo (~0.6 gray);
  NEVER near-black albedo.
- Collision naming suffixes Godot auto-imports: `-col` (trimesh), `-convcol`
  (convex), `-colonly` (collision-only, no visual).
- Origins: bottom-center; walls use bottom inner edge.
- GLB export: zero the asset to origin first, then
  `bpy.ops.export_scene.gltf(filepath=..., use_selection=True, export_apply=True,
  export_yup=True, export_cameras=False, export_lights=False)` — textures embed
  automatically.
- Export dir: `C:/Stuff/godot-fps-cogito-master/godot-fps-cogito-master/Assets/Blender`.
- Keep visual meshes low-poly (a 4 m wall ≈ tens of tris; a rubble pile ≤ ~500 tris).

## Tool selection quick map

- Scene overview → `blender_get_objects_summary`; one object deep-dive →
  `blender_get_object_detail_summary` (evaluated counts, modifiers, materials).
- See the screen → `blender_get_area_screenshot` (VIEW_3D) / window variant;
  UI geometry without pixels → `blender_get_screenshot_as_json`.
- Render to a file → `blender_render_viewport_to_path` /
  `blender_render_thumbnail_to_path` (frames one object automatically).
- API uncertainty → `blender_search_api_docs` or `blender_get_python_api_docs`
  (live, matches installed version) before guessing operator names.
- Inspect a .blend WITHOUT touching the open session → the `*_cli` summary tools
  or `blender_execute_python_cli`.
- Everything else → `blender_execute_python`.

## Safety rules

- NEVER delete or overwrite user objects/collections without being asked;
  when a task implies clearing the scene, confirm the scene is disposable first
  (default startup Cube/Light/Camera is fair game).
- NEVER call `bpy.ops.wm.save_mainfile()` on the interactive session unless the
  user explicitly asks to save.
- Batch related edits into one call, but keep destructive steps separate from
  verification steps.
- On "connection refused": tell the user to start Blender with the MCP addon
  server running (port 9876) — do not retry in a loop.
