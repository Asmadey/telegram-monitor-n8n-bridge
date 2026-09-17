#!/usr/bin/env python3
"""Trim noisy command output before it reaches the model's context.

Reads a captured log file, prints at most KEEP_TAIL trailing lines plus any
error-looking lines from the part that was cut. The full log stays on disk and
its path is printed, so nothing is actually lost — only the tokens are.
"""
import os
import re
import sys

KEEP_TAIL = int(os.environ.get("CLAUDE_TRIM_TAIL", "30"))
MAX_ERRORS = int(os.environ.get("CLAUDE_TRIM_MAX_ERRORS", "40"))

# Tight on purpose: a pattern that matches half of a build log saves nothing.
ERROR_RE = re.compile(
    r"(?:^|[^a-z])(?:error|errors|failed|failure|fatal|traceback|exception|panic"
    r"|assertionerror|segfault|not found|permission denied|connection refused"
    r"|E\d{3}\b|FAILED|ERROR)(?:[^a-z]|$)",
    re.IGNORECASE,
)


def main() -> int:
    if len(sys.argv) < 2:
        return 0
    path = sys.argv[1]
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return 0

    if len(lines) <= KEEP_TAIL:
        print("\n".join(lines))
        return 0

    head, tail = lines[:-KEEP_TAIL], lines[-KEEP_TAIL:]
    errors = [ln for ln in head if ERROR_RE.search(ln)]
    dropped = len(head) - len(errors)

    if errors:
        shown = errors[:MAX_ERRORS]
        print(f"--- {len(shown)} error line(s) from the {len(head)} trimmed lines ---")
        print("\n".join(shown))
        if len(errors) > MAX_ERRORS:
            print(f"... and {len(errors) - MAX_ERRORS} more error lines (see full log)")
        print()

    print(f"--- trimmed {dropped} quiet line(s); last {KEEP_TAIL} below ---")
    print("\n".join(tail))
    print(f"--- full log: {path} ---")
    return 0


if __name__ == "__main__":
    sys.exit(main())
