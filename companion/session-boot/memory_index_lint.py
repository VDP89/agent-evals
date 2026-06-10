#!/usr/bin/env python3
"""Lint a Claude Code memory index before it silently outgrows its load budget.

An auto-loaded MEMORY.md that exceeds the harness budget loads truncated, and a
diligent agent compensates by re-reading everything — the index becomes a tax.
This linter enforces the contract that keeps it an index:

  1. Total size within a byte budget (default 32 000).
  2. Entry lines within a max length (default 150 chars).
  3. Every markdown link resolves to a file on disk.
  4. No orphan .md files (memories on disk referenced by no index).
  5. No duplicate references across indexes (an entry lives in exactly one).

Usage:
  python memory_index_lint.py <MEMORY.md> [--extra-index FILE]... \
      [--budget-bytes 32000] [--max-line 150]

Exit 1 on budget overflow or broken links; orphans and long lines are warnings.
Run it in CI, a SessionStart hook, or by hand after editing memories.
"""
import argparse
import re
import sys
from pathlib import Path

MD_LINK = re.compile(r"\[([^\]]+)\]\(([^)#][^)]*\.md)\)")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("index", type=Path, help="auto-loaded index (MEMORY.md)")
    ap.add_argument("--extra-index", type=Path, action="append", default=[],
                    help="archive/sub-index files (not auto-loaded, still linted)")
    ap.add_argument("--budget-bytes", type=int, default=32_000)
    ap.add_argument("--max-line", type=int, default=150)
    args = ap.parse_args()

    indexes = [args.index] + args.extra_index
    memory_dir = args.index.parent
    errors: list[str] = []
    warnings: list[str] = []
    referenced: dict[str, str] = {}  # filename -> first index that references it

    # 1. byte budget (only the auto-loaded index pays the budget)
    size = args.index.stat().st_size
    if size > args.budget_bytes:
        errors.append(f"{args.index.name} is {size:,} bytes (> budget {args.budget_bytes:,}) "
                      f"— it will load truncated")

    for idx in indexes:
        if not idx.exists():
            errors.append(f"index not found: {idx}")
            continue
        for n, line in enumerate(idx.read_text(encoding="utf-8").splitlines(), 1):
            # 2. line length (entry lines only)
            if line.lstrip().startswith("- ") and len(line) > args.max_line:
                warnings.append(f"{idx.name}:{n} entry is {len(line)} chars (> {args.max_line})")
            # 3. links resolve / 5. duplicates
            for _label, target in MD_LINK.findall(line):
                if not (memory_dir / target).exists():
                    errors.append(f"{idx.name}:{n} broken link: {target}")
                prev = referenced.get(target)
                if prev and prev != idx.name and target not in {i.name for i in indexes}:
                    warnings.append(f"{target} referenced in both {prev} and {idx.name}")
                referenced.setdefault(target, idx.name)

    # 4. orphans
    index_names = {i.name for i in indexes}
    for f in sorted(memory_dir.glob("*.md")):
        if f.name not in index_names and f.name not in referenced:
            warnings.append(f"orphan memory (no index references it): {f.name}")

    for w in warnings:
        print(f"WARN  {w}")
    for e in errors:
        print(f"ERROR {e}")
    ok_bytes = f"{size:,} / {args.budget_bytes:,} bytes"
    print(f"{'FAIL' if errors else 'OK'}  {args.index.name}: {ok_bytes}, "
          f"{len(referenced)} referenced files, {len(warnings)} warning(s), {len(errors)} error(s)")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
