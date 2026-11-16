"""The utilities the agent can request. None of them run without user approval
(enforced in agent.py), and all file access goes through sandbox.safe_path()."""

import difflib
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

import sandbox
from sandbox import SandboxError

MAX_PREVIEW_LINES = 60


class ToolError(Exception):
    """A tool failed in a way the model should be told about."""


def _require_str(args: dict, key: str) -> str:
    value = args.get(key)
    if not isinstance(value, str):
        raise ToolError(f"Missing or non-string argument: {key!r}")
    return value


def _read_text(path) -> str:
    if path.stat().st_size > sandbox.MAX_READ_BYTES:
        raise ToolError(
            f"File is larger than the {sandbox.MAX_READ_BYTES:,}-byte read limit."
        )
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise ToolError("File is not UTF-8 text.") from None


def _clip(lines: list[str]) -> str:
    if len(lines) > MAX_PREVIEW_LINES:
        hidden = len(lines) - MAX_PREVIEW_LINES
        lines = lines[:MAX_PREVIEW_LINES] + [f"... ({hidden} more lines not shown)"]
    return "\n".join(lines)


# --- list_directory ---------------------------------------------------------

def list_directory(args: dict) -> str:
    path = sandbox.safe_path(args.get("path") or ".")
    if not path.is_dir():
        raise ToolError(f"Not a directory: {sandbox.display_path(path)}")
    entries = sorted(
        (e for e in path.iterdir() if not sandbox.is_blocked_name(e.name)),
        key=lambda e: (e.is_file(), e.name.lower()),
    )
    if not entries:
        return "(empty directory)"
    lines = [
        f"{e.name}/" if e.is_dir() else f"{e.name}  ({e.stat().st_size:,} bytes)"
        for e in entries[: sandbox.MAX_LIST_ENTRIES]
    ]
    if len(entries) > sandbox.MAX_LIST_ENTRIES:
        lines.append(f"... ({len(entries) - sandbox.MAX_LIST_ENTRIES} more entries)")
    return "\n".join(lines)


# --- read_file --------------------------------------------------------------

def read_file(args: dict) -> str:
    path = sandbox.safe_path(_require_str(args, "path"))
    if not path.is_file():
        raise ToolError(f"File not found: {sandbox.display_path(path)}")
    return _read_text(path)


# --- write_file -------------------------------------------------------------

def _check_write(args: dict):
    path = sandbox.safe_path(_require_str(args, "path"))
    content = _require_str(args, "content")
    if path.is_dir():
        raise ToolError(f"Path is a directory: {sandbox.display_path(path)}")
    if len(content.encode("utf-8")) > sandbox.MAX_WRITE_BYTES:
        raise ToolError(
            f"Content is larger than the {sandbox.MAX_WRITE_BYTES:,}-byte write limit."
        )
    return path, content


def write_file(args: dict) -> str:
    path, content = _check_write(args)
    note = ""
    if path.exists():
        backup = sandbox.backup_file(path)
        note = f" Previous version backed up to {sandbox.display_path(backup)}."
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="")
    return f"Wrote {len(content):,} characters to {sandbox.display_path(path)}.{note}"


def preview_write_file(args: dict) -> str:
    """Shown in the approval prompt: a diff for overwrites, the content for new files."""
    path, content = _check_write(args)
    if not path.exists():
        return "NEW FILE. Content:\n" + _clip(content.splitlines())
    try:
        old = _read_text(path)
    except ToolError as exc:
        return f"OVERWRITES an existing file that can't be diffed ({exc})"
    diff = list(
        difflib.unified_diff(
            old.splitlines(), content.splitlines(), "current", "proposed", lineterm=""
        )
    )
    if not diff:
        return "OVERWRITES existing file with identical content (no changes)."
    return "OVERWRITES existing file (a backup is kept). Diff:\n" + _clip(diff)


# --- get_current_time -------------------------------------------------------

def get_current_time(args: dict) -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")


# --- registry ---------------------------------------------------------------

@dataclass
class Tool:
    schema: dict
    run: Callable[[dict], str]
    preview: Callable[[dict], str] | None = None


def _schema(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {
        "name": name,
        "description": description,
        "input_schema": {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        },
    }


_PATH_DESC = "Path relative to the workspace root, e.g. 'notes/todo.txt'."

TOOLS: dict[str, Tool] = {
    "list_directory": Tool(
        _schema(
            "list_directory",
            "List the files and folders in a workspace directory.",
            {"path": {"type": "string", "description": _PATH_DESC + " Use '.' for the root."}},
            ["path"],
        ),
        list_directory,
    ),
    "read_file": Tool(
        _schema(
            "read_file",
            "Read a UTF-8 text file from the workspace.",
            {"path": {"type": "string", "description": _PATH_DESC}},
            ["path"],
        ),
        read_file,
    ),
    "write_file": Tool(
        _schema(
            "write_file",
            "Create or overwrite a text file in the workspace with the full new "
            "content. Parent folders are created as needed.",
            {
                "path": {"type": "string", "description": _PATH_DESC},
                "content": {"type": "string", "description": "Complete file content."},
            },
            ["path", "content"],
        ),
        write_file,
        preview_write_file,
    ),
    "get_current_time": Tool(
        _schema("get_current_time", "Get the current local date and time.", {}, []),
        get_current_time,
    ),
}

__all__ = ["TOOLS", "Tool", "ToolError", "SandboxError"]
