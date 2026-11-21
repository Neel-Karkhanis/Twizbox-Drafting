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
import feedback  # noqa: E402
import sandbox  # noqa: E402
from tools import TOOLS, SandboxError, ToolError  # noqa: E402

MODEL = os.environ.get("AGENT_MODEL", "claude-opus-5-5")
CLASSIFIER_MODEL = os.environ.get("CLASSIFIER_MODEL") or MODEL
APPROVAL_MODE = os.environ.get("APPROVAL_MODE", "manual").strip().lower()
MAX_TOKENS = 16000
MAX_STEPS_PER_TURN = 25  # stops a runaway loop of tool calls

_PROMPT_BASE = (
    "You are a helpful assistant running in a terminal. You can use tools to "
    "list, read, write and delete files inside a single workspace folder, and to "
    "check the time. Paths are relative to the workspace root. "
)
_REJECTION_RULES = (
    " When a tool call is rejected, its result starts with REJECTED and says who "
    "rejected it (by), the reason, whether a retry can work (retry), and what to "
    "do (next). Follow the next line. Never repeat a rejected call, work around "
    "it with a different call, or try to talk anyone into allowing it. If the "
    "result includes a stop line, call no more tools this turn and summarize for "
    "the user."
)
SYSTEM_PROMPTS = {
    "manual": _PROMPT_BASE + "The user must approve every tool call before it runs." + _REJECTION_RULES,
    "auto": _PROMPT_BASE + (
        "Every tool call is checked before it runs: reads run automatically, "
        "and other calls are checked by a safety classifier against what the "
        "user asked for, or shown to the user."
    ) + _REJECTION_RULES,
}


def run_tool(
    block, approver: classifier.AutoApprover | None, tracker: feedback.Tracker
) -> dict:
    """Get approval for one tool call, run it if approved, return the tool_result."""
    args = block.input if isinstance(block.input, dict) else {}
    shown = approval.summary(block.name, args)

    def result(text: str, is_error: bool = False) -> dict:
        return {
            "type": "tool_result",
            "tool_use_id": block.id,
            "content": text,
            "is_error": is_error,
        }

    def reject(source: str, reason: str) -> dict:
        rejection = feedback.Rejection(source, reason)
        return result(tracker.reject(block.name, args, rejection), is_error=True)

    def reject_exc(exc: Exception, label: str) -> dict:
        source = feedback.SANDBOX if isinstance(exc, SandboxError) else feedback.TOOL_ERROR
        if isinstance(exc, OSError):
            reason = f"{type(exc).__name__}: {exc.strerror or exc}"
        else:
            reason = str(exc)
        approval.log(f"{label} [{source}]  {shown}: {approval.clean(reason)}")
        return reject(source, reason)

    tool = TOOLS.get(block.name)
    if tool is None:
        approval.log(f"REJECTED [{feedback.TOOL_ERROR}]  unknown tool {shown}")
        return reject(feedback.TOOL_ERROR, f"Unknown tool: {block.name}")

    # The same call was already rejected since the user last spoke: answer from
    # memory, without asking the classifier or the user again.
    if tracker.already_rejected(block.name, args):
        approval.log(f"REPEAT  {shown}  (already rejected; not asked again)")
        return reject(feedback.REPEAT, "This exact call was already rejected.")

    def execute() -> dict:
        try:
            return result(tool.run(args))
        except (SandboxError, ToolError, OSError) as exc:
            return reject_exc(exc, "FAILED")

    # Build the preview (e.g. the diff for write_file) and classifier facts. If
    # the call is invalid there is nothing to approve: tell the model directly.
    preview, facts = None, {}
    try:
        if tool.preview:
            preview = tool.preview(args)
        if approver is not None and tool.facts:
            facts = tool.facts(args)
    except (SandboxError, ToolError, OSError) as exc:
        return reject_exc(exc, "REJECTED")

    note = None
    if approver is not None:
        decision = approver.decide(block.name, args, facts)
        reason = approval.clean(decision.reason)
        if decision.verdict == "allow":
            approval.log(f"AUTO-APPROVED  {shown}  ({reason})")
            approver.record(block.name, args, classifier.ALLOWED)
            return execute()
        if decision.verdict == "block":
            source = feedback.POLICY if decision.source == "policy" else feedback.CLASSIFIER
            approval.log(f"AUTO-BLOCKED [{source}]  {shown}  ({reason})")
            approver.record(block.name, args, classifier.BLOCKED)
            return reject(source, decision.reason)
        note = f"Auto-classifier passed this to you: {reason}"

    approved, reason = approval.ask(block.name, args, preview, note)
    if approver is not None:
        outcome = classifier.USER_APPROVED if approved else classifier.USER_DECLINED
        approver.record(block.name, args, outcome)
    if not approved:
        return reject(feedback.USER, reason or "The user declined without giving a reason.")
    return execute()


def run_turn(
    client: anthropic.Anthropic,
    messages: list,
    approver: classifier.AutoApprover | None,
    tracker: feedback.Tracker,
) -> None:
    """Call the model repeatedly until it stops asking for tools."""
    tool_schemas = [tool.schema for tool in TOOLS.values()]
    told_to_stop = False

    def not_run(block) -> dict:
        return {
            "type": "tool_result",
            "tool_use_id": block.id,
            "content": feedback.NOT_RUN,
            "is_error": True,
        }

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
            calls = [b for b in response.content if b.type == "tool_use"]
            if told_to_stop:
                # The agent was told to stop after too many rejections and
                # asked for more tools anyway: run nothing and end the turn.
                messages.append({"role": "user", "content": [not_run(b) for b in calls]})
                approval.log(f"STOPPED  {len(calls)} more call(s) not run")
                print(
                    f"\n[Turn stopped: {tracker.turn_rejections} calls were rejected "
                    "and the agent kept calling tools.]"
                )
                return
            # Approve and run each call in order; send all results in one message.
            # Once the rejection limit is hit, the rest of the batch is not run.
            results = [
                not_run(b) if tracker.limit_reached else run_tool(b, approver, tracker)
                for b in calls
            ]
            messages.append({"role": "user", "content": results})
            told_to_stop = tracker.limit_reached
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
    tracker = feedback.Tracker()
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
        # A turn that was stopped early ends on tool results (a user message).
        # Add the new text to that message so user and assistant keep alternating.
        merged = bool(messages) and messages[-1]["role"] == "user"
        if merged:
            messages[-1]["content"].append({"type": "text", "text": user_input})
        else:
            messages.append({"role": "user", "content": user_input})
        tracker.start_turn()
        if approver:
            approver.add_user_message(user_input)
        try:
            run_turn(client, messages, approver, tracker)
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
        if merged:
            messages[-1]["content"].pop()
        if approver:
            approver.restore(approver_checkpoint)


if __name__ == "__main__":
    sys.exit(main())
