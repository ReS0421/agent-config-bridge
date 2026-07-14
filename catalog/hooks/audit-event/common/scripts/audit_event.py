#!/usr/bin/env python3
"""Harmless example hook that writes only a known event name to stderr."""

from __future__ import annotations

import sys
from contextlib import suppress

_KNOWN_EVENTS = frozenset(
    {
        "Notification",
        "PermissionRequest",
        "PostToolUse",
        "PreCompact",
        "PreToolUse",
        "SessionEnd",
        "SessionStart",
        "Stop",
        "SubagentStop",
        "UserPromptSubmit",
    }
)


def main() -> int:
    """Write one event name and always allow the host operation to continue."""
    if len(sys.argv) != 2 or sys.argv[1] not in _KNOWN_EVENTS:
        return 0
    with suppress(OSError):
        print(sys.argv[1], file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
