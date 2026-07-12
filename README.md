# blender-mcp

An MCP server that lets AI coding agents control Blender — 26 tools bridging
to the Blender MCP add-on you already have installed (TCP port 9876), plus
an Agent Skill (`SKILL.md`) that teaches an agent how to use them well.
Works with any MCP-capable, Agent-Skills-capable agent: Codex CLI, Grok
Build, Cursor, Antigravity, Claude Code, and others.

CLI-variant tools launch a headless background Blender to inspect `.blend`
files on disk without touching your open session.

## Prerequisites

1. Blender with the MCP add-on installed and its server running (port 9876).
2. Python 3.10+, with the `mcp` package installed for whichever Python your
   agent will launch this server with:

       pip install mcp

3. Clone this repo somewhere on disk. All setup below refers to
   `<repo>/blender_mcp_server.py` — substitute your actual clone path.

## Setup per platform

Every platform below wants the same two things: (1) register
`blender_mcp_server.py` as an MCP server, and (2) make
`skill/blender-mcp/SKILL.md` discoverable so the agent knows how to use it.
Skip step 2 for a platform with no skill support — the MCP tools still work,
the agent just won't have the extra guidance.

### Codex CLI

Add to `config.toml` (default `~/.codex/config.toml` — check `codex doctor`
for `CODEX_HOME` if you've customized it):

```toml
[mcp_servers.blender]
command = "python"
args = ["<repo>/blender_mcp_server.py"]
```

Copy the skill so Codex can discover it:

    <CODEX_HOME>/skills/blender-mcp/SKILL.md

Verify: `codex mcp list` should show `blender`; `codex exec "Which skill do
you have for Blender work?"` should mention `blender-mcp`.

### Grok Build CLI

    grok mcp add blender python -- <repo>/blender_mcp_server.py

Copy the skill to `~/.agents/skills/blender-mcp/SKILL.md` (shared convention,
see below) or `.agents/skills/blender-mcp/SKILL.md` inside a specific repo
for project scope.

Verify: `grok mcp list` and `grok inspect` should both show `blender-mcp`.

### Cursor

Add to `.cursor/mcp.json` (project) or `~/.cursor/mcp.json` (global):

```json
{
  "mcpServers": {
    "blender": {
      "command": "python",
      "args": ["<repo>/blender_mcp_server.py"]
    }
  }
}
```

Copy the skill folder into `.cursor/skills/blender-mcp/` in your project so
Cursor discovers it on the next session.

### Antigravity (CLI, IDE, Antigravity 2.0)

Add to `~/.gemini/config/mcp_config.json`:

```json
{
  "mcpServers": {
    "blender": {
      "command": "python",
      "args": ["<repo>/blender_mcp_server.py"]
    }
  }
}
```

Copy the skill to `~/.gemini/config/skills/blender-mcp/` — the one location
recognized by all Antigravity flavors (CLI, IDE, 2.0). Project-scoped skills
also work at `.agents/skills/blender-mcp/` inside a workspace.

### Any other Agent Skills / MCP-compatible platform

`SKILL.md` is an open, cross-agent standard (same format used by Claude
Code, Codex, Cursor, Antigravity, and others) — copying
`skill/blender-mcp/` as-is into whatever skills directory your agent reads
is normally enough. For MCP registration, look for a `mcpServers` (JSON) or
`[mcp_servers.*]` (TOML) config block and add:

    command: python
    args: ["<repo>/blender_mcp_server.py"]

The `.agents/skills/` directory (project-level) is an emerging shared
convention several of the above tools already read — worth trying first if
your platform isn't listed here.

## Tools (26)

Read-only (19):
- blender_get_blendfile_datablocks_summary (+ _cli)
- blender_get_blendfile_missing_files_summary (+ _cli)
- blender_get_blendfile_linked_libraries_summary (+ _cli)
- blender_get_blendfile_path_info_summary (+ _cli)
- blender_get_blendfile_usage_summary (+ _cli)
- blender_get_objects_summary
- blender_get_object_detail_summary
- blender_get_python_api_docs
- blender_get_window_screenshot   (returns inline PNG)
- blender_get_area_screenshot     (returns inline PNG)
- blender_get_screenshot_as_json
- blender_render_viewport_to_path
- blender_search_api_docs
- blender_search_user_manual

Write / state-changing (7):
- blender_execute_python          (the power tool - arbitrary bpy code)
- blender_execute_python_cli      (arbitrary code on a .blend file, headless)
- blender_switch_workspace
- blender_switch_matching_workspace
- blender_focus_object
- blender_focus_object_data
- blender_render_thumbnail_to_path

## Conventions

- Interactive tools: executed code assigns a JSON-serializable dict to `result`.
- CLI tools: executed code assigns the dict to `summary`; changes to the
  .blend file are DISCARDED unless the code calls bpy.ops.wm.save_mainfile().
- CLI tools auto-detect blender.exe from the running instance; override with
  the BLENDER_BINARY environment variable if Blender is closed.

## Env vars

BLENDER_MCP_HOST (default localhost), BLENDER_MCP_PORT (default 9876),
BLENDER_MCP_TIMEOUT (default 120 s), BLENDER_BINARY (path to blender.exe).

## Notes

- Requests are short-lived one-shot connections, so multiple agents can
  share the same Blender instance — just avoid firing heavy operations from
  more than one at the same instant.
- Screenshot tools return images inline as MCP image content; clients that
  render images can literally see your viewport.
