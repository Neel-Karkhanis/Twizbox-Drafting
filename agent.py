"""Terminal agent: the model asks for utilities, the user approves each call.

Run:  python agent.py     (or ./run.ps1 to run it inside Docker)
"""

import os
import sys
from pathlib import Path


def load_env_file() -> None:
    """Read KEY=value lines from .env next to this file (real env vars win)."""
    env_path = Path(__file__).parent / ".env"
    if not env_path.is_file():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


load_env_file()

import anthropic  # noqa: E402

import approval  # noqa: E402
import sandbox  # noqa: E402
from tools import TOOLS, SandboxError, ToolError  # noqa: E402

MODEL = os.environ.get("AGENT_MODEL", "claude-opus-5-5")
MAX_TOKENS = 16000
MAX_STEPS_PER_TURN = 25  # stops a runaway loop of tool calls

SYSTEM_PROMPT = (
    "You are a helpful assistant running in a terminal. You can use tools to "
    "list, read and write files inside a single workspace folder, and to check "
    "the time. Paths are relative to the workspace root. The user must approve "
    "every tool call before it runs. If the user declines a call, do not retry "
    "the same call; change approach or ask what they would prefer."
)


def run_tool(block) -> dict:
    """Ask the user about one tool call, run it if approved, return the tool_result."""
    def result(text: str, is_error: bool = False) -> dict:
        return {
            "type": "tool_result",
            "tool_use_id": block.id,
            "content": text,
            "is_error": is_error,
        }

    tool = TOOLS.get(block.name)
    if tool is None:
        return result(f"Unknown tool: {block.name}", is_error=True)
    args = block.input if isinstance(block.input, dict) else {}

    # Build the preview (e.g. the diff for write_file). If the call is invalid
    # there is nothing to approve: tell the model without bothering the user.
    preview = None
    if tool.preview:
        try:
            preview = tool.preview(args)
        except (SandboxError, ToolError) as exc:
            approval.log(f"REJECTED  {block.name}: {exc}")
            return result(f"Error: {exc}", is_error=True)

    approved, reason = approval.ask(block.name, args, preview)
    if not approved:
        message = "User declined this action."
        if reason:
            message += f" Reason: {reason}"
        return result(message, is_error=True)

    try:
        return result(tool.run(args))
    except (SandboxError, ToolError) as exc:
        approval.log(f"FAILED  {block.name}: {exc}")
        return result(f"Error: {exc}", is_error=True)
    except OSError as exc:
        approval.log(f"FAILED  {block.name}: {exc}")
        return result(f"Error: {type(exc).__name__}: {exc.strerror or exc}", is_error=True)


def run_turn(client: anthropic.Anthropic, messages: list) -> None:
    """Call the model repeatedly until it stops asking for tools."""
    tool_schemas = [tool.schema for tool in TOOLS.values()]

    for _ in range(MAX_STEPS_PER_TURN):
        response = client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=SYSTEM_PROMPT,
            tools=tool_schemas,
            messages=messages,
        )
        # Keep the full content (not just text) so tool calls stay in history.
        messages.append({"role": "assistant", "content": response.content})

        for block in response.content:
            if block.type == "text" and block.text.strip():
                print(f"\nAgent: {approval.clean(block.text)}")

        if response.stop_reason == "tool_use":
            # Approve and run each call in order; send all results in one message.
            results = [run_tool(b) for b in response.content if b.type == "tool_use"]
            messages.append({"role": "user", "content": results})
            continue

        if response.stop_reason == "max_tokens":
            print("\n[Reply was cut off at the output limit.]")
        elif response.stop_reason == "refusal":
            print("\n[The model declined this request.]")
        return

    print(f"\n[Stopped after {MAX_STEPS_PER_TURN} steps in one turn.]")


def main() -> int:
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        print("No API key found. Copy .env.example to .env and fill in your key.")
        return 1

    client = anthropic.Anthropic()  # reads key and ANTHROPIC_BASE_URL from the env
    sandbox.WORKSPACE.mkdir(parents=True, exist_ok=True)

    print(f"Model:     {MODEL}")
    print(f"Workspace: {sandbox.WORKSPACE}")
    print("Type a request. 'exit' or Ctrl+C to quit.")

    messages: list = []
    while True:
        try:
            user_input = input("\nYou: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not user_input:
            continue
        if user_input.lower() in ("exit", "quit"):
            return 0

        checkpoint = len(messages)
        messages.append({"role": "user", "content": user_input})
        try:
            run_turn(client, messages)
        except KeyboardInterrupt:
            print("\n[Turn cancelled.]")
            del messages[checkpoint:]
        except anthropic.AuthenticationError:
            print("\n[API error: the key was rejected. Check .env.]")
            del messages[checkpoint:]
        except anthropic.RateLimitError:
            print("\n[API error: rate limited or out of credit. Try again shortly.]")
            del messages[checkpoint:]
        except anthropic.APIStatusError as exc:
            print(f"\n[API error {exc.status_code}: {exc.message}]")
            del messages[checkpoint:]
        except anthropic.APIConnectionError:
            print("\n[Network error: could not reach the API.]")
            del messages[checkpoint:]


if __name__ == "__main__":
    sys.exit(main())
