# Using your Zotero library as the metadata source

This is the manual for the optional Zotero half of the pipeline: where the API key
goes, how to check it works, and what to do when it does not.

You do **not** need Zotero installed, and you do not need to be at the machine
that runs Zotero. The pipeline talks to the Zotero Web API (`api.zotero.org`),
which reads the library your account has synced. It is **read-only**: the code
issues GET requests and has no path that writes to a library, so a read-only key
is not just enough, it is the right thing to create.

## Why bother

Without it, metadata is matched by title against arXiv and Crossref. That works,
but a title match can land on the wrong record — measured: *Attention Is All You
Need* matched a 2025 book chapter, and a search for one paper's title matched a
different publisher's chapter about the same work.

With it, a paper that is already in your library is identified **exactly**, by
DOI, ISBN or an exact title, and the record you curated is what ends up in the
note. Nothing has to be guessed, and the fields are the ones Zotero itself defines.

## Step 1 — create a key

1. Go to <https://www.zotero.org/settings/keys/new> (this is the web account
   settings, not the desktop app).
2. Name it something you will recognise later, e.g. `bilingual-paper-notes`.
3. Under **Personal Library**, tick **Allow library access**.
4. Leave **Allow write access** unticked. This pipeline never writes.
5. If the papers live in a group, tick that group instead, and note its id.
6. Press **Save Key**.

The key is shown **once** — copy it now. The same page shows your numeric
**user ID**; you do not need it, the pipeline reads it from the key itself.

## Step 2 — put the key in a file

Create `.bilingual-paper-notes.json` in the directory you run the pipeline from
(or any parent directory of it), and give it a `zotero` section:

```json
{
  "zotero": {
    "api_key": "PASTE_THE_KEY_HERE"
  }
}
```

That file is gitignored by this project for exactly this reason: it holds
credentials and must never be committed or shared.

Two alternatives, both equivalent:

```json
{ "zotero": { "api_key": "...", "library": "groups/1234567" } }
```

```bash
export ZOTERO_API_KEY=...        # or ZOTERO_LIBRARY=groups/1234567
```

`library` is only needed for a group library, or to point the pipeline at a
library other than the key owner's own; the default is `users/<the key's owner>`.

Priority: an explicit `--zotero-key` argument, then `ZOTERO_API_KEY`, then the
config file.

## Step 3 — check it works

```bash
python scripts/zotero.py --whoami
```

Expected:

```
config: /path/to/.bilingual-paper-notes.json  (key from config file)
userID  : 1234567
username: Your Name
library : /users/1234567
access  : user.files=yes, user.library=yes, user.notes=yes, user.write=no
```

If it prints the setup instructions instead, no key was found — that is the same
text `python scripts/zotero.py --guide` prints any time.

Then a real lookup, by an identifier you know is in the library:

```bash
python scripts/zotero.py --find-doi 10.1103/PhysRevLett.116.061102
```

## What the pipeline actually does

1. It reads the DOI, arXiv id or ISBN printed on the document's first pages.
2. It looks for that identifier in your library, comparing values exactly. If
   there is no identifier, it falls back to an **exact** title match (normalised
   whitespace and punctuation), and refuses when the title is ambiguous.
3. A match makes that record authoritative: its item type and fields are used,
   and only fields it leaves empty are filled from arXiv and Crossref. Authors
   keep the affiliation and correspondence address printed on the paper, joined
   by surname.
4. No match is not an error: arXiv and Crossref answer instead, producing the
   same fields.

Skip the library for one run with `--no-zotero`.

## Troubleshooting

| Symptom | Meaning and what to do |
|---|---|
| `403: the key was rejected or has no access to this library` | Wrong key, a revoked key, or a key scoped to a group while the pipeline asks for the personal library. Re-copy the key, or set `"library": "groups/<id>"`. |
| `404: library or endpoint not found` | The `library` value is malformed — it must look like `users/1234` or `groups/5678`. |
| `429 rate-limited (Retry-After ...)` | Normal under bursts; the client waits and retries. A wait over 60s ends the attempt and the run falls back to arXiv/Crossref. |
| `no item in the library carries DOI ...; no exact title match either` | The work is not in the library, or its title differs from the document's. Nothing is guessed — add it to Zotero, or let arXiv/Crossref answer. |
| `N library items have this exact title` | Ambiguous title: two records share it. The pipeline refuses rather than picking one; add a DOI to the record you mean. |
| Setup instructions printed instead of a lookup | No key found: the file is not in the working directory or a parent, or the section is not named `zotero`. |
| `note: this key can write` | The key has write access. Nothing in this pipeline writes; rotate to a read-only key when convenient. |

## What stays empty without Zotero

These have no source outside a library, and are left empty rather than invented:
`citationKey` (only Zotero or Better BibTeX assigns one), `libraryCatalog`,
`callNumber`, `archive`, `archiveLocation`. They are valid Zotero fields, so the
item stays inside Zotero's model; there is simply nothing to put in them.

## Housekeeping

- A key pasted into a chat, an issue or a commit is a leaked key. Rotate it at
  <https://www.zotero.org/settings/keys> and paste the new one into the config
  file only.
- `.bilingual-paper-notes.json` is gitignored here; keep it that way in your own
  repositories too.
- The pipeline never asks for your Zotero password, and never needs it. An API
  key can be limited and revoked; a password cannot.
