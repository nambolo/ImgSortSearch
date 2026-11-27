from datetime import datetime
from pathlib import Path

import numpy as np
import pytest

from conftest import mache_bild
from imgsortsearch.datenbank import normalisiere
from imgsortsearch.klassifizierung import Themen, einordnen
from imgsortsearch.metadata import datum_aus_dateiname, lies_metadaten
from imgsortsearch.einstellungen import STANDARD
from imgsortsearch import sortieren


def test_datum_aus_dateiname():
    assert datum_aus_dateiname("IMG_20230105_081500.jpg") == datetime(2023, 1, 5, 8, 15)
    assert datum_aus_dateiname("IMG-20230105-WA0001.jpg") == datetime(2023, 1, 5)
    assert datum_aus_dateiname("2022-07-01 18.30.05.jpg") == datetime(2022, 7, 1, 18, 30, 5)
    assert datum_aus_dateiname("urlaub.jpg") is None


def test_metadaten_pillow(tmp_path):
    p = mache_bild(tmp_path / "a.jpg", "SM-A546B", "2024:03:12 10:15:00", gps=(48.1372, 11.5762))
    info = lies_metadaten([p], exiftool=False)[0]
    assert info.datum == datetime(2024, 3, 12, 10, 15) and info.datum_quelle == "exif"
    assert info.kamera == "SMA546B"
    assert info.gps_lat == pytest.approx(48.1372, abs=1e-4)


def test_normalisiere():
    assert normalisiere("Größe MÜLLER") == "grosse muller"


def test_einordnen_zeit_und_inhalt():
    e = dict(STANDARD)
    z = {"datum": "2024-03-12 10:15:00", "datum_quelle": "exif"}  # Dienstag
    nur_zeit = einordnen(z, None, e)
    assert nur_zeit["kategorie"] == "Arbeit"
    inhalt = {"kategorien": {"Arbeit": 0.1, "Privat": 0.9}, "tags": ["Strand"]}
    assert einordnen(z, inhalt, e)["kategorie"] in ("Privat", "Unklar")
    sonntag = {"datum": "2024-03-17 10:15:00", "datum_quelle": "exif"}
    assert einordnen(sonntag, inhalt, e)["kategorie"] == "Privat"
    # Dateidatum zählt nicht als Aufnahmezeit
    assert einordnen({"datum": "2024-03-12 10:15:00", "datum_quelle": "dateidatum"}, None, e)["kategorie"] == "Unklar"


def test_eigene_regel_und_kategorie():
    e = dict(STANDARD, kategorien=[{"name": "Arbeit"}, {"name": "Privat"}, {"name": "Personen"}],
             regeln=[{"name": "Gesichter", "kategorie": "Personen", "gewicht": 3, "mit_gesichtern": True}])
    z = {"datum": None, "anzahl_gesichter": 2}
    assert einordnen(z, None, e)["kategorie"] == "Personen"


def test_themen_auswerten():
    v = np.eye(3, dtype=np.float32)
    t = Themen([{"name": "A", "kategorie": "Arbeit"}, {"name": "B", "kategorie": "Privat"},
                {"name": "C", "kategorie": ""}], v)
    erg = t.auswerten(v)
    assert erg[0]["tags"][0] == "A" and erg[0]["kategorien"]["Arbeit"] > 0.99
    assert erg[2]["kategorien"] == {"Arbeit": pytest.approx(0, abs=1e-6), "Privat": pytest.approx(0, abs=1e-6)}


def test_ziel_relativ_ohne_und_mit_umbenennen():
    a = sortieren.Auftrag(Path("/x/IMG_1.JPG"), "Arbeit", datetime(2024, 3, 12, 10, 15), "iPhone15", [], [])
    s = {"ordner": "{kategorie}/{datum:%Y}", "name": "{datum:%Y-%m-%d_%H%M%S}_{kamera}", "modus": "kopieren"}
    assert sortieren.ziel_relativ(a, dict(s, umbenennen=False)) == Path("Arbeit/2024/IMG_1.JPG")
    assert sortieren.ziel_relativ(a, dict(s, umbenennen=True)) == Path("Arbeit/2024/2024-03-12_101500_iPhone15.jpg")
    ohne = sortieren.Auftrag(Path("/x/a.jpg"), "Privat", None, "", [], [])
    assert sortieren.ziel_relativ(ohne, dict(s, umbenennen=True)) == Path("Privat/ohne-Datum/a.jpg")


def test_xmp_gueltig():
    import xml.dom.minidom
    a = sortieren.Auftrag(Path("a.jpg"), "Arbeit", None, "", ["A&B"], ["Anna <Test>"], "Ein \"Bild\"")
    xml.dom.minidom.parseString(sortieren.xmp_sidecar(a).encode())
