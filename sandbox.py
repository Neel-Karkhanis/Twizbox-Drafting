"""Filesystem jail: every file tool goes through safe_path() before touching disk."""

import fnmatch
import os
import shutil
from datetime import datetime
from pathlib import Path

# The only folder the agent may touch. Docker sets AGENT_WORKSPACE=/workspace.
WORKSPACE = Path(
    os.environ.get("AGENT_WORKSPACE", Path(__file__).parent / "workspace")
).resolve()

BACKUP_DIR_NAME = ".backups"

# Names the agent may never read, write or list, even with approval.
BLOCKED_PATTERNS = [
    ".env", ".env.*", "*.key", "*.pem", "*.pfx", "id_rsa*", "id_ed25519*",
    ".git", ".ssh", BACKUP_DIR_NAME,
]

MAX_READ_BYTES = 200_000
MAX_WRITE_BYTES = 1_000_000
MAX_LIST_ENTRIES = 200


class SandboxError(Exception):
    """Raised when a tool call would break a sandbox rule."""


def is_blocked_name(name: str) -> bool:
    lowered = name.lower()
    return any(fnmatch.fnmatch(lowered, pattern) for pattern in BLOCKED_PATTERNS)


def safe_path(user_path: str) -> Path:
    """Resolve a model-supplied path and refuse anything outside the workspace."""
    if not isinstance(user_path, str) or not user_path.strip():
        raise SandboxError("Path must be a non-empty string.")
    if "\x00" in user_path:
        raise SandboxError("Path contains a null byte.")

    WORKSPACE.mkdir(parents=True, exist_ok=True)
    # resolve() collapses ".." and follows symlinks/junctions, so the check
    # below sees where the path really lands.
    resolved = (WORKSPACE / user_path).resolve()
    if resolved != WORKSPACE and not resolved.is_relative_to(WORKSPACE):
        raise SandboxError(f"Path escapes the workspace: {user_path!r}")

    for part in resolved.relative_to(WORKSPACE).parts:
        if is_blocked_name(part):
            raise SandboxError(f"Access to {part!r} is blocked by the sandbox.")
    return resolved


def display_path(path: Path) -> str:
    """Workspace-relative path for showing to the user and the model."""
    rel = path.relative_to(WORKSPACE).as_posix()
    return rel or "."


def backup_file(path: Path) -> Path:
    """Copy an existing file into workspace/.backups/<timestamp>/ before overwrite."""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    target = WORKSPACE / BACKUP_DIR_NAME / stamp / path.relative_to(WORKSPACE)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, target)
    return target
