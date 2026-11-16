# Twizbox agent (draft)

A terminal agent where the model can ask for a utility, and nothing runs until you
type `y`. There is no "always allow".

## Setup

1. Copy `.env.example` to `.env` and put your key in it.
2. Run it in the Docker sandbox (recommended):

   ```powershell
   .\run.ps1
   ```

   Or directly on your machine (path jail and backups only, no container):

   ```powershell
   pip install -r requirements.txt
   python agent.py
   ```

Type `exit` or press Ctrl+C to quit.

## Utilities

| Tool | What it does |
|---|---|
| `list_directory` | Lists a workspace folder |
| `read_file` | Reads a UTF-8 text file |
| `write_file` | Creates or overwrites a file (shows a diff first, keeps a backup) |
| `get_current_time` | Returns the local date and time |

## Safety layers

1. **Approval** (`approval.py`): every call is shown and needs an explicit `y`.
   A "no" is sent back to the model as "User declined", with your reason if given.
2. **Path jail** (`sandbox.py`): tools can only touch `workspace/`. Paths that
   escape it, and names like `.env`, `*.key`, `.git`, are refused. Reads are
   capped at 200 KB and writes at 1 MB.
3. **Backups**: before an overwrite, the old file is copied to
   `workspace/.backups/<timestamp>/`. The agent can't see or change that folder.
4. **Docker** (`run.ps1`): non-root user, read-only filesystem, no Linux
   capabilities, and only `workspace/` mounted from your machine. The container
   still has network access, which it needs to reach the model.

## Files

- `agent.py`: chat loop; asks for approval, then runs the tool
- `approval.py`: the y/n prompt
- `tools.py`: the four utilities and their schemas
- `sandbox.py`: path jail, limits, backups
- `Dockerfile`, `run.ps1`: container setup

## Adding a utility

Write a function in `tools.py` that takes the argument dict and returns a string,
then add it to `TOOLS` with a schema. It gets the approval prompt automatically.
Use `sandbox.safe_path()` for any file path.
