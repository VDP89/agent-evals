#!/usr/bin/env python3
"""
agent-evals — monthly aggregation.

Reads the daily session records produced by `session_eval.py` for one month and
emits two files: a machine-readable `.json` of aggregate signals, and a readable
`.md`. The point is NOT a dashboard. The point is to turn a month of cheap
captures into a short list of *mechanism* proposals: a rule to add, a habit to
fix at the source, a model choice to recalibrate.

Philosophy: mechanism, not catalog. A failure that repeats is not a "known
limitation" you write down. It is a missing mechanism. The signals below exist
to point at the cause, not to tally the effect.

This reference version computes the signals deterministically and prints them.
In a richer setup you would hand the `.json` to a capable model ONCE a month to
read the signals and draft the proposals — model proposes, a human decides. No
model is in the capture loop; the only model touch is this slow monthly read.

Usage:
    python monthly.py            # previous month
    python monthly.py 2026-05    # a specific month (YYYY-MM)
"""
import sys
import os
import json
from pathlib import Path
from datetime import datetime
from collections import Counter, defaultdict

EVALS_ROOT = Path(os.environ.get("AGENT_EVALS_DIR") or (Path.cwd() / ".agent-evals"))

# Tunable: a low-level rule firing more than this many times in a SINGLE session
# is read as evidence that some higher-level rule got skipped (look one layer down).
SCAR_IN_SESSION_THRESHOLD = 2
# Tunable: a session with at least this many correction turns is worth reviewing.
FRICTION_THRESHOLD = 3


def _prev_month():
    now = datetime.now()
    y, m = (now.year - 1, 12) if now.month == 1 else (now.year, now.month - 1)
    return "%04d-%02d" % (y, m)


def load_month(month):
    d = EVALS_ROOT / "daily" / month
    records = []
    if not d.exists():
        return records
    for f in sorted(d.glob("%s-*.json" % month)):
        try:
            records.append(json.loads(f.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    return records


def aggregate(records):
    agg = {
        "sessions": len(records),
        "totals": Counter(),
        "tokens": Counter(),
        "models": Counter(),
        "tools": Counter(),
        "skills": Counter(),
        "dirs_focus": Counter(),
        "scars_total": Counter(),
        "hooks_total": Counter(),
        "signals": {
            "scar_in_session_overfires": [],   # (date, sid, scar, count)
            "most_recurrent_scar": None,        # upstream-mechanism candidate
            "high_friction_sessions": [],       # (date, sid, correction_turns)
            "model_distribution": {},           # share of assistant turns by model
        },
    }
    for r in records:
        c = r.get("counts") or {}
        for k, v in c.items():
            agg["totals"][k] += int(v or 0)
        for k, v in (r.get("tokens") or {}).items():
            agg["tokens"][k] += int(v or 0)
        for k, v in (r.get("models") or {}).items():
            agg["models"][k] += int(v or 0)
        for k, v in (r.get("tools") or {}).items():
            agg["tools"][k] += int(v or 0)
        for s in (r.get("skills") or []):
            agg["skills"][s] += 1
        for d in (r.get("dirs_touched") or []):
            agg["dirs_focus"][d] += 1
        for k, v in (r.get("hooks_fired") or {}).items():
            agg["hooks_total"][k] += int(v or 0)

        sid = (r.get("session_id") or "unknown")[:8]
        date = r.get("date") or "?"
        for scar, n in (r.get("scars_fired") or {}).items():
            agg["scars_total"][scar] += int(n or 0)
            if int(n or 0) > SCAR_IN_SESSION_THRESHOLD:
                agg["signals"]["scar_in_session_overfires"].append((date, sid, scar, int(n)))

        ft = ((r.get("friction") or {}).get("correction_turns")) or 0
        if int(ft) >= FRICTION_THRESHOLD:
            agg["signals"]["high_friction_sessions"].append((date, sid, int(ft)))

    if agg["scars_total"]:
        scar, n = agg["scars_total"].most_common(1)[0]
        agg["signals"]["most_recurrent_scar"] = {"scar": scar, "fires": n}

    total_turns = sum(agg["models"].values()) or 1
    agg["signals"]["model_distribution"] = {
        m: round(100.0 * n / total_turns, 1) for m, n in agg["models"].most_common()
    }
    return agg


def render_md(month, agg):
    L = ["# agent-evals — %s" % month, "",
         "**Sessions:** %d" % agg["sessions"], ""]
    t = agg["totals"]
    L += ["## Totals", "",
          "- user prompts: %d" % t["user_prompts"],
          "- assistant turns: %d" % t["assistant_turns"],
          "- tool calls: %d" % t["tool_calls"],
          "- errors: %d" % t["errors"],
          "- subagents: %d" % t["subagents"],
          "- skills invoked: %d" % t["skills_invoked"], ""]

    md = agg["signals"]["model_distribution"]
    if md:
        L += ["## Model distribution (share of assistant turns)", ""]
        L += ["- %s: %s%%" % (m, p) for m, p in md.items()]
        L += ["", "_If one model dominates against your task->model policy, that is a recalibration signal._", ""]

    if agg["dirs_focus"]:
        L += ["## Where the work actually went", ""]
        L += ["- %s: %d sessions" % (d, n) for d, n in agg["dirs_focus"].most_common(10)]
        L += [""]

    sig = agg["signals"]
    L += ["## Signals (mechanism candidates)", ""]
    if sig["most_recurrent_scar"]:
        s = sig["most_recurrent_scar"]
        L += ["- **Most recurrent rule:** `%s` fired %d times -> candidate for an *upstream* mechanism "
              "(fix the cause, not the symptom)." % (s["scar"], s["fires"])]
    over = sig["scar_in_session_overfires"]
    if over:
        L += ["- **Over-fires in a single session** (>%d): a low-level rule firing this much in one "
              "session usually means a higher-level rule got skipped:" % SCAR_IN_SESSION_THRESHOLD]
        L += ["  - %s `%s` x%d (session %s)" % (d, scar, n, sid) for (d, sid, scar, n) in over[:15]]
    hf = sig["high_friction_sessions"]
    if hf:
        L += ["- **High-friction sessions** (>=%d corrections) — review instructions/quality:" % FRICTION_THRESHOLD]
        L += ["  - %s session %s: %d corrections" % (d, sid, n) for (d, sid, n) in hf[:15]]
    if not (sig["most_recurrent_scar"] or over or hf):
        L += ["- No hard signals this month."]
    L += ["", "---", "_mechanism, not catalog — turn each signal into a proposal, or drop it._", ""]
    return "\n".join(L)


def main():
    month = sys.argv[1] if len(sys.argv) > 1 else _prev_month()
    records = load_month(month)
    if not records:
        print("agent-evals: no records for %s under %s" % (month, EVALS_ROOT / "daily" / month))
        return
    agg = aggregate(records)
    out_dir = EVALS_ROOT / "monthly"
    out_dir.mkdir(parents=True, exist_ok=True)

    # Counter is not JSON-serializable as-is in nested form; coerce to plain dicts.
    serializable = {
        "month": month,
        "sessions": agg["sessions"],
        "totals": dict(agg["totals"]),
        "tokens": dict(agg["tokens"]),
        "models": dict(agg["models"]),
        "tools": dict(agg["tools"]),
        "skills": dict(agg["skills"]),
        "dirs_focus": dict(agg["dirs_focus"]),
        "scars_total": dict(agg["scars_total"]),
        "hooks_total": dict(agg["hooks_total"]),
        "signals": agg["signals"],
    }
    (out_dir / ("%s__aggregate.json" % month)).write_text(
        json.dumps(serializable, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / ("%s__aggregate.md" % month)).write_text(render_md(month, agg), encoding="utf-8")
    print("agent-evals: wrote %s aggregate (%d sessions) to %s"
          % (month, agg["sessions"], out_dir))


if __name__ == "__main__":
    main()
