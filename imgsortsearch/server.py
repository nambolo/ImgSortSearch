"""Lokaler Webserver für die Oberfläche (nur auf 127.0.0.1 erreichbar)."""
from __future__ import annotations

import io
import logging
import shutil
import threading
from datetime import datetime
from pathlib import Path

import numpy as np
from fastapi import Body, FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from PIL import Image

from . import modelle, personen, sortieren, zwischenablage
from .datenbank import Datenbank
from .einstellungen import Einstellungen, daten_ordner
from .indexer import Indexer, Modelle, alle_neu_einordnen, lade_themen, text_suche_bedingung, vorschau_erzeugen, \
    vorschau_pfad
from .klassifizierung import UNKLAR

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
        self.fenster = None           # pywebview-Fenster, falls vorhanden
        self.sortier_status = {"laeuft": False, "fertig": 0, "gesamt": 0, "ergebnis": None}

    def _personen_zuordnen(self):
        self.indexer.status["meldung"] = "Gesichter werden Personen zugeordnet ..."
        personen.neue_gesichter_zuordnen(self.db, float(self.e["gesichter"].get("schwelle", 0.40)))


def erstelle_app(z: Zustand | None = None) -> FastAPI:
    z = z or Zustand()
    app = FastAPI(title="ImgSortSearch")
    app.state.z = z

    def bild_oder_404(bild_id: int):
        r = z.db.eins(f"SELECT *, {KAT} AS kategorie FROM bilder WHERE id=?", (bild_id,))
        if not r:
            raise HTTPException(404, "Bild nicht gefunden")
        return r

    def kurz(r) -> dict:
        return {"id": r["id"], "pfad": r["pfad"], "name": Path(r["pfad"]).name, "datum": r["datum"],
                "kategorie": r["kategorie"], "manuell": r["kategorie_manuell"] is not None,
                "sicherheit": r["sicherheit"], "tags": r["tags"] or "", "beschreibung": r["beschreibung"] or "",
                "kamera": r["kamera"], "gesichter": r["anzahl_gesichter"] or 0}

    # ------------------------------------------------------------ Status / Einstellungen

    @app.get("/api/status")
    def status():
        zahlen = {r["k"]: r["n"] for r in z.db.abfrage(f"SELECT {KAT} AS k, COUNT(*) AS n FROM bilder GROUP BY k")}
        offen = z.db.eins("SELECT COUNT(*) AS n FROM bilder WHERE schritt_clip=0 AND fehler IS NULL")["n"]
        return {"index": z.indexer.status, "kategorien": zahlen, "gesamt": sum(zahlen.values()),
                "offen": offen, "modell_fehler": z.m.fehler, "geraete": z.m.geraete(), "exiftool": shutil.which("exiftool") is not None,
                "sortieren": z.sortier_status}

    @app.get("/api/einstellungen")
    def einstellungen_lesen():
        return z.e.werte

    @app.post("/api/einstellungen")
    def einstellungen_schreiben(neu: dict = Body(...)):
        alt_beschl = z.e.get("beschleunigung")
        werte = z.e.aktualisieren(neu)
        if werte.get("beschleunigung") != alt_beschl and not z.indexer.status["laeuft"]:
            z.m.entladen()  # beim nächsten Gebrauch auf dem neuen Gerät laden
        if any(k in neu for k in ("kategorien", "arbeitszeit", "inhalt", "schwelle", "regeln", "themen")):
            threading.Thread(target=_einordnen, daemon=True).start()
        return werte

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
            erg = z.fenster.create_file_dialog(webview.FileDialog.FOLDER)
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
        return {"jahre": jahre, "kameras": kameras,
                "kategorien": [k["name"] for k in z.e["kategorien"]] + [UNKLAR]}

    @app.get("/api/bilder")
    def bilder(q: str = "", kategorie: str = "", jahr: str = "", kamera: str = "", person: int = 0,
               aehnlich_zu: int = 0, nur_manuell: bool = False, sortierung: str = "datum_ab",
               offset: int = 0, limit: int = 200, art: str = "beides"):
        bed, par = ["1=1"], []
        if kategorie:
            bed.append(f"{KAT}=?")
            par.append(kategorie)
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
        wo = " AND ".join(bed)

        rang: dict[int, float] | None = None
        if aehnlich_zu:
            r = z.db.eins("SELECT clip FROM bilder WHERE id=?", (aehnlich_zu,))
            if not r or r["clip"] is None:
                raise HTTPException(400, "Für dieses Bild gibt es noch keinen Bildvektor.")
            from .datenbank import blob_zu_vektor
            rang = _vektor_rang(blob_zu_vektor(r["clip"]), schwelle=0.0, max_n=1000)
        elif q.strip():
            rang = {}
            if art in ("beides", "text"):
                tb, tp = text_suche_bedingung(q)
                for r in z.db.abfrage(f"SELECT id FROM bilder WHERE {tb}", tp):
                    rang[r["id"]] = 2.0  # Textbefunde zuerst
            if art in ("beides", "bedeutung"):
                clip = z.m.clip()
                if clip is not None:
                    sem = _vektor_rang(clip.texte([q.strip()])[0], schwelle=0.17, max_n=600)
                    if sem:
                        # nur Treffer nahe am besten: sonst kommt bei seltenen Begriffen viel Rauschen dazu
                        grenze = max(sem.values()) - (0.08 if art == "bedeutung" else 0.05)
                        for i, s in sem.items():
                            if s >= grenze:
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
            treffer.sort(key=lambda r: -rang[r["id"]])
            return {"gesamt": len(treffer), "bilder": [kurz(r) for r in treffer[offset:offset + limit]]}

        ordnung = {"datum_ab": "datum DESC", "datum_auf": "datum ASC", "name": "pfad ASC",
                   "unsicher": "sicherheit ASC, datum DESC"}.get(sortierung, "datum DESC")
        gesamt = z.db.eins(f"SELECT COUNT(*) n FROM bilder WHERE {wo}", par)["n"]
        zeilen = z.db.abfrage(f"SELECT *, {KAT} AS kategorie FROM bilder WHERE {wo} ORDER BY {ordnung} "
                              f"LIMIT ? OFFSET ?", (*par, limit, offset))
        return {"gesamt": gesamt, "bilder": [kurz(r) for r in zeilen]}

    def _vektor_rang(v: np.ndarray, schwelle: float, max_n: int) -> dict[int, float]:
        ids, mat = z.db.clip_matrix()
        if not len(ids) or mat.shape[1] != v.shape[0]:
            return {}
        sims = mat @ (v / (np.linalg.norm(v) + 1e-9))
        ordnung = np.argsort(-sims)[:max_n]
        return {int(ids[i]): float(sims[i]) for i in ordnung if sims[i] >= schwelle}

    @app.post("/api/suche-mit-bild")
    async def suche_mit_bild(datei: UploadFile = File(...)):
        img = _upload_bild(await datei.read())
        clip = z.m.clip()
        if clip is None:
            raise HTTPException(503, "Bildmodell nicht verfügbar")
        rang = _vektor_rang(clip.bilder([img])[0], schwelle=0.0, max_n=300)
        zeilen = z.db.abfrage(f"SELECT *, {KAT} AS kategorie FROM bilder WHERE id IN ({','.join('?' * len(rang))})",
                              list(rang)) if rang else []
        zeilen = sorted(zeilen, key=lambda r: -rang[r["id"]])
        return {"gesamt": len(zeilen), "bilder": [kurz(r) for r in zeilen]}

    @app.get("/api/bild/{bild_id}")
    def bild_details(bild_id: int):
        r = bild_oder_404(bild_id)
        d = {k: r[k] for k in r.keys() if k not in ("clip", "suchtext")}
        d["name"] = Path(r["pfad"]).name
        d["gesichter"] = [dict(g) for g in z.db.abfrage(
            "SELECT g.id, g.x, g.y, g.w, g.h, g.person_id, p.name FROM gesichter g "
            "LEFT JOIN personen p ON p.id=g.person_id WHERE g.bild_id=?", (bild_id,))]
        return d

    @app.get("/api/vorschau/{bild_id}")
    def vorschau(bild_id: int):
        p = vorschau_pfad(bild_id)
        if not p.exists():
            r = bild_oder_404(bild_id)
            try:
                vorschau_erzeugen(bild_id, modelle.bild_laden(Path(r["pfad"]), max_kante=800))
            except Exception:
                raise HTTPException(404, "Keine Vorschau möglich")
        return FileResponse(p, media_type="image/jpeg", headers={"Cache-Control": "max-age=86400"})

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
    def gesicht_bild(gesicht_id: int):
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
        return FileResponse(cache, media_type="image/jpeg")

    @app.post("/api/bilder/kategorie")
    def kategorie_setzen(daten: dict = Body(...)):
        ids = [int(i) for i in daten.get("ids", [])]
        kat = daten.get("kategorie") or None  # None = wieder automatisch
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
        zwischenablage.im_explorer_zeigen(bild_oder_404(bild_id)["pfad"])
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
        return {"personen": personen.personen_liste(z.db)}

    @app.post("/api/personen/neu-gruppieren")
    def personen_gruppieren():
        n = personen.neue_gesichter_zuordnen(z.db, float(z.e["gesichter"].get("schwelle", 0.40)))
        return {"zugeordnet": n}

    @app.post("/api/personen/{person_id}/name")
    def person_benennen(person_id: int, daten: dict = Body(...)):
        personen.umbenennen(z.db, person_id, daten.get("name", ""))
        return {"ok": True}

    @app.post("/api/personen/zusammenfuehren")
    def person_zusammen(daten: dict = Body(...)):
        personen.zusammenfuehren(z.db, int(daten["ziel"]), [int(i) for i in daten["quellen"]])
        return {"ok": True}

    @app.get("/api/personen/{person_id}/gesichter")
    def person_gesichter(person_id: int):
        return {"gesichter": [dict(g) for g in z.db.abfrage(
            "SELECT id, bild_id FROM gesichter WHERE person_id=? ORDER BY score DESC LIMIT 300", (person_id,))]}

    @app.post("/api/gesicht/{gesicht_id}/entfernen")
    def gesicht_entfernen(gesicht_id: int):
        personen.gesicht_entfernen(z.db, gesicht_id)
        return {"ok": True}

    @app.post("/api/personensuche")
    async def personensuche(datei: UploadFile = File(...), gesicht: int = 0):
        img = _upload_bild(await datei.read())
        modell = z.m.gesichter()
        if modell is None:
            raise HTTPException(503, f"Gesichtserkennung nicht verfügbar: {z.m.fehler.get('gesichter')}")
        gef = modell.finde(img)
        if not gef:
            return {"gesichter": [], "bilder": [], "gesamt": 0}
        gef.sort(key=lambda g: -g["w"] * g["h"])
        wahl = gef[min(gesicht, len(gef) - 1)]
        rang = dict(personen.aehnliche_gesichter(z.db, wahl["vektor"]))
        zeilen = z.db.abfrage(f"SELECT *, {KAT} AS kategorie FROM bilder WHERE id IN ({','.join('?' * len(rang))})",
                              list(rang)) if rang else []
        zeilen = sorted(zeilen, key=lambda r: -rang[r["id"]])
        w, h = img.size
        return {"gesichter": [{"x": g["x"] / w, "y": g["y"] / h, "w": g["w"] / w, "h": g["h"] / h} for g in gef],
                "gewaehlt": min(gesicht, len(gef) - 1), "gesamt": len(zeilen), "bilder": [kurz(r) for r in zeilen]}

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
        zeilen = z.db.abfrage(f"SELECT id, pfad, datum, kamera, tags, beschreibung, {KAT} AS kategorie FROM bilder")
        namen = {}
        for r in z.db.abfrage("SELECT g.bild_id, p.name FROM gesichter g JOIN personen p ON p.id=g.person_id "
                              "WHERE p.name IS NOT NULL"):
            namen.setdefault(r["bild_id"], []).append(r["name"])
        erg = []
        for r in zeilen:
            if nur and r["kategorie"] not in nur:
                continue
            erg.append(sortieren.Auftrag(
                pfad=Path(r["pfad"]), kategorie=r["kategorie"],
                datum=datetime.fromisoformat(r["datum"]) if r["datum"] else None, kamera=r["kamera"] or "",
                tags=[t.strip() for t in (r["tags"] or "").split(",") if t.strip()],
                personen=sorted(set(namen.get(r["id"], []))), beschreibung=r["beschreibung"] or "",
                bild_id=r["id"]))
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

        def lauf():
            st = z.sortier_status
            st.update(laeuft=True, fertig=0, gesamt=len(auftraege), ergebnis=None)
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
        with z.db.verbindung() as v:
            for bild_id, pfad in aenderungen:
                if bild_id:
                    st = Path(pfad).stat()
                    v.execute("UPDATE bilder SET pfad=?, mtime=? WHERE id=?", (pfad, st.st_mtime, bild_id))

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

    # ------------------------------------------------------------ Oberfläche

    @app.exception_handler(Exception)
    async def fehler(_req, ex):
        log.exception("Fehler")
        return JSONResponse({"detail": str(ex)}, status_code=500)

    app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")
    return app
