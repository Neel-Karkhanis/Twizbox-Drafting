"""Structured feedback for the agent when a tool call is rejected.

Every rejection tells the agent who rejected the call, why, whether a retry can
work, and what to do next. The retry and next-step text is fixed per source;
only the reason varies. The tracker also stops repeats of a rejected call and
caps rejections per turn.
"""

import json
from dataclasses import dataclass

# Who rejected the call.
SANDBOX = "sandbox"
POLICY = "policy"
CLASSIFIER = "auto-classifier"
USER = "user"
TOOL_ERROR = "tool error"
REPEAT = "repeat guard"

MAX_REJECTIONS_PER_TURN = 3
MAX_REASON_CHARS = 500

# source -> (can a retry work?, what the agent should do next)
_GUIDANCE = {
    SANDBOX: (
        "never",
        "Use a different path inside the workspace. Never ask the user to get "
        "around the sandbox.",
    ),
    POLICY: (
        "never, this session",
        "Stop trying this action, with any arguments. Tell the user it is "
        "turned off.",
    ),
    CLASSIFIER: (
        "no",
        "Don't repeat this call or work around it with a different one. Tell "
        "the user it was blocked and the reason. Don't ask them to approve it.",
    ),
    USER: (
        "no",
        "Don't repeat this call. Use the user's reason to change approach, or "
        "ask what they would prefer.",
    ),
    TOOL_ERROR: (
        "yes, with corrected arguments",
        "Fix the arguments and try again, or tell the user why it can't be done.",
    ),
    REPEAT: (
        "no",
        "This exact call was already rejected since the user's last message. "
        "Follow the earlier rejection instead of repeating it.",
    ),
}

# Rejections that make an identical repeat pointless until the user speaks again.
# Tool errors are left out: the cause (e.g. a missing file) may be fixed by a later call.
_GUARDED_SOURCES = {SANDBOX, POLICY, CLASSIFIER, USER}

STOP_NOTE = (
    f"{MAX_REJECTIONS_PER_TURN} calls were rejected this turn. Do not call any "
    "more tools. Tell the user what you tried, what was rejected and why."
)
NOT_RUN = (
    "Not run: too many calls were rejected this turn. Stop calling tools and "
    "tell the user what happened."
)


@dataclass
class Rejection:
    source: str
    reason: str

    def render(self, stop: bool = False) -> str:
        retry, next_step = _GUIDANCE[self.source]
        reason = " ".join(self.reason.split())[:MAX_REASON_CHARS] or "No reason given."
        lines = [
            "REJECTED",
            f"by: {self.source}",
            f"reason: {reason}",
            f"retry: {retry}",
            f"next: {next_step}",
        ]
        if stop:
            lines.append(f"stop: {STOP_NOTE}")
        return "\n".join(lines)


def _key(tool_name: str, args: dict) -> str:
    return json.dumps([tool_name, args], sort_keys=True, default=str)


class Tracker:
    """Remembers rejected calls until the user's next message and counts
    rejections in the current turn."""

    def __init__(self) -> None:
        self._rejected: set[str] = set()
        self.turn_rejections = 0

    def start_turn(self) -> None:
        """Call on every new user message: they may have changed what they want."""
        self._rejected.clear()
        self.turn_rejections = 0

    @property
    def limit_reached(self) -> bool:
        return self.turn_rejections >= MAX_REJECTIONS_PER_TURN

    def already_rejected(self, tool_name: str, args: dict) -> bool:
        return _key(tool_name, args) in self._rejected

    def reject(self, tool_name: str, args: dict, rejection: Rejection) -> str:
        """Record a rejection and return the text to send back to the agent."""
        if rejection.source == TOOL_ERROR:
            # Ordinary failures (missing file, bad argument) are part of normal
            # work, so they don't count toward the per-turn limit.
            return rejection.render()
        if rejection.source in _GUARDED_SOURCES:
            self._rejected.add(_key(tool_name, args))
        self.turn_rejections += 1
        return rejection.render(stop=self.limit_reached)
