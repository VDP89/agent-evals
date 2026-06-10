# session-boot — companion code for "Anatomy of a four-minute boot"

Reference implementations for cutting Claude Code session-start time without losing
informational context. Companion to the blog post:
<https://victordelpuerto.com/posts/anatomy-of-a-four-minute-boot/>

Same model as the rest of this repo: read it, adapt it, no package to install.

## The problem

A session that spends minutes before doing useful work, typically because:

1. The auto-loaded memory index (`MEMORY.md`) outgrew its load budget and the agent
   compensates by re-reading everything.
2. Hooks spawn one interpreter per script, per event.
3. A keyword-recall hook with false positives forces irrelevant file reads
   (every false positive burns a full model turn).

Measured in our setup: hooks were ~2.7 s of the problem; sequential model read-turns
were ~3.5 minutes of it. Fix the turns first.

## Files

| File | What it does |
|---|---|
| `session_start_dispatcher.py` | Runs all your SessionStart hook scripts in ONE Python process (children unchanged, via `runpy`), with daily caching for expensive disk scans and mtime-gating for sync jobs. Merges outputs into a single hook JSON. |
| `prompt_submit_dispatcher.py` | Same consolidation for UserPromptSubmit hooks. |
| `memory_index_lint.py` | Lints your memory index(es): byte budget, per-line length, broken links, orphan files, duplicate entries. Run it before the index outgrows its load budget silently. |

## Usage

Point your `settings.json` at the dispatchers instead of N individual scripts:

```json
{
  "hooks": {
    "SessionStart": [{ "hooks": [{ "type": "command",
      "command": "python /path/to/session_start_dispatcher.py || true", "timeout": 30 }] }],
    "UserPromptSubmit": [{ "hooks": [{ "type": "command",
      "command": "python /path/to/prompt_submit_dispatcher.py || true", "timeout": 20 }] }]
  }
}
```

Edit the lists at the top of each dispatcher to register your scripts. New hooks go
into the dispatcher lists, not into `settings.json` — otherwise you lose the consolidation.

Lint the index:

```bash
python memory_index_lint.py ~/.claude/projects/<project>/memory/MEMORY.md \
    --extra-index MEMORY_ARCHIVE.md --budget-bytes 32000 --max-line 150
```

## Caveats

- Dispatchers only merge `additionalContext` and `systemMessage`. A hook that emits
  a blocking `decision` must stay as a standalone hook.
- Claude Code hooks read stdin. Benchmarking one without piping stdin blocks forever
  waiting for EOF. Measure the way the harness invokes them: `echo '{}' | python hook.py`.
- Children run in-process: a child that calls `sys.exit()` is handled, but one that
  mutates global interpreter state could affect siblings. Keep hook scripts self-contained.
