"""Baut die Weltkarte für den Offline-Betrieb (static/vendor/welt.geojson) aus world-atlas (Natural Earth,
gemeinfrei). Nur für Entwickler, die Datei liegt fertig im Repository.

Aufruf: python werkzeuge/welt_bauen.py <countries-50m.json>
"""
import json
import sys
from pathlib import Path

ZIEL = Path(__file__).resolve().parent.parent / "imgsortsearch" / "static" / "vendor" / "welt.geojson"


def main(datei):
    topo = json.load(open(datei, encoding="utf-8"))
    sx, sy = topo["transform"]["scale"]
    tx, ty = topo["transform"]["translate"]
    boegen = []
    for bogen in topo["arcs"]:
        x = y = 0
        punkte = []
        for dx, dy in bogen:
            x += dx
            y += dy
            punkte.append([round(x * sx + tx, 2), round(y * sy + ty, 2)])
        boegen.append(punkte)

    def ring(indizes):
        erg = []
        for i in indizes:
            teil = boegen[i] if i >= 0 else boegen[~i][::-1]
            erg.extend(teil if not erg else teil[1:])
        return erg

    features = []
    for g in topo["objects"]["countries"]["geometries"]:
        if g["type"] == "Polygon":
            koord = [[ring(r) for r in g["arcs"]]]
        elif g["type"] == "MultiPolygon":
            koord = [[ring(r) for r in p] for p in g["arcs"]]
        else:
            continue
        features.append({"type": "Feature", "properties": {},
                         "geometry": {"type": "MultiPolygon", "coordinates": koord}})
    ZIEL.write_text(json.dumps({"type": "FeatureCollection", "features": features}, separators=(",", ":")), "utf-8")
    print(len(features), "Länder,", ZIEL.stat().st_size // 1024, "KB")


if __name__ == "__main__":
    main(sys.argv[1])
