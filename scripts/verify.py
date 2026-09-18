#!/usr/bin/env python
"""Structural checks on a converted note. Exit 1 when something is broken.

    python scripts/verify.py out/md/blocks.jsonl [--pdf paper.pdf] [--no-translate]

Checks, and why each one exists:

  HARD  dead cross-reference links   a [[#^ref-1]] with no ^ref-1 anywhere is a
                                     silently broken click target
  HARD  assets referenced but absent the note points at an image that was not copied
  HARD  leftover ⟦n⟧ placeholders    a masked formula/link was lost in translation
  HARD  unbalanced $ or ``` fences   the note will render wrong
  WARN  untranslated blocks          translation requested but some blocks are empty
  WARN  coverage vs the PDF text     catches a parser that dropped whole regions
  INFO  counts                       equations / tables / images / citations / links

The coverage check needs PyMuPDF and the source PDF; everything else is pure
text analysis of blocks.jsonl and the note next to it.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


def find_note(blocks_path: Path, explicit: Path | None) -> Path | None:
    """The note lives next to blocks.jsonl and is named after the source PDF."""
    if explicit:
        return explicit if explicit.exists() else None
    stem = None
    meta_path = blocks_path.parent / "meta.json"
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            stem = meta.get("stem") or Path(meta.get("source_pdf") or "").stem or None
        except Exception:
            pass
    cands = sorted(blocks_path.parent.glob("*.md"))
    if stem:
        for c in cands:
            if c.stem == stem or c.name.startswith(stem):
                return c
    return cands[0] if len(cands) == 1 else None


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("blocks", type=Path)
    ap.add_argument("--note", type=Path, default=None, help="the .md to check")
    ap.add_argument("--pdf", type=Path, default=None)
    ap.add_argument("--no-translate", action="store_true")
    args = ap.parse_args(argv)

    blocks_path = args.blocks
    md_path = find_note(blocks_path, args.note)
    if md_path is None:
        sys.exit(f"cannot tell which .md to check next to {blocks_path}; pass --note")
    md = md_path.read_text(encoding="utf-8")
    blocks = [json.loads(l) for l in blocks_path.read_text(encoding="utf-8").splitlines() if l.strip()]

    hard: list[str] = []
    warn: list[str] = []
    info: list[str] = []

    # ---- links -----------------------------------------------------------
    anchors = set(re.findall(r"(?m)\^([A-Za-z][\w-]*)\s*$", md))
    wants = set(re.findall(r"\[\[#\^([^\]|]+)", md))
    dead = sorted(wants - anchors)
    if dead:
        hard.append(f"{len(dead)} dead block links: {dead[:6]}")
    info.append(f"block links: {len(wants)} targets, {len(anchors)} anchors, "
                f"{len(anchors - wants)} unused")
    heads = set(re.findall(r"(?m)^#+\s+(.+?)\s*$", md))
    bad_heads = [h for h in set(re.findall(r"\[\[#(?!\^)([^\]|]+)", md)) if h not in heads]
    if bad_heads:
        hard.append(f"{len(bad_heads)} heading links with no matching heading: {bad_heads[:4]}")
    ext = re.findall(r"\]\(([^)\s]+)\)", md)
    if ext:
        info.append(f"external links: {len(ext)}")

    # ---- assets ----------------------------------------------------------
    assets = re.findall(r"!\[\[([^\]|]+)", md) + re.findall(r"!\[[^\]]*\]\(([^)]+)\)", md)
    missing = [a for a in assets if not (md_path.parent / a).exists()]
    if missing:
        hard.append(f"{len(missing)} referenced assets missing on disk: {missing[:4]}")
    info.append(f"images referenced: {len(assets)}")

    # ---- masking ---------------------------------------------------------
    leftover = len(re.findall(r"[\u27e6\u27e7]", md))
    if leftover:
        hard.append(f"{leftover} leftover placeholder characters (\u27e6 \u27e7) in the note")

    # ---- markdown sanity -------------------------------------------------
    fence = sum(1 for l in md.split("\n") if l.startswith("```"))
    if fence % 2:
        hard.append(f"unbalanced code fences ({fence} fence lines)")
    body = re.sub(r"\$\$.*?\$\$", "", md, flags=re.S)
    body = "\n".join(l for l in body.split("\n")
                     if not l.startswith("```") and not l.startswith("|")
                     and not l.startswith("    "))
    odd = [i + 1 for i, l in enumerate(body.split("\n")) if l.count("$") % 2]
    if odd:
        warn.append(f"{len(odd)} lines with an odd number of $ (possible broken inline math): "
                    f"lines {odd[:5]}")
    info.append(f"display equations: {md.count(chr(10) + '$$' + chr(10))}")

    # ---- translation -----------------------------------------------------
    tr = [b for b in blocks if b.get("translatable") and (b.get("text") or "").strip()]
    done = [b for b in tr if b.get("zh")]
    failed = [b["id"] for b in blocks if "translate_failed" in (b.get("flags") or [])]
    dup = [b["id"] for b in blocks if "translate_dup_placeholder" in (b.get("flags") or [])]
    if not args.no_translate:
        if len(done) < len(tr):
            warn.append(f"translated {len(done)}/{len(tr)} blocks; empty: "
                        f"{[b['id'] for b in tr if not b.get('zh')][:6]}")
        same = [b["id"] for b in done if b["zh"].strip() == b["text"].strip()]
        if same:
            info.append(f"blocks whose translation equals the source (usually pure math): "
                        f"{len(same)}")
    if failed:
        hard.append(f"{len(failed)} blocks failed translation: {failed[:6]}")
    # independent check: the translation must not contain FEWER formulas than its
    # source. A translation may restate one (the model rephrasing), but losing one
    # means a placeholder was dropped -- this is the failure that hides best.
    math_re = re.compile(r"\$[^$\n]+\$")
    lost = [(b["id"], len(math_re.findall(b["text"])), len(math_re.findall(b["zh"])))
            for b in done
            if len(math_re.findall(b["zh"])) < len(math_re.findall(b["text"]))]
    if lost:
        hard.append(f"{len(lost)} translations contain fewer formulas than their source: "
                    f"{lost[:4]}")
    else:
        info.append("formula preservation: every translation keeps all of its inline math")
    if dup:
        info.append(f"{len(dup)} blocks where the model repeated a placeholder while "
                    f"rephrasing (accepted, worth a look): {dup[:6]}")

    counts: dict[str, int] = {}
    for b in blocks:
        counts[b["type"]] = counts.get(b["type"], 0) + 1
    info.append("blocks: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())))

    # ---- coverage --------------------------------------------------------
    pdf = args.pdf
    toc_pages: set[int] = set()
    meta_path = blocks_path.parent / "meta.json"
    if meta_path.exists():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        toc_pages = set(meta.get("toc_pages") or [])
        if pdf is None and meta.get("source_pdf"):
            pdf = Path(meta["source_pdf"])
    if pdf and Path(pdf).exists():
        try:
            import pymupdf
            doc = pymupdf.open(str(pdf))
            # pages that actually carry text; a full-page figure legitimately has none
            text_pages = [i for i in range(doc.page_count)
                          if len(re.sub(r"\s+", "", doc[i].get_text())) > 400]
            doc.close()
        except ImportError:
            text_pages = None
            info.append("page coverage: skipped (PyMuPDF not installed)")
        if text_pages is not None:
            # any block counts, not just text ones: a page may legitimately hold
            # only a figure
            covered = {b["page"] for b in blocks}
            skipped = [p + 1 for p in text_pages if p in toc_pages]
            missing = [p + 1 for p in text_pages if p not in covered and p not in toc_pages]
            msg = (f"page coverage: {len(text_pages) - len(missing) - len(skipped)}/"
                   f"{len(text_pages)} text-bearing pages produced blocks"
                   + (f" ({len(skipped)} table-of-contents pages skipped by design)"
                      if skipped else ""))
            if missing:
                extra = f" (+{len(missing) - 8} more)" if len(missing) > 8 else ""
                (warn if len(missing) > 2 else info).append(
                    f"{msg}; pages with no extracted block: {missing[:8]}{extra}")
            else:
                info.append(msg)

    # ---- report ----------------------------------------------------------
    for line in info:
        print(f"  info  {line}")
    for line in warn:
        print(f"  WARN  {line}")
    for line in hard:
        print(f"  FAIL  {line}")
    print(f"{'FAILED' if hard else 'ok'}: {len(hard)} hard problems, {len(warn)} warnings")
    return 1 if hard else 0


if __name__ == "__main__":
    sys.exit(main())
