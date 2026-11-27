"""Windows-nahe Teile, die sich auch ohne Windows prüfen lassen: Grafikkartenwahl,
fortsetzbare Downloads, WebView2-Erkennung."""
from __future__ import annotations

import http.server
import sys
import threading
import types

import pytest

from imgsortsearch import app, modelle


# ------------------------------------------------------------------ DirectML-Karte

@pytest.mark.parametrize("namen, erwartet", [
    (["AMD Radeon RX 5600 XT"], 0),
    (["Intel(R) UHD Graphics 630", "AMD Radeon RX 5600 XT"], 1),
    (["AMD Radeon(TM) Graphics", "AMD Radeon RX 6700 XT"], 1),       # Ryzen-Grafik + Karte
    (["Intel(R) Iris(R) Xe Graphics", "NVIDIA GeForce RTX 3050 Laptop GPU"], 1),
    (["Microsoft Basic Render Driver", "Intel(R) UHD Graphics 620"], 1),
    (["Intel(R) UHD Graphics", "Intel(R) Arc(TM) A770 Graphics"], 1),
    (["AMD Radeon RX 5600 XT", "AMD Radeon RX 6800"], 0),             # gleichwertig: die erste
    ([], 0),
])
def test_dml_karte_waehlen(namen, erwartet):
    assert modelle.dml_karte_waehlen(namen) == erwartet


def test_waehle_geraet_nimmt_dedizierte_dml_karte(monkeypatch):
    import torch
    monkeypatch.setattr(torch.cuda, "is_available", lambda: False)
    if hasattr(torch, "xpu"):
        monkeypatch.setattr(torch.xpu, "is_available", lambda: False)
    fake = types.SimpleNamespace(
        is_available=lambda: True, device_count=lambda: 2,
        device_name=lambda i: ["Intel(R) UHD Graphics 630", "AMD Radeon RX 5600 XT"][i],
        device=lambda i=0: f"privateuseone:{i}", default_device=lambda: 0)
    monkeypatch.setitem(sys.modules, "torch_directml", fake)
    geraet, name = modelle.waehle_geraet("auto")
    assert geraet == "privateuseone:1"
    assert "RX 5600 XT" in name


# ------------------------------------------------------------------ Downloads mit Fortsetzen

INHALT = bytes(range(256)) * 4000  # ca. 1 MB


class _RangeServer(http.server.BaseHTTPRequestHandler):
    anfragen: list = []
    mit_range = True

    def do_GET(self):
        rng = self.headers.get("Range")
        type(self).anfragen.append(rng)
        if rng and self.mit_range:
            start = int(rng.split("=")[1].rstrip("-"))
            if start >= len(INHALT):
                self.send_response(416)
                self.end_headers()
                return
            teil = INHALT[start:]
            self.send_response(206)
        else:
            teil = INHALT
            self.send_response(200)
        self.send_header("Content-Length", str(len(teil)))
        self.end_headers()
        self.wfile.write(teil)

    def log_message(self, *a):
        pass


@pytest.fixture
def server():
    _RangeServer.anfragen = []
    _RangeServer.mit_range = True
    s = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _RangeServer)
    t = threading.Thread(target=s.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{s.server_address[1]}/datei", _RangeServer
    s.shutdown()


def test_lade_datei_setzt_fort(server, tmp_path):
    url, h = server
    ziel = tmp_path / "buffalo_l.zip"
    (tmp_path / "buffalo_l.zip.part").write_bytes(INHALT[:300_000])   # abgebrochener Download
    modelle.lade_datei([url], ziel)
    assert ziel.read_bytes() == INHALT
    assert h.anfragen == ["bytes=300000-"]
    assert not (tmp_path / "buffalo_l.zip.part").exists()


def test_lade_datei_ohne_range_von_vorn(server, tmp_path):
    url, h = server
    h.mit_range = False
    ziel = tmp_path / "x.zip"
    (tmp_path / "x.zip.part").write_bytes(b"alt" * 1000)
    modelle.lade_datei([url], ziel)
    assert ziel.read_bytes() == INHALT


def test_teil_schon_komplett(server, tmp_path):
    url, h = server
    teil = tmp_path / "w.safetensors.part"
    teil.write_bytes(INHALT)
    # Größe bekannt: gar keine Anfrage
    modelle._teil_laden(url, teil, len(INHALT))
    assert h.anfragen == []
    # Größe unbekannt: Server antwortet 416, Datei wird neu geladen statt ewig zu scheitern
    modelle._teil_laden(url, teil)
    assert teil.read_bytes() == INHALT


def test_teil_zu_gross_wird_neu_geladen(server, tmp_path):
    url, _ = server
    teil = tmp_path / "w.part"
    teil.write_bytes(INHALT + b"muell")
    modelle._teil_laden(url, teil, len(INHALT))
    assert teil.read_bytes() == INHALT


def test_teil_laden_abbrechen(server, tmp_path):
    url, _ = server
    stopp = threading.Event()
    stopp.set()
    with pytest.raises(InterruptedError):
        modelle._teil_laden(url, tmp_path / "a.part", stopp=stopp)


# ------------------------------------------------------------------ WebView2

def test_ohne_webview2_kein_ie_fenster(monkeypatch):
    plattformen = types.ModuleType("webview.platforms")
    winforms = types.ModuleType("webview.platforms.winforms")
    plattformen.winforms = winforms
    monkeypatch.setitem(sys.modules, "webview.platforms", plattformen)
    monkeypatch.setitem(sys.modules, "webview.platforms.winforms", winforms)
    monkeypatch.setattr(sys, "platform", "win32")
    winforms.renderer = "mshtml"
    assert app._nur_alter_internet_explorer() is True
    winforms.renderer = "edgechromium"
    assert app._nur_alter_internet_explorer() is False
    monkeypatch.setattr(sys, "platform", "linux")
    winforms.renderer = "mshtml"
    assert app._nur_alter_internet_explorer() is False
