"""Terminal agent: the model asks for utilities, and each call is approved by the
user (APPROVAL_MODE=manual, the default) or by an auto-classifier that falls back
to the user when unsure (APPROVAL_MODE=auto).

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
import classifier  # noqa: E402
import sandbox  # noqa: E402
from tools import TOOLS, SandboxError, ToolError  # noqa: E402

MODEL = os.environ.get("AGENT_MODEL", "claude-opus-5-5")
CLASSIFIER_MODEL = os.environ.get("CLASSIFIER_MODEL") or MODEL
APPROVAL_MODE = os.environ.get("APPROVAL_MODE", "manual").strip().lower()
MAX_TOKENS = 16000
MAX_STEPS_PER_TURN = 25  # stops a runaway loop of tool calls

_PROMPT_BASE = (
    "You are a helpful assistant running in a terminal. You can use tools to "
    "list, read and write files inside a single workspace folder, and to check "
    "the time. Paths are relative to the workspace root. "
)
SYSTEM_PROMPTS = {
    "manual": _PROMPT_BASE + (
        "The user must approve every tool call before it runs. If the user "
        "declines a call, do not retry the same call; change approach or ask "
        "what they would prefer."
    ),
    "auto": _PROMPT_BASE + (
        "Every tool call is checked before it runs: reads run automatically, "
        "and other calls are checked by a safety classifier against what the "
        "user asked for, or shown to the user. If a call is blocked or "
        "declined, do not retry the same call; change approach or ask the user "
        "what they would prefer."
    ),
}


def run_tool(block, approver: classifier.AutoApprover | None) -> dict:
    """Get approval for one tool call, run it if approved, return the tool_result."""
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

    def execute() -> dict:
        try:
            return result(tool.run(args))
        except (SandboxError, ToolError) as exc:
            approval.log(f"FAILED  {block.name}: {exc}")
            return result(f"Error: {exc}", is_error=True)
        except OSError as exc:
            approval.log(f"FAILED  {block.name}: {exc}")
            return result(f"Error: {type(exc).__name__}: {exc.strerror or exc}", is_error=True)

    # Build the preview (e.g. the diff for write_file) and classifier facts. If
    # the call is invalid there is nothing to approve: tell the model directly.
    preview, facts = None, {}
    try:
        if tool.preview:
            preview = tool.preview(args)
        if approver is not None and tool.facts:
            facts = tool.facts(args)
    except (SandboxError, ToolError) as exc:
        approval.log(f"REJECTED  {block.name}: {exc}")
        return result(f"Error: {exc}", is_error=True)

    note = None
    if approver is not None:
        decision = approver.decide(block.name, args, facts)
        reason = approval.clean(decision.reason)
        if decision.verdict == "allow":
            approval.log(f"AUTO-APPROVED  {approval.summary(block.name, args)}  ({reason})")
            approver.record(block.name, args, classifier.ALLOWED)
            return execute()
        if decision.verdict == "block":
            approval.log(f"AUTO-BLOCKED  {approval.summary(block.name, args)}  ({reason})")
            approver.record(block.name, args, classifier.BLOCKED)
            return result(f"Blocked by the safety classifier. Reason: {decision.reason}", is_error=True)
        note = f"Auto-classifier passed this to you: {reason}"

    approved, reason = approval.ask(block.name, args, preview, note)
    if approver is not None:
        outcome = classifier.USER_APPROVED if approved else classifier.USER_DECLINED
        approver.record(block.name, args, outcome)
    if not approved:
        message = "User declined this action."
        if reason:
            message += f" Reason: {reason}"
        return result(message, is_error=True)
    return execute()


def run_turn(
    client: anthropic.Anthropic, messages: list, approver: classifier.AutoApprover | None
) -> None:
    """Call the model repeatedly until it stops asking for tools."""
    tool_schemas = [tool.schema for tool in TOOLS.values()]

    for _ in range(MAX_STEPS_PER_TURN):
        response = client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=SYSTEM_PROMPTS[APPROVAL_MODE],
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
            results = [run_tool(b, approver) for b in response.content if b.type == "tool_use"]
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
    if APPROVAL_MODE not in SYSTEM_PROMPTS:
        print(f"APPROVAL_MODE must be 'manual' or 'auto', not {APPROVAL_MODE!r}.")
        return 1

    client = anthropic.Anthropic()  # reads key and ANTHROPIC_BASE_URL from the env
    sandbox.WORKSPACE.mkdir(parents=True, exist_ok=True)
    approver = (
        classifier.AutoApprover(client, CLASSIFIER_MODEL) if APPROVAL_MODE == "auto" else None
    )

    print(f"Model:     {MODEL}")
    print(f"Workspace: {sandbox.WORKSPACE}")
    if approver:
        print(f"Approval:  auto (classifier: {CLASSIFIER_MODEL}; asks you when unsure)")
    else:
        print("Approval:  manual (you approve every call)")
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
        approver_checkpoint = approver.checkpoint() if approver else None
        messages.append({"role": "user", "content": user_input})
        if approver:
            approver.add_user_message(user_input)
        try:
            run_turn(client, messages, approver)
            continue
        except KeyboardInterrupt:
            print("\n[Turn cancelled.]")
        except anthropic.AuthenticationError:
            print("\n[API error: the key was rejected. Check .env.]")
        except anthropic.RateLimitError:
            print("\n[API error: rate limited or out of credit. Try again shortly.]")
        except anthropic.APIStatusError as exc:
            print(f"\n[API error {exc.status_code}: {exc.message}]")
        except anthropic.APIConnectionError:
            print("\n[Network error: could not reach the API.]")
        # The turn failed: forget it so the history stays consistent.
        del messages[checkpoint:]
        if approver:
            approver.restore(approver_checkpoint)


if __name__ == "__main__":
    sys.exit(main())
