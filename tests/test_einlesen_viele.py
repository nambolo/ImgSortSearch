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


# ---------------------------------------------------------------- große Indexe

def test_bildvektoren_zwischengespeichert_und_aktualisiert(zustand):
    import numpy as np
    from imgsortsearch.datenbank import vektor_zu_blob
    ix, db = zustand.indexer, zustand.db
    ix.dateien_einlesen()
    ix.analysieren()
    ids1, m1 = db.clip_matrix()
    ids2, m2 = db.clip_matrix()
    assert len(ids1) == 5 and np.array_equal(m1, m2)
    assert db.clip_matrix(halb=True)[1] is db.clip_matrix(halb=True)[1]  # aus dem Zwischenspeicher
    # ein Bild verschwindet: Matrix wird neu geladen
    eins = int(ids1[0])
    indexer.bilder_entfernen(db, [eins])
    assert eins not in db.clip_matrix()[0].tolist()
    # Ähnlichkeit blockweise wie mat @ v
    v = np.ones(m1.shape[1], dtype=np.float32)
    ids, sims = db.clip_aehnlichkeit(v)
    _, m = db.clip_matrix()
    assert np.allclose(sims, m @ v, atol=1e-3)
    # gemischte Vektorlängen (Modellwechsel läuft): nur die häufigste Länge
    with db.verbindung() as c:
        c.execute("UPDATE bilder SET clip=? WHERE id=?", (vektor_zu_blob(np.ones(7)), int(ids[0])))
    db.clip_vergessen()
    ids3, m3 = db.clip_matrix()
    assert m3.shape == (len(ids) - 1, m1.shape[1])


def test_personen_nur_neu_gruppieren_wenn_sich_etwas_geaendert_hat(zustand):
    from imgsortsearch import personen
    ix, db = zustand.indexer, zustand.db
    ix.dateien_einlesen()
    ix.analysieren()
    erst = personen.neu_gruppieren(db, zustand.e.werte, nur_wenn_geaendert=True)
    assert not erst.get("uebersprungen")
    assert personen.neu_gruppieren(db, zustand.e.werte, nur_wenn_geaendert=True).get("uebersprungen")
    # Korrektur durch den Nutzer (Name) zählt als Änderung
    pid = db.eins("SELECT person_id FROM gesichter WHERE person_id IS NOT NULL")["person_id"]
    personen.umbenennen(db, pid, "Anna")
    assert not personen.neu_gruppieren(db, zustand.e.werte, nur_wenn_geaendert=True).get("uebersprungen")
    # Knopf "Neu gruppieren" rechnet immer
    assert not personen.neu_gruppieren(db, zustand.e.werte).get("uebersprungen")


def test_nachbarn_wie_vollstaendig_sortiert():
    """Die schnelle Nachbarsuche liefert dieselben Nachbarn wie das komplette Sortieren je Zeile."""
    import numpy as np
    from imgsortsearch import personen
    rng = np.random.default_rng(5)
    m = rng.normal(size=(40, 16)).astype(np.float32)
    f = m[rng.integers(0, 40, 700)] + rng.normal(scale=0.5, size=(700, 16)).astype(np.float32)
    f /= np.linalg.norm(f, axis=1, keepdims=True)
    erg = personen._nachbarn(f, 0.5, k=5, block=64)
    sims = f @ f.T
    np.fill_diagonal(sims, -1)
    for i in range(len(f)):
        erwartet = [j for j in np.argsort(-sims[i])[:5] if sims[i, j] >= 0.5]
        assert sorted(j for j, _ in erg[i]) == sorted(int(j) for j in erwartet)
