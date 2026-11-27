"""Bugsuche Personen / Sortieren / Dubletten (Ende November 2026)."""
import json
from pathlib import Path

import numpy as np
import pytest

from imgsortsearch import dubletten, personen, sortieren
from imgsortsearch.datenbank import Datenbank

from test_gesichter import _gesicht, _person


@pytest.fixture
def db(tmp_path):
    return Datenbank(tmp_path / "t.sqlite")


def _pid(db, gid):
    return db.eins("SELECT person_id FROM gesichter WHERE id=?", (gid,))["person_id"]


def test_abgelehnte_gruppe_kommt_nicht_als_ganze_gruppe_zurueck(db):
    """'Nicht diese Person' für mehrere Gesichter: sie bilden eine eigene Gruppe, die der Person
    ähnelt. Der Schritt 'ganze Gruppe passt zur Person' darf sie nicht wieder zurückholen."""
    rng = np.random.default_rng(11)
    a = _person(rng, 30, streuung=0.6)
    with db.verbindung() as v:
        pid = v.execute("INSERT INTO personen (name, fest) VALUES ('Anna', 1)").lastrowid
        anna = [_gesicht(v, db, x, person=pid, fest=1) for x in a[:20]]
        rest = [_gesicht(v, db, x) for x in a[20:]]
    personen.neu_gruppieren(db)
    assert all(_pid(db, g) == pid for g in rest), "Testaufbau"
    personen.nicht_diese_person(db, rest)
    personen.neu_gruppieren(db)
    assert all(_pid(db, g) != pid for g in rest)


def test_umbenennen_auf_namen_mit_anderer_schreibweise_fuehrt_zusammen(db):
    with db.verbindung() as v:
        a = v.execute("INSERT INTO personen (name) VALUES ('Marlena')").lastrowid
        b = v.execute("INSERT INTO personen (name) VALUES (NULL)").lastrowid
        _gesicht(v, db, np.ones(512) / np.sqrt(512), person=a)
        g = _gesicht(v, db, np.ones(512) / np.sqrt(512), person=b)
    assert personen.umbenennen(db, b, "marlena ") == a
    assert _pid(db, g) == a
    # Zuweisen mit Namen ebenso
    neu = personen.zuweisen(db, [g], name="MARLENA")
    assert neu == a
    assert len([p for p in personen.personen_liste(db, 1) if p["name"]]) == 1


def test_dubletten_zwei_laeufe_in_derselben_sekunde(tmp_path, monkeypatch):
    db = Datenbank(tmp_path / "t.sqlite")
    q = tmp_path / "fotos"
    q.mkdir()
    ids = []
    with db.verbindung() as v:
        for n in "ab":
            (q / f"{n}.jpg").write_bytes(n.encode() * 100)
            ids.append(v.execute("INSERT INTO bilder (pfad) VALUES (?)", (str(q / f"{n}.jpg"),)).lastrowid)
    qu = tmp_path / "quarantaene"
    prot = tmp_path / "prot"

    class Fest(dubletten.datetime):
        @classmethod
        def now(cls, tz=None):
            return dubletten.datetime(2026, 11, 27, 18, 0, 0)
    monkeypatch.setattr(dubletten, "datetime", Fest)
    e1 = dubletten.bereinigen(db, [ids[0]], set(), "quarantaene", qu, prot)
    e2 = dubletten.bereinigen(db, [ids[1]], set(), "quarantaene", qu, prot)
    assert e1["protokoll"] != e2["protokoll"]
    # Rückgängig des ersten Laufs holt nur dessen Datei zurück
    sortieren.rueckgaengig(Path(e1["protokoll"]), echt=True)
    assert (q / "a.jpg").exists() and not (q / "b.jpg").exists()


def test_quarantaene_ueberschreibt_nie(tmp_path, monkeypatch):
    """Gleicher Pfad zweimal aussortiert (dazwischen rückgängig + Datei neu) im selben Lauf-Ordner."""
    db = Datenbank(tmp_path / "t.sqlite")
    q = tmp_path / "fotos"
    q.mkdir()
    (q / "a.jpg").write_bytes(b"eins")
    with db.verbindung() as v:
        i = v.execute("INSERT INTO bilder (pfad) VALUES (?)", (str(q / "a.jpg"),)).lastrowid

    class Fest(dubletten.datetime):
        @classmethod
        def now(cls, tz=None):
            return dubletten.datetime(2026, 11, 27, 18, 0, 0)
    monkeypatch.setattr(dubletten, "datetime", Fest)
    qu = tmp_path / "quarantaene"
    dubletten.bereinigen(db, [i], set(), "quarantaene", qu, tmp_path / "p")
    (q / "a.jpg").write_bytes(b"zwei")
    dubletten.bereinigen(db, [i], set(), "quarantaene", qu, tmp_path / "p")
    inhalte = sorted(p.read_bytes() for p in qu.rglob("a*.jpg"))
    assert inhalte == [b"eins", b"zwei"]


def test_kopieren_nimmt_vorhandene_xmp_mit(tmp_path):
    q = tmp_path / "q"
    q.mkdir()
    (q / "IMG.jpg").write_bytes(b"bild")
    (q / "IMG.jpg.xmp").write_text("<lightroom/>", "utf-8")
    a = sortieren.Auftrag(pfad=q / "IMG.jpg", kategorie="Privat", datum=None, kamera="", tags=[], personen=[])
    erg = sortieren.ausfuehren([a], {"modus": "kopieren", "ziel": str(tmp_path / "z"), "tags": "keine"},
                               tmp_path / "p", echt=True)
    assert (tmp_path / "z" / "Privat" / "IMG.jpg.xmp").read_text("utf-8") == "<lightroom/>"
    assert (q / "IMG.jpg.xmp").exists()
    sortieren.rueckgaengig(Path(erg["protokoll"]), echt=True)
    assert not (tmp_path / "z").exists()
    assert (q / "IMG.jpg.xmp").read_text("utf-8") == "<lightroom/>"


@pytest.mark.skipif(not sortieren.shutil.which("exiftool"), reason="exiftool fehlt")
def test_kopieren_mit_xmp_ergaenzt_tags_in_der_kopie(tmp_path):
    q = tmp_path / "q"
    q.mkdir()
    (q / "IMG.jpg").write_bytes(b"bild")
    lr = ('<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
          '<rdf:Description rdf:about="" xmlns:crs="http://ns.adobe.com/camera-raw-settings/1.0/" '
          'crs:Exposure2012="+0.50"/></rdf:RDF></x:xmpmeta>')
    (q / "IMG.jpg.xmp").write_text(lr, "utf-8")
    a = sortieren.Auftrag(pfad=q / "IMG.jpg", kategorie="Privat", datum=None, kamera="", tags=["Strand"],
                          personen=["Anna"])
    sortieren.ausfuehren([a], {"modus": "kopieren", "ziel": str(tmp_path / "z"), "tags": "sidecar"},
                         tmp_path / "p", echt=True)
    kopie = (tmp_path / "z" / "Privat" / "IMG.jpg.xmp").read_text("utf-8")
    assert "Exposure2012" in kopie and "Strand" in kopie and "Anna" in kopie
    assert (q / "IMG.jpg.xmp").read_text("utf-8") == lr  # Original unverändert


@pytest.mark.parametrize("mit_exiftool", [False, True])
def test_gps_ohne_empfang_legt_karte_nicht_lahm(zustand, heim, monkeypatch, mit_exiftool):
    """Kameras ohne GPS-Empfang schreiben 0/0 als Bruch: NaN darf nicht in den Index (Karte = Fehler 500)."""
    from fastapi.testclient import TestClient
    from PIL import Image
    from PIL.TiffImagePlugin import IFDRational as R
    from imgsortsearch.metadata import _gps_gueltig, _gps_zu_grad
    from imgsortsearch.server import erstelle_app
    assert _gps_zu_grad((R(0, 0), R(0, 0), R(0, 0)), "N") is None
    assert abs(_gps_zu_grad((R(48, 1), R(8, 1), R(0, 0)), "N") - 48.1333) < 1e-3
    assert not _gps_gueltig(float("nan"), 11.0) and not _gps_gueltig(95.0, 11.0)
    img = Image.new("RGB", (64, 48), (20, 220, 20))
    exif = Image.Exif()
    exif.get_ifd(0x8825).update({1: "N", 2: (R(0, 0), R(0, 0), R(0, 0)), 3: "E", 4: (R(0, 0), R(0, 0), R(0, 0))})
    img.save(heim / "backup" / "nan.jpg", exif=exif)
    # Sekunden als 0/0, Grad und Minuten stimmen: Standort soll erhalten bleiben
    exif = Image.Exif()
    exif.get_ifd(0x8825).update({1: "N", 2: (R(48, 1), R(8, 1), R(0, 0)), 3: "E", 4: (R(11, 1), R(34, 1), R(0, 0))})
    img.save(heim / "backup" / "halb.jpg", exif=exif)
    from imgsortsearch import metadata
    if not mit_exiftool:
        monkeypatch.setattr(metadata, "exiftool_verfuegbar", lambda: False)
    elif not metadata.exiftool_verfuegbar():
        pytest.skip("exiftool fehlt")
    c = TestClient(erstelle_app(zustand), headers={"x-imgsortsearch": "1"})
    c.post("/api/index/start")
    import time
    for _ in range(300):
        if not c.get("/api/status").json()["index"]["laeuft"]:
            break
        time.sleep(0.1)
    r = c.get("/api/karte/punkte")
    assert r.status_code == 200, r.text
    assert len(r.json()["punkte"]) == 1
    _, lat, lon, _ = r.json()["punkte"][0]
    assert abs(lat - 48.1333) < 1e-3 and abs(lon - 11.5667) < 1e-3


def test_dubletten_behalten_muss_noch_da_sein(zustand, heim):
    """Veraltetes Ergebnis: das Bild zum Behalten gibt es nicht mehr -> nichts aussortieren."""
    from fastapi.testclient import TestClient
    from imgsortsearch.server import erstelle_app
    c = TestClient(erstelle_app(zustand), headers={"x-imgsortsearch": "1"})
    q = heim / "backup"
    with zustand.db.verbindung() as v:
        weg = v.execute("INSERT INTO bilder (pfad) VALUES (?)", (str(q / "a.jpg"),)).lastrowid
        fehlt = v.execute("INSERT INTO bilder (pfad) VALUES (?)", (str(q / "gibts-nicht.jpg"),)).lastrowid
    r = c.post("/api/dubletten/bereinigen", json={"quarantaene": str(heim / "qu"),
                                                   "gruppen": [{"behalten": [fehlt], "weg": [weg]}]})
    assert r.status_code == 400
    assert (q / "a.jpg").exists()
    r = c.post("/api/dubletten/bereinigen", json={"quarantaene": str(heim / "qu"),
                                                   "gruppen": [{"behalten": [99999], "weg": [weg]}]})
    assert r.status_code == 400 and (q / "a.jpg").exists()
