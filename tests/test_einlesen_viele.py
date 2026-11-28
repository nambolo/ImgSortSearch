"""Einlesen großer Bildbestände: hängende Dateien, Fortschritt, Anhalten und Fortsetzen."""
from __future__ import annotations

import os
import shutil
import threading

import pytest

from conftest import mache_bild
from imgsortsearch import indexer, metadata

EXIFTOOL = shutil.which("exiftool") is not None


def test_dateisuche_liefert_groesse_und_zeit(tmp_path):
    a = mache_bild(tmp_path / "a" / "x.jpg")
    mache_bild(tmp_path / ".versteckt" / "y.jpg")
    mache_bild(tmp_path / "a" / "._z.jpg")
    (tmp_path / "a" / "text.txt").write_text("kein Bild")
    erg = metadata.finde_bilder_mit_stat(tmp_path)
    assert [(p, g) for p, g, _ in erg] == [(a, a.stat().st_size)]
    assert abs(erg[0][2] - a.stat().st_mtime) < 0.01
    assert metadata.finde_bilder(tmp_path) == [a]


@pytest.mark.skipif(not EXIFTOOL or not hasattr(os, "mkfifo"), reason="braucht exiftool und mkfifo")
def test_haengende_datei_haelt_den_lauf_nicht_auf(zustand, heim, monkeypatch):
    """Eine Datei, deren Lesen nie fertig wird (Cloud-Datei, Netzlaufwerk), wird übersprungen.
    Früher blieb das Einlesen daran für immer stehen."""
    monkeypatch.setattr(metadata.ExiftoolLeser, "ZEITLIMIT", 2.0)
    monkeypatch.setattr(indexer, "METADATEN_STAPEL", 3)
    q = heim / "backup"
    for i in range(7):
        mache_bild(q / f"viele/IMG_2024010{i}_120000.jpg")
    os.mkfifo(q / "viele" / "IMG_20240105_999999.jpg")  # öffnen blockiert, bis jemand schreibt
    ix = zustand.indexer
    t = threading.Thread(target=ix.dateien_einlesen, daemon=True)
    t.start()
    t.join(timeout=60)
    assert not t.is_alive(), "Einlesen hängt"
    db = zustand.db
    assert db.eins("SELECT COUNT(*) AS n FROM bilder")["n"] == 5 + 7 + 1
    h = db.eins("SELECT * FROM bilder WHERE pfad LIKE '%999999%'")
    assert h["fehler"] and h["datum"].startswith("2024-01-05")
    assert any("reagiert nicht" in f for f in ix.status["fehler"])
    # wird bei der Analyse ausgelassen (das Öffnen bliebe genauso hängen)
    assert all("999999" not in r["pfad"] for r in ix._offen())
    assert ix.status["fertig"] == ix.status["gesamt"] == 13


def test_anhalten_beim_einlesen_und_fortsetzen(zustand, heim, monkeypatch):
    monkeypatch.setattr(indexer, "METADATEN_STAPEL", 5)
    monkeypatch.setattr(indexer, "METADATEN_THREADS", 2)
    q = heim / "backup"
    for i in range(40):
        mache_bild(q / f"viele/{i:03d}.jpg", "iPhone 13", "2023:05:01 10:00:00")
    ix = zustand.indexer
    alt = indexer.Indexer._metadaten_speichern
    gespeichert = []

    def speichern(self, infos):
        alt(self, infos)
        gespeichert.append(len(infos))
        if len(gespeichert) == 2:
            ix.stoppen()
    monkeypatch.setattr(indexer.Indexer, "_metadaten_speichern", speichern)
    ix.dateien_einlesen()
    n1 = zustand.db.eins("SELECT COUNT(*) AS n FROM bilder")["n"]
    # angefangene Stapel werden noch gespeichert, danach nichts Neues mehr
    assert 10 <= n1 <= 5 * 2 * 2 + 5 and n1 < 45
    ix._stop.clear()
    ix.dateien_einlesen()
    assert zustand.db.eins("SELECT COUNT(*) AS n FROM bilder")["n"] == 45
    assert ix.status["gesamt"] == 45 - n1  # nur der Rest wird gelesen
    r = zustand.db.eins("SELECT * FROM bilder WHERE pfad LIKE '%039.jpg'")
    assert r["kamera"] == "iPhone13" and r["datum"] == "2023-05-01 10:00:00"
    assert "von" in ix.status["meldung"] and "noch etwa" in ix.status["meldung"]


@pytest.mark.skipif(not EXIFTOOL, reason="braucht exiftool")
def test_exiftool_bleibt_gestartet(tmp_path):
    bilder = [mache_bild(tmp_path / f"{i}.jpg", "SM-A546B", "2024:03:12 10:15:00") for i in range(4)]
    leser = metadata.ExiftoolLeser()
    try:
        a = metadata.lies_metadaten(bilder[:2], leser=leser)
        proc = leser.proc
        b = metadata.lies_metadaten(bilder[2:] + [tmp_path / "fehlt.jpg"], leser=leser)
        assert leser.proc is proc  # derselbe Prozess, kein Neustart je Stapel
    finally:
        leser.schliessen()
    assert [i.modell for i in a + b[:2]] == ["SM-A546B"] * 4
    assert all(i.datum_quelle == "exif" for i in a + b[:2])
