#!/usr/bin/env python3
"""
blender_mcp v2: full-surface MCP server that lets Codex (or any MCP client)
control Blender. Tool surface mirrors ahujasid/blender-mcp's design; talks
to the official Blender Lab addon's protocol instead.

Bridges MCP tool calls to the Blender MCP add-on's TCP socket server
(localhost:9876). CLI-variant tools instead launch a headless Blender
subprocess to inspect .blend files on disk without touching the open session.

Codex config (~/.codex/config.toml):

    [mcp_servers.blender]
    command = "python"
    args = ["C:/Stuff/blender-codex-mcp/blender_mcp_server.py"]

Requires:  pip install mcp
Optional env vars: BLENDER_MCP_HOST, BLENDER_MCP_PORT, BLENDER_MCP_TIMEOUT,
                   BLENDER_BINARY (path to blender.exe for CLI tools;
                   auto-detected from the running instance when unset)
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import tempfile
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field

from mcp.server.fastmcp import FastMCP, Image

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BLENDER_HOST = os.environ.get("BLENDER_MCP_HOST", "localhost")
BLENDER_PORT = int(os.environ.get("BLENDER_MCP_PORT", "9876"))
DEFAULT_TIMEOUT = float(os.environ.get("BLENDER_MCP_TIMEOUT", "120"))

CONNECT_HELP = (
    f"Could not connect to Blender at {BLENDER_HOST}:{BLENDER_PORT}. "
    "Make sure that: (1) Blender is running, (2) the MCP add-on is enabled "
    "(Edit > Preferences > Add-ons), and (3) the add-on's server is started."
)

mcp = FastMCP("blender_mcp")

# ---------------------------------------------------------------------------
# Transport: interactive Blender (socket) and headless Blender (subprocess)
# ---------------------------------------------------------------------------


async def send_to_blender(code: str, strict_json: bool = False,
                          timeout: float = DEFAULT_TIMEOUT) -> dict[str, Any]:
    """Send Python code to the interactive Blender add-on; return its response."""
    payload = json.dumps({"type": "execute", "code": code, "strict_json": strict_json})
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(BLENDER_HOST, BLENDER_PORT), timeout=10.0)
    except (ConnectionRefusedError, OSError, asyncio.TimeoutError) as exc:
        raise ConnectionError(CONNECT_HELP) from exc
    try:
        writer.write(payload.encode("utf-8") + b"\0")
        await writer.drain()
        raw = await asyncio.wait_for(reader.readuntil(b"\0"), timeout=timeout)
    except asyncio.TimeoutError as exc:
        raise TimeoutError(
            f"Blender did not respond within {timeout:.0f}s. Long operations may "
            "need a higher timeout. Blender may still be busy executing the code."
        ) from exc
    except asyncio.IncompleteReadError as exc:
        raise ConnectionError(
            "Blender closed the connection before sending a complete response.") from exc
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass
    return json.loads(raw[:-1].decode("utf-8"))


def format_response(response: dict[str, Any]) -> str:
    out: dict[str, Any] = {"status": response.get("status", "unknown")}
    if "result" in response:
        out["result"] = response["result"]
    for key in ("message", "stdout", "stderr"):
        if response.get(key):
            out[key] = response[key]
    return json.dumps(out, indent=2, ensure_ascii=False)


async def run_tool_code(code: str, timeout: float = DEFAULT_TIMEOUT) -> str:
    try:
        response = await send_to_blender(code, strict_json=False, timeout=timeout)
    except (ConnectionError, TimeoutError) as exc:
        return json.dumps({"status": "error", "message": str(exc)})
    return format_response(response)


async def run_tool_code_raw(code: str, timeout: float = DEFAULT_TIMEOUT) -> dict[str, Any]:
    """Like run_tool_code but returns the parsed dict (for image tools)."""
    try:
        return await send_to_blender(code, strict_json=False, timeout=timeout)
    except (ConnectionError, TimeoutError) as exc:
        return {"status": "error", "message": str(exc)}


# --- headless Blender for the *_cli tools ---------------------------------

_CLI_MARKER_A = "===BLENDER_MCP_JSON_BEGIN==="
_CLI_MARKER_B = "===BLENDER_MCP_JSON_END==="


async def detect_blender_binary() -> Optional[str]:
    """Find the Blender executable: env var first, else ask the running instance."""
    env = os.environ.get("BLENDER_BINARY")
    if env and os.path.exists(env):
        return env
    try:
        resp = await send_to_blender(
            "import bpy\nresult = {'binary': bpy.app.binary_path}", timeout=15)
        binary = resp.get("result", {}).get("binary")
        if binary and os.path.exists(binary):
            return binary
    except (ConnectionError, TimeoutError):
        pass
    return None


async def run_in_headless_blender(blendfile: str, snippet: str,
                                  timeout: float = 180) -> str:
    """Open *blendfile* in a background Blender process, run *snippet*
    (which must assign a dict to `summary`), and return formatted JSON."""
    if not os.path.exists(blendfile):
        return json.dumps({"status": "error",
                           "message": f"Blend file not found: {blendfile}"})
    binary = await detect_blender_binary()
    if binary is None:
        return json.dumps({"status": "error", "message": (
            "Could not locate the Blender executable. Either start the "
            "interactive Blender with the MCP add-on (auto-detect), or set "
            "the BLENDER_BINARY environment variable to blender.exe.")})

    expr = (
        "import json\n"
        + snippet
        + f"\nprint({_CLI_MARKER_A!r} + json.dumps(summary) + {_CLI_MARKER_B!r})\n"
    )
    proc = await asyncio.create_subprocess_exec(
        binary, "--background", blendfile, "--factory-startup",
        "--python-expr", expr,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return json.dumps({"status": "error",
                           "message": f"Headless Blender timed out after {timeout:.0f}s."})
    text = stdout.decode("utf-8", errors="replace")
    if _CLI_MARKER_A in text and _CLI_MARKER_B in text:
        blob = text.split(_CLI_MARKER_A, 1)[1].split(_CLI_MARKER_B, 1)[0]
        try:
            return json.dumps({"status": "ok", "result": json.loads(blob)},
                              indent=2, ensure_ascii=False)
        except json.JSONDecodeError:
            pass
    return json.dumps({"status": "error",
                       "message": "Headless Blender did not produce a parsable result.",
                       "stdout_tail": text[-2000:],
                       "stderr_tail": stderr.decode("utf-8", errors="replace")[-2000:]})


# ---------------------------------------------------------------------------
# Shared analysis snippets (used by BOTH interactive and CLI variants).
# Each snippet must define a dict named `summary` using only `bpy` + stdlib.
# ---------------------------------------------------------------------------

SNIPPET_DATABLOCKS = r"""
import bpy
kinds = [
    "objects", "meshes", "materials", "textures", "images", "collections",
    "scenes", "worlds", "cameras", "lights", "curves", "armatures", "actions",
    "node_groups", "fonts", "sounds", "movieclips", "brushes", "libraries",
]
counts = {}
for kind in kinds:
    data = getattr(bpy.data, kind, None)
    if data is not None:
        items = [{"name": d.name, "users": d.users,
                  "library": (d.library.filepath if getattr(d, "library", None) else None)}
                 for d in data]
        counts[kind] = {"count": len(items), "items": items[:100]}
summary = {"blendfile": bpy.data.filepath or "(unsaved)", "datablocks": counts}
"""

SNIPPET_MISSING_FILES = r"""
import bpy, os
missing, checked = [], 0
def check(kind, coll):
    global checked
    for d in coll:
        fp = getattr(d, "filepath", "")
        if not fp:
            continue
        if getattr(d, "packed_file", None):
            continue
        checked += 1
        ab = bpy.path.abspath(fp, library=getattr(d, "library", None))
        if not os.path.exists(ab):
            missing.append({"kind": kind, "name": d.name,
                            "filepath": fp, "absolute": ab})
check("image", bpy.data.images)
check("library", bpy.data.libraries)
check("sound", getattr(bpy.data, "sounds", []))
check("font", [f for f in bpy.data.fonts if f.filepath not in ("", "<builtin>")])
check("movieclip", getattr(bpy.data, "movieclips", []))
summary = {"blendfile": bpy.data.filepath or "(unsaved)",
           "checked_external_files": checked,
           "missing_count": len(missing), "missing": missing}
"""

SNIPPET_LINKED_LIBRARIES = r"""
import bpy, os
libs = []
for lib in bpy.data.libraries:
    ab = bpy.path.abspath(lib.filepath)
    users = []
    for kind in ("objects", "meshes", "materials", "collections", "node_groups"):
        for d in getattr(bpy.data, kind):
            if getattr(d, "library", None) is lib:
                users.append({"kind": kind[:-1], "name": d.name})
    libs.append({"filepath": lib.filepath, "absolute": ab,
                 "exists": os.path.exists(ab),
                 "linked_datablocks": users[:100],
                 "linked_count": len(users)})
summary = {"blendfile": bpy.data.filepath or "(unsaved)",
           "library_count": len(libs), "libraries": libs}
"""

SNIPPET_PATH_INFO = r"""
import bpy, os
fp = bpy.data.filepath
external = []
for img in bpy.data.images:
    if img.filepath and not img.packed_file:
        external.append({"kind": "image", "name": img.name, "filepath": img.filepath})
for lib in bpy.data.libraries:
    external.append({"kind": "library", "name": lib.name, "filepath": lib.filepath})
rel = sum(1 for e in external if e["filepath"].startswith("//"))
summary = {
    "blendfile": fp or "(unsaved)",
    "directory": os.path.dirname(fp) if fp else None,
    "is_saved": bool(fp),
    "is_dirty": bpy.data.is_dirty,
    "external_path_count": len(external),
    "relative_paths": rel,
    "absolute_paths": len(external) - rel,
    "external_paths": external[:100],
}
"""

SNIPPET_USAGE_GUESS = r"""
import bpy
obj_types = {}
for o in bpy.data.objects:
    obj_types[o.type] = obj_types.get(o.type, 0) + 1
has_anim = any(a for a in bpy.data.actions) or any(
    o.animation_data and o.animation_data.action for o in bpy.data.objects)
has_rig = obj_types.get("ARMATURE", 0) > 0
has_nodes_comp = any(s.use_nodes for s in bpy.data.scenes)
mesh_count = obj_types.get("MESH", 0)
guesses = []
if has_rig and has_anim:
    guesses.append("character/creature animation or rigging file")
elif has_anim:
    guesses.append("animation file")
if mesh_count >= 5 and not has_anim:
    guesses.append("asset kit / environment modeling file")
elif mesh_count > 0:
    guesses.append("modeling file")
if any(len(m.node_tree.nodes) > 8 for m in bpy.data.materials
       if m.use_nodes and m.node_tree):
    guesses.append("material/shading work present")
if not guesses:
    guesses.append("undetermined / mostly empty file")
summary = {
    "blendfile": bpy.data.filepath or "(unsaved)",
    "object_types": obj_types,
    "has_animation": bool(has_anim),
    "has_armature": has_rig,
    "scene_count": len(bpy.data.scenes),
    "material_count": len(bpy.data.materials),
    "usage_guess": guesses,
}
"""

_SUMMARY_SNIPPETS = {
    "datablocks": SNIPPET_DATABLOCKS,
    "missing_files": SNIPPET_MISSING_FILES,
    "linked_libraries": SNIPPET_LINKED_LIBRARIES,
    "path_info": SNIPPET_PATH_INFO,
    "usage_guess": SNIPPET_USAGE_GUESS,
}


def interactive_wrapper(snippet: str) -> str:
    return snippet + "\nresult = summary\n"


# ---------------------------------------------------------------------------
# Input models
# ---------------------------------------------------------------------------

class ExecuteCodeInput(BaseModel):
    """Input for executing arbitrary Python code inside Blender."""
    model_config = ConfigDict(str_strip_whitespace=False, extra="forbid")
    code: str = Field(..., min_length=1, description=(
        "Python code to execute inside Blender. Import modules yourself "
        "(`import bpy`). Assign a JSON-serializable dict to a variable named "
        "`result` to return data."))
    timeout: Optional[float] = Field(default=None, ge=1, le=3600, description=(
        "Seconds to wait (default 120). Increase for sims/renders."))


class ExecuteCodeCliInput(BaseModel):
    """Input for executing code on a .blend file in a headless Blender."""
    model_config = ConfigDict(str_strip_whitespace=False, extra="forbid")
    blendfile: str = Field(..., min_length=3,
                           description="Absolute path to the .blend file to open.")
    code: str = Field(..., min_length=1, description=(
        "Python code run in a background Blender with the file open. Assign a "
        "JSON-serializable dict to `summary` to return data. To persist "
        "changes, call bpy.ops.wm.save_mainfile() in the code."))
    timeout: Optional[float] = Field(default=None, ge=5, le=3600)


class BlendfileInput(BaseModel):
    """Input identifying a .blend file on disk."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    blendfile: str = Field(..., min_length=3,
                           description="Absolute path to the .blend file.")


class ObjectNameInput(BaseModel):
    """Input identifying one object by name."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    name: str = Field(..., min_length=1, description="Exact object name, e.g. 'Cube'.")


class ApiDocsInput(BaseModel):
    """Input for fetching docs of one API identifier."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    identifier: str = Field(..., min_length=3, description=(
        "Dotted identifier, e.g. 'bpy.ops.object.shade_auto_smooth' or "
        "'bpy.types.BevelModifier'."))


class SearchInput(BaseModel):
    """Input for a keyword search."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    query: str = Field(..., min_length=2, description="Search term, e.g. 'bevel'.")
    limit: Optional[int] = Field(default=10, ge=1, le=50)


class AreaScreenshotInput(BaseModel):
    """Input for capturing one editor area."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    area_ui_type: str = Field(default="VIEW_3D", description=(
        "Editor type to capture: VIEW_3D, OUTLINER, PROPERTIES, "
        "SHADER_EDITOR ('ShaderNodeTree'), UV, IMAGE_EDITOR, etc."))


class RenderViewportInput(BaseModel):
    """Input for rendering the viewport to a file."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    output_path: str = Field(..., min_length=3,
                             description="Absolute PNG path, e.g. 'C:/tmp/x.png'.")
    width: Optional[int] = Field(default=1280, ge=64, le=7680)
    height: Optional[int] = Field(default=720, ge=64, le=4320)


class RenderThumbnailInput(BaseModel):
    """Input for rendering an object-framed thumbnail."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    output_path: str = Field(..., min_length=3,
                             description="Absolute PNG path for the thumbnail.")
    object_name: Optional[str] = Field(default=None, description=(
        "Object to frame. When omitted, frames the current selection/all."))
    size: Optional[int] = Field(default=512, ge=64, le=2048,
                                description="Square thumbnail size in pixels.")


class WorkspaceInput(BaseModel):
    """Input naming a workspace tab."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    name: str = Field(..., min_length=1,
                      description="Workspace tab name, e.g. 'Layout', 'Modeling'.")


class SpaceTypeInput(BaseModel):
    """Input naming an editor space type."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    space_type: str = Field(..., min_length=2, description=(
        "Editor space type to find a workspace for, e.g. 'VIEW_3D', "
        "'NODE_EDITOR', 'IMAGE_EDITOR', 'SEQUENCE_EDITOR'."))


# ---------------------------------------------------------------------------
# READ-ONLY TOOLS
# ---------------------------------------------------------------------------

_RO = {"readOnlyHint": True, "destructiveHint": False,
       "idempotentHint": True, "openWorldHint": False}


def _summary_tool(kind: str, title: str):
    async def _interactive() -> str:
        return await run_tool_code(interactive_wrapper(_SUMMARY_SNIPPETS[kind]))
    _interactive.__doc__ = (
        f"{title} for the CURRENTLY OPEN file in the interactive Blender.\n\n"
        "Returns: str: JSON with status and result (see the corresponding "
        "summary fields).")
    return _interactive


def _summary_tool_cli(kind: str, title: str):
    async def _cli(params: BlendfileInput) -> str:
        return await run_in_headless_blender(params.blendfile, _SUMMARY_SNIPPETS[kind])
    _cli.__doc__ = (
        f"{title} for a .blend FILE ON DISK, opened in a headless background "
        "Blender (does not disturb the interactive session).\n\n"
        "Returns: str: JSON with status and result.")
    return _cli


mcp.tool(name="blender_get_blendfile_datablocks_summary",
         annotations={"title": "Get Blend-File Data-blocks Summary", **_RO})(
    _summary_tool("datablocks", "Count and list all data-blocks (objects, meshes, "
                  "materials, images, collections, etc.)"))
mcp.tool(name="blender_get_blendfile_datablocks_summary_cli",
         annotations={"title": "Get Blend-File Data-blocks Summary (CLI)", **_RO})(
    _summary_tool_cli("datablocks", "Count and list all data-blocks"))

mcp.tool(name="blender_get_blendfile_missing_files_summary",
         annotations={"title": "Get Blend-File Missing Files Summary", **_RO})(
    _summary_tool("missing_files", "Report external file references (images, "
                  "libraries, sounds, fonts) whose files are missing on disk"))
mcp.tool(name="blender_get_blendfile_missing_files_summary_cli",
         annotations={"title": "Get Blend-File Missing Files Summary (CLI)", **_RO})(
    _summary_tool_cli("missing_files", "Report missing external file references"))

mcp.tool(name="blender_get_blendfile_linked_libraries_summary",
         annotations={"title": "Get Blend-File Linked Library Summary", **_RO})(
    _summary_tool("linked_libraries", "List linked .blend libraries and which "
                  "data-blocks are linked from each"))
mcp.tool(name="blender_get_blendfile_linked_libraries_summary_cli",
         annotations={"title": "Get Blend-File Linked Library Summary (CLI)", **_RO})(
    _summary_tool_cli("linked_libraries", "List linked .blend libraries"))

mcp.tool(name="blender_get_blendfile_path_info_summary",
         annotations={"title": "Get Blend-File Path Info Summary", **_RO})(
    _summary_tool("path_info", "Report the file's save path, dirty state, and "
                  "all external path references (relative vs absolute)"))
mcp.tool(name="blender_get_blendfile_path_info_summary_cli",
         annotations={"title": "Get Blend-File Path Info Summary (CLI)", **_RO})(
    _summary_tool_cli("path_info", "Report save path and external path references"))

mcp.tool(name="blender_get_blendfile_usage_summary",
         annotations={"title": "Get Blend-File Usage Summary", **_RO})(
    _summary_tool("usage_guess", "Heuristically guess what the file is used for "
                  "(modeling, animation, rigging, shading) from its contents"))
mcp.tool(name="blender_get_blendfile_usage_summary_cli",
         annotations={"title": "Get Blend-File Usage Summary (CLI)", **_RO})(
    _summary_tool_cli("usage_guess", "Heuristically guess the file's purpose"))


@mcp.tool(name="blender_get_objects_summary",
          annotations={"title": "Get Objects Summary", **_RO})
async def blender_get_objects_summary() -> str:
    """Inventory of the current scene: every object (name, type, collections,
    location, dimensions, visibility, modifiers), active object, mode, frame
    range, materials, and collections.

    Returns: str: JSON with status and result{scene, mode, active_object,
    frame_current, frame_range, object_count, objects[], materials[],
    collections[]}."""
    code = r"""
import bpy
objs = []
for o in bpy.data.objects:
    objs.append({
        "name": o.name, "type": o.type,
        "collections": [c.name for c in o.users_collection],
        "location": [round(v, 4) for v in o.location],
        "dimensions": [round(v, 4) for v in o.dimensions] if o.type == 'MESH' else None,
        "hidden": o.hide_get(),
        "modifiers": [m.type for m in o.modifiers] if hasattr(o, "modifiers") else [],
    })
active = bpy.context.view_layer.objects.active
result = {
    "scene": bpy.context.scene.name, "mode": bpy.context.mode,
    "active_object": active.name if active else None,
    "frame_current": bpy.context.scene.frame_current,
    "frame_range": [bpy.context.scene.frame_start, bpy.context.scene.frame_end],
    "object_count": len(objs), "objects": objs,
    "materials": [m.name for m in bpy.data.materials],
    "collections": [c.name for c in bpy.data.collections],
}
"""
    return await run_tool_code(code)


@mcp.tool(name="blender_get_object_detail_summary",
          annotations={"title": "Get Object Detail Summary", **_RO})
async def blender_get_object_detail_summary(params: ObjectNameInput) -> str:
    """Detailed info for one object: transform, evaluated mesh stats (verts/tris
    with modifiers applied), UV layers, materials, modifier stack, parent,
    children, custom properties. Suggests similar names when not found.

    Returns: str: JSON with status and result{...} or {found: false,
    similar_names[]}."""
    name_literal = json.dumps(params.name)
    code = f"""
import bpy, math
name = {name_literal}
o = bpy.data.objects.get(name)
if o is None:
    close = [x.name for x in bpy.data.objects if name.lower() in x.name.lower()]
    result = {{"found": False, "message": "Object not found", "similar_names": close[:10]}}
else:
    info = {{
        "found": True, "name": o.name, "type": o.type,
        "transform": {{
            "location": [round(v, 4) for v in o.location],
            "rotation_euler_deg": [round(math.degrees(v), 2) for v in o.rotation_euler],
            "scale": [round(v, 4) for v in o.scale],
            "dimensions": [round(v, 4) for v in o.dimensions],
        }},
        "hidden": o.hide_get(),
        "collections": [c.name for c in o.users_collection],
        "parent": o.parent.name if o.parent else None,
        "children": [c.name for c in o.children],
        "custom_properties": {{k: repr(o[k]) for k in o.keys() if k != "_RNA_UI"}},
    }}
    if o.type == 'MESH':
        dg = bpy.context.evaluated_depsgraph_get()
        me = o.evaluated_get(dg).to_mesh()
        info["mesh_stats"] = {{
            "vertices": len(me.vertices),
            "triangles": sum(len(p.vertices) - 2 for p in me.polygons),
            "uv_layers": [uv.name for uv in me.uv_layers],
        }}
        o.evaluated_get(dg).to_mesh_clear()
        info["materials"] = [s.material.name if s.material else None for s in o.material_slots]
    info["modifiers"] = [{{"name": m.name, "type": m.type}} for m in o.modifiers] if hasattr(o, "modifiers") else []
    result = info
"""
    return await run_tool_code(code)


@mcp.tool(name="blender_get_python_api_docs",
          annotations={"title": "Get Python API Docs", **_RO})
async def blender_get_python_api_docs(params: ApiDocsInput) -> str:
    """Fetch documentation for one Blender Python API identifier (operator,
    type, or property), resolved in the LIVE Blender so it always matches the
    installed version. For operators, includes the signature docstring; for
    types, the class docstring and property list.

    Returns: str: JSON with status and result{identifier, found, kind, doc,
    properties?}."""
    ident_literal = json.dumps(params.identifier)
    code = f"""
import bpy
ident = {ident_literal}
parts = ident.split(".")
obj = None
try:
    if parts[0] == "bpy":
        obj = bpy
        for p in parts[1:]:
            obj = getattr(obj, p)
    else:
        import importlib
        obj = importlib.import_module(parts[0])
        for p in parts[1:]:
            obj = getattr(obj, p)
except (AttributeError, ImportError) as e:
    result = {{"identifier": ident, "found": False, "message": str(e)}}
else:
    info = {{"identifier": ident, "found": True,
             "kind": type(obj).__name__,
             "doc": (obj.__doc__ or "").strip()[:4000]}}
    rna = getattr(obj, "bl_rna", None)
    if rna is not None:
        info["properties"] = [
            {{"name": p.identifier, "type": p.type,
              "description": (p.description or "")[:200]}}
            for p in rna.properties if p.identifier != "rna_type"
        ][:60]
    result = info
"""
    return await run_tool_code(code)


_SCREENSHOT_SNIPPET = r"""
import bpy, os, base64, tempfile
__PLACE__
"""


async def _capture_screenshot(area_filter: Optional[str]) -> Any:
    """Capture the Blender window (or one area) and return it as an MCP image."""
    tmp_token = json.dumps(os.path.join(tempfile.gettempdir(),
                                        "blender_mcp_shot.png").replace("\\", "/"))
    if area_filter is None:
        body = f"""
import bpy, os, base64
path = {tmp_token}
if os.path.exists(path):
    os.remove(path)
win = bpy.context.window_manager.windows[0]
with bpy.context.temp_override(window=win):
    bpy.ops.screen.screenshot(filepath=path)
with open(path, "rb") as f:
    data = base64.b64encode(f.read()).decode("ascii")
result = {{"png_base64": data, "bytes": os.path.getsize(path)}}
"""
    else:
        area_literal = json.dumps(area_filter)
        body = f"""
import bpy, os, base64
path = {tmp_token}
if os.path.exists(path):
    os.remove(path)
target = {area_literal}
win = bpy.context.window_manager.windows[0]
area = next((a for a in win.screen.areas if a.ui_type == target or a.type == target), None)
if area is None:
    result = {{"error": "No area of type %s found. Open that editor first." % target,
               "available": sorted({{a.ui_type for a in win.screen.areas}})}}
else:
    with bpy.context.temp_override(window=win, area=area):
        bpy.ops.screen.screenshot_area(filepath=path)
    with open(path, "rb") as f:
        data = base64.b64encode(f.read()).decode("ascii")
    result = {{"png_base64": data, "bytes": os.path.getsize(path)}}
"""
    resp = await run_tool_code_raw(body, timeout=60)
    if resp.get("status") != "ok":
        return json.dumps({"status": "error",
                           "message": resp.get("message", "screenshot failed")})
    res = resp.get("result", {})
    if "error" in res:
        return json.dumps({"status": "error", "message": res["error"],
                           "available_areas": res.get("available", [])})
    return Image(data=base64.b64decode(res["png_base64"]), format="png")


@mcp.tool(name="blender_get_window_screenshot",
          annotations={"title": "Get Window Screenshot", **_RO})
async def blender_get_window_screenshot() -> Any:
    """Capture a screenshot of the ENTIRE Blender window (all editors, headers,
    sidebars) and return it as an inline PNG image.

    Returns: image content (PNG), or a JSON error string on failure."""
    return await _capture_screenshot(None)


@mcp.tool(name="blender_get_area_screenshot",
          annotations={"title": "Get Area Screenshot", **_RO})
async def blender_get_area_screenshot(params: AreaScreenshotInput) -> Any:
    """Capture a screenshot of ONE editor area (e.g. just the 3D viewport)
    and return it as an inline PNG image. Lists available areas when the
    requested type is not open.

    Returns: image content (PNG), or a JSON error string on failure."""
    return await _capture_screenshot(params.area_ui_type)


@mcp.tool(name="blender_get_screenshot_as_json",
          annotations={"title": "Get Screenshot as JSON", **_RO})
async def blender_get_screenshot_as_json() -> str:
    """Describe the Blender window layout as structured JSON instead of pixels:
    every area's editor type, position, size, and regions. Useful for
    reasoning about the UI without image processing.

    Returns: str: JSON with status and result{window{width,height},
    workspace, areas[{type, ui_type, x, y, width, height, regions[]}]}."""
    code = r"""
import bpy
win = bpy.context.window_manager.windows[0]
areas = []
for a in win.screen.areas:
    areas.append({
        "type": a.type, "ui_type": a.ui_type,
        "x": a.x, "y": a.y, "width": a.width, "height": a.height,
        "regions": [{"type": r.type, "width": r.width, "height": r.height}
                    for r in a.regions],
    })
result = {
    "window": {"width": win.width, "height": win.height},
    "workspace": win.workspace.name,
    "screen": win.screen.name,
    "areas": areas,
}
"""
    return await run_tool_code(code)


@mcp.tool(name="blender_render_viewport_to_path",
          annotations={"title": "Render Viewport to Path", **_RO})
async def blender_render_viewport_to_path(params: RenderViewportInput) -> str:
    """Render the current 3D viewport (OpenGL, current shading mode, no camera
    required) to a PNG file on disk.

    Returns: str: JSON with status and result{filepath, width, height,
    file_written}."""
    path_literal = json.dumps(params.output_path)
    code = f"""
import bpy, os
out_path = {path_literal}
w, h = {int(params.width or 1280)}, {int(params.height or 720)}
scene = bpy.context.scene
prev = (scene.render.filepath, scene.render.resolution_x, scene.render.resolution_y,
        scene.render.resolution_percentage, scene.render.image_settings.file_format)
scene.render.filepath = out_path
scene.render.resolution_x = w
scene.render.resolution_y = h
scene.render.resolution_percentage = 100
scene.render.image_settings.file_format = 'PNG'
area = next((a for a in bpy.context.screen.areas if a.type == 'VIEW_3D'), None)
try:
    if area is None:
        raise RuntimeError("No 3D viewport area found")
    region = next(r for r in area.regions if r.type == 'WINDOW')
    with bpy.context.temp_override(area=area, region=region):
        bpy.ops.render.opengl(write_still=True, view_context=True)
    result = {{"filepath": out_path, "width": w, "height": h,
               "file_written": os.path.exists(out_path)}}
finally:
    (scene.render.filepath, scene.render.resolution_x, scene.render.resolution_y,
     scene.render.resolution_percentage, scene.render.image_settings.file_format) = prev
"""
    return await run_tool_code(code, timeout=180)


@mcp.tool(name="blender_search_api_docs",
          annotations={"title": "Search API Docs", **_RO})
async def blender_search_api_docs(params: SearchInput) -> str:
    """Keyword-search the live Blender Python API: operator names (bpy.ops.*)
    and type names (bpy.types.*), with first-line docstrings.

    Returns: str: JSON with status and result{query, count,
    matches[{identifier, kind, doc}]}."""
    query_literal = json.dumps(params.query.lower())
    code = f"""
import bpy
query = {query_literal}
limit = {int(params.limit or 10)}
matches = []
for cat_name in dir(bpy.ops):
    if cat_name.startswith("_"):
        continue
    cat = getattr(bpy.ops, cat_name)
    for op_name in dir(cat):
        if query in op_name.lower() or query in cat_name.lower():
            doc = ""
            try:
                doc = (getattr(cat, op_name).__doc__ or "").strip().split("\\n")[0][:200]
            except Exception:
                pass
            matches.append({{"identifier": "bpy.ops.%s.%s" % (cat_name, op_name),
                             "kind": "operator", "doc": doc}})
            if len(matches) >= limit:
                break
    if len(matches) >= limit:
        break
if len(matches) < limit:
    for t_name in dir(bpy.types):
        if query in t_name.lower():
            doc = ""
            try:
                doc = (getattr(bpy.types, t_name).__doc__ or "").strip().split("\\n")[0][:200]
            except Exception:
                pass
            matches.append({{"identifier": "bpy.types." + t_name,
                             "kind": "type", "doc": doc}})
            if len(matches) >= limit:
                break
result = {{"query": query, "matches": matches, "count": len(matches)}}
"""
    return await run_tool_code(code)


@mcp.tool(name="blender_search_user_manual",
          annotations={"title": "Search User Manual",
                       "readOnlyHint": True, "destructiveHint": False,
                       "idempotentHint": True, "openWorldHint": True})
async def blender_search_user_manual(params: SearchInput) -> str:
    """Search the official Blender user manual (docs.blender.org) for pages
    matching the query, via Blender's own operator-to-manual mapping plus the
    online manual URL patterns. Requires internet access from this machine.

    Returns: str: JSON with status and result{query, pages[{title, url}]}."""
    query_literal = json.dumps(params.query.lower())
    code = f"""
import bpy
query = {query_literal}
limit = {int(params.limit or 10)}
# Use Blender's official manual-map API (validated on Blender 5.1).
pages = []
try:
    for entry in bpy.utils.manual_map():
        prefix, mapping = entry() if callable(entry) else entry
        for patterns, path in mapping:
            hay = (patterns if isinstance(patterns, str) else " ".join(patterns)).lower()
            if query in hay or query in path.lower():
                pages.append({{"match": patterns if isinstance(patterns, str) else patterns[0],
                               "url": prefix + path}})
                if len(pages) >= limit:
                    break
        if len(pages) >= limit:
            break
except Exception as e:
    pages = [{{"error": "manual mapping unavailable: %s" % e}}]
result = {{"query": query, "count": len(pages), "pages": pages}}
"""
    return await run_tool_code(code)


# ---------------------------------------------------------------------------
# WRITE / STATE-CHANGING TOOLS
# ---------------------------------------------------------------------------

_RW = {"readOnlyHint": False, "destructiveHint": True,
       "idempotentHint": False, "openWorldHint": False}
_RW_SAFE = {"readOnlyHint": False, "destructiveHint": False,
            "idempotentHint": True, "openWorldHint": False}


@mcp.tool(name="blender_execute_python", annotations={
    "title": "Execute Python Code", **_RW})
async def blender_execute_python(params: ExecuteCodeInput) -> str:
    """Execute arbitrary Python code inside the running interactive Blender.
    The primary power tool: anything bpy/bmesh/mathutils can do.

    Conventions: import modules in the code; assign a JSON-serializable dict
    to `result` to return data; print() is captured as 'stdout'; errors return
    status 'error' with the traceback in 'message'.

    Returns: str: JSON with status, result, message, stdout, stderr."""
    timeout = params.timeout if params.timeout else DEFAULT_TIMEOUT
    return await run_tool_code(params.code, timeout=timeout)


@mcp.tool(name="blender_execute_python_cli", annotations={
    "title": "Execute Python Code (CLI)", **_RW})
async def blender_execute_python_cli(params: ExecuteCodeCliInput) -> str:
    """Execute Python code on a .blend FILE ON DISK in a headless background
    Blender process, without disturbing the interactive session. Assign a
    JSON-serializable dict to `summary` to return data. Changes are DISCARDED
    unless the code explicitly calls bpy.ops.wm.save_mainfile().

    Returns: str: JSON with status and result."""
    timeout = params.timeout if params.timeout else 180.0
    return await run_in_headless_blender(params.blendfile, params.code,
                                         timeout=timeout)


@mcp.tool(name="blender_switch_workspace", annotations={
    "title": "Switch to Workspace", **_RW_SAFE})
async def blender_switch_workspace(params: WorkspaceInput) -> str:
    """Switch the Blender window to the named workspace tab (e.g. 'Layout',
    'Modeling', 'Shading', 'UV Editing'). Lists available workspaces when the
    name is not found.

    Returns: str: JSON with status and result{switched_to} or
    {found: false, available[]}."""
    name_literal = json.dumps(params.name)
    code = f"""
import bpy
name = {name_literal}
ws = bpy.data.workspaces.get(name)
if ws is None:
    match = next((w for w in bpy.data.workspaces
                  if w.name.lower() == name.lower()), None)
    ws = match
if ws is None:
    result = {{"found": False,
               "available": [w.name for w in bpy.data.workspaces]}}
else:
    bpy.context.window.workspace = ws
    result = {{"found": True, "switched_to": ws.name}}
"""
    return await run_tool_code(code)


@mcp.tool(name="blender_switch_matching_workspace", annotations={
    "title": "Switch to Matching Workspace", **_RW_SAFE})
async def blender_switch_matching_workspace(params: SpaceTypeInput) -> str:
    """Switch to whichever workspace tab contains an editor of the given space
    type (e.g. 'NODE_EDITOR' finds Shading; 'IMAGE_EDITOR' finds UV Editing).

    Returns: str: JSON with status and result{switched_to, matched_area} or
    {found: false, available[]}."""
    st_literal = json.dumps(params.space_type.upper())
    code = f"""
import bpy
space_type = {st_literal}
found = None
for ws in bpy.data.workspaces:
    for screen in ws.screens:
        for area in screen.areas:
            if area.type == space_type or area.ui_type == space_type:
                found = (ws, area.type)
                break
        if found: break
    if found: break
if found is None:
    result = {{"found": False, "requested": space_type,
               "available": sorted({{a.type for ws in bpy.data.workspaces
                                    for s in ws.screens for a in s.areas}})}}
else:
    bpy.context.window.workspace = found[0]
    result = {{"found": True, "switched_to": found[0].name,
               "matched_area": found[1]}}
"""
    return await run_tool_code(code)


def _focus_code(name_literal: str, data_mode: bool) -> str:
    extra = ""
    if data_mode:
        extra = r"""
    # jump into the object's data context: open Properties to object-data tab
    for area in bpy.context.screen.areas:
        if area.type == 'PROPERTIES':
            area.spaces.active.context = 'DATA'
            break
"""
    return f"""
import bpy
name = {name_literal}
o = bpy.data.objects.get(name)
if o is None:
    close = [x.name for x in bpy.data.objects if name.lower() in x.name.lower()]
    result = {{"found": False, "similar_names": close[:10]}}
else:
    if bpy.context.mode != 'OBJECT':
        bpy.ops.object.mode_set(mode='OBJECT')
    for other in bpy.context.view_layer.objects:
        other.select_set(False)
    o.hide_set(False)
    o.select_set(True)
    bpy.context.view_layer.objects.active = o
    area = next((a for a in bpy.context.screen.areas if a.type == 'VIEW_3D'), None)
    if area is not None:
        region = next(r for r in area.regions if r.type == 'WINDOW')
        with bpy.context.temp_override(area=area, region=region):
            bpy.ops.view3d.view_selected()
{extra}
    result = {{"found": True, "focused": o.name, "type": o.type}}
"""


@mcp.tool(name="blender_focus_object", annotations={
    "title": "Focus on Object", **_RW_SAFE})
async def blender_focus_object(params: ObjectNameInput) -> str:
    """Select the named object, make it active, unhide it, and frame it in the
    3D viewport (like pressing numpad-period). Suggests similar names when not
    found.

    Returns: str: JSON with status and result{focused, type} or
    {found: false, similar_names[]}."""
    return await run_tool_code(_focus_code(json.dumps(params.name), False))


@mcp.tool(name="blender_focus_object_data", annotations={
    "title": "Focus on Object Data", **_RW_SAFE})
async def blender_focus_object_data(params: ObjectNameInput) -> str:
    """Like Focus on Object, but additionally switches the Properties editor to
    the object-data tab so the object's mesh/curve/light data is visible.

    Returns: str: JSON with status and result{focused, type}."""
    return await run_tool_code(_focus_code(json.dumps(params.name), True))


@mcp.tool(name="blender_render_thumbnail_to_path", annotations={
    "title": "Render Thumbnail to Path", **_RW_SAFE})
async def blender_render_thumbnail_to_path(params: RenderThumbnailInput) -> str:
    """Render a square thumbnail PNG of one object (framed automatically) or of
    the current view. Uses viewport OpenGL rendering; restores the previous
    view afterwards.

    Returns: str: JSON with status and result{filepath, size, file_written}."""
    path_literal = json.dumps(params.output_path)
    name_literal = json.dumps(params.object_name) if params.object_name else "None"
    size = int(params.size or 512)
    code = f"""
import bpy, os
out_path = {path_literal}
obj_name = {name_literal}
size = {size}
scene = bpy.context.scene
prev = (scene.render.filepath, scene.render.resolution_x, scene.render.resolution_y,
        scene.render.resolution_percentage, scene.render.image_settings.file_format)
scene.render.filepath = out_path
scene.render.resolution_x = size
scene.render.resolution_y = size
scene.render.resolution_percentage = 100
scene.render.image_settings.file_format = 'PNG'
area = next((a for a in bpy.context.screen.areas if a.type == 'VIEW_3D'), None)
try:
    if area is None:
        raise RuntimeError("No 3D viewport open")
    region = next(r for r in area.regions if r.type == 'WINDOW')
    if obj_name:
        o = bpy.data.objects.get(obj_name)
        if o is None:
            raise RuntimeError("Object not found: %s" % obj_name)
        if bpy.context.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')
        for other in bpy.context.view_layer.objects:
            other.select_set(False)
        o.hide_set(False)
        o.select_set(True)
        bpy.context.view_layer.objects.active = o
        with bpy.context.temp_override(area=area, region=region):
            bpy.ops.view3d.view_selected()
    with bpy.context.temp_override(area=area, region=region):
        bpy.ops.render.opengl(write_still=True, view_context=True)
    result = {{"filepath": out_path, "size": size,
               "file_written": os.path.exists(out_path)}}
finally:
    (scene.render.filepath, scene.render.resolution_x, scene.render.resolution_y,
     scene.render.resolution_percentage, scene.render.image_settings.file_format) = prev
"""
    return await run_tool_code(code, timeout=120)


# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run()
