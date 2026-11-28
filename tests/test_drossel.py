"""Rechner schonen (Drosselung) und Wahl der Grafikkarte nach Grafikspeicher."""
from __future__ import annotations

import sys
import threading
import time
import types

import numpy as np
import pytest

from conftest import mache_bild
from imgsortsearch import drossel, modelle
from imgsortsearch.drossel import Drossel


def _d(**kw):
    return Drossel({"drosselung": kw})


# ------------------------------------------------------------------ Drossel

def test_standard_ist_an_und_leicht():
    from imgsortsearch.einstellungen import STANDARD
    assert STANDARD["drosselung"] == {"aktiv": True, "stufe": "leicht"}
    d = Drossel(STANDARD)
    assert d.aktiv and d.anteil == 0.75


def test_stufen_und_aus():
    assert _d(aktiv=False, stufe="stark").anteil == 1.0
    assert _d(aktiv=True, stufe="stark").anteil == 0.5
    assert _d(aktiv=True, stufe="unbekannt").anteil == 0.75
    assert Drossel(None).anteil == 0.75  # ohne Einstellungen: Standard


def test_pausendauer():
    assert _d(aktiv=False).pausendauer(2.0) == 0
    assert _d(aktiv=True, stufe="leicht").pausendauer(3.0) == pytest.approx(1.0)
    assert _d(aktiv=True, stufe="stark").pausendauer(2.0) == pytest.approx(2.0)
    assert _d(aktiv=True, stufe="stark").pausendauer(600) == drossel.LAENGSTE_PAUSE


def test_threads_laptop_mit_4_threads(monkeypatch):
    monkeypatch.setattr(drossel.os, "cpu_count", lambda: 4)  # i5-7200U: 2 Kerne, 4 Threads
    assert _d(aktiv=False).rechen_threads() == 3
    assert _d(aktiv=True, stufe="leicht").rechen_threads() == 2
    assert _d(aktiv=True, stufe="stark").rechen_threads() == 2
    assert _d(aktiv=True, stufe="stark").threads(2) == 1
    assert _d(aktiv=True, stufe="leicht").threads(2) == 2
    assert _d(aktiv=True, stufe="stark").threads(1) == 1


def test_arbeit_pausiert_im_verhaeltnis():
    d = _d(aktiv=True, stufe="stark")
    t = time.perf_counter()
    with d.arbeit():
        time.sleep(0.2)
    assert time.perf_counter() - t >= 0.38  # 0,2 s Arbeit + 0,2 s Pause


def test_pause_endet_beim_anhalten():
    d = _d(aktiv=True, stufe="stark")
    stop = threading.Event()
    threading.Timer(0.1, stop.set).start()
    t = time.perf_counter()
    d.pause(8.0, stop)
    assert time.perf_counter() - t < 2


def test_einstellung_wirkt_sofort():
    e = {"drosselung": {"aktiv": True, "stufe": "stark"}}
    d = Drossel(e)
    assert d.pausendauer(1.0) == pytest.approx(1.0)
    e["drosselung"] = {"aktiv": False, "stufe": "stark"}
    assert d.pausendauer(1.0) == 0


def test_hintergrund_prioritaet_erst_nach_letztem_lauf_zurueck(monkeypatch):
    aufrufe = []
    monkeypatch.setattr(drossel, "prioritaet_setzen", lambda niedrig: aufrufe.append(niedrig))
    d = _d(aktiv=True, stufe="leicht")
    with d.hintergrund():
        with d.hintergrund():
            pass
        assert aufrufe == [True, True]  # innerer Lauf zu Ende: Priorität bleibt niedrig
    assert aufrufe == [True, True, False]


def test_creationflags_nur_windows(monkeypatch):
    d = _d(aktiv=True, stufe="leicht")
    monkeypatch.setattr(drossel.os, "name", "nt")
    assert d.creationflags() == drossel.BELOW_NORMAL_PRIORITY_CLASS
    assert _d(aktiv=False).creationflags() == 0
    monkeypatch.setattr(drossel.os, "name", "posix")
    assert d.creationflags() == 0


def test_indexlauf_pausiert_und_setzt_prioritaet(zustand, heim, monkeypatch):
    """Ein Indexlauf mit Drosselung pausiert nach den Arbeitsschritten und senkt die Priorität."""
    for i in range(6):
        mache_bild(heim / "backup" / f"IMG_2024010{i}_120000.jpg", farbe=(200, 30, 30))
    prio = []
    monkeypatch.setattr(drossel, "prioritaet_setzen", lambda niedrig: prio.append(niedrig))
    pausen = []
    monkeypatch.setattr(Drossel, "pause", lambda self, arbeit, stop=None: pausen.append(self.pausendauer(arbeit)))
    zustand.e.aktualisieren({"drosselung": {"aktiv": True, "stufe": "stark"}})
    ix = zustand.indexer
    ix.starten()
    ix._thread.join(60)
    assert ix.status["phase"] == "fertig", ix.status
    assert prio and prio[0] is True and prio[-1] is False
    assert len(pausen) >= 6 and any(p > 0 for p in pausen)

    # ohne Drosselung: keine Pausen
    pausen.clear()
    zustand.e.aktualisieren({"drosselung": {"aktiv": False}})
    mache_bild(heim / "backup" / "IMG_20240201_120000.jpg", farbe=(30, 200, 30))
    ix.starten()
    ix._thread.join(60)
    assert ix.status["phase"] == "fertig"
    assert pausen and all(p == 0 for p in pausen)


def test_einstellung_ueber_api(zustand):
    from fastapi.testclient import TestClient

    from imgsortsearch.server import erstelle_app
    c = TestClient(erstelle_app(zustand), headers={"X-ImgSortSearch": "1"})
    assert c.get("/api/einstellungen").json()["drosselung"] == {"aktiv": True, "stufe": "leicht"}
    r = c.post("/api/einstellungen", json={"drosselung": {"aktiv": False}})
    assert r.status_code == 200, r.text
    w = r.json()
    assert w["drosselung"] == {"aktiv": False, "stufe": "leicht"}
    assert zustand.indexer.drossel.anteil == 1.0
    assert zustand.dubletten.drossel.anteil == 1.0


# ------------------------------------------------------------------ Grafikkarte nach Grafikspeicher

LAPTOP = ["Intel(R) HD Graphics 620", "NVIDIA GeForce 940MX"]
LAPTOP_MB = {"Intel(R) HD Graphics 620": 1024, "NVIDIA GeForce 940MX": 2048}


@pytest.mark.parametrize("bedarf, erwartet", [
    (0, 1),        # ohne Angabe wie bisher: die eigene Grafikkarte
    (900, 1),      # Bildteil von CLIP passt auf die 940MX
    (2400, 0),     # ganzes CLIP / BLIP passt nicht: Prozessorgrafik mit gemeinsamem Arbeitsspeicher
])
def test_dml_karte_nach_speicher(bedarf, erwartet):
    assert modelle.dml_karte_waehlen(LAPTOP, bedarf, LAPTOP_MB) == erwartet


def test_dml_karte_ohne_alternative_bleibt_bei_der_karte():
    assert modelle.dml_karte_waehlen(["Microsoft Basic Render Driver", "NVIDIA GeForce 940MX"], 4000,
                                     {"NVIDIA GeForce 940MX": 2048}) == 1


def test_dml_karte_gross_genug_und_unbekannt():
    assert modelle.dml_karte_waehlen(["Intel(R) UHD Graphics 630", "AMD Radeon RX 5600 XT"], 2400,
                                     {"AMD Radeon RX 5600 XT": 6128}) == 1
    # Speicher unbekannt: wie bisher die eigene Karte
    assert modelle.dml_karte_waehlen(LAPTOP, 2400, {}) == 1
    # Namen leicht anders geschrieben
    assert modelle.dml_karte_waehlen(LAPTOP, 2400, {"NVIDIA GeForce 940MX ": 2048}) == 0


def _fake_dml(monkeypatch, namen, speicher):
    import torch
    monkeypatch.setattr(torch.cuda, "is_available", lambda: False)
    if hasattr(torch, "xpu"):
        monkeypatch.setattr(torch.xpu, "is_available", lambda: False)
    fake = types.SimpleNamespace(is_available=lambda: True, device_count=lambda: len(namen),
                                 device_name=lambda i: namen[i], device=lambda i=0: f"privateuseone:{i}")
    monkeypatch.setitem(sys.modules, "torch_directml", fake)
    monkeypatch.setattr(modelle, "grafikspeicher_mb", lambda: speicher)
    monkeypatch.setattr(modelle, "_KARTEN_SPEICHER", {})


def test_waehle_geraet_laptop_940mx(monkeypatch):
    pytest.importorskip("torch")
    _fake_dml(monkeypatch, LAPTOP, LAPTOP_MB)
    g, name = modelle.waehle_geraet("auto", bedarf_mb=2400)
    assert g == "privateuseone:0" and "HD Graphics 620" in name
    assert modelle.geraet_speicher_mb(g) is None  # Prozessorgrafik: gemeinsamer Arbeitsspeicher
    g, name = modelle.waehle_geraet("auto", bedarf_mb=900)
    assert g == "privateuseone:1" and "940MX" in name
    assert modelle.geraet_speicher_mb(g) == 2048
    # ausdrücklich DirectML gewählt: keine Rücksicht auf den Speicher, die beste Karte
    assert modelle.waehle_geraet("directml", bedarf_mb=2400)[0] == "privateuseone:1"


def test_waehle_geraet_cuda_zu_wenig_speicher(monkeypatch):
    torch = pytest.importorskip("torch")
    monkeypatch.setattr(torch.cuda, "is_available", lambda: True)
    monkeypatch.setattr(torch.cuda, "get_device_name", lambda i=0: "NVIDIA GeForce 940MX")
    monkeypatch.setattr(torch.cuda, "mem_get_info", lambda i=0: (1800 * 2 ** 20, 2048 * 2 ** 20))
    monkeypatch.setattr(modelle, "_cuda_pruefen", lambda t: "")
    monkeypatch.setitem(sys.modules, "torch_directml", None)  # CUDA-Installation: kein DirectML
    if hasattr(torch, "xpu"):
        monkeypatch.setattr(torch.xpu, "is_available", lambda: False)
    assert modelle.waehle_geraet("auto", bedarf_mb=2400)[0] == "cpu"
    monkeypatch.setattr(torch.cuda, "get_device_properties", lambda i=0: types.SimpleNamespace(total_memory=2048 * 2 ** 20))
    assert modelle.waehle_geraet("auto", bedarf_mb=900)[0] == "cuda"
    assert modelle.waehle_geraet("cuda", bedarf_mb=2400)[0] == "cuda"  # ausdrücklich gewählt


def test_waehle_geraet_cuda_karte_zu_alt(monkeypatch):
    torch = pytest.importorskip("torch")
    monkeypatch.setattr(torch.cuda, "is_available", lambda: True)
    monkeypatch.setattr(torch.cuda, "get_device_name", lambda i=0: "NVIDIA GeForce 940MX")
    monkeypatch.setattr(modelle, "_cuda_pruefen", lambda t: "diese PyTorch-Version unterstützt die Karte nicht")
    monkeypatch.setitem(sys.modules, "torch_directml", None)
    if hasattr(torch, "xpu"):
        monkeypatch.setattr(torch.xpu, "is_available", lambda: False)
    g, name = modelle.waehle_geraet("auto")
    assert g == "cpu"
    g, name = modelle.waehle_geraet("cuda")
    assert g == "cpu" and "unterstützt" in name


def test_cuda_pruefen_meldet_fehler():
    class T:
        @staticmethod
        def ones(*a, **k):
            raise RuntimeError("CUDA error: no kernel image is available for execution on the device")
    assert "no kernel image" in modelle._cuda_pruefen(T)


def test_clip_textteil_auf_prozessor_wenn_karte_zu_klein(monkeypatch, tmp_path):
    """Passt nur der Bildteil in den Grafikspeicher, kommt nur er auf die Karte, der Textteil bleibt beim Prozessor."""
    torch = pytest.importorskip("torch")
    pytest.importorskip("open_clip")
    monkeypatch.setenv("IMGSORTSEARCH_HOME", str(tmp_path))
    bedarfe = []

    def waehle(wunsch, bedarf_mb=0):
        bedarfe.append(bedarf_mb)
        return torch.device("cpu", 0), "Testkarte"   # "cpu:0": ein anderes Gerät als "cpu", rechnet aber hier
    monkeypatch.setattr(modelle, "waehle_geraet", waehle)
    monkeypatch.setattr(modelle, "geraet_speicher_mb", lambda g: 1000)
    c = modelle.ClipModell("ViT-B-32", None, "auto")
    assert c.text_geraet == "cpu" and "Textteil auf dem Prozessor" in c.geraet_name
    assert 700 < bedarfe[0] < 1000  # nur der Bildteil (ViT-B-32: etwa 335 MB Gewichte)
    from PIL import Image
    v = c.bilder([Image.new("RGB", (64, 64), "red")])
    t = c.texte(["ein Hund"])
    assert v.shape == (1, 512) and t.shape == (1, 512)
    assert np.isfinite(v).all() and np.isfinite(t).all()


def test_rechen_threads_gelten_fuer_modelle(zustand, monkeypatch):
    monkeypatch.setattr(drossel.os, "cpu_count", lambda: 4)
    zustand.e.aktualisieren({"drosselung": {"aktiv": True, "stufe": "stark"}})
    assert zustand.m._threads() == 2
    assert modelle.rechen_threads() == 2
    zustand.e.aktualisieren({"drosselung": {"aktiv": False}})
    monkeypatch.setattr(drossel.os, "cpu_count", lambda: 16)
    assert zustand.m._threads() == 15
    modelle.rechen_threads_setzen(None)
