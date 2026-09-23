#!/usr/bin/env python
"""Stage 4: identify the document and fill in its Zotero item.

    python enrich_meta.py out/<stem>/md/meta.json [--pdf paper.pdf]
                           [--zotero-key KEY | --no-zotero] [--zotero-library users/123]

The metadata in meta.json is a Zotero item: its field names and the set of
fields it may carry come from Zotero's own schema (scripts/data/zotero-schema.json).
Nothing outside that model is written, and the final item is validated against
it, so the note's properties cannot drift.

Sources, in the order they are consulted:

  1. a Zotero library         -- when a key is configured, and the paper is in it
  2. the PDF's own first pages -- an explicit DOI, arXiv id or ISBN beats any search
  3. arXiv                     -- id, date, abstract, repository
  4. Crossref                  -- venue, volume/issue/pages, publisher, ISBN, ISSN

A library record wins outright when its identifier matches exactly, because it is
the one source that does not have to guess. Everything else fills fields that are
still empty, so a later run never erases what an earlier one resolved.

Nothing is guessed into existence: a field with no source stays empty. The
reasoning behind every decision is printed rather than stored, which is why
meta.json holds the item and no provenance keys.
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

from zotero_schema import (CROSSREF_TYPES, allowed_fields, creator_types,
                          load as load_schema, validate_item)

UA = "bilingual-paper-notes/0.1 (https://github.com/; mailto:{mailto})"
MAILTO = "bilingual-paper-notes@example.com"
SIM_ACCEPT = 0.90
SIM_STRONG = 0.97
DOI_RE = re.compile(r"\b10\.\d{4,9}/[-._;()/:A-Za-z0-9]+")
ARXIV_RE = re.compile(r"arXiv:\s*(\d{4}\.\d{4,5})(v\d+)?")
ISBN_RE = re.compile(r"\b(?:ISBN[- ]?(?:13|10)?[:\s]*)?(97[89][-\s]?(?:\d[-\s]?){9}\d|\d{9}[\dXx])\b")


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
    last = None
    for i in range(attempts):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA.format(mailto=MAILTO)})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", "ignore"), None
        except Exception as e:
            last = f"{type(e).__name__}: {e}"
            time.sleep(2 * (i + 1))
    return None, last


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


def surnames(names) -> set:
    """Last word of each author string, lowercased. Works for both
    ['Yifan Shi', ...] and Zotero's or Crossref's [{given, family}]."""
    out = set()
    for n in names or []:
        if isinstance(n, dict):
            n = n.get("family") or n.get("lastName") or n.get("name") or ""
        n = re.sub(r"[^A-Za-z\- ]", " ", str(n)).strip()
        if n:
            out.add(n.split()[-1].lower())
    return out


def split_name(name: str) -> tuple[str, str]:
    """First/given and last/family halves of a name, as Zotero stores them."""
    parts = re.sub(r"\s+", " ", (name or "").strip()).split()
    if not parts:
        return "", ""
    if len(parts) == 1:
        return "", parts[0]
    return " ".join(parts[:-1]), parts[-1]


# --------------------------------------------------------------------------
# sources
# --------------------------------------------------------------------------

def scan_pdf_ids(pdf: Path, pages: int = 2) -> dict:
    """Identifiers printed on the document itself: the strongest signal there is."""
    try:
        import pymupdf
    except ImportError:
        return {}
    text = ""
    doc = pymupdf.open(str(pdf))
    for i in range(min(pages, doc.page_count)):
        text += doc[i].get_text()
    doc.close()
    found = {}
    m = DOI_RE.search(text)
    if m:
        found["doi"] = m.group(0).rstrip(".,;)")
    m = ARXIV_RE.search(text)
    if m:
        found["arxiv"] = m.group(1)
    m = ISBN_RE.search(text)
    if m:
        found["isbn"] = re.sub(r"[^0-9Xx]", "", m.group(1)).upper()
    return found


def from_arxiv(rec: dict, arxiv_id: str) -> dict:
    """An arXiv entry as Zotero fields. A preprint stays a preprint: that is what
    a Zotero library would hold, and it is what tells a reader this is not the
    published version."""
    out = {"itemType": "preprint", "repository": "arXiv", "archiveID": arxiv_id}
    if rec.get("title"):
        out["title"] = rec["title"]
    if rec.get("published"):
        out["date"] = rec["published"]
    if rec.get("summary"):
        out["abstractNote"] = rec["summary"]
    if rec.get("doi"):
        out["DOI"] = rec["doi"]
    url = rec.get("url") or (f"https://arxiv.org/abs/{arxiv_id}" if arxiv_id else None)
    if url:
        out["url"] = url
    creators = []
    for name in rec.get("authors") or []:
        given, family = split_name(name)
        creators.append({"creatorType": "author", "firstName": given, "lastName": family})
    if creators:
        out["creators"] = creators
    return out


def container_field(item_type: str) -> str | None:
    """Where the container a work sits in belongs, per Zotero's model."""
    return {"journalArticle": "publicationTitle",
            "conferencePaper": "proceedingsTitle",
            "bookSection": "bookTitle",
            "magazineArticle": "publicationTitle",
            "newspaperArticle": "publicationTitle",
            "encyclopediaArticle": "encyclopediaTitle",
            "dictionaryEntry": "dictionaryTitle"}.get(item_type)


def from_crossref(cr: dict) -> dict:
    """A Crossref record as Zotero fields."""
    item_type = CROSSREF_TYPES.get(cr.get("type") or "")
    out = {}
    if item_type:
        out["itemType"] = item_type
    title = (cr.get("title") or [""])[0]
    if title:
        out["title"] = " ".join(title.split())
    field = container_field(item_type or "")
    container = cr.get("container-title") or []
    if isinstance(container, str):
        container = [container]
    if field and container:
        out[field] = container[0]
    event = cr.get("event") or {}
    if item_type == "conferencePaper":
        if event.get("name"):
            out["conferenceName"] = event["name"]
        if event.get("location"):
            out["eventPlace"] = event["location"]
    for src_key, zotero_field in (("volume", "volume"), ("issue", "issue"), ("page", "pages"),
                                  ("publisher", "publisher"), ("language", "language"),
                                  ("edition-number", "edition"), ("DOI", "DOI"), ("URL", "url")):
        value = cr.get(src_key)
        if value:
            out[zotero_field] = str(value)
    if cr.get("ISBN"):
        out["ISBN"] = " ".join(cr["ISBN"])
    if cr.get("ISSN"):
        out["ISSN"] = " ".join(cr["ISSN"])
    if item_type == "journalArticle" and (cr.get("short-container-title") or []):
        out["journalAbbreviation"] = cr["short-container-title"][0]
    if cr.get("abstract"):
        text = re.sub(r"<[^>]+>", " ", cr["abstract"])
        text = re.sub(r"\s+", " ", text).strip()
        if text:
            out["abstractNote"] = text
    date = date_of(cr)
    if date:
        out["date"] = date
    creators = []
    for role, key in (("author", "author"), ("editor", "editor"), ("translator", "translator")):
        for person in cr.get(key) or []:
            family = person.get("family") or person.get("name") or ""
            given = person.get("given") or ""
            if not family and not given:
                continue
            creators.append({"creatorType": role, "firstName": given, "lastName": family}
                            if family else {"creatorType": role, "name": given})
    if creators:
        out["creators"] = creators
    return out


def date_of(item: dict) -> str | None:
    """Crossref's date-parts as the date string Zotero stores."""
    for key in ("issued", "published", "published-print", "published-online", "created"):
        parts = ((item.get(key) or {}).get("date-parts") or [[]])[0]
        if parts and parts[0]:
            return "-".join(f"{p:02d}" if i else str(p) for i, p in enumerate(parts[:3]))
    return None


def year_of(item: dict) -> int | None:
    date = date_of(item)
    return int(date[:4]) if date and date[:4].isdigit() else None


def arxiv_by_id(arxiv_id: str):
    xml, err = http_text("http://export.arxiv.org/api/query?id_list=" + urllib.parse.quote(arxiv_id))
    return _parse_arxiv(xml), err


def arxiv_search(title: str, rows: int = 3):
    url = ("http://export.arxiv.org/api/query?search_query=ti:%22"
           + urllib.parse.quote(title) + f"%22&max_results={rows}")
    xml, err = http_text(url)
    if err or not xml:
        return None, err
    best = None
    for entry in xml.split("<entry>")[1:]:
        rec = _parse_arxiv("<entry>" + entry)
        if not rec:
            continue
        score = sim(title, rec["title"] or "")
        if best is None or score > best[0]:
            best = (score, rec)
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
    authors = [m.group(1) for m in re.finditer(r"<name>(.*?)</name>", entry, re.S)]
    return {
        "arxiv": short,
        "title": tag("title"),
        "published": (tag("published") or "")[:10] or None,
        "url": raw_id or None,
        "doi": tag("doi"),
        "summary": tag("summary"),
        "journal_ref": tag("journal_ref"),
        "authors": [" ".join(a.split()) for a in authors],
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
    want = surnames(authors)
    # with no author list to corroborate a match, only a near-exact title counts
    threshold = SIM_ACCEPT if want else 0.97
    cands, rejected = [], []
    for it in d.get("message", {}).get("items", []):
        t = (it.get("title") or [""])[0]
        score = sim(title, t)
        if score < threshold:
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
        cands.append({"sim": score, "item": it, "author_overlap": overlap})
    if rejected:
        print(f"  crossref: rejected {len(rejected)} candidates on year mismatch "
              f"vs {ref_year}: {rejected[:2]}")
    if not cands:
        return None, None
    published = ("journal-article", "proceedings-article", "book-chapter")
    cands.sort(key=lambda c: (c["author_overlap"] is not False,
                              c["item"].get("type") in published,
                              c["sim"] >= SIM_STRONG,
                              c["sim"], c["item"].get("is-referenced-by-count", 0)),
               reverse=True)
    return cands[0], None


# --------------------------------------------------------------------------
# assembling the item
# --------------------------------------------------------------------------

def keep_affiliations(creators: list[dict], previous: list[dict]) -> list[dict]:
    """Carry the title block's affiliations onto the source's author list.

    The library or Crossref knows the authors; only the PDF knows which
    institution each one is at. They are joined by surname, and only when that
    is unambiguous: an author whose name does not match keeps no affiliation,
    rather than inheriting someone else's.
    """
    if not previous:
        return creators
    by_last = {}
    for c in previous:
        name = c.get("name") or c.get("lastName") or ""
        last = str(name).split()[-1].lower()
        if "(" in name:                      # "A B (Institute, a@b.c)"
            by_last[last] = name
    if not by_last:
        return creators
    out = []
    for c in creators:
        last = str(c.get("lastName") or c.get("name") or "").split()[-1].lower()
        if last in by_last:
            out.append({"creatorType": c.get("creatorType", "author"), "name": by_last[last]})
        else:
            out.append(c)
    return out


def fill_empty(item: dict, other: dict, label: str, notes: list[str],
               schema: dict | None = None) -> None:
    """Add the fields `other` has and `item` does not. Never overwrites.

    A field the item's own type does not allow is dropped, not merged: taking
    `bookTitle` or `archiveID` from a source that described a different kind of
    item would put the metadata outside Zotero's model, which is the one thing
    this stage must not do.
    """
    schema = schema or load_schema()
    item_type = item.get("itemType") or "journalArticle"
    allowed = allowed_fields(schema, item_type)
    roles = creator_types(schema, item_type)
    added, dropped = [], []
    for field, value in other.items():
        if value in (None, "", []):
            continue
        if field == "creators":
            if item.get("creators"):
                continue
            keep = [c for c in value if c.get("creatorType") in roles]
            if len(keep) != len(value):
                dropped.append(f"creators ({len(value) - len(keep)} with a role "
                               f"{item_type} does not allow)")
            if keep:
                item["creators"] = keep
                added.append(f"creators x{len(keep)}")
            continue
        if field == "itemType":
            continue                        # handled by the caller, which knows the evidence
        if field not in allowed:
            dropped.append(field)
            continue
        if item.get(field) in (None, "", []):
            item[field] = value
            added.append(field)
    if added:
        notes.append(f"{label}: filled {', '.join(added)}")
    if dropped:
        notes.append(f"{label}: {len(dropped)} field(s) not allowed on {item_type}, dropped "
                     f"({', '.join(dropped[:6])})")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("meta", type=Path)
    ap.add_argument("--pdf", type=Path, default=None,
                    help="the source PDF, for the identifiers printed on it")
    ap.add_argument("--no-zotero", action="store_true",
                    help="do not consult a Zotero library at all")
    ap.add_argument("--zotero-key", default=None, help="Zotero API key (env ZOTERO_API_KEY)")
    ap.add_argument("--zotero-library", default=None, help='e.g. "users/123" or "groups/456"')
    ap.add_argument("--mailto", default=None, help="contact address for the Crossref polite pool")
    args = ap.parse_args(argv)

    global MAILTO
    MAILTO = args.mailto or MAILTO

    meta = json.loads(args.meta.read_text(encoding="utf-8"))
    schema = load_schema()
    item = dict(meta.get("item") or {})
    parsed_creators = item.get("creators") or []
    notes: list[str] = []

    # ---- 1. a Zotero library --------------------------------------------
    zotero = None
    try:
        import zotero as zclient
    except ImportError:
        zclient = None
    if not args.no_zotero and zclient is not None:
        conf = zclient.resolve(key=args.zotero_key, library=args.zotero_library)
        if conf["api_key"]:
            pdf = args.pdf
            ids = scan_pdf_ids(pdf) if pdf and pdf.exists() else {}
            client = zclient.Client(conf["api_key"], conf["library"])
            record, err = client.by_identifier(doi=ids.get("doi"), isbn=ids.get("isbn"),
                                               arxiv=ids.get("arxiv"),
                                               title=item.get("title"))
            if record:
                data = {k: v for k, v in (record.get("data") or {}).items()
                        if k not in ("key", "version", "dateAdded", "dateModified",
                                     "collections", "relations", "tags")}
                notes.append(f"zotero: exact match on {record.get('data', {}).get('itemType')} "
                             f"(key {record.get('key')})")
                zotero = data
            else:
                notes.append(f"zotero: {err}")
        else:
            notes.append("zotero: no API key configured")
    elif args.no_zotero:
        notes.append("zotero: skipped (--no-zotero)")

    if zotero:
        item.update(zotero)
        item["creators"] = keep_affiliations(item.get("creators") or [], parsed_creators)

    # ---- 2. identifiers printed on the PDF ------------------------------
    pdf = args.pdf
    ids = scan_pdf_ids(pdf) if pdf and pdf.exists() else {}
    if not ids and (meta.get("source_kind") or "pdf") == "pdf" and not pdf:
        notes.append("no --pdf given: identifiers printed on the document cannot be read")
    for kind, value in ids.items():
        if kind == "doi" and not item.get("DOI"):
            item["DOI"] = value
            notes.append(f"DOI found in the PDF: {value}")
        if kind == "arxiv":
            item.setdefault("_arxiv_hint", value)
            notes.append(f"arXiv id found in the PDF: {value}")
        if kind == "isbn" and not item.get("ISBN"):
            item["ISBN"] = value
            notes.append(f"ISBN found in the PDF: {value}")

    # ---- 3. arXiv -------------------------------------------------------
    arxiv_id = item.pop("_arxiv_hint", None)
    arx = None
    if not zotero:
        if arxiv_id:
            arx, err = arxiv_by_id(arxiv_id)
        else:
            best, err = arxiv_search(item.get("title") or "")
            time.sleep(1)
            if best and best[0] >= SIM_ACCEPT:
                arx = best[1]
                arxiv_id = arx.get("arxiv")
                notes.append(f"arxiv: title search matched sim {best[0]}")
            else:
                notes.append("arxiv: " + (err or (f"no title match (best sim {best[0]:.3f})"
                                                  if best else "no results")))
        if arx:
            if item.get("title") and arx.get("title") and sim(item["title"], arx["title"]) < 0.98:
                notes.append(f"arxiv title differs from the document's: {arx['title']!r}")
            # a work that is only on arXiv is a preprint in Zotero, and that is the
            # one case where arXiv is allowed to decide the type: no DOI and no
            # published venue means there is no published version on record
            if not item.get("DOI") and not item.get("publicationTitle") \
                    and item.get("itemType") == "journalArticle":
                item["itemType"] = "preprint"
                notes.append("arxiv: itemType is preprint (no DOI and no venue on record)")
            fill_empty(item, from_arxiv(arx, arxiv_id or arx.get("arxiv") or ""),
                       "arxiv", notes, schema)
            item["creators"] = keep_affiliations(item.get("creators") or [], parsed_creators)

    # ---- 4. Crossref ----------------------------------------------------
    if not zotero:
        cr = None
        by_doi = bool(item.get("DOI"))
        if by_doi:
            cr, err = crossref_by_doi(item["DOI"])
            if not cr and err:
                notes.append(f"crossref: {err}")
        if not cr:
            ref_year = int((arx or {}).get("published", "0000")[:4]) or None if arx else None
            cand, err = crossref_search(item.get("title") or "", item.get("creators"), ref_year=ref_year)
            time.sleep(1)
            if cand:
                cr = cand["item"]
                notes.append(f"crossref: title match sim={cand['sim']} "
                             f"authors_overlap={cand['author_overlap']}")
                if cand["author_overlap"] is False:
                    notes.append("crossref WARNING: no author surname overlap -- "
                                 "this may be a different document")
            else:
                notes.append("crossref: no candidate above the similarity threshold")
        if cr:
            mapped = from_crossref(cr)
            # only a DOI lookup is authoritative enough to overrule the item type;
            # a title match can land on a chapter *about* the same work
            if by_doi and mapped.get("itemType") and mapped["itemType"] != item.get("itemType"):
                notes.append(f"crossref: itemType {item.get('itemType')} -> {mapped['itemType']} "
                             f"(looked up by DOI)")
                item["itemType"] = mapped["itemType"]
            elif mapped.get("itemType") and mapped["itemType"] != item.get("itemType"):
                notes.append(f"crossref: kept itemType {item.get('itemType')} "
                             f"(a title match proposed {mapped['itemType']})")
            fill_empty(item, mapped, "crossref", notes, schema)
            item["creators"] = keep_affiliations(item.get("creators") or [], parsed_creators)

    # ---- assemble -------------------------------------------------------
    item.setdefault("itemType", "journalArticle")
    item["accessDate"] = time.strftime("%Y-%m-%d")
    item = {k: v for k, v in item.items() if v not in (None, "", []) and not k.startswith("_")}
    meta["item"] = item
    args.meta.write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")

    problems = validate_item(schema, item)
    print(f"itemType: {item.get('itemType')}")
    print(f"title   : {item.get('title')}")
    if item.get("creators"):
        print(f"creators: {len(item['creators'])} " +
              ", ".join((c.get('name') or f"{c.get('firstName','')} {c.get('lastName','')}").strip()
                        for c in item["creators"][:4]) +
              (" …" if len(item["creators"]) > 4 else ""))
    print("fields  : " + ", ".join(sorted(k for k in item if k != "itemType")))
    for n in notes:
        print("  -", n)
    if problems:
        print("SCHEMA: the item is outside Zotero's model:")
        for p in problems:
            print("  !", p)
        return 1
    print("schema  : ok (every field belongs to this item type)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
