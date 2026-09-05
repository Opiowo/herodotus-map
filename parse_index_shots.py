#!/usr/bin/env python3
"""Turn screenshots of a bilingual book index into a place-name CSV.

Built for the index of 徐松岩's Herodotus/Thucydides/Xenophon translations,
whose entries look like:

    阿卡纳尼亚（Acarnania）城市，在西北希腊，II.10; VII.126。
    阿凯亚（Achaea）（1）伯罗奔尼撒的阿凯亚，VII.94...

Pipeline:
  1. OCR each image (PaddleOCR by default, Tesseract as a fallback).
  2. Re-flow OCR lines into entries -- an entry starts at a Chinese head word
     followed by a bracketed Latin name, and continues until the next one.
  3. Parse each entry into (chinese, latin, gloss, citations).
  4. Keep the geographic ones, drop people/gods, and write a CSV that
     herodotus_map.py's --zh-names can consume after id resolution.

The output still needs `resolve_zh_ids.py` to attach Pleiades ids -- OCR
gives names, not coordinates.

Usage:
    python parse_index_shots.py data/index_shots/            # OCR + parse
    python parse_index_shots.py data/index_shots/ --engine tesseract
    python parse_index_shots.py --text-only dump.txt         # skip OCR
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path

# --------------------------------------------------------------------------
# OCR back ends
# --------------------------------------------------------------------------

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}


def ocr_paddle(paths: list[Path]) -> list[str]:
    """PaddleOCR: markedly better than Tesseract on mixed CJK/Latin lines."""
    from paddleocr import PaddleOCR
    # enable_mkldnn=False: PaddleOCR 3.7 + paddle 3.3 crash in the oneDNN
    # kernel on this CPU ("ConvertPirAttribute2RuntimeAttribute not support").
    ocr = PaddleOCR(use_textline_orientation=False, lang="ch",
                    enable_mkldnn=False)
    from PIL import Image
    out: list[str] = []
    for i, p in enumerate(paths, 1):
        print(f"  [{i}/{len(paths)}] {p.name}", flush=True)
        im = Image.open(p)
        frags: list[dict] = []
        for top, band in slice_tall(im):
            tmp = p.parent / f".band_{p.stem}_{top}.png"
            band.save(tmp)
            try:
                for page in ocr.predict(str(tmp)):
                    texts = page.get("rec_texts") or []
                    boxes = page.get("rec_polys") or page.get("dt_polys") or []
                    for t, b in zip(texts, boxes):
                        ys = [pt[1] for pt in b]
                        xs = [pt[0] for pt in b]
                        frags.append({"y0": min(ys) + top, "y1": max(ys) + top,
                                      "x0": min(xs), "text": t})
            finally:
                tmp.unlink(missing_ok=True)
        out.extend(group_lines(frags))
    return out


# A whole-page screenshot can be 11,000px tall; PaddleOCR downscales anything
# over 4,000px, which blurs dense index type. Feeding it overlapping bands at
# native resolution keeps the detail -- and at native size each printed line
# is usually detected whole, so no fragment stitching is needed.
BAND_HEIGHT = 1600
BAND_OVERLAP = 120
MAX_NATIVE_SIDE = 4000


def slice_tall(im):
    """Yield (top_offset, band) covering a tall image, with overlap."""
    if im.height <= MAX_NATIVE_SIDE:
        yield 0, im
        return
    step = BAND_HEIGHT - BAND_OVERLAP
    for top in range(0, im.height, step):
        bottom = min(top + BAND_HEIGHT, im.height)
        yield top, im.crop((0, top, im.width, bottom))
        if bottom >= im.height:
            break


def group_lines(frags: list[dict]) -> list[str]:
    """Stitch OCR fragments back into visual lines.

    The detector splits one printed line into several boxes ("安诺派亚",
    "(Anopaea)", "山道，在德摩比利" ...). Sorting by a fixed y-quantum
    interleaves them wrongly, so instead group by vertical overlap: a
    fragment joins the current line when its box overlaps that line's
    vertical span by more than half its own height.
    """
    if not frags:
        return []
    frags.sort(key=lambda f: (f["y0"], f["x0"]))
    lines: list[list[dict]] = [[frags[0]]]
    for f in frags[1:]:
        cur = lines[-1]
        top = min(g["y0"] for g in cur)
        bot = max(g["y1"] for g in cur)
        h = max(1.0, f["y1"] - f["y0"])
        overlap = min(bot, f["y1"]) - max(top, f["y0"])
        if overlap > 0.5 * h:
            cur.append(f)
        else:
            lines.append([f])
    texts = ["".join(g["text"] for g in sorted(line, key=lambda g: g["x0"]))
             for line in lines]
    # Overlapping bands re-read the same lines; drop consecutive repeats.
    deduped: list[str] = []
    recent: list[str] = []
    for t in texts:
        key = re.sub(r"\s+", "", t)
        if key and key in recent:
            continue
        deduped.append(t)
        recent.append(key)
        recent = recent[-40:]
    return deduped


def ocr_tesseract(paths: list[Path]) -> list[str]:
    import pytesseract
    from PIL import Image
    out: list[str] = []
    for i, p in enumerate(paths, 1):
        print(f"  [{i}/{len(paths)}] {p.name}", flush=True)
        txt = pytesseract.image_to_string(Image.open(p), lang="chi_sim+eng")
        out.extend(l for l in txt.splitlines() if l.strip())
    return out


# --------------------------------------------------------------------------
# Entry re-flow and parsing
# --------------------------------------------------------------------------

CJK = r"一-鿿"
# An entry head: Chinese word(s), then a bracketed Latin name.
# Brackets may be full-width （） or half-width (), and OCR sometimes drops
# the space before them.
HEAD_RE = re.compile(
    rf"^\s*([{CJK}·〇A-Za-z]{{1,20}}?)\s*[（(]\s*([A-Za-z][^）)]{{0,60}}?)\s*[）)]"
)

# Roman-numeral book citations: I.46; VIII.27,33 -- also I. 46 with a space.
CITE_RE = re.compile(r"\b([IVX]{1,5})\s*\.\s*(\d[\d,\s—–—-]*)")

# Words that mark an entry as a person, not a place.
PERSON_HINTS = (
    "之子", "之父", "之女", "之妻", "之兄", "国王", "王，", "僭主", "将军",
    "诗人", "预言家", "祭司", "执政官", "监察官", "统帅", "指挥官", "英雄",
    "神", "女神", "传说中的", "作者", "哲学家", "医生", "画家", "雕刻家",
    "使者", "信使", "斯巴达人，", "雅典人，", "波斯人，", "一位",
)
# Words that mark a place.
PLACE_HINTS = (
    "城市", "城镇", "城邦", "地区", "岛屿", "岛", "河流", "河", "山", "海",
    "湾", "海角", "海峡", "平原", "村", "德莫", "神殿", "神庙", "圣域",
    "卫城", "堡垒", "要塞", "首都", "港", "泉", "湖", "半岛", "沙漠",
    "在", "位于",
)


def reflow(lines: list[str]) -> list[str]:
    """Join wrapped OCR lines back into one string per index entry."""
    entries: list[str] = []
    buf = ""
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        # A single letter alone on a line is the A/B/C section header.
        if len(line) <= 2 and re.fullmatch(r"[A-Za-z]{1,2}", line):
            continue
        if HEAD_RE.match(line) and buf:
            entries.append(buf)
            buf = line
        elif HEAD_RE.match(line):
            buf = line
        elif buf:
            buf += line
    if buf:
        entries.append(buf)
    return entries


def classify(gloss: str) -> str:
    """place / person / unknown, from the descriptive text after the name."""
    head = gloss[:40]
    if any(h in head for h in PERSON_HINTS):
        return "person"
    if any(h in gloss for h in PLACE_HINTS):
        return "place"
    return "unknown"


SPLIT_RE = re.compile(rf"(?<=[。;；])\s*(?=[{CJK}]{{1,20}}?\s*[（(]\s*[A-Za-z])")


def split_run_ons(entries: list[str]) -> list[str]:
    """Break lines that swallowed the next entry.

    A long index line can wrap in a way that puts two entries on one visual
    line. Split where a sentence ends and a new "中文（Latin）" head begins.
    """
    out: list[str] = []
    for e in entries:
        out.extend(part for part in SPLIT_RE.split(e) if part.strip())
    return out


def parse_entry(text: str) -> dict | None:
    m = HEAD_RE.match(text)
    if not m:
        return None
    zh, latin = m.group(1).strip(), m.group(2).strip()
    # Sub-entries wrap onto their own line as "在塞斯托斯（Sestos）...", so the
    # head word picks up a leading preposition. Strip it: the place is the
    # same one, and leaving it in creates a duplicate spelling.
    zh = re.sub(r"^(?:在|于|从|到|至|往|向|经|近)(?=[^\s])", "", zh) or zh
    rest = text[m.end():]
    # Stop the gloss at the next entry head, if one bled into this line.
    nxt = re.search(rf"[{CJK}]{{1,20}}?\s*[（(]\s*[A-Za-z][^）)]{{0,60}}?[）)]", rest)
    if nxt and nxt.start() > 0:
        rest = rest[:nxt.start()]
    # Strip the citation tail so the gloss stays readable.
    gloss = CITE_RE.sub("", rest)
    gloss = re.sub(r"[;；,，。\s]{2,}", " ", gloss).strip(" ;；,，。")
    cites = [f"{b}.{n.strip()}" for b, n in CITE_RE.findall(rest)]
    return {
        "zh": zh,
        "en": latin,
        "kind": classify(rest),
        "gloss": gloss[:120],
        "citations": "; ".join(cites[:8]),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("source", nargs="?", type=Path,
                    help="directory of screenshots (or a .txt with --text-only)")
    ap.add_argument("--engine", choices=["paddle", "tesseract"], default="paddle")
    ap.add_argument("--text-only", action="store_true",
                    help="source is an already-OCR'd text file")
    ap.add_argument("--out", type=Path, default=Path("data/index_parsed.csv"))
    ap.add_argument("--raw-out", type=Path, default=Path("data/index_ocr_raw.txt"),
                    help="where to save the raw OCR text, for checking")
    ap.add_argument("--keep-all", action="store_true",
                    help="keep person/unknown entries too (default: places only)")
    args = ap.parse_args()

    if not args.source:
        ap.error("give a screenshot directory, or a .txt with --text-only")

    if args.text_only:
        lines = args.source.read_text(encoding="utf-8").splitlines()
    else:
        if not args.source.is_dir():
            sys.exit(f"{args.source} is not a directory")
        paths = sorted(p for p in args.source.iterdir()
                       if p.suffix.lower() in IMAGE_SUFFIXES)
        if not paths:
            sys.exit(f"no images found in {args.source}")
        print(f"OCR: {len(paths)} image(s) with {args.engine}")
        lines = (ocr_paddle if args.engine == "paddle" else ocr_tesseract)(paths)
        args.raw_out.parent.mkdir(parents=True, exist_ok=True)
        args.raw_out.write_text("\n".join(lines), encoding="utf-8")
        print(f"  raw OCR -> {args.raw_out}  ({len(lines):,} lines)")

    entries = split_run_ons(reflow(lines))
    parsed = [e for e in (parse_entry(t) for t in entries) if e]
    places = [e for e in parsed if e["kind"] == "place"]
    people = [e for e in parsed if e["kind"] == "person"]
    unknown = [e for e in parsed if e["kind"] == "unknown"]
    print(f"entries: {len(entries):,} re-flowed, {len(parsed):,} parsed")
    print(f"  place={len(places):,}  person={len(people):,}  unknown={len(unknown):,}")

    rows = parsed if args.keep_all else places + unknown
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["zh", "en", "kind", "gloss", "citations"])
        w.writeheader()
        w.writerows(rows)
    print(f"  wrote {args.out}  ({len(rows):,} rows)")
    print("  next: python resolve_zh_ids.py  # attach Pleiades ids")


if __name__ == "__main__":
    main()
