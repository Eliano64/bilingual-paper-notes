---
name: translate-markdown
description: Turn an English Markdown note into a bilingual Obsidian note. Adds a paragraph-level Chinese translation in collapsible callouts while leaving headings, code, tables, equations, references and the author's own wikilinks untouched. Use when the user wants an existing .md note translated, wants English notes readable bilingually in Obsidian, or has a paper's Markdown source (rather than a PDF) to translate. Not for PDF input (use pdf-to-obsidian) and not for authoring or fixing Obsidian syntax itself (use obsidian-markdown).
license: MIT
compatibility: Requires Python 3.10+. No PDF tooling involved. Translation needs an OpenAI-compatible endpoint and API key (BPN_* or OPENAI_* environment variables, a .bilingual-paper-notes.json config file, or pi's own provider config). Tested on Windows and Linux.
---

# English Markdown → bilingual Obsidian note

For notes that are already Markdown. There is no extraction step: the file is
segmented into typed blocks and translated. Everything downstream is the same
code the PDF pipeline uses.

## When to use

- The user has an English `.md` note (their own, or a paper's Markdown source)
  and wants a Chinese translation under each paragraph.
- The user wants to read English notes bilingually inside Obsidian.

## Scope

| Input | Status |
|---|---|
| English prose notes, paper Markdown sources, exported articles | Supported |
| PDFs | Use `pdf-to-obsidian` instead; this path does no extraction. |
| Notes with images referenced relatively | Supported: the note is written **beside its source**, so relative links keep working. |
| Obsidian syntax authoring/repair | Use `obsidian-markdown`. |

## Usage

Metadata is not looked up for Markdown input: there is no document to identify,
so the pipeline keeps the note's own properties and only segments, translates and
renders it. (Identifiers, arXiv/Crossref lookup and Zotero all apply to PDF input
through the `pdf-to-obsidian` skill.)

```bash
python ../../scripts/run.py note.md                    # all stages
python ../../scripts/run.py note.md --no-translate     # segmentation only
python ../../scripts/run.py note.md --glossary my.txt  # domain glossary
python ../../scripts/run.py note.md --stage translate   # re-run after a glossary edit
```

Layout, which is chosen so the note stays a good citizen in a vault:

```
my-note.md                     the source, never modified
my-note.bilingual.md           the result (assets keep resolving relatively)
my-note.png                    untouched
.bilingual-paper-notes/my-note/         blocks.jsonl, meta.json, translate.sqlite
```

Use `--out DIR` to write the note elsewhere and `--note-name NAME` to rename it.

## What gets translated

| Block | Behaviour |
|---|---|
| Paragraphs, blockquotes, list items | translated, English kept above |
| Headings | **not** translated (outline pane stays coherent) |
| Footnotes (`[^1]: …`) | translated, collected at the end |
| Fenced code, `$$` display math, tables, images | not translated |
| A section after a "References"/"Bibliography" heading | treated as references, not translated |
| `$…$` inline math, `` `code` ``, `[[wikilinks]]`, `[^n]` references | masked before the request and restored after, so they survive byte for byte |

Cross-reference linkification is **off** for Markdown input: the author's own
`[12]` citations and `[[wikilinks]]` are left exactly as written. (Pass
`--no-xref` explicitly if a converted paper source still needs it forced off; the
switch comes from `meta.json`, not from a flag, so it stays consistent across
`--stage render` runs.)

## Verification

`run.py` ends with `verify.py`, which fails on broken asset references, dead
block links, leftover `⟦n⟧` placeholders, unbalanced fences or `$`, translations
that lost inline math, and blocks that failed translation. Read its output; do
not report success without it.

## Pitfalls

1. **Do not translate headings.** Users read the outline in English and use it
   to navigate; translated headings break that and every `[[#heading]]` link.
2. **Mask before translating, verify after.** A citation left unmasked comes back
   as a full-width `（1）`; an unmasked `$…$` can come back rewritten. The pipeline
   masks `$…$`, wikilinks, `[[…]]`, `<sup>/<sub>`, `[^n]` and `[12]`-style
   citations, then checks every placeholder came back exactly once (a *repeated*
   placeholder is accepted — the model restating a formula is faithful; a
   *missing* one is not).
3. **Keep the note beside its source.** Moving it into a work directory breaks
   relative image links; that is why the layout above puts only the work files in
   `.bilingual-paper-notes/`.
4. **A sentence-fragment note translates badly.** Bullet-fragment decks and
   outline-only notes have no sentences to translate; expect literal output. Tell
   the user rather than pretending otherwise.
5. **The translation cache is keyed on the glossary and prompt**, so editing the
   glossary re-translates everything once. Settle terminology before large runs.

## Portability

No pi-specific code: the scripts use the standard library only, and the endpoint
comes from flags, environment variables (`BPN_*` / `OPENAI_*`) or a
`.bilingual-paper-notes.json` config file. Reading pi's config is a last-resort fallback.

The scripts are shared at `../../scripts/`, so **run from a checkout of the whole
repository**. To use this skill in another harness, point that harness at this
repository's `skills/` directory rather than copying this directory out of it.

## Files

```
scripts/run.py            orchestrator (start here)
scripts/md2blocks.py      Markdown -> blocks.jsonl (the only Markdown-specific part)
scripts/translate.py      block-level translation + sqlite cache
scripts/render.py   renderer (blocks.jsonl -> note.md)
scripts/verify.py         structural checks
scripts/glossary.txt      default glossary (keep the structural terms)
```
