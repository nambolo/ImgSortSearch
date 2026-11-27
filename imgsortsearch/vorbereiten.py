"""Lädt alle Modelle einmalig in den Datenordner, damit das Programm danach offline läuft.

Aufruf: python -m imgsortsearch.vorbereiten
"""
from __future__ import annotations

import sys

from .einstellungen import Einstellungen
from .indexer import Modelle


def main() -> int:
    e = Einstellungen()
    m = Modelle(e)
    schritte = [("Bildmodell (CLIP, ca. 1,1 GB)", m.clip), ("Gesichtserkennung (ca. 40 MB)", m.gesichter),
                ("Texterkennung", m.ocr)]
    if e["beschreibung"]["aktiv"]:
        schritte.append(("Bildbeschreibung und Übersetzer (ca. 1,3 GB)", m.beschreiber))
    ok = True
    for name, laden in schritte:
        print(f"  - {name} ...", flush=True)
        if laden() is None:
            ok = False
            print(f"    FEHLER: {m.fehler}", flush=True)
    if m._ocr is not None:
        print(f"  Texterkennung: {m._ocr.backend}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
