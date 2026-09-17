#!/usr/bin/env python3
"""PreToolUse/Bash: route noisy commands through the output trimmer.

PostToolUse cannot do this job — its only context field is `additionalContext`,
which appends. Trimming has to happen before the output exists, so this rewrites
the command via `updatedInput` to capture output to a file and print a trimmed
view instead. The full log stays on disk; only the token cost is cut.

Emits nothing for commands that are not noisy, so the tool input is untouched.
"""
import json
import os
import re
import sys

HELPERS = os.path.dirname(os.path.abspath(__file__))
TRIMMER = os.path.join(HELPERS, "trim-output.py")
LOGDIR = os.environ.get("CLAUDE_TRIM_LOGDIR", "/tmp/claude-trim")
MARKER = "__cl_trim"

# Commands whose output is bulk progress noise. Anchored to the start of the
# command or to a pipeline/;/&& boundary so "echo npm" does not match.
NOISY = re.compile(
    r"(?:^|[|;&]\s*|\s&&\s|\s\|\|\s)\s*(?:[\w./-]*(?:python3?|\.venv/bin/python)\s+-m\s+)?"
    r"(?:"
    r"npm|pnpm|yarn|bun|npx"
    r"|pip3?|poetry|uv|pipenv"
    r"|pytest|tox|nox|unittest"
    r"|cargo|go\s+(?:build|test|mod)"
    r"|make|cmake|ninja|gradle|gradlew|mvn"
    r"|docker|docker-compose"
    r"|tsc|webpack|vite|rollup|esbuild|jest|vitest|eslint"
    r"|mypy|alembic|terraform|ansible"
    r")\b",
    re.IGNORECASE,
)

# Shapes the wrapper would corrupt.
UNSAFE = re.compile(r"<<|>\s*[\w./-]|&\s*$|\bexec\b|\bexit\b")


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0

    if payload.get("tool_name") != "Bash":
        return 0
    tool_input = payload.get("tool_input") or {}
    cmd = tool_input.get("command")
    if not isinstance(cmd, str) or not cmd.strip():
        return 0
    # Background tasks stream their own output; leave them alone.
    if tool_input.get("run_in_background"):
        return 0
    if MARKER in cmd or not NOISY.search(cmd) or UNSAFE.search(cmd):
        return 0

    wrapped = (
        f"mkdir -p {LOGDIR}; {MARKER}=$(mktemp {LOGDIR}/run-XXXXXX.log); "
        f"{{ {cmd}\n}} >\"${MARKER}\" 2>&1; {MARKER}_rc=$?; "
        f"python3 {TRIMMER} \"${MARKER}\"; (exit ${MARKER}_rc)"
    )
    updated = dict(tool_input)
    updated["command"] = wrapped

    json.dump(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "updatedInput": updated,
            }
        },
        sys.stdout,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
