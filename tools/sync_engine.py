#!/usr/bin/env python
"""Copy the shared engine into each skill's own scripts/ directory.

Agent Skills are meant to be self-contained: a skill directory must work when it
is the only thing a harness loads. The two skills therefore carry their own copy
of the engine instead of reaching up to the repository root, and this script is
the single source of truth for those copies.

    engine/                  the one place to edit
    skills/<name>/scripts/   generated copies (committed, so the skill stands alone)

    python tools/sync_engine.py           # rewrite every copy
    python tools/sync_engine.py --check   # exit 1 when a copy is stale

Nothing under a skill is hand-edited except SKILL.md and references/; run this
after touching engine/.
"""
from __future__ import annotations

import argparse
import filecmp
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
ENGINE = ROOT / "engine"
SKILLS = ("pdf-to-obsidian", "translate-markdown")

# Files the skills need at run time, relative to engine/. Everything else in
# engine/ is development material only.
PAYLOAD = ("*.py", "glossary.txt", "data/zotero-schema.json")


def payload_files() -> list[Path]:
    files: list[Path] = []
    for pattern in PAYLOAD:
        files.extend(sorted(ENGINE.glob(pattern)))
    return [p for p in files if p.is_file()]


def target_dirs() -> list[Path]:
    return [ROOT / "skills" / name / "scripts" for name in SKILLS]


def relative_targets() -> set[Path]:
    return {p.relative_to(ENGINE) for p in payload_files()}


def sync(dry_run: bool) -> list[str]:
    """Write every payload file into every skill. Returns a report line per change."""
    wanted = relative_targets()
    report: list[str] = []
    for target in target_dirs():
        for rel in sorted(wanted):
            src, dst = ENGINE / rel, target / rel
            if dst.exists() and filecmp.cmp(src, dst, shallow=False):
                continue
            report.append(f"update {dst.relative_to(ROOT)}")
            if not dry_run:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
        # remove files a previous run copied but the source no longer has
        if target.exists():
            for stale in sorted(target.rglob("*")):
                if not stale.is_file():
                    continue
                rel = stale.relative_to(target)
                if "__pycache__" in rel.parts or rel.suffix == ".pyc":
                    continue
                if rel not in wanted:
                    report.append(f"remove {stale.relative_to(ROOT)}")
                    if not dry_run:
                        stale.unlink()
    return report


def check() -> list[str]:
    """Problems that make a committed copy differ from engine/."""
    wanted = relative_targets()
    problems: list[str] = []
    for target in target_dirs():
        rel_target = target.relative_to(ROOT)
        if not target.exists():
            problems.append(f"{rel_target}: missing entirely (run tools/sync_engine.py)")
            continue
        for rel in sorted(wanted):
            src, dst = ENGINE / rel, target / rel
            if not dst.exists():
                problems.append(f"{rel_target / rel}: missing (run tools/sync_engine.py)")
            elif not filecmp.cmp(src, dst, shallow=False):
                problems.append(f"{rel_target / rel}: differs from engine/{rel} "
                                f"(run tools/sync_engine.py)")
        for stale in sorted(target.rglob("*")):
            if not stale.is_file():
                continue
            rel = stale.relative_to(target)
            if "__pycache__" in rel.parts or rel.suffix == ".pyc":
                continue
            if rel not in wanted:
                problems.append(f"{rel_target / rel}: not produced by engine/ "
                                f"(stale copy)")
    return problems


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true",
                    help="do not write; exit 1 when any skill copy is stale")
    args = ap.parse_args(argv)

    if not ENGINE.is_dir():
        sys.exit(f"no engine/ directory at {ENGINE}")

    if args.check:
        problems = check()
        for p in problems:
            print("  " + p)
        print("sync:", "ok" if not problems else f"{len(problems)} problem(s)")
        return 1 if problems else 0

    report = sync(dry_run=False)
    print(f"synced {len(payload_files())} file(s) into {len(target_dirs())} skill(s)")
    for line in report:
        print("  " + line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
