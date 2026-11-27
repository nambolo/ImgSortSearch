"""Testhilfen: Beispielbilder und Ersatzmodelle (die echten Modelle werden nicht geladen)."""
import hashlib
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

D = 32


def _grad(x):
    g = int(x)
    m = int((x - g) * 60)
    return (g, m, round(((x - g) * 60 - m) * 60, 4))


def mache_bild(pfad: Path, modell=None, datum=None, gps=None, farbe=(200, 30, 30), groesse=(64, 48)):
    pfad.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", groesse, farbe)
    exif = Image.Exif()
    if modell:
        exif[271], exif[272] = "Samsung", modell
    if datum:
        exif.get_ifd(0x8769)[36867] = datum
    if gps:
        exif.get_ifd(0x8825).update({1: "N", 2: _grad(gps[0]), 3: "E", 4: _grad(gps[1])})
    img.save(pfad, exif=exif)
    return pfad


def _einheit(seed: str) -> np.ndarray:
    rng = np.random.default_rng(int(hashlib.md5(seed.encode()).hexdigest()[:8], 16))
    v = rng.normal(size=D).astype(np.float32)
    return v / np.linalg.norm(v)


class FakeClip:
    """Rot -> 'Whiteboard', Grün -> 'Strand', Blau -> 'Hund'. Texte mit diesen Wörtern zeigen dorthin."""
    kennung = "fake"
    BASIS = {"whiteboard": _einheit("whiteboard"), "strand": _einheit("strand"), "hund": _einheit("hund")}

    def _richtung(self, text: str) -> np.ndarray:
        t = text.lower()
        for wort, v in self.BASIS.items():
            if wort in t:
                return v
        return _einheit(t)

    def bilder(self, bilder):
        erg = []
        for b in bilder:
            r, g, bl = b.getpixel((0, 0))
            wort = "whiteboard" if r > 150 else "strand" if g > 150 else "hund"
            erg.append(self.BASIS[wort])
        return np.stack(erg)

    def texte(self, texte):
        return np.stack([self._richtung(t) for t in texte])


class FakeGesichter:
    def finde(self, img, max_kante=1600):
        # blaue Bilder enthalten "Person A"
        r, g, b = img.getpixel((0, 0))
        if b > 150:
            v = _einheit("person-a")
            return [{"x": 1, "y": 1, "w": 20, "h": 20, "score": 0.95, "vektor": v}]
        return []


class FakeOCR:
    def lesen(self, img):
        return "Rechnung Nr 4711 Müller"


class FakeBeschreiber:
    def beschreiben(self, bilder):
        return [("Ein Bild", "A picture") for _ in bilder]


@pytest.fixture
def heim(tmp_path, monkeypatch):
    monkeypatch.setenv("IMGSORTSEARCH_HOME", str(tmp_path / "daten"))
    return tmp_path


@pytest.fixture
def zustand(heim, monkeypatch):
    from imgsortsearch import indexer
    from imgsortsearch.server import Zustand
    z = Zustand()
    z.m._clip, z.m._gesichter, z.m._ocr, z.m._beschreiber = FakeClip(), FakeGesichter(), FakeOCR(), FakeBeschreiber()
    monkeypatch.setattr(indexer.Modelle, "clip_kennung", lambda self: "fake")
    q = heim / "backup"
    mache_bild(q / "a.jpg", "SM-A546B", "2024:03:12 10:15:00", farbe=(220, 20, 20))       # Di, Arbeitszeit, rot
    mache_bild(q / "b.jpg", "iPhone 13", "2024:03:16 14:00:00", farbe=(20, 220, 20))      # Sa, grün
    mache_bild(q / "c.jpg", "iPhone 13", "2024:03:17 11:00:00", farbe=(20, 20, 220))      # So, blau, Person
    mache_bild(q / "d.jpg", "iPhone 13", "2024:03:18 18:00:00", farbe=(20, 20, 230))      # Mo abends, Person
    mache_bild(q / "Screenshot_1.png", farbe=(230, 30, 30))
    z.e.aktualisieren({"quellen": [str(q)], "themen": [
        {"name": "Whiteboard", "prompt": "whiteboard", "kategorie": "Arbeit"},
        {"name": "Strand", "prompt": "strand", "kategorie": "Privat"},
        {"name": "Hund", "prompt": "hund", "kategorie": "Privat"},
    ]})
    return z
