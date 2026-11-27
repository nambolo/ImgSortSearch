"""Tests zu Fehlern, die beim Durchspielen mit einer großen Testbibliothek aufgefallen sind."""
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from conftest import mache_bild
from imgsortsearch import sortieren
from imgsortsearch.metadata import datum_aus_dateiname
from imgsortsearch.server import erstelle_app

hat_exiftool = pytest.mark.skipif(not shutil.which("exiftool"), reason="exiftool fehlt")


@pytest.fixture
def client(zustand):
    return TestClient(erstelle_app(zustand), headers={"X-ImgSortSearch": "1"})


def warte(c, max_s=30):
    for _ in range(max_s * 20):
        s = c.get("/api/status").json()
        if not s["index"]["laeuft"] and not s["sortieren"]["laeuft"]:
            return s
        time.sleep(0.05)
    raise AssertionError("läuft noch")


@pytest.mark.parametrize("name", [
    "Bildschirmfoto 2023-06-22 um 18.37.41.png",          # Mac, deutsch
    "Screenshot 2023-06-22 at 18.37.41.png",              # Mac, englisch
    "WhatsApp Image 2023-06-22 at 18.37.41.jpeg",
    "Screenshot 2023-06-22 183741.png",                   # Windows 11 Snipping Tool
    "signal-2023-06-22-183741.jpg",
    "2023-06-22 18.37.41.jpg",
])
def test_uhrzeit_aus_dateinamen(name):
    # ohne Uhrzeit landen Screenshots um 0 Uhr und damit immer in der Freizeit
    assert datum_aus_dateiname(name) == datetime(2023, 6, 22, 18, 37, 41)


def test_nur_tag_bleibt_nur_tag():
    assert datum_aus_dateiname("IMG-20230622-WA0001.jpg") == datetime(2023, 6, 22)
    assert datum_aus_dateiname("2023-06-22 1837.jpg") == datetime(2023, 6, 22)


def test_kaputte_bilder_per_dateiname_auffindbar(client, heim):
    (heim / "backup" / "Kaputt").mkdir()
    (heim / "backup" / "Kaputt" / "leer.jpg").write_bytes(b"")
    client.post("/api/index/start")
    warte(client)
    treffer = client.get("/api/bilder", params={"q": "leer", "art": "text"}).json()["bilder"]
    assert [b["name"] for b in treffer] == ["leer.jpg"]


def test_suchtext_schon_nach_metadaten(zustand):
    """Neue Bilder sind über Dateiname/Ordner auffindbar, bevor die Analyse sie erreicht."""
    zustand.indexer.dateien_einlesen()
    r = zustand.db.eins("SELECT suchtext FROM bilder WHERE pfad LIKE '%Screenshot_1.png'")
    assert "screenshot_1" in r["suchtext"]


@hat_exiftool
def test_einbetten_bmp_bekommt_xmp_datei(tmp_path):
    bmp = tmp_path / "scan.bmp"
    Image.new("RGB", (8, 8), "white").save(bmp)
    a = sortieren.Auftrag(pfad=bmp, kategorie="Arbeit", datum=None, kamera="", tags=[], personen=[])
    erg = sortieren.ausfuehren([a], {"modus": "nur_taggen", "tags": "einbetten"}, tmp_path / "prot", echt=True)
    assert erg["zaehler"]["ok"] == 1 and not erg["fehler"]
    assert (tmp_path / "scan.bmp.xmp").exists()


@hat_exiftool
def test_einbetten_viele_dateien_ein_exiftool(tmp_path):
    q = tmp_path / "Ürlaub & Co"
    auftraege = []
    for i in range(5):
        p = mache_bild(q / f"Bild {i} Jürgen.jpg", "iPhone 13", "2024:03:12 10:15:00")
        auftraege.append(sortieren.Auftrag(pfad=p, kategorie="Privat", datum=None, kamera="", tags=["Strand"],
                                           personen=["Jürgen Müller"]))
    kaputt = q / "kaputt.jpg"
    kaputt.write_text("kein Bild")
    auftraege.insert(2, sortieren.Auftrag(pfad=kaputt, kategorie="Privat", datum=None, kamera="", tags=[],
                                          personen=[]))
    starts = []
    echt = sortieren._Exiftool._starten

    def zaehlen(self):
        starts.append(1)
        echt(self)

    sortieren._Exiftool._starten = zaehlen
    try:
        erg = sortieren.ausfuehren(auftraege, {"modus": "nur_taggen", "tags": "einbetten"}, tmp_path / "p", echt=True)
    finally:
        sortieren._Exiftool._starten = echt
    assert erg["zaehler"]["ok"] == 5 and erg["zaehler"]["fehler"] == 1, erg
    assert "kaputt.jpg" in erg["fehler"][0]
    assert len(starts) == 1  # ein Prozess für alle Dateien, auch nach dem Fehler
    out = subprocess.run(["exiftool", "-s3", "-Subject", "-charset", "filename=utf8", str(auftraege[0].pfad)],
                         capture_output=True).stdout.decode("utf-8")
    assert "Jürgen Müller" in out and "Strand" in out
    # rückgängig stellt die Originale wieder her
    sortieren.rueckgaengig(Path(erg["protokoll"]), echt=True)
    assert not list(q.glob("*_original"))


def test_ordner_entfernen_mit_abfrage(client, zustand, heim):
    """Beim Entfernen eines Bilder-Ordners entscheidet man, ob seine Bilder im Index bleiben."""
    andere = heim / "zweiter"
    mache_bild(andere / "x.jpg", "iPhone 13", "2024:03:12 10:15:00", farbe=(20, 20, 220))
    zustand.e.aktualisieren({"quellen": zustand.e["quellen"] + [str(andere)]})
    client.post("/api/index/start")
    warte(client)
    assert client.get("/api/status").json()["gesamt"] == 6
    assert client.get("/api/quellen/anzahl", params={"pfad": str(andere)}).json()["anzahl"] == 1

    # behalten: Ordner ist ausgetragen, Bild bleibt im Index
    r = client.post("/api/quellen/entfernen", json={"pfad": str(andere), "aus_index": False}).json()
    assert str(andere) not in r["einstellungen"]["quellen"] and r["entfernt"] == 0
    assert client.get("/api/status").json()["gesamt"] == 6

    # entfernen: Bilder, Gesichter und leere Personen sind weg, die Datei bleibt
    backup = zustand.e["quellen"][0]
    n = client.get("/api/quellen/anzahl", params={"pfad": backup}).json()["anzahl"]
    assert n == 5
    r = client.post("/api/quellen/entfernen", json={"pfad": backup, "aus_index": True}).json()
    assert r["entfernt"] == 5 and r["einstellungen"]["quellen"] == []
    assert client.get("/api/status").json()["gesamt"] == 1
    assert (heim / "backup" / "a.jpg").exists()
    personen = zustand.db.abfrage("SELECT id FROM personen WHERE id NOT IN (SELECT person_id FROM gesichter)")
    assert not personen


def test_ordner_entfernen_laesst_unterordner_einer_anderen_quelle(client, zustand, heim):
    unter = heim / "backup" / "neu"
    mache_bild(unter / "e.jpg", "iPhone 13", "2024:05:01 12:00:00")
    zustand.e.aktualisieren({"quellen": zustand.e["quellen"] + [str(unter)]})
    client.post("/api/index/start")
    warte(client)
    r = client.post("/api/quellen/entfernen", json={"pfad": str(heim / "backup"), "aus_index": True}).json()
    assert r["entfernt"] == 5  # e.jpg gehört weiter zum eingetragenen Unterordner
    assert [b["name"] for b in client.get("/api/bilder").json()["bilder"]] == ["e.jpg"]
