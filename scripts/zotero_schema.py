#!/usr/bin/env python
"""Zotero's data model, as a compact snapshot of the official schema.

A note's metadata is meant to *be* a Zotero item, so the field names and the set
of fields an item type allows come from Zotero rather than from a list kept here.
`data/zotero-schema.json` is that model, derived from the live schema at
https://api.zotero.org/schema.

    python zotero_schema.py --check              # snapshot is readable and sane
    python zotero_schema.py --show journalArticle
    python zotero_schema.py --refresh            # re-derive it (needs network)

The full schema is ~500 KB and mostly describes item types this pipeline never
touches (artwork, patent, podcast, ...), so the snapshot keeps only what is used:
per-type fields and creator roles, plus the CSL mappings Zotero publishes.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
SNAPSHOT = HERE / "data" / "zotero-schema.json"
SCHEMA_URL = "https://api.zotero.org/schema"

# Keys Zotero stores on an item outside its type's field list: the object
# envelope, plus structural keys that belong to attachment and note items.
# Everything else must be a field of the item's type.
ENVELOPE_KEYS = {
    "key", "version", "itemType", "creators", "tags", "collections", "relations",
    "dateAdded", "dateModified",
    "note", "linkMode", "filename", "contentType", "charset", "md5", "mtime",
    "parentItem", "annotationType", "annotationText", "annotationComment",
    "annotationColor", "annotationPageLabel", "annotationSortIndex",
    "annotationPosition", "annotationIsExternal", "snapshot", "numChildren",
}

# The types this pipeline can produce. A paper is an article or a preprint; the
# rest are what a book split into chapters, a conference paper or a report is.
PAPER_TYPES = ("journalArticle", "preprint", "conferencePaper")
OTHER_TYPES = ("book", "bookSection", "thesis", "report", "manuscript", "document")

# Crossref's type -> Zotero's item type. Anything absent is not mapped, and the
# caller leaves itemType unset rather than guessing one.
CROSSREF_TYPES = {
    "journal-article": "journalArticle",
    "proceedings-article": "conferencePaper",
    "book-chapter": "bookSection",
    "book-part": "bookSection",
    "book": "book",
    "monograph": "book",
    "edited-book": "book",
    "reference-book": "book",
    "posted-content": "preprint",
    "dissertation": "thesis",
    "report": "report",
    "standard": "report",
    "dataset": "dataset",
    "peer-review": None,
    "component": None,
}


def derive(live: dict) -> dict:
    """Reduce the live schema to what this pipeline reads."""
    types = {}
    for t in live["itemTypes"]:
        types[t["itemType"]] = {
            "fields": [f["field"] for f in t["fields"]],
            "creators": [c["creatorType"] for c in t.get("creatorTypes", [])],
        }
    # Zotero publishes the CSL mapping with CSL names as keys; invert both maps
    # so a lookup goes Zotero -> CSL, which is the direction this pipeline needs.
    csl_types = {}
    for csl_name, zotero_types in (live.get("csl", {}).get("types") or {}).items():
        for zt in zotero_types or []:
            csl_types.setdefault(zt, csl_name)
    csl_creators = {}
    for zotero_role, csl_name in (live.get("csl", {}).get("names") or {}).items():
        csl_creators.setdefault(zotero_role, csl_name)
    return {
        "source": SCHEMA_URL,
        "version": live.get("version"),
        "derived": "itemTypes -> fields/creators; csl -> type and creator names (inverted)",
        "itemTypes": types,
        "cslTypes": csl_types,
        "cslCreators": csl_creators,
    }


def fetch(url: str = SCHEMA_URL, timeout: int = 30) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "bilingual-paper-notes/0.1"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def load(path: Path | None = None) -> dict:
    with (path or SNAPSHOT).open(encoding="utf-8") as fh:
        return json.load(fh)


def item_types(schema: dict) -> dict:
    return schema["itemTypes"]


def known(schema: dict, item_type: str) -> bool:
    return item_type in schema["itemTypes"]


def allowed_fields(schema: dict, item_type: str) -> set:
    """The fields Zotero allows on this item type. An unknown type allows nothing.

    This is a set, so it answers "may this field be here", not "in what order" —
    for Zotero's own field order, read the snapshot's list.
    """
    return set(schema["itemTypes"].get(item_type, {}).get("fields", []))


def creator_types(schema: dict, item_type: str) -> set:
    return set(schema["itemTypes"].get(item_type, {}).get("creators", []))


def csl_type(schema: dict, item_type: str) -> str | None:
    return schema["cslTypes"].get(item_type)


def csl_creator(schema: dict, role: str) -> str | None:
    return schema["cslCreators"].get(role)


def validate_item(schema: dict, item: dict) -> list[str]:
    """Problems that stop `item` from being a valid Zotero item.

    Empty means the item is inside Zotero's model. A non-empty list means the
    metadata drifted out of it, which is the one thing this pipeline must not do.
    """
    problems: list[str] = []
    item_type = item.get("itemType")
    if not item_type:
        return ["itemType is missing"]
    if not known(schema, item_type):
        return [f"unknown itemType {item_type!r}"]

    allowed = allowed_fields(schema, item_type)
    roles = creator_types(schema, item_type)
    for key in item:
        if key in ENVELOPE_KEYS:
            continue
        if key not in allowed:
            problems.append(f"{key!r} is not a field of {item_type}")
    for creator in item.get("creators") or []:
        role = creator.get("creatorType")
        if role not in roles:
            problems.append(f"creator role {role!r} is not allowed on {item_type}")
        has_split = "firstName" in creator or "lastName" in creator
        if not has_split and not creator.get("name"):
            problems.append("a creator has neither firstName/lastName nor name")
        if has_split and creator.get("name"):
            problems.append("a creator has both split names and a single-field name")
    return problems


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true", help="validate the bundled snapshot")
    ap.add_argument("--refresh", action="store_true", help=f"re-derive from {SCHEMA_URL}")
    ap.add_argument("--show", metavar="ITEMTYPE", help="print one item type's model")
    ap.add_argument("--path", type=Path, default=None, help="use another snapshot file")
    args = ap.parse_args(argv)

    if args.refresh:
        live = fetch()
        snapshot = derive(live)
        (args.path or SNAPSHOT).parent.mkdir(parents=True, exist_ok=True)
        (args.path or SNAPSHOT).write_text(
            json.dumps(snapshot, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        print(f"wrote {(args.path or SNAPSHOT)} from schema version {snapshot['version']}")
        return 0

    schema = load(args.path)

    if args.show:
        name = args.show
        if not known(schema, name):
            print(f"unknown item type {name!r}", file=sys.stderr)
            return 2
        t = schema["itemTypes"][name]
        print(f"{name}  (CSL type: {csl_type(schema, name) or 'unmapped'})")
        print("  fields  :", ", ".join(t["fields"]))
        print("  creators:", ", ".join(t["creators"]))
        return 0

    if args.check:
        problems = []
        if not schema.get("itemTypes"):
            problems.append("no item types in the snapshot")
        if not schema.get("version"):
            problems.append("no schema version recorded")
        for name in PAPER_TYPES + OTHER_TYPES:
            if not known(schema, name):
                problems.append(f"{name} missing from the snapshot")
                continue
            if not schema["itemTypes"][name]["fields"]:
                problems.append(f"{name} has no fields")
            if not schema["itemTypes"][name]["creators"]:
                problems.append(f"{name} has no creator roles")
        # Zotero's own child-only types (note, annotation, attachment) carry no
        # fields or creators at all, so emptiness there is not a defect.
        # an item assembled by us must pass validation
        good = {"itemType": "journalArticle", "title": "T", "DOI": "10.x/y",
                "creators": [{"creatorType": "author", "firstName": "A", "lastName": "B"}]}
        if validate_item(schema, good):
            problems.append(f"a valid item was rejected: {validate_item(schema, good)}")
        for bad, why in (({"title": "T"}, "no itemType"),
                         ({"itemType": "nope", "title": "T"}, "unknown type"),
                         ({"itemType": "journalArticle", "nonsense": 1}, "stray field"),
                         ({"itemType": "journalArticle",
                           "creators": [{"creatorType": "composer", "lastName": "B"}]}, "bad role")):
            if not validate_item(schema, bad):
                problems.append(f"an invalid item was accepted: {why}")
        print(f"schema version {schema['version']} · {len(schema['itemTypes'])} item types "
              f"· {len(set(f for t in schema['itemTypes'].values() for f in t['fields']))} field names")
        for p in problems:
            print("  PROBLEM:", p)
        print("check:", "ok" if not problems else f"{len(problems)} problem(s)")
        return 0 if not problems else 1

    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
