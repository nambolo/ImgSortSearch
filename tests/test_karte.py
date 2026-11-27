"""Karte: Ortsnamen ohne Internet, Filter nach Ort und Kartenbereich, Kartenbilder-Zwischenspeicher."""
import time
import urllib.error
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from conftest import mache_bild
from imgsortsearch import orte
from imgsortsearch.server import erstelle_app


@pytest.fixture(scope="module")
def liste():
    return orte.ortsliste()


def test_ortsname_aus_koordinaten(liste):
    o = liste.ort(48.1372, 11.5762)  # Marienplatz
    assert (o["name"], o["region"], o["land"]) == ("München", "Bayern", "Deutschland")
    assert o["text"] == "München, Bayern, Deutschland"
    assert "Munich" in o["such"]  # englischer Name bleibt suchbar
    assert liste.ort(41.9028, 12.4964)["stadt"] == "Rom"  # Stadtteil Trevi: „Trevi bei Rom“
    assert liste.ort(47.0707, 15.4395)["land"] == "Österreich"


def test_kleiner_ort_bei_grosser_stadt(liste):
    o = liste.ort(48.2489, 11.6532)  # Garching bei München
    assert o["name"] != "München" and o["stadt"] == "München"
    assert " bei München" in o["text"]


def test_kein_ort_auf_dem_meer(liste):
    o = liste.ort(30.0, -40.0)  # mitten im Atlantik
    assert o["name"] == "" and o["land"] == ""
    assert liste.ort(None, None)["text"] == ""


def test_bereich_bedingung_ueber_datumsgrenze():
    b, p = orte.bereich_bedingung("10,170,20,190")
    assert "OR" in b and p == [10.0, 20.0, 170.0, -170.0]
    b, p = orte.bereich_bedingung("10,-200,20,200")
    assert "gps_lon" not in b
    assert orte.bereich_bedingung("unsinn") == ("1=1", [])


def warte_auf_index(c):
    for _ in range(300):
        if not c.get("/api/status").json()["index"]["laeuft"]:
            return
        time.sleep(0.1)
    raise AssertionError("Index wurde nicht fertig")


@pytest.fixture
def client(zustand, heim):
    q = heim / "backup"
    mache_bild(q / "muenchen.jpg", "iPhone 13", "2023:07:01 12:00:00", gps=(48.1372, 11.5762), farbe=(20, 220, 20))
    mache_bild(q / "garching.jpg", "iPhone 13", "2023:07:02 12:00:00", gps=(48.2489, 11.6532), farbe=(20, 220, 20))
    mache_bild(q / "rom.jpg", "iPhone 13", "2022:05:01 12:00:00", gps=(41.9028, 12.4964), farbe=(20, 220, 20))
    c = TestClient(erstelle_app(zustand), headers={"X-ImgSortSearch": "1"})
    assert c.post("/api/index/start").json()["gestartet"]
    warte_auf_index(c)
    return c


def namen(c, **params):
    return {Path(b["pfad"]).name for b in c.get("/api/bilder", params=params).json()["bilder"]}


def test_suche_nach_ort_und_filter(client):
    assert namen(client, q="München", art="text") == {"muenchen.jpg", "garching.jpg"}
    assert namen(client, q="Munich", art="text") == {"muenchen.jpg", "garching.jpg"}
    assert namen(client, q="Italien", art="text") == {"rom.jpg"}
    assert namen(client, ort="ort:München") == {"muenchen.jpg", "garching.jpg"}
    assert namen(client, ort="land:Italien") == {"rom.jpg"}
    assert namen(client, bereich="48.0,11.4,48.2,11.7") == {"muenchen.jpg"}
    f = client.get("/api/filter").json()
    assert {"name": "Deutschland", "anzahl": 2} in f["laender"]
    assert any(o["name"] == "München" and o["anzahl"] == 2 for o in f["orte"])
    d = client.get(f"/api/bild/{next(b['id'] for b in client.get('/api/bilder').json()['bilder'] if b['pfad'].endswith('rom.jpg'))}").json()
    assert d["ort"].endswith("bei Rom, Latium, Italien") and d["ort_stadt"] == "Rom"


def test_kartenpunkte(client):
    r = client.get("/api/karte/punkte").json()
    assert len(r["punkte"]) == 3 and r["ohne_gps"] == 5
    ort = {r["orte"][p[3]] for p in r["punkte"]}
    assert "München, Bayern, Deutschland" in ort
    assert len(client.get("/api/karte/punkte", params={"jahr": "2022"}).json()["punkte"]) == 1


def test_geaenderte_gps_daten_neu_zuordnen(client, zustand):
    i = zustand.db.eins("SELECT id FROM bilder WHERE pfad LIKE '%rom.jpg'")["id"]
    with zustand.db.verbindung() as v:
        v.execute("UPDATE bilder SET gps_lat=NULL, gps_lon=NULL WHERE id=?", (i,))
    assert orte.orte_zuordnen(zustand.db) == 1
    assert zustand.db.eins("SELECT ort, ort_land FROM bilder WHERE id=?", (i,))["ort"] == ""
    assert namen(client, q="Italien", art="text") == set()
    assert orte.orte_zuordnen(zustand.db) == 0  # nichts mehr zu tun


# ------------------------------------------------------------------ Kartenbilder

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 20


def test_kacheln_nur_mit_erlaubnis_aus_dem_netz(tmp_path):
    geholt = []
    k = orte.Kacheln(tmp_path / "kacheln", holen=lambda url: geholt.append(url) or PNG)
    assert k.kachel(3, 4, 5, online=False) is None and not geholt
    assert k.kachel(3, 4, 5, online=True) == PNG
    assert geholt == ["https://tile.openstreetmap.org/3/4/5.png"]
    assert k.kachel(3, 4, 5, online=True) == PNG and len(geholt) == 1  # aus dem Zwischenspeicher
    assert k.kachel(3, 4, 5, online=False) == PNG                    # auch offline
    assert k.kachel(3, 8, 0, online=True) is None                    # x außerhalb der Zoomstufe
    assert k.groesse() == (1, len(PNG))
    k.leeren()
    assert k.groesse() == (0, 0)


def test_kacheln_ohne_verbindung(tmp_path):
    versuche = []

    def kaputt(url):
        versuche.append(url)
        raise urllib.error.URLError("keine Verbindung")

    k = orte.Kacheln(tmp_path / "kacheln", holen=kaputt)
    assert k.kachel(1, 0, 0, online=True) is None
    assert k.kachel(1, 1, 0, online=True) is None
    assert len(versuche) == 1  # nach einem Fehler eine Weile nicht weiter versuchen
    assert k.offline_bis > time.time() and "keine Verbindung" in k.letzter_fehler


def test_benutzerkennung_fuer_osm():
    assert orte.BENUTZER_KENNUNG.startswith("ImgSortSearch/")
    assert "github.com" in orte.BENUTZER_KENNUNG


def test_kachel_endpunkt_und_einstellung(zustand):
    c = TestClient(erstelle_app(zustand), headers={"X-ImgSortSearch": "1"})
    assert zustand.e["karte"]["online"] is False  # standardmäßig nichts aus dem Internet
    zustand.kacheln._holen = lambda url: pytest.fail("darf ohne Erlaubnis nichts laden")
    assert c.get("/api/karte/kachel/2/1/1.png").status_code == 404
    zustand.kacheln._holen = lambda url: PNG
    c.post("/api/einstellungen", json={"karte": {"online": True}})
    r = c.get("/api/karte/kachel/2/1/1.png")
    assert r.status_code == 200 and r.content == PNG and r.headers["content-type"] == "image/png"
    info = c.get("/api/karte/info").json()
    assert info["online"] and info["kacheln"] == 1
    assert c.post("/api/karte/leeren").status_code == 200
    assert c.get("/api/karte/info").json()["kacheln"] == 0


def test_statische_kartendateien_liegen_bei(zustand):
    c = TestClient(erstelle_app(zustand))
    for datei in ("vendor/leaflet.js", "vendor/leaflet.markercluster.js", "vendor/welt.geojson", "karte.js"):
        assert c.get("/" + datei).status_code == 200, datei
