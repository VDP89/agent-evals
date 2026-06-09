# agent-evals

**Session-level evals for Claude Code agents.** Interoception for your coding agent — it learns to feel its own state.

A scar log tells you *that one correction* fired. It tells you nothing about the regressions you never wrote a rule for, or whether your agent, taken as a whole, is getting better or just accumulating rules. You cannot answer that from inside one session. You have to instrument the whole thing.

`agent-evals` is the smallest honest way to do it: one deterministic record per session, written when the session closes, at **zero token cost**, that **never blocks** the session. A month of those records becomes a short list of *mechanism* proposals — a rule to add, a habit to fix at the source, a model choice to recalibrate.

This is a **reference implementation**, not a product. There is no `pip install`, no login, no remote telemetry. It is a sibling in spirit to [`lucy-syndrome`](https://github.com/VDP89/lucy-syndrome) (the paper + companion code), not to a maintained package. Copy the two files, wire them, change them. The essay that explains the *why* is [*How do you know a correction held?*](https://victordelpuerto.com).

Apache-2.0.

## The shape

```
  every session close (SessionEnd)
        |
        v
  session_eval.py     reads the transcript JSONL, deterministic parser
        |             (zero tokens, never blocks the close)
        v
  .agent-evals/daily/YYYY-MM/YYYY-MM-DD__<sid>.json     <- one record per session
        |
        v   (once a month)
  monthly.py          aggregates, applies a few hard heuristics
        |
        v
  .agent-evals/monthly/YYYY-MM__aggregate.{md,json}     <- signals -> proposals
```

And, because the capture hook's whole job is to make failures visible, it must not be able to fail invisibly:

```
  every session start (SessionStart)
        |
        v
  freshness_check.py  warns you if the last capture failed, or if nothing has
                      been captured in N days. The watcher's watcher.
```

## Install (5 minutes)

1. Drop `session_eval.py` and `freshness_check.py` somewhere (this repo, or vendored into your project).
2. Wire them into your Claude Code `settings.json` — see `settings.snippet.json`:

```json
"hooks": {
  "SessionEnd":   [{ "hooks": [{ "type": "command", "command": "python \"/abs/path/agent-evals/session_eval.py\"" }] }],
  "SessionStart": [{ "hooks": [{ "type": "command", "command": "python \"/abs/path/agent-evals/freshness_check.py\"" }] }]
}
```

> Note: do **not** append `2>/dev/null || true` to the SessionEnd command. Swallowing the error is exactly how an eval engine goes silently dead. The hook already protects itself (try/except, always exits 0) and leaves a marker the freshness check reads.

3. Close a session (`/clear`, exit). A record appears under `.agent-evals/daily/`.
4. At the end of the month: `python monthly.py` (or `python monthly.py 2026-05`).

## Configuration (all optional, env vars)

| Variable | What | Default |
|---|---|---|
| `AGENT_EVALS_DIR` | where records are written | `<cwd>/.agent-evals` |
| `AGENT_EVALS_PROJECT` | project root used to derive `dirs_touched` | cwd |
| `AGENT_EVALS_SENSITIVE` | comma-separated path substrings that mark a session sensitive (text samples suppressed) | none |
| `AGENT_EVALS_SCAR_RE` | regex (one capture group) to pull a rule id from a fired hook command | fscars-style `scar_NNN` |
| `AGENT_EVALS_STALE_DAYS` | freshness check warns if newest record is this old | 3 |

## What one record holds

Counts (prompts, turns, tool calls, errors, subagents, skills). Token split. Which models did the turns. Tools and skills used. Which directories the work actually touched. Deliverables produced. How many times each correction-rule fired (the friction signal). A friction count from user corrections. And a `sensitive` flag.

What it does **not** hold: the raw text of prompts and responses. The daily layer is metrics and short labels by design. If a session touches a path you listed in `AGENT_EVALS_SENSITIVE`, the record marks itself sensitive and drops even the short friction samples. Privacy is a property of the capture layer, not a policy you have to remember to apply later.

Full field list: `schema/session-eval.v1.json`.

## What the monthly pass looks for

- **Most recurrent rule** of the month → candidate for an *upstream* mechanism (fix the cause, not the symptom).
- **Over-fires in one session** — a low-level rule firing many times in a single session usually means a higher-level rule got skipped. Look one layer down.
- **High-friction sessions** (many corrections) → review the instructions or the quality bar.
- **Model distribution** — if one model dominates against your own task→model policy, that is a recalibration signal.

The reference `monthly.py` computes these and prints them. In a richer setup you hand the `.json` to a capable model **once** a month to read the signals and draft proposals — model proposes, a human decides. No model is in the capture loop.

## Generic vs. yours

The capture, the zero-token/never-block discipline, the monthly heuristics, and the freshness check are generic. Two things are stubs you are meant to replace with your own taxonomy:

- `dirs_touched` maps a touched file to the immediate subdir of your project root. Swap `_dir_of()` for a mapping to whatever "areas of work" mean in your setup.
- `scars_fired` assumes a correction-rule system (e.g. [fscars](https://github.com/VDP89/fscars)). If you don't run one, it stays empty and everything else still works.

## What this is not

It is not a product or a SaaS. It is not a model — capture is pure parsing, fully traceable, no inference. It is not a dashboard to look at for reassurance; a metric you don't turn into a decision changes nothing. The output that matters is the monthly proposal, and the only thing that closes the loop is acting on it.

## Related

- [*Lucy Syndrome in LLM Agents*](https://doi.org/10.5281/zenodo.19555971) — the paper: corrections don't survive across sessions, and the fix runs outside the model.
- [fscars](https://github.com/VDP89/fscars) — the correction primitive. agent-evals is the measurement layer above it; `scars_fired` is the bridge.
- The essay this repo accompanies: [*How do you know a correction held?*](https://victordelpuerto.com)
