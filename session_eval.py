#!/usr/bin/env python3
"""
agent-evals — SessionEnd capture hook for Claude Code agents.

Fires when a Claude Code session closes (clear / logout / exit). Reads the
session transcript JSONL and writes ONE deterministic evaluation record
(no model call — zero token cost per session) under your evals directory.

The qualitative judgement (what to improve, which correction keeps repeating,
whether to recalibrate model choice) is NOT done here. It is done once a month
by `monthly.py` over the corpus of records. Capture is cheap and dumb; analysis
is slow and smart. Keeping them apart is the whole point.

Design rules (do not relax these):
  - It MUST NEVER break session close. Everything runs inside try/except and the
    process always exits 0. An instrument that crashes the thing it measures is
    worse than no instrument.
  - It does NOT store raw prompt/response text — only counts and short labels.
    If the session touched a path you marked sensitive, the record is flagged
    `sensitive` and any text sample is suppressed.
  - Explicit UTF-8 on every read/write (Windows cp1252 trap).

Configuration (all optional, via environment variables):
  AGENT_EVALS_DIR        Where to write records. Default: <cwd>/.agent-evals
  AGENT_EVALS_PROJECT     Project root used to derive `dirs_touched`. Default: cwd
  AGENT_EVALS_SENSITIVE   Comma-separated path substrings that mark a session
                          sensitive (text samples suppressed). Default: none.
  AGENT_EVALS_SCAR_RE     Regex with one capture group to extract a "scar"/rule
                          id from a fired hook command. Default matches
                          fscars-style `scar_NNN` / `hook_scar_NNN`.

This file is a reference implementation derived from a system the author runs in
production. It is deliberately small. Copy it, wire it, change it.
"""
import sys
import os
import json
import re
from pathlib import Path
import traceback
from datetime import datetime, timezone

SCHEMA = "session-eval/v1"

EVALS_ROOT = Path(os.environ.get("AGENT_EVALS_DIR") or (Path.cwd() / ".agent-evals"))
PROJECT_ROOT = Path(os.environ.get("AGENT_EVALS_PROJECT") or Path.cwd())
LAST_ERROR = EVALS_ROOT / "_last_error.json"  # visible failure marker (see monthly README)

DELIVERABLE_EXT = {".docx", ".xlsx", ".pptx", ".pdf", ".dxf", ".csv"}

# Paths that mark a session sensitive: no text samples are kept for it.
SENSITIVE_MARKERS = tuple(
    s.strip() for s in (os.environ.get("AGENT_EVALS_SENSITIVE") or "").split(",") if s.strip()
)

# Extract a rule/scar id from a fired hook command (pluggable).
SCAR_RE = re.compile(os.environ.get("AGENT_EVALS_SCAR_RE") or r"(?:hook_)?(scar_\d{3})", re.IGNORECASE)

# User-turn signals that a correction happened (a cheap proxy for friction/quality).
FRICTION_PATTERNS = re.compile(
    r"\b(that's wrong|that is wrong|actually|you misunderstood|not what i|"
    r"no es asi|no era|esta mal|está mal|incorrecto|en realidad|corregi|corregí|"
    r"me equivoque|otra vez|de nuevo)\b",
    re.IGNORECASE,
)


def _iso_to_local_date(iso_str):
    """ISO-8601 UTC ('...Z') -> local date YYYY-MM-DD."""
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone().strftime("%Y-%m-%d")
    except Exception:
        return None


def _dir_of(path_str):
    """Immediate subdirectory of the project root that a touched file lives under.

    This is the generic stand-in for 'areas_touched'. In a domain-specific setup
    you would replace this with a mapping to your own taxonomy."""
    if not path_str:
        return None
    try:
        rel = Path(str(path_str)).resolve().relative_to(PROJECT_ROOT.resolve())
    except (ValueError, OSError):
        return None
    parts = rel.parts
    return parts[0] if len(parts) > 1 else None


def parse_transcript(transcript_path):
    """Walk the session JSONL and build a deterministic record."""
    rec = {
        "schema": SCHEMA,
        "session_id": None,
        "date": None,
        "started_at": None,
        "ended_at": None,
        "duration_min": None,
        "cc_version": None,
        "entrypoint": None,
        "git_branch": None,
        "cwd": None,
        "counts": {
            "user_prompts": 0,
            "assistant_turns": 0,
            "tool_calls": 0,
            "errors": 0,
            "subagents": 0,
            "skills_invoked": 0,
        },
        "models": {},
        "tokens": {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0},
        "tools": {},
        "skills": [],
        "subagent_types": [],
        "deliverables": [],
        "files_touched": [],
        "dirs_touched": [],
        "scars_fired": {},
        "hooks_fired": {},
        "friction": {"correction_turns": 0, "samples": []},
        "sensitive": False,
    }

    timestamps, files, dirs, skills, subtypes = [], set(), set(), set(), []

    try:
        raw = Path(transcript_path).read_text(encoding="utf-8", errors="replace")
    except (OSError, UnicodeError):
        return rec

    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            o = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue

        typ = o.get("type")
        ts = o.get("timestamp")
        if ts:
            timestamps.append(ts)
        if rec["session_id"] is None and o.get("sessionId"):
            rec["session_id"] = o.get("sessionId")
        for k_src, k_dst in (("version", "cc_version"), ("entrypoint", "entrypoint"),
                             ("gitBranch", "git_branch"), ("cwd", "cwd")):
            if o.get(k_src):
                rec[k_dst] = o.get(k_src)
        if rec["cwd"] and any(m in str(rec["cwd"]) for m in SENSITIVE_MARKERS):
            rec["sensitive"] = True

        msg = o.get("message") or {}
        content = msg.get("content")

        # --- attachments: hooks / scars that fired ---
        if typ == "attachment":
            att = o.get("attachment") or {}
            hook_event = att.get("hookEvent") or ""
            cmd = att.get("command") or ""
            blob = "%s %s" % (att.get("content") or "", att.get("stdout") or "")
            # Skip the SessionStart index injection — that is not a fire.
            if hook_event != "SessionStart" and blob.strip():
                m = SCAR_RE.search(cmd)
                if m:
                    sid = m.group(1)
                    rec["scars_fired"][sid] = rec["scars_fired"].get(sid, 0) + 1
                base = os.path.basename(cmd.split()[0]) if cmd.strip() else (att.get("hookName") or "")
                if base:
                    rec["hooks_fired"][base] = rec["hooks_fired"].get(base, 0) + 1
            continue

        # --- assistant turns ---
        if typ == "assistant":
            rec["counts"]["assistant_turns"] += 1
            model = msg.get("model")
            if model:
                rec["models"][model] = rec["models"].get(model, 0) + 1
            usage = msg.get("usage") or {}
            for k_src, k_dst in (("input_tokens", "input"), ("output_tokens", "output"),
                                 ("cache_read_input_tokens", "cache_read"),
                                 ("cache_creation_input_tokens", "cache_creation")):
                try:
                    rec["tokens"][k_dst] += int(usage.get(k_src) or 0)
                except (TypeError, ValueError):
                    pass
            if isinstance(content, list):
                for b in content:
                    if not isinstance(b, dict) or b.get("type") != "tool_use":
                        continue
                    name = b.get("name") or "?"
                    rec["counts"]["tool_calls"] += 1
                    rec["tools"][name] = rec["tools"].get(name, 0) + 1
                    inp = b.get("input") or {}
                    if name in ("Write", "Edit", "NotebookEdit"):
                        fp = inp.get("file_path") or inp.get("notebook_path")
                        if fp:
                            files.add(str(fp))
                            d = _dir_of(fp)
                            if d:
                                dirs.add(d)
                            if os.path.splitext(str(fp))[1].lower() in DELIVERABLE_EXT:
                                rec["deliverables"].append(str(fp))
                    elif name == "Read":
                        d = _dir_of(inp.get("file_path"))
                        if d:
                            dirs.add(d)
                    elif name == "Skill":
                        rec["counts"]["skills_invoked"] += 1
                        sk = inp.get("skill")
                        if sk:
                            skills.add(str(sk))
                    elif name in ("Agent", "Task"):
                        rec["counts"]["subagents"] += 1
                        st = inp.get("subagent_type")
                        if st:
                            subtypes.append(str(st))
            continue

        # --- user turns: real prompts + tool errors + friction ---
        if typ == "user":
            is_tool_result, text_parts = False, []
            if isinstance(content, list):
                for b in content:
                    if isinstance(b, dict):
                        if b.get("type") == "tool_result":
                            is_tool_result = True
                            if b.get("is_error"):
                                rec["counts"]["errors"] += 1
                        elif b.get("type") == "text":
                            text_parts.append(b.get("text") or "")
            elif isinstance(content, str):
                text_parts.append(content)

            tur = o.get("toolUseResult")
            if isinstance(tur, dict) and (tur.get("is_error") or tur.get("error")):
                rec["counts"]["errors"] += 1

            joined = " ".join(text_parts).strip()
            if joined and not is_tool_result and not joined.startswith("<system-reminder>"):
                rec["counts"]["user_prompts"] += 1
                if FRICTION_PATTERNS.search(joined):
                    rec["friction"]["correction_turns"] += 1
                    if not rec["sensitive"] and len(rec["friction"]["samples"]) < 2:
                        rec["friction"]["samples"].append(joined[:80])
            continue

    # --- consolidate ---
    if timestamps:
        timestamps.sort()
        rec["started_at"], rec["ended_at"] = timestamps[0], timestamps[-1]
        rec["date"] = _iso_to_local_date(timestamps[-1])
        try:
            a = datetime.fromisoformat(timestamps[0].replace("Z", "+00:00"))
            b = datetime.fromisoformat(timestamps[-1].replace("Z", "+00:00"))
            rec["duration_min"] = round((b - a).total_seconds() / 60.0, 1)
        except Exception:
            pass
    if rec["date"] is None:
        rec["date"] = datetime.now().astimezone().strftime("%Y-%m-%d")

    rec["files_touched"] = sorted(files)
    rec["dirs_touched"] = sorted(dirs)
    rec["skills"] = sorted(skills)
    rec["subagent_types"] = subtypes
    if any(m in f for f in files for m in SENSITIVE_MARKERS):
        rec["sensitive"] = True
    if rec["sensitive"]:
        rec["friction"]["samples"] = []
    return rec


def _write_error_marker(where, err):
    """Leave a VISIBLE trace of a hook failure (read by a SessionStart freshness
    check) instead of swallowing it. Never raises."""
    try:
        EVALS_ROOT.mkdir(parents=True, exist_ok=True)
        LAST_ERROR.write_text(json.dumps({
            "ts": datetime.now().astimezone().isoformat(),
            "where": where,
            "error": str(err),
            "traceback": traceback.format_exc() if err else "",
        }, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def _clear_error_marker():
    try:
        if LAST_ERROR.exists():
            LAST_ERROR.unlink()
    except Exception:
        pass


def main():
    # Never blocks session close (always exit 0), but a failure leaves a trace in
    # _last_error.json instead of vanishing the way `2>/dev/null || true` would.
    try:
        payload = {}
        try:
            data = sys.stdin.read()
            if data:
                payload = json.loads(data)
        except Exception:
            payload = {}

        transcript_path = payload.get("transcript_path")
        end_reason = payload.get("reason") or payload.get("hook_event_name") or "unknown"

        if not transcript_path or not os.path.exists(transcript_path):
            _write_error_marker("no_transcript", "SessionEnd with no valid transcript_path in payload")
            return

        rec = parse_transcript(transcript_path)
        rec["end_reason"] = end_reason
        if rec.get("session_id") is None:
            rec["session_id"] = payload.get("session_id")

        date = rec.get("date") or datetime.now().astimezone().strftime("%Y-%m-%d")
        month = date[:7]
        sid = (rec.get("session_id") or "unknown")[:8]
        out_dir = EVALS_ROOT / "daily" / month
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / ("%s__%s.json" % (date, sid))

        out_path.write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
        _clear_error_marker()
    except Exception as e:
        _write_error_marker("main", e)
    return  # ALWAYS exit 0


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass  # session close must NEVER fail because of the eval engine
    sys.exit(0)
