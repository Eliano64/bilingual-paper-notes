#!/usr/bin/env python
"""One-command pipeline: PDF -> bilingual Obsidian note.

    python scripts/run.py paper.pdf                 # everything, standard tier
    python scripts/run.py paper.pdf --no-translate  # structure only, no LLM calls
    python scripts/run.py paper.pdf --vault ~/Obsidian/MyVault
    python scripts/run.py paper.pdf --stage translate   # re-run one stage

Stages, and what each one needs:

    parse      MinerU 4.x (`mineru-kit`)     PDF -> middle_json.json + images
    md         this repo, PyMuPDF optional   middle_json -> blocks.jsonl + note
    meta       network (arXiv/Crossref/S2)   adds doi/venue/year/citations
    translate  an OpenAI-compatible endpoint blocks.jsonl gets Chinese
    render     this repo                     note.md is written
    verify     this repo                     structural checks, exit 1 on failure

Every stage is idempotent and writes into <out>/:
    <out>/parse/            raw MinerU output (kept, so md/meta can be re-run alone)
    <out>/md/blocks.jsonl   the intermediate layer everything else reads
    <out>/md/<stem>.md      the note
    <out>/md/<stem>.assets/ images and equation crops
    <out>/md/meta.json      document metadata + warnings
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
STAGES = ("parse", "md", "meta", "translate", "render", "verify")


def run(cmd, env=None, cwd=None):
    print("  $ " + " ".join(str(c) for c in cmd), flush=True)
    r = subprocess.run([str(c) for c in cmd], env=env, cwd=cwd)
    if r.returncode != 0:
        sys.exit(f"stage failed (exit {r.returncode})")


def find_middle_json(root: Path):
    hits = sorted(root.rglob("middle_json.json"))
    return hits[0] if hits else None


TIER_MODEL_SIZE = {"flash": "no models needed",
                  "basic": "~800 MB of ONNX models",
                  "standard": "~2 GB (ONNX models + a 1.2 GB VLM)",
                  "advanced": "~2 GB (same models as standard)"}


def has_models(home: Path) -> bool:
    models = home / "models"
    try:
        return models.is_dir() and any(models.iterdir())
    except OSError:
        return False


def model_dirs(home: Path) -> list[str]:
    """Real model directories, skipping MinerU's own bookkeeping (.locks, ...)."""
    models = home / "models"
    try:
        return sorted(p.name for p in models.iterdir()
                      if p.is_dir() and not p.name.startswith("."))
    except OSError:
        return []


def resolve_mineru_home(explicit: str | None, cwd: Path) -> tuple[Path, str]:
    """Where MinerU will read (and if necessary write) its models.

    Order: an explicit setting wins; then a populated project-local `.mineru`;
    then MinerU's own default home; and only if neither has models, download
    into the project directory. A fresh 2 GB copy per project is the price of
    that last rule, so the caller prints the path and size before it happens;
    setting MINERU_HOME (or keeping one copy in `~/.mineru`) shares one store.
    """
    if explicit:
        return Path(explicit).expanduser(), "explicit setting"
    local, default = cwd / ".mineru", Path("~/.mineru").expanduser()
    if has_models(local):
        return local, "models found in the current directory"
    if has_models(default):
        return default, "models found in MinerU's default home"
    return local, "nothing found here yet"


def report_models(home: Path, why: str, tier: str, explicit: str | None, cwd: Path):
    name, is_mscope = "modelscope", os.environ.get("MINERU_MODEL_SOURCE", "auto")
    print(f"  models: {home}")
    print(f"          ({why}; source={is_mscope})")
    dirs = model_dirs(home)
    if dirs:
        print(f"          present: {', '.join(dirs)}")
    else:
        print(f"          empty -> MinerU will download {TIER_MODEL_SIZE.get(tier, 'the models')} "
              f"on first use")
        print(f"          to share one copy across projects, put them in ~/.mineru or set "
              f"MINERU_HOME")
    # an explicit home that is empty while a populated one exists is the only way
    # to still pay for the download twice; say so instead of doing it quietly
    if explicit:
        others = [p for p in (cwd / ".mineru", Path("~/.mineru").expanduser())
                  if not has_models(home) and str(p) != str(home) and has_models(p)]
        if others:
            print(f"          ! WARNING: {home} has no models but {others[0]} does; "
                  f"this parse will download a second copy unless you drop "
                  f"--mineru-home or point it at that path")


def stage_parse(pdf: Path, out: Path, tier: str, mineru_home: str | None, mineru_cmd: str):
    parse_dir = out / "parse"
    parse_dir.mkdir(parents=True, exist_ok=True)
    if find_middle_json(parse_dir) and not os.environ.get("PDF2MD_FORCE_PARSE"):
        print("  parse: reusing existing output (set PDF2MD_FORCE_PARSE=1 to redo)")
        return
    # MinerU often lives in its own environment (conda/venv) rather than on PATH,
    # so allow a command, not just an executable name
    cmd = shlex.split(mineru_cmd)
    if not cmd or (shutil.which(cmd[0]) is None and not Path(cmd[0]).exists()):
        sys.exit(
            f"MinerU is not available ('{cmd[0] if cmd else mineru_cmd}' not found).\n"
            "  pip install -U 'mineru>=4.0,<5'\n"
            "  mineru-kit models download --tier basic   # or --tier standard for a VLM pass\n"
            "If it is installed in another environment, point at it:\n"
            "  run.py paper.pdf --mineru-cmd 'conda run -n mineru mineru-kit'\n"
            "Parsing is the only stage that needs MinerU; with a middle_json.json in\n"
            "place already, run --stage md.")
    env = dict(os.environ)
    home, why = resolve_mineru_home(mineru_home, Path.cwd())
    env["MINERU_HOME"] = str(home)
    report_models(home, why, tier, mineru_home, Path.cwd())
    env.setdefault("PYTHONUTF8", "1")
    run(cmd + ["parse", str(pdf), "-o", str(parse_dir), "--tier", tier, "--format", "zip"], env=env)
    for z in parse_dir.glob("*.zip"):
        with zipfile.ZipFile(z) as zf:
            zf.extractall(parse_dir / z.stem)
        z.unlink()


def stage_md(pdf: Path, out: Path, extra: list[str], note_name: str | None = None):
    mj = find_middle_json(out / "parse")
    if not mj:
        sys.exit(f"no middle_json.json under {out/'parse'} -- run --stage parse first")
    run([sys.executable, HERE / "pdf2obsidian.py", mj, pdf, "-o", out / "md", *extra]
        + (["--note-name", note_name] if note_name else []))


def stage_md_from_markdown(source: Path, out: Path, extra: list[str], note_name: str):
    """Markdown in: segment it into a work dir, then render the note beside the
    source so that its relative image links keep resolving."""
    work = markdown_work_dir(source, out)
    run([sys.executable, HERE / "md2blocks.py", source, "-o", work])
    stage_render_markdown(source, out, extra, note_name)


def markdown_work_dir(source: Path, out: Path | None = None) -> Path:
    """Work files for a markdown source live in <source dir>/.pdf2obsidian/<stem>."""
    return source.parent / ".pdf2obsidian" / source.stem


def markdown_note_path(source: Path, note_name: str) -> Path:
    return source.parent / note_name


def stage_render_markdown(source: Path, out: Path, extra: list[str], note_name: str):
    work = markdown_work_dir(source, out)
    if not (work / "blocks.jsonl").exists():
        sys.exit("blocks.jsonl missing -- run --stage md first")
    run([sys.executable, HERE / "pdf2obsidian.py", work / "blocks.jsonl", source,
         "-o", source.parent, "--blocks", work / "blocks.jsonl", "--render-only",
         "--note-name", note_name, *extra])


def stage_meta(out: Path):
    meta = out / "md" / "meta.json"
    if not meta.exists():
        sys.exit("meta.json missing -- run --stage md first")
    run([sys.executable, HERE / "enrich_meta.py", meta])


def stage_translate(out: Path, extra: list[str], blocks_path: Path | None = None):
    blocks = blocks_path or (out / "md" / "blocks.jsonl")
    if not blocks.exists():
        sys.exit(f"blocks.jsonl missing at {blocks} -- run --stage md first")
    run([sys.executable, HERE / "translate.py", blocks, *extra])


def stage_render(pdf: Path, out: Path, extra: list[str], note_name: str | None = None):
    mj = find_middle_json(out / "parse")
    if not mj:
        sys.exit("no middle_json.json -- run --stage parse first")
    run([sys.executable, HERE / "pdf2obsidian.py", mj, pdf, "-o", out / "md",
         "--render-only", *extra] + (["--note-name", note_name] if note_name else []))


def stage_verify(out: Path, no_translate: bool = False, blocks_path: Path | None = None,
                 note: Path | None = None):
    blocks = blocks_path or (out / "md" / "blocks.jsonl")
    run([sys.executable, HERE / "verify.py", blocks]
        + (["--no-translate"] if no_translate else [])
        + (["--note", str(note)] if note else []))


def copy_to_vault(out: Path, vault: Path, pdf: Path):
    """The note is portable: asset links are [[<stem>.assets/...]] and Obsidian
    resolves those by path suffix, so the folder can sit anywhere in the vault."""
    md_dir = out / "md"
    stem = pdf.stem
    vault.mkdir(parents=True, exist_ok=True)
    for name in (f"{stem}.md", f"{stem}.pdf", "meta.json"):
        src = md_dir / name if name != f"{stem}.pdf" else pdf
        if src.exists():
            shutil.copy2(src, vault / name)
    src_assets = md_dir / f"{stem}.assets"
    if src_assets.exists():
        dst = vault / f"{stem}.assets"
        if dst.exists():
            shutil.rmtree(dst)
        shutil.copytree(src_assets, dst)
    print(f"  copied into vault: {vault}")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("source", type=Path,
                    help="a PDF (full pipeline) or an English .md note (segment + translate)")
    ap.add_argument("-o", "--out", type=Path, default=None,
                    help="output dir; for .md input it defaults to the note's own folder "
                         "so that relative image links keep working")
    ap.add_argument("--note-name", default=None,
                    help="output file name (default <stem>.md for PDF, "
                         "<stem>.bilingual.md for Markdown)")
    ap.add_argument("--vault", type=Path, default=None,
                    help="also copy the note, its assets and the PDF into an Obsidian vault")
    ap.add_argument("--stage", choices=("all",) + STAGES, default="all")
    ap.add_argument("--tier", default="standard",
                    help="MinerU tier: flash|basic|standard|advanced (default standard)")
    ap.add_argument("--mineru-cmd", default=os.environ.get("PDF2MD_MINERU_CMD", "mineru-kit"),
                    help="how to invoke MinerU; e.g. 'conda run -n mineru mineru-kit' "
                         "(env PDF2MD_MINERU_CMD)")
    ap.add_argument("--mineru-home", default=os.environ.get("PDF2MD_MINERU_HOME"),
                    help="where MinerU keeps models; default: a populated <cwd>/.mineru "
                         "or ~/.mineru, else <cwd>/.mineru (downloaded on first use)")
    ap.add_argument("--no-translate", action="store_true")
    ap.add_argument("--translate-captions", action="store_true",
                    help="also translate figure/table captions (default: off)")
    ap.add_argument("--glossary", type=Path, default=None)
    ap.add_argument("--zh-style", default="callout-open",
                    choices=("callout-open", "callout-folded", "quote"))
    ap.add_argument("--model", default=None)
    ap.add_argument("--config", type=Path, default=None)
    ap.add_argument("--page-markers", default="none",
                    choices=("comment", "link", "none"))
    ap.add_argument("--block-anchors", action="store_true")
    args = ap.parse_args(argv)

    source = args.source.expanduser().resolve()
    if not source.exists():
        sys.exit(f"no such file: {source}")
    is_markdown = source.suffix.lower() in (".md", ".markdown")
    if is_markdown:
        # the note must sit beside its source (relative image links), while work
        # files go to a hidden directory next to it
        out = (args.out or source.parent).expanduser().resolve()
        note_name = args.note_name or f"{source.stem}.bilingual.md"
        work = markdown_work_dir(source, out)
        work.mkdir(parents=True, exist_ok=True)
        todo = [s for s in ["md", "translate", "render", "verify"]
                if args.stage in ("all", s)]
        if not todo:
            sys.exit(f"--stage {args.stage} does not apply to Markdown input "
                     f"(available: md, translate, render, verify)")
    else:
        out = (args.out or Path.cwd() / f"{source.stem}.md-out").expanduser().resolve()
        note_name = args.note_name
        work = out / "md"
        todo = list(STAGES) if args.stage == "all" else [args.stage]
    work.mkdir(parents=True, exist_ok=True)

    render_flags = ["--zh-style", args.zh_style, "--page-markers", args.page_markers]
    if args.block_anchors:
        render_flags.append("--block-anchors")
    translate_flags = []
    if args.glossary:
        translate_flags += ["--glossary", str(args.glossary)]
    if args.translate_captions:
        translate_flags.append("--translate-captions")
    if args.model:
        translate_flags += ["--model", args.model]
    if args.config:
        translate_flags += ["--config", str(args.config)]

    print(f"source: {source}{'  (markdown)' if is_markdown else ''}")
    print(f"out   : {out}")
    print(f"stages: {', '.join(todo)}")

    for s in todo:
        if s == "translate" and args.no_translate:
            print("stage translate: skipped (--no-translate)")
            continue
        print(f"stage {s}")
        if s == "parse":
            stage_parse(source, out, args.tier, args.mineru_home, args.mineru_cmd)
        elif s == "md":
            if is_markdown:
                stage_md_from_markdown(source, out, render_flags, note_name)
            else:
                stage_md(source, out, render_flags, note_name)
        elif s == "meta":
            if not is_markdown:
                stage_meta(out)
        elif s == "translate":
            stage_translate(work, translate_flags, blocks_path=work / "blocks.jsonl")
        elif s == "render":
            if is_markdown:
                stage_render_markdown(source, out, render_flags, note_name)
            else:
                stage_render(source, out, render_flags, note_name)
        elif s == "verify":
            stage_verify(work, args.no_translate, blocks_path=work / "blocks.jsonl",
                         note=(markdown_note_path(source, note_name) if is_markdown else None))

    note_name_for_print = note_name or f"{source.stem}.md"
    note = markdown_note_path(source, note_name_for_print) if is_markdown \
        else (out / "md" / note_name_for_print)
    if note.exists():
        print(f"\nnote : {note}")
    if args.vault and not is_markdown:
        copy_to_vault(out, args.vault.expanduser().resolve(), source)
    elif args.vault:
        print("  vault copy: skipped for markdown input (assets already live beside it)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
