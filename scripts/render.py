#!/usr/bin/env python
"""PDF -> Obsidian Markdown, stage 1 (normalize) + stage 2 (render).

Input : MinerU 4.x middle_json.json (+ the source PDF, for the outline/TOC)
Output: blocks.jsonl  (unified intermediate layer, one JSON record per block)
        meta.json     (document metadata + warnings)
        <stem>.assets/*.jpg   (kept beside the note so the folder can be
                               dropped anywhere in a vault: Obsidian resolves
                               [[<stem>.assets/x.jpg]] by path suffix)
        <stem>.md     (Obsidian-flavoured Markdown)

Stage 3 (translation) fills the "zh" field of translatable records and re-runs
the render stage.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from html.parser import HTMLParser
from pathlib import Path


# --------------------------------------------------------------------------
# LaTeX / text normalization
# --------------------------------------------------------------------------

def _join_spaced_letters(m):
    """{d o m} -> {dom}, {i . e .} -> {i.e.}

    The OCR models emit letter-by-letter spacing inside \\mathrm/\\operatorname.
    Only join when every whitespace-separated token is a single character, so
    real prose in \\text{...} is left alone.
    """
    inner = m.group(1)
    toks = inner.split()
    if len(toks) >= 2 and all(len(t) == 1 for t in toks):
        return "{" + "".join(toks) + "}"
    return m.group(0)


_LATEX_SUBST = [
    (re.compile(r"\{([^{}]*)\}"), _join_spaced_letters),
    (re.compile(r"\s*\^\s*"), "^"),
    (re.compile(r"\s*_\s*"), "_"),
    (re.compile(r"(\\[a-zA-Z]+)\s+\{"), r"\1{"),
    (re.compile(r"\{\s+"), "{"),
    (re.compile(r"\s+\}"), "}"),
    (re.compile(r"\s*&\s*"), " & "),
    (re.compile(r"[ \t]{2,}"), " "),
]


def latex_clean(s: str) -> str:
    """Collapse the OCR spacing that PP-FormulaNet / the VLM emit.

    \\operatorname { d o m }  ->  \\operatorname{dom}
    """
    if not s:
        return ""
    s = s.replace("\r", " ").replace("\n", " ")
    # MinerU occasionally emits an equation span whose content already carries
    # $...$ (or a stray label like "(1) $"), which would break our own wrapper
    s = s.replace("$", "")
    s = re.sub(r"\s+", " ", s)
    prev = None
    while prev != s:  # iterate: rules can unlock each other
        prev = s
        for rx, rep in _LATEX_SUBST:
            s = rx.sub(rep, s)
    return s.strip()


def get_text(node) -> str:
    """Flatten MinerU's irregular content shapes into plain text.

    Shapes seen in the wild: str, [span], [[span]], {type, content: str|list}.
    """
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "".join(get_text(x) for x in node)
    if isinstance(node, dict):
        if node.get("type") == "hyperlink":
            label = get_text(node.get("content")) or node.get("url", "")
            return f"[{label}]({node.get('url', '')})"
        return get_text(node.get("content"))
    return ""


def styles_wrap(text: str, styles) -> str:
    if not text:
        return text
    out = text
    if "bold" in styles:
        out = f"**{out}**"
    if "italic" in styles:
        out = f"*{out}*"
    if "superscript" in styles:
        out = f"<sup>{out}</sup>"
    if "subscript" in styles:
        out = f"<sub>{out}</sub>"
    return out


def spans_to_md(spans, footnote_markers) -> str:
    """Render a span list to inline Markdown.

    equation_inline spans become $...$ so the translation stage can skip them
    by span type instead of guessing with a regex.
    """
    out = []
    for sp in spans or []:
        if isinstance(sp, str):
            out.append(sp)
            continue
        kind = sp.get("type")
        if kind == "equation_inline":
            out.append(f"${latex_clean(get_text(sp.get('content')) or '')}$")
        elif kind == "hyperlink":
            out.append(get_text(sp))
        elif kind == "text":
            raw = get_text(sp.get("content"))
            styles = sp.get("styles") or []
            # a bare number in a superscript on a page that carries footnotes
            # is a footnote reference, not an exponent
            if "superscript" in styles and raw.strip() in footnote_markers:
                out.append(f"[^{raw.strip()}]")
            else:
                out.append(styles_wrap(raw, styles))
        else:
            out.append(get_text(sp.get("content")))
    text = "".join(out)
    return re.sub(r"[ \t]{2,}", " ", text).strip()


# --------------------------------------------------------------------------
# HTML table -> GFM
# --------------------------------------------------------------------------

class _TableParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None
        self._colspan = 1
        self._depth = 0

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self._row = []
        elif tag in ("td", "th"):
            self._cell = []
            self._colspan = int(dict(attrs).get("colspan") or 1)
        elif tag == "br" and self._cell is not None:
            self._cell.append(" ")
        elif tag in ("sup", "sub") and self._cell is not None:
            self._cell.append(f"<{tag}>")
            self._depth += 1

    def handle_endtag(self, tag):
        if tag == "tr" and self._row is not None:
            self.rows.append(self._row)
            self._row = None
        elif tag in ("td", "th") and self._cell is not None and self._row is not None:
            cell = "".join(self._cell).strip()
            self._row.append(cell)
            self._row.extend([""] * (self._colspan - 1))
            self._cell = None
        elif tag in ("sup", "sub") and self._depth:
            self._cell.append(f"</{tag}>")
            self._depth -= 1

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)


def html_table_to_gfm(html: str) -> str:
    p = _TableParser()
    p.feed(html or "")
    rows = [r for r in p.rows if any(c.strip() for c in r)]
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]

    def esc(c):
        return c.replace("|", "\\|").replace("\n", " ").strip()

    lines = ["| " + " | ".join(esc(c) for c in rows[0]) + " |",
             "| " + " | ".join(["---"] * width) + " |"]
    for r in rows[1:]:
        lines.append("| " + " | ".join(esc(c) for c in r) + " |")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Stage 1: normalize
# --------------------------------------------------------------------------

SKIP_TYPES = {"page_number", "page_header", "header", "footer", "index", "doc_title"}
# margin text: on arXiv PDFs this is the vertical "arXiv:1706.03762v7 [cs.CL]" stamp.
# Dropped from the body, but mined for identifiers first (see normalize()).
ASIDE_TYPES = {"aside_text", "aside"}

# MinerU block type -> our type
TYPE_MAP = {
    "text": "text",
    "paragraph_title": "title",
    "equation": "equation",
    "image": "image",
    "chart": "image",       # a chart is a figure: chart_body carries the crop
    "table": "table",
    "code": "code",
    "page_footnote": "footnote",
    "ref_text": "ref",
    "list": "list",
}

NOT_TRANSLATABLE = {"equation", "table", "code", "ref", "image"}


def guess_type(content):
    """Fallback for block types we have never seen.

    Dropping an unknown type silently loses content (a real case: MinerU's
    `chart` carried two attention-visualisation figures). Decide by shape
    instead: something with an image_path is a figure, something with text is
    body text, anything else is genuinely empty.
    """
    parts = content if isinstance(content, list) else []
    if any(isinstance(p, dict) and p.get("image_path") for p in parts):
        return "image"
    if isinstance(content, str) and content.strip():
        return "text"
    if any(isinstance(p, dict) and (p.get("content") or p.get("html")) for p in parts):
        return "text"
    return None


UNNUMBERED_OK = re.compile(
    r"^(abstract|contents|references|bibliography|acknowledge?ments?|appendix(\s+[A-Z0-9])?|"
    r"introduction|conclusion|related work|preliminaries|background|discussion|"
    r"future work|limitations|notation)\b", re.I)


def looks_like_heading(title: str, toc_map: dict[str, int]) -> bool:
    """Guard against paragraph_title false positives.

    MinerU labels a body paragraph as paragraph_title now and then (observed:
    the paragraph that opens with "Definition 51. Write").
    """
    t = re.sub(r"\s+", " ", title).strip()
    if t.rstrip(". ") in toc_map:
        return True
    if re.match(r"^\d+(?:\.\d+)*\.?\s", t):
        return True
    if re.match(r"^[A-Z]\.\s", t):
        return True
    if UNNUMBERED_OK.match(t):
        return True
    return False


def _pymupdf():
    """PyMuPDF is optional: without it we lose the PDF outline (heading levels
    then come from section numbering) and page count, but nothing else."""
    try:
        import pymupdf
        return pymupdf
    except ImportError:
        return None


def build_toc_map(pdf_path: Path) -> dict[str, int]:
    pymupdf = _pymupdf()
    if pymupdf is None or pdf_path is None or not Path(pdf_path).exists():
        return {}
    out = {}
    doc = pymupdf.open(str(pdf_path))
    for lvl, title, _page in doc.get_toc(simple=True):
        key = re.sub(r"\s+", " ", title).strip().rstrip(". ")
        out.setdefault(key, lvl)
        # outlines often say "Introduction" where the page says "1 Introduction"
        bare = re.sub(r"^\d+(?:\.\d+)*\.?\s*", "", key).strip()
        if bare:
            out.setdefault(bare, lvl)
    doc.close()
    return out


def look_up_outline(title: str, toc_map: dict[str, int]):
    key = re.sub(r"\s+", " ", title).strip().rstrip(". ")
    if key in toc_map:
        return toc_map[key]
    return toc_map.get(re.sub(r"^\d+(?:\.\d+)*\.?\s*", "", key).strip())


def heading_level(title: str, toc_map: dict[str, int]) -> int:
    """Markdown heading depth: doc title is #, so a top-level section is ##."""
    t = re.sub(r"\s+", " ", title).strip()
    from_outline = look_up_outline(t, toc_map)
    if from_outline:
        return min(from_outline + 1, 6)
    m = re.match(r"^(\d+(?:\.\d+)*)\.?\s", t)
    if m:
        return min(m.group(1).count(".") + 2, 6)
    return 2


def pdf_page_count(pdf_path: Path):
    pymupdf = _pymupdf()
    if pymupdf is None or pdf_path is None or not Path(pdf_path).exists():
        return None
    doc = pymupdf.open(str(pdf_path))
    n = doc.page_count
    doc.close()
    return n


INSTITUTION_RE = re.compile(
    r"(universit|institut|college|academy|laborator|\blabs?\b|research|school|"
    r"department|company|inc\.|corp|hospital|center|centre|\bAI\b|group)", re.I)


def _clean_affiliation_text(s: str) -> str:
    s = re.sub(r"</?su[bp]>", "", s)
    s = re.sub(r"\[\^[^\]]*\]", "", s)          # footnote markers leaked as [^∗]
    s = re.sub(r"[\w.+-]+@[\w.-]+", "", s)      # emails
    for ch in "*†‡∗§":
        s = s.replace(ch, " ")
    return re.sub(r"\s+", " ", s).strip(" ,;.·|-–")


def extract_affiliations(front_text: list[str]) -> list[str]:
    """Pull institution names out of the title block on page 1.

    Only dedicated affiliation lines are used. Author lines are skipped: on real
    papers those lines interleave person names, emails and institutions in ways
    that cannot be split reliably, and a wrong affiliation is worse than a
    missing one. Measured failures that motivated this: "**Aidan N. Gomez**\u2020
    University of Toronto aidan@cs.toronto.edu" used to yield the author as an
    affiliation, and "**Yifan Shi** Equal contribution. Tsinghua University" used
    to yield the whole line.
    """
    out: list[str] = []
    for t in front_text:
        if re.search(r"[\w.+-]+@[\w.-]+", t):     # an author line, not an affiliation
            continue
        if t.lstrip().startswith("**") or NON_AFFILIATION_RE.search(t):
            continue                                # a named author, or a statement
        cleaned = _clean_affiliation_text(t)
        for p in (x.strip(" ,;.·|-–") for x in re.split(r"\d+", cleaned)):
            if len(p) > 2 and INSTITUTION_RE.search(p):
                if p.lower() not in {o.lower() for o in out}:
                    out.append(p)
    return out


EMAIL_RE = re.compile(r"[\w.+-]+@[\w.-]+")
# A superscript-style marker: 1, 1,2 or a symbol; the author list and the
# affiliation list both use them, which is what makes the pairing explicit.
MARKER_RUN_RE = re.compile(r"[\d*†‡∗§][\d\s,*†‡∗§]*")
NAME_RE = re.compile(r"^([A-Z][\w.'’\-]+(?:\s+[A-Z][\w.'’\-]+){1,3})\s*$")


def _strip_markers(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[*†‡∗§]", " ", text)).strip(" ,;.")


def affiliation_markers(front_text: list[str]) -> dict:
    """Marker -> institution, for title blocks that label affiliations 1,2,*.

    Only the marker-to-text pairing the paper itself prints is read; nothing is
    inferred from an institution's position in a list. Author lines are skipped:
    they are names, markers and addresses, not affiliation statements.
    """
    out: dict[str, str] = {}
    for line in front_text:
        if EMAIL_RE.search(line) or "**" in line:
            continue
        for chunk in re.split(r"(?<=[A-Za-z])\s+(?=\d)\s*", line):
            m = re.match(r"(?P<marker>[\d*†‡∗§][\d\s,*†‡∗§]*?)\s*(?P<text>.+)$", chunk.strip())
            if not m:
                continue
            text = _clean_affiliation_text(_strip_markers(m.group("text")))
            if not text or not INSTITUTION_RE.search(text):
                continue
            for marker in re.findall(r"\d+|[*†‡∗§]", m.group("marker")):
                out.setdefault(marker, text)
    return out


NON_AFFILIATION_RE = re.compile(
    r"equal contribution|correspond|work done|currently at|now at|jointly|"
    r"these authors|shared first|alphabetical", re.I)


def _inline_affiliation(line: str, name: str, email: str | None) -> str | None:
    """The institution an author's own line states, if it states one.

    On a line like `**Ashish Vaswani** Google Brain avaswani@google.com` the text
    after the name (and after the address) is the affiliation, whether or not it
    reads like an institution - `Google Brain` has no "university" in it. A
    contribution statement on the same line is dropped before deciding, so
    `**Yifan Shi** Equal contribution. Tsinghua University` yields the university
    and a line that only talks about contributions yields nothing.
    """
    if not line:
        return None
    tail = re.sub(re.escape(name), " ", line, flags=re.I)
    if email:
        tail = tail.replace(email, " ")
    tail = _clean_affiliation_text(tail)
    tail = re.sub(r"\s+", " ", NON_AFFILIATION_RE.sub(" ", tail)).strip(" .,;·|-–")
    if not tail or len(tail) > 80:
        return None
    return tail


def authors_from_front(front_text: list[str], title: str = "") -> list[tuple[str, str]]:
    """(name, markers) pairs from the title block.

    MinerU's own author metadata is often empty, while the title block has the
    authors one per line - in bold, or in the comma-separated run a compact
    paper puts under the title. A candidate only counts when the paper marks it
    as an author (bold, or carrying an affiliation marker), so a section heading
    or the title itself cannot slip in.
    """
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for line in front_text:
        # split between authors, never inside a marker run like "1,2"
        for chunk in [c.strip() for c in re.split(r",\s*(?=[A-Z])", line)]:
            if not chunk:
                continue
            bold = re.match(r"^\*\*(?P<name>.+?)\*\*(?P<rest>.*)$", chunk)
            name, rest = (bold.group("name"), bold.group("rest")) if bold else (None, chunk)
            if name is None:
                # an address on the line belongs to the author, not to the name
                candidate = re.sub(r"[\w.+-]+@[\w.-]+", " ", chunk)
                m = NAME_RE.match(re.sub(r"[\d\s,*†‡∗§]+$", "", candidate).strip())
                if not m:
                    continue
                name = m.group(1)
                rest = (re.search(r"([\d*†‡∗§][\d\s,*†‡∗§]*)$", candidate.strip()) or [None, ""])[1]
            name = _strip_markers(name)
            if not name or INSTITUTION_RE.search(name) or name.lower() in seen:
                continue
            markers = ",".join(re.findall(r"\d+|[*†‡∗§]", rest))
            seen.add(name.lower())
            out.append((name, markers))
    return out


def _corresponding_email(front_text: list[str]) -> str | None:
    """The correspondence address, when the front block makes it unambiguous.

    Per-author addresses are not recoverable from a paper's title block, so this
    only answers for the one address a paper usually prints, and only when it is
    named as the correspondence one or is the only address present.
    """
    found = [m.group(0) for t in front_text for m in EMAIL_RE.finditer(t)]
    found = list(dict.fromkeys(found))
    if len(found) == 1:
        return found[0]
    for t in front_text:
        if re.search(r"correspond\w*|通讯作者", t, re.I):
            m = EMAIL_RE.search(t)
            if m:
                return m.group(0)
    return None


def creators_from_front(authors, front_text: list[str],
                        affiliations: list[str]) -> list[dict]:
    """Zotero creators from the title block, with the affiliations we can pair.

    The pairing only follows what the paper states: the institution on an
    author's own line, the affiliation markers that line carries (1,2,*), or the
    single affiliation of the whole title block. Institutions are never matched
    to people by position, because that is how a wrong affiliation gets in. The
    correspondence address goes to the author it is printed beside.
    """
    email = _corresponding_email(front_text)
    markers = affiliation_markers(front_text)
    entries = [e if isinstance(e, tuple) else (re.sub(r"\s+", " ", str(e)).strip(), "")
               for e in (authors or [])]
    names = [n for n, _ in entries if n]
    out: list[dict] = []
    for name, carried in entries:
        if not name:
            continue
        line = next((t for t in front_text if name.lower() in t.lower()), "")
        line_email = EMAIL_RE.search(line).group(0) if line and EMAIL_RE.search(line) else None
        # an author's own line states their affiliation only when it is their line:
        # on a shared line the remaining text is the other authors, not an institution
        shared = [n for n in names if n.lower() != name.lower() and n.lower() in line.lower()]
        affiliation = None if shared else _inline_affiliation(line, name, line_email)
        if not affiliation:
            own = [a for a in affiliations if line and a.lower() in line.lower()]
            if not own and carried and markers:
                own = [markers[m] for m in re.findall(r"\d+|[*†‡∗§]", carried) if m in markers]
            if own:
                affiliation = ", ".join(dict.fromkeys(own))
            elif len(affiliations) == 1:
                affiliation = affiliations[0]
        address = email if (email and line and email in line) else None
        suffix = ", ".join(x for x in (affiliation, address) if x)
        if suffix:
            out.append({"creatorType": "author", "name": f"{name} ({suffix})"})
            continue
        parts = name.split()
        out.append({"creatorType": "author",
                    "firstName": " ".join(parts[:-1]),
                    "lastName": parts[-1]})
    return out


def normalize(middle_json: Path, pdf_path: Path, out_dir: Path,
              stem: str) -> tuple[list[dict], dict]:
    data = json.loads(middle_json.read_text(encoding="utf-8"))
    toc_map = build_toc_map(pdf_path)
    pages = data["pages"]
    blocks: list[dict] = []
    warnings: list[str] = []
    unmatched_titles: list[str] = []

    # footnote markers present per page, so body references can be resolved
    markers_by_page: dict[int, set[str]] = {}
    for pg in pages:
        found = set()
        for b in pg["blocks"]:
            if b.get("type") == "page_footnote":
                c = b.get("content") or []
                if c and isinstance(c[0], dict):
                    found.add(get_text(c[0]).strip())
        markers_by_page[pg["page_idx"]] = found

    fid = 0
    seen_heading = False
    front_text: list[str] = []
    aside_text: list[str] = []
    doc_title_block = ""
    toc_pages: set[int] = set()
    for pg in pages:
        pno = pg["page_idx"]
        markers = markers_by_page.get(pno, set())
        for b in pg["blocks"]:
            btype = b.get("type")
            if btype in SKIP_TYPES:
                if btype == "index":
                    toc_pages.add(pno)
                elif btype == "doc_title":
                    # MinerU reports this as a block; its own metadata.document.title
                    # is often empty (arXiv PDFs), so the block is the real source
                    doc_title_block = get_text(b.get("content")).strip()
                continue
            if btype in ASIDE_TYPES:
                aside_text.append(get_text(b.get("content")))
                continue
            our = TYPE_MAP.get(btype)
            if our is None:
                our = guess_type(b.get("content"))
                if our is None:
                    warnings.append(f"dropped empty block of unknown type {btype!r} "
                                    f"on page {pno + 1}")
                    continue
                warnings.append(f"unknown block type {btype!r} on page {pno + 1}, "
                                f"kept as {our}")

            rec = {
                "id": f"b-{fid:06d}",
                "type": our,
                "page": pno,
                "bbox": [round(x, 4) for x in b.get("bbox", [])],
                "text": "",
                "translatable": our not in NOT_TRANSLATABLE,
                "zh": "",
                "asset": None,
                "caption": "",
                "flags": [],
            }
            fid += 1

            content = b.get("content")

            if our == "title":
                title = get_text(content)
                if not looks_like_heading(title, toc_map):
                    # demote a misclassified paragraph back to body text
                    rec["type"] = "text"
                    rec["text"] = title
                    rec["translatable"] = True
                    blocks.append(rec)
                    warnings.append(f"demoted false heading on page {pno + 1}: {title[:60]!r}")
                    continue
                rec["text"] = title
                rec["level"] = heading_level(title, toc_map)
                seen_heading = True
                # headings stay in the source language: the outline pane and
                # cross-reference text both read better untranslated
                rec["translatable"] = False
                if re.sub(r"\s+", " ", title).strip().rstrip(". ") not in toc_map and \
                        not look_up_outline(title, toc_map):
                    unmatched_titles.append(title)
            elif our == "text":
                rec["text"] = spans_to_md(content, markers)
                if not rec["text"]:
                    continue
                # MinerU sometimes lifts an equation's number out of the display
                # equation into its own text block; the number is already in \tag{}
                if re.fullmatch(r"\(\d{1,3}\)", rec["text"].strip()):
                    warnings.append(f"dropped stray equation label {rec['text']} on page {pno + 1}")
                    continue
                # the title block (authors, affiliations, keywords) belongs in the
                # frontmatter, not repeated at the top of the body
                if pno == 0 and not seen_heading and len(rec["text"]) < 300:
                    front_text.append(rec["text"])
                    continue
            elif our == "equation":
                rec["latex"] = latex_clean(get_text(content))
                rec["text"] = rec["latex"]
                if b.get("image_path"):
                    rec["asset"] = f"{stem}.assets/{Path(b['image_path']).name}"
                    rec["asset_src"] = b["image_path"]
            elif our == "code":
                body = content[0].get("content") if isinstance(content, list) and content else content
                rec["text"] = get_text(body)
                if not rec["text"].strip():
                    continue
            elif our == "table":
                caption, footnote, body_html, asset = "", "", "", None
                for c in content or []:
                    ct = c.get("type")
                    if ct == "table_caption":
                        caption = spans_to_md(c.get("content"), markers)
                    elif ct == "table_footnote":
                        footnote = spans_to_md(c.get("content"), markers)
                    elif ct == "table_body":
                        body_html = c.get("content") or ""
                        if c.get("image_path"):
                            asset = f"{stem}.assets/{Path(c['image_path']).name}"
                            rec["asset_src"] = c["image_path"]
                rec["caption"] = caption
                rec["text"] = html_table_to_gfm(body_html)
                rec["asset"] = asset
                rec["footnote"] = footnote
                # MinerU is inconsistent about which field carries "Table N | ...",
                # and it sometimes labels the paragraph *before* the table as its
                # caption (that paragraph can itself begin with "Table 1 is ...").
                cap_ok = bool(re.match(r"^Table\s*\d+\s*[|.:]", caption))
                note_ok = bool(re.match(r"^Table\s*\d+\s*[|.:]", footnote))
                if note_ok and not cap_ok:
                    rec["caption"], rec["footnote"] = footnote, caption
                leftover = rec["footnote"]
                if len(leftover) > 160:
                    extra = dict(rec, id=f"b-{fid:06d}", type="text", text=leftover,
                                 caption="", footnote="", bbox=[])
                    extra.pop("asset", None)
                    extra.pop("asset_src", None)
                    fid += 1
                    blocks.append(extra)   # prose before the table stays before it
                    rec["footnote"] = ""
                    warnings.append(f"long table_caption kept as text on page {pno + 1}")
                if not rec["text"]:
                    warnings.append(f"empty table on page {pno + 1}")
                    continue
            elif our == "image":
                asset, cap = None, ""
                for c in content or []:
                    ct = str(c.get("type") or "")
                    # image_body, chart_body, picture_body... all carry image_path
                    if c.get("image_path") and (ct.endswith("_body") or asset is None):
                        asset = f"{stem}.assets/{Path(c['image_path']).name}"
                        rec["asset_src"] = c["image_path"]
                    elif ct.endswith("caption") or ct == "caption":
                        cap = spans_to_md(c.get("content"), markers) or cap
                rec["asset"] = asset
                rec["caption"] = cap
                if not asset:
                    continue
            elif our == "footnote":
                marker = ""
                parts = []
                for i, sp in enumerate(content or []):
                    if isinstance(sp, dict) and "superscript" in (sp.get("styles") or []) and i == 0:
                        marker = get_text(sp).strip()
                    else:
                        parts.append(spans_to_md([sp], markers))
                rec["marker"] = marker or str(pno + 1)
                rec["text"] = " ".join(p for p in parts if p).strip()
                rec["translatable"] = True
            elif our == "ref":
                rec["text"] = spans_to_md(content, markers)
                if not rec["text"].strip():
                    continue

            blocks.append(rec)

    # merge tables/captions that MinerU split into neighbouring blocks
    document = dict(data.get("metadata", {}).get("document", {}) or {})
    if not document.get("title") and doc_title_block:
        document["title"] = doc_title_block
    affiliations = extract_affiliations(front_text)
    authors = document.get("authors") or []
    if authors and isinstance(authors[0], str) and "," in authors[0]:
        authors = [a.strip() for a in authors[0].split(",")]
    if not authors:
        # MinerU's author metadata is often empty; the title block still has them
        authors = authors_from_front(front_text, document.get("title") or "")
    item = {"itemType": "journalArticle",   # replaced once the document is identified
            "title": document.get("title") or stem,
            "creators": creators_from_front(authors, front_text, affiliations)}
    meta = {
        # The metadata is a Zotero item. Every other key in this file is pipeline
        # state, and none of it reaches the note.
        "item": item,
        "page_count": pdf_page_count(pdf_path),
        "stem": stem,
        "source_kind": "pdf",
        "parser": data.get("metadata", {}).get("producer", {}),
        "doc_title_block": doc_title_block,
        "block_count": len(blocks),
        "warnings": warnings,
        # only meaningful when the PDF actually has an outline to compare against
        # (arXiv PDFs generally do not)
        "titles_not_in_outline": unmatched_titles if toc_map else [],
        "front_text": front_text,
        "aside_text": aside_text,
        "toc_pages": sorted(toc_pages),
        "is_full_document": data.get("is_full_document", False),
    }
    return blocks, meta


# --------------------------------------------------------------------------
# Stage 2: render
# --------------------------------------------------------------------------

STATEMENT_CLASSES = {
    "Theorem": "thm", "Lemma": "lem", "Definition": "def", "Corollary": "cor",
    "Proposition": "prop", "Remark": "rem", "Example": "ex",
}
_XREF_WORDS = "Figure|Fig\\.|Table|Algorithm|Theorem|Lemma|Definition|Corollary|Proposition|Section"
_STMT_RE = re.compile(r"^\*\*(%s)\s+(\d+)\.\*\*" % "|".join(STATEMENT_CLASSES))


def collect_targets(blocks: list[dict]) -> dict:
    """First pass: every place a cross-reference may point at."""
    tg = {"refs": {}, "figs": set(), "tabs": set(), "algos": set(), "eqs": set(),
          "statements": {}, "headings": {}, "dupes": []}
    for rec in blocks:
        t, txt = rec["type"], (rec.get("text") or "")
        if t == "ref":
            m = re.match(r"^\[(\d+)\]", txt)
            if m:
                tg["refs"].setdefault(m.group(1), rec["id"])
        elif t == "title":
            m = re.match(r"^(\d+(?:\.\d+)*)\.?\s", txt)
            if m:
                tg["headings"][m.group(1)] = txt
        elif t == "equation":
            for m in re.finditer(r"\\tag\{(\d+)\}", rec.get("latex") or ""):
                tg["eqs"].add(m.group(1))
        elif t == "image":
            m = re.match(r"^(?:Figure|Fig\.)\s*(\d+)", rec.get("caption") or "")
            if m:
                tg["figs"].add(m.group(1))
        elif t == "table":
            m = re.match(r"^Table\s*(\d+)", rec.get("caption") or "")
            if m:
                tg["tabs"].add(m.group(1))
        elif t == "code":
            m = re.match(r"^Algorithm\s*(\d+)", txt)
            if m:
                tg["algos"].add(m.group(1))
        elif t == "text":
            m = _STMT_RE.match(txt)
            if m:
                key = (m.group(1), m.group(2))
                if key in tg["statements"]:
                    tg["dupes"].append(key)
                else:
                    tg["statements"][key] = rec["id"]
    return tg


def anchor_of(rec: dict, tg: dict) -> str:
    """Block id, but only where a cross-reference can actually land.

    Emitting one on every paragraph buries the source in ^b-000123 noise.
    """
    t, txt = rec["type"], (rec.get("text") or "")
    if t == "ref":
        m = re.match(r"^\[(\d+)\]", txt)
        if m and tg["refs"].get(m.group(1)) == rec["id"]:
            return f"ref-{m.group(1)}"
    elif t == "image":
        m = re.match(r"^(?:Figure|Fig\.)\s*(\d+)", rec.get("caption") or "")
        if m and m.group(1) in tg["figs"]:
            return f"fig-{m.group(1)}"
    elif t == "table":
        m = re.match(r"^Table\s*(\d+)", rec.get("caption") or "")
        if m and m.group(1) in tg["tabs"]:
            return f"tab-{m.group(1)}"
    elif t == "code":
        m = re.match(r"^Algorithm\s*(\d+)", txt)
        if m and m.group(1) in tg["algos"]:
            return f"algo-{m.group(1)}"
    elif t == "equation":
        m = re.search(r"\\tag\{(\d+)\}", rec.get("latex") or "")
        if m and m.group(1) in tg["eqs"]:
            return f"eq-{m.group(1)}"
    elif t == "text":
        m = _STMT_RE.match(txt)
        if m and tg["statements"].get((m.group(1), m.group(2))) == rec["id"]:
            return f"{STATEMENT_CLASSES[m.group(1)]}-{m.group(2)}"
    return ""


def _expand_range(inner: str, max_span: int = 8) -> str:
    """[8-10] cites 8, 9 and 10 -- expand the dash rather than drop the middle."""
    out, pos = [], 0
    for m in re.finditer(r"(\d+)\s*[–—-]\s*(\d+)", inner):
        a, b = int(m.group(1)), int(m.group(2))
        if 0 < b - a <= max_span:
            out.append(inner[pos:m.start()])
            out.append(", ".join(str(n) for n in range(a, b + 1)))
            pos = m.end()
    out.append(inner[pos:])
    return "".join(out)


def _linkify_plain(s: str, tg: dict) -> str:
    def cite(m):
        # expand first: the numbers the range stands for are what must resolve
        inner = _expand_range(m.group(1))
        nums = re.findall(r"\d+", inner)
        # only treat it as a citation when *every* number resolves
        if not nums or not all(n in tg["refs"] for n in nums):
            return m.group(0)
        # A literal "[" may not sit next to "[[": Obsidian's parser would see
        # "[[[#^ref-1|1]]]". The brackets are therefore dropped.
        return re.sub(r"\d+", lambda mm: f"[[#^ref-{mm.group(0)}|{mm.group(0)}]]", inner)

    s = re.sub(r"\[(\d+(?:\s*[,–—-]\s*\d+)*)\]", cite, s)

    def named(m):
        word, num = m.group(1), m.group(2)
        if word == "Section":
            target = tg["headings"].get(num)
            return f"[[#{target}|{word} {num}]]" if target else m.group(0)
        if word in ("Figure", "Fig."):
            return f"[[#^fig-{num}|{word} {num}]]" if num in tg["figs"] else m.group(0)
        if word == "Table":
            return f"[[#^tab-{num}|{word} {num}]]" if num in tg["tabs"] else m.group(0)
        if word == "Algorithm":
            return f"[[#^algo-{num}|{word} {num}]]" if num in tg["algos"] else m.group(0)
        kind = STATEMENT_CLASSES.get(word)
        if kind and (word, num) in tg["statements"]:
            return f"[[#^{kind}-{num}|{word} {num}]]"
        return m.group(0)

    s = re.sub(rf"\b({_XREF_WORDS})\s+(\d+(?:\.\d+)*)", named, s)

    # The Chinese translation renders the same references in Chinese ("Theorem 7"
    # becomes 定理 7), so link those forms too -- otherwise every cross-reference
    # in the translated half of the document is dead text.
    zh_kind = {"定理": "thm", "引理": "lem", "定义": "def", "推论": "cor",
               "命题": "prop", "图": "fig", "表": "tab", "表格": "tab",
               "算法": "algo"}

    def zh_named(m):
        word, num = m.group(1), m.group(2)
        if word in ("节", "章", "章节"):
            target = tg["headings"].get(num)
            return f"[[#{target}|{m.group(0)}]]" if target else m.group(0)
        kind = zh_kind[word]
        pool = {"fig": tg["figs"], "tab": tg["tabs"], "algo": tg["algos"]}.get(kind)
        if pool is not None:
            return f"[[#^{kind}-{num}|{m.group(0)}]]" if num in pool else m.group(0)
        en = {v: k for k, v in STATEMENT_CLASSES.items()}.get(kind, "")
        if (en, num) in tg["statements"]:
            return f"[[#^{kind}-{num}|{m.group(0)}]]"
        return m.group(0)

    s = re.sub(r"(定理|引理|定义|推论|命题|图|表|表格|算法|节|章|章节)\s*(\d+(?:\.\d+)*)",
               zh_named, s)

    def zh_section(m):
        num = m.group(1)
        target = tg["headings"].get(num)
        return f"[[#{target}|{m.group(0)}]]" if target else m.group(0)

    return re.sub(r"第\s*(\d+(?:\.\d+)*)\s*(?:节|章)", zh_section, s)


def linkify(text: str, tg: dict) -> str:
    """Turn citation numbers and named cross-references into wikilinks.

    Math spans are left untouched so $...$ content is never rewritten.
    """
    parts = re.split(r"(\$\$.*?\$\$|\$[^$\n]+\$)", text, flags=re.S)
    return "".join(p if i % 2 else _linkify_plain(p, tg) for i, p in enumerate(parts))


_ZOTERO_SCHEMA: dict | None = None


def zotero_fields(item_type: str) -> list[str]:
    """The fields Zotero allows on this item type, in Zotero's own order."""
    global _ZOTERO_SCHEMA
    if _ZOTERO_SCHEMA is None:
        try:
            from zotero_schema import load as _load
            _ZOTERO_SCHEMA = _load()
        except Exception:
            _ZOTERO_SCHEMA = {}
    return list(_ZOTERO_SCHEMA.get("itemTypes", {}).get(item_type, {}).get("fields", []))


def frontmatter(item: dict) -> list[str]:
    """The note's properties: the fields of a Zotero item, in Zotero's order.

    A field the item's type does not allow is dropped here rather than written,
    so the properties can never drift outside Zotero's model; verify.py reports
    what was dropped.
    """
    item_type = item.get("itemType") or "document"
    out = [f"itemType: {item_type}"]
    if item.get("title"):
        out.append(f"title: {json.dumps(item['title'], ensure_ascii=False)}")
    creators = item.get("creators") or []
    if creators:
        out.append("creators:")
        for c in creators:
            out.append(f"  - creatorType: {c.get('creatorType', 'author')}")
            if c.get("name"):
                out.append(f"    name: {json.dumps(c['name'], ensure_ascii=False)}")
            else:
                out.append(f"    firstName: {json.dumps(c.get('firstName') or '', ensure_ascii=False)}")
                out.append(f"    lastName: {json.dumps(c.get('lastName') or '', ensure_ascii=False)}")
    for field in zotero_fields(item_type):
        if field in ("title",):
            continue
        value = item.get(field)
        if value in (None, "", []):
            continue
        out.append(f"{field}: {json.dumps(value, ensure_ascii=False)}")
    return out


def render(blocks: list[dict], meta: dict, out_dir: Path, stem: str,
           block_anchors: bool = False,
           outline_nav: bool = False, xref: bool = True,
           zh_style: str = "callout-open") -> tuple[str, dict]:
    L: list[str] = []
    item = meta.get("item") or {}

    L.append("---")
    L.extend(frontmatter(item))
    L.append(f"tags: [{('note' if meta.get('source_kind') == 'markdown' else 'paper')}, "
             "bilingual-paper-notes]")
    L.append("---")
    L.append("")

    if outline_nav and meta.get("outline"):
        L.append("## Outline")
        L.append("")
        for lvl, title, page in meta["outline"]:
            indent = "  " * max(0, lvl - 1)
            L.append(f"{indent}- {title}")
        L.append("")

    tg = collect_targets(blocks) if xref else None
    footnotes: list[tuple[str, str]] = []
    footnotes_zh: dict[str, str] = {}
    for rec in blocks:
        aid = anchor_of(rec, tg) if tg else ""
        anchor = f" ^{aid}" if aid else ""
        if not aid and block_anchors:
            anchor = f" ^{rec['id']}"

        t = rec["type"]
        zh = (rec.get("zh") or "").strip()
        cap_zh = (rec.get("caption_zh") or "").strip()

        def zh_lines(text: str, indent: str = "") -> list[str]:
            """The Chinese translation, as a foldable callout (default) or a quote.

            "+ " starts it open (still foldable), "- " starts it folded. A blank
            line inside a callout still needs a ">" or the callout is cut short.
            """
            if zh_style == "quote":
                head = []
            else:
                flag = "-" if zh_style == "callout-folded" else "+"
                head = [f"> [!zh]{flag} 译文"]
            body = []
            for ln in text.split("\n"):
                if ln.strip():
                    body.append("> " + (linkify(ln, tg) if tg else ln))
                else:
                    body.append(">")
            return [indent + ln for ln in head + body]

        if t == "title":
            L.append("")
            L.append("#" * rec.get("level", 2) + " " + rec["text"])
            if zh:
                L.append("")
                L.extend(zh_lines(zh))
            L.append("")
        elif t == "text":
            body = rec["text"]
            if tg:
                m = _STMT_RE.match(body)
                head = m.group(0) if m else ""
                body = head + (" " if head else "") + linkify(body[len(head):].lstrip(), tg)
            L.append(body + anchor)
            L.append("")
            if zh:
                L.extend(zh_lines(zh))
                L.append("")
        elif t == "equation":
            L.append("$$")
            L.append(rec["text"])
            L.append("$$")
            L.append("")
            if aid:
                # structured block: the id goes on its own line, blank lines around
                L.append("^" + aid)
                L.append("")
        elif t == "code":
            L.append("```plaintext")
            L.append(rec["text"])
            L.append("```")
            L.append("")
            if anchor:
                L.append(anchor.strip())
                L.append("")
        elif t == "table":
            if rec.get("caption"):
                L.append("*" + rec["caption"] + "*")
                L.append("")
                if cap_zh:
                    L.extend(zh_lines(cap_zh))
                    L.append("")
            L.append(rec["text"])
            L.append("")
            if anchor:
                # structured block: own line, blank lines around
                L.append(anchor.strip())
                L.append("")
            if rec.get("footnote"):
                L.append("*" + rec["footnote"] + "*")
                L.append("")
        elif t == "image":
            if rec.get("asset"):
                L.append(f"![[{rec['asset']}]]")
                L.append("")
                if rec.get("caption"):
                    L.append("*" + rec["caption"] + "*" + anchor)
                    L.append("")
                    if cap_zh:
                        L.extend(zh_lines(cap_zh))
                        L.append("")
        elif t == "footnote":
            footnotes.append((rec.get("marker", ""), rec["text"]))
            if zh:
                footnotes_zh[rec.get("marker", "")] = zh
        elif t == "ref":
            L.append(rec["text"] + anchor)
            L.append("")

    if footnotes:
        L.append("")
        for marker, text in footnotes:
            L.append(f"[^{marker}]: {text}")
            zh_fn = footnotes_zh.get(marker, "")
            if zh_fn:
                # a footnote continuation line must be indented for Obsidian to
                # keep it inside the definition; kept as plain indented text,
                # not a callout, since nested callouts here render unreliably
                L.append("")
                for line in zh_fn.split("\n"):
                    L.append("    " + (linkify(line, tg) if tg else line))
        L.append("")

    md = "\n".join(L).rstrip() + "\n"
    stats = {
        "citation_links": len(re.findall(r"\[\[#\^ref-", md)),
        "item_links": len(re.findall(r"\[\[#\^(?:fig|tab|algo|thm|lem|def|cor|prop)-", md)),
        "section_links": len(re.findall(r"\[\[#(?!\^)", md)),
        "zh_links": sum(len(re.findall(r"\[\[#", ln)) for ln in md.split("\n") if ln.startswith("> ")),
        "anchors": len(re.findall(r"\^[a-z]+-\d+\s*$", md, re.M)),
        "eq_anchors": len(re.findall(r"\^eq-\d+", md)),
        "orphan_anchors": len(set(re.findall(r"(?m)\^([A-Za-z][\w-]*)\s*$", md))
                               - set(re.findall(r"\[\[#\^([^\]|]+)", md))),
    }
    return md, stats


# --------------------------------------------------------------------------

def dead_links(md: str) -> set[str]:
    """Cross-reference targets that no block id in the same file defines."""
    anchors = set(re.findall(r"(?m)\^([A-Za-z][\w-]*)\s*$", md))
    wants = set(re.findall(r"\[\[#\^([^\]|]+)", md))
    return wants - anchors


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("middle_json", type=Path)
    ap.add_argument("pdf", type=Path)
    ap.add_argument("-o", "--out", type=Path, required=True)
    ap.add_argument("--blocks", type=Path, default=None,
                    help="read blocks.jsonl/meta.json from here and write the note into "
                         "--out; lets the note live beside its source (Markdown input) "
                         "while work files stay elsewhere")
    ap.add_argument("--block-anchors", action="store_true",
                    help="also give every block a ^b-xxxxxx id (noisy; off by default)")
    ap.add_argument("--outline", action="store_true",
                    help="emit a redundant Outline section (Obsidian already has an outline pane)")
    ap.add_argument("--no-xref", action="store_true",
                    help="do not build citation / cross-reference wikilinks")
    ap.add_argument("--zh-style", choices=["callout-open", "callout-folded", "quote"],
                    default="callout-open",
                    help="how the Chinese translation is presented (default: foldable callout, open)")
    ap.add_argument("--note-name", default=None,
                    help="output file name (default <stem>.md)")
    ap.add_argument("--render-only", action="store_true",
                    help="reuse an existing blocks.jsonl (skip normalize)")
    args = ap.parse_args(argv)

    pdf = args.pdf.resolve()
    stem = pdf.stem
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    blocks_path = args.blocks or (out / "blocks.jsonl")
    meta_path = blocks_path.parent / "meta.json"

    if args.render_only:
        blocks = [json.loads(l) for l in blocks_path.read_text(encoding="utf-8").splitlines() if l.strip()]
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    else:
        blocks, meta = normalize(args.middle_json, pdf, out, stem)
        blocks_path.write_text(
            "\n".join(json.dumps(b, ensure_ascii=False) for b in blocks) + "\n",
            encoding="utf-8")

        pymupdf = _pymupdf()
        if pymupdf is None:
            meta["outline"] = []
            meta["warnings"].append("PyMuPDF not installed: heading levels derived from "
                                    "section numbering only, page count unavailable")
        else:
            d = pymupdf.open(str(pdf))
            meta["outline"] = [[lvl, title, page] for lvl, title, page in d.get_toc(simple=True)]
            d.close()

        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")

        # copy assets
        src_root = args.middle_json.parent
        for rec in blocks:
            src = rec.pop("asset_src", None)
            if not src:
                continue
            dst = out / rec["asset"]
            dst.parent.mkdir(parents=True, exist_ok=True)
            s = src_root / src
            if s.exists():
                shutil.copy2(s, dst)
            else:
                rec["flags"].append("missing_asset")
                meta["warnings"].append(f"missing asset {src}")

    md, xstats = render(blocks, meta, out, stem,
                        block_anchors=args.block_anchors,
                        outline_nav=args.outline,
                        xref=(not args.no_xref) and bool(meta.get("xref", True)),
                        zh_style=args.zh_style)
    note_name = args.note_name or f"{stem}.md"
    md_path = out / note_name
    md_path.write_text(md, encoding="utf-8")

    counts: dict[str, int] = {}
    for b in blocks:
        counts[b["type"]] = counts.get(b["type"], 0) + 1
    print(f"blocks : {len(blocks)}  {counts}")
    print(f"md     : {md_path}  ({len(md)} chars)")
    print(f"xref   : citations={xstats['citation_links']} items={xstats['item_links']} "
          f"sections={xstats['section_links']} anchors={xstats['anchors']} "
          f"orphans={xstats['orphan_anchors']} zh_links={xstats['zh_links']}")
    dead = dead_links(md)
    if dead:
        print(f"DEAD LINKS ({len(dead)}): {sorted(dead)[:8]}")
    if meta.get("titles_not_in_outline"):
        print(f"titles not matched to outline: {meta['titles_not_in_outline'][:8]}")
    if meta.get("warnings"):
        print(f"warnings: {len(meta['warnings'])}")
        for w in meta["warnings"][:8]:
            print("  -", w)
    return 0


if __name__ == "__main__":
    sys.exit(main())
