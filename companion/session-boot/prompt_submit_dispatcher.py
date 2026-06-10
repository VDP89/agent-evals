#!/usr/bin/env python3
"""Single-process dispatcher for Claude Code UserPromptSubmit hooks.

These hooks run on EVERY prompt, so per-spawn overhead compounds fast: six scripts
cost ~1 s per prompt; one dispatcher costs ~0.3 s. Children run unchanged via runpy
with the same stdin payload; their JSON outputs are merged (additionalContext joined,
systemMessage joined with " | ").

Do NOT register here a hook that emits a blocking `decision` — the merge only carries
context and messages. Keep blockers as standalone hooks.

Reference implementation — adapt the CHILDREN list to your setup.
"""
import contextlib
import io
import json
import runpy
import sys
from pathlib import Path

# ---------------------------------------------------------------- configure me
HOOKS_DIR = Path(__file__).parent  # wherever your hook scripts live

CHILDREN: list[Path] = [
    # HOOKS_DIR / "memory_recall.py",
    # HOOKS_DIR / "skill_suggest.py",
]
# ------------------------------------------------------------------------------


def run_child(path: Path, stdin_text: str) -> str:
    out = io.StringIO()
    old_stdin, old_argv = sys.stdin, sys.argv
    sys.stdin = io.StringIO(stdin_text)
    sys.argv = [str(path)]
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            runpy.run_path(str(path), run_name="__main__")
    except SystemExit:
        pass
    except Exception:  # noqa: BLE001 — a broken hook must not break the prompt
        pass
    finally:
        sys.stdin, sys.argv = old_stdin, old_argv
    return out.getvalue().strip()


def main() -> int:
    try:
        stdin_raw = sys.stdin.read()
    except Exception:
        stdin_raw = ""
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    contexts: list[str] = []
    sysmsgs: list[str] = []
    for path in CHILDREN:
        raw = run_child(path, stdin_raw)
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            contexts.append(raw)
            continue
        ctx = (data.get("hookSpecificOutput") or {}).get("additionalContext", "")
        if ctx:
            contexts.append(ctx)
        msg = data.get("systemMessage", "")
        if msg:
            sysmsgs.append(msg)

    if not contexts and not sysmsgs:
        return 0

    output: dict = {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": "\n\n".join(contexts),
        }
    }
    if sysmsgs:
        output["systemMessage"] = " | ".join(sysmsgs)
    json.dump(output, sys.stdout, ensure_ascii=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
