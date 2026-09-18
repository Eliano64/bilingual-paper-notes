#!/usr/bin/env python
"""Stage 3: translate the translatable blocks of blocks.jsonl into Chinese.

Writes "zh" (body/title/footnote) and "caption_zh" (figure/table captions) back
into blocks.jsonl, so re-rendering is just: render.py --render-only.

    python translate.py out/dsh/md/blocks.jsonl --limit 20        # smoke test
    python translate.py out/dsh/md/blocks.jsonl --dry-run         # cost estimate
    python translate.py out/dsh/md/blocks.jsonl                   # full run

Design notes
------------
* Endpoint: flags > environment (BPN_* or OPENAI_*) > config file
  (.bilingual-paper-notes.json) > pi's own config. No key is ever stored by this script.
* Thinking must be disabled explicitly: DeepSeek enables it by default and
  spends the whole max_tokens budget on reasoning (measured: 60/60 tokens).
  Output tokens dominate cost, so leaving it on is a pure waste here.
* Everything structural (math, wikilinks, sup/sub, footnote refs) is masked to
  placeholders before the request and restored afterwards, then verified: a
  missing placeholder means the block failed and is not silently accepted.
* Cached in translate.sqlite keyed by hash(model + prompt + glossary + text),
  so re-runs only pay for what changed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

PH = "\u27e6{}\u27e7"          # ⟦0⟧ — rare enough that no model rewrites it
DEFAULT_GLOSSARY = Path(__file__).with_name("glossary.txt")

MASK_RULES = [
    (re.compile(r"\$\$.*?\$\$", re.S), "math"),
    (re.compile(r"\$[^$\n]+?\$"), "math"),
    (re.compile(r"\[\[[^\]]+\]\]"), "link"),
    (re.compile(r"<su[bp]>.*?</su[bp]>"), "script"),
    (re.compile(r"\[\^\d+\]"), "footnote"),
    # citation brackets are plain text at this stage (wikilinks are built at render
    # time), so the model would happily turn [1] into （1）and break the link
    (re.compile(r"\[\d+(?:\s*[,––—-]\s*\d+)*\]"), "cite"),
]

SYSTEM_TEMPLATE = """你是学术论文的翻译助手，把英文段落翻译成简体中文。

规则：
1. 学术语体，术语严格按下方术语表统一；术语表未覆盖的专业名词，优先选学术界通行译法。
2. 占位符 ⟦n⟧ 代表原文中的数学公式、内部链接、上下标、脚注引用等。必须原样保留，编号不变，位置不变；不得翻译、增删、改写或调整其周围的空格。
3. 保留 **加粗** 和 *斜体* 标记；保留原文的标点风格。
4. 代码标识符、函数名、包名（如 ctx.effect、fiber.target、@@store、require.cache）保持英文原样。
5. 不增删内容，不合并或拆分段落，不加译者注，不解释。
6. 每个编号必须单独给出一条译文，哪怕原文很短；禁止把相邻编号合并、省略或只译其一。

术语表（英文 = 中文）：
{glossary}

输出格式：只输出一个 JSON 对象，键是段落编号（字符串），值是中文译文。
必须覆盖输入的每一个编号，不要输出任何额外文字。
例如：{{"1": "第一段译文", "2": "第二段译文"}}"""


# --------------------------------------------------------------------------
# masking
# --------------------------------------------------------------------------

def mask(text: str) -> tuple[str, list[str]]:
    spans: list[str] = []

    def repl(m):
        spans.append(m.group(0))
        return PH.format(len(spans) - 1)

    for rx, _kind in MASK_RULES:
        text = rx.sub(repl, text)
    return text, spans


_BRACKETS = "\u27ea\u27eb\u27e6\u27e7"            # ⟪ ⟫ ⟦ ⟧
_FRAMING_RE = re.compile("[/\\\\]?\\s*\\d*\\s*[" + _BRACKETS + "]+")


def strip_framing(text: str) -> str:
    """Remove batch-framing delimiters the model echoed into its output.

    Seen in the wild: one translation ended with "\u27ea/1\u27eb" and another with
    "/1\u27e7" -- the model reused the *placeholder* brackets for the batch
    delimiter. Framing is never content, so dropping "brackets, optionally
    preceded by /digits" is safe and catches both spellings.
    """
    t = _FRAMING_RE.sub("", text)
    t = re.sub(r"[ \t]+\n", "\n", t)
    return re.sub(r"\n{3,}", "\n\n", t).strip()


def unmask(text: str, spans: list[str]) -> tuple[str, bool, bool]:
    """Restore placeholders.

    Returns (text, ok, duplicated). A *missing* placeholder means real content
    was dropped and the block is rejected; a *repeated* one only means the model
    restated the same formula while rephrasing (observed: "reads a $\\theta_n$
    that ... denies" -> "读取一个 $\\theta_n$，该 $\\theta_n$ 被 ... 拒绝"), which is
    faithful Chinese, so it is accepted and flagged for review.
    """
    ok, duplicated = True, False
    for i, span in enumerate(spans):
        tok = PH.format(i)
        n = text.count(tok)
        if n == 0:
            ok = False
        elif n > 1:
            duplicated = True
        text = text.replace(tok, span)
    # a renumbered/invented placeholder: ⟦12⟧ where only 0..n exist
    if re.search(r"\u27e6\d+\u27e7", text):
        ok = False
    # framing residue can only be recognized *after* restoration, because the
    # model reuses the placeholder brackets for the batch delimiter
    text = strip_framing(text)
    if "\u27e6" in text or "\u27e7" in text:
        ok = False
    return text, ok, duplicated


# --------------------------------------------------------------------------
# endpoint
# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
# endpoint resolution
# --------------------------------------------------------------------------
# Order: explicit flags > environment > config file > pi's own config.
# pi's config is consulted last so that this also works, unchanged, for people
# who do not use pi at all.

CONFIG_CANDIDATES = (".bilingual-paper-notes.json", "~/.config/bilingual-paper-notes/config.json",
                     "~/.bilingual-paper-notes.json")
ENV_PAIRS = (("BPN_BASE_URL", "BPN_API_KEY", "BPN_MODEL"),
             ("OPENAI_BASE_URL", "OPENAI_API_KEY", "OPENAI_MODEL"))


def load_config(explicit: Path | None) -> dict:
    paths = [explicit] if explicit else [Path(c).expanduser() for c in CONFIG_CANDIDATES]
    for p in paths:
        if p and p.exists():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except Exception as e:
                print(f"warning: ignoring bad config {p}: {e}")
    return {}


def from_pi_config(provider: str | None, model: str | None):
    """Reuse a provider already configured for pi, if there is one."""
    root = Path(os.environ.get("PI_AGENT_DIR", "~/.pi/agent")).expanduser()
    try:
        provs = json.loads((root / "models.json").read_text(encoding="utf-8")).get("providers", {})
    except Exception:
        return {}
    if not provs:
        return {}
    want = provider
    if not want:
        try:
            want = json.loads((root / "settings.json").read_text(encoding="utf-8")).get("defaultProvider")
        except Exception:
            want = None
    p = provs.get(want) if want else None
    if not (p and p.get("apiKey")):
        p = next((v for v in provs.values() if v.get("apiKey")), None)
    if not p:
        return {}
    return {"base_url": p.get("baseUrl"), "api_key": p.get("apiKey"),
            "model": model or (want and provs.get(want) is p and _pi_default_model(root))}


def _pi_default_model(root: Path):
    try:
        return json.loads((root / "settings.json").read_text(encoding="utf-8")).get("defaultModel")
    except Exception:
        return None


def load_endpoint(cfg: dict, model: str | None, base_url: str | None,
                  api_key: str | None, provider: str | None = None):
    base = key = None
    for ebase, ekey, emodel in ENV_PAIRS:
        if not base and os.environ.get(ebase):
            base = os.environ[ebase]
        if not key and os.environ.get(ekey):
            key = os.environ[ekey]
        model = model or os.environ.get(emodel)
    if not key:
        base = base or cfg.get("base_url")
        key = cfg.get("api_key") or cfg.get("apiKey")
        model = model or cfg.get("model")
    if not key:
        p = from_pi_config(provider, model)
        base = base or p.get("base_url")
        key = p.get("api_key")
        model = model or p.get("model")
    base = (base_url or base or "").rstrip("/")
    key = api_key or key
    if not base:
        sys.exit("no API base url: pass --base-url, set BPN_BASE_URL, or add it to "
                 + str(CONFIG_CANDIDATES[0]))
    if not key:
        sys.exit("no API key: pass --api-key, set BPN_API_KEY / OPENAI_API_KEY, "
                 "or add \"api_key\" to " + str(CONFIG_CANDIDATES[0]))
    if not model:
        sys.exit("no model: pass --model, set BPN_MODEL, or add \"model\" to "
                 + str(CONFIG_CANDIDATES[0]))
    return base, key, model


def price_table(model: str, cfg: dict | None = None) -> dict:
    """Per-1M-token prices. Optional: only used to print a cost estimate."""
    cfg = cfg or {}
    if cfg.get("price"):
        return cfg["price"]
    try:   # pi records prices for its own catalogue; reuse if present
        root = Path(os.environ.get("PI_AGENT_DIR", "~/.pi/agent")).expanduser()
        data = json.loads((root / "models-store.json").read_text(encoding="utf-8"))
        for _prov, body in data.items():
            for m in body.get("models", []):
                if m.get("id") == model:
                    return m.get("cost") or {}
    except Exception:
        pass
    return {}


# --------------------------------------------------------------------------
# cache
# --------------------------------------------------------------------------

class Cache:
    def __init__(self, path: Path):
        self.lock = threading.Lock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute("CREATE TABLE IF NOT EXISTS t (k TEXT PRIMARY KEY, v TEXT, at REAL)")
        self.db.commit()

    def get(self, key: str):
        with self.lock:
            row = self.db.execute("SELECT v FROM t WHERE k=?", (key,)).fetchone()
        return row[0] if row else None

    def put(self, key: str, value: str):
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO t VALUES (?,?,?)", (key, value, time.time()))
            self.db.commit()


# --------------------------------------------------------------------------
# translation
# --------------------------------------------------------------------------

class Stats:
    def __init__(self):
        self.lock = threading.Lock()
        self.calls = 0
        self.hits = 0
        self.retries = 0
        self.failed: list[str] = []
        self.duplicated: list[str] = []
        self.prompt_tokens = 0
        self.cached_tokens = 0
        self.completion_tokens = 0
        self.retry_completion_tokens = 0   # output paid again on a re-ask
        self.escalations = 0               # units re-asked with thinking enabled
        self.escalation_tokens = 0
        self.units = 0

    def add(self, **kw):
        with self.lock:
            for k, v in kw.items():
                setattr(self, k, getattr(self, k) + v)


def parse_json_loose(raw: str) -> dict:
    """Parse the reply, tolerating several objects in a row.

    The model sometimes answers with one JSON object per line instead of a single
    object (measured as "Extra data: line 3 column 1"). Merging them is safe:
    keys are unit numbers and the first value wins.
    """
    try:
        out = json.loads(raw)
        if isinstance(out, dict):
            return out
    except Exception:
        pass
    dec = json.JSONDecoder()
    merged: dict = {}
    i, n = 0, len(raw)
    while i < n:
        while i < n and raw[i] in " \t\r\n,":
            i += 1
        if i >= n:
            break
        try:
            obj, j = dec.raw_decode(raw, i)
        except Exception:
            break
        if isinstance(obj, dict):
            for k, v in obj.items():
                merged.setdefault(str(k), v)
        i = j
    return merged


def call_api(base: str, key: str, model: str, system: str, user: str,
             timeout: int = 300, thinking: bool = False) -> tuple[str, dict]:
    body = {
        "model": model,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "temperature": 0,
        "max_tokens": 8192,
        "response_format": {"type": "json_object"},
        "thinking": {"type": "enabled" if thinking else "disabled"},
    }
    req = urllib.request.Request(
        f"{base}/chat/completions",
        data=json.dumps(body, ensure_ascii=False).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.load(r)
    content = (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
    return content, data.get("usage") or {}


ESCALATION_CAP = 20


def translate_escalated(unit, base, key, model, system, cache, stats):
    """Last resort for a unit that keeps dropping a placeholder: ask once more
    with thinking enabled.

    Measured on the one block that failed every other way: with thinking off the
    model omits the same formula in every attempt (finish_reason=stop, so it is a
    rephrasing omission, not truncation); with thinking on it keeps it, at the
    price of ~170 -> ~6000 output tokens. Worth it for a handful of units per
    paper, which is why there is a cap.
    """
    bid, field, masked, spans = unit
    if stats.escalations >= ESCALATION_CAP:
        return None
    stats.add(escalations=1)
    try:
        raw, usage = call_api(base, key, model, system,
                              f"\u27ea1\u27eb\n{masked}\n\u27ea/1\u27eb", thinking=True)
    except Exception as e:
        print(f"  ! escalation request failed ({e})", file=sys.stderr)
        return None
    tokens = usage.get("completion_tokens", 0)
    stats.add(calls=1, retry_completion_tokens=tokens, escalation_tokens=tokens)
    zh_masked = parse_json_loose(raw).get("1")
    if not zh_masked:
        return None
    zh, ok, dup = unmask(str(zh_masked), spans)
    if not ok:
        return None
    if dup:
        with stats.lock:
            stats.duplicated.append(bid)
    cache.put(cache_key(model, system, masked), str(zh_masked))
    return (bid, field, zh)


def translate_batch(batch, base, key, model, system, cache, stats, attempts=3, depth=0):
    """batch: list of (block_id, field, masked_text, spans)

    A batch that comes back incomplete is *not* re-sent whole. Whatever returned
    valid is kept, and only the missing units are asked for again (renumbered, so
    the request stays a valid 1..n list). With thinking disabled the model
    occasionally merges neighbouring short blocks, so the units left over are
    retried in steadily smaller requests.
    """
    done: list[tuple[str, str, str]] = []
    todo = list(batch)
    # per-unit record of the placeholders the last attempt dropped, fed back as an
    # explicit instruction: the usual cause is the model rephrasing a sentence
    # until the formula no longer fits, which a blind retry reproduces faithfully
    # (measured: one block dropped the same \\mathcal{V}_k three times in a row,
    # and mentioned it explicitly succeeded three times out of three)
    dropped: dict[str, list[int]] = {}
    reasons: dict[str, str] = {}      # bid -> why the last attempt rejected it
    transport_errors = 0
    while todo and attempts > 0:
        payload = "\n\n".join(f"\u27ea{i + 1}\u27eb\n{u[2]}\n\u27ea/{i + 1}\u27eb"
                              for i, u in enumerate(todo))
        notes = []
        for i, u in enumerate(todo, 1):
            why = reasons.get(u[0])
            if why:
                notes.append(f"第 {i} 段的上一版{why}。")
        if notes:
            payload = ("补充要求：" + "".join(notes)
                       + "重译时必须让每一个 \u27e6n\u27e7 都在译文中原样出现一次；"
                         "可以调整句式，但不得省略或改写任何 \u27e6n\u27e7，也不得遗漏任何一段。\n\n"
                       + payload)
        try:
            raw, usage = call_api(base, key, model, system, payload)
        except Exception as e:
            # a network hiccup must not consume the semantic retry budget
            stats.add(retries=1, calls=1)
            transport_errors += 1
            print(f"  ! request failed ({e}); retry {transport_errors}", file=sys.stderr)
            if transport_errors > 4:
                attempts -= 1
            time.sleep(min(2 * transport_errors, 10))
            continue
        attempts -= 1
        out_tokens = usage.get("completion_tokens", 0)
        stats.add(calls=1,
                  prompt_tokens=usage.get("prompt_tokens", 0),
                  cached_tokens=usage.get("prompt_cache_hit_tokens", 0),
                  completion_tokens=out_tokens)
        if done:
            # this request only existed to re-ask for the leftovers
            stats.add(retry_completion_tokens=out_tokens)
        try:
            out = parse_json_loose(raw)
            if not out:
                raise ValueError("no JSON object in the response")
        except Exception as e:
            stats.add(retries=1)
            print(f"  ! unusable response ({e}); retrying", file=sys.stderr)
            for u in todo:
                reasons.setdefault(u[0], "没有返回可用的 JSON 结果")
            continue

        leftovers = []
        for i, (bid, field, masked, spans) in enumerate(todo):
            zh_masked = out.get(str(i + 1))
            if not (zh_masked and str(zh_masked).strip()):
                reasons[bid] = "缺少译文（没有为它返回任何结果）"
                leftovers.append((bid, field, masked, spans))
                continue
            zh, ok, dup = unmask(str(zh_masked), spans)
            if not ok:
                # remember exactly which ones went missing so the next attempt can
                # say so instead of repeating the same mistake
                miss = [j for j, _s in enumerate(spans) if PH.format(j) not in str(zh_masked)]
                if miss:
                    dropped[bid] = miss
                    toks = "\u3001".join(PH.format(j) for j in miss[:6])
                    reasons[bid] = f"译文漏掉了 {toks}，这是不可接受的"
                else:
                    reasons[bid] = "译文里有多余或编号错误的 \u27e6n\u27e7 占位符"
                leftovers.append((bid, field, masked, spans))
                continue
            if dup:
                with stats.lock:
                    stats.duplicated.append(bid)
            cache.put(cache_key(model, system, masked), str(zh_masked))
            dropped.pop(bid, None)
            reasons.pop(bid, None)
            done.append((bid, field, zh))

        if not leftovers:
            return done
        stats.add(retries=1)
        todo = leftovers
        if attempts:
            time.sleep(min(2 ** (3 - attempts), 6))

    if todo:
        # a last resort before giving up: thinking on, for single units only
        if len(todo) <= 2:
            for unit in list(todo):
                r = translate_escalated(unit, base, key, model, system, cache, stats)
                if r:
                    done.append(r)
                    todo.remove(unit)
        # still stuck: split the remainder, because merging is what breaks a batch
        if todo and len(todo) > 1 and depth < 4:
            mid = len(todo) // 2
            done += translate_batch(todo[:mid], base, key, model, system, cache,
                                    stats, attempts=2, depth=depth + 1)
            done += translate_batch(todo[mid:], base, key, model, system, cache,
                                    stats, attempts=2, depth=depth + 1)
        elif todo:
            with stats.lock:
                stats.failed.extend(u[0] for u in todo)
            for u in todo:
                print(f"  ! {u[0]} failed: {reasons.get(u[0], 'unknown reason')}",
                      file=sys.stderr)
    return done


def cache_key(model: str, system: str, masked: str) -> str:
    h = hashlib.sha256()
    h.update(f"{model}\n".encode())
    h.update(system.encode())
    h.update(masked.encode())
    return h.hexdigest()


# --------------------------------------------------------------------------

def build_units(blocks, captions=False):
    """Translatable units.

    Captions are off by default: the user wants structural labels (headings,
    figure/table names) to stay in the source language. --translate-captions
    turns them back on; the render stage already knows how to show caption_zh.

    Blocks that are nothing but a formula/symbol are skipped: there is no prose
    to translate, and asking for it made the model emit one JSON object per line
    (their batches were the only ones that failed in a full run).
    """
    units = []
    for b in blocks:
        if b.get("translatable") and (b.get("text") or "").strip():
            masked, _spans = mask(b["text"])
            if len(re.findall(r"[A-Za-z]", masked)) <= 3:
                b["translatable"] = False
                flags = b.setdefault("flags", [])
                if "math_only" not in flags:
                    flags.append("math_only")
                continue
            units.append((b["id"], "text", b["text"]))
        if captions and b.get("type") in ("table", "image") and (b.get("caption") or "").strip():
            units.append((b["id"], "caption", b["caption"]))
    return units


def make_batches(masked_units, max_chars=3000, max_units=5):
    batches, cur, size = [], [], 0
    for u in masked_units:
        n = len(u[2])
        if cur and (size + n > max_chars or len(cur) >= max_units):
            batches.append(cur)
            cur, size = [], 0
        cur.append(u)
        size += n
    if cur:
        batches.append(cur)
    return batches


def read_glossary(path: Path | None) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path or not Path(path).exists():
        return out
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        if k.strip():
            out[k.strip().lower()] = v.strip()
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("blocks", type=Path, help="blocks.jsonl produced by render.py")
    ap.add_argument("--glossary", type=Path, default=None,
                    help="extra domain glossary; merged over the built-in one "
                         "(the built-in structural terms must stay in effect or "
                         "Chinese cross-references stop being linked)")
    ap.add_argument("--model", default=None)
    ap.add_argument("--base-url", default=None)
    ap.add_argument("--api-key", default=None)
    ap.add_argument("--config", type=Path, default=None,
                    help="json with base_url/api_key/model/price; defaults to "
                         "./.bilingual-paper-notes.json then ~/.config/bilingual-paper-notes/config.json")
    ap.add_argument("--provider", default=None,
                    help="when falling back to pi's config, which provider to use")
    ap.add_argument("--batch-units", type=int, default=5,
                    help="units per request (smaller = fewer merge failures, more calls)")
    ap.add_argument("--batch-chars", type=int, default=3000,
                    help="max characters per request")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--limit", type=int, default=0, help="only the first N units (smoke test)")
    ap.add_argument("--dry-run", action="store_true", help="estimate cost without calling the API")
    ap.add_argument("--force", action="store_true", help="ignore the cache")
    ap.add_argument("--translate-captions", action="store_true",
                    help="also translate figure/table captions (off by default)")
    args = ap.parse_args(argv)

    blocks = [json.loads(l) for l in args.blocks.read_text(encoding="utf-8").splitlines() if l.strip()]
    # the built-in glossary always applies (it carries the structural terms the
    # render stage depends on); a user glossary is merged on top of it
    gloss = read_glossary(DEFAULT_GLOSSARY)
    if args.glossary:
        gloss.update(read_glossary(args.glossary))
    glossary = "\n".join(f"{k} = {v}" for k, v in gloss.items())
    system = SYSTEM_TEMPLATE.format(glossary=glossary or "（无）")

    units = build_units(blocks, captions=args.translate_captions)
    if args.limit:
        units = units[:args.limit]

    masked_units = []
    for bid, field, text in units:
        m, spans = mask(text)
        masked_units.append((bid, field, m, spans))
    batches = make_batches(masked_units, max_chars=args.batch_chars, max_units=args.batch_units)

    cache = Cache(args.blocks.parent / "translate.sqlite")
    cfg = load_config(args.config)
    base, key, model = load_endpoint(cfg, args.model, args.base_url, args.api_key,
                                     args.provider)

    # split into cached and to-do, restoring the cached ones immediately
    todo: list[list] = []
    results: dict[tuple[str, str], str] = {}
    hits = 0
    stats = Stats()
    for batch in batches:
        keep = []
        for bid, field, masked, spans in batch:
            hit = None if args.force else cache.get(cache_key(model, system, masked))
            if hit is None:
                keep.append((bid, field, masked, spans))
                continue
            zh, ok, dup = unmask(hit, spans)
            if not ok:
                # a cached value that no longer validates must count as a miss:
                # silently accepting it is how content disappears unnoticed
                keep.append((bid, field, masked, spans))
                continue
            if dup:
                stats.duplicated.append(bid)
            hits += 1
            results[(bid, field)] = zh
        if keep:
            todo.append(keep)

    print(f"units={len(units)}  batches={len(todo)}  cache_hits={hits}")
    if args.dry_run:
        pin = sum(len(u[2]) for b in todo for u in b)
        prefix = len(system) / 4 * len(todo)
        print(f"dry-run: ~{pin / 4 + prefix:,.0f} input tokens, ~{pin / 4 * 1.95:,.0f} output tokens")
        print("         (prompt prefix is identical across calls, so DeepSeek caches it after the first)")
        return 0

    t0 = time.time()
    if todo:
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futs = [ex.submit(translate_batch, b, base, key, model, system, cache, stats)
                    for b in todo]
            for i, f in enumerate(futs, 1):
                for bid, field, zh in f.result():
                    results[(bid, field)] = zh
                print(f"\r  batch {i}/{len(futs)}  calls={stats.calls} retries={stats.retries} "
                      f"failed={len(stats.failed)}", end="", file=sys.stderr)
            print(file=sys.stderr)

    for b in blocks:
        if (b["id"], "text") in results:
            b["zh"] = results[(b["id"], "text")]
        if (b["id"], "caption") in results:
            b["caption_zh"] = results[(b["id"], "caption")]
        if b["id"] in stats.failed:
            b["flags"].append("translate_failed")
        if b["id"] in stats.duplicated:
            b["flags"].append("translate_dup_placeholder")

    args.blocks.write_text(
        "\n".join(json.dumps(b, ensure_ascii=False) for b in blocks) + "\n", encoding="utf-8")

    meta_path = args.blocks.parent / "meta.json"
    if meta_path.exists():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta["translated_by"] = model
        meta["translated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")

    price = price_table(model, cfg)
    fresh = max(0, stats.prompt_tokens - stats.cached_tokens)
    cost = (fresh * price.get("input", 0) + stats.cached_tokens * price.get("cacheRead", 0)
            + stats.completion_tokens * price.get("output", 0)) / 1e6
    print(f"done in {time.time() - t0:.0f}s | translated={len(results)}/{len(units)} "
          f"cache_hits={hits} failed={len(stats.failed)} dup_placeholder={len(stats.duplicated)}")
    calls_per_batch = stats.calls / max(1, len(batches))
    retry_share = (stats.retry_completion_tokens / stats.completion_tokens * 100
                   if stats.completion_tokens else 0)
    print(f"calls: {stats.calls} for {len(batches)} batches ({calls_per_batch:.2f}x)"
          + (f" | re-asked output {stats.retry_completion_tokens:,} tok "
             f"= {retry_share:.0f}% of output" if stats.retry_completion_tokens else "")
          + (f" | escalated {stats.escalations} unit(s) with thinking on "
             f"({stats.escalation_tokens:,} tok)" if stats.escalations else ""))
    print(f"tokens: prompt={stats.prompt_tokens:,} (cached {stats.cached_tokens:,}) "
          f"completion={stats.completion_tokens:,}"
          + (f" | est. cost={cost:.4f}" if price else " | cost: no price table for this model"))
    if stats.failed:
        print(f"failed block ids: {stats.failed[:10]}")
    return 1 if stats.failed else 0


if __name__ == "__main__":
    sys.exit(main())
