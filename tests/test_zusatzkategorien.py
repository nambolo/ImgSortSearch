"""Zusatzkategorien (schließen sich nicht aus, mehrere pro Bild) und Neu bewerten."""
import sqlite3
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from imgsortsearch.datenbank import Datenbank
from imgsortsearch.klassifizierung import einordnen
from imgsortsearch.server import erstelle_app


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


def bilder(c, **params):
    return {Path(b["pfad"]).name: b for b in c.get("/api/bilder", params=params).json()["bilder"]}


def mit_zusatz(client, zustand):
    """Natur und Familie als Zusatzkategorien; ein Thema 'Meer' vergibt Natur automatisch."""
    kats = zustand.e["kategorien"] + [{"name": "Natur", "zusatz": True}, {"name": "Familie", "zusatz": True}]
    themen = zustand.e["themen"] + [{"name": "Meer", "prompt": "strand am meer", "kategorie": "Natur"}]
    client.post("/api/einstellungen", json={"kategorien": kats, "themen": themen})
    client.post("/api/index/start")
    warte(client)
    client.post("/api/einordnen")


def test_alter_index_bekommt_neue_spalten(tmp_path):
    datei = tmp_path / "alt.sqlite"
    Datenbank(datei)
    con = sqlite3.connect(datei)  # Stand vor den Zusatzkategorien nachbauen
    con.execute("ALTER TABLE bilder DROP COLUMN zusatz")
    con.execute("ALTER TABLE bilder DROP COLUMN zusatz_auto")
    con.execute("INSERT INTO bilder (pfad, kategorie_manuell) VALUES ('C:/a.jpg', 'Arbeit')")
    con.commit()
    con.close()
    db = Datenbank(datei)
    r = db.eins("SELECT * FROM bilder")
    assert r["kategorie_manuell"] == "Arbeit" and r["zusatz"] == "" and r["zusatz_auto"] == ""


def test_zusatz_kategorien_laufen_nicht_im_wettbewerb():
    e = {"kategorien": [{"name": "Arbeit"}, {"name": "Privat"}, {"name": "Natur", "zusatz": True}],
         "inhalt": {"aktiv": True, "gewicht": 3}, "arbeitszeit": {"aktiv": False}, "regeln": [], "schwelle": 1.0}
    erg = einordnen({}, {"kategorien": {"Arbeit": 0.0, "Privat": 0.9, "Natur": 1.0}, "tags": ["Strand"]}, e)
    assert erg["kategorie"] == "Privat"


def test_automatisch_und_von_hand(client, zustand):
    mit_zusatz(client, zustand)
    alle = bilder(client)
    assert "Natur" in alle["b.jpg"]["zusatz"]  # grün = Strand/Meer
    assert alle["b.jpg"]["kategorie"] == "Privat"  # Hauptkategorie bleibt
    assert alle["a.jpg"]["zusatz"] == []

    # mehrere Bilder auf einmal: Familie dazu
    ids = [alle["a.jpg"]["id"], alle["b.jpg"]["id"], alle["c.jpg"]["id"]]
    assert client.post("/api/bilder/zusatz", json={"ids": ids, "kategorie": "Familie", "an": True}).status_code == 200
    alle = bilder(client)
    assert alle["b.jpg"]["zusatz"] == ["Natur", "Familie"]
    assert alle["a.jpg"]["kategorie"] == "Arbeit" and alle["a.jpg"]["zusatz"] == ["Familie"]

    # Filter, Zähler, Textsuche
    assert set(bilder(client, kategorie="Familie")) == {"a.jpg", "b.jpg", "c.jpg"}
    s = client.get("/api/status").json()
    assert s["kategorien"]["Familie"] == 3 and s["gesamt"] == 5
    assert "b.jpg" in bilder(client, q="familie", art="text")

    # von Hand entfernen schlägt automatisch, auch nach neuem Einordnen
    b_id = alle["b.jpg"]["id"]
    client.post("/api/bilder/zusatz", json={"ids": [b_id], "kategorie": "Natur", "an": False})
    client.post("/api/einordnen")
    assert bilder(client)["b.jpg"]["zusatz"] == ["Familie"]
    d = client.get(f"/api/bild/{b_id}").json()
    assert d["zusatz_hand"] == {"Natur": False, "Familie": True}
    # wieder automatisch
    client.post("/api/bilder/zusatz", json={"ids": [b_id], "kategorie": "Natur", "an": None})
    assert bilder(client)["b.jpg"]["zusatz"] == ["Natur", "Familie"]

    # Hauptkategorie-Knopf mit einer Zusatzkategorie ersetzt nichts
    client.post("/api/bilder/kategorie", json={"ids": [alle["d.jpg"]["id"]], "kategorie": "Natur"})
    d_bild = bilder(client)["d.jpg"]
    assert "Natur" in d_bild["zusatz"] and not d_bild["manuell"]

    assert client.post("/api/bilder/zusatz", json={"ids": ids, "kategorie": "Arbeit", "an": True}).status_code == 400


def test_kategorie_geloescht_verschwindet(client, zustand):
    mit_zusatz(client, zustand)
    ids = [b["id"] for b in bilder(client).values()]
    client.post("/api/bilder/zusatz", json={"ids": ids, "kategorie": "Familie", "an": True})
    kats = [k for k in zustand.e["kategorien"] if k["name"] != "Familie"]
    client.post("/api/einstellungen", json={"kategorien": kats})
    warte(client)
    client.post("/api/einordnen")
    assert all("Familie" not in b["zusatz"] for b in bilder(client).values())


def test_hauptkategorie_wird_zusatz(client, zustand):
    client.post("/api/index/start")
    warte(client)
    a_id = bilder(client)["a.jpg"]["id"]
    client.post("/api/einstellungen", json={"kategorien": zustand.e["kategorien"] + [{"name": "Natur"}]})
    client.post("/api/bilder/kategorie", json={"ids": [a_id], "kategorie": "Natur"})
    kats = [dict(k, zusatz=True) if k["name"] == "Natur" else k for k in zustand.e["kategorien"]]
    client.post("/api/einstellungen", json={"kategorien": kats})
    a = bilder(client)["a.jpg"]
    assert a["zusatz"] == ["Natur"] and not a["manuell"]


def test_sortieren_mit_zusatz(client, zustand, heim):
    mit_zusatz(client, zustand)
    alle = bilder(client)
    client.post("/api/bilder/zusatz", json={"ids": [alle["a.jpg"]["id"]], "kategorie": "Familie", "an": True})
    ziel = heim / "ziel"
    s = {"ziel": str(ziel), "modus": "kopieren", "tags": "sidecar", "ordner": "{kategorie}"}
    r = client.post("/api/sortieren/vorschau", json={"einstellungen": s, "nur": ["Familie"]}).json()
    assert [Path(v["von"]).name for v in r["vorschau"]] == ["a.jpg"]
    assert Path(r["vorschau"][0]["nach"]).parent.name == "Arbeit"  # Ordner nach Hauptkategorie
    client.post("/api/sortieren/ausfuehren", json={"einstellungen": s, "nur": ["Familie"]})
    warte(client)
    xmp = (ziel / "Arbeit" / "a.jpg.xmp").read_text("utf-8")
    assert "<rdf:li>Familie</rdf:li>" in xmp and "ImgSortSearch|Kategorie|Familie" in xmp


def test_neu_analysieren_behaelt_handarbeit(client, zustand):
    client.post("/api/index/start")
    warte(client)
    a = bilder(client)["a.jpg"]
    client.post("/api/bilder/kategorie", json={"ids": [a["id"]], "kategorie": "Privat"})
    r = client.post("/api/bilder/neu-analysieren", json={"ids": [a["id"]]}).json()
    assert r["vorgemerkt"] == 1
    s = warte(client)
    assert s["index"]["gesamt"] == 1  # nur das eine Bild
    neu = client.get(f"/api/bild/{a['id']}").json()
    assert neu["kategorie_manuell"] == "Privat" and neu["schritt_clip"] == 1
    assert client.post("/api/bilder/neu-analysieren", json={"ids": []}).status_code == 400


def test_themen_filter_in_der_suche(client, zustand):
    client.post("/api/index/start")
    warte(client)
    f = client.get("/api/filter").json()
    namen = {t["name"]: t["anzahl"] for t in f["themen"]}
    assert namen["Whiteboard"] == 2 and namen["Hund"] == 2  # a.jpg + Screenshot, c.jpg + d.jpg
    assert set(bilder(client, thema="Hund")) == {"c.jpg", "d.jpg"}
    assert set(bilder(client, thema="Hund", kategorie="Privat")) <= {"c.jpg", "d.jpg"}
    assert bilder(client, thema="Hun") == {}  # nur ganze Themen
