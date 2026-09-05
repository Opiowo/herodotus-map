#!/usr/bin/env python3
"""Fetch Chinese place names from Wikidata, keyed by Pleiades id.

A fallback source for data/zh_names.csv: Wikidata is CC0, so its labels can
be redistributed freely, but its style differs from a published translation
(it says 古雅典 "ancient Athens" where 徐松岩 says plain 雅典). Rows written
here are marked source=Wikidata and are always overridden by index rows.

Usage:
    python fetch_wikidata_zh.py                    # all places on the map
    python fetch_wikidata_zh.py --limit 50         # try a subset first
    python fetch_wikidata_zh.py --merge            # merge into data/zh_names.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
from pathlib import Path

import pandas as pd
import requests

API = "https://www.wikidata.org/w/api.php"
# Characters that only occur in traditional forms; used to prefer a
# simplified alias when offering a second spelling.
TRADITIONAL = re.compile(r"[亞於們區爾龍國學東現無為與來時發後開關聯樣體羅島陸戰澤]")
UA = "herodotus-map/1.0 (academic research; contact via repo)"
# Preference order: a plain simplified label beats the generic zh one.
LANGS = ["zh-cn", "zh-hans", "zh", "zh-sg", "zh-my"]


def get(session: requests.Session, params: dict, tries: int = 6):
    """GET with backoff.

    Retries 429 (rate limit) and 5xx (the API returns 503 under load, which
    killed an earlier run after all the slow lookups had already succeeded).
    """
    delay = 1.0
    for attempt in range(tries):
        try:
            r = session.get(API, params=params, timeout=60)
        except requests.RequestException:
            if attempt == tries - 1:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 60)
            continue
        if r.status_code == 429 or r.status_code >= 500:
            time.sleep(delay)
            delay = min(delay * 2, 60)
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError(f"API kept failing after {tries} tries")


def qid_for_pleiades(pid: str, session: requests.Session) -> str | None:
    """Find the Wikidata item carrying this Pleiades id (property P1584)."""
    d = get(session, {
        "action": "query", "list": "search", "format": "json",
        "srsearch": f"haswbstatement:P1584={pid}", "srlimit": 1})
    hits = d.get("query", {}).get("search", [])
    return hits[0]["title"] if hits else None


def labels_for(qids: list[str], session: requests.Session) -> dict[str, dict]:
    """Chinese labels + aliases for up to 50 items per call."""
    out: dict[str, dict] = {}
    for i in range(0, len(qids), 50):
        chunk = qids[i:i + 50]
        try:
            d = get(session, {
                "action": "wbgetentities", "ids": "|".join(chunk),
                "props": "labels|aliases", "languages": "|".join(LANGS + ["en"]),
                "format": "json"})
        except Exception as exc:        # one bad chunk shouldn't lose the rest
            print(f"  ! labels chunk {i // 50}: {type(exc).__name__}")
            continue
        for qid, e in d.get("entities", {}).items():
            labs = {k: v["value"] for k, v in e.get("labels", {}).items()}
            zh = next((labs[l] for l in LANGS if l in labs), None)
            if not zh:
                continue
            aliases = []
            for l in LANGS:
                aliases += [a["value"] for a in e.get("aliases", {}).get(l, [])]
            out[qid] = {"zh": zh, "en": labs.get("en", ""),
                        "aliases": list(dict.fromkeys(aliases))[:3]}
        time.sleep(0.2)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mentions", type=Path,
                    default=Path("output/herodotus_mentions.csv"))
    ap.add_argument("--out", type=Path,
                    default=Path("data/zh_wikidata.csv"))
    ap.add_argument("--zh-names", type=Path, default=Path("data/zh_names.csv"))
    ap.add_argument("--limit", type=int, default=0,
                    help="only process the N most-mentioned places")
    ap.add_argument("--merge", action="store_true",
                    help="merge results into --zh-names (index rows win)")
    args = ap.parse_args()

    if not args.mentions.exists():
        sys.exit(f"{args.mentions} not found -- run herodotus_map.py first")

    df = pd.read_csv(args.mentions)
    counts = df.groupby(df["pleiades_id"].astype(str)).size()
    counts = counts.sort_values(ascending=False)
    pids = list(counts.index)
    uniq = df.drop_duplicates("pleiades_id").copy()
    uniq["pid"] = uniq["pleiades_id"].astype(str)
    titles = uniq.set_index("pid")["place"].to_dict()
    if args.limit:
        pids = pids[:args.limit]

    # Skip places already carrying a translation from a published index.
    have: set[str] = set()
    if args.zh_names.exists():
        with open(args.zh_names, encoding="utf-8-sig") as fh:
            rows = [l for l in fh if not l.lstrip().startswith("#")]
        for r in csv.DictReader(rows):
            if (r.get("pleiades_id") or "").strip() and (r.get("zh") or "").strip():
                have.add(r["pleiades_id"].strip())
    todo = [p for p in pids if p not in have]
    print(f"places on map: {len(pids):,}   already named: {len(have):,}   "
          f"to fetch: {len(todo):,}")

    session = requests.Session()
    session.headers["User-Agent"] = UA

    # Lookups are slow (rate limits force backoff), so the pid->qid map is
    # cached: an interrupted run resumes instead of starting over.
    cache_path = args.out.with_suffix(".qids.json")
    pid_to_qid: dict[str, str] = {}
    if cache_path.exists():
        pid_to_qid = json.loads(cache_path.read_text())
        print(f"  resuming with {len(pid_to_qid):,} cached lookups")

    pending = [p for p in todo if p not in pid_to_qid]
    for i, pid in enumerate(pending, 1):
        try:
            qid = qid_for_pleiades(pid, session)
        except Exception as e:                      # transient API failure
            print(f"  ! {pid}: {type(e).__name__}")
            continue
        pid_to_qid[pid] = qid or ""                 # "" = looked up, no item
        if i % 25 == 0:
            found = sum(1 for v in pid_to_qid.values() if v)
            print(f"  looked up {i}/{len(pending)}  (matched {found})")
            cache_path.write_text(json.dumps(pid_to_qid))
        time.sleep(0.25)                            # be polite to the API
    cache_path.write_text(json.dumps(pid_to_qid))
    pid_to_qid = {k: v for k, v in pid_to_qid.items() if v}
    print(f"  Wikidata items found: {len(pid_to_qid):,}")

    labels = labels_for(list(pid_to_qid.values()), session)
    print(f"  with a Chinese label: {len(labels):,}")

    out_rows = []
    for pid, qid in pid_to_qid.items():
        info = labels.get(qid)
        if not info:
            continue
        # The primary label is what Wikidata editors settled on; aliases are
        # a grab-bag (traditional forms, descriptive phrases, rare variants).
        # Keep the label first and offer at most one simplified alias after
        # it, so the search box gets a second spelling without the noise.
        zh = info["zh"]
        alt = next((a for a in info["aliases"]
                    if a != zh and not TRADITIONAL.search(a)
                    and len(a) <= len(zh) + 2), None)
        if alt:
            zh = f"{zh}/{alt}"
        out_rows.append({
            "pleiades_id": pid,
            "en": titles.get(pid, info["en"]),
            "zh": zh,
            "source": "Wikidata",
            "note": f"{qid} · {counts.get(pid, 0)} mention(s)",
        })
    out_rows.sort(key=lambda r: -counts.get(r["pleiades_id"], 0))

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["pleiades_id", "en", "zh", "source", "note"])
        w.writeheader()
        w.writerows(out_rows)
    print(f"  wrote {args.out}  ({len(out_rows):,} rows)")

    if args.merge:
        merge_into(args.zh_names, out_rows)


def merge_into(path: Path, new_rows: list[dict]) -> None:
    """Add Wikidata rows to zh_names.csv without touching index-sourced ones."""
    raw = path.read_text(encoding="utf-8-sig").splitlines() if path.exists() else []
    header = [l for l in raw if l.startswith("#") or l.startswith("pleiades_id")]
    body = [l for l in raw if l and not l.startswith("#")
            and not l.startswith("pleiades_id")]
    fields = ["pleiades_id", "en", "zh", "source", "note"]
    existing = {r["pleiades_id"]: r
                for r in csv.DictReader(body, fieldnames=fields)}
    added = 0
    for r in new_rows:
        if r["pleiades_id"] not in existing:        # never overwrite an index row
            existing[r["pleiades_id"]] = r
            added += 1
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write("\n".join(header) + "\n")
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writerows(existing.values())
    print(f"  merged into {path}: +{added} new rows, {len(existing)} total")


if __name__ == "__main__":
    main()
