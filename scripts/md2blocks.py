#!/usr/bin/env python
"""Stage 1 for Markdown input: an English .md note -> blocks.jsonl.

    python md2blocks.py note.md -o out/md

No parsing is involved: the file is already Markdown, so this only segments it
into blocks and classifies them, producing the same intermediate layer the PDF
pipeline produces. Everything downstream (translate.py, pdf2obsidian.py --render-only,
verify.py) then works unchanged.

Classification rules, in order:

    ``` fence          -> code      (never translated)
    $$ ... $$          -> equation  (never translated)
    #+ heading         -> title     (level = number of #; never translated)
    | ... |            -> table     (never translated)
    ![[x]] / ![](x)    -> image     (never translated)
    [^n]: text         -> footnote  (translated, collected at the end)
    [N] ... after a "References" heading -> ref (never translated)
    anything else      -> text      (translated)

Blocks get page: null, so the render stage emits no page markers and no PDF page
links. meta.json records source_kind: markdown, which switches cross-reference
linkification off by default: a hand-written note is not a paper, and rewriting
`[12]` inside it is more likely to break the author's text than to help.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# headings stay in the source language, matching the user's decision for the PDF
# pipeline: the outline pane and cross-references read better that way
NOT_TRANSLATABLE = {"code", "equation", "table", "image", "ref", "title"}
FOOTNOTE_RE = re.compile(r"^\[\^([^\]]+)\]:\s*(.*)$")
REF_LINE_RE = re.compile(r"^\[(\d+)\]\s")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
IMAGE_RE = re.compile(r"^!\[\[([^\]]+)\]\]\s*$|^!\[[^\]]*\]\(([^)]+)\)\s*$")
REFS_HEADING_RE = re.compile(r"^(references|bibliography|参考文献)\s*$", re.I)


def split_blocks(text: str):
    """Yield raw markdown chunks, one per block, keeping fences/$$/tables whole."""
    lines = text.split("\n")
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        if line.startswith("```"):                       # fenced code
            chunk = [line]
            i += 1
            while i < n and not lines[i].startswith("```"):
                chunk.append(lines[i])
                i += 1
            if i < n:
                chunk.append(lines[i])
                i += 1
            yield "code", "\n".join(chunk[1:-1]), None
            continue
        if line.strip() == "$$":                         # display math
            chunk = []
            i += 1
            while i < n and lines[i].strip() != "$$":
                chunk.append(lines[i])
                i += 1
            i += 1
            yield "equation", "\n".join(chunk).strip(), None
            continue
        m = HEADING_RE.match(line)
        if m:
            yield "title", m.group(2).strip(), len(m.group(1))
            i += 1
            continue
        if line.lstrip().startswith("|"):                # table
            chunk = []
            while i < n and lines[i].lstrip().startswith("|"):
                chunk.append(lines[i].strip())
                i += 1
            yield "table", "\n".join(chunk), None
            continue
        if IMAGE_RE.match(line.strip()):                 # a lone image embed
            m2 = IMAGE_RE.match(line.strip())
            yield "image", (m2.group(1) or m2.group(2) or "").strip(), None
            i += 1
            continue
        m = FOOTNOTE_RE.match(line.strip())
        if m:
            yield "footnote", m.group(2).strip(), m.group(1)
            i += 1
            continue
        chunk = [line]                                   # paragraph
        i += 1
        while i < n and lines[i].strip() and not (
                lines[i].startswith("```") or lines[i].strip() == "$$"
                or HEADING_RE.match(lines[i]) or lines[i].lstrip().startswith("|")
                or IMAGE_RE.match(lines[i].strip()) or FOOTNOTE_RE.match(lines[i].strip())):
            chunk.append(lines[i])
            i += 1
        yield "text", "\n".join(chunk).strip(), None


def convert(md_path: Path, out: Path) -> tuple[list[dict], dict]:
    text = md_path.read_text(encoding="utf-8")
    body = text
    fm_title = None
    if text.startswith("---\n"):                         # keep frontmatter aside
        end = text.find("\n---", 4)
        if end > 0:
            fm = text[4:end]
            m = re.search(r"(?m)^title:\s*[\"']?(.+?)[\"']?\s*$", fm)
            fm_title = m.group(1) if m else None
            body = text[end + 4:].lstrip("\n")

    blocks: list[dict] = []
    warnings: list[str] = []
    in_refs = False
    heading_level = 99
    fid = 0
    for kind, content, extra in split_blocks(body):
        if kind == "title":
            heading_level = extra
            if REFS_HEADING_RE.match(content):
                in_refs = True
            elif extra <= heading_level:
                in_refs = False
        elif in_refs and kind == "text" and not REF_LINE_RE.match(content):
            in_refs = False                                # refs section ended
        if in_refs and kind == "text":
            kind = "ref"

        rec = {
            "id": f"b-{fid:06d}",
            "type": kind,
            "page": None,
            "bbox": [],
            "text": content,
            "translatable": kind not in NOT_TRANSLATABLE,
            "zh": "",
            "asset": content if kind == "image" else None,
            "caption": "",
            "flags": [],
        }
        if kind == "title":
            rec["level"] = max(1, min(6, extra if isinstance(extra, int) else 2))
        if kind == "footnote":
            rec["marker"] = str(extra)
            rec["translatable"] = True
        fid += 1
        if (rec["text"] or "").strip() or kind == "image":
            blocks.append(rec)
        else:
            warnings.append(f"skipped empty {kind} block")

    title = fm_title
    if not title:
        for b in blocks:
            if b["type"] == "title":
                title = b["text"]
                break
    meta = {
        "source_pdf": None,
        "source_md": str(md_path),
        "source_kind": "markdown",
        "stem": md_path.stem,
        "page_count": None,
        "document": {"title": title or md_path.stem},
        "title_pdf": title or "",
        "outline": [],
        "toc_pages": [],
        "affiliations": [],
        "block_count": len(blocks),
        "warnings": warnings,
        "xref": False,          # do not rewrite the author's own [12] citations
    }
    (out / "blocks.jsonl").write_text(
        "\n".join(json.dumps(b, ensure_ascii=False) for b in blocks) + "\n", encoding="utf-8")
    (out / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    return blocks, meta


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("markdown", type=Path)
    ap.add_argument("-o", "--out", type=Path, required=True)
    args = ap.parse_args(argv)
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    blocks, meta = convert(args.markdown.expanduser().resolve(), out)
    counts: dict[str, int] = {}
    for b in blocks:
        counts[b["type"]] = counts.get(b["type"], 0) + 1
    print(f"blocks : {len(blocks)}  {counts}")
    print(f"title  : {meta['document']['title']!r}")
    print(f"units  : {sum(1 for b in blocks if b['translatable'])} translatable")
    for w in meta["warnings"][:5]:
        print("  -", w)
    return 0


if __name__ == "__main__":
    sys.exit(main())
