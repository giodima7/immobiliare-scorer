"""Build data_sources/milano_civici/civici_index.json (geo_index.py) from the
Comune di Milano house-number dataset.

1. Download the CSV zip of "Numeri civici con coordinate geografiche"
   (ds634, CC BY) from https://dati.comune.milano.it/dataset/ds634-numeri-civici-coordinate
   into data_sources/milano_civici/.
2. python3 tools/build_civici_index.py

The CSV is ';'-separated; rows carry a few more trailing fields than the
header, so columns are read by position: 1 NUMERO, 2 LETTERA, 11
DATA_SOPPRESSIONE, 28 TIPO, 29 DENOMINAZIONE ("MANZONI ALESSANDRO"),
32 DESCRITTIVO ("ALESSANDRO MANZONI"), and lon / lat in the last-but-two /
last-but-one fields.
"""
import json
import math
import sys
import zipfile
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from geo_index import INDEX_PATH, norm_street  # noqa: E402

SRC_DIR = INDEX_PATH.parent


def main() -> None:
    zips = sorted(SRC_DIR.glob("ds634*.zip"))
    if not zips:
        raise SystemExit(f"no ds634 zip in {SRC_DIR}")
    z = zipfile.ZipFile(zips[-1])
    lines = z.read(z.namelist()[0]).decode("utf-8-sig", errors="replace").splitlines()
    streets: dict = defaultdict(lambda: {"n": {}, "active": {}, "pts": []})
    names: dict = {}
    for line in lines[1:]:
        r = line.split(";")
        if len(r) < 40:
            continue
        try:
            lon, lat = float(r[-3]), float(r[-2])
        except ValueError:
            continue
        if not (45.3 < lat < 45.6 and 9.0 < lon < 9.35):
            continue
        tipo, denom, descr = r[28].strip(), r[29].strip(), r[32].strip()
        key = norm_street(f"{tipo} {descr or denom}")
        if not key:
            continue
        names[key] = (tipo, denom, descr)
        st = streets[key]
        num = (r[1].strip() + r[2].strip()).upper()
        active = r[11].strip() in ("", "0")
        pt = [round(lat, 6), round(lon, 6)]
        st["pts"].append(pt)
        if num and (num not in st["n"] or (active and not st["active"].get(num))):
            st["n"][num] = pt
            st["active"][num] = active

    out, alias, alias_hits = {}, {}, defaultdict(set)
    for key, st in streets.items():
        pts = st["pts"]
        lat = sum(p[0] for p in pts) / len(pts)
        lon = sum(p[1] for p in pts) / len(pts)
        ext = max(_dist(lat, lon, p[0], p[1]) for p in pts) * 2
        out[key] = {"n": st["n"], "c": [round(lat, 6), round(lon, 6)], "ext": round(ext)}
        tipo, denom, descr = names[key]
        # Other ways listings write the same street: surname-first
        # ("via manzoni alessandro") and surname only ("via manzoni").
        for other in (norm_street(f"{tipo} {denom}"), norm_street(f"{tipo} {denom.split(' ')[0]}")):
            if other and other != key:
                alias_hits[other].add(key)
    for other, keys in alias_hits.items():
        if len(keys) == 1 and other not in out:     # ambiguous surnames stay out
            alias[other] = next(iter(keys))
    INDEX_PATH.write_text(json.dumps({"source": zips[-1].name, "streets": out, "alias": alias},
                                     separators=(",", ":")))
    print(f"{len(out)} streets, {sum(len(s['n']) for s in out.values())} house numbers, "
          f"{len(alias)} aliases → {INDEX_PATH} ({INDEX_PATH.stat().st_size // 1024} KB)")


def _dist(a1, o1, a2, o2) -> float:
    r = math.pi / 180
    x = (o2 - o1) * r * math.cos((a1 + a2) / 2 * r)
    y = (a2 - a1) * r
    return 6371000 * math.hypot(x, y)


if __name__ == "__main__":
    main()
