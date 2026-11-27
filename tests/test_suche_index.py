"""Bugsuche Suche und Index (2026-11-27): Sortierung, Namenssuche, doppelte Treffer, Vorschau-Zwischenspeicher,
Doppelstart, nachträglich vorgemerkte Bilder, Texterkennung "alle", Bildmodellwechsel."""
import threading
import time
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from conftest import mache_bild
from imgsortsearch import indexer
from imgsortsearch.datenbank import saetze_zu_blob
from imgsortsearch.server import erstelle_app


def warte(c, max_s=30):
    for _ in range(max_s * 20):
        s = c.get("/api/status").json()
        if not s["index"]["laeuft"]:
            return s
        time.sleep(0.05)
    raise AssertionError("läuft noch")


@pytest.fixture
def client(zustand):
    return TestClient(erstelle_app(zustand), headers={"X-ImgSortSearch": "1"})


def indizieren(c):
    assert c.post("/api/index/start").json()["gestartet"]
    return warte(c)


def namen(c, **params):
    return [Path(b["pfad"]).name for b in c.get("/api/bilder", params=params).json()["bilder"]]


# ------------------------------------------------------------------ Sortierung

def test_sortierung_gilt_auch_fuer_suchtreffer(client, zustand):
    """Bei einer Suche wurde "Älteste zuerst" / "Nach Name" still ignoriert."""
    client.post("/api/einstellungen", json={"ocr": {"modus": "alle"}})
    indizieren(client)
    with zustand.db.verbindung() as v:
        v.execute("UPDATE bilder SET datum=NULL WHERE pfad LIKE '%Screenshot_1.png'")
    # alle Bilder haben denselben OCR-Text: gleicher Rang
    standard = namen(client, q="rechnung", art="text")
    assert standard[:4] == ["d.jpg", "c.jpg", "b.jpg", "a.jpg"]  # bei gleichem Rang die neuesten zuerst
    assert standard[-1] == "Screenshot_1.png"  # ohne Datum zuletzt
    auf = namen(client, q="rechnung", art="text", sortierung="datum_auf")
    assert auf == ["a.jpg", "b.jpg", "c.jpg", "d.jpg", "Screenshot_1.png"]
    assert namen(client, q="rechnung", art="text", sortierung="name") == sorted(standard, key=str.lower)
    # Rang geht vor: der Dateiname trifft per Text, der Rest nur über die Bedeutung
    assert namen(client, q="a.jpg")[0] == "a.jpg"


def test_sortierung_ohne_suche_eindeutig_und_ohne_datum_zuletzt(client, zustand, heim):
    for i in range(30):  # viele Bilder mit gleichem Datum: "Weitere laden" darf keines doppelt bringen
        mache_bild(heim / "backup" / "gleich" / f"g{i:02d}.jpg", "iPhone 13", "2024:01:01 12:00:00")
    indizieren(client)
    with zustand.db.verbindung() as v:
        v.execute("UPDATE bilder SET datum=NULL WHERE pfad LIKE '%Screenshot_1.png'")
    ids = []
    for off in range(0, 40, 7):
        ids += [b["id"] for b in client.get("/api/bilder", params={"offset": off, "limit": 7}).json()["bilder"]]
    assert len(ids) == len(set(ids)) == 35
    assert namen(client, sortierung="datum_auf")[-1] == "Screenshot_1.png"  # ohne Datum nicht zuerst
    assert namen(client, sortierung="datum_ab")[-1] == "Screenshot_1.png"
    # "Unsicherste zuerst": von Hand eingeordnete Bilder ans Ende, die sind ja schon geprüft
    a = next(b for b in client.get("/api/bilder").json()["bilder"] if b["name"] == "a.jpg")
    with zustand.db.verbindung() as v:
        v.execute("UPDATE bilder SET sicherheit=0")
    client.post("/api/bilder/kategorie", json={"ids": [a["id"]], "kategorie": "Privat"})
    assert namen(client, sortierung="unsicher")[-1] == "a.jpg"


# ------------------------------------------------------------------ Namenssuche

def _person_benennen(client, name):
    pid = client.get("/api/personen").json()["personen"][0]["id"]
    client.post(f"/api/personen/{pid}/name", json={"name": name})
    return pid


def test_name_und_begriff_nur_bilder_der_person(client, zustand):
    """ "Anna Strand": Bilder der Person zuerst und nur diese, auch wenn der Begriff nur dem
    Bildinhalt nach passt. Früher überschrieb die Textsuche den Rang der Personentreffer, und die
    Bedeutungssuche mischte Strandbilder anderer Leute dazu."""
    indizieren(client)
    _person_benennen(client, "Anna")
    # c.jpg und d.jpg zeigen Anna (blau = Hund laut Ersatzmodell); b.jpg ist ein Strandbild ohne Anna
    for art in ("beides", "bedeutung"):
        assert set(namen(client, q="Anna Hund", art=art)) == {"c.jpg", "d.jpg"}, art
        assert namen(client, q="Anna Strand", art=art) == [], art  # kein Strandbild mit Anna
    # Name + Text: Treffer der Person vor allen anderen
    with zustand.db.verbindung() as v:
        v.execute("UPDATE bilder SET ocr_text='Urlaub' WHERE pfad LIKE '%c.jpg'")
    zustand.db.suchtext_aktualisieren_viele(r["id"] for r in zustand.db.abfrage("SELECT id FROM bilder"))
    assert namen(client, q="anna urlaub", art="text") == ["c.jpg"]
    # nur Wortanfang ("Ann"): weiter auch andere Treffer, aber die Person zuerst
    assert namen(client, q="ann urlaub")[0] == "c.jpg"


def test_satzzeichen_in_der_suche(client):
    indizieren(client)
    assert "a.jpg" in namen(client, q="„Rechnung“,", art="text")
    assert "a.jpg" in namen(client, q='"Müller"', art="text")
    assert namen(client, q='""', art="text") == []  # nur Satzzeichen: nichts, nicht alles


# ------------------------------------------------------------------ Vorschaubilder

def test_vorschau_wird_nach_zuruecksetzen_nicht_aus_dem_browser_genommen(client, zustand):
    """Nach "Index zurücksetzen" vergibt SQLite die Bildnummern neu. Mit max-age=86400 zeigte der
    Browser unter derselben Adresse einen Tag lang das alte Vorschaubild."""
    indizieren(client)
    bid = client.get("/api/bilder").json()["bilder"][0]["id"]
    r = client.get(f"/api/vorschau/{bid}")
    assert r.status_code == 200 and "max-age" not in r.headers.get("cache-control", "")
    etag = r.headers["etag"]
    assert client.get(f"/api/vorschau/{bid}", headers={"If-None-Match": etag}).status_code == 304
    # neue Vorschau unter derselben Nummer: anderes ETag, also lädt der Browser neu
    time.sleep(0.01)
    indexer.vorschau_pfad(bid).write_bytes(client.get(f"/api/vorschau/{bid}").content + b"\0")
    r2 = client.get(f"/api/vorschau/{bid}", headers={"If-None-Match": etag})
    assert r2.status_code == 200 and r2.headers["etag"] != etag


# ------------------------------------------------------------------ Indexlauf

def test_index_startet_nicht_doppelt(zustand, monkeypatch):
    ix = zustand.indexer
    los = threading.Event()
    monkeypatch.setattr(ix, "_lauf", lambda schritte: los.wait(5))
    erg = []
    threads = [threading.Thread(target=lambda: erg.append(ix.starten())) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert erg.count(True) == 1
    assert ix.status["laeuft"]  # sofort, damit z. B. "Index zurücksetzen" es sieht
    los.set()


def test_waehrend_des_laufs_vorgemerkte_bilder_kommen_gleich_dran(client, zustand):
    """ "Neu analysieren" während eines Laufs: die Bilder blieben bis zum nächsten Lauf liegen."""
    indizieren(client)
    ix = zustand.indexer
    a = zustand.db.eins("SELECT id FROM bilder WHERE pfad LIKE '%a.jpg'")["id"]
    original = ix.analysieren
    aufrufe = []

    def analysieren(schritte=None):
        aufrufe.append(1)
        original(schritte)
        if len(aufrufe) == 1:  # Nutzer merkt während des Laufs ein Bild vor
            client.post("/api/bilder/neu-analysieren", json={"ids": [a]})
    ix.analysieren = analysieren
    with zustand.db.verbindung() as v:
        v.execute("UPDATE bilder SET schritt_ocr=0 WHERE pfad LIKE '%b.jpg'")
    indizieren(client)
    assert len(aufrufe) == 2
    assert zustand.db.eins("SELECT COUNT(*) n FROM bilder WHERE schritt_clip=0 OR schritt_ocr=0")["n"] == 0


def test_fehlendes_modell_fuehrt_nicht_zu_endlosen_durchgaengen(client, zustand):
    zustand.m._gesichter = None
    zustand.m.fehler["gesichter"] = "nicht da"
    zustand.m.gesichter = lambda: None
    s = indizieren(client)
    assert s["index"]["phase"] == "fertig"
    assert sum("gesichter übersprungen" in f for f in s["index"]["fehler"]) == 1


def test_texterkennung_alle_holt_uebersprungene_bilder_nach(client, zustand):
    """Mit "nur Bilder mit Text" übersprungene Fotos galten als erledigt. Nach dem Umstellen auf
    "alle Bilder" wurden sie nie gelesen."""
    indizieren(client)  # Standard text_bilder: nur Bilder, die nach Text aussehen, werden gelesen
    leer = zustand.db.eins("SELECT COUNT(*) n FROM bilder WHERE ocr_text IS NULL OR ocr_text=''")["n"]
    assert leer > 0
    client.post("/api/einstellungen", json={"ocr": {"modus": "alle"}})
    indizieren(client)
    assert zustand.db.eins("SELECT COUNT(*) n FROM bilder WHERE ocr_text IS NULL OR ocr_text=''")["n"] == 0


def test_bildmodellwechsel_berechnet_satzvektoren_neu(client, zustand, monkeypatch):
    """Die Satzvektoren der ausführlichen Beschreibungen kommen vom Bildmodell. Nach einem Wechsel
    passten sie nicht mehr zur Suche und wurden nie neu berechnet."""
    indizieren(client)
    with zustand.db.verbindung() as v:
        v.execute("UPDATE bilder SET beschreibung_lang='Ein Hund am Strand. Viel Sonne.', schritt_lang=1, "
                  "beschreibung_lang_vek=?", (saetze_zu_blob(np.ones((2, 7))),))  # altes Modell: 7 Werte
    monkeypatch.setattr(indexer.Modelle, "clip_kennung", lambda self: "fake2")
    indizieren(client)
    ids, mat = zustand.db.beschreibung_matrix()
    assert len(ids) == 2 * zustand.db.eins("SELECT COUNT(*) n FROM bilder")["n"]
    assert mat.shape[1] == 32  # Vektorlänge des neuen (Ersatz-)Modells
