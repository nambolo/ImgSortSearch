import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from imgsortsearch.server import erstelle_app


def warte_auf_index(c: TestClient, max_s=30):
    for _ in range(max_s * 10):
        s = c.get("/api/status").json()
        if not s["index"]["laeuft"]:
            return s
        time.sleep(0.1)
    raise AssertionError("Index wurde nicht fertig")


@pytest.fixture
def client(zustand):
    return TestClient(erstelle_app(zustand))


def bilder(c, **params):
    return {Path(b["pfad"]).name: b for b in c.get("/api/bilder", params=params).json()["bilder"]}


def test_index_einordnen_suchen(client, zustand):
    assert client.post("/api/index/start").json()["gestartet"]
    s = warte_auf_index(client)
    assert s["index"]["phase"] == "fertig", s
    assert s["gesamt"] == 5 and s["offen"] == 0

    alle = bilder(client)
    assert alle["a.jpg"]["kategorie"] == "Arbeit"       # Whiteboard + Arbeitszeit
    assert alle["b.jpg"]["kategorie"] == "Privat"       # Strand + Wochenende
    assert "Whiteboard" in alle["a.jpg"]["tags"]
    assert alle["c.jpg"]["gesichter"] == 1

    # Volltext: OCR-Text (Umlaut egal) und Beschreibung
    assert "Screenshot_1.png" in bilder(client, q="muller", art="text")
    assert "Screenshot_1.png" in bilder(client, q="Müller 4711", art="text")
    # Bedeutungssuche
    treffer = list(bilder(client, q="Hund im Park", art="bedeutung"))
    assert set(treffer[:2]) == {"c.jpg", "d.jpg"}
    # ähnliche Bilder
    a_id = alle["a.jpg"]["id"]
    aehnlich = list(bilder(client, aehnlich_zu=a_id))
    assert set(aehnlich[:2]) == {"a.jpg", "Screenshot_1.png"}
    # Filter
    assert set(bilder(client, kategorie="Privat")) >= {"b.jpg"}

    # zweiter Lauf: nichts mehr zu tun
    client.post("/api/index/start")
    s = warte_auf_index(client)
    assert s["index"]["gesamt"] == 0


def test_neue_bilder_nur_neu_indizieren(client, zustand, heim):
    from conftest import mache_bild
    client.post("/api/index/start")
    warte_auf_index(client)
    mache_bild(heim / "backup" / "neu" / "e.jpg", "iPhone 13", "2024:05:01 12:00:00", farbe=(20, 220, 20))
    client.post("/api/index/start")
    s = warte_auf_index(client)
    assert s["index"]["gesamt"] == 1 and s["gesamt"] == 6


def test_themen_umstellen_ordnet_neu_ein(client, zustand):
    client.post("/api/index/start")
    warte_auf_index(client)
    themen = zustand.e["themen"]
    for t in themen:
        if t["name"] == "Strand":
            t["kategorie"] = "Natur"
    client.post("/api/einstellungen", json={
        "kategorien": [{"name": "Arbeit"}, {"name": "Privat"}, {"name": "Natur"}],
        "themen": themen, "arbeitszeit": {"aktiv": False}})
    client.post("/api/einordnen")
    assert bilder(client)["b.jpg"]["kategorie"] == "Natur"
    uebersicht = client.get("/api/themen").json()
    strand = next(t for t in uebersicht["themen"] if t["name"] == "Strand")
    assert strand["anzahl"] == 1 and len(strand["beispiele"]) == 1


def test_personen(client, zustand):
    client.post("/api/index/start")
    warte_auf_index(client)
    p = client.get("/api/personen").json()["personen"]
    assert len(p) == 1 and p[0]["bilder"] == 2
    client.post(f"/api/personen/{p[0]['id']}/name", json={"name": "Anna"})
    assert set(bilder(client, q="anna", art="text")) == {"c.jpg", "d.jpg"}
    assert set(bilder(client, person=p[0]["id"])) == {"c.jpg", "d.jpg"}
    assert client.get(f"/api/gesicht/{client.get('/api/personen').json()['personen'][0]['titel_gesicht']}").status_code == 200


def test_personensuche_mit_referenzbild(client, zustand):
    import io
    from PIL import Image
    client.post("/api/index/start")
    warte_auf_index(client)
    puffer = io.BytesIO()
    Image.new("RGB", (50, 50), (10, 10, 240)).save(puffer, "JPEG")
    r = client.post("/api/personensuche", files={"datei": ("ref.jpg", puffer.getvalue(), "image/jpeg")}).json()
    assert {Path(b["pfad"]).name for b in r["bilder"]} == {"c.jpg", "d.jpg"}


def test_sortieren_kopieren_ohne_umbenennen_und_rueckgaengig(client, zustand, heim):
    client.post("/api/index/start")
    warte_auf_index(client)
    ziel = heim / "sortiert"
    einst = {"ziel": str(ziel), "modus": "kopieren", "umbenennen": False, "tags": "sidecar",
             "ordner": "{kategorie}/{datum:%Y}"}
    v = client.post("/api/sortieren/vorschau", json={"einstellungen": einst}).json()
    assert v["zaehler"]["ok"] == 5 and not ziel.exists()
    client.post("/api/sortieren/ausfuehren", json={"einstellungen": einst})
    for _ in range(100):
        if not client.get("/api/status").json()["sortieren"]["laeuft"]:
            break
        time.sleep(0.05)
    assert (ziel / "Arbeit" / "2024" / "a.jpg").exists()        # Name unverändert
    assert (ziel / "Arbeit" / "2024" / "a.jpg.xmp").exists()
    assert len(list((heim / "backup").glob("*.*"))) == 5          # Originale unberührt
    prot = client.get("/api/protokolle").json()["protokolle"][0]
    client.post(f"/api/protokolle/{prot['id']}/rueckgaengig", params={"echt": True})
    assert not list(ziel.rglob("*.jpg"))


def test_nur_taggen_mit_umbenennen_und_zurueck(client, zustand, heim):
    client.post("/api/index/start")
    warte_auf_index(client)
    einst = {"modus": "nur_taggen", "umbenennen": True, "tags": "keine",
             "name": "{datum:%Y-%m-%d_%H%M%S}_{kategorie}"}
    client.post("/api/sortieren/ausfuehren", json={"einstellungen": einst})
    for _ in range(100):
        if not client.get("/api/status").json()["sortieren"]["laeuft"]:
            break
        time.sleep(0.05)
    assert (heim / "backup" / "2024-03-12_101500_Arbeit.jpg").exists()
    # Index kennt den neuen Pfad
    assert "2024-03-12_101500_Arbeit.jpg" in bilder(client)
    prot = client.get("/api/protokolle").json()["protokolle"][0]
    client.post(f"/api/protokolle/{prot['id']}/rueckgaengig", params={"echt": True})
    assert (heim / "backup" / "a.jpg").exists()
    assert "a.jpg" in bilder(client)


def test_manuelle_kategorie(client, zustand):
    client.post("/api/index/start")
    warte_auf_index(client)
    b = bilder(client)["b.jpg"]
    client.post("/api/bilder/kategorie", json={"ids": [b["id"]], "kategorie": "Arbeit"})
    assert bilder(client)["b.jpg"]["kategorie"] == "Arbeit"
    client.post("/api/einordnen")  # automatische Neueinordnung überschreibt Handarbeit nicht
    assert bilder(client)["b.jpg"]["kategorie"] == "Arbeit"
    client.post("/api/bilder/kategorie", json={"ids": [b["id"]], "kategorie": None})
    assert bilder(client)["b.jpg"]["kategorie"] == "Privat"


def test_ui_wird_ausgeliefert(client):
    r = client.get("/")
    assert r.status_code == 200 and "ImgSortSearch" in r.text
