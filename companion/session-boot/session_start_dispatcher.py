#!/usr/bin/env python3
"""Single-process dispatcher for Claude Code SessionStart hooks.

Why: each hook entry in settings.json spawns its own interpreter. Six scripts cost
~2.7 s per session on Windows; one dispatcher running the same six in-process costs
0.2-0.7 s. Children are executed unchanged via runpy with captured stdio, so this is
a drop-in consolidation, not a rewrite.

Register your scripts in ALWAYS / DAILY / WATCHED below. Outputs are merged into one
SessionStart hook JSON (additionalContext + systemMessage). A child that fails is
skipped; the dispatcher never breaks session start.

Reference implementation — adapt paths and lists to your setup.
"""
import contextlib
import io
import json
import runpy
import sys
import time
from datetime import date
from pathlib import Path

# ---------------------------------------------------------------- configure me
HOOKS_DIR = Path(__file__).parent  # wherever your hook scripts live

# Run every session (cheap scripts: context injection, env warnings).
ALWAYS: list[tuple[Path, list[str]]] = [
    # (HOOKS_DIR / "inject_context.py", []),
]

# Run once per day (expensive full-disk validators). Optional second element:
# a file whose mtime change invalidates the cache early (e.g. the file being validated).
DAILY: list[tuple[Path, list[str], Path | None]] = [
    # (HOOKS_DIR / "structure_validator.py", [], None),
    # (HOOKS_DIR / "memory_sync_check.py", [], Path.home() / ".claude" / "memory" / "MEMORY.md"),
]

# Run only when files matching the glob changed since the last run (sync jobs).
WATCHED: list[tuple[Path, list[str], Path, str]] = [
    # (HOOKS_DIR / "sync_something.py", ["--apply"], Path("/path/to/watched"), "*.md"),
]

CACHE_FILE = Path.home() / ".claude" / "tmp" / "session_boot_cache.json"
# ------------------------------------------------------------------------------

STDIN_RAW = ""


def run_child(path: Path, argv: list[str]) -> str:
    """Execute a child script in-process, capturing stdout+stderr. Never raises."""
    out, err = io.StringIO(), io.StringIO()
    old_stdin, old_argv = sys.stdin, sys.argv
    sys.stdin = io.StringIO(STDIN_RAW)
    sys.argv = [str(path)] + argv
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            runpy.run_path(str(path), run_name="__main__")
    except SystemExit:
        pass
    except Exception as e:  # noqa: BLE001 — a broken hook must not break the boot
        err.write(f"[dispatcher] {path.name} failed: {e}")
    finally:
        sys.stdin, sys.argv = old_stdin, old_argv
    text = out.getvalue()
    err_text = err.getvalue().strip()
    if err_text:
        text += ("\n" if text else "") + err_text
    return text.strip()


def extract_context(raw: str) -> str:
    """Hook JSON -> its additionalContext; plain text -> itself."""
    if not raw:
        return ""
    try:
        data = json.loads(raw)
        return (data.get("hookSpecificOutput") or {}).get("additionalContext", "") or ""
    except (json.JSONDecodeError, AttributeError):
        return raw


def load_cache() -> dict:
    try:
        return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_cache(cache: dict) -> None:
    try:
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:
        pass


def main() -> int:
    global STDIN_RAW
    try:
        STDIN_RAW = sys.stdin.read()
    except Exception:
        STDIN_RAW = ""
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    today = date.today().isoformat()
    now_hhmm = time.strftime("%H:%M")
    cache = load_cache()
    fresh_day = cache.get("date") == today
    contexts: list[str] = []
    summary: list[str] = []

    for path, argv in ALWAYS:
        ctx = extract_context(run_child(path, argv))
        if ctx:
            contexts.append(ctx)

    for path, argv, invalidator in DAILY:
        key = f"daily:{path.name}"
        inv_mtime = invalidator.stat().st_mtime if invalidator and invalidator.exists() else 0.0
        hit = fresh_day and cache.get(key) and cache.get(key + ":mtime") == inv_mtime
        if hit:
            out_text, mark = cache[key], f" (cached {cache.get('stamp_hhmm', '')})"
        else:
            out_text = run_child(path, argv) or f"[{path.stem}] no output"
            cache[key], cache[key + ":mtime"], mark = out_text, inv_mtime, ""
        contexts.append(out_text)
        summary.append(out_text.splitlines()[0][:80] + mark)

    for path, argv, watch_dir, pattern in WATCHED:
        key = f"watched:{path.name}"
        try:
            newest = max((p.stat().st_mtime for p in watch_dir.glob(pattern)), default=0.0)
        except OSError:
            newest = 0.0
        if newest > cache.get(key, 0.0):
            run_child(path, argv)
            cache[key] = time.time()
            summary.append(f"{path.stem}: ran (watched files changed)")

    cache["date"] = today
    if not fresh_day:
        cache["stamp_hhmm"] = now_hhmm
    save_cache(cache)

    json.dump(
        {
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": "\n\n".join(c for c in contexts if c),
            },
            "systemMessage": " | ".join(s for s in summary if s),
        },
        sys.stdout,
        ensure_ascii=False,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
