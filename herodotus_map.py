#!/usr/bin/env python3
"""
Herodotus place-name map
========================

Pipeline:
  1. Download the Macaulay translation of Herodotus from Project Gutenberg
     (Vol 1 = Books I-IV, Vol 2 = Books V-IX). Public domain, 1890.
  2. Download the Pleiades gazetteer daily dumps (names + places) for
     ancient-world coordinates. CC-BY, Ancient World Mapping Center / ISAW.
  3. Match capitalised tokens in the text against the gazetteer, with
     Greek<->Latin transliteration normalisation (Miletos ~ Miletus).
  4. Write an interactive Folium map (one toggleable layer per Book) plus a
     tidy CSV of every mention for further analysis.

All downloads are cached in --datadir so you only pay for them once.

Usage:
    python herodotus_map.py                 # full run
    python herodotus_map.py --top 30        # also print the 30 most-mentioned places
"""

from __future__ import annotations

import argparse
import csv
import gzip
import io
import json
import re
import sys
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from itertools import combinations
from pathlib import Path

import pandas as pd
import requests

# --------------------------------------------------------------------------
# Sources
# --------------------------------------------------------------------------

GUTENBERG = {
    # ebook id -> (label, books contained)
    2707: ("Volume 1", ["I", "II", "III", "IV"]),
    2456: ("Volume 2", ["V", "VI", "VII", "VIII", "IX"]),
}
GUTENBERG_URL = "https://www.gutenberg.org/cache/epub/{eid}/pg{eid}.txt"

# Thucydides, Peloponnesian War (Crawley translation) -- the comparison
# corpus. One generation later, one war, a far tighter geographic range.
THUCYDIDES_EID = 7142

PLEIADES_BASE = "https://atlantides.org/downloads/pleiades/dumps"
PLEIADES_NAMES = f"{PLEIADES_BASE}/pleiades-names-latest.csv.gz"
PLEIADES_PLACES = f"{PLEIADES_BASE}/pleiades-places-latest.csv.gz"

# Keep only place categories that make sense as "somewhere Herodotus talks about".
FEATURE_TYPES = {
    "settlement", "settlement-modern", "region", "province", "island", "river",
    "mountain", "people", "lake", "bay", "cape", "plain", "pass", "valley",
    "port", "fort", "temple", "sanctuary", "spring", "well", "canal", "strait",
    "estate", "urban", "oasis", "coast", "desert", "delta", "gulf", "peninsula",
}

# Rough ancient-world envelope: Iberia -> Indus, Nubia -> Scythia.
BBOX = dict(min_lon=-12.0, max_lon=82.0, min_lat=12.0, max_lat=58.0)

# Personal / divine names that collide with gazetteer entries.
# Extend freely -- this is the main lever on precision.
STOPWORDS = {
    "cyrus", "darius", "xerxes", "croesus", "cambyses", "alexander", "philip",
    "leonidas", "themistocles", "aristagoras", "histiaeus", "miltiades",
    "solon", "homer", "zeus", "apollo", "athene", "athena", "artemis",
    "demeter", "poseidon", "hera", "heracles", "hercules", "dionysos",
    "dionysus", "hermes", "aphrodite", "helen", "paris", "io", "medea",
    "perseus", "danae", "cadmus", "minos", "midas", "gyges", "amasis",
    "psammetichos", "psammetichus", "polycrates", "pausanias", "cleomenes",
    "demaratos", "demaratus", "mardonius", "datis", "harpagus", "otanes",
    "artemisia", "hellen", "ion", "dorus", "aeolus", "pelops", "theseus",
    "orestes", "agamemnon", "menelaus", "priam", "hector", "achilles",
    "sesostris", "cheops", "rhampsinitus", "nitocris", "semiramis",
    # Generic words that collide with real gazetteer entries. "Barbarians"
    # was matching Barbaros, a river in Albania, 205 times.
    "barbarian", "barbarians", "barbaros", "greek", "greeks", "hellenes",
    "island", "islands", "islanders", "continent", "river", "mountain",
}

# English exonyms -> a form the gazetteer actually carries.
# Values are looked up through norm(), so any spelling in the same family works.
ALIASES = {
    "athens": "Athenai", "egypt": "Aigyptos", "thebes": "Thebai",
    "corinth": "Korinthos", "troy": "Ilion", "sparta": "Sparte",
    "danube": "Ister", "ethiopia": "Aithiopia", "persia": "Persis",
    "carthage": "Karkhedon", "syracuse": "Syrakousai", "marathon": "Marathon",
    "asia minor": "Asia", "the nile": "Neilos", "euxine": "Pontos Euxeinos",
    # Macaulay's spellings that the gazetteer files under another form.
    # Found by cross-checking 徐松岩's index, which prints both variants.
    "egina": "Aigina", "eginetans": "Aigina", "eginetan": "Aigina",
    "agbatana": "Ecbatana",
    "acheloos": "Achelous", "amathus": "Amathous",
}

# Names with a genuine homonym problem, where the centroid heuristic picks
# wrong. Pinned straight to a Pleiades id, each checked against the Pleiades
# record itself (pleiades.stoa.org/places/<id>).
PINNED: dict[str, str] = {
    # Boeotian Thebes (Thiva), not Thebe in the Troad (Turkey).
    "thebes": "541138", "thebans": "541138", "theban": "541138",
    # Egypt: no gazetteer name resolves "Egypt"/"Aigyptos", and the region
    # record's centroid falls in Sudan because the polygon runs the whole
    # Nile. Memphis is where Herodotus's Egypt actually centres.
    "egypt": "736963", "egyptians": "736963", "egyptian": "736963",
    "aegyptus": "736963", "aigyptos": "736963",
    # The island off Attica where the battle was, not Salamis on Cyprus.
    "salamis": "580101",
    # Mesopotamian Babylon, not the Roman fort at Old Cairo.
    "babylon": "893951", "babylonians": "893951", "babylonian": "893951",
    # Argos in the Argolid, not the Karian namesake.
    "argos": "570106", "argives": "570106", "argive": "570106",
    # Euboea the island, not Euboia in Sicily.
    "euboea": "540775", "euboia": "540775", "euboeans": "540775",
    # Thera = Santorini, not the Karian settlement.
    "thera": "599971", "theraeans": "599971",
    # Cilicia in SE Anatolia, not the Cappadocian label.
    "cilicia": "658440", "cilicians": "658440", "cilician": "658440",
    # Artemision off north Euboea -- the naval battle of 480 BC.
    "artemision": "540667", "artemisium": "540667",
    # The Arabian peninsula, not Arabia-in-Egypt or the Iraqi label point.
    "arabia": "29475", "arabians": "29475", "arabian": "29475",
    "arabs": "29475",
    # Syria the region (id 1306), not the Ionian-island settlement of the
    # same name. (As)Syria/569131178 is Assyria proper -- a different place.
    "syria": "1306", "syrians": "1306", "syrian": "1306",
    # Cyprus the island, not the Herodian fort of Kypros in Judaea.
    "cyprus": "707498", "kypros": "707498", "cyprians": "707498",
    "cyprian": "707498",
    # Island vs. its city: Herodotus means the polity in both cases.
    "samos": "599926", "samians": "599926", "samian": "599926",
    "chios": "550497", "chians": "550497",
    "naxos": "599822", "naxians": "599822",
    "delos": "599588", "delians": "599588",
}

# Suffixes that turn a people into its land: Lydians -> Lydia, Athenians -> Athenai.
ETHNIC_SUFFIXES = ("ians", "ian", "ans", "an", "oi", "ai", "es")
ETHNIC_ENDINGS = ("ia", "ai", "a", "os", "e", "is")

# --------------------------------------------------------------------------
# Normalisation: fold Latin and Greek transliteration variants together
# --------------------------------------------------------------------------

_ENDINGS = [
    (re.compile(r"(us|os)$"), "os"),
    (re.compile(r"(um|on)$"), "on"),
    # English plural exonyms (Thebes, Athens, Sardes) against the Greek
    # plural the gazetteer carries (Thebai, Athenai, Sardeis). Without this
    # every "Thebes" missed Thebai/Thebae entirely and fell through to the
    # nearest lookalike -- Thebe in the Troad, i.e. Turkey.
    (re.compile(r"(ae|ai|es|eis)$"), "ai"),
]


def norm(name: str) -> str:
    """Canonical key so 'Miletus', 'Miletos', 'Mīlētos' all collapse together."""
    s = unicodedata.normalize("NFKD", name)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower().strip()
    # Pleiades packs variants into one title ("Thebai/Thebae"); take the
    # first, or the slash silently welds them into one nonsense key.
    s = s.split("/")[0]
    # Trailing disambiguators -- "Salamis (island)", "Lydia (region)" --
    # are Pleiades bookkeeping, not part of the name.
    s = re.sub(r"\s*\([^)]*\)", " ", s)
    s = re.sub(r"[^a-z\s'-]", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    if not s:
        return ""
    s = s.replace("ch", "kh").replace("c", "k")
    s = s.replace("ae", "ai").replace("oe", "oi")
    s = s.replace("ph", "f").replace("th", "0")  # 0 = theta placeholder
    s = s.replace("y", "u")
    s = re.sub(r"([a-z])\1", r"\1", s)  # collapse doubled consonants
    for pat, repl in _ENDINGS:
        s = pat.sub(repl, s)
    return s


# --------------------------------------------------------------------------
# Download helpers
# --------------------------------------------------------------------------


def fetch(url: str, dest: Path, desc: str) -> Path:
    if dest.exists() and dest.stat().st_size > 0:
        print(f"  cached  {desc}  ({dest.stat().st_size/1e6:.1f} MB)")
        return dest
    print(f"  fetching {desc} ...", flush=True)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with requests.get(url, stream=True, timeout=120,
                      headers={"User-Agent": "herodotus-map/1.0"}) as r:
        r.raise_for_status()
        with open(dest, "wb") as fh:
            for chunk in r.iter_content(1 << 16):
                fh.write(chunk)
    print(f"  saved    {dest}  ({dest.stat().st_size/1e6:.1f} MB)")
    return dest


def read_gz_csv(path: Path, usecols=None) -> pd.DataFrame:
    """Pleiades dumps are UTF-8 (sometimes with BOM) and have ragged quoting."""
    with gzip.open(path, "rt", encoding="utf-8-sig", newline="") as fh:
        return pd.read_csv(fh, usecols=usecols, low_memory=False,
                           quoting=csv.QUOTE_MINIMAL, on_bad_lines="skip")


def pick_col(df: pd.DataFrame, *candidates: str) -> str | None:
    lower = {c.lower(): c for c in df.columns}
    for c in candidates:
        if c.lower() in lower:
            return lower[c.lower()]
    return None


# --------------------------------------------------------------------------
# Gazetteer
# --------------------------------------------------------------------------


@dataclass
class Place:
    pid: str
    title: str
    lat: float
    lon: float
    ftypes: str
    periods: str
    url: str


def load_gazetteer(datadir: Path) -> tuple[dict[str, Place], dict[str, list[str]]]:
    places_gz = fetch(PLEIADES_PLACES, datadir / "pleiades-places-latest.csv.gz",
                      "Pleiades places")
    names_gz = fetch(PLEIADES_NAMES, datadir / "pleiades-names-latest.csv.gz",
                     "Pleiades names")

    pdf = read_gz_csv(places_gz)
    c_id = pick_col(pdf, "id")
    c_title = pick_col(pdf, "title")
    c_lat = pick_col(pdf, "reprLat", "representative_latitude", "lat")
    c_lon = pick_col(pdf, "reprLong", "representative_longitude", "lon", "long")
    c_ft = pick_col(pdf, "featureTypes", "feature_types")
    c_tp = pick_col(pdf, "timePeriods", "time_periods")
    c_path = pick_col(pdf, "path")
    if not all([c_id, c_title, c_lat, c_lon]):
        sys.exit(f"Unexpected places schema. Columns seen: {list(pdf.columns)}")

    pdf = pdf.dropna(subset=[c_lat, c_lon])
    pdf[c_lat] = pd.to_numeric(pdf[c_lat], errors="coerce")
    pdf[c_lon] = pd.to_numeric(pdf[c_lon], errors="coerce")
    pdf = pdf.dropna(subset=[c_lat, c_lon])
    pdf = pdf[
        pdf[c_lon].between(BBOX["min_lon"], BBOX["max_lon"])
        & pdf[c_lat].between(BBOX["min_lat"], BBOX["max_lat"])
    ]

    # Explicitly pinned places survive the filters below: Cyprus (island) is
    # tagged Roman/Byzantine only, and Syria's region record carries no
    # period at all, yet both are central to Herodotus.
    keep = pdf[c_id].astype(str).isin(set(PINNED.values()))

    if c_ft:
        ft_ok = pdf[c_ft].fillna("").str.lower().apply(
            lambda s: bool({t.strip() for t in s.split(",")} & FEATURE_TYPES))
        pdf = pdf[ft_ok | keep]
        keep = keep.loc[pdf.index]
    if c_tp:
        # Keep Archaic (A) / Classical (C) / Hellenistic (H) horizons.
        tp_ok = pdf[c_tp].fillna("").str.upper().str.contains("[ACH]", regex=True)
        pdf = pdf[tp_ok | keep]

    places: dict[str, Place] = {}
    for d in pdf.to_dict("records"):
        pid = str(d[c_id])
        path = str(d[c_path]) if c_path and pd.notna(d.get(c_path)) else f"places/{pid}"
        places[pid] = Place(
            pid=pid,
            title=str(d[c_title]),
            lat=float(d[c_lat]),
            lon=float(d[c_lon]),
            ftypes=str(d[c_ft]) if c_ft else "",
            periods=str(d[c_tp]) if c_tp else "",
            url=f"https://pleiades.stoa.org/{path.lstrip('/')}",
        )
    print(f"  places kept: {len(places):,}")

    ndf = read_gz_csv(names_gz)
    n_pid = pick_col(ndf, "pid", "place_id")
    n_cols = [c for c in ("title", "nameTransliterated", "nameRomanised",
                          "nameAttested") if pick_col(ndf, c)]
    n_cols = [pick_col(ndf, c) for c in n_cols]
    if not n_pid or not n_cols:
        sys.exit(f"Unexpected names schema. Columns seen: {list(ndf.columns)}")

    index: dict[str, list[str]] = defaultdict(list)

    def add(key_src: str, pid: str):
        if not isinstance(key_src, str):
            return
        k = norm(key_src)
        if len(k) < 4 or " " in k and len(k) < 6:
            return
        if pid in places and pid not in index[k]:
            index[k].append(pid)

    for d in ndf.to_dict("records"):
        pid = str(d[n_pid])
        if pid not in places:
            continue
        for c in n_cols:
            add(d.get(c), pid)
    # Place titles themselves are good keys too.
    for pid, p in places.items():
        add(p.title, pid)

    index = {k: v for k, v in index.items() if k not in {norm(w) for w in STOPWORDS}}
    print(f"  name keys : {len(index):,}")
    return places, index


# --------------------------------------------------------------------------
# Text
# --------------------------------------------------------------------------

BOOK_RE = re.compile(r"^\s*BOOK\s+([IVX]+)\b", re.M)
NOTES_RE = re.compile(r"^\s*NOTES\s+TO\b", re.M)
CHAP_RE = re.compile(r"^\s*(\d{1,3})\.\s")
START_RE = re.compile(r"\*\*\*\s*START OF TH[EIS]+ PROJECT GUTENBERG", re.I)
END_RE = re.compile(r"\*\*\*\s*END OF TH[EIS]+ PROJECT GUTENBERG", re.I)


@dataclass
class Passage:
    book: str
    chapter: str
    text: str


def load_text(datadir: Path) -> list[Passage]:
    passages: list[Passage] = []
    for eid, (label, _books) in GUTENBERG.items():
        path = fetch(GUTENBERG_URL.format(eid=eid), datadir / f"pg{eid}.txt",
                     f"Herodotus {label}")
        raw = path.read_text(encoding="utf-8", errors="replace")
        m = START_RE.search(raw)
        if m:
            raw = raw[m.end():]
        m = END_RE.search(raw)
        if m:
            raw = raw[:m.start()]
        # Drop transliterated-Greek brackets and footnote markers.
        raw = re.sub(r"\[[^\]\n]{0,200}\]", " ", raw)
        raw = re.sub(r"\{[^}\n]{0,200}\}", " ", raw)

        book = "?"
        in_notes = False
        chapter = "?"
        for para in re.split(r"\n\s*\n", raw):
            para = para.strip()
            if not para:
                continue
            bm = BOOK_RE.match(para) or BOOK_RE.match(para.split("\n")[0])
            if bm:
                book, in_notes = bm.group(1), False
                continue
            if NOTES_RE.match(para):
                in_notes = True
                continue
            if in_notes or book == "?":
                continue
            cm = CHAP_RE.match(para)
            if cm:
                chapter = cm.group(1)
            passages.append(Passage(book, chapter, " ".join(para.split())))
    print(f"  passages  : {len(passages):,}")
    return passages


# --------------------------------------------------------------------------
# Matching
# --------------------------------------------------------------------------

TOKEN_RE = re.compile(r"\b[A-Z][a-zA-Z]{3,}(?:\s+[A-Z][a-zA-Z]{3,})?\b")


@dataclass
class Hit:
    pid: str
    surface: str
    book: str
    chapter: str
    snippet: str
    via: str = "name"


def candidates(surface: str, use_ethnonyms: bool):
    """Yield (lookup_string, provenance) attempts for one capitalised token."""
    yield surface, "name"
    head = surface.split()[0]
    if head != surface:
        yield head, "name"
    low = surface.lower()
    if low in ALIASES:
        yield ALIASES[low], "alias"
    if ALIASES.get(head.lower()):
        yield ALIASES[head.lower()], "alias"
    if use_ethnonyms:
        for suf in ETHNIC_SUFFIXES:
            if len(head) > len(suf) + 3 and head.lower().endswith(suf):
                stem = head[: -len(suf)]
                for end in ETHNIC_ENDINGS:
                    yield stem + end, "ethnonym"
                break


def find_hits(passages: list[Passage], index: dict[str, list[str]],
              places: dict[str, Place], use_ethnonyms: bool = True) -> list[Hit]:
    raw_hits: list[tuple[str, list[str], Passage, int, str]] = []
    for p in passages:
        for m in TOKEN_RE.finditer(p.text):
            surface = m.group(0)
            if surface.lower() in STOPWORDS or surface.split()[0].lower() in STOPWORDS:
                continue
            # A pin wins outright: it also rescues names the gazetteer has no
            # usable key for at all ("Egypt" matches nothing in Pleiades).
            pin = (PINNED.get(surface.lower())
                   or PINNED.get(surface.split()[0].lower()))
            if pin and pin in places:
                raw_hits.append((surface, [pin], p, m.start(), "pinned"))
                continue
            for cand, via in candidates(surface, use_ethnonyms):
                key = norm(cand)
                if key in index:
                    raw_hits.append((surface, index[key], p, m.start(), via))
                    break

    # Disambiguation: names that resolve to exactly one place define the
    # centre of gravity of the work; ambiguous names go to whichever candidate
    # sits closest to it.
    unique = [places[pids[0]] for _, pids, _, _, _ in raw_hits if len(pids) == 1]
    if unique:
        clat = sorted(p.lat for p in unique)[len(unique) // 2]
        clon = sorted(p.lon for p in unique)[len(unique) // 2]
    else:
        clat, clon = 38.0, 27.0

    hits: list[Hit] = []
    for surface, pids, p, pos, via in raw_hits:
        pinned = PINNED.get(surface.lower()) or PINNED.get(surface.split()[0].lower())
        if pinned and pinned in places:
            pid, via = pinned, "pinned"
        elif len(pids) > 1:
            pid = min(pids, key=lambda i: (places[i].lat - clat) ** 2
                      + (places[i].lon - clon) ** 2)
        else:
            pid = pids[0]
        lo, hi = max(0, pos - 90), min(len(p.text), pos + 110)
        hits.append(Hit(pid, surface, p.book, p.chapter,
                        ("..." if lo else "") + p.text[lo:hi] + ("..." if hi < len(p.text) else ""),
                        via))
    print(f"  mentions  : {len(hits):,}  across {len({h.pid for h in hits}):,} places")
    return hits


def load_thucydides(datadir: Path) -> list[Passage]:
    """The comparison corpus, parsed the same way but tagged book 'T'."""
    path = fetch(GUTENBERG_URL.format(eid=THUCYDIDES_EID),
                 datadir / f"pg{THUCYDIDES_EID}.txt", "Thucydides")
    raw = path.read_text(encoding="utf-8", errors="replace")
    m = START_RE.search(raw)
    if m:
        raw = raw[m.end():]
    m = END_RE.search(raw)
    if m:
        raw = raw[:m.start()]
    raw = re.sub(r"\[[^\]\n]{0,200}\]", " ", raw)
    raw = re.sub(r"\{[^}\n]{0,200}\}", " ", raw)

    passages: list[Passage] = []
    chapter = "?"
    started = False
    for para in re.split(r"\n\s*\n", raw):
        para = para.strip()
        if not para:
            continue
        bm = BOOK_RE.match(para) or BOOK_RE.match(para.split("\n")[0])
        if bm:
            started = True
            chapter = bm.group(1)
            continue
        if not started:
            continue
        passages.append(Passage("T", chapter, " ".join(para.split())))
    print(f"  thucydides: {len(passages):,} passages")
    return passages


# --------------------------------------------------------------------------
# Analysis
# --------------------------------------------------------------------------

# Herodotus was born at Halikarnassos; distances are measured from there.
HALIKARNASSOS = (37.0382, 27.4241)

# Long edges were once filtered out as disambiguation noise, but that was
# treating the symptom: the real fix was in norm()/PINNED. Long edges are
# now genuine and often the most interesting -- Athens-Persia (2,778 km,
# 77 shared chapters) is the spine of the whole work.
COOC_MIN_WEIGHT = 3
COOC_TOP_N = 250


def load_zh_names(path: Path) -> dict[str, dict[str, str]]:
    """Chinese place names, keyed by Pleiades id.

    Deliberately an external file rather than a table baked into this script:
    the translations come from a published translation (徐松岩's Herodotus,
    Thucydides and Xenophon), and that provenance should stay visible and
    correctable without editing code.
    """
    if not path.exists():
        return {}
    out: dict[str, dict[str, str]] = {}
    unsourced: list[str] = []
    with open(path, encoding="utf-8-sig", newline="") as fh:
        rows = [ln for ln in fh if not ln.lstrip().startswith("#")]
    for d in csv.DictReader(rows):
        pid = (d.get("pleiades_id") or "").strip()
        zh = (d.get("zh") or "").strip()
        if not pid or not zh:
            continue
        src = (d.get("source") or "").strip()
        out[pid] = {"zh": zh, "source": src, "en": (d.get("en") or "").strip()}
        if not src or src == "待核":
            unsourced.append(f"{pid} {zh}")
    print(f"  zh names  : {len(out):,}"
          + (f"  ({len(unsourced)} unsourced)" if unsourced else ""))
    return out


def zh_label(pid: str, zh_names: dict[str, dict[str, str]]) -> str:
    """The Chinese name for a place, or '' when we don't have one."""
    entry = zh_names.get(pid)
    return entry["zh"] if entry else ""


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in km."""
    import math
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = (math.sin(dp / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2)
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


def book_word_counts(passages: list[Passage]) -> dict[str, int]:
    """Words per book, so mention counts can be length-normalised."""
    words: Counter[str] = Counter()
    for p in passages:
        words[p.book] += len(p.text.split())
    return dict(words)


def density_per_book(hits: list[Hit], passages: list[Passage]) -> list[dict]:
    """Mentions per 1,000 words -- Book II's 248 and Book VII's 545 are not
    comparable until you divide by how long each book actually is."""
    words = book_word_counts(passages)
    counts = Counter(h.book for h in hits)
    rows = []
    for b in BOOK_ORDER:
        w = words.get(b, 0)
        if not w:
            continue
        rows.append({
            "book": b, "name": BOOK_NAMES.get(b, b), "mentions": counts.get(b, 0),
            "words": w, "per_1000": 1000.0 * counts.get(b, 0) / w,
            "distinct_places": len({h.pid for h in hits if h.book == b}),
        })
    return rows


def cooccurrence(hits: list[Hit], places: dict[str, Place],
                 min_weight: int = 3, top_n: int = 250) -> list[dict]:
    """Places named in the same chapter, pairwise.

    This is Herodotus's mental geography: which places are bound together in
    his head, regardless of how far apart they actually are.
    """
    by_chapter: dict[tuple[str, str], set[str]] = defaultdict(set)
    for h in hits:
        by_chapter[(h.book, h.chapter)].add(h.pid)

    pairs: Counter[tuple[str, str]] = Counter()
    for pids in by_chapter.values():
        if len(pids) < 2 or len(pids) > 40:  # skip degenerate/huge chapters
            continue
        for a, b in combinations(sorted(pids), 2):
            pairs[(a, b)] += 1

    edges = []
    for (a, b), w in pairs.most_common():
        if w < min_weight:
            break
        pa, pb = places[a], places[b]
        edges.append({
            "a": a, "b": b, "a_title": pa.title, "b_title": pb.title,
            "weight": w,
            "a_lat": pa.lat, "a_lon": pa.lon, "b_lat": pb.lat, "b_lon": pb.lon,
            "km": haversine_km(pa.lat, pa.lon, pb.lat, pb.lon),
        })
        if len(edges) >= top_n:
            break
    return edges


def distance_decay(hits: list[Hit], places: dict[str, Place],
                   bin_km: int = 250) -> tuple[list[dict], list[dict]]:
    """Mentions against distance from Halikarnassos.

    Returns (per-place rows, binned rows). The binned curve is where the
    edge of the known world starts to fall away.
    """
    totals = Counter(h.pid for h in hits)
    rows = []
    for pid, n in totals.items():
        p = places[pid]
        rows.append({
            "pid": pid, "title": p.title, "mentions": n,
            "lat": p.lat, "lon": p.lon,
            "km": haversine_km(*HALIKARNASSOS, p.lat, p.lon),
        })
    rows.sort(key=lambda r: r["km"])

    bins: dict[int, list[dict]] = defaultdict(list)
    for r in rows:
        bins[int(r["km"] // bin_km)].append(r)
    binned = [{
        "bin_km": b * bin_km,
        "label": f"{b * bin_km}–{(b + 1) * bin_km} km",
        "places": len(rs),
        "mentions": sum(r["mentions"] for r in rs),
        "mentions_per_place": sum(r["mentions"] for r in rs) / len(rs),
    } for b, rs in sorted(bins.items())]
    return rows, binned


# --------------------------------------------------------------------------
# Map
# --------------------------------------------------------------------------

BOOK_ORDER = ["I", "II", "III", "IV", "V", "VI", "VII", "VIII", "IX"]
BOOK_NAMES = {
    "I": "I · Clio", "II": "II · Euterpe", "III": "III · Thaleia",
    "IV": "IV · Melpomene", "V": "V · Terpsichore", "VI": "VI · Erato",
    "VII": "VII · Polymnia", "VIII": "VIII · Urania", "IX": "IX · Calliope",
}
PALETTE = ["#c1440e", "#d97706", "#b45309", "#4d7c0f", "#0f766e",
           "#0e7490", "#1d4ed8", "#6d28d9", "#9d174d"]

def build_map(hits: list[Hit], places: dict[str, Place], out: Path,
              passages: list[Passage],
              thuc_hits: list[Hit] | None = None,
              zh_names: dict[str, dict[str, str]] | None = None) -> None:
    import folium
    from folium.plugins import (Fullscreen, HeatMap, MarkerCluster,
                                MiniMap, Search)

    zh_names = zh_names or {}

    def label(pid: str) -> str:
        """'Thebai/Thebae · 底比斯' when we have a Chinese name, else plain."""
        zh = zh_label(pid, zh_names)
        return f"{places[pid].title} · {zh}" if zh else places[pid].title

    by_place: dict[str, list[Hit]] = defaultdict(list)
    for h in hits:
        by_place[h.pid].append(h)

    totals = Counter(h.pid for h in hits)
    book_totals = Counter(h.book for h in hits)

    # Each place gets exactly one marker (sized by its overall total, coloured
    # by whichever book mentions it most) so places shared across several
    # active book layers don't stack several translucent circles on top of
    # each other -- that stacking is what was showing up as black dots.
    dominant_book: dict[str, str] = {
        pid: Counter(h.book for h in hs).most_common(1)[0][0]
        for pid, hs in by_place.items()
    }

    m = folium.Map(location=[37.0, 27.0], zoom_start=5, tiles=None,
                   control_scale=True)
    # Key-free basemaps, every URL probed live before being listed here.
    # Ancient-world first, modern reference underneath.
    folium.TileLayer(
        tiles="https://dh.gu.se/tiles/imperium/{z}/{x}/{y}.png",
        attr="DARE, Centre for Digital Humanities, Univ. of Gothenburg (CC BY 4.0)",
        # The service actually serves z3-z11; the old min_zoom=4 blanked the
        # map when you zoomed out past the Mediterranean.
        name="Roman Empire (DARE)", min_zoom=3, max_zoom=11,
        show=True).add_to(m)
    folium.TileLayer(
        tiles="https://server.arcgisonline.com/ArcGIS/rest/services/"
              "World_Physical_Map/MapServer/tile/{z}/{y}/{x}",
        attr="Esri", name="Physical relief", max_zoom=8, show=False).add_to(m)
    folium.TileLayer(
        tiles="https://server.arcgisonline.com/ArcGIS/rest/services/"
              "World_Shaded_Relief/MapServer/tile/{z}/{y}/{x}",
        attr="Esri", name="Shaded relief", max_zoom=13, show=False).add_to(m)
    # No modern borders, roads or city names: closest thing to seeing the
    # landscape Herodotus actually walked.
    folium.TileLayer(
        tiles="https://server.arcgisonline.com/ArcGIS/rest/services/"
              "Elevation/World_Hillshade/MapServer/tile/{z}/{y}/{x}",
        attr="Esri", name="Bare terrain (hillshade)", max_zoom=14,
        show=False).add_to(m)
    # Bathymetry: the Aegean's sailing distances and island-hopping routes
    # read far better with sea depth shown.
    folium.TileLayer(
        tiles="https://server.arcgisonline.com/ArcGIS/rest/services/"
              "Ocean/World_Ocean_Base/MapServer/tile/{z}/{y}/{x}",
        attr="Esri", name="Ocean & bathymetry", max_zoom=13,
        show=False).add_to(m)
    folium.TileLayer(
        tiles="https://a.tile.opentopomap.org/{z}/{x}/{y}.png",
        attr="OpenTopoMap (CC-BY-SA)", name="Topographic", max_zoom=14,
        show=False).add_to(m)
    # Muted greys: the best backdrop for reading the coloured markers.
    folium.TileLayer(
        tiles="https://a.basemaps.cartocdn.com/light_all/{z}/{x}/{y}.png",
        attr="CARTO / OpenStreetMap contributors", name="Minimal light",
        max_zoom=14, show=False).add_to(m)
    folium.TileLayer(
        tiles="https://a.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}.png",
        attr="CARTO / OpenStreetMap contributors", name="Minimal dark",
        max_zoom=14, show=False).add_to(m)
    folium.TileLayer(
        tiles="https://server.arcgisonline.com/ArcGIS/rest/services/"
              "NatGeo_World_Map/MapServer/tile/{z}/{y}/{x}",
        attr="Esri / National Geographic", name="National Geographic",
        max_zoom=12, show=False).add_to(m)
    folium.TileLayer("OpenStreetMap", name="Modern OSM", show=False).add_to(m)

    places_by_dominant: dict[str, list[str]] = defaultdict(list)
    for pid, book in dominant_book.items():
        places_by_dominant[book].append(pid)

    for i, book in enumerate(BOOK_ORDER):
        if book not in places_by_dominant:
            continue
        colour = PALETTE[i % len(PALETTE)]
        fg = folium.FeatureGroup(
            name=f"Book {BOOK_NAMES.get(book, book)}  ({book_totals[book]})",
            show=(book == "I"))
        for pid in places_by_dominant[book]:
            hs = by_place[pid]
            p = places[pid]
            radius = 3 + 2.2 * (totals[pid] ** 0.5)
            per_book = Counter(h.book for h in hs)
            breakdown = " · ".join(
                f"Bk {b} ×{per_book[b]}" for b in BOOK_ORDER if b in per_book)
            chapters = sorted({h.chapter for h in hs}, key=lambda c: int(c) if c.isdigit() else 0)
            snippets = "".join(
                f"<li><b>{h.book}.{h.chapter}</b> {h.snippet}</li>" for h in hs[:3])
            zh = zh_label(pid, zh_names)
            zh_head = (f"<div style='font-size:16px;font-weight:bold'>{zh}</div>"
                       if zh else "")
            html = (
                f"<div style='font:13px/1.45 Georgia,serif;max-width:340px'>"
                f"{zh_head}"
                f"<div style='font-size:15px'><b>{p.title}</b></div>"
                f"<div style='color:#666'>{p.ftypes} · {totals[pid]} mention(s) total</div>"
                f"<div style='color:#666'>{breakdown}</div>"
                f"<div style='color:#666'>chapters: {', '.join(chapters[:14])}</div>"
                f"<ul style='padding-left:16px;margin:6px 0'>{snippets}</ul>"
                f"<a href='{p.url}' target='_blank'>Pleiades →</a></div>"
            )
            folium.CircleMarker(
                location=[p.lat, p.lon], radius=radius, color=colour,
                weight=1, fill=True, fill_color=colour, fill_opacity=0.55,
                tooltip=f"{label(pid)} ({totals[pid]})",
                popup=folium.Popup(html, max_width=380),
            ).add_to(fg)
        fg.add_to(m)

    # ---- Heat map: mention-weighted, so the centre of gravity shows up ----
    heat_pts = [[places[pid].lat, places[pid].lon, float(n)]
                for pid, n in totals.items()]
    heat_fg = folium.FeatureGroup(name="Heat map (mention-weighted)", show=False)
    HeatMap(heat_pts, min_opacity=0.25, radius=18, blur=24,
            max_zoom=9, gradient={0.15: "#1d4ed8", 0.35: "#0f766e",
                                  0.6: "#d97706", 1.0: "#c1440e"}).add_to(heat_fg)
    heat_fg.add_to(m)

    # ---- Clustered markers: tames the Aegean pile-up ----
    cluster_fg = folium.FeatureGroup(name="Clustered places", show=False)
    cluster = MarkerCluster(options={"maxClusterRadius": 35,
                                     "disableClusteringAtZoom": 9}).add_to(cluster_fg)
    for pid, hs in by_place.items():
        p = places[pid]
        # CircleMarker, not Marker: the default pin needs an icon PNG that
        # never loads here, which is what rendered as black squares.
        colour = PALETTE[BOOK_ORDER.index(dominant_book[pid]) % len(PALETTE)]
        folium.CircleMarker(
            location=[p.lat, p.lon],
            radius=3 + 2.0 * (totals[pid] ** 0.5),
            color="#fff8e7", weight=1.5,
            fill=True, fill_color=colour, fill_opacity=0.8,
            tooltip=f"{label(pid)} ({totals[pid]})",
            popup=folium.Popup(
                f"<div style='font:13px/1.45 Georgia,serif'>"
                f"<b>{label(pid)}</b><br>"
                f"{totals[pid]} mention(s) · dominant Book {dominant_book[pid]}<br>"
                f"<a href='{p.url}' target='_blank'>Pleiades →</a></div>",
                max_width=300),
        ).add_to(cluster)
    cluster_fg.add_to(m)

    # ---- Searchable layer: type "Sardis", "Thebes" or "底比斯" ----
    # Leaflet's Search matches one property, so everything searchable for a
    # place is concatenated into it: the gazetteer title, the English
    # exonyms readers actually type, and the Chinese name.
    en_aliases: dict[str, set[str]] = defaultdict(set)
    for word, pid in PINNED.items():
        if pid in by_place:
            en_aliases[pid].add(word.capitalize())
    title_key = defaultdict(list)
    for pid in by_place:
        title_key[norm(places[pid].title)].append(pid)
    for word, target in ALIASES.items():
        for pid in title_key.get(norm(target), []):
            en_aliases[pid].add(word.capitalize())

    search_features = []
    for pid in by_place:
        p = places[pid]
        zh = zh_label(pid, zh_names)
        terms = [p.title, *sorted(en_aliases.get(pid, ()))]
        if zh:
            terms.extend(zh.split("/"))
        search_features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [p.lon, p.lat]},
            "properties": {"title": " · ".join(dict.fromkeys(terms)),
                           "name": label(pid),
                           "mentions": totals[pid]}})

    search_geo = folium.GeoJson(
        {"type": "FeatureCollection", "features": search_features},
        name="Searchable place index", show=False,
        marker=folium.CircleMarker(radius=6, color="#111", weight=1,
                                   fill=True, fill_color="#fde68a",
                                   fill_opacity=0.9),
        tooltip=folium.GeoJsonTooltip(fields=["name", "mentions"],
                                      aliases=["Place", "Mentions"]),
    )
    search_geo.add_to(m)
    Search(layer=search_geo, search_label="title",
           placeholder="搜索地名 / Search a place…",
           collapsed=True, position="topright").add_to(m)

    # ---- Co-occurrence network: places named in the same chapter ----
    edges = cooccurrence(hits, places,
                         min_weight=COOC_MIN_WEIGHT, top_n=COOC_TOP_N)
    if edges:
        heaviest = max(e["weight"] for e in edges)
        net_fg = folium.FeatureGroup(
            name=f"Co-occurrence network ({len(edges)} links)", show=False)
        for e in edges:
            folium.PolyLine(
                [[e["a_lat"], e["a_lon"]], [e["b_lat"], e["b_lon"]]],
                color="#6d28d9", weight=0.6 + 3.4 * (e["weight"] / heaviest),
                opacity=0.25 + 0.5 * (e["weight"] / heaviest),
                tooltip=(f"{e['a_title']} ↔ {e['b_title']} · "
                         f"{e['weight']} shared chapters · {e['km']:.0f} km"),
            ).add_to(net_fg)
        net_fg.add_to(m)

    # ---- Distance decay from Halikarnassos ----
    _, decay_bins = distance_decay(hits, places)
    decay_fg = folium.FeatureGroup(
        name="Distance from Halikarnassos", show=False)
    folium.CircleMarker(
        location=list(HALIKARNASSOS), radius=7, color="#111", weight=2,
        fill=True, fill_color="#fde68a", fill_opacity=1.0,
        tooltip="Halikarnassos — Herodotus's birthplace",
    ).add_to(decay_fg)
    for b in decay_bins:
        if not b["places"]:
            continue
        folium.Circle(
            location=list(HALIKARNASSOS), radius=(b["bin_km"] + 250) * 1000,
            color="#111", weight=1, opacity=0.35, fill=False,
            dash_array="4,8",
            tooltip=(f"{b['label']} · {b['places']} place(s) · "
                     f"{b['mentions']} mention(s) · "
                     f"{b['mentions_per_place']:.1f} per place"),
        ).add_to(decay_fg)
    decay_fg.add_to(m)

    # ---- Thucydides comparison layer ----
    if thuc_hits:
        t_totals = Counter(h.pid for h in thuc_hits)
        thuc_fg = folium.FeatureGroup(
            name=f"Thucydides ({len(t_totals)} places)", show=False)
        for pid, n in t_totals.items():
            p = places[pid]
            shared = pid in totals
            folium.CircleMarker(
                location=[p.lat, p.lon], radius=3 + 2.0 * (n ** 0.5),
                color="#1f2937", weight=1,
                fill=True, fill_color="#111827",
                fill_opacity=0.5 if shared else 0.75,
                tooltip=(f"{label(pid)} · Thucydides ×{n}"
                         + (f" · Herodotus ×{totals[pid]}" if shared
                            else " · not in Herodotus")),
            ).add_to(thuc_fg)
        thuc_fg.add_to(m)

    # ---- Length-normalised book density, as a legend box ----
    density = density_per_book(hits, passages)
    if density:
        peak = max(r["per_1000"] for r in density)
        bars = "".join(
            f"<tr><td style='padding-right:8px;white-space:nowrap'>{r['name']}</td>"
            f"<td style='width:120px'>"
            f"<div style='background:{PALETTE[BOOK_ORDER.index(r['book']) % len(PALETTE)]};"
            f"height:9px;width:{100 * r['per_1000'] / peak:.0f}%'></div></td>"
            f"<td style='padding-left:8px;text-align:right'>{r['per_1000']:.1f}</td></tr>"
            for r in density)
        legend = folium.Element(f"""
<button id="density-legend-toggle" aria-expanded="false"
        aria-controls="density-legend" aria-label="Show place-density legend"
        style="position:fixed;bottom:calc(48px + env(safe-area-inset-bottom));
               left:12px;z-index:10000;width:44px;height:44px;
               border-radius:50%;border:1px solid #c9bfae;
               background:rgba(255,252,245,.94);box-shadow:0 1px 6px rgba(0,0,0,.25);
               font-size:18px;line-height:1;cursor:pointer;padding:0">📊</button>
<div id="density-legend" style="display:none;position:fixed;
            bottom:calc(100px + env(safe-area-inset-bottom));left:12px;z-index:9999;
            max-width:calc(100vw - 24px);overflow:auto;box-sizing:border-box;
            background:rgba(255,252,245,.94);border:1px solid #c9bfae;
            border-radius:4px;padding:10px 12px;font:12px/1.4 Georgia,serif;
            box-shadow:0 1px 6px rgba(0,0,0,.2)">
  <div style="font-weight:bold;margin-bottom:6px">Place mentions per 1,000 words</div>
  <table style="border-collapse:collapse">{bars}</table>
  <div style="margin-top:6px;color:#666;font-size:11px">
    Length-normalised: Book II is the <i>least</i> place-dense, not the shortest.</div>
</div>
<script>
(function() {{
  var btn = document.getElementById('density-legend-toggle');
  var box = document.getElementById('density-legend');
  btn.addEventListener('click', function() {{
    var open = box.style.display !== 'none';
    box.style.display = open ? 'none' : 'block';
    btn.setAttribute('aria-expanded', String(!open));
    btn.setAttribute('aria-label',
      open ? 'Show place-density legend' : 'Hide place-density legend');
  }});
}})();
</script>""")
        m.get_root().html.add_child(legend)

    # ---- Mobile-friendly tweaks (iPhone-class screens) ----
    mobile_css = folium.Element("""
<style>
@media (max-width: 700px) {
  .leaflet-control-minimap { display: none !important; }
  .leaflet-control-layers-toggle,
  .leaflet-control-search .search-button,
  .leaflet-control-fullscreen a,
  .leaflet-control-zoom a {
    width: 44px !important;
    height: 44px !important;
    line-height: 44px !important;
  }
  .leaflet-control-search { max-width: calc(100vw - 90px); }
  .leaflet-control-search .search-input { font-size: 16px; }
}
</style>""")
    m.get_root().html.add_child(mobile_css)

    folium.LayerControl(collapsed=True).add_to(m)
    Fullscreen().add_to(m)
    # MiniMap defaults to the official OSM tile server, which returns 403s
    # for non-browser/no-Referer requests (e.g. opening the file over file://).
    # Reuse a key-free Esri layer instead, same as the main basemaps above.
    minimap_tiles = folium.TileLayer(
        tiles="https://server.arcgisonline.com/ArcGIS/rest/services/"
              "World_Shaded_Relief/MapServer/tile/{z}/{y}/{x}",
        attr="Esri", max_zoom=13)
    MiniMap(tile_layer=minimap_tiles, toggle_display=True).add_to(m)
    out.parent.mkdir(parents=True, exist_ok=True)
    m.save(str(out))
    print(f"  map       : {out}")


# --------------------------------------------------------------------------


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--datadir", type=Path, default=Path("data"))
    ap.add_argument("--outdir", type=Path, default=Path("output"))
    ap.add_argument("--no-ethnonyms", action="store_true",
                    help="do not map peoples (Lydians) onto their land (Lydia)")
    ap.add_argument("--top", type=int, default=0,
                    help="print the N most-mentioned places")
    ap.add_argument("--thucydides", action="store_true",
                    help="add a Thucydides comparison layer (extra download)")
    ap.add_argument("--zh-names", type=Path, default=Path("data/zh_names.csv"),
                    help="CSV of Chinese place names (pleiades_id,en,zh,source)")
    args = ap.parse_args()

    print("1/4  gazetteer")
    places, index = load_gazetteer(args.datadir)
    print("2/4  text")
    passages = load_text(args.datadir)
    print("3/4  matching")
    hits = find_hits(passages, index, places, use_ethnonyms=not args.no_ethnonyms)
    print("4/4  output")

    rows = [{
        "book": h.book, "chapter": h.chapter, "surface_form": h.surface,
        "matched_via": h.via,
        "place": places[h.pid].title, "pleiades_id": h.pid,
        "feature_types": places[h.pid].ftypes,
        "lat": places[h.pid].lat, "lon": places[h.pid].lon,
        "pleiades_url": places[h.pid].url, "context": h.snippet,
    } for h in hits]
    df = pd.DataFrame(rows)
    args.outdir.mkdir(parents=True, exist_ok=True)
    csv_path = args.outdir / "herodotus_mentions.csv"
    df.to_csv(csv_path, index=False, encoding="utf-8-sig")
    print(f"  csv       : {csv_path}  ({len(df):,} rows)")

    thuc_hits = None
    if args.thucydides:
        thuc_passages = load_thucydides(args.datadir)
        thuc_hits = find_hits(thuc_passages, index, places,
                              use_ethnonyms=not args.no_ethnonyms)
        h_pids, t_pids = {h.pid for h in hits}, {h.pid for h in thuc_hits}
        h_km = [haversine_km(*HALIKARNASSOS, places[p].lat, places[p].lon)
                for p in h_pids]
        t_km = [haversine_km(*HALIKARNASSOS, places[p].lat, places[p].lon)
                for p in t_pids]
        print(f"  Herodotus : {len(h_pids):3} places · median "
              f"{sorted(h_km)[len(h_km) // 2]:.0f} km · max {max(h_km):.0f} km")
        print(f"  Thucydides: {len(t_pids):3} places · median "
              f"{sorted(t_km)[len(t_km) // 2]:.0f} km · max {max(t_km):.0f} km")
        print(f"  shared    : {len(h_pids & t_pids):3} places")

    zh_names = load_zh_names(args.zh_names)
    build_map(hits, places, args.outdir / "herodotus_map.html", passages,
              thuc_hits=thuc_hits, zh_names=zh_names)

    if args.top:
        print()
        print(df.groupby(["place", "pleiades_id"]).size()
              .sort_values(ascending=False).head(args.top).to_string())


if __name__ == "__main__":
    main()
