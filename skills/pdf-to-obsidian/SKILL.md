---
name: pdf-to-obsidian
description: Convert a PDF into an Obsidian-flavoured Markdown note, and optionally its bilingual half in the same run. Extracts structure (headings, lists, GFM tables, LaTeX equations, figure crops, footnotes, reference list) and links cross-references in both halves; the note's properties are a Zotero item (type, authors with the affiliations the paper prints, venue or repository, date, DOI, ISBN, url), read from your Zotero library when a key is set and otherwise from arXiv/Crossref. Two ways to run it — PDF to Markdown only (`--no-translate`, no translation endpoint needed), or PDF to Markdown plus Chinese translation in collapsible callouts (default when an endpoint is configured). Use when the user wants a paper PDF turned into an Obsidian note, its structure and metadata extracted, or the paper readable bilingually. Input that is already Markdown belongs to translate-markdown. Not for creating, merging or form-filling PDFs (pdf skill); not for authoring Obsidian syntax (obsidian-markdown).
license: MIT
compatibility: Requires Python 3.10+. Parsing needs MinerU 4.x (`pip install -U "mineru>=4.0,<5"` plus `mineru-kit models download`); PyMuPDF (`pip install pymupdf`) is optional but improves heading levels. Translation needs an OpenAI-compatible endpoint and API key (or pi's own provider config); pass `--no-translate` to produce the structure half only. Network access is used for the metadata stage and is optional. Tested on Windows and Linux.
---

# PDF → bilingual Obsidian note

English stays as it is; a Chinese translation sits underneath each paragraph in
a collapsible callout; cross-references actually click.

## When to use

- The user wants an English paper readable or annotatable inside Obsidian.
- The user wants a paper translated paragraph by paragraph (English preserved).
- The user asks for a paper's structure, metadata or cross-reference links.
- The user wants a paper PDF turned into Markdown at all — including "just the
  Markdown", which is `--no-translate`.

## The two skills, and the four things people ask for

`pdf-to-obsidian` takes a PDF; its sibling `translate-markdown` takes a note that
is already Markdown. That is the only difference in what they take in — both
write a bilingual Obsidian note, and both are the same engine over the same
intermediate layer.

| The user wants | What to run |
|---|---|
| a PDF turned into an Obsidian note | `python scripts/run.py paper.pdf` |
| a PDF readable bilingually | the same command; translation is on by default |
| structure / metadata / links only, no translation | `python scripts/run.py paper.pdf --no-translate` |
| an existing `.md` note translated | the `translate-markdown` skill |

Translating a note that this skill produced is best done through its work
directory, because the typed blocks are still there — re-segmenting a rendered
note loses them (captions become ordinary paragraphs, and would be translated):

```bash
python scripts/run.py paper.pdf --stage translate   # translates blocks.jsonl
python scripts/run.py paper.pdf --stage render      # re-renders the note
```

## Scope: what this does NOT handle

Be explicit with the user rather than producing a bad artifact:

| Input | Status |
|---|---|
| Academic papers, preprints, technical reports | Supported |
| **Slides / lecture decks** | **Not supported.** Slides are landscape with a few short text boxes per page; the paragraph-level callout layout and the paper-specific heuristics (front-matter block, references section, citation linkification) all assume prose. Metadata lookup would also return nothing useful. Do not silently run it on a deck. |
| **Books / long textbooks (100+ pages)** | **Not supported.** One note would be several MB (a 92-page paper already yields ~0.5 MB), which degrades Obsidian's editor, search and outline pane, and makes the artifact unreviewable. Split the book by chapter first (ideally along its own PDF outline) and produce one note per chapter plus an index note. |
| Scanned PDFs with no text layer | Works only if MinerU's OCR handles the language; check the output. |
| Legal/medical text where a mistranslation is unacceptable | Use it to draft, then have a human verify. |

Related skills, so you pick the right one:

- `pdf` — creating, merging, form-filling, or layout-critical visual review of PDFs.
- `obsidian-markdown` — authoring or fixing Obsidian syntax itself (wikilinks, callouts, properties).
- `obsidian-cli` — vault operations, search, plugin development.
- `translate-markdown` — the same bilingual output for input that is already Markdown.

## Setup (once)

```bash
pip install -U "mineru>=4.0,<5" pymupdf
```

You do not have to pre-download the models: they are fetched on the first parse,
and `run.py` reports which store it will use before parsing starts. It resolves,
in order:

1. `--mineru-home` / `BPN_MINERU_HOME`, if you set one
2. a populated `<cwd>/.mineru`
3. a populated `~/.mineru` (MinerU's own default)
4. otherwise `<cwd>/.mineru`, and the first parse downloads there
   (~800 MB for `--tier basic`, ~2 GB for `standard`)

So a second project downloads its own copy. To share one store across projects,
pre-download into `~/.mineru` (`mineru-kit models download --tier standard` does
exactly that) or set `MINERU_HOME` to one path and pass the same value every
time. If an explicit home is empty while another store already holds models,
`run.py` prints a warning naming both paths instead of silently downloading
2 GB.

If MinerU lives in another environment, use
`--mineru-cmd 'conda run -n mineru mineru-kit'`.

Translation endpoint, first match wins:

```bash
export BPN_BASE_URL=https://api.example.com/v1 BPN_API_KEY=... BPN_MODEL=...
# or a config file: ./.bilingual-paper-notes.json, then ~/.config/bilingual-paper-notes/config.json
```

pi users need no key: pi's own provider config is the last fallback.

## Metadata source (Zotero)

The note's properties are a Zotero item: field names and the fields an item type
allows come from Zotero's schema (`scripts/data/zotero-schema.json`), and
`enrich_meta.py` validates the result against it. Nothing outside that model is
written to the note.

Metadata is read, in order, from a Zotero library (when a key is configured),
the identifiers printed on the PDF, arXiv, and Crossref. A library record that
matches an identifier exactly is authoritative — which is the point: it needs no
title matching, so it cannot land on the wrong record.

When the user wants their library used, or says an author / venue / year is
wrong:

1. Check whether a key is configured: `python scripts/zotero.py --whoami`.
2. If it prints setup instructions instead, walk them through **ZOTERO.md** (or
   `python scripts/zotero.py --guide`): the key goes in
   `.bilingual-paper-notes.json` under `"zotero": {"api_key": "..."}`, created
   at <https://www.zotero.org/settings/keys/new> with read access only.
3. Never ask for their Zotero password — an API key is what that is for, and it
   can be limited and revoked. Never print the key back, and never write it into
   a tracked file: the config file is gitignored here for that reason.

`--no-zotero` skips the library for a run; without a key nothing breaks, because
arXiv and Crossref fill the same fields.

## Usage

```bash
python ../../scripts/run.py paper.pdf                     # all stages
python ../../scripts/run.py paper.pdf --vault ~/Vault      # and copy into a vault
python ../../scripts/run.py paper.pdf --no-translate      # structure only, no LLM calls
python ../../scripts/run.py paper.pdf --stage translate   # re-run one stage
python ../../scripts/run.py paper.pdf --zh-style callout-folded
```

Stages: `parse` (MinerU) → `md` (normalise + render) → `meta` (Zotero, else
arXiv / Crossref) → `translate` → `render` → `verify`. Each is re-runnable, and
everything for a document lands in one output directory.

Always read `verify.py`'s output last: it exits non-zero on dead links, missing
images, lost placeholders, lost formulas or failed translation.

## What the note looks like

- Properties are a **Zotero item**: `itemType`, `title`, `authors`, then the
  fields that type allows, in Zotero's order (`publicationTitle` or `bookTitle`
  or `repository`, `date`, `volume`, `issue`, `pages`, `publisher`, `ISBN` /
  `ISSN`, `DOI`, `url`, `language`, `abstractNote`). `authors` is a plain string
  list — the one deliberate deviation, because Obsidian's Properties panel cannot
  display an array of objects; `meta.json` keeps Zotero's own `creators` shape.
  Author entries carry the affiliation and correspondence address the paper
  prints, e.g. `"Ashish Vaswani (Google Brain)"`.
- The note does **not** link or reference the source PDF, and carries no page
  numbers or page links.
- **Headings and figure/table captions stay in English** (outline pane and
  cross-references stay coherent); body paragraphs and footnotes are translated
  as foldable callouts:

  ```markdown
  The track/recover model of the previous section has two limitations:

  > [!zh]+ 译文
  > 上一节的 track/recover 模型有两个局限：
  ```

- Equations are LaTeX with `\tag{n}`; tables are GFM; figures are crops in
  `<stem>.assets/`; references are a list with DOI links. None are translated.
- Cross-references are links in both languages: `[[#^ref-20|20]]`,
  `[[#^thm-7|Theorem 7]]`, `[[#^tab-1|Table 1]]`, `[[#3.1. Effect Functions|Section 3.1]]`
  and the Chinese forms (`[[#^thm-7|定理 7]]`). Anchors are emitted only where
  something can point at them.

## Options worth knowing

| Flag | Effect |
|---|---|
| `--zh-style quote\|callout-folded` | presentation of the Chinese half |
| `--translate-captions` | also translate figure/table captions (default off) |
| `--glossary FILE` | extra domain glossary, merged over the built-in one |
| `--stage meta` | refresh the metadata (Zotero / arXiv / Crossref) without re-parsing or re-translating |
| `--stage render` | change layout only; free, no LLM calls |
| `--zotero-key` / `--no-zotero` | read metadata from a Zotero library, or skip it (see ZOTERO.md) |
| `--batch-units` / `--batch-chars` | translation request size (see pitfalls) |

## Pitfalls (measured, not guessed)

1. **Do not use `pymupdf4llm` as the parser.** Its layout mode detects formula
   regions but has no formula→LaTeX model, so display equations vanish (measured:
   79 formula boxes, zero output). MinerU is the only open tool here that emits
   LaTeX for equations.
2. **Disable thinking on DeepSeek** (`thinking={"type": "disabled"}`). It is on by
   default and spends the whole budget on reasoning (a "reply OK" burned 15 of 17
   output tokens). `thinking=false` is a 422; `enable_thinking=false` is ignored.
   Exception: the pipeline deliberately re-asks a stubborn block with thinking
   **on** — that rescues the one block where the model keeps dropping a formula.
3. **Mask citations before translating.** In `blocks.jsonl` a citation is still
   plain `[1]`, so an unmasked one comes back as a full-width `（1）` and the
   Chinese line loses the link.
4. **Never re-send a whole batch on partial failure.** Keep what validated and
   re-ask only the missing units, and tell the model *which* placeholder it
   dropped: a block that omits a formula reliably reproduces the omission on a
   blind retry (3/3) and fixes it when told (3/3). Together with skipping
   formula-only blocks this cut a full run's cost by 42% and its request count
   from 1.85× to 1.05× per batch.
5. **Crossref's `score` is not a similarity measure** (an unrelated book chapter
   scored 21.9/100). Match titles yourself, and never accept a candidate whose
   year is far from the identifier's year: "Attention Is All You Need" matches a
   2025 book chapter whose chapter title is identical.
6. **OpenAlex is credit-budgeted** (`Insufficient budget … Resets at midnight
   UTC`, `Retry-After` ~19600 s) — do not depend on it. That is why the metadata
   sources are a Zotero library (exact, offline) and then arXiv and Crossref.
7. **Unknown block types must not be dropped.** MinerU's `chart` carries figures
   and `aside_text` carries the arXiv stamp; classify unknown blocks by shape
   (image_path → figure, text → body).
8. **The title may live in a `doc_title` block** while `metadata.document.title`
   is empty (common for arXiv PDFs); dropping it silently falls back to the file name.
9. **Block ids are position-sensitive.** For structured blocks the `^id` goes on
   its own line with blank lines around; for a paragraph, at the end of the line.
   A `[` may not sit directly before `[[`, which is why citations render as `1`.
10. **`\tag{}` renders in Obsidian, `\label{}` does not.** Pandoc complains about
    `\tag`/`\begin{array}` — that is pandoc's math parser, not Obsidian's.
11. **Re-run safety is deliberate.** A temporarily unavailable source must not
    drop metadata a previous run resolved, so old values are kept and labelled.
12. **Model stores must not duplicate silently.** `run.py` resolves the store
    (explicit → `<cwd>/.mineru` → `~/.mineru` → download into `<cwd>/.mineru`),
    prints the path and its contents before parsing, and warns when an explicit
    `--mineru-home` is empty while another store already holds models. On that
    warning, drop the flag or point it at the populated path — do not let the
    parse run and re-download 2 GB.

## Recovering from a bad run

- Wrong heading levels → install PyMuPDF (the outline supplies levels).
- Bad terminology → edit the glossary and re-run `--stage translate`; only
  changed blocks are re-sent (the cache key includes the glossary).
- Layout wrong → `--stage render` only. Free.
- Parse quality poor → `--tier standard` instead of `basic`, then
  `BPN_FORCE_PARSE=1 --stage parse`.

## Portability

No pi-specific code: the scripts use the standard library plus optional PyMuPDF,
and the endpoint comes from flags, environment variables (`BPN_*` /
`OPENAI_*`) or a `.bilingual-paper-notes.json` config file. Reading pi's config
(`~/.pi/agent/models.json`) is only a last-resort fallback; without pi you get an
error naming the variables to set.

The scripts are shared at `../../scripts/`, so **run from a checkout of the whole
repository**. To use this skill in another harness, point that harness at this
repository's `skills/` directory rather than copying this directory out of it.

## Files

```
scripts/run.py            orchestrator (start here)
scripts/render.py   MinerU middle_json -> blocks.jsonl -> note.md
scripts/translate.py      block-level translation + sqlite cache
scripts/enrich_meta.py    Zotero / arXiv / Crossref -> the note's Zotero item
scripts/zotero.py         read-only Zotero Web API client (see ZOTERO.md)
scripts/zotero_schema.py  Zotero's data model, from the official schema
scripts/verify.py         structural checks
scripts/md2blocks.py      Markdown input entry (see the translate-markdown skill)
scripts/glossary.txt      default glossary (keep the structural terms)
examples/glossary.example.txt  a filled-in domain glossary
assets/bilingual-paper-notes.css       Obsidian snippet that styles the 译文 callout
assets/workflow.svg       the diagram in the README (source: workflow.puml)
```
