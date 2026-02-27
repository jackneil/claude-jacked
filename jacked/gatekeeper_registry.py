"""Gatekeeper tool registry — single source of truth for tool metadata.

Lightweight module with no heavy imports, safe to import from CLI and API.
The gatekeeper subprocess uses a hardcoded copy of defaults (cannot import
this module at runtime) — see _read_enabled_tools() in security_gatekeeper.py.
A sync test in tests/unit/test_security_gatekeeper.py verifies both copies match.
"""

GATEKEEPER_TOOL_REGISTRY: dict[str, dict] = {
    # Default enabled (current 9)
    "Bash":         {"label": "Bash",         "desc": "Shell command execution",     "category": "shell",      "default_enabled": True,  "locked": True},
    "Read":         {"label": "Read",         "desc": "Read file contents",          "category": "file_read",  "default_enabled": True,  "locked": False},
    "Edit":         {"label": "Edit",         "desc": "Edit existing files",         "category": "file_write", "default_enabled": True,  "locked": False},
    "Write":        {"label": "Write",        "desc": "Create or overwrite files",   "category": "file_write", "default_enabled": True,  "locked": False},
    "Grep":         {"label": "Grep",         "desc": "Search file contents",        "category": "file_read",  "default_enabled": True,  "locked": False},
    "Glob":         {"label": "Glob",         "desc": "Find files by pattern",       "category": "file_read",  "default_enabled": True,  "locked": False},
    "NotebookEdit": {"label": "NotebookEdit", "desc": "Edit Jupyter notebooks",      "category": "file_write", "default_enabled": True,  "locked": False},
    "WebFetch":     {"label": "WebFetch",     "desc": "Fetch web page content",      "category": "web",        "default_enabled": True,  "locked": False},
    "WebSearch":    {"label": "WebSearch",    "desc": "Search the web",              "category": "web",        "default_enabled": True,  "locked": False},
    # Available but off by default
    "Search":       {"label": "Search",       "desc": "Find files by pattern (alt)", "category": "file_read",  "default_enabled": False, "locked": False},
    "LS":           {"label": "LS",           "desc": "List directory contents",      "category": "file_read",  "default_enabled": False, "locked": False},
    "NotebookRead": {"label": "NotebookRead", "desc": "Read Jupyter notebooks",      "category": "file_read",  "default_enabled": False, "locked": False},
    # MCP tools — pattern-matched via mcp__ prefix, not individual tool names.
    # Convention: mcp__<server>__<tool> (e.g. mcp__claude-in-chrome__navigate).
    "MCPTools":     {"label": "MCP Tools",    "desc": "MCP server tools — auto-approve and log (mcp__*)", "category": "mcp", "default_enabled": True,  "locked": False},
}


def get_default_tools() -> list[str]:
    """Tool names enabled by default."""
    return [k for k, v in GATEKEEPER_TOOL_REGISTRY.items() if v["default_enabled"]]


def get_all_tools() -> list[str]:
    """All known tool names."""
    return list(GATEKEEPER_TOOL_REGISTRY.keys())


def get_locked_tools() -> set[str]:
    """Tool names that cannot be disabled."""
    return {k for k, v in GATEKEEPER_TOOL_REGISTRY.items() if v["locked"]}
