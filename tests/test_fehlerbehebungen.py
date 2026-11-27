"""Tests zu behobenen Fehlern in Index, Suche, Personen, Modellen und Sortieren."""
import json
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from conftest import mache_bild
from imgsortsearch import indexer, sortieren
from imgsortsearch.klassifizierung import in_arbeitszeit, regel_trifft
from imgsortsearch.server import erstelle_app


def warte(c, schluessel="index", max_s=30):
    for _ in range(max_s * 20):
        s = c.get("/api/status").json()
        if not (s["index"]["laeuft"] if schluessel == "index" else s["sortieren"]["laeuft"]):
            return s
        time.sleep(0.05)
    raise AssertionError("läuft noch")


@pytest.fixture
def client(zustand):
    return TestClient(erstelle_app(zustand), headers={"X-ImgSortSearch": "1"})


def namen(c, **params):
    return {Path(b["pfad"]).name: b for b in c.get("/api/bilder", params=params).json()["bilder"]}


def indizieren(c):
    assert c.post("/api/index/start").json()["gestartet"]
    return warte(c)


# ------------------------------------------------------------------ Index

def test_aehnlicher_ordnername_wird_nicht_ausgetragen(client, zustand, heim):
    """D:\\Fotos2 liegt nicht in D:\\Fotos: Bilder dort dürfen nicht aus dem Index fliegen."""
    nachbar = mache_bild(heim / "backup2" / "x.jpg", "iPhone 13", "2024:03:16 14:00:00")
    zustand.e.aktualisieren({"quellen": [str(heim / "backup"), str(heim / "backup2")]})
    indizieren(client)
    assert "x.jpg" in namen(client)
    zustand.e.aktualisieren({"quellen": [str(heim / "backup")]})  # backup2 abgehängt
    indizieren(client)
    assert nachbar.exists() and "x.jpg" in namen(client)


def test_kopien_im_zielordner_innerhalb_der_quelle_nicht_doppelt(client, zustand, heim):
    indizieren(client)
    ziel = heim / "backup" / "Sortiert"
    einst = {"ziel": str(ziel), "modus": "kopieren", "tags": "keine", "ordner": "{kategorie}"}
    client.post("/api/sortieren/ausfuehren", json={"einstellungen": einst})
    warte(client, "sortieren")
    assert len(list(ziel.rglob("*.jpg"))) == 4
    s = indizieren(client)
    assert s["gesamt"] == 5 and s["index"]["gesamt"] == 0  # keine Kopien aufgenommen
    # neue echte Bilder in der Quelle werden aber weiter gefunden
    mache_bild(heim / "backup" / "neu.jpg", "iPhone 13", "2024:05:01 12:00:00")
    assert indizieren(client)["gesamt"] == 6


def test_fehlendes_zusatzmodell_bremst_spaetere_laeufe_nicht(client, zustand, monkeypatch):
    """Ohne Gesichtsmodell galt der Schritt nie als erledigt: jeder Lauf lud alle Bilder neu."""
    zustand.m._gesichter = None
    monkeypatch.setattr(indexer.Modelle, "gesichter", lambda self: None)
    s = indizieren(client)
    assert s["index"]["phase"] == "fertig" and s["offen"] == 0
    assert any("gesichter" in f for f in s["index"]["fehler"])
    assert indizieren(client)["index"]["gesamt"] == 0


def test_gesichtsbilder_werden_beim_austragen_geloescht(client, zustand, heim):
    indizieren(client)
    gid = client.get("/api/personen").json()["personen"][0]["titel_gesicht"]
    assert client.get(f"/api/gesicht/{gid}").status_code == 200
    cache = indexer.gesicht_cache_pfad(gid)
    assert cache.exists()
    bild_id = zustand.db.eins("SELECT bild_id FROM gesichter WHERE id=?", (gid,))["bild_id"]
    pfad = Path(zustand.db.eins("SELECT pfad FROM bilder WHERE id=?", (bild_id,))["pfad"])
    pfad.unlink()
    indizieren(client)
    assert not cache.exists() and not indexer.vorschau_pfad(bild_id).exists()


def test_bildmodell_wechsel_berechnet_vektoren_neu(client, zustand, monkeypatch):
    indizieren(client)
    assert indizieren(client)["index"]["gesamt"] == 0
    monkeypatch.setattr(indexer.Modelle, "clip_kennung", lambda self: "anderes-modell")
    assert indizieren(client)["index"]["gesamt"] == 5


def test_modell_wird_nach_einstellungsaenderung_neu_geladen(heim, monkeypatch):
    from imgsortsearch.einstellungen import Einstellungen
    geladen = []

    class Clip:
        def __init__(self, modell, pretrained, beschl):
            geladen.append((modell, beschl))

    monkeypatch.setattr(indexer.modelle, "ClipModell", Clip)
    m = indexer.Modelle(Einstellungen())
    a = m.clip()
    assert m.clip() is a and len(geladen) == 1
    m.e.aktualisieren({"beschleunigung": "cpu"})
    assert m.clip_falls_geladen() is None  # altes Modell nicht mehr für Textvektoren nutzen
    assert m.clip() is not a and geladen[-1][1] == "cpu"


def test_einordnen_laedt_fehlende_themenvektoren(client, zustand, monkeypatch):
    """Nach einem Neustart ist das Bildmodell nicht geladen. Fehlten die Themen-Vektoren im
    Zwischenspeicher, wurde der Bildinhalt beim Einordnen still ignoriert."""
    indizieren(client)
    fake = zustand.m._clip
    zustand.m._clip = None
    monkeypatch.setattr(indexer.Modelle, "clip", lambda self: fake)
    with zustand.db.verbindung() as v:
        v.execute("DELETE FROM textvektoren")
    indexer.alle_neu_einordnen(zustand.db, zustand.e, zustand.m)
    assert namen(client)["b.jpg"]["kategorie"] == "Privat"  # nur mit Bildinhalt (Strand) eindeutig


# ------------------------------------------------------------------ Suche

def test_unterstrich_ist_kein_platzhalter(client, zustand, heim):
    mache_bild(heim / "backup" / "IMGx2023.jpg", "iPhone 13", "2024:03:16 14:00:00")
    mache_bild(heim / "backup" / "IMG_2023.jpg", "iPhone 13", "2024:03:16 14:00:00")
    indizieren(client)
    assert set(namen(client, q="img_2023", art="text")) == {"IMG_2023.jpg"}
    assert namen(client, q="100%", art="text") == {}


def test_umbenannte_datei_ist_unter_neuem_namen_suchbar(client, zustand, heim):
    indizieren(client)
    einst = {"modus": "nur_taggen", "umbenennen": True, "tags": "keine", "name": "{datum:%Y%m%d}_neu"}
    client.post("/api/sortieren/ausfuehren", json={"einstellungen": einst})
    warte(client, "sortieren")
    assert "20240312_neu.jpg" in namen(client, q="20240312_neu", art="text")
    assert "20240312_neu.jpg" not in namen(client, q="a.jpg", art="text")


# ------------------------------------------------------------------ Personen

def test_zusammenfuehren_und_entfernen_aktualisieren_suche(client, zustand):
    indizieren(client)
    pid = client.get("/api/personen").json()["personen"][0]["id"]
    client.post(f"/api/personen/{pid}/name", json={"name": "Anna"})
    gesichter = client.get(f"/api/personen/{pid}/gesichter").json()["gesichter"]
    # "Nicht diese Person": Bild darf nicht mehr unter "Anna" kommen
    client.post(f"/api/gesicht/{gesichter[0]['id']}/entfernen")
    assert len(namen(client, q="anna", art="text")) == 1
    assert zustand.db.eins("SELECT person_id FROM gesichter WHERE id=?", (gesichter[0]["id"],))["person_id"] is None
    # Neu gruppieren ordnet es Anna nicht wieder zu, obwohl es gleich aussieht
    client.post("/api/personen/neu-gruppieren")
    assert len(namen(client, q="anna", art="text")) == 1
    # wieder von Hand zuweisen: beide Bilder unter "Anna"
    client.post("/api/gesichter/zuweisen", json={"gesichter": [gesichter[0]["id"]], "person": pid})
    assert set(namen(client, q="anna", art="text")) == {"c.jpg", "d.jpg"}


# ------------------------------------------------------------------ Einordnen

def test_arbeitszeit_ueber_mitternacht():
    az = {"wochentage": [1, 2, 3, 4, 5], "von": "22:00", "bis": "06:00"}
    assert in_arbeitszeit(datetime(2024, 3, 12, 23, 0), az)       # Di 23 Uhr
    assert in_arbeitszeit(datetime(2024, 3, 16, 2, 0), az)        # Sa 2 Uhr = Freitagsschicht
    assert not in_arbeitszeit(datetime(2024, 3, 18, 2, 0), az)    # Mo 2 Uhr = Sonntagnacht
    assert not in_arbeitszeit(datetime(2024, 3, 12, 12, 0), az)
    z = {"datum": "2024-03-12 23:30:00", "datum_quelle": "exif"}
    assert regel_trifft({"uhrzeit": "22:00-06:00"}, z)


def test_ungueltige_regex_bricht_nicht_ab():
    assert not regel_trifft({"dateiname_regex": "IMG_(["}, {"pfad": "IMG_1.jpg"})


# ------------------------------------------------------------------ Sortieren

def test_verschieben_nimmt_vorhandene_xmp_mit_und_zurueck(tmp_path):
    quelle = mache_bild(tmp_path / "q" / "a.jpg", "iPhone 13", "2024:03:12 10:15:00")
    alt_xmp = quelle.with_name("a.jpg.xmp")
    alt_xmp.write_text("<lightroom/>", "utf-8")
    raw = tmp_path / "q" / "b.cr2"
    raw.write_bytes(b"raw")
    raw.with_suffix(".xmp").write_text("<raw-bearbeitung/>", "utf-8")
    auftraege = [sortieren.Auftrag(pfad=p, kategorie="Arbeit", datum=datetime(2024, 3, 12), kamera="",
                                   tags=[], personen=[], bild_id=i) for i, p in ((1, quelle), (2, raw))]
    s = {"ziel": str(tmp_path / "z"), "modus": "verschieben", "tags": "sidecar", "ordner": "{kategorie}"}
    erg = sortieren.ausfuehren(auftraege, s, tmp_path / "prot", echt=True)
    assert erg["zaehler"]["ok"] == 2
    assert (tmp_path / "z" / "Arbeit" / "a.jpg.xmp").read_text("utf-8") == "<lightroom/>"
    assert (tmp_path / "z" / "Arbeit" / "b.xmp").read_text("utf-8") == "<raw-bearbeitung/>"
    assert not alt_xmp.exists()
    sortieren.rueckgaengig(Path(erg["protokoll"]), echt=True)
    assert quelle.exists() and alt_xmp.read_text("utf-8") == "<lightroom/>"
    assert raw.with_suffix(".xmp").exists()


def test_ungueltige_vorlage_gibt_verstaendlichen_fehler(client, zustand):
    indizieren(client)
    r = client.post("/api/sortieren/vorschau", json={"einstellungen": {
        "ziel": "x", "modus": "kopieren", "ordner": "{kategorie}/{monat}"}})
    assert r.status_code == 400 and "{monat}" in r.json()["detail"]


def test_exiftool_argumente():
    a = sortieren.Auftrag(pfad=Path("x.jpg"), kategorie="Arbeit", datum=None, kamera="", tags=["Büro"],
                          personen=["Jürgen"], beschreibung="Zeile1\nZeile2")
    args = sortieren._einbetten_argumente(Path("C:/Bilder/Jürgen.jpg"), a, sicherung=False)
    assert all("\n" not in x for x in args)
    i_weg = args.index("-XMP-lr:HierarchicalSubject-=Personen|Jürgen")
    assert args[i_weg + 1] == "-XMP-lr:HierarchicalSubject+=Personen|Jürgen"
    assert "filename=utf8" in args and args[-1].endswith("Jürgen.jpg")


# ------------------------------------------------------------------ Bilder & Einstellungen

def test_transparente_png_wird_weiss_hinterlegt(tmp_path):
    from imgsortsearch.modelle import bild_laden
    p = tmp_path / "s.png"
    Image.new("RGBA", (10, 10), (0, 0, 0, 0)).save(p)
    assert bild_laden(p).getpixel((5, 5)) == (255, 255, 255)
    p16 = tmp_path / "g.png"
    Image.fromarray(np.full((4, 4), 30000, dtype=np.uint16)).save(p16)
    assert bild_laden(p16).getpixel((0, 0))[0] == 255


def test_kaputte_einstellungen_werden_gesichert(heim):
    from imgsortsearch.einstellungen import Einstellungen
    datei = heim / "e.json"
    datei.write_text('{"quellen": ["D:/Fotos"],', "utf-8")
    e = Einstellungen(datei)
    assert e["quellen"] == []
    assert datei.with_name("e.json.defekt").read_text("utf-8").startswith('{"quellen"')
    e.aktualisieren({"port": 1})
    assert json.loads(datei.read_text("utf-8"))["port"] == 1


def test_exif_wird_gelesen_solange_datei_offen(tmp_path, monkeypatch):
    """Windows/TIFF: get_ifd() las nach dem Schließen nach -> 'seek of closed file' brach den Lauf ab."""
    from PIL import Image as PILImage
    from imgsortsearch import metadata
    p = mache_bild(tmp_path / "a.jpg", "SM-A546B", "2024:03:12 10:15:00")
    offen = {"ja": False}
    echt_open, echt_get_ifd = PILImage.open, PILImage.Exif.get_ifd

    class Wrapper:
        def __init__(self, img):
            self.img = img

        def __enter__(self):
            offen["ja"] = True
            return self.img

        def __exit__(self, *a):
            offen["ja"] = False
            self.img.close()

    def get_ifd(self, tag):
        if not offen["ja"]:
            raise ValueError("seek of closed file")
        return echt_get_ifd(self, tag)

    monkeypatch.setattr(PILImage, "open", lambda f: Wrapper(echt_open(f)))
    monkeypatch.setattr(PILImage.Exif, "get_ifd", get_ifd)
    info = metadata.lies_metadaten([p], exiftool=False)[0]
    assert info.datum == datetime(2024, 3, 12, 10, 15) and info.datum_quelle == "exif"


def test_fehler_bei_einem_bild_bricht_metadaten_nicht_ab(tmp_path, monkeypatch):
    from imgsortsearch import metadata
    a = mache_bild(tmp_path / "IMG_20230105_081500.jpg")
    b = mache_bild(tmp_path / "b.jpg", "iPhone 13", "2024:03:16 14:00:00")

    def kaputt(info):
        if info.pfad == a:
            raise ValueError("seek of closed file")
        return echt(info)
    echt = metadata._aus_pillow
    monkeypatch.setattr(metadata, "_aus_pillow", kaputt)
    infos = metadata.lies_metadaten([a, b], exiftool=False)
    assert infos[0].datum == datetime(2023, 1, 5, 8, 15) and infos[1].datum_quelle == "exif"
