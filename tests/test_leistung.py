"""Indexlauf-Tempo: schnelles JPEG-Verkleinern, Fließband, Zeitmessung, Gesichter auf der Grafikkarte."""
from __future__ import annotations

import logging
import sys
import types

import numpy as np
from PIL import Image

from imgsortsearch import indexer, modelle


def test_bild_laden_dekodiert_verkleinert(tmp_path, monkeypatch):
    p = tmp_path / "quer.jpg"
    Image.new("RGB", (4000, 3000), (10, 120, 200)).save(p, quality=80)
    from PIL import JpegImagePlugin
    gesehen = {}
    alt = JpegImagePlugin.JpegImageFile.draft

    def draft(self, mode, size):
        erg = alt(self, mode, size)
        gesehen["groesse"] = self.size  # Größe, die wirklich dekodiert wird
        return erg
    monkeypatch.setattr(JpegImagePlugin.JpegImageFile, "draft", draft)
    img = modelle.bild_laden(p, max_kante=2000)
    assert gesehen["groesse"] == (2000, 1500)   # halbe Größe direkt aus dem JPEG, nicht 4000x3000
    assert img.size == (2000, 1500)


def test_bild_laden_klein_bleibt(tmp_path):
    p = tmp_path / "klein.jpg"
    Image.new("RGB", (800, 600), "white").save(p)
    assert modelle.bild_laden(p, max_kante=2000).size == (800, 600)


def test_zeitmessung_protokolliert(caplog):
    z = indexer.Zeitmessung(alle_sekunden=0)
    with z.messen("laden"):
        pass
    z.hinzu("clip", 0.5, 16)
    with caplog.at_level(logging.INFO, logger="imgsortsearch.indexer"):
        z.stapel_fertig(16)
    assert "Analyse-Zeiten" in caplog.text and "clip" in caplog.text and "laden" in caplog.text
    assert "31 ms je Bild" in caplog.text  # 0,5 s / 16 Bilder


def test_fliessband_mit_kaputtem_bild(zustand, heim, monkeypatch):
    """Alle Bilder werden analysiert, ein kaputtes wird markiert, der Fortschritt endet bei 100 %."""
    monkeypatch.setattr(indexer, "LADE_THREADS", 3)
    q = heim / "backup"
    (q / "kaputt.jpg").write_bytes(b"kein bild")
    for i in range(40):
        from conftest import mache_bild
        mache_bild(q / f"viele/{i}.jpg", farbe=(20, 20 + i, 220))
    zustand.e.aktualisieren({"clip": {"stapel": 4}})
    ix = zustand.indexer
    ix.dateien_einlesen()
    ix.analysieren()
    db = zustand.db
    assert db.eins("SELECT COUNT(*) n FROM bilder WHERE schritt_clip=0 AND fehler IS NULL")["n"] == 0
    assert db.eins("SELECT COUNT(*) n FROM bilder WHERE fehler IS NOT NULL")["n"] == 1
    assert ix.status["fertig"] == ix.status["gesamt"]


def test_fliessband_anhalten(zustand, heim):
    ix = zustand.indexer
    ix.dateien_einlesen()
    ix._stop.set()
    ix.analysieren()   # darf nicht hängen bleiben
    assert zustand.db.eins("SELECT COUNT(*) n FROM bilder WHERE schritt_clip=1")["n"] == 0


# ------------------------------------------------------------------ Gesichter mit DirectML

class _Sitzung:
    def __init__(self, anbieter, kaputt=False, abweichung=0.0):
        self.anbieter = [a[0] if isinstance(a, tuple) else a for a in anbieter]
        self.kaputt, self.abweichung = kaputt, abweichung

    def get_providers(self):
        return self.anbieter

    def get_inputs(self):
        return [types.SimpleNamespace(name="x")]

    def run(self, _, eingaben):
        if self.kaputt and self.anbieter[0] == "DmlExecutionProvider":
            raise RuntimeError("DML kaputt")
        x = eingaben["x"]
        aus = np.full((x.shape[0], 4), float(x.mean()), dtype=np.float32)
        if self.anbieter[0] == "DmlExecutionProvider":
            aus = aus + self.abweichung
        return [aus]


def _fake_ort(monkeypatch, dml=True, kaputt_ab=None, abweichung=0.0):
    zaehler = {"n": 0}

    def sitzung(pfad, opt, providers):
        zaehler["n"] += 1
        kaputt = kaputt_ab is not None and zaehler["n"] >= kaputt_ab
        return _Sitzung(providers, kaputt=kaputt, abweichung=abweichung)
    ort = types.SimpleNamespace(
        get_available_providers=lambda: (["DmlExecutionProvider"] if dml else []) + ["CPUExecutionProvider"],
        SessionOptions=lambda: types.SimpleNamespace(),
        ExecutionMode=types.SimpleNamespace(ORT_SEQUENTIAL=0),
        InferenceSession=sitzung)
    monkeypatch.setitem(sys.modules, "onnxruntime", ort)
    monkeypatch.setattr(modelle, "gesichtsmodelle_laden", lambda: ("det.onnx", "rec.onnx"))


def test_gesichter_auf_dml(monkeypatch):
    _fake_ort(monkeypatch)
    g = modelle.GesichtsModell(beschleunigung="auto")
    assert g.geraet_name.startswith("DirectML")
    assert g.det.get_providers()[0] == "DmlExecutionProvider"


def test_gesichter_cpu_wenn_gewuenscht_oder_ohne_dml(monkeypatch):
    _fake_ort(monkeypatch)
    assert modelle.GesichtsModell(beschleunigung="cpu").det.get_providers() == ["CPUExecutionProvider"]
    _fake_ort(monkeypatch, dml=False)
    assert modelle.GesichtsModell(beschleunigung="auto").geraet_name.startswith("CPU")


def test_gesichter_dml_rechnet_falsch_bleibt_cpu(monkeypatch):
    _fake_ort(monkeypatch, abweichung=1.0)
    g = modelle.GesichtsModell(beschleunigung="auto")
    assert g.geraet_name.startswith("CPU")
    assert g.det.get_providers() == ["CPUExecutionProvider"]


def test_gesichter_dml_faellt_spaeter_aus(monkeypatch):
    _fake_ort(monkeypatch)
    g = modelle.GesichtsModell(beschleunigung="auto")
    g.det.kaputt = True
    aus = g._ausfuehren("det", np.zeros((1, 3, 8, 8), np.float32))
    assert aus[0].shape == (1, 4)
    assert g.geraet_name.startswith("CPU (Grafikkarte fehlgeschlagen")
