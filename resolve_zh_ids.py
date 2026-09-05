#!/usr/bin/env python3
"""Attach Pleiades ids to parsed index entries, then merge into zh_names.csv.

parse_index_shots.py gives names; the map needs ids. This resolves each
Latin name the same way herodotus_map.py's matcher does (PINNED, then
ALIASES, then the gazetteer index), keeps only places that actually appear
on the map, and reports what it could not place so those can be checked by
hand rather than guessed at.

Usage:
    python resolve_zh_ids.py                          # dry run, prints a report
    python resolve_zh_ids.py --merge                  # write into data/zh_names.csv
    python resolve_zh_ids.py --source "徐松岩《历史》2018修订本索引"
"""

from __future__ import annotations

import argparse
import csv
from collections import defaultdict
from pathlib import Path

import pandas as pd

import herodotus_map as H

FIELDS = ["pleiades_id", "en", "zh", "source", "note"]


def build_resolver(places: dict, index: dict, on_map: set[str], counts: dict):
    def resolve(latin: str) -> list[str]:
        """Candidate pids for one printed Latin name, best first."""
        found: list[str] = []
        # The index prints variants as "Abydus, Abydos" or "Aega / Aege".
        forms = [f.strip() for f in latin.replace(",", "/").split("/") if f.strip()]
        for form in forms:
            low = form.lower()
            pin = H.PINNED.get(low)
            if pin and pin in on_map:
                found.append(pin)
                continue
            alias = H.ALIASES.get(low)
            if alias:
                found += [p for p in index.get(H.norm(alias), []) if p in on_map]
            found += [p for p in index.get(H.norm(form), []) if p in on_map]
        seen: list[str] = []
        for p in found:
            if p not in seen:
                seen.append(p)
        return sorted(seen, key=lambda p: -counts.get(p, 0))
    return resolve


def read_existing(path: Path) -> tuple[list[str], dict[str, dict]]:
    if not path.exists():
        return ["pleiades_id,en,zh,source,note"], {}
    raw = path.read_text(encoding="utf-8-sig").splitlines()
    header = [l for l in raw if l.startswith("#") or l.startswith("pleiades_id")]
    body = [l for l in raw if l and not l.startswith("#")
            and not l.startswith("pleiades_id")]
    rows = {r["pleiades_id"]: r for r in csv.DictReader(body, fieldnames=FIELDS)}
    return header, rows


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--parsed", type=Path, default=Path("data/index_parsed.csv"))
    ap.add_argument("--mentions", type=Path,
                    default=Path("output/herodotus_mentions.csv"))
    ap.add_argument("--zh-names", type=Path, default=Path("data/zh_names.csv"))
    ap.add_argument("--datadir", type=Path, default=Path("data"))
    ap.add_argument("--source", default="徐松岩译本索引",
                    help="value for the source column")
    ap.add_argument("--merge", action="store_true",
                    help="actually write into --zh-names (default: dry run)")
    ap.add_argument("--overwrite", action="store_true",
                    help="let these rows replace existing ones (e.g. Wikidata)")
    args = ap.parse_args()

    if not args.parsed.exists():
        raise SystemExit(f"{args.parsed} not found -- run parse_index_shots.py first")

    entries = list(csv.DictReader(args.parsed.open(encoding="utf-8-sig")))
    print(f"parsed entries: {len(entries):,}")

    places, index = H.load_gazetteer(args.datadir)
    df = pd.read_csv(args.mentions)
    counts = df.groupby(df["pleiades_id"].astype(str)).size().to_dict()
    on_map = set(counts)
    resolve = build_resolver(places, index, on_map, counts)

    hits: list[dict] = []
    ambiguous: list[tuple] = []
    misses: list[tuple] = []
    by_pid: dict[str, list[str]] = defaultdict(list)

    for e in entries:
        zh, latin = e.get("zh", "").strip(), e.get("en", "").strip()
        if not zh or not latin:
            continue
        cands = resolve(latin)
        if not cands:
            misses.append((zh, latin, e.get("gloss", "")))
            continue
        pid = cands[0]
        by_pid[pid].append(zh)
        if len(cands) > 1:
            ambiguous.append((zh, latin, [places[c].title for c in cands[:3]]))
        hits.append({"pleiades_id": pid, "en": places[pid].title, "zh": zh,
                     "source": args.source,
                     "note": (e.get("gloss") or "").strip()[:60]})

    # One pid claimed by several entries means the index distinguishes places
    # the map merges -- worth seeing rather than silently picking one.
    collisions = {p: n for p, n in by_pid.items() if len(set(n)) > 1}

    uniq: dict[str, dict] = {}
    for r in hits:
        uniq.setdefault(r["pleiades_id"], r)
    ranked = sorted(uniq.values(), key=lambda r: -counts.get(r["pleiades_id"], 0))

    print(f"  resolved : {len(ranked):,} unique places")
    print(f"  ambiguous: {len(ambiguous):,}   unresolved: {len(misses):,}")
    if collisions:
        print(f"  collisions ({len(collisions)}): one map place, several index entries")
        for pid, names in list(collisions.items())[:6]:
            print(f"     {pid} {places[pid].title[:26]:26} <- {sorted(set(names))}")

    print("\n  top resolved:")
    for r in ranked[:12]:
        print(f"     {r['pleiades_id']:>9} {r['zh']:12} {r['en'][:28]:28} "
              f"n={counts.get(r['pleiades_id'], 0)}")
    if misses:
        print(f"\n  unresolved (first 15 of {len(misses)}):")
        for zh, latin, gloss in misses[:15]:
            print(f"     {zh:12} {latin[:24]:24} {gloss[:30]}")

    if not args.merge:
        print("\n  dry run -- pass --merge to write into", args.zh_names)
        return

    header, existing = read_existing(args.zh_names)
    added = replaced = 0
    for r in ranked:
        pid = r["pleiades_id"]
        if pid in existing:
            if not args.overwrite and "徐松岩" in (existing[pid].get("source") or ""):
                continue                     # keep the index row already there
            if not args.overwrite:
                continue
            replaced += 1
        else:
            added += 1
        existing[pid] = r

    args.zh_names.parent.mkdir(parents=True, exist_ok=True)
    with open(args.zh_names, "w", encoding="utf-8", newline="") as fh:
        fh.write("\n".join(header) + "\n")
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        w.writerows(existing.values())
    print(f"\n  merged into {args.zh_names}: +{added} new, {replaced} replaced, "
          f"{len(existing)} total")


if __name__ == "__main__":
    main()
