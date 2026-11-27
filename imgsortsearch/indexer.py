"""Suchindex aufbauen: läuft im Hintergrund, Schritt für Schritt, jederzeit abbrechbar.

1. Dateien finden und Metadaten lesen (schnell)
2. Pro Bild: Vorschaubild, Bildvektor (CLIP), Gesichter, Texterkennung, Beschreibung
3. Alle Bilder neu einordnen und neue Gesichter Personen zuordnen

Bereits erledigte Schritte werden übersprungen, ein Abbruch verliert also nichts.
"""
from __future__ import annotations

import logging
import threading
import time
import traceback
from pathlib import Path

import numpy as np

from . import metadata, modelle
from .datenbank import Datenbank, normalisiere, vektor_zu_blob
from .einstellungen import Einstellungen, daten_ordner
from .klassifizierung import Themen, einordnen, thema_prompt

log = logging.getLogger(__name__)

VORSCHAU_KANTE = 360
TEXT_ENDUNGEN = {".png"}  # Screenshots sind fast immer PNG


def vorschau_pfad(bild_id: int) -> Path:
    p = daten_ordner() / "vorschau" / f"{bild_id // 1000:03d}" / f"{bild_id}.jpg"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def vorschau_erzeugen(bild_id: int, img) -> Path:
    p = vorschau_pfad(bild_id)
    klein = img.copy()
    klein.thumbnail((VORSCHAU_KANTE, VORSCHAU_KANTE))
    klein.save(p, "JPEG", quality=82)
    return p


class Modelle:
    """Lädt Modelle erst, wenn sie gebraucht werden, und hält sie dann im Speicher."""

    def __init__(self, e: Einstellungen):
        self.e = e
        self._clip = self._gesichter = self._ocr = self._beschreiber = None
        self._lock = threading.Lock()
        self.fehler: dict[str, str] = {}

    def _laden(self, name, fabrik):
        with self._lock:
            attr = f"_{name}"
            if getattr(self, attr) is None and name not in self.fehler:
                try:
                    setattr(self, attr, fabrik())
                except Exception as ex:
                    log.exception("Modell %s nicht verfügbar", name)
                    self.fehler[name] = str(ex)
            return getattr(self, attr)

    def clip(self):
        c = self.e["clip"]
        return self._laden("clip", lambda: modelle.ClipModell(c["modell"], c["pretrained"],
                                                               self.e.get("beschleunigung", "auto")))

    def gesichter(self):
        return self._laden("gesichter", lambda: modelle.GesichtsModell(self.e["gesichter"]["min_groesse"]))

    def ocr(self):
        o = self.e["ocr"]
        return self._laden("ocr", lambda: modelle.OCR(o["backend"], o["sprache"]))

    def beschreiber(self):
        b = self.e["beschreibung"]
        return self._laden("beschreiber", lambda: modelle.Beschreiber(b["modell"], b.get("uebersetzer", ""),
                                                                       self.e.get("beschleunigung", "auto")))

    def geraete(self) -> dict:
        """Welche Modelle gerade auf welchem Gerät laufen."""
        erg = {}
        for name in ("clip", "beschreiber"):
            m = getattr(self, f"_{name}")
            if m is not None and hasattr(m, "geraet_name"):
                erg[name] = m.geraet_name
        return erg

    def entladen(self) -> None:
        with self._lock:
            self._clip = self._beschreiber = None
            self.fehler.pop("clip", None)
            self.fehler.pop("beschreiber", None)

    def clip_kennung(self) -> str:
        c = self.e["clip"]
        return f"{c['modell']}/{c['pretrained']}"


def lade_themen(db: Datenbank, e: Einstellungen, modelle_: Modelle, laden: bool = False) -> Themen | None:
    """Themen mit Vektoren. Ohne geladenes Modell nur aus dem Zwischenspeicher."""
    themen = e["themen"]
    if not themen:
        return None
    clip = modelle_.clip() if laden else modelle_._clip
    vek = db.textvektoren([thema_prompt(t) for t in themen], modelle_.clip_kennung(), clip)
    return Themen(themen, vek) if vek is not None else None


def alle_neu_einordnen(db: Datenbank, e: Einstellungen, modelle_: Modelle) -> int:
    """Wendet Themen, Arbeitszeit und Regeln auf alle Bilder an (ohne neue Analyse)."""
    themen = lade_themen(db, e, modelle_, laden=False)
    zeilen = db.abfrage("SELECT * FROM bilder")
    ids, mat = db.clip_matrix()
    auswertung = {}
    if themen is not None and len(ids):
        for i, a in zip(ids, themen.auswerten(mat)):
            auswertung[int(i)] = a
    updates = []
    for z in zeilen:
        d = dict(z)
        inh = auswertung.get(d["id"])
        erg = einordnen(d, inh, e.werte)
        tags = ", ".join(inh["tags"]) if inh else d.get("tags") or ""
        updates.append((erg["kategorie"], erg["sicherheit"], erg["gruende"], tags, d["id"]))
    with db.verbindung() as v:
        v.executemany("UPDATE bilder SET kategorie_auto=?, sicherheit=?, gruende=?, tags=? WHERE id=?", updates)
    for z in zeilen:
        db.suchtext_aktualisieren(z["id"])
    return len(updates)


class Indexer:
    def __init__(self, db: Datenbank, e: Einstellungen, modelle_: Modelle):
        self.db, self.e, self.m = db, e, modelle_
        self.status = {"laeuft": False, "phase": "bereit", "fertig": 0, "gesamt": 0,
                       "meldung": "", "fehler": [], "start": None, "aktuell": None, "aktuell_pfad": ""}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.nach_lauf = []  # Callbacks, z. B. Personen zuordnen

    # ---------------------------------------------------------- Steuerung

    def starten(self, schritte: set[str] | None = None) -> bool:
        if self._thread and self._thread.is_alive():
            return False
        self._stop.clear()
        self._thread = threading.Thread(target=self._lauf, args=(schritte,), daemon=True)
        self._thread.start()
        return True

    def stoppen(self) -> None:
        self._stop.set()

    def _setze(self, **kw):
        self.status.update(kw)

    def _lauf(self, schritte):
        self._setze(laeuft=True, fehler=[], start=time.time(), meldung="")
        try:
            self.dateien_einlesen()
            if not self._stop.is_set():
                self.analysieren(schritte)
            self._setze(phase="einordnen", meldung="Bilder werden eingeordnet ...")
            alle_neu_einordnen(self.db, self.e, self.m)
            for cb in self.nach_lauf:
                cb()
            self._setze(phase="fertig" if not self._stop.is_set() else "angehalten",
                        meldung="Index ist aktuell." if not self._stop.is_set() else "Angehalten.")
        except Exception as ex:
            log.exception("Indexlauf fehlgeschlagen")
            self._setze(phase="fehler", meldung=f"Fehler: {ex}")
            self.status["fehler"].append(traceback.format_exc(limit=3))
        finally:
            self._setze(laeuft=False)

    # ---------------------------------------------------------- Schritt 1

    def dateien_einlesen(self):
        self._setze(phase="suchen", meldung="Suche Dateien ...", fertig=0, gesamt=0)
        gefunden: list[Path] = []
        for q in self.e["quellen"]:
            qp = Path(q)
            if qp.is_dir():
                gefunden += metadata.finde_bilder(qp, videos=False)
        bekannt = {z["pfad"]: (z["id"], z["groesse"], z["mtime"])
                   for z in self.db.abfrage("SELECT id, pfad, groesse, mtime FROM bilder")}

        # verschwundene Dateien austragen (nur unterhalb erreichbarer Quellen)
        erreichbar = [str(Path(q)) for q in self.e["quellen"] if Path(q).is_dir()]
        vorhanden = {str(p) for p in gefunden}
        weg = [i for pfad, (i, _, _) in bekannt.items()
               if pfad not in vorhanden and any(pfad.startswith(q) for q in erreichbar)]
        if weg:
            with self.db.verbindung() as v:
                v.executemany("DELETE FROM bilder WHERE id=?", [(i,) for i in weg])

        neu_oder_geaendert = []
        for p in gefunden:
            try:
                st = p.stat()
            except OSError:
                continue
            alt = bekannt.get(str(p))
            if not alt or alt[1] != st.st_size or abs((alt[2] or 0) - st.st_mtime) > 1:
                neu_oder_geaendert.append(p)
        self._setze(phase="metadaten", gesamt=len(neu_oder_geaendert), fertig=0,
                    meldung=f"{len(gefunden)} Dateien, {len(neu_oder_geaendert)} neu oder geändert. Lese Metadaten ...")
        for i in range(0, len(neu_oder_geaendert), 400):
            if self._stop.is_set():
                return
            teil = neu_oder_geaendert[i:i + 400]
            infos = metadata.lies_metadaten(teil)
            with self.db.verbindung() as v:
                for info in infos:
                    st = info.pfad.stat()
                    v.execute("""
                        INSERT INTO bilder (pfad, groesse, mtime, datum, datum_quelle, hersteller, modell, kamera,
                                            gps_lat, gps_lon, breite, hoehe, schlagwoerter)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(pfad) DO UPDATE SET groesse=excluded.groesse, mtime=excluded.mtime,
                          datum=excluded.datum, datum_quelle=excluded.datum_quelle, hersteller=excluded.hersteller,
                          modell=excluded.modell, kamera=excluded.kamera, gps_lat=excluded.gps_lat,
                          gps_lon=excluded.gps_lon, breite=excluded.breite, hoehe=excluded.hoehe,
                          schlagwoerter=excluded.schlagwoerter, clip=NULL, schritt_clip=0, schritt_ocr=0,
                          schritt_gesichter=0, schritt_beschreibung=0, fehler=NULL""",
                              (str(info.pfad), st.st_size, st.st_mtime,
                               info.datum.isoformat(sep=" ") if info.datum else None, info.datum_quelle,
                               info.hersteller, info.modell, info.kamera, info.gps_lat, info.gps_lon,
                               info.breite, info.hoehe, ", ".join(info.schlagwoerter)))
            self._setze(fertig=min(len(neu_oder_geaendert), i + 400))

    # ---------------------------------------------------------- Schritt 2

    def _offen(self) -> list:
        e = self.e
        bedingungen = ["schritt_clip=0"]
        if e["gesichter"]["aktiv"]:
            bedingungen.append("schritt_gesichter=0")
        if e["ocr"]["modus"] != "aus":
            bedingungen.append("schritt_ocr=0")
        if e["beschreibung"]["aktiv"]:
            bedingungen.append("schritt_beschreibung=0")
        return self.db.abfrage(f"SELECT id, pfad, schritt_clip, schritt_ocr, schritt_gesichter, "
                               f"schritt_beschreibung FROM bilder WHERE ({' OR '.join(bedingungen)}) "
                               f"AND fehler IS NULL ORDER BY datum DESC")

    def analysieren(self, schritte=None):
        offen = self._offen()
        self._setze(phase="analyse", gesamt=len(offen), fertig=0,
                    meldung=f"Analysiere {len(offen)} Bilder (beim ersten Mal werden Modelle geladen) ...")
        if not offen:
            return
        e = self.e
        clip = self.m.clip()
        if clip is None:
            raise RuntimeError(f"Bildmodell (CLIP) nicht verfügbar: {self.m.fehler.get('clip')}")
        text_vek = self.db.textvektoren(modelle.PROMPTS_TEXT + modelle.PROMPTS_KEIN_TEXT,
                                        self.m.clip_kennung(), clip)
        n_text = len(modelle.PROMPTS_TEXT)
        gesichter = self.m.gesichter() if e["gesichter"]["aktiv"] else None
        ocr = self.m.ocr() if e["ocr"]["modus"] != "aus" else None
        beschreiber = self.m.beschreiber() if e["beschreibung"]["aktiv"] else None
        stapel = int(e["clip"].get("stapel", 16))
        t0, erledigt = time.time(), 0

        for i in range(0, len(offen), stapel):
            if self._stop.is_set():
                return
            teil = offen[i:i + stapel]
            geladen = []
            for z in teil:
                try:
                    img = modelle.bild_laden(Path(z["pfad"]), max_kante=2000)
                    vorschau_erzeugen(z["id"], img)
                    geladen.append((z, img))
                    self._setze(aktuell=z["id"], aktuell_pfad=z["pfad"])
                except Exception as ex:
                    with self.db.verbindung() as v:
                        v.execute("UPDATE bilder SET fehler=? WHERE id=?", (str(ex)[:300], z["id"]))
            if not geladen:
                continue

            # Bildvektoren im Stapel
            brauchen_clip = [(z, img) for z, img in geladen if not z["schritt_clip"]]
            vek_map = {}
            if brauchen_clip:
                vek = clip.bilder([img for _, img in brauchen_clip])
                pt = modelle.softmax(vek @ text_vek.T)[:, :n_text].sum(axis=1)
                with self.db.verbindung() as v:
                    for (z, _), x, t in zip(brauchen_clip, vek, pt):
                        v.execute("UPDATE bilder SET clip=?, textlastig=?, schritt_clip=1 WHERE id=?",
                                  (vektor_zu_blob(x), float(t), z["id"]))
                        vek_map[z["id"]] = float(t)

            # Beschreibungen im Stapel
            if beschreiber:
                brauchen = [(z, img) for z, img in geladen if not z["schritt_beschreibung"]]
                if brauchen:
                    try:
                        texte = beschreiber.beschreiben([img for _, img in brauchen])
                        with self.db.verbindung() as v:
                            for (z, _), (de, en) in zip(brauchen, texte):
                                v.execute("UPDATE bilder SET beschreibung=?, beschreibung_en=?, "
                                          "schritt_beschreibung=1 WHERE id=?", (de, en, z["id"]))
                    except Exception as ex:
                        self._fehler(f"Beschreibung: {ex}")

            for z, img in geladen:
                if gesichter and not z["schritt_gesichter"]:
                    try:
                        gef = gesichter.finde(img)
                        bw, bh = img.size  # Boxen relativ (0..1) speichern
                        with self.db.verbindung() as v:
                            v.execute("DELETE FROM gesichter WHERE bild_id=?", (z["id"],))
                            for g in gef:
                                v.execute("INSERT INTO gesichter (bild_id, x, y, w, h, score, vektor) "
                                          "VALUES (?,?,?,?,?,?,?)",
                                          (z["id"], g["x"] / bw, g["y"] / bh, g["w"] / bw, g["h"] / bh, g["score"],
                                           vektor_zu_blob(g["vektor"])))
                            v.execute("UPDATE bilder SET schritt_gesichter=1, anzahl_gesichter=? WHERE id=?",
                                      (len(gef), z["id"]))
                    except Exception as ex:
                        self._fehler(f"Gesichter {Path(z['pfad']).name}: {ex}")
                if ocr and not z["schritt_ocr"]:
                    text = ""
                    textlastig = vek_map.get(z["id"])
                    if textlastig is None:
                        r = self.db.eins("SELECT textlastig FROM bilder WHERE id=?", (z["id"],))
                        textlastig = r["textlastig"] or 0
                    pfad = Path(z["pfad"])
                    lohnt = (e["ocr"]["modus"] == "alle" or textlastig >= 0.5
                             or pfad.suffix.lower() in TEXT_ENDUNGEN or "screenshot" in pfad.name.lower())
                    if lohnt:
                        try:
                            text = ocr.lesen(img)
                        except Exception as ex:
                            self._fehler(f"Texterkennung {pfad.name}: {ex}")
                    with self.db.verbindung() as v:
                        v.execute("UPDATE bilder SET ocr_text=?, schritt_ocr=1 WHERE id=?", (text, z["id"]))
                self.db.suchtext_aktualisieren(z["id"])
                erledigt += 1

            dauer = time.time() - t0
            rest = (len(offen) - erledigt) * dauer / max(1, erledigt)
            self._setze(fertig=min(len(offen), i + len(teil)),
                        meldung=f"Analysiere Bilder ... noch etwa {_dauer_text(rest)}")

    def _fehler(self, text):
        log.warning(text)
        if len(self.status["fehler"]) < 50:
            self.status["fehler"].append(text)


def _dauer_text(sek: float) -> str:
    if sek < 90:
        return f"{int(sek)} s"
    if sek < 5400:
        return f"{int(sek / 60)} min"
    return f"{sek / 3600:.1f} h"


def text_suche_bedingung(begriffe: str) -> tuple[str, list]:
    """SQL-Bedingung: alle Wörter müssen im Suchtext vorkommen."""
    woerter = [w for w in normalisiere(begriffe).split() if w]
    if not woerter:
        return "1=1", []
    return " AND ".join("suchtext LIKE ?" for _ in woerter), [f"%{w}%" for w in woerter]


__all__ = ["Indexer", "Modelle", "alle_neu_einordnen", "lade_themen", "vorschau_pfad", "np"]
