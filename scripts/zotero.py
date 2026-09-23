#!/usr/bin/env python
"""Read bibliographic metadata from a Zotero library, through the Zotero Web API.

A Zotero library is the authoritative source for the papers you already keep: it
needs no fuzzy title matching, so it cannot pick the wrong book chapter the way a
Crossref title search can. This module is deliberately **read-only** — it only
issues GET requests and has no code path that writes to a library.

The key comes from, in order: an explicit argument, `ZOTERO_API_KEY`, then the
`zotero` section of `.bilingual-paper-notes.json` (found in the working directory
or a parent). The library id is derived from the key, so a key alone is enough;
say `"library": "groups/12345"` to read a group library instead.

    python zotero.py --guide                       # how to get and place a key
    python zotero.py --whoami                      # which key, which library
    python zotero.py --find-doi 10.1103/PhysRevLett.116.061102
    python zotero.py --find-title "Attention Is All You Need"
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from zotero_schema import OTHER_TYPES, PAPER_TYPES

CONFIG_NAME = ".bilingual-paper-notes.json"
DEFAULT_BASE = "https://api.zotero.org"
UA = "bilingual-paper-notes/0.1"
ENV_KEY = "ZOTERO_API_KEY"
ENV_LIBRARY = "ZOTERO_LIBRARY"

# Items that are not bibliographic records, so they never answer a lookup.
NON_RECORDS = {"attachment", "note", "annotation"}


def find_config(start: Path | None = None, levels: int = 10) -> Path | None:
    """The nearest config file at or above `start`."""
    here = (start or Path.cwd()).resolve()
    for _ in range(levels):
        candidate = here / CONFIG_NAME
        if candidate.is_file():
            return candidate
        if here.parent == here:
            break
        here = here.parent
    return None


def read_config(path: Path | None = None) -> dict:
    if path is None:
        path = find_config()
    if not path or not Path(path).is_file():
        return {}
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    zotero = data.get("zotero")
    return zotero if isinstance(zotero, dict) else {}


def resolve(start: Path | None = None, config_path: Path | None = None,
            key: str | None = None, library: str | None = None) -> dict:
    """Where the credentials come from, in the order they win."""
    section = read_config(config_path)
    out = {"api_key": None, "library": None, "source": None, "config": None}
    if key:
        out.update(api_key=key, source="argument")
    elif os.environ.get(ENV_KEY):
        out.update(api_key=os.environ[ENV_KEY].strip(), source=f"environment ({ENV_KEY})")
    elif section.get("api_key"):
        out.update(api_key=str(section["api_key"]).strip(), source="config file")
    out["library"] = library or os.environ.get(ENV_LIBRARY) or section.get("library") or None
    path = config_path if config_path is not None else find_config(start)
    out["config"] = str(path) if path else None
    return out


def guidance(reason: str = "no Zotero API key was found") -> str:
    """What to print when there is no key: where to get one and where to put it."""
    return f"""Zotero metadata source is off ({reason}).

Your own library is the most reliable source of metadata: it is authoritative,
works offline from title matching, and returns exactly the fields Zotero defines.
To turn it on:

  1. Create a key at https://www.zotero.org/settings/keys/new
     It needs read access only. Leave "Allow write access" unticked — this
     pipeline never writes to a library. Scope it to your personal library, or
     to a single group if that is where the papers live.

  2. Put it in {CONFIG_NAME}, next to where you run the pipeline
     (that file is gitignored, and so is never committed or shared):

       {{ "zotero": {{ "api_key": "PASTE_THE_KEY_HERE" }} }}

     Nothing else is needed: the library id is read from the key itself. To use a
     group library instead, add {{ "library": "groups/12345" }} beside the key.

  3. Or export it for the session instead of writing a file:
     export {ENV_KEY}=...

Without a key the pipeline reads arXiv and Crossref, which produce the same
fields but have to match by title. To skip Zotero on purpose, pass --no-zotero.
"""


def _normalize_isbn(value: str) -> str:
    return re.sub(r"[^0-9Xx]", "", value or "").upper()


def summarize(item: dict) -> dict:
    """The little of a record that matching needs, so a scan stays small."""
    d = item.get("data") or {}
    creators = []
    for c in d.get("creators") or []:
        name = c.get("lastName") or c.get("name") or ""
        if name:
            creators.append(str(name).split()[-1].lower())
    return {
        "key": item.get("key"),
        "itemType": d.get("itemType"),
        "title": d.get("title") or "",
        "date": d.get("date") or "",
        "creators": creators,
        "doi": d.get("DOI") or "",
        "isbn": [_normalize_isbn(x) for x in (d.get("ISBN") or "").split() if x],
        "archiveID": (d.get("archiveID") or "").strip().lower(),
    }


class Client:
    """Read-only Zotero Web API client.

    Every method issues a GET. There is no write path by design: this pipeline
    uses a library as a source, never as a destination.
    """

    def __init__(self, api_key: str | None = None, library: str | None = None,
                 base: str = DEFAULT_BASE, timeout: int = 30, attempts: int = 3):
        self.api_key = api_key
        self.library = library
        self.base = base.rstrip("/")
        self.timeout = timeout
        self.attempts = attempts
        self._identity: dict | None = None
        self.notes: list[str] = []

    # -- transport ---------------------------------------------------------
    def _get(self, path: str, params: dict | None = None):
        url = self.base + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        headers = {"User-Agent": UA, "Accept": "application/json"}
        if self.api_key:
            headers["Zotero-API-Key"] = self.api_key
        last = None
        for attempt in range(self.attempts):
            try:
                req = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    return json.loads(r.read().decode("utf-8", "replace")), dict(r.headers), None
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    wait = int(e.headers.get("Retry-After") or e.headers.get("Backoff") or 5)
                    last = f"429 rate-limited (Retry-After {wait}s)"
                    if wait > 60:
                        return None, {}, last
                    time.sleep(min(wait, 60))
                    continue
                if e.code == 403:
                    return None, {}, "403: the key was rejected or has no access to this library"
                if e.code == 404:
                    return None, {}, "404: library or endpoint not found"
                last = f"HTTP {e.code}"
                time.sleep(2 * (attempt + 1))
            except Exception as e:  # offline, DNS, TLS, timeout
                last = f"{type(e).__name__}: {e}"
                time.sleep(2 * (attempt + 1))
        return None, {}, last

    # -- identity ----------------------------------------------------------
    def whoami(self):
        """The key's owner and its access scope, straight from Zotero."""
        if self._identity is None:
            data, _, err = self._get("/keys/current")
            if data is None:
                self.notes.append(f"whoami: {err}")
            self._identity = data or {}
        return self._identity

    def library_path(self):
        """`users/<id>` for the key's owner, or the explicitly configured library."""
        if self.library:
            return "/" + self.library.strip("/")
        info = self.whoami()
        user_id = info.get("userID")
        if not user_id:
            return None
        return f"/users/{user_id}"

    # -- lookups -----------------------------------------------------------
    @staticmethod
    def _records(items):
        return [i for i in items or []
                if isinstance(i, dict) and (i.get("data") or {}).get("itemType") not in NON_RECORDS]

    def _search(self, query: str, limit: int = 25, qmode: str = "everything"):
        path = self.library_path()
        if not path:
            return None, "no library: " + (self.notes[-1] if self.notes else "no key")
        data, _, err = self._get(f"{path}/items/top",
                                 {"q": query, "qmode": qmode, "limit": limit, "format": "json"})
        if data is None:
            return None, err
        return self._records(data), None

    def scan(self, item_types=None, per_type_cap: int = 1000, page: int = 100):
        """Summaries of every top-level item of the wanted types.

        The API's own `q` search does not index DOI or ISBN (measured: an ISBN
        that is present in the library does not match its own query), so an
        identifier lookup cannot be a search. Paging per item type also skips
        the attachments and notes that make up most of a real library.

        Returns (summaries, error). Each summary keeps only what matching needs;
        the record itself is fetched by key once a candidate is chosen.
        """
        path = self.library_path()
        if not path:
            return None, "no library: " + (self.notes[-1] if self.notes else "no key")
        wanted = list(item_types or (PAPER_TYPES + OTHER_TYPES))
        out, err = [], None
        for item_type in wanted:
            start = 0
            while start < per_type_cap:
                data, _, e = self._get(f"{path}/items/top", {
                    "itemType": item_type, "limit": page, "start": start, "format": "json"})
                if data is None:
                    err = err or e
                    break
                if not data:
                    break
                for item in self._records(data):
                    out.append(summarize(item))
                if len(data) < page:
                    break
                start += page
        return out, err

    def fetch(self, key: str):
        """The full record behind a key."""
        path = self.library_path()
        data, _, err = self._get(f"{path}/items/{key}", {"format": "json"})
        if data is None:
            return None, err
        return data, None

    def by_identifier(self, doi=None, isbn=None, arxiv=None, title=None, item_types=None):
        """Look a record up by an identifier, or by an exact title.

        Returns (item, error). Identifiers are compared exactly, and a title only
        counts when it matches the record's own title once normalised - never on a
        similarity score. That is the whole reason to consult a library instead of
        a title search: it either knows the work or it does not.
        """
        want = []
        if doi:
            want.append(("DOI", doi.strip().lower()))
        if isbn:
            want.append(("ISBN", _normalize_isbn(isbn)))
        if arxiv:
            want.append(("arXiv", re.sub(r"^arxiv:", "", arxiv.strip(), flags=re.I).lower()))
        if not want and not title:
            return None, "no identifier given"

        summaries, err = self.scan(item_types)
        if not summaries:
            return None, err or "the library returned no items of the searched types"
        for kind, value in want:
            for s in summaries:
                if kind == "DOI" and (s.get("doi") or "").lower() == value:
                    return self.fetch(s["key"])
                if kind == "ISBN" and value and value in (s.get("isbn") or []):
                    return self.fetch(s["key"])
                if kind == "arXiv" and value and value == (s.get("archiveID") or "").lower():
                    return self.fetch(s["key"])
        if want:
            miss = f"no item in the library carries {want[0][0]} {want[0][1]}"
        else:
            miss = "no identifier was available"

        if title:
            wanted = re.sub(r"\W+", " ", title.lower()).strip()
            same = [s for s in summaries
                    if re.sub(r"\W+", " ", (s.get("title") or "").lower()).strip() == wanted
                    and wanted]
            if len(same) == 1:
                return self.fetch(same[0]["key"])
            if len(same) > 1:
                keys = ", ".join(str(s["key"]) for s in same[:4])
                return None, f"{len(same)} library items have this exact title ({keys})"
        return None, f"{miss}; no exact title match either (scanned {len(summaries)} records)"

    def candidates(self, item_types=None):
        """Every scanned summary, for a caller that wants to score titles itself."""
        return self.scan(item_types)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--guide", action="store_true", help="print the setup instructions")
    ap.add_argument("--whoami", action="store_true", help="show the key's owner and scope")
    ap.add_argument("--find-doi", metavar="DOI")
    ap.add_argument("--find-isbn", metavar="ISBN")
    ap.add_argument("--find-title", metavar="TITLE")
    ap.add_argument("--config", type=Path, default=None, help="path to the config file")
    ap.add_argument("--key", default=None, help="override the API key")
    ap.add_argument("--library", default=None, help='e.g. "users/123" or "groups/456"')
    ap.add_argument("--json", action="store_true", help="print the record as JSON")
    args = ap.parse_args(argv)

    if args.guide:
        print(guidance())
        return 0

    conf = resolve(config_path=args.config, key=args.key, library=args.library)
    if not conf["api_key"]:
        print(guidance(), file=sys.stderr)
        return 2

    client = Client(conf["api_key"], conf["library"])
    if conf["config"]:
        print(f"config: {conf['config']}  (key from {conf['source']})")
    else:
        print(f"key from {conf['source']} (no {CONFIG_NAME} found)")

    if args.whoami or not (args.find_doi or args.find_isbn or args.find_title):
        info = client.whoami()
        if not info:
            print("could not read the key:", client.notes[-1] if client.notes else "unknown")
            return 1
        scope = info.get("access") or {}
        user = scope.get("user") or {}
        print(f"userID  : {info.get('userID')}")
        print(f"username: {info.get('username')}")
        print(f"library : {client.library_path()}")
        print("access  : " + ", ".join(f"user.{k}={'yes' if v else 'no'}"
                                       for k, v in sorted(user.items())) or "unknown")
        groups = scope.get("groups") or {}
        if groups:
            print("          groups: " + ", ".join(groups))
        if user.get("write"):
            print("note    : this key can write. This pipeline never does; a read-only "
                  "key is enough and is the safer thing to create.")
        return 0

    if args.find_doi or args.find_isbn:
        item, err = client.by_identifier(doi=args.find_doi, isbn=args.find_isbn)
    else:
        items, err = client.candidates()
        if items is not None and args.find_title:
            want = args.find_title.lower()
            items = [s for s in items if want in (s.get("title") or "").lower()]
        item = None
        if items is not None:
            print(f"{len(items)} candidate record(s)")
            for it in items:
                d = it.get("data") or {}
                print(f"  {d.get('itemType'):18s} {d.get('date','?'):10s} {str(d.get('title'))[:70]}")
            item = items[0] if items else None
    if err:
        print(err)
        return 1
    if item is None:
        print("no match")
        return 1
    d = item.get("data") or {}
    print(f"found: {d.get('itemType')} · key={item.get('key')}")
    if args.json:
        print(json.dumps(item, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
