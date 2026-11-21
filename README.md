# Twizbox agent (draft)

A terminal agent where the model can ask for a utility. By default nothing runs
until you type `y`, and there is no "always allow". An optional auto mode lets a
classifier model approve calls that match what you asked for (see below).

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

## Approval modes

Set `APPROVAL_MODE` in `.env`:

- `manual` (default): every call is shown and needs an explicit `y`.
- `auto`: modelled on Claude Code's auto mode.
  - Reads (`list_directory`, `read_file`, `get_current_time`) run without asking.
  - Writes go to a classifier: a second call to the same model and key
    (`CLASSIFIER_MODEL` overrides the model). It sees only what you typed and the
    tool calls the agent made (names and arguments). It never sees the agent's
    own text or any tool results, so a file the agent read can't talk it into
    anything.
  - If the classifier blocks a call, the reason goes back to the agent.
  - After 3 blocks in a row (until something is approved), or 10 in a session,
    calls come to you instead.
  - If the classifier errors or gives an unusable answer, the call comes to you.
    It never allows anything by default.

## Safety layers

1. **Approval** (`approval.py`, `classifier.py`): you or the classifier approve
   each call, as set by `APPROVAL_MODE`. A "no" is sent back to the model with
   the reason.
2. **Path jail** (`sandbox.py`): tools can only touch `workspace/`. Paths that
   escape it, and names like `.env`, `*.key`, `.git`, are refused. Reads are
   capped at 200 KB and writes at 1 MB.
3. **Backups**: before an overwrite, the old file is copied to
   `workspace/.backups/<timestamp>/`. The agent can't see or change that folder.
4. **Docker** (`run.ps1`): non-root user, read-only filesystem, no Linux
   capabilities, and only `workspace/` mounted from your machine. The container
   still has network access, which it needs to reach the model.

## Files

- `agent.py`: chat loop; gets approval, then runs the tool
- `approval.py`: the y/n prompt
- `classifier.py`: the auto-mode classifier
- `tools.py`: the four utilities and their schemas
- `sandbox.py`: path jail, limits, backups
- `Dockerfile`, `run.ps1`: container setup

## Adding a utility

Write a function in `tools.py` that takes the argument dict and returns a string,
then add it to `TOOLS` with a schema. It gets the approval prompt automatically.
In auto mode it goes to the classifier unless you add it to `READ_ONLY_TOOLS` in
`classifier.py`, which you should only do for tools that change nothing.
Use `sandbox.safe_path()` for any file path.
