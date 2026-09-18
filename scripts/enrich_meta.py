#!/usr/bin/env python
"""Stage 4: enrich meta.json with bibliographic metadata.

    python enrich_meta.py out/dsh/md/meta.json [--s2-key KEY] [--mailto you@x.com]

Sources, in the order they are consulted:
  1. the PDF's own first pages  -- an explicit DOI or arXiv id beats any search
  2. arXiv API                  -- works for preprints, gives id, date, category
  3. Crossref                   -- venue, year, DOI, url, is-referenced-by-count
  4. Semantic Scholar           -- best-effort citationCount (needs a key to be
                                   reliable; unauthenticated access is throttled)

Not used: OpenAlex. Measured 2026-09: it answers 429 "Insufficient budget ...
you only have $0 remaining. Resets at midnight UTC" with Retry-After ~19600s,
i.e. a per-IP daily credit budget, which is not something a repeatable pipeline
should depend on.

Crossref's own `score` is not a similarity measure: for an unpublished paper it
returned 21.9/100 for an unrelated book chapter. Matching is therefore done here
with a title-similarity threshold, and a field is only written when a candidate
clears it.
"""
from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

UA = "bilingual-paper-notes/0.1 (https://github.com/; mailto:{mailto})"
MAILTO = "bilingual-paper-notes@example.com"
SIM_ACCEPT = 0.90
SIM_STRONG = 0.97
DOI_RE = re.compile(r"\b10\.\d{4,9}/[-._;()/:A-Za-z0-9]+")
ARXIV_RE = re.compile(r"arXiv:\s*(\d{4}\.\d{4,5})(v\d+)?")


# --------------------------------------------------------------------------
# http helpers
# --------------------------------------------------------------------------

def http_json(url: str, attempts: int = 3, timeout: int = 30):
    """GET json, with backoff. Returns (data, error)."""
    last = None
    for i in range(attempts):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA.format(mailto=MAILTO)})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r), None
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}"
            if e.code == 429:
                wait = int(e.headers.get("Retry-After") or 0)
                if wait > 300:            # not worth blocking the run
                    return None, f"429 rate-limited (Retry-After {wait}s)"
                time.sleep(min(wait or 5, 60))
            else:
                time.sleep(2 * (i + 1))
        except Exception as e:
            last = f"{type(e).__name__}: {e}"
            time.sleep(2 * (i + 1))
    return None, last


def http_text(url: str, attempts: int = 3, timeout: int = 30):
    for i in range(attempts):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA.format(mailto=MAILTO)})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", "ignore"), None
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            time.sleep(2 * (i + 1))
    return None, err


# --------------------------------------------------------------------------
# title / author matching
# --------------------------------------------------------------------------

def norm_title(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", (s or "").lower())).strip()


def sim(a: str, b: str) -> float:
    a, b = norm_title(a), norm_title(b)
    if not a or not b:
        return 0.0
    ta, tb = set(a.split()), set(b.split())
    jac = len(ta & tb) / max(1, len(ta | tb))
    return round(0.5 * jac + 0.5 * difflib.SequenceMatcher(None, a, b).ratio(), 4)


def surnames(names) -> set[str]:
    """Last word of each author string, lowercased. Works for both
    ['Yifan Shi', ...] and Crossref's [{given, family}]."""
    out = set()
    for n in names or []:
        if isinstance(n, dict):
            n = n.get("family") or n.get("name") or ""
        n = re.sub(r"[^A-Za-z\- ]", " ", str(n)).strip()
        if n:
            out.add(n.split()[-1].lower())
    return out


# --------------------------------------------------------------------------
# sources
# --------------------------------------------------------------------------

def scan_pdf_ids(pdf: Path, pages: int = 2) -> tuple[str | None, str | None, str]:
    try:
        import pymupdf
    except ImportError:
        return None, None, ""
    text = ""
    doc = pymupdf.open(str(pdf))
    for i in range(min(pages, doc.page_count)):
        text += doc[i].get_text()
    doc.close()
    doi = None
    m = DOI_RE.search(text)
    if m:
        doi = m.group(0).rstrip(".,;)")
    arx = None
    m = ARXIV_RE.search(text)
    if m:
        arx = m.group(1)
    return doi, arx, text


def arxiv_by_id(arxiv_id: str):
    xml, err = http_text("http://export.arxiv.org/api/query?id_list=" + urllib.parse.quote(arxiv_id))
    return _parse_arxiv(xml), err


def arxiv_search(title: str, rows: int = 3):
    url = ("http://export.arxiv.org/api/query?search_query=ti:%22"
           + urllib.parse.quote(title) + f"%22&max_results={rows}")
    xml, err = http_text(url)
    if err or not xml:
        return None, err
    entries = xml.split("<entry>")[1:]
    best = None
    for e in entries:
        e = "<entry>" + e
        rec = _parse_arxiv(e)
        if not rec:
            continue
        s = sim(title, rec["title"])
        if best is None or s > best[0]:
            best = (s, rec)
    return best, None


def _parse_arxiv(xml: str | None):
    if not xml or "<entry>" not in xml:
        return None
    entry = xml.split("<entry>", 1)[1]

    def tag(t):
        m = re.search(rf"<{t}[^>]*>(.*?)</{t}>", entry, re.S)
        return " ".join(m.group(1).split()) if m else None

    raw_id = tag("id") or ""
    short = raw_id.rsplit("/", 1)[-1].split("v")[0] if raw_id else None
    cat = re.search(r'<arxiv:primary_category[^>]*term="([^"]+)"', entry) \
        or re.search(r'<category[^>]*term="([^"]+)"', entry)
    return {
        "arxiv": short,
        "title": tag("title"),
        "published": (tag("published") or "")[:10] or None,
        "updated": (tag("updated") or "")[:10] or None,
        "journal_ref": tag("journal_ref"),
        "doi": tag("doi"),
        "category": cat.group(1) if cat else None,
    }


def crossref_by_doi(doi: str):
    d, err = http_json("https://api.crossref.org/works/" + urllib.parse.quote(doi))
    return (d or {}).get("message"), err


def crossref_search(title: str, authors=None, rows: int = 5, ref_year: int | None = None):
    url = ("https://api.crossref.org/works?rows=%d&query.bibliographic=%s"
           % (rows, urllib.parse.quote(title)))
    d, err = http_json(url)
    if err or not d:
        return None, err
    cands = []
    want = surnames(authors)
    # with no author list to corroborate a match, only a near-exact title counts
    threshold = SIM_ACCEPT if want else 0.97
    rejected = []
    for it in d.get("message", {}).get("items", []):
        t = (it.get("title") or [""])[0]
        s = sim(title, t)
        if s < threshold:
            continue
        got = surnames(it.get("author") or [])
        overlap = len(want & got) if (want and got) else None
        y = year_of(it)
        # a title match whose year is far from the identifier's year is a
        # different document: a derivative work, a chapter *about* the paper, ...
        # (measured: "Attention Is All You Need" matched a 2025 book chapter)
        if ref_year and y and abs(y - ref_year) > 2:
            rejected.append((t[:60], it.get("DOI"), y))
            continue
        cands.append({"sim": s, "item": it, "author_overlap": overlap})
    if rejected:
        print(f"  crossref: rejected {len(rejected)} candidates on year mismatch "
              f"vs {ref_year}: {rejected[:2]}")
    if not cands:
        return None, None
    # near-identical titles are common (a preprint and its journal version):
    # prefer a published type, then citations
    published = ("journal-article", "proceedings-article", "book-chapter")
    cands.sort(key=lambda c: (
        c["author_overlap"] is not False,
        c["item"].get("type") in published,
        c["sim"] >= SIM_STRONG,
        c["sim"],
        c["item"].get("is-referenced-by-count", 0),
    ), reverse=True)
    return cands[0], None


def s2_by_id(sid: str, api_key: str | None = None):
    url = (f"https://api.semanticscholar.org/graph/v1/paper/{urllib.parse.quote(sid)}"
           "?fields=title,year,venue,citationCount,influentialCitationCount,"
           "externalIds,publicationVenue,url")
    if api_key:
        url += "&x-api-key=" + urllib.parse.quote(api_key)
    return http_json(url, attempts=2)


# --------------------------------------------------------------------------

def year_of(item: dict):
    for key in ("issued", "published", "published-print", "published-online", "created"):
        parts = ((item.get(key) or {}).get("date-parts") or [[]])[0]
        if parts and parts[0]:
            return int(parts[0])
    return None


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("meta", type=Path)
    ap.add_argument("--pdf", type=Path, default=None,
                    help="defaults to meta['source_pdf']")
    ap.add_argument("--s2-key", default=os.environ.get("S2_API_KEY"))
    ap.add_argument("--mailto", default=None, help="contact address for the Crossref polite pool")
    args = ap.parse_args(argv)

    global MAILTO
    MAILTO = args.mailto or MAILTO

    meta = json.loads(args.meta.read_text(encoding="utf-8"))
    # a source being temporarily unavailable (429) must not silently drop what a
    # previous run already resolved
    prev = {k: meta.get(k) for k in
            ("doi", "arxiv", "venue", "year", "url", "citations", "citations_asof")}
    doc = meta.get("document", {})
    title = doc.get("title") or ""
    authors = doc.get("authors") or []
    if authors and isinstance(authors[0], str) and "," in authors[0]:
        authors = [a.strip() for a in authors[0].split(",")]

    pdf = args.pdf or (Path(meta["source_pdf"]) if meta.get("source_pdf") else None)
    sources: dict[str, str] = {}
    notes: list[str] = []

    # ---- 1. identifiers printed in the paper itself -------------------------
    doi = arxiv_id = None
    if pdf and pdf.exists():
        doi, arxiv_id, _ = scan_pdf_ids(pdf)
        if doi:
            sources["doi"] = "pdf text"
            notes.append(f"DOI found in the PDF: {doi}")
        if arxiv_id:
            sources["arxiv"] = "pdf text"
            notes.append(f"arXiv id found in the PDF: {arxiv_id}")

    # ---- 2. arXiv ---------------------------------------------------------
    arx = None
    err = None
    if arxiv_id:
        arx, err = arxiv_by_id(arxiv_id)
    else:
        best, err = arxiv_search(title)
        time.sleep(1)
        if best and best[0] >= SIM_ACCEPT:
            arx, arxiv_id = best[1], best[1]["arxiv"]
            sources["arxiv"] = f"arxiv title search (sim {best[0]})"
        else:
            err = err or (f"no title match (best sim {best[0]:.3f})" if best else "no results")
    if arx:
        arxiv_id = arx.get("arxiv") or arxiv_id
        notes.append(f"arXiv {arxiv_id} · {arx.get('published')} · {arx.get('category')}")
        if arx.get("title"):
            out_title = arx["title"]
            if title and sim(title, out_title) < 0.98:
                notes.append(f"WARNING: PDF title and arXiv title differ:\n"
                             f"    pdf    {title!r}\n    arXiv  {out_title!r}")
            meta["title_resolved"] = out_title
    else:
        notes.append(f"arXiv: {err or 'not found'}")

    # ---- 3. Crossref ------------------------------------------------------
    cr = None
    if doi:
        cr, err = crossref_by_doi(doi)
    if not cr:
        ref_year = int((arx or {}).get("published", "0000")[:4]) or None if arx else None
        cand, err = crossref_search(title, authors, ref_year=ref_year)
        time.sleep(1)
        if cand:
            cr = cand["item"]
            if not doi:
                doi = cr.get("DOI")
                sources["doi"] = f"crossref title search (sim {cand['sim']})"
            notes.append(f"Crossref match sim={cand['sim']} "
                         f"authors_overlap={cand['author_overlap']}")
            if cand["author_overlap"] is False:
                notes.append("WARNING: no author surname overlap -- metadata may be another paper")
        else:
            notes.append("Crossref: no candidate above the similarity threshold "
                         f"(this is the correct outcome for an unregistered paper)")
    elif doi and "pdf text" not in sources.get("doi", ""):
        sources["doi"] = "crossref"
        notes.append(f"Crossref resolved DOI {doi}")

    # ---- 4. Semantic Scholar (best effort) --------------------------------
    s2 = None
    sid = None
    if arxiv_id:
        sid = f"arXiv:{arxiv_id}"
    elif doi:
        sid = f"DOI:{doi}"
    if sid:
        s2, err = s2_by_id(sid, args.s2_key)
        if not s2:
            notes.append(f"Semantic Scholar: {err} "
                         + ("" if args.s2_key else "(no key: shared pool is throttled; "
                                                  "set S2_API_KEY for a reliable citation count)"))

    # ---- assemble --------------------------------------------------------
    out = {}
    if doi:
        out["doi"] = doi
    if arxiv_id:
        out["arxiv"] = arxiv_id
    if cr:
        container = cr.get("container-title") or []
        if isinstance(container, str):
            container = [container]
        out["venue"] = container[0] if container else None
        out["year"] = year_of(cr)
        out["url"] = cr.get("URL")
        n = cr.get("is-referenced-by-count")
        if n is not None:
            out["citations"] = n
            out["citations_asof"] = time.strftime("%Y-%m-%d")
            sources["citations"] = "crossref (is-referenced-by-count)"
    if arx:
        out.setdefault("year", int((arx.get("published") or "0000")[:4]) or None)
        if not out.get("venue"):
            out["venue"] = "arXiv preprint" + (f" ({arx['category']})" if arx.get("category") else "")
            sources["venue"] = "arxiv"
        out.setdefault("url", f"https://arxiv.org/abs/{arxiv_id}" if arxiv_id else None)
    if s2:
        if s2.get("citationCount") is not None:
            out["citations"] = s2["citationCount"]
            out["citations_asof"] = time.strftime("%Y-%m-%d")
            sources["citations"] = "semantic scholar"
        if s2.get("venue"):
            out.setdefault("venue", s2["venue"])
        out.setdefault("year", s2.get("year"))
        out.setdefault("url", s2.get("url"))

    for k, v in prev.items():
        if v not in (None, "", []) and k not in out:
            out[k] = v
            sources.setdefault(k, "kept from a previous run")
    out = {k: v for k, v in out.items() if v not in (None, "", [])}
    meta.update(out)
    meta["meta_sources"] = sources
    meta["meta_notes"] = notes
    if sources.get("doi", "").startswith("crossref title search") and \
            any(n.startswith("WARNING") for n in notes):
        meta["meta_confidence"] = "low"
    elif out.get("doi") or out.get("arxiv"):
        meta["meta_confidence"] = "high"
    elif out:
        meta["meta_confidence"] = "medium"
    else:
        meta["meta_confidence"] = "none"
    args.meta.write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"title : {title}")
    print(f"wrote : {json.dumps(out, ensure_ascii=False)}")
    for k, v in sources.items():
        print(f"  {k:10s} <- {v}")
    print("notes:")
    for n in notes:
        print("  -", n)
    return 0


if __name__ == "__main__":
    sys.exit(main())
