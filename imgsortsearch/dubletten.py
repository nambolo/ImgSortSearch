"""Dubletten finden und bereinigen.

Exakt gleich: gleiche Dateigröße und gleicher Inhalt (SHA-1). Gelesen werden nur Dateien,
deren Größe mehrfach vorkommt.

Ähnlich (verkleinert, neu komprimiert, WhatsApp-Kopie): Bildfingerabdruck (dHash, 128 Bit)
aus dem vorhandenen Vorschaubild, bestätigt über den Bildvektor aus dem Index (CLIP).
Serienbilder werden ausgeschlossen: Haben beide Bilder eine Aufnahmezeit aus der Kamera
und liegen mehr als ein paar Sekunden auseinander, sind es verschiedene Aufnahmen.

Es wird nichts gelöscht. Aussortierte Dateien wandern in einen Quarantäne-Ordner oder in den
Windows-Papierkorb. Jeder Lauf hat ein Protokoll und lässt sich rückgängig machen.
"""
from __future__ import annotations

import json
import os
import re
import shutil
from datetime import datetime
from pathlib import Path

import numpy as np
from PIL import Image

from . import modelle, papierkorb
from .datenbank import Datenbank, blob_zu_vektor
from .drossel import OHNE
from .sortieren import _hash, _sidecars

SCHEMA = """
CREATE TABLE IF NOT EXISTS dubletten_cache (
    bild_id INTEGER PRIMARY KEY REFERENCES bilder(id) ON DELETE CASCADE,
    groesse INTEGER, mtime REAL,
    sha1 TEXT,
    dhash TEXT
);
"""

DHASH_BITS = 128
GRENZE_SICHER = 10      # so wenige abweichende Bits: auch ohne Bildvektor dasselbe Bild
GRENZE_MIT_CLIP = 18    # mit Bildvektor-Bestätigung
CLIP_MIN = 0.93
FLACH = 4.0             # Helligkeitsstreuung, unter der ein Bild als einfarbig gilt
KEIN_HASH = "-"
SERIE_SEKUNDEN = 3
KOPIE_MUSTER = re.compile(r"(kopie|copy|\(\d+\)|[-_ ]\d{1,2}$|whatsapp|-wa\d+|bearbeitet|edited)", re.I)


def schema(db: Datenbank) -> None:
    with db.verbindung() as v:
        v.executescript(SCHEMA)


# ---------------------------------------------------------------- Fingerabdruck

def dhash(img: Image.Image) -> str:
    """128-Bit-Differenz-Hash (waagerecht + senkrecht) als Hex-Text, ``KEIN_HASH`` bei einfarbigen Bildern."""
    g = img.convert("L")
    klein = np.asarray(g.resize((32, 32), Image.Resampling.BILINEAR), dtype=np.float32)
    if klein.std() < FLACH:
        return KEIN_HASH  # fast einfarbig (schwarzes Bild, leere Seite): nur exakt vergleichen
    w = np.asarray(g.resize((9, 8), Image.Resampling.LANCZOS), dtype=np.int16)
    s = np.asarray(g.resize((8, 9), Image.Resampling.LANCZOS), dtype=np.int16)
    bits = np.concatenate([(w[:, 1:] > w[:, :-1]).ravel(), (s[1:, :] > s[:-1, :]).ravel()])
    return np.packbits(bits).tobytes().hex()


def _bits(hexe: list[str]) -> np.ndarray:
    roh = np.frombuffer(bytes.fromhex("".join(hexe)), dtype=np.uint8).reshape(len(hexe), DHASH_BITS // 8)
    return np.unpackbits(roh, axis=1).astype(np.float32)


# ---------------------------------------------------------------- Suche

class Suche:
    """Sucht im Hintergrund; ``status`` zeigt den Fortschritt für die Oberfläche."""

    def __init__(self, db: Datenbank, vorschau_pfad, stop=None):
        self.db = db
        self.vorschau_pfad = vorschau_pfad
        self.stop = stop
        self.drossel = OHNE  # Rechner schonen: setzt der Server (Pausen nach jedem Bild)
        self.status = {"laeuft": False, "phase": "", "fertig": 0, "gesamt": 0, "meldung": ""}

    def _abbruch(self) -> bool:
        return bool(self.stop and self.stop.is_set())

    def _cache(self, zeilen) -> dict[int, dict]:
        alt = {r["bild_id"]: dict(r) for r in self.db.abfrage("SELECT * FROM dubletten_cache")}
        erg = {}
        for z in zeilen:
            c = alt.get(z["id"])
            if c and c["groesse"] == z["groesse"] and abs((c["mtime"] or 0) - (z["mtime"] or 0)) <= 1:
                erg[z["id"]] = c
            else:
                erg[z["id"]] = {"bild_id": z["id"], "groesse": z["groesse"], "mtime": z["mtime"],
                                "sha1": None, "dhash": None, "neu": True}
        return erg

    def _speichern(self, eintraege: list[dict]) -> None:
        if eintraege:
            with self.db.verbindung() as v:
                v.executemany("INSERT OR REPLACE INTO dubletten_cache VALUES (?,?,?,?,?)",
                              [(e["bild_id"], e["groesse"], e["mtime"], e["sha1"], e["dhash"]) for e in eintraege])

    def ausfuehren(self) -> dict:
        schema(self.db)
        st = self.status
        st.update(laeuft=True, phase="hash", fertig=0, gesamt=0, meldung="Suche gleiche Dateien ...")
        zeilen = self.db.abfrage("SELECT id, pfad, groesse, mtime, breite, hoehe, datum, datum_quelle, clip "
                                 "FROM bilder WHERE fehler IS NULL OR fehler=''")
        info = {z["id"]: z for z in zeilen}
        cache = self._cache(zeilen)

        # 1. exakt gleich: nur Dateien mit gleicher Größe lesen
        nach_groesse: dict[int, list[int]] = {}
        for z in zeilen:
            if z["groesse"]:
                nach_groesse.setdefault(z["groesse"], []).append(z["id"])
        kandidaten = [i for ids in nach_groesse.values() if len(ids) > 1 for i in ids]
        st.update(gesamt=len(kandidaten))
        geaendert = []
        for n, i in enumerate(kandidaten, 1):
            if self._abbruch():
                return self._fertig(None)
            c = cache[i]
            if not c["sha1"]:
                try:
                    with self.drossel.arbeit(self.stop):
                        c["sha1"] = _hash(Path(info[i]["pfad"]))
                    geaendert.append(c)
                except OSError:
                    pass
            if n % 200 == 0:
                st.update(fertig=n)
                self._speichern(geaendert)
                geaendert = []
        self._speichern(geaendert)
        kanten: list[tuple[int, int, str, float]] = []
        nach_sha: dict[str, list[int]] = {}
        for i in kandidaten:
            if cache[i]["sha1"]:
                nach_sha.setdefault(cache[i]["sha1"], []).append(i)
        for ids in nach_sha.values():
            for j in ids[1:]:
                kanten.append((ids[0], j, "exakt", 0))

        # 2. Fingerabdrücke (aus der Vorschau, sonst aus der Datei)
        st.update(phase="fingerabdruck", fertig=0, gesamt=len(zeilen), meldung="Bilder werden verglichen ...")
        geaendert = []
        for n, z in enumerate(zeilen, 1):
            if self._abbruch():
                return self._fertig(None)
            c = cache[z["id"]]
            if not c["dhash"]:
                try:
                    with self.drossel.arbeit(self.stop):
                        vp = self.vorschau_pfad(z["id"])
                        if vp.exists():
                            with Image.open(vp) as img:
                                c["dhash"] = dhash(img)
                        else:
                            c["dhash"] = dhash(modelle.bild_laden(Path(z["pfad"]), max_kante=720))
                    geaendert.append(c)
                except Exception:
                    pass
            if n % 200 == 0:
                st.update(fertig=n)
                self._speichern(geaendert)
                geaendert = []
        self._speichern(geaendert)

        mit = [z["id"] for z in zeilen if cache[z["id"]]["dhash"] not in (None, "", KEIN_HASH)]
        kanten += self._aehnliche_paare(mit, cache, info)
        gruppen = self._gruppieren(kanten, info, cache)
        return self._fertig(gruppen)

    def _aehnliche_paare(self, ids, cache, info) -> list[tuple[int, int, str, float]]:
        if len(ids) < 2:
            return []
        self.status.update(phase="vergleich", meldung="Ähnliche Bilder werden gesucht ...", fertig=0, gesamt=len(ids))
        bits = _bits([cache[i]["dhash"] for i in ids])
        anzahl = bits.sum(axis=1)
        clip = [info[i]["clip"] for i in ids]
        dim = next((len(blob_zu_vektor(c)) for c in clip if c is not None), 0)
        vek = np.zeros((len(ids), max(dim, 1)), dtype=np.float32)
        hat_clip = np.zeros(len(ids), dtype=bool)
        for k, c in enumerate(clip):
            if c is not None:
                v = blob_zu_vektor(c)
                if len(v) == dim:
                    vek[k] = v / (np.linalg.norm(v) + 1e-9)
                    hat_clip[k] = True
        seitenv = np.array([_seitenverhaeltnis(info[i]) for i in ids], dtype=np.float32)
        zeit = [_kamerazeit(info[i]) for i in ids]
        paare = []
        # Ein Block (Zeilen x alle Bilder) soll höchstens rund 150 MB je Zwischenergebnis belegen:
        # mit festen 1000 Zeilen waren es bei 180.000 Bildern über 2 GB
        block = int(max(32, min(1000, 4e7 // len(ids))))
        for a in range(0, len(ids), block):
            if self._abbruch():
                return []
            e = min(a + block, len(ids))
            # Hamming-Abstand = gesetzte Bits in a + in b - 2 * gemeinsame; nur gegen die späteren Bilder
            # (j > i), jedes Paar also einmal. Halb so viele Multiplikationen wie vorher, ein Viertel der Paare.
            abstand = anzahl[a:e, None] + anzahl[None, a:] - 2 * (bits[a:e] @ bits[a:].T)
            breite = len(ids) - a
            for k in np.flatnonzero(abstand <= GRENZE_MIT_CLIP).tolist():
                zi, sj = divmod(k, breite)
                i = a + zi
                j = a + sj
                if j <= i:
                    continue
                d = abstand[zi, sj]
                if seitenv[i] and seitenv[j] and abs(seitenv[i] - seitenv[j]) > 0.03 * max(seitenv[i], seitenv[j]):
                    continue  # anderer Ausschnitt
                if zeit[i] and zeit[j] and abs((zeit[i] - zeit[j]).total_seconds()) > SERIE_SEKUNDEN:
                    continue  # Serienbild: andere Aufnahme
                if hat_clip[i] and hat_clip[j]:
                    if float(vek[i] @ vek[j]) < CLIP_MIN:
                        continue
                elif d > GRENZE_SICHER:
                    continue
                paare.append((ids[i], ids[j], "aehnlich", float(d)))
            self.status.update(fertig=min(len(ids), a + block))
        return paare

    def _gruppieren(self, kanten, info, cache) -> list[dict]:
        eltern: dict[int, int] = {}

        def wurzel(x):
            while eltern.setdefault(x, x) != x:
                eltern[x] = eltern[eltern[x]]
                x = eltern[x]
            return x

        # Kamerazeiten je Gruppe: über eine Kopie ohne EXIF dürfen zwei Serienbilder
        # nicht in dieselbe Gruppe rutschen. Exakte Paare zuerst, dann die ähnlichsten.
        zeiten: dict[int, list] = {}
        for a, b, _art, _d in sorted(kanten, key=lambda k: (k[2] != "exakt", k[3])):
            ra, rb = wurzel(a), wurzel(b)
            if ra == rb:
                continue
            za = zeiten.get(ra, [t for t in [_kamerazeit(info[ra])] if t])
            zb = zeiten.get(rb, [t for t in [_kamerazeit(info[rb])] if t])
            if any(abs((x - y).total_seconds()) > SERIE_SEKUNDEN for x in za for y in zb):
                continue
            eltern[ra] = rb
            zeiten[rb] = za + zb
            zeiten.pop(ra, None)
        gruppen: dict[int, list[int]] = {}
        for x in list(eltern):
            gruppen.setdefault(wurzel(x), []).append(x)
        erg = []
        for ids in gruppen.values():
            ids = [i for i in ids if Path(info[i]["pfad"]).exists()]
            if len(ids) < 2:
                continue
            shas = {cache[i]["sha1"] for i in ids}
            art = "exakt" if len(shas) == 1 and None not in shas else "aehnlich"
            erg.append(gruppe_bauen(self.db, ids, art))
        erg.sort(key=lambda g: (g["art"] != "exakt", -g["sparen"]))
        for n, g in enumerate(erg, 1):
            g["nr"] = n
        return erg

    def _fertig(self, gruppen):
        abgebrochen = gruppen is None
        self.status.update(laeuft=False, phase="angehalten" if abgebrochen else "fertig",
                           meldung="Angehalten." if abgebrochen else
                           (f"{len(gruppen)} Gruppen gefunden." if gruppen else "Keine Dubletten gefunden."))
        return {"gruppen": gruppen or [], "zeit": datetime.now().isoformat(timespec="seconds"),
                "abgebrochen": abgebrochen}


def _seitenverhaeltnis(z) -> float:
    if not z["breite"] or not z["hoehe"]:
        return 0.0
    a, b = sorted((z["breite"], z["hoehe"]))
    return b / a


def _kamerazeit(z):
    if z["datum_quelle"] != "exif" or not z["datum"]:
        return None
    try:
        return datetime.fromisoformat(z["datum"])
    except ValueError:
        return None


# ---------------------------------------------------------------- Vorschlag

def gruppe_bauen(db: Datenbank, ids: list[int], art: str) -> dict:
    platz = ",".join("?" * len(ids))
    zeilen = [dict(r) for r in db.abfrage(
        f"SELECT id, pfad, groesse, mtime, breite, hoehe, datum, datum_quelle, kamera FROM bilder "
        f"WHERE id IN ({platz})", ids)]
    bilder = []
    for z in zeilen:
        p = Path(z["pfad"])
        bilder.append({"id": z["id"], "pfad": z["pfad"], "name": p.name, "ordner": str(p.parent),
                       "groesse": z["groesse"] or 0, "breite": z["breite"], "hoehe": z["hoehe"],
                       "datum": z["datum"], "datum_quelle": z["datum_quelle"], "kamera": z["kamera"] or "",
                       "geaendert": datetime.fromtimestamp(z["mtime"]).isoformat(sep=" ", timespec="seconds")
                       if z["mtime"] else None, "_mtime": z["mtime"] or 0})
    behalten, gruende = vorschlag(bilder)
    for b in bilder:
        b.pop("_mtime")
        b["behalten"] = b["id"] == behalten
    bilder.sort(key=lambda b: (not b["behalten"], b["pfad"]))
    sparen = sum(b["groesse"] for b in bilder if not b["behalten"])
    return {"art": art, "bilder": bilder, "behalten": behalten, "gruende": gruende, "sparen": sparen}


def _kopie_name(b) -> bool:
    return bool(KOPIE_MUSTER.search(Path(b["name"]).stem)) or "whatsapp" in b["ordner"].lower()


def vorschlag(bilder: list[dict]) -> tuple[int, list[str]]:
    """Welches Bild behalten? Höchste Auflösung, Original-EXIF, größte Datei, Name ohne
    Kopie-Kennzeichen, älteste Datei, kürzester Pfad (in dieser Reihenfolge)."""
    def punkte(b):
        return ((b["breite"] or 0) * (b["hoehe"] or 0), b["datum_quelle"] == "exif", b["groesse"],
                not _kopie_name(b), -b["_mtime"], -len(b["pfad"]))

    beste = max(bilder, key=punkte)
    andere = [b for b in bilder if b is not beste]
    gruende = []
    px = (beste["breite"] or 0) * (beste["hoehe"] or 0)
    if any((b["breite"] or 0) * (b["hoehe"] or 0) < px for b in andere):
        gruende.append("höchste Auflösung")
    if beste["datum_quelle"] == "exif" and any(b["datum_quelle"] != "exif" for b in andere):
        gruende.append("Aufnahmedaten der Kamera")
    if any(b["groesse"] < beste["groesse"] for b in andere):
        gruende.append("größte Datei")
    if not _kopie_name(beste) and any(_kopie_name(b) for b in andere):
        gruende.append("Name ohne Kopie-Kennzeichen")
    if any(b["_mtime"] > beste["_mtime"] for b in andere):
        gruende.append("älteste Datei")
    return beste["id"], gruende or ["kürzester Pfad"]


# ---------------------------------------------------------------- Bereinigen

def _quarantaene_ziel(quarantaene: Path, lauf: str, pfad: Path) -> Path:
    """Ursprüngliche Ordnerstruktur nachbauen: D:\\Fotos\\a.jpg -> <Q>\\<lauf>\\D\\Fotos\\a.jpg"""
    laufwerk, rest = os.path.splitdrive(str(pfad))
    teile = [t for t in Path(rest).parts if t not in ("\\", "/")]
    kopf = laufwerk.replace(":", "").strip("\\/").replace("\\", "_") or "_"
    return quarantaene / lauf / kopf / Path(*teile)


def bereinigen(db: Datenbank, weg_ids: list[int], behalten_ids: set[int], art: str, quarantaene: Path,
               protokoll_ordner: Path) -> dict:
    """Dateien in Quarantäne oder Papierkorb legen. Ein Bild, das zum Behalten markiert ist,
    wird nie angefasst. Liefert Protokolldatei, verschobene Bild-ids und Fehler."""
    if art not in ("quarantaene", "papierkorb"):
        raise ValueError("Unbekanntes Ziel")
    if art == "papierkorb" and not papierkorb.verfuegbar():
        raise ValueError("Den Papierkorb gibt es nur unter Windows. Bitte den Quarantäne-Ordner wählen.")
    weg_ids = [i for i in dict.fromkeys(int(x) for x in weg_ids) if i not in behalten_ids]
    if not weg_ids:
        raise ValueError("Keine Dateien zum Aussortieren gewählt.")
    platz = ",".join("?" * len(weg_ids))
    pfade = {r["id"]: Path(r["pfad"]) for r in db.abfrage(f"SELECT id, pfad FROM bilder WHERE id IN ({platz})",
                                                          weg_ids)}
    protokoll_ordner.mkdir(parents=True, exist_ok=True)
    # eigener Name je Lauf, auch bei zwei Läufen in derselben Sekunde: sonst landen beide im selben
    # Protokoll (Rückgängig nimmt beide zurück) und im selben Quarantäne-Ordner
    basis = f"{datetime.now():%Y%m%d-%H%M%S}"
    lauf, n = basis, 1
    while (protokoll_ordner / f"dubletten-{lauf}.jsonl").exists() or (quarantaene / lauf).exists():
        n += 1
        lauf = f"{basis}_{n}"
    prot_datei = protokoll_ordner / f"dubletten-{lauf}.jsonl"
    erledigt, fehler, bytes_ = [], [], 0
    with open(prot_datei, "a", encoding="utf-8") as prot:
        def schreibe(e):
            e["zeit"] = datetime.now().isoformat(timespec="seconds")
            prot.write(json.dumps(e, ensure_ascii=False) + "\n")
            prot.flush()

        for i in weg_ids:
            p = pfade.get(i)
            if p is None or not p.exists():
                fehler.append(f"Nicht gefunden: {p or i}")
                continue
            try:
                groesse = p.stat().st_size
                begleiter = _sidecars(p)
                if art == "papierkorb":
                    papierkorb.hineinlegen(p)
                    schreibe({"op": "papierkorb", "quelle": str(p), "ziel": str(p)})
                    for x in begleiter:
                        papierkorb.hineinlegen(x)
                        schreibe({"op": "papierkorb", "quelle": str(x), "ziel": str(x)})
                else:
                    ziel = _quarantaene_ziel(quarantaene, lauf, p)
                    k = 1
                    while ziel.exists():  # nie eine Datei in der Quarantäne überschreiben
                        k += 1
                        ziel = _quarantaene_ziel(quarantaene, lauf, p.with_name(f"{p.stem}_{k}{p.suffix}"))
                    ziel.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(p), str(ziel))
                    schreibe({"op": "verschoben", "quelle": str(p), "ziel": str(ziel)})
                    for x in begleiter:
                        x_ziel = ziel.parent / x.name
                        if not x_ziel.exists():
                            shutil.move(str(x), str(x_ziel))
                            schreibe({"op": "verschoben", "quelle": str(x), "ziel": str(x_ziel)})
                erledigt.append(i)
                bytes_ += groesse
            except Exception as ex:
                fehler.append(f"{p}: {ex}")
    if not erledigt:
        prot_datei.unlink(missing_ok=True)
    return {"protokoll": str(prot_datei) if erledigt else None, "erledigt": erledigt, "bytes": bytes_,
            "fehler": fehler[:100]}
