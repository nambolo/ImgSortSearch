"""Lokaler Webserver für die Oberfläche (nur auf 127.0.0.1 erreichbar)."""
from __future__ import annotations

import io
import logging
import shutil
import threading
import time
from datetime import datetime
from pathlib import Path

import numpy as np
from fastapi import Body, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from PIL import Image

from . import dubletten, modelle, orte, papierkorb, personen, sortieren, zwischenablage
from .datenbank import Datenbank
from .einstellungen import Einstellungen, daten_ordner
from .indexer import Indexer, Modelle, alle_neu_einordnen, bilder_entfernen, bilder_im_ordner, lade_themen, \
    quarantaene_ordner, text_suche_bedingung, vorschau_erzeugen, vorschau_pfad
from .klassifizierung import UNKLAR, haupt_kategorien, zusatz_kategorien

log = logging.getLogger(__name__)
STATIC = Path(__file__).with_name("static")
KAT = "COALESCE(kategorie_manuell, kategorie_auto, 'Unklar')"


class Zustand:
    def __init__(self):
        self.e = Einstellungen()
        self.db = Datenbank(daten_ordner() / "index.sqlite")
        self.m = Modelle(self.e)
        self.indexer = Indexer(self.db, self.e, self.m)
        self.indexer.nach_lauf.append(self._personen_zuordnen)
        self.m.download.nach_download.append(self._nach_download)
        # neues Gesichtsmodell: nur den Gesichtsschritt neu einplanen, der Rest des Index bleibt
        if personen.modellwechsel_pruefen(self.db):
            log.info("Gesichtsmodell gewechselt, Gesichter werden beim nächsten Indexlauf neu erkannt")
        self.fenster = None           # pywebview-Fenster, falls vorhanden
        self.sortier_status = {"laeuft": False, "fertig": 0, "gesamt": 0, "ergebnis": None}
        self.dubletten_stop = threading.Event()
        self.dubletten = dubletten.Suche(self.db, vorschau_pfad, self.dubletten_stop)
        self.dubletten_ergebnis = None
        self.kacheln = orte.Kacheln()
        self.indexer.nach_metadaten.append(self._orte_zuordnen)
        # ältere Indexe: Ortsnamen und Suchtext einmal nachtragen (geht schnell, es wird kein Bild geöffnet)
        threading.Thread(target=self._nachtragen, daemon=True).start()

    def _nachtragen(self):
        self._orte_zuordnen()
        try:
            self.db.suchtext_auffrischen()
        except Exception:
            log.exception("Suchtext auffrischen fehlgeschlagen")

    def _orte_zuordnen(self):
        try:
            orte.orte_zuordnen(self.db)
        except Exception:
            log.exception("Ortsnamen zuordnen fehlgeschlagen")

    def _nach_download(self, modell: str):
        """Modell für ausführliche Beschreibungen ist da: gleich loslegen, falls es noch gewählt ist."""
        a = self.e["beschreibung"]["ausfuehrlich"]
        if a["aktiv"] and a["modell"] == modell and self.e["quellen"] and self.indexer.offen_lang():
            self.indexer.starten()

    def ausfuehrlich_info(self) -> dict:
        a = self.e["beschreibung"]["ausfuehrlich"]
        sek = self.db.meta("lang_sek_pro_bild")
        return {"aktiv": a["aktiv"], "modell": a["modell"], "vorhanden": modelle.ausfuehrlich_vorhanden(a["modell"]),
                "offen": self.indexer.offen_lang(),
                "fertig": self.db.eins("SELECT COUNT(*) AS n FROM bilder WHERE beschreibung_lang IS NOT NULL")["n"],
                "sek_pro_bild": float(sek) if sek else None, "download": self.m.download.status}

    def _personen_zuordnen(self):
        self.indexer.status["meldung"] = "Gesichter werden Personen zugeordnet ..."
        personen.neu_gruppieren(self.db, self.e.werte)


def erstelle_app(z: Zustand | None = None) -> FastAPI:
    z = z or Zustand()
    app = FastAPI(title="ImgSortSearch")
    app.state.z = z

    @app.middleware("http")
    async def nur_lokal(request, call_next):
        # Schutz gegen fremde Webseiten im Browser: DNS-Rebinding (falscher Host) und
        # Cross-Site-Anfragen, die sonst z. B. ein Sortieren auslösen könnten.
        host = (request.headers.get("host") or "").rsplit(":", 1)[0].strip("[]").lower()
        if host not in ("127.0.0.1", "localhost", "::1", "testserver"):
            return JSONResponse({"detail": "Nur lokal erreichbar"}, status_code=403)
        if request.method not in ("GET", "HEAD", "OPTIONS") and request.headers.get("x-imgsortsearch") != "1":
            return JSONResponse({"detail": "Anfrage abgelehnt"}, status_code=403)
        return await call_next(request)

    def bild_oder_404(bild_id: int):
        r = z.db.eins(f"SELECT *, {KAT} AS kategorie FROM bilder WHERE id=?", (bild_id,))
        if not r:
            raise HTTPException(404, "Bild nicht gefunden")
        return r

    def zusatz_liste(r) -> list[str]:
        return [k for k in (r["zusatz"] or "").split(",") if k]

    def kurz(r) -> dict:
        return {"id": r["id"], "pfad": r["pfad"], "name": Path(r["pfad"]).name, "datum": r["datum"],
                "kategorie": r["kategorie"], "manuell": r["kategorie_manuell"] is not None,
                "zusatz": zusatz_liste(r),
                "sicherheit": r["sicherheit"], "tags": r["tags"] or "", "beschreibung": r["beschreibung"] or "",
                "kamera": r["kamera"], "gesichter": r["anzahl_gesichter"] or 0, "ort": r["ort"] or "",
                "gps": [r["gps_lat"], r["gps_lon"]] if r["gps_lat"] is not None else None}

    # ------------------------------------------------------------ Status / Einstellungen

    @app.get("/api/status")
    def status():
        zahlen = {r["k"]: r["n"] for r in z.db.abfrage(f"SELECT {KAT} AS k, COUNT(*) AS n FROM bilder GROUP BY k")}
        gesamt = sum(zahlen.values())
        for k in zusatz_kategorien(z.e.werte):  # Zusatzkategorien zählen extra, ein Bild kann in mehreren sein
            zahlen[k] = z.db.eins("SELECT COUNT(*) AS n FROM bilder WHERE instr(zusatz, ?) > 0", (f",{k},",))["n"]
        offen = z.db.eins("SELECT COUNT(*) AS n FROM bilder WHERE schritt_clip=0 AND fehler IS NULL")["n"]
        return {"index": z.indexer.status, "kategorien": zahlen, "gesamt": gesamt,
                "offen": offen, "modell_fehler": z.m.fehler, "geraete": z.m.geraete(), "exiftool": shutil.which("exiftool") is not None,
                "sortieren": z.sortier_status, "ausfuehrlich": z.ausfuehrlich_info(),
                "quellen_fehlen": [q for q in z.e["quellen"] if not Path(q).is_dir()]}

    @app.get("/api/einstellungen")
    def einstellungen_lesen():
        return z.e.werte

    @app.post("/api/einstellungen")
    def einstellungen_schreiben(neu: dict = Body(...)):
        alt_beschl = z.e.get("beschleunigung")
        alt_lang = dict(z.e["beschreibung"]["ausfuehrlich"])
        alt_ocr = z.e["ocr"]["modus"]
        werte = z.e.aktualisieren(neu)
        if alt_ocr == "text_bilder" and werte["ocr"]["modus"] == "alle":
            # Bisher wurde nur bei Bildern mit Text gelesen. Die übrigen sind schon "erledigt" und kämen
            # sonst nie dran: Texterkennung für Bilder ohne erkannten Text neu einplanen.
            with z.db.verbindung() as v:
                v.execute("UPDATE bilder SET schritt_ocr=0 WHERE schritt_ocr=1 AND (ocr_text IS NULL OR ocr_text='')")
        if "kategorien" in neu:
            # Wurde eine Kategorie zur Zusatzkategorie, werden von Hand gesetzte Zuordnungen mitgenommen
            zusatz = zusatz_kategorien(werte)
            for k in zusatz:
                ids = [r["id"] for r in z.db.abfrage("SELECT id FROM bilder WHERE kategorie_manuell=?", (k,))]
                if ids:
                    with z.db.verbindung() as v:
                        v.execute("UPDATE bilder SET kategorie_manuell=NULL WHERE kategorie_manuell=?", (k,))
                    z.db.zusatz_setzen(ids, k, True, zusatz)
        if werte.get("beschleunigung") != alt_beschl and not z.indexer.status["laeuft"]:
            z.m.entladen()  # beim nächsten Gebrauch auf dem neuen Gerät laden
        a = werte["beschreibung"]["ausfuehrlich"]
        if a["aktiv"] and (not alt_lang["aktiv"] or alt_lang["modell"] != a["modell"]):
            # erst jetzt (gewählt und noch nicht da) herunterladen; ist es schon da, gleich beschreiben
            if not z.m.download.starten(a["modell"]) and modelle.ausfuehrlich_vorhanden(a["modell"]):
                z._nach_download(a["modell"])
        if any(k in neu for k in ("kategorien", "arbeitszeit", "inhalt", "schwelle", "regeln", "themen")):
            threading.Thread(target=_einordnen, daemon=True).start()
        return werte

    @app.get("/api/quellen/anzahl")
    def quelle_anzahl(pfad: str):
        """Wie viele Bilder im Index aus diesem Bilder-Ordner stammen (für die Abfrage beim Entfernen)."""
        andere = [q for q in z.e["quellen"] if q != pfad]
        return {"anzahl": len(bilder_im_ordner(z.db, pfad, andere))}

    @app.post("/api/quellen/entfernen")
    def quelle_entfernen(daten: dict = Body(...)):
        """Bilder-Ordner austragen; mit ``aus_index`` auch seine Bilder aus dem Index (Dateien bleiben)."""
        pfad = daten.get("pfad") or ""
        aus_index = bool(daten.get("aus_index"))
        if aus_index and z.indexer.status["laeuft"]:
            raise HTTPException(409, "Der Index läuft gerade. Bitte erst anhalten.")
        andere = [q for q in z.e["quellen"] if q != pfad]
        werte = z.e.aktualisieren({"quellen": andere})
        entfernt = bilder_entfernen(z.db, bilder_im_ordner(z.db, pfad, andere)) if aus_index and pfad else 0
        return {"einstellungen": werte, "entfernt": entfernt}

    def _einordnen():
        try:
            if lade_themen(z.db, z.e, z.m, laden=False) is None and z.db.eins(
                    "SELECT 1 FROM bilder WHERE clip IS NOT NULL LIMIT 1"):
                lade_themen(z.db, z.e, z.m, laden=True)  # neue Themen brauchen das Modell
            alle_neu_einordnen(z.db, z.e, z.m)
        except Exception:
            log.exception("Neu einordnen fehlgeschlagen")

    @app.post("/api/ordner-waehlen")
    def ordner_waehlen():
        if z.fenster is not None:
            import webview
            art = getattr(getattr(webview, "FileDialog", None), "FOLDER", None)
            erg = z.fenster.create_file_dialog(art if art is not None else webview.FOLDER_DIALOG)
            if isinstance(erg, str):  # je nach pywebview-Version Text oder Liste
                return {"pfad": erg or None}
            return {"pfad": erg[0] if erg else None}
        try:  # Fallback ohne eigenes Fenster
            import tkinter
            from tkinter import filedialog
            root = tkinter.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            pfad = filedialog.askdirectory()
            root.destroy()
            return {"pfad": pfad or None}
        except Exception:
            return {"pfad": None}

    # ------------------------------------------------------------ Index

    @app.post("/api/index/start")
    def index_start():
        if not z.e["quellen"]:
            raise HTTPException(400, "Bitte zuerst einen Bilder-Ordner hinzufügen.")
        return {"gestartet": z.indexer.starten()}

    @app.post("/api/index/stop")
    def index_stop():
        z.indexer.stoppen()
        return {"ok": True}

    @app.post("/api/einordnen")
    def einordnen_jetzt():
        _einordnen()
        return {"ok": True}

    # ------------------------------------------------------------ Bilder & Suche

    @app.get("/api/filter")
    def filter_werte():
        jahre = [r["j"] for r in z.db.abfrage(
            "SELECT DISTINCT substr(datum,1,4) AS j FROM bilder WHERE datum IS NOT NULL ORDER BY j DESC")]
        kameras = [r["kamera"] for r in z.db.abfrage(
            "SELECT kamera, COUNT(*) n FROM bilder GROUP BY kamera ORDER BY n DESC LIMIT 50")]
        # Themen, die bei mindestens einem Bild unter den erkannten Themen sind, mit Anzahl
        zaehler: dict[str, int] = {}
        for r in z.db.abfrage("SELECT tags FROM bilder WHERE tags IS NOT NULL AND tags != ''"):
            for t in r["tags"].split(","):
                t = t.strip()
                if t:
                    zaehler[t] = zaehler.get(t, 0) + 1
        reihenfolge = [t["name"] for t in z.e["themen"]]
        themen = sorted(zaehler, key=lambda t: (reihenfolge.index(t) if t in reihenfolge else len(reihenfolge), t))
        laender = [{"name": r["ort_land"], "anzahl": r["n"]} for r in z.db.abfrage(
            "SELECT ort_land, COUNT(*) n FROM bilder WHERE ort_land IS NOT NULL AND ort_land != '' "
            "GROUP BY ort_land ORDER BY n DESC")]
        orte_liste = [{"name": r["ort_stadt"], "land": r["ort_land"], "anzahl": r["n"]} for r in z.db.abfrage(
            "SELECT ort_stadt, ort_land, COUNT(*) n FROM bilder WHERE ort_stadt IS NOT NULL AND ort_stadt != '' "
            "GROUP BY ort_stadt, ort_land ORDER BY n DESC LIMIT 300")]
        return {"jahre": jahre, "kameras": kameras, "laender": laender, "orte": orte_liste,
                "themen": [{"name": t, "anzahl": zaehler[t]} for t in themen],
                "kategorien": haupt_kategorien(z.e.werte) + [UNKLAR] + zusatz_kategorien(z.e.werte)}

    def bedingungen(kategorie="", jahr="", kamera="", person=0, thema="", nur_manuell=False, ort="", bereich=""):
        bed, par = ["1=1"], []
        if kategorie and kategorie in zusatz_kategorien(z.e.werte):
            bed.append("instr(zusatz, ?) > 0")
            par.append(f",{kategorie},")
        elif kategorie:
            bed.append(f"{KAT}=?")
            par.append(kategorie)
        if thema:  # eines der erkannten Themen des Bildes (Spalte tags: "Whiteboard, Technik")
            bed.append("instr(', ' || tags || ',', ?) > 0")
            par.append(f", {thema},")
        if jahr:
            bed.append("substr(datum,1,4)=?")
            par.append(jahr)
        if kamera:
            bed.append("kamera=?")
            par.append(kamera)
        if person:
            bed.append("id IN (SELECT bild_id FROM gesichter WHERE person_id=?)")
            par.append(person)
        if nur_manuell:
            bed.append("kategorie_manuell IS NOT NULL")
        for wert, fn in ((ort, orte.ort_bedingung), (bereich, orte.bereich_bedingung)):
            if wert:
                b, p = fn(wert)
                bed.append(b)
                par += p
        return " AND ".join(bed), par

    @app.get("/api/bilder")
    def bilder(q: str = "", kategorie: str = "", jahr: str = "", kamera: str = "", person: int = 0, thema: str = "",
               aehnlich_zu: int = 0, nur_manuell: bool = False, sortierung: str = "relevanz",
               offset: int = 0, limit: int = 200, art: str = "beides", ort: str = "", bereich: str = ""):
        wo, par = bedingungen(kategorie, jahr, kamera, person, thema, nur_manuell, ort, bereich)

        rang: dict[int, float] | None = None
        if aehnlich_zu:
            r = z.db.eins("SELECT clip FROM bilder WHERE id=?", (aehnlich_zu,))
            if not r or r["clip"] is None:
                raise HTTPException(400, "Für dieses Bild gibt es noch keinen Bildvektor.")
            from .datenbank import blob_zu_vektor
            rang = _vektor_rang(blob_zu_vektor(r["clip"]), schwelle=0.0, max_n=1000)
        elif q.strip():
            rang = {}
            # Namen erkannter Personen: direkt über die Gesichter, egal welche Suchart eingestellt ist
            # und auch dann, wenn der Suchtext eines Bildes (noch) nicht aktuell ist
            pids, rest, exakt = personen.namen_treffer(z.db, q)
            if pids:
                bilder_p = personen.bilder_mit_personen(z.db, pids)
                if rest and bilder_p:
                    tb, tp = text_suche_bedingung(rest)
                    teil = set()
                    for i in range(0, len(bilder_p), 900):
                        stueck = bilder_p[i:i + 900]
                        teil |= {r["id"] for r in z.db.abfrage(
                            f"SELECT id FROM bilder WHERE {tb} AND id IN ({','.join('?' * len(stueck))})", (*tp, *stueck))}
                    # "Anna Strand": auch Bilder der Person, die nur dem Inhalt nach zum Rest passen
                    if art in ("beides", "bedeutung"):
                        clip = z.m.clip()
                        if clip is not None:
                            sem_p = _vektor_rang(clip.texte([rest])[0], schwelle=0.17, max_n=600, nur=bilder_p)
                            if sem_p:
                                grenze = max(sem_p.values()) - 0.05
                                for i, s in sem_p.items():
                                    if s >= grenze and i not in teil:
                                        rang[i] = 2.5 + s  # hinter den Textbefunden der Person
                    bilder_p = list(teil)
                for i in bilder_p:
                    rang[i] = 3.0
            # Nur ein Name gesucht: genau die Bilder dieser Person. Die Bedeutungssuche kennt keine Namen
            # und würde sonst zufällige Bilder beimischen, die Textsuche Teilwörter ("Anna" in "Johanna").
            # Name + weiterer Begriff ("Anna Strand"): ebenso nur Bilder dieser Person, die dazu passen
            nur_name = bool(pids) and (exakt or (bool(rest) and _name_teil_exakt(q, rest)))
            if art in ("beides", "text") and not nur_name:
                tb, tp = text_suche_bedingung(q)
                for r in z.db.abfrage(f"SELECT id FROM bilder WHERE {tb}", tp):
                    rang[r["id"]] = max(rang.get(r["id"], 0), 2.0)  # Textbefunde zuerst, Personentreffer davor
            if art in ("beides", "bedeutung") and not nur_name:
                clip = z.m.clip()
                if clip is not None:
                    qv = clip.texte([q.strip()])[0]
                    sem = _vektor_rang(qv, schwelle=0.17, max_n=600)
                    if sem:
                        # nur Treffer nahe am besten: sonst kommt bei seltenen Begriffen viel Rauschen dazu
                        grenze = max(sem.values()) - (0.08 if art == "bedeutung" else 0.05)
                        sem = {i: s for i, s in sem.items() if s >= grenze}
                        for i, s in sem.items():
                            rang[i] = max(rang.get(i, 0), s)
                    for i, s in _beschreibung_rang(qv, sem).items():
                        rang[i] = max(rang.get(i, 0), s)
        if rang is not None:
            if not rang:
                return {"gesamt": 0, "bilder": []}
            ids = list(rang)
            treffer = []
            for i in range(0, len(ids), 900):
                teil = ids[i:i + 900]
                treffer += z.db.abfrage(f"SELECT *, {KAT} AS kategorie FROM bilder WHERE {wo} AND id IN "
                                        f"({','.join('?' * len(teil))})", (*par, *teil))
            # Standard: beste Treffer zuerst, bei gleichem Rang die neuesten. Eine andere gewählte
            # Sortierung gilt auch für die Treffer (früher wurde sie bei einer Suche still ignoriert).
            _sortieren(treffer, rang, sortierung)
            return {"gesamt": len(treffer), "bilder": [kurz(r) for r in treffer[offset:offset + limit]]}

        # immer eindeutig (id als letzter Schlüssel), sonst kann "Weitere laden" Bilder doppelt bringen
        ordnung = {"datum_ab": "datum DESC, id DESC", "datum_auf": "datum IS NULL, datum ASC, id ASC",
                   "name": "pfad COLLATE NOCASE ASC, id ASC",
                   "unsicher": "kategorie_manuell IS NOT NULL, sicherheit ASC, datum DESC, id DESC"
                   }.get(sortierung, "datum DESC, id DESC")
        gesamt = z.db.eins(f"SELECT COUNT(*) n FROM bilder WHERE {wo}", par)["n"]
        zeilen = z.db.abfrage(f"SELECT *, {KAT} AS kategorie FROM bilder WHERE {wo} ORDER BY {ordnung} "
                              f"LIMIT ? OFFSET ?", (*par, limit, offset))
        return {"gesamt": gesamt, "bilder": [kurz(r) for r in zeilen]}

    def _sortieren(treffer: list, rang: dict[int, float], sortierung: str) -> None:
        """Suchtreffer sortieren. Standard ("relevanz"): beste Treffer zuerst, bei gleichem Rang die neuesten.
        Eine andere gewählte Sortierung gilt auch für Suchtreffer (früher wurde sie bei einer Suche ignoriert)."""
        neueste = lambda r: (r["datum"] is not None, r["datum"] or "", r["id"])  # noqa: E731 (ohne Datum zuletzt)
        if sortierung == "datum_ab":
            treffer.sort(key=neueste, reverse=True)
        elif sortierung == "datum_auf":
            treffer.sort(key=lambda r: (r["datum"] is None, r["datum"] or "", r["id"]))
        elif sortierung == "name":
            treffer.sort(key=lambda r: (r["pfad"].lower(), r["id"]))
        elif sortierung == "unsicher":
            treffer.sort(key=neueste, reverse=True)
            treffer.sort(key=lambda r: (r["kategorie_manuell"] is not None, r["sicherheit"] or 0))
        else:
            treffer.sort(key=neueste, reverse=True)
            treffer.sort(key=lambda r: -rang[r["id"]])  # stabil: bei gleichem Rang bleiben die neuesten vorn

    def _name_teil_exakt(q: str, rest: str) -> bool:
        """Sind die Wörter der Anfrage, die zu einem Namen gehören, ganze Namen (nicht nur Wortanfänge)?"""
        from .datenbank import normalisiere
        uebrig = rest.split()
        name_woerter = []
        for w in normalisiere(q).split():
            if w in uebrig:
                uebrig.remove(w)
            else:
                name_woerter.append(w)
        return bool(name_woerter) and personen.namen_treffer(z.db, " ".join(name_woerter))[2]

    def _beschreibung_rang(v: np.ndarray, sem: dict[int, float]) -> dict[int, float]:
        """Bedeutungssuche in den ausführlichen Beschreibungen (Satz für Satz).
        Text-zu-Text-Ähnlichkeiten liegen höher als Text-zu-Bild, deshalb keine feste Schwelle:
        ein Satz zählt, wenn er deutlich über dem Durchschnitt aller Sätze liegt (in Standardabweichungen)
        und nahe am besten Satz. Treffer werden hinter den passenden Bildern einsortiert."""
        ids, mat = z.db.beschreibung_matrix()
        if len(ids) < 20 or mat.shape[1] != v.shape[0]:
            return {}
        norm = np.linalg.norm(mat, axis=1) + 1e-9
        sims = (mat @ (v / (np.linalg.norm(v) + 1e-9))) / norm
        strenge = float(z.e["beschreibung"]["ausfuehrlich"].get("such_strenge", 3.0))
        grenze = max(float(sims.mean() + strenge * sims.std()), float(sims.max()) - 0.06)
        bester: dict[int, float] = {}
        for i, s in zip(ids.tolist(), sims.tolist()):
            if s >= grenze and s > bester.get(i, -1):
                bester[i] = s
        basis = min(sem.values()) if sem else 0.2  # knapp unter dem schwächsten Bildtreffer
        top = max(bester.values(), default=0)
        return {i: basis - 0.001 - (top - s) for i, s in sorted(bester.items(), key=lambda x: -x[1])[:300]}

    def _vektor_rang(v: np.ndarray, schwelle: float, max_n: int, nur=None) -> dict[int, float]:
        ids, mat = z.db.clip_matrix()
        if not len(ids) or mat.shape[1] != v.shape[0]:
            return {}
        if nur is not None:  # nur unter diesen Bildern suchen
            maske = np.isin(ids, np.fromiter(nur, dtype=np.int64))
            ids, mat = ids[maske], mat[maske]
            if not len(ids):
                return {}
        sims = mat @ (v / (np.linalg.norm(v) + 1e-9))
        ordnung = np.argsort(-sims)[:max_n]
        return {int(ids[i]): float(sims[i]) for i in ordnung if sims[i] >= schwelle}

    @app.post("/api/suche-mit-bild")
    async def suche_mit_bild(datei: UploadFile = File(...), kategorie: str = "", jahr: str = "", kamera: str = "",
                             thema: str = "", ort: str = "", bereich: str = ""):
        img = _upload_bild(await datei.read())
        clip = z.m.clip()
        if clip is None:
            raise HTTPException(503, "Bildmodell nicht verfügbar")
        wo, par = bedingungen(kategorie, jahr, kamera, 0, thema, False, ort, bereich)
        # mit Filtern weiter suchen, sonst blieben von den 300 ähnlichsten kaum welche übrig
        rang = _vektor_rang(clip.bilder([img])[0], schwelle=0.0, max_n=300 if wo == "1=1" else 3000)
        zeilen = _zeilen_mit_filter(rang, wo, par)[:300]
        return {"gesamt": len(zeilen), "bilder": [kurz(r) for r in zeilen]}

    @app.get("/api/bild/{bild_id}")
    def bild_details(bild_id: int):
        r = bild_oder_404(bild_id)
        d = {k: r[k] for k in r.keys() if k not in ("clip", "suchtext", "beschreibung_lang_vek")}
        d["name"] = Path(r["pfad"]).name
        d["zusatz"] = zusatz_liste(r)
        d["zusatz_hand"] = {h["kategorie"]: bool(h["an"]) for h in z.db.abfrage(
            "SELECT kategorie, an FROM bild_zusatz WHERE bild_id=?", (bild_id,))}
        d["gesichter"] = [dict(g) for g in z.db.abfrage(
            "SELECT g.id, g.x, g.y, g.w, g.h, g.person_id, p.name FROM gesichter g "
            "LEFT JOIN personen p ON p.id=g.person_id WHERE g.bild_id=? AND g.ignoriert=0", (bild_id,))]
        return d

    def _bild_antwort(p: Path, request: Request):
        """Zwischengespeichertes Bild mit ETag; der Browser fragt jedes Mal kurz nach (no-cache)."""
        st = p.stat()
        etag = f'"{st.st_mtime_ns:x}-{st.st_size:x}"'
        kopf = {"Cache-Control": "no-cache", "ETag": etag}
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers=kopf)
        return FileResponse(p, media_type="image/jpeg", headers=kopf)

    @app.get("/api/vorschau/{bild_id}")
    def vorschau(bild_id: int, request: Request):
        p = vorschau_pfad(bild_id)
        if not p.exists():
            r = bild_oder_404(bild_id)
            try:
                vorschau_erzeugen(bild_id, modelle.bild_laden(Path(r["pfad"]), max_kante=800))
            except Exception:
                raise HTTPException(404, "Keine Vorschau möglich")
        # nicht blind zwischenspeichern: nach "Index zurücksetzen" oder Entfernen von Bildern vergibt die
        # Datenbank die Nummern neu, der Browser zeigte sonst einen Tag lang die alten Vorschaubilder.
        # "no-cache" heißt: kurz nachfragen (ETag), unverändert kommt nur "304" zurück.
        return _bild_antwort(p, request)

    @app.get("/api/ansicht/{bild_id}")
    def ansicht(bild_id: int, kante: int = 1600):
        r = bild_oder_404(bild_id)
        try:
            img = modelle.bild_laden(Path(r["pfad"]), max_kante=max(200, min(kante, 4000)))
        except Exception as ex:
            raise HTTPException(404, str(ex))
        puffer = io.BytesIO()
        img.save(puffer, "JPEG", quality=88)
        return Response(puffer.getvalue(), media_type="image/jpeg")

    @app.get("/api/gesicht/{gesicht_id}")
    def gesicht_bild(gesicht_id: int, request: Request):
        g = z.db.eins("SELECT g.*, b.pfad FROM gesichter g JOIN bilder b ON b.id=g.bild_id WHERE g.id=?",
                      (gesicht_id,))
        if not g:
            raise HTTPException(404)
        cache = daten_ordner() / "gesichter" / f"{gesicht_id}.jpg"
        if not cache.exists():
            cache.parent.mkdir(parents=True, exist_ok=True)
            img = modelle.bild_laden(Path(g["pfad"]), max_kante=2000)
            w, h = img.size
            rand = 0.25
            x0, y0 = (g["x"] - g["w"] * rand) * w, (g["y"] - g["h"] * rand) * h
            x1, y1 = (g["x"] + g["w"] * (1 + rand)) * w, (g["y"] + g["h"] * (1 + rand)) * h
            ausschnitt = img.crop((max(0, int(x0)), max(0, int(y0)), min(w, int(x1)), min(h, int(y1))))
            ausschnitt.thumbnail((200, 200))
            ausschnitt.save(cache, "JPEG", quality=85)
        return _bild_antwort(cache, request)

    @app.post("/api/bilder/zusatz")
    def zusatz_setzen(daten: dict = Body(...)):
        """Zusatzkategorie für mehrere Bilder: an=true hinzufügen, false entfernen, null wieder automatisch."""
        ids = [int(i) for i in daten.get("ids", [])]
        kat = daten.get("kategorie") or ""
        gueltig = zusatz_kategorien(z.e.werte)
        if kat not in gueltig:
            raise HTTPException(400, f"„{kat}“ ist keine Zusatzkategorie")
        an = daten.get("an")
        z.db.zusatz_setzen(ids, kat, None if an is None else bool(an), gueltig)
        return {"geaendert": len(ids)}

    @app.post("/api/bilder/neu-analysieren")
    def neu_analysieren(daten: dict = Body(...)):
        """Markierte Bilder komplett neu analysieren (Bildinhalt, Gesichter, Text, Beschreibung).
        Von Hand gesetzte Kategorien bleiben erhalten."""
        ids = [int(i) for i in daten.get("ids", [])]
        if not ids:
            raise HTTPException(400, "Keine Bilder ausgewählt")
        with z.db.verbindung() as v:
            v.executemany("UPDATE bilder SET schritt_clip=0, schritt_ocr=0, schritt_gesichter=0, "
                          "schritt_beschreibung=0, schritt_lang=0, fehler=NULL WHERE id=?", [(i,) for i in ids])
        gestartet = z.indexer.starten()
        return {"vorgemerkt": len(ids), "gestartet": gestartet}

    @app.post("/api/ausfuehrlich/download")
    def ausfuehrlich_download():
        a = z.e["beschreibung"]["ausfuehrlich"]
        return {"gestartet": z.m.download.starten(a["modell"]), "ausfuehrlich": z.ausfuehrlich_info()}

    @app.post("/api/ausfuehrlich/abbrechen")
    def ausfuehrlich_abbrechen():
        z.m.download.abbrechen()
        return {"ok": True}

    @app.post("/api/bilder/neu-beschreiben")
    def neu_beschreiben(daten: dict = Body(...)):
        """Nur die ausführliche Beschreibung neu erzeugen, für markierte Bilder oder alle (alle=true).
        Bildinhalt, Gesichter, Text und Kategorien bleiben, wie sie sind."""
        if not z.e["beschreibung"]["ausfuehrlich"]["aktiv"]:
            raise HTTPException(400, "Ausführliche Beschreibungen sind in den Einstellungen ausgeschaltet.")
        with z.db.verbindung() as v:
            if daten.get("alle"):
                n = v.execute("UPDATE bilder SET schritt_lang=0").rowcount
            else:
                ids = [int(i) for i in daten.get("ids", [])]
                if not ids:
                    raise HTTPException(400, "Keine Bilder ausgewählt")
                v.executemany("UPDATE bilder SET schritt_lang=0 WHERE id=?", [(i,) for i in ids])
                n = len(ids)
        gestartet = z.indexer.starten() if z.e["quellen"] else False
        return {"vorgemerkt": n, "gestartet": gestartet}

    @app.post("/api/bilder/kategorie")
    def kategorie_setzen(daten: dict = Body(...)):
        ids = [int(i) for i in daten.get("ids", [])]
        kat = daten.get("kategorie") or None  # None = wieder automatisch
        if kat in zusatz_kategorien(z.e.werte):  # Zusatzkategorie: hinzufügen statt ersetzen
            z.db.zusatz_setzen(ids, kat, True, zusatz_kategorien(z.e.werte))
            return {"geaendert": len(ids)}
        with z.db.verbindung() as v:
            v.executemany("UPDATE bilder SET kategorie_manuell=? WHERE id=?", [(kat, i) for i in ids])
        return {"geaendert": len(ids)}

    @app.post("/api/bilder/kopieren")
    def in_zwischenablage(daten: dict = Body(...)):
        ids = [int(i) for i in daten.get("ids", [])][:5000]
        pfade = [r["pfad"] for r in z.db.abfrage(
            f"SELECT pfad FROM bilder WHERE id IN ({','.join('?' * len(ids))})", ids)] if ids else []
        try:
            zwischenablage.dateien_kopieren(pfade)
        except Exception as ex:
            raise HTTPException(400, str(ex))
        return {"kopiert": len(pfade)}

    @app.post("/api/bilder/{bild_id}/explorer")
    def explorer(bild_id: int):
        try:
            zwischenablage.im_explorer_zeigen(bild_oder_404(bild_id)["pfad"])
        except (OSError, RuntimeError) as ex:
            raise HTTPException(400, str(ex))
        return {"ok": True}

    # ------------------------------------------------------------ Karte

    @app.get("/api/karte/punkte")
    def karte_punkte(kategorie: str = "", jahr: str = "", kamera: str = "", person: int = 0, thema: str = ""):
        """Alle Bilder mit GPS-Daten, kompakt: [id, Breite, Länge, Ortsnummer] und die Liste der Ortsnamen."""
        wo, par = bedingungen(kategorie, jahr, kamera, person, thema)
        namen: dict[str, int] = {}
        punkte = []
        for r in z.db.abfrage(f"SELECT id, gps_lat, gps_lon, ort FROM bilder WHERE {wo} AND gps_lat IS NOT NULL "
                              "AND gps_lon IS NOT NULL ORDER BY datum DESC", par):
            n = namen.setdefault(r["ort"] or "", len(namen))
            punkte.append([r["id"], round(r["gps_lat"], 6), round(r["gps_lon"], 6), n])
        ohne = z.db.eins(f"SELECT COUNT(*) n FROM bilder WHERE {wo} AND gps_lat IS NULL", par)["n"]
        return {"punkte": punkte, "orte": list(namen), "ohne_gps": ohne}

    @app.get("/api/karte/info")
    def karte_info():
        anzahl, groesse = z.kacheln.groesse()
        k = z.e["karte"]
        return {"online": bool(k.get("online")), "detail": bool(k.get("detail", True)),
                "kacheln": anzahl, "bytes": groesse,
                "offline": time.time() < z.kacheln.offline_bis, "fehler": z.kacheln.letzter_fehler}

    @app.get("/api/karte/kachel/{kz}/{kx}/{ky}.png")
    def karte_kachel(kz: int, kx: int, ky: int):
        k = z.e["karte"]
        daten = z.kacheln.kachel(kz, kx, ky, online=bool(k.get("online")),
                                 url=k.get("kachel_url") or orte.KACHEL_URL)
        if daten is None:
            return Response(status_code=404)
        art = "image/jpeg" if daten.startswith(b"\xff\xd8") else "image/png"
        return Response(daten, media_type=art, headers={"Cache-Control": "max-age=86400"})

    @app.post("/api/karte/leeren")
    def karte_leeren():
        z.kacheln.leeren()
        return {"ok": True}

    # ------------------------------------------------------------ Themen

    @app.get("/api/themen")
    def themen_uebersicht():
        themen = lade_themen(z.db, z.e, z.m, laden=False)
        ids, mat = z.db.clip_matrix()
        zaehler, beispiele = {}, {}
        if themen is not None and len(ids):
            p = themen.wahrscheinlichkeiten(mat)
            top = np.argmax(p, axis=1)
            for j, t in enumerate(themen.themen):
                treffer = np.where(top == j)[0]
                zaehler[t["name"]] = int(len(treffer))
                beste = treffer[np.argsort(-p[treffer, j])][:6]
                beispiele[t["name"]] = [int(ids[i]) for i in beste]
        return {"themen": [dict(t, anzahl=zaehler.get(t["name"], 0), beispiele=beispiele.get(t["name"], []))
                           for t in z.e["themen"]],
                "kategorien": z.e["kategorien"]}

    # ------------------------------------------------------------ Personen

    @app.get("/api/personen")
    def personen_liste():
        offen = z.db.eins("SELECT COUNT(*) AS n FROM bilder WHERE schritt_gesichter=0 AND fehler IS NULL")["n"]
        return {"personen": personen.personen_liste(z.db), "gesichter_offen": offen if z.e["gesichter"]["aktiv"] else 0}

    @app.post("/api/personen/neu-gruppieren")
    def personen_gruppieren():
        return personen.neu_gruppieren(z.db, z.e.werte)

    @app.post("/api/personen/{person_id}/name")
    def person_benennen(person_id: int, daten: dict = Body(...)):
        return {"id": personen.umbenennen(z.db, person_id, daten.get("name", ""))}

    @app.get("/api/personen/vorschlaege")
    def personen_vorschlaege():
        return personen.vorschlaege(z.db, z.e.werte)

    @app.post("/api/personen/vorschlag")
    def personen_vorschlag_anwenden(daten: dict = Body(...)):
        ziel = personen.vorschlag_anwenden(z.db, int(daten["ziel"]), [int(i) for i in daten.get("ja", [])],
                                           [int(i) for i in daten.get("nein", [])])
        return {"ziel": ziel}

    @app.post("/api/personen/verschieden")
    def personen_verschieden(daten: dict = Body(...)):
        personen.verschieden_merken(z.db, int(daten["a"]), int(daten["b"]))
        return {"ok": True}

    @app.post("/api/personen/zusammenfuehren")
    def person_zusammen(daten: dict = Body(...)):
        ziel = personen.zusammenfuehren(z.db, int(daten["ziel"]), [int(i) for i in daten["quellen"]])
        return {"ok": True, "ziel": ziel}

    @app.get("/api/personen/{person_id}/gesichter")
    def person_gesichter(person_id: int):
        return {"gesichter": personen.person_gesichter(z.db, person_id)}

    @app.get("/api/personen/{person_id}/typisch")
    def person_typisch(person_id: int, anzahl: int = 8):
        """Typische Gesichter einer Person (zum Vergleichen)."""
        return {"gesichter": personen._vertreter(z.db, person_id, max(1, min(anzahl, 24)))}

    def _gesicht_ids(daten: dict) -> list[int]:
        ids = [int(i) for i in daten.get("gesichter", [])]
        if not ids:
            raise HTTPException(400, "Keine Gesichter ausgewählt")
        return ids

    @app.post("/api/gesichter/nicht-diese-person")
    def gesichter_ablehnen(daten: dict = Body(...)):
        personen.nicht_diese_person(z.db, _gesicht_ids(daten))
        return {"ok": True}

    @app.post("/api/gesichter/kein-gesicht")
    def gesichter_ignorieren(daten: dict = Body(...)):
        personen.kein_gesicht(z.db, _gesicht_ids(daten))
        return {"ok": True}

    @app.post("/api/gesichter/zuweisen")
    def gesichter_zuweisen(daten: dict = Body(...)):
        pid = personen.zuweisen(z.db, _gesicht_ids(daten), int(daten.get("person") or 0) or None,
                                daten.get("name", ""))
        return {"person": pid}

    @app.post("/api/gesicht/{gesicht_id}/entfernen")
    def gesicht_entfernen(gesicht_id: int):
        personen.nicht_diese_person(z.db, [gesicht_id])
        return {"ok": True}

    @app.post("/api/personensuche")
    async def personensuche(datei: UploadFile = File(...), gesicht: int = 0, kategorie: str = "", jahr: str = "",
                            kamera: str = "", thema: str = "", ort: str = "", bereich: str = ""):
        img = _upload_bild(await datei.read())
        modell = z.m.gesichter()
        if modell is None:
            raise HTTPException(503, f"Gesichtserkennung nicht verfügbar: {z.m.fehler.get('gesichter')}")
        gef = modell.finde(img)
        if not gef:
            return {"gesichter": [], "bilder": [], "gesamt": 0}
        gef.sort(key=lambda g: -g["w"] * g["h"])
        wahl = gef[min(gesicht, len(gef) - 1)]
        rang = dict(personen.aehnliche_gesichter(z.db, wahl["vektor"], personen.schwellen(z.e.werte)["suche"]))
        wo, par = bedingungen(kategorie, jahr, kamera, 0, thema, False, ort, bereich)
        zeilen = _zeilen_mit_filter(rang, wo, par)
        w, h = img.size
        return {"gesichter": [{"x": g["x"] / w, "y": g["y"] / h, "w": g["w"] / w, "h": g["h"] / h} for g in gef],
                "gewaehlt": min(gesicht, len(gef) - 1), "gesamt": len(zeilen), "bilder": [kurz(r) for r in zeilen]}

    def _zeilen_mit_filter(rang: dict, wo: str, par: list) -> list:
        """Bilder aus rang (id -> Ähnlichkeit), die die Filter erfüllen, die ähnlichsten zuerst."""
        ids, zeilen = list(rang), []
        for i in range(0, len(ids), 900):
            teil = ids[i:i + 900]
            zeilen += z.db.abfrage(f"SELECT *, {KAT} AS kategorie FROM bilder WHERE {wo} AND id IN "
                                   f"({','.join('?' * len(teil))})", (*par, *teil))
        return sorted(zeilen, key=lambda r: -rang[r["id"]])

    def _upload_bild(daten: bytes) -> Image.Image:
        from PIL import ImageOps
        try:
            img = Image.open(io.BytesIO(daten))
            img = ImageOps.exif_transpose(img).convert("RGB")
            img.thumbnail((2000, 2000))
            return img
        except Exception:
            raise HTTPException(400, "Bild konnte nicht gelesen werden")

    # ------------------------------------------------------------ Sortieren

    def _auftraege(nur: list[str] | None) -> list[sortieren.Auftrag]:
        zeilen = z.db.abfrage(f"SELECT id, pfad, datum, kamera, tags, beschreibung, zusatz, {KAT} AS kategorie "
                              "FROM bilder")
        namen = {}
        for r in z.db.abfrage("SELECT g.bild_id, p.name FROM gesichter g JOIN personen p ON p.id=g.person_id "
                              "WHERE p.name IS NOT NULL"):
            namen.setdefault(r["bild_id"], []).append(r["name"])
        erg = []
        for r in zeilen:
            zusatz = zusatz_liste(r)
            if nur and r["kategorie"] not in nur and not set(zusatz) & set(nur):
                continue
            erg.append(sortieren.Auftrag(
                pfad=Path(r["pfad"]), kategorie=r["kategorie"],
                datum=datetime.fromisoformat(r["datum"]) if r["datum"] else None, kamera=r["kamera"] or "",
                tags=[t.strip() for t in (r["tags"] or "").split(",") if t.strip()],
                personen=sorted(set(namen.get(r["id"], []))), beschreibung=r["beschreibung"] or "",
                bild_id=r["id"], zusatz=zusatz))
        return erg

    @app.post("/api/sortieren/vorschau")
    def sortieren_vorschau(daten: dict = Body(...)):
        s = dict(z.e["sortieren"], **daten.get("einstellungen", {}))
        try:
            erg = sortieren.ausfuehren(_auftraege(daten.get("nur")), s, daten_ordner() / "protokolle", echt=False)
        except ValueError as ex:
            raise HTTPException(400, str(ex))
        return erg

    @app.post("/api/sortieren/ausfuehren")
    def sortieren_start(daten: dict = Body(...)):
        if z.sortier_status["laeuft"]:
            raise HTTPException(409, "Läuft bereits")
        s = dict(z.e["sortieren"], **daten.get("einstellungen", {}))
        z.e.aktualisieren({"sortieren": s})
        auftraege = _auftraege(daten.get("nur"))
        # schon hier setzen: sonst sieht die Oberfläche kurz "läuft nicht" und zeigt ein altes Ergebnis
        z.sortier_status.update(laeuft=True, fertig=0, gesamt=len(auftraege), ergebnis=None)

        def lauf():
            st = z.sortier_status
            try:
                erg = sortieren.ausfuehren(auftraege, s, daten_ordner() / "protokolle", echt=True,
                                           fortschritt=lambda n, g: st.update(fertig=n, gesamt=g))
                _pfade_aktualisieren(erg["pfad_aenderungen"])
                if erg["protokoll"]:
                    with z.db.verbindung() as v:
                        v.execute("INSERT INTO protokolle (datei, zeit, beschreibung) VALUES (?,?,?)",
                                  (erg["protokoll"], datetime.now().isoformat(timespec="seconds"),
                                   f"{s['modus']}, {erg['zaehler']['ok']} Dateien"
                                   + (", umbenannt" if s.get("umbenennen") else "")))
                erg.pop("pfad_aenderungen")
                st["ergebnis"] = erg
            except Exception as ex:
                st["ergebnis"] = {"fehler": [str(ex)]}
            finally:
                st["laeuft"] = False

        threading.Thread(target=lauf, daemon=True).start()
        return {"gestartet": True}

    def _pfade_aktualisieren(aenderungen):
        """(bild_id, alter Pfad, neuer Pfad). Das Bild wird über den alten Pfad gefunden, nicht über die
        Nummer: nach einem Zurücksetzen des Index gehört die Nummer womöglich einem anderen Bild."""
        geaendert = []
        with z.db.verbindung() as v:
            for _bild_id, alt, pfad in aenderungen:
                try:
                    st = Path(pfad).stat()
                except OSError:  # Datei inzwischen weg: der nächste Indexlauf räumt auf
                    continue
                r = v.execute("SELECT id FROM bilder WHERE pfad=?", (alt,)).fetchone()
                if not r:  # nicht (mehr) im Index: der nächste Indexlauf nimmt die Datei auf
                    continue
                # Größe mitführen (eingebettete Tags ändern sie), sonst gilt das Bild als geändert
                # und wird beim nächsten Lauf komplett neu analysiert
                v.execute("UPDATE OR IGNORE bilder SET pfad=?, mtime=?, groesse=? WHERE id=?",
                          (pfad, st.st_mtime, st.st_size, r[0]))
                geaendert.append(r[0])
        z.db.suchtext_aktualisieren_viele(geaendert)  # neuer Dateiname soll gefunden werden

    # ------------------------------------------------------------ Index zurücksetzen

    @app.post("/api/index/zuruecksetzen")
    def index_zuruecksetzen(daten: dict = Body(...)):
        """Index leeren (Bilder, Gesichter, Personen, Vorschaubilder), um von vorn anzufangen.
        Bleiben: Einstellungen, heruntergeladene Modelle, Protokolle (Rückgängig), Quarantäne, Kartenbilder.
        Die Bilddateien selbst werden nicht angefasst."""
        if daten.get("bestaetigt") is not True:
            raise HTTPException(400, "Bitte bestätigen.")
        if z.indexer.status["laeuft"] or z.sortier_status["laeuft"] or z.dubletten.status.get("laeuft"):
            raise HTTPException(409, "Es läuft gerade etwas (Index, Sortieren oder Dublettensuche). Bitte erst anhalten.")
        n = z.db.zuruecksetzen()
        for ordner in ("vorschau", "gesichter"):
            shutil.rmtree(daten_ordner() / ordner, ignore_errors=True)
        z.dubletten_ergebnis = None
        z.indexer.status.update(phase="bereit", fertig=0, gesamt=0, meldung="Index zurückgesetzt.", fehler=[],
                                aktuell=None, aktuell_pfad="")
        log.info("Index zurückgesetzt (%d Bilder entfernt)", n)
        return {"entfernt": n}

    @app.get("/api/protokolle")
    def protokolle():
        return {"protokolle": [dict(r) for r in z.db.abfrage("SELECT * FROM protokolle ORDER BY id DESC")]}

    @app.post("/api/protokolle/{pid}/rueckgaengig")
    def protokoll_rueckgaengig(pid: int, echt: bool = False):
        r = z.db.eins("SELECT * FROM protokolle WHERE id=?", (pid,))
        if not r:
            raise HTTPException(404)
        erg = sortieren.rueckgaengig(Path(r["datei"]), echt=echt)
        if echt:
            _pfade_aktualisieren(erg["pfad_aenderungen"])
            with z.db.verbindung() as v:
                v.execute("UPDATE protokolle SET rueckgaengig=1 WHERE id=?", (pid,))
        erg.pop("pfad_aenderungen")
        return erg

    # ------------------------------------------------------------ Dubletten

    @app.get("/api/dubletten")
    def dubletten_lesen():
        d = z.e.get("dubletten") or {}
        return {"status": z.dubletten.status, "ergebnis": z.dubletten_ergebnis,
                "ziel": d.get("ziel", "quarantaene"), "quarantaene": str(quarantaene_ordner(z.e)),
                "papierkorb": papierkorb.verfuegbar()}

    @app.post("/api/dubletten/suchen")
    def dubletten_suchen():
        if z.dubletten.status["laeuft"]:
            return {"gestartet": False}
        z.dubletten_stop.clear()
        z.dubletten.status.update(laeuft=True, phase="start", fertig=0, gesamt=0, meldung="Wird gestartet ...")

        def lauf():
            try:
                erg = z.dubletten.ausfuehren()
                if not erg["abgebrochen"] or z.dubletten_ergebnis is None:
                    z.dubletten_ergebnis = erg
            except Exception as ex:
                log.exception("Dublettensuche fehlgeschlagen")
                z.dubletten.status.update(laeuft=False, phase="fehler", meldung=f"Fehler: {ex}")

        threading.Thread(target=lauf, daemon=True).start()
        return {"gestartet": True}

    @app.post("/api/dubletten/stop")
    def dubletten_stop():
        z.dubletten_stop.set()
        return {"ok": True}

    @app.post("/api/dubletten/bereinigen")
    def dubletten_bereinigen(daten: dict = Body(...)):
        """``gruppen``: [{"behalten": [ids], "weg": [ids]}]. Jede Gruppe muss ein Bild behalten."""
        if z.dubletten.status["laeuft"]:
            raise HTTPException(409, "Die Dublettensuche läuft noch.")
        neu = {k: daten[k] for k in ("ziel", "quarantaene") if k in daten}
        if neu:
            z.e.aktualisieren({"dubletten": neu})
        d = z.e["dubletten"]
        weg, behalten = [], set()
        for g in daten.get("gruppen", []):
            b = {int(i) for i in g.get("behalten", [])}
            if not b:
                raise HTTPException(400, "In jeder Gruppe muss mindestens ein Bild behalten werden.")
            # das behaltene Bild muss wirklich noch da sein, sonst wäre danach keine Fassung mehr übrig
            # (z. B. veraltetes Ergebnis, Datei inzwischen woanders)
            da = [r["pfad"] for r in z.db.abfrage(
                f"SELECT pfad FROM bilder WHERE id IN ({','.join('?' * len(b))})", list(b))]
            if not any(Path(p).is_file() for p in da):
                raise HTTPException(400, "Das Bild zum Behalten ist nicht mehr da. Bitte die Dublettensuche "
                                         "neu starten.")
            behalten |= b
            weg += [int(i) for i in g.get("weg", [])]
        quarantaene = quarantaene_ordner(z.e)
        try:
            erg = dubletten.bereinigen(z.db, weg, behalten, d.get("ziel", "quarantaene"), quarantaene,
                                       daten_ordner() / "protokolle")
        except ValueError as ex:
            raise HTTPException(400, str(ex))
        if erg["protokoll"]:
            ort = "Papierkorb" if d.get("ziel") == "papierkorb" else "Quarantäne"
            with z.db.verbindung() as v:
                v.execute("INSERT INTO protokolle (datei, zeit, beschreibung) VALUES (?,?,?)",
                          (erg["protokoll"], datetime.now().isoformat(timespec="seconds"),
                           f"Dubletten aussortiert ({ort}), {len(erg['erledigt'])} Dateien"))
            erg["protokoll_id"] = z.db.eins("SELECT MAX(id) AS i FROM protokolle")["i"]
        bilder_entfernen(z.db, erg["erledigt"])  # Rückgängig legt sie zurück, der nächste Indexlauf nimmt sie auf
        # erledigte Gruppen aus der Liste nehmen (nicht verschobene Dateien bleiben in ihrer Gruppe sichtbar)
        erledigt = set(erg["erledigt"])
        if z.dubletten_ergebnis:
            fertig = {int(g["nr"]) for g in daten.get("gruppen", []) if g.get("nr") is not None}
            rest = []
            for g in z.dubletten_ergebnis["gruppen"]:
                bilder = [b for b in g["bilder"] if b["id"] not in erledigt]
                offen_weg = [b for b in g["bilder"] if b["id"] in set(weg) and b["id"] not in erledigt]
                if len(bilder) >= 2 and (g["nr"] not in fertig or offen_weg):
                    rest.append(dict(g, bilder=bilder))
            z.dubletten_ergebnis["gruppen"] = rest
        return erg

    # ------------------------------------------------------------ Oberfläche

    @app.exception_handler(Exception)
    async def fehler(_req, ex):
        log.exception("Fehler")
        return JSONResponse({"detail": str(ex)}, status_code=500)

    app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")
    return app
