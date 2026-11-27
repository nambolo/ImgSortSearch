"""Suchindex aufbauen: läuft im Hintergrund, Schritt für Schritt, jederzeit abbrechbar.

1. Dateien finden und Metadaten lesen (schnell)
2. Pro Bild: Vorschaubild, Bildvektor (CLIP), Gesichter, Texterkennung, Beschreibung
3. Alle Bilder neu einordnen und neue Gesichter Personen zuordnen

Bereits erledigte Schritte werden übersprungen, ein Abbruch verliert also nichts.
"""
from __future__ import annotations

import logging
import os
import threading
import time
import traceback
from pathlib import Path

import numpy as np

from . import metadata, modelle, personen
from .datenbank import Datenbank, normalisiere, vektor_zu_blob
from .einstellungen import Einstellungen, daten_ordner
from .klassifizierung import Themen, einordnen, thema_prompt, zusatz_kategorien

log = logging.getLogger(__name__)

VORSCHAU_KANTE = 360
TEXT_ENDUNGEN = {".png"}  # Screenshots sind fast immer PNG


def vorschau_pfad(bild_id: int) -> Path:
    p = daten_ordner() / "vorschau" / f"{bild_id // 1000:03d}" / f"{bild_id}.jpg"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def gesicht_cache_pfad(gesicht_id: int) -> Path:
    return daten_ordner() / "gesichter" / f"{gesicht_id}.jpg"


def caches_entfernen(bild_ids=(), gesicht_ids=()) -> None:
    """Vorschau- und Gesichtsbilder löschen. SQLite vergibt die Nummern gelöschter
    Einträge neu, sonst zeigt ein neues Bild die Vorschau eines alten."""
    for p in [vorschau_pfad(i) for i in bild_ids] + [gesicht_cache_pfad(i) for i in gesicht_ids]:
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass


def _unter(pfad: str, ordner: list[str]) -> bool:
    """Liegt ``pfad`` in einem der Ordner? (D:\\Fotos2 liegt nicht in D:\\Fotos)"""
    pfad = os.path.normcase(pfad)
    for o in ordner:
        o = os.path.normcase(o).rstrip("\\/")
        if pfad.startswith(o + os.sep) or pfad.startswith(o + "/"):
            return True
    return False


def bilder_entfernen(db: Datenbank, ids: list[int]) -> int:
    """Bilder aus dem Index austragen (mit Gesichtern und Vorschaubildern). Die Dateien bleiben unberührt."""
    gesicht_ids = []
    for start in range(0, len(ids), 500):
        teil = ids[start:start + 500]
        gesicht_ids += [r["id"] for r in db.abfrage(
            f"SELECT id FROM gesichter WHERE bild_id IN ({','.join('?' * len(teil))})", teil)]
    with db.verbindung() as v:
        v.executemany("DELETE FROM bilder WHERE id=?", [(i,) for i in ids])
        # Personen ohne Gesichter bleiben sonst als leere Einträge zurück
        v.execute("DELETE FROM personen WHERE id NOT IN (SELECT DISTINCT person_id FROM gesichter "
                  "WHERE person_id IS NOT NULL)")
    caches_entfernen(ids, gesicht_ids)
    return len(ids)


def bilder_im_ordner(db: Datenbank, ordner: str, ausser: list[str] = ()) -> list[int]:
    """Bilder im Index, die in ``ordner`` liegen, aber in keinem der Ordner aus ``ausser``
    (z. B. wenn ein anderer Bilder-Ordner darüber oder darunter weiter eingetragen ist)."""
    return [r["id"] for r in db.abfrage("SELECT id, pfad FROM bilder")
            if _unter(r["pfad"], [ordner]) and not _unter(r["pfad"], list(ausser))]


def quarantaene_ordner(e: Einstellungen) -> Path:
    return Path((e.get("dubletten") or {}).get("quarantaene") or (daten_ordner() / "dubletten-quarantaene"))


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
        self._geladen_mit: dict[str, tuple] = {}  # Einstellungen, mit denen ein Modell geladen wurde

    def _laden(self, name, schluessel: tuple, fabrik):
        """Lädt bei Bedarf. Wurden die Einstellungen des Modells (Modellname, Grafikkarte, Sprache ...)
        seit dem Laden geändert, wird es neu geladen, statt still mit dem alten weiterzuarbeiten."""
        with self._lock:
            attr = f"_{name}"
            alt = self._geladen_mit.get(name)
            if alt is not None and alt != schluessel:
                setattr(self, attr, None)
                self.fehler.pop(name, None)
                self._geladen_mit.pop(name, None)
            if getattr(self, attr) is None and name not in self.fehler:
                try:
                    setattr(self, attr, fabrik())
                    self._geladen_mit[name] = schluessel
                except Exception as ex:
                    log.exception("Modell %s nicht verfügbar", name)
                    self.fehler[name] = str(ex)
            return getattr(self, attr)

    def clip(self):
        c, beschl = self.e["clip"], self.e.get("beschleunigung", "auto")
        return self._laden("clip", (c["modell"], c["pretrained"], beschl),
                           lambda: modelle.ClipModell(c["modell"], c["pretrained"], beschl))

    def gesichter(self):
        g = self.e["gesichter"]["min_groesse"]
        return self._laden("gesichter", (g,), lambda: modelle.GesichtsModell(g))

    def ocr(self):
        o = self.e["ocr"]
        return self._laden("ocr", (o["backend"], o["sprache"]), lambda: modelle.OCR(o["backend"], o["sprache"]))

    def beschreiber(self):
        b, beschl = self.e["beschreibung"], self.e.get("beschleunigung", "auto")
        return self._laden("beschreiber", (b["modell"], b.get("uebersetzer", ""), beschl),
                           lambda: modelle.Beschreiber(b["modell"], b.get("uebersetzer", ""), beschl))

    def clip_falls_geladen(self):
        """Das Bildmodell, falls es schon geladen ist und noch zu den Einstellungen passt."""
        c = self.e["clip"]
        if self._geladen_mit.get("clip") not in (None, (c["modell"], c["pretrained"],
                                                        self.e.get("beschleunigung", "auto"))):
            return None
        return self._clip

    def geraete(self) -> dict:
        """Welche Modelle gerade auf welchem Gerät laufen."""
        erg = {}
        for name in ("clip", "beschreiber"):
            m = getattr(self, f"_{name}")
            if m is not None and hasattr(m, "geraet_name"):
                erg[name] = m.geraet_name
        return erg

    def entladen(self, *namen: str) -> None:
        """Modelle freigeben, damit sie beim nächsten Gebrauch neu geladen werden
        (anderes Gerät oder anderes Modell). Ohne Namen: Bildmodell und Beschreiber."""
        with self._lock:
            for name in namen or ("clip", "beschreiber"):
                setattr(self, f"_{name}", None)
                self.fehler.pop(name, None)
                self._geladen_mit.pop(name, None)

    def fehler_vergessen(self) -> None:
        """Beim nächsten Gebrauch erneut laden (z. B. Download war beim letzten Mal offline)."""
        with self._lock:
            self.fehler.clear()

    def clip_kennung(self) -> str:
        c = self.e["clip"]
        return f"{c['modell']}/{c['pretrained']}"


def lade_themen(db: Datenbank, e: Einstellungen, modelle_: Modelle, laden: bool = False) -> Themen | None:
    """Themen mit Vektoren. Ohne geladenes Modell nur aus dem Zwischenspeicher."""
    themen = e["themen"]
    if not themen:
        return None
    clip = modelle_.clip() if laden else modelle_.clip_falls_geladen()
    vek = db.textvektoren([thema_prompt(t) for t in themen], modelle_.clip_kennung(), clip)
    return Themen(themen, vek) if vek is not None else None


def alle_neu_einordnen(db: Datenbank, e: Einstellungen, modelle_: Modelle) -> int:
    """Wendet Themen, Arbeitszeit und Regeln auf alle Bilder an (ohne neue Analyse)."""
    ids, mat = db.clip_matrix()
    themen = lade_themen(db, e, modelle_, laden=False)
    if themen is None and len(ids) and e["themen"]:
        # Themen-Vektoren fehlen im Zwischenspeicher (neue Themen, anderes Modell):
        # ohne sie würde der Bildinhalt still ignoriert, also Modell laden.
        themen = lade_themen(db, e, modelle_, laden=True)
    if themen is not None and len(ids) and mat.shape[1] != themen.vektoren.shape[1]:
        log.warning("Bildvektoren passen nicht zum Bildmodell, Inhalt wird beim Einordnen ignoriert")
        themen = None
    zeilen = db.abfrage("SELECT id, pfad, datum, datum_quelle, hersteller, modell, gps_lat, gps_lon, "
                        "anzahl_gesichter, suchtext, tags, zusatz_auto FROM bilder")
    zusatz = zusatz_kategorien(e.werte)
    # Themen, die auf eine Zusatzkategorie zeigen, vergeben sie automatisch (zusätzlich zur Hauptkategorie)
    thema_zusatz = {t["name"]: t["kategorie"] for t in e["themen"] if t.get("kategorie") in zusatz}
    auswertung = {}
    if themen is not None and len(ids):
        for i, a in zip(ids, themen.auswerten(mat)):
            auswertung[int(i)] = a
    updates, tags_neu = [], []
    for z in zeilen:
        d = dict(z)
        inh = auswertung.get(d["id"])
        erg = einordnen(d, inh, e.werte)
        tags = ", ".join(inh["tags"]) if inh else d.get("tags") or ""
        z_auto = ",".join(dict.fromkeys(thema_zusatz[t] for t in inh["tags"] if t in thema_zusatz)) if inh \
            else d.get("zusatz_auto") or ""
        updates.append((erg["kategorie"], erg["sicherheit"], erg["gruende"], tags, z_auto, d["id"]))
        if tags != (d.get("tags") or ""):
            tags_neu.append(d["id"])
    with db.verbindung() as v:
        v.executemany("UPDATE bilder SET kategorie_auto=?, sicherheit=?, gruende=?, tags=?, zusatz_auto=? "
                      "WHERE id=?", updates)
    db.suchtext_aktualisieren_viele(tags_neu)  # nur wo sich die Schlagwörter geändert haben
    db.zusatz_aktualisieren(zusatz)
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
        self.m.fehler_vergessen()  # Modelle, die beim letzten Mal fehlten, erneut versuchen
        try:
            self._bildmodell_pruefen()
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

    def _bildmodell_pruefen(self):
        """Wurde in den Einstellungen ein anderes Bildmodell gewählt, passen die gespeicherten
        Bildvektoren nicht mehr zur Suche. Dann werden sie neu berechnet."""
        kennung = self.m.clip_kennung()
        alt = self.db.meta("clip_kennung")
        if alt is None:
            self.db.meta_setzen("clip_kennung", kennung)
        elif alt != kennung:
            log.info("Bildmodell gewechselt (%s -> %s), Bildvektoren werden neu berechnet", alt, kennung)
            with self.db.verbindung() as v:
                v.execute("UPDATE bilder SET clip=NULL, schritt_clip=0")
            self.db.meta_setzen("clip_kennung", kennung)

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
        weg = [i for pfad, (i, _, _) in bekannt.items() if pfad not in vorhanden and _unter(pfad, erreichbar)]
        if weg:
            bilder_entfernen(self.db, weg)

        # Kopien, die das Programm selbst in einen Zielordner innerhalb der Quellen gelegt hat,
        # nicht als neue Bilder aufnehmen (sonst ist danach alles doppelt im Index)
        ziel = (self.e.get("sortieren") or {}).get("ziel")
        if ziel and _unter(str(Path(ziel)), erreichbar) and not any(
                _unter(q, [str(Path(ziel))]) for q in erreichbar):  # Ziel liegt echt innerhalb einer Quelle
            gefunden = [p for p in gefunden if str(p) in bekannt or not _unter(str(p), [str(Path(ziel))])]

        # aussortierte Dubletten im Quarantäne-Ordner nicht wieder aufnehmen
        quarantaene = str(quarantaene_ordner(self.e))
        gefunden = [p for p in gefunden if not _unter(str(p), [quarantaene])]

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
                    try:
                        st = info.pfad.stat()
                    except OSError:  # inzwischen gelöscht oder Laufwerk weg
                        continue
                    try:
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
                    except Exception as ex:  # ein Bild mit seltsamen Metadaten bricht nicht den Lauf ab
                        self._fehler(f"Metadaten {info.pfad.name}: {ex}")
            # Suchtext (Dateiname, Ordner, Kamera) gleich anlegen: so findet die Suche neue Bilder schon
            # während der Analyse läuft, und auch Dateien, die sich nicht analysieren lassen
            pfade = [str(p) for p in teil]
            self.db.suchtext_aktualisieren_viele(r["id"] for r in self.db.abfrage(
                f"SELECT id FROM bilder WHERE pfad IN ({','.join('?' * len(pfade))})", pfade))
            self._setze(fertig=min(len(neu_oder_geaendert), i + 400))
        # ältere Indexe: Bilder, die nie analysiert werden konnten, hatten keinen Suchtext
        if not self._stop.is_set():
            self.db.suchtext_aktualisieren_viele(
                r["id"] for r in self.db.abfrage("SELECT id FROM bilder WHERE suchtext IS NULL OR suchtext=''"))

    # ---------------------------------------------------------- Schritt 2

    def _aktive_schritte(self) -> set[str]:
        e = self.e
        aktiv = {"clip"}
        if e["gesichter"]["aktiv"]:
            aktiv.add("gesichter")
        if e["ocr"]["modus"] != "aus":
            aktiv.add("ocr")
        if e["beschreibung"]["aktiv"]:
            aktiv.add("beschreibung")
        return aktiv

    def _offen(self, aktiv: set[str] | None = None) -> list:
        aktiv = self._aktive_schritte() if aktiv is None else aktiv
        bedingungen = [f"schritt_{s}=0" for s in ("clip", "gesichter", "ocr", "beschreibung") if s in aktiv]
        return self.db.abfrage(f"SELECT id, pfad, schritt_clip, schritt_ocr, schritt_gesichter, "
                               f"schritt_beschreibung FROM bilder WHERE ({' OR '.join(bedingungen)}) "
                               f"AND fehler IS NULL ORDER BY datum DESC")

    def analysieren(self, schritte=None):
        aktiv = self._aktive_schritte()
        offen = self._offen(aktiv)
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
        gesichter = self.m.gesichter() if "gesichter" in aktiv else None
        ocr = self.m.ocr() if "ocr" in aktiv else None
        beschreiber = self.m.beschreiber() if "beschreibung" in aktiv else None
        # Fehlt ein Zusatzmodell, diesen Schritt auslassen. Sonst würde jedes Bild bei jedem Lauf
        # erneut geladen, weil der Schritt nie als erledigt gilt.
        fehlend = {name for name, m in (("gesichter", gesichter), ("ocr", ocr), ("beschreibung", beschreiber))
                   if name in aktiv and m is None}
        if fehlend:
            for name in sorted(fehlend):
                schluessel = "beschreiber" if name == "beschreibung" else name
                self._fehler(f"{name} übersprungen, Modell nicht verfügbar: {self.m.fehler.get(schluessel)}")
            aktiv -= fehlend
            offen = self._offen(aktiv)
            self._setze(gesamt=len(offen))
            if not offen:
                return
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
                        self._gesichter_speichern(z["id"], gef, bw, bh,
                                                  getattr(gesichter, "kennung", modelle.GESICHT_KENNUNG))
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

    def _gesichter_speichern(self, bild_id: int, gef: list[dict], bw: int, bh: int, kennung: str) -> None:
        """Ersetzt die Gesichter eines Bildes. Was der Nutzer an derselben Stelle festgelegt hat
        (Person, "Nicht diese Person", "kein Gesicht"), wird auf das neue Gesicht übertragen."""
        alte = [dict(r) for r in self.db.abfrage(
            "SELECT g.id, g.x, g.y, g.w, g.h, g.person_id, g.person_fest, g.ignoriert, "
            "       (p.name IS NOT NULL OR p.fest=1) AS person_bleibt "
            "FROM gesichter g LEFT JOIN personen p ON p.id=g.person_id WHERE g.bild_id=?", (bild_id,))]
        abgelehnt: dict[int, list[int]] = {}
        if alte:
            for r in self.db.abfrage(f"SELECT gesicht_id, person_id FROM gesicht_abgelehnt WHERE gesicht_id IN "
                                     f"({','.join('?' * len(alte))})", [a["id"] for a in alte]):
                abgelehnt.setdefault(r["gesicht_id"], []).append(r["person_id"])
        boxen = [(g["x"] / bw, g["y"] / bh, g["w"] / bw, g["h"] / bh) for g in gef]
        vorgaenger = personen.uebernahme_finden(alte, boxen)
        with self.db.verbindung() as v:
            v.execute("DELETE FROM gesichter WHERE bild_id=?", (bild_id,))
            for g, box, alt in zip(gef, boxen, vorgaenger):
                person_id, fest, ignoriert = None, 0, 0
                if alt:
                    ignoriert = alt["ignoriert"] or 0
                    # nur Zuordnungen übernehmen, die der Nutzer gesehen hat; der Rest wird neu gruppiert
                    if alt["person_id"] and (alt["person_fest"] or alt["person_bleibt"]):
                        person_id, fest = alt["person_id"], alt["person_fest"] or 0
                gid = v.execute("INSERT INTO gesichter (bild_id, x, y, w, h, score, qualitaet, modell, vektor, "
                                "person_id, person_fest, ignoriert) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                                (bild_id, *box, g["score"], g.get("qualitaet", g["score"]), kennung,
                                 vektor_zu_blob(g["vektor"]), person_id, fest, ignoriert)).lastrowid
                if alt:
                    v.executemany("INSERT OR IGNORE INTO gesicht_abgelehnt VALUES (?,?)",
                                  [(gid, pid) for pid in abgelehnt.get(alt["id"], [])])
            v.execute("UPDATE bilder SET schritt_gesichter=1, anzahl_gesichter=? WHERE id=?",
                      (sum(1 for g, a in zip(gef, vorgaenger) if not (a and a["ignoriert"])), bild_id))
        caches_entfernen(gesicht_ids=[a["id"] for a in alte])

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
    # _ und % sind in LIKE Platzhalter, in Dateinamen (IMG_2023) aber normale Zeichen
    maskiert = [w.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") for w in woerter]
    return (" AND ".join("suchtext LIKE ? ESCAPE '\\'" for _ in woerter),
            [f"%{w}%" for w in maskiert])


__all__ = ["Indexer", "Modelle", "alle_neu_einordnen", "lade_themen", "vorschau_pfad", "np"]
