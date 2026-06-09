#!/usr/bin/env python3
"""
agent-evals — SessionStart freshness check (the watcher's watcher).

The capture hook's whole job is to make failures visible. So it must not be able
to fail invisibly. This runs at session START, is deterministic and zero-token,
and warns you at the top of the next session if the eval engine stopped working:

  - there is a `_last_error.json` marker (the last SessionEnd failed), or
  - the most recent record is older than STALE_DAYS (nothing has been captured).

Wire it into settings.json under SessionStart. It prints one line and exits 0;
it never blocks startup.
"""
import os
import json
from pathlib import Path
from datetime import datetime, timezone

EVALS_ROOT = Path(os.environ.get("AGENT_EVALS_DIR") or (Path.cwd() / ".agent-evals"))
STALE_DAYS = int(os.environ.get("AGENT_EVALS_STALE_DAYS") or 3)


def _latest_record_date():
    daily = EVALS_ROOT / "daily"
    if not daily.exists():
        return None
    latest = None
    for f in daily.rglob("*.json"):
        name = f.name[:10]  # YYYY-MM-DD prefix
        try:
            d = datetime.strptime(name, "%Y-%m-%d").date()
        except ValueError:
            continue
        if latest is None or d > latest:
            latest = d
    return latest


def main():
    try:
        marker = EVALS_ROOT / "_last_error.json"
        if marker.exists():
            try:
                info = json.loads(marker.read_text(encoding="utf-8"))
                where = info.get("where", "?")
            except Exception:
                where = "?"
            print("[agent-evals] WARN - last SessionEnd capture failed (where=%s). See %s" % (where, marker))
            return

        latest = _latest_record_date()
        if latest is None:
            print("[agent-evals] WARN - no records yet under %s" % (EVALS_ROOT / "daily"))
            return
        age = (datetime.now(timezone.utc).date() - latest).days
        if age >= STALE_DAYS:
            print("[agent-evals] WARN - newest record is %d days old (%s). Capture may be broken." % (age, latest))
        else:
            print("[agent-evals] OK - newest record %s (%d days old)" % (latest, age))
    except Exception as e:
        # Even the watcher must never break startup.
        print("[agent-evals] (freshness check skipped: %s)" % e)


if __name__ == "__main__":
    main()
