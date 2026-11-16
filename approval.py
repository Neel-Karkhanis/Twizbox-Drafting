"""Human approval gate. Every tool call is shown to the user and needs an explicit
'y' before it runs. There is deliberately no "always allow" option."""

from datetime import datetime

MAX_VALUE_CHARS = 300


def clean(text: str) -> str:
    """Strip control characters so model-written text can't spoof the terminal."""
    return "".join(ch if ch in "\n\t" or ch.isprintable() else "?" for ch in text)


def _short(value) -> str:
    if isinstance(value, str) and len(value) > MAX_VALUE_CHARS:
        return f"{value[:MAX_VALUE_CHARS]!r}... ({len(value):,} chars total)"
    return repr(value)


def log(message: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {message}")


def ask(tool_name: str, args: dict, preview: str | None = None) -> tuple[bool, str]:
    """Show a pending tool call and block until the user answers.

    Returns (approved, reason). Only an explicit 'y'/'yes' approves.
    """
    print()
    print("=" * 64)
    print(f"Agent wants to run: {clean(tool_name)}")
    if not args:
        print("   (no arguments)")
    for key, value in args.items():
        print(f"   {clean(str(key))}: {clean(_short(value))}")
    if preview:
        print("-" * 64)
        print(clean(preview))
    print("=" * 64)

    while True:
        try:
            answer = input("Approve? [y]es / [n]o > ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            log(f"DENIED  {tool_name} (input closed)")
            return False, "No answer from the user."
        if answer in ("y", "yes"):
            log(f"APPROVED  {tool_name}")
            return True, ""
        if answer in ("n", "no"):
            try:
                reason = input("Reason (optional, Enter to skip) > ").strip()
            except (EOFError, KeyboardInterrupt):
                reason = ""
            log(f"DENIED  {tool_name}" + (f" ({reason})" if reason else ""))
            return False, reason
        print("Please type y or n.")
