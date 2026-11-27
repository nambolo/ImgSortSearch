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
    assert g.geraet_name == "DirectML (Grafikkarte)"
    assert g.det.get_providers()[0] == "DmlExecutionProvider"
    assert g.rec.get_providers()[0] == "DmlExecutionProvider"


def test_gesichter_nur_ein_modell_auf_dml(monkeypatch):
    """Kann DirectML nur eines der beiden Modelle, läuft nur das andere auf dem Prozessor."""
    _fake_ort(monkeypatch)
    alt = _Sitzung.run

    def run(self, _, eingaben):
        if self.anbieter[0] == "DmlExecutionProvider" and eingaben["x"].shape[-1] == 112:
            raise RuntimeError("BatchNormalization: Falscher Parameter")
        return alt(self, _, eingaben)
    monkeypatch.setattr(_Sitzung, "run", run)
    g = modelle.GesichtsModell(beschleunigung="auto")
    assert g.det.get_providers()[0] == "DmlExecutionProvider"
    assert g.rec.get_providers() == ["CPUExecutionProvider"]
    assert g.geraet_name == "DirectML (Finden), Prozessor (Wiedererkennen)"


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
    assert g.det.get_providers() == ["CPUExecutionProvider"]
    assert g.geraet_name == "Prozessor (Finden), DirectML (Wiedererkennen)"


# ------------------------------------------------------------------ DirectML-Anpassungen

def test_einfache_attention_gleiches_ergebnis():
    import pytest
    torch = pytest.importorskip("torch")
    open_clip = pytest.importorskip("open_clip")
    torch.manual_seed(0)
    m, _, _ = open_clip.create_model_and_transforms("ViT-B-32", pretrained=None)
    m.eval()
    with torch.no_grad():
        for p in m.visual.parameters():
            p.add_(torch.randn_like(p) * 0.05)
    x = torch.rand(2, 3, 224, 224)
    with torch.no_grad():
        vorher = m.encode_image(x)
    assert modelle.einfache_attention_einsetzen(m.visual) == 12
    benutzt = []
    alt = torch._native_multi_head_attention
    torch._native_multi_head_attention = lambda *a, **k: benutzt.append(1) or alt(*a, **k)
    try:
        with torch.no_grad():
            nachher = m.encode_image(x)
    finally:
        torch._native_multi_head_attention = alt
    assert not benutzt   # der Befehl, den DirectML nicht kann, kommt nicht mehr vor
    assert torch.allclose(vorher, nachher, atol=1e-4)


def test_det_feste_groesse(tmp_path):
    import pytest
    onnx = pytest.importorskip("onnx")
    ort = pytest.importorskip("onnxruntime")
    from onnx import TensorProto, helper, numpy_helper
    # Mini-Detektor wie SCRFD: Resize mit zur Laufzeit berechneter Zielgröße (doppelte Eingabegröße)
    k = lambda name, arr: numpy_helper.from_array(np.array(arr), name)  # noqa: E731
    knoten = [
        helper.make_node("Shape", ["x"], ["s"]),
        helper.make_node("Slice", ["s", "a", "b"], ["hw"]),
        helper.make_node("Mul", ["hw", "zwei"], ["hw2"]),
        helper.make_node("Concat", ["eins", "hw2"], ["groesse"], axis=0),
        helper.make_node("Resize", ["x", "leer", "leer", "groesse"], ["y"], mode="nearest",
                         coordinate_transformation_mode="asymmetric", nearest_mode="floor"),
    ]
    g = helper.make_graph(knoten, "mini", [helper.make_tensor_value_info("x", TensorProto.FLOAT, [1, 3, "?", "?"])],
                          [helper.make_tensor_value_info("y", TensorProto.FLOAT, None)],
                          [k("a", np.array([2], np.int64)), k("b", np.array([4], np.int64)),
                           k("zwei", np.array([2], np.int64)), k("eins", np.array([1, 3], np.int64)),
                           numpy_helper.from_array(np.zeros(0, np.float32), "leer")])
    m = helper.make_model(g, opset_imports=[helper.make_opsetid("", 11)])
    m.ir_version = 6
    quelle = tmp_path / "det.onnx"
    onnx.save(m, str(quelle))
    fest = modelle.det_feste_groesse(quelle, 8)
    assert fest.name == "det_fest8.onnx"
    x = np.random.default_rng(0).random((1, 3, 8, 8)).astype(np.float32)
    a = ort.InferenceSession(str(quelle), providers=["CPUExecutionProvider"]).run(None, {"x": x})[0]
    b_s = ort.InferenceSession(str(fest), providers=["CPUExecutionProvider"])
    assert b_s.get_inputs()[0].shape == [1, 3, 8, 8]
    assert np.array_equal(a, b_s.run(None, {"x": x})[0])
    assert modelle.det_feste_groesse(quelle, 8) == fest   # zweites Mal aus dem Zwischenspeicher


def test_rec_ohne_flache_batchnorm(tmp_path):
    import pytest
    onnx = pytest.importorskip("onnx")
    ort = pytest.importorskip("onnxruntime")
    from onnx import TensorProto, helper, numpy_helper
    rng = np.random.default_rng(0)
    init = [numpy_helper.from_array(rng.normal(size=(6, 4)).astype(np.float32), "w"),
            numpy_helper.from_array(rng.normal(size=6).astype(np.float32), "b")]
    for n, wert in (("g", rng.normal(size=6)), ("be", rng.normal(size=6)), ("mu", rng.normal(size=6)),
                    ("var", rng.uniform(0.5, 2, 6))):
        init.append(numpy_helper.from_array(wert.astype(np.float32), n))
    knoten = [helper.make_node("Gemm", ["x", "w", "b"], ["h"], transB=1),
              helper.make_node("BatchNormalization", ["h", "g", "be", "mu", "var"], ["y"], epsilon=1e-5)]
    g = helper.make_graph(knoten, "mini", [helper.make_tensor_value_info("x", TensorProto.FLOAT, ["N", 4])],
                          [helper.make_tensor_value_info("y", TensorProto.FLOAT, [1, 6])], init)
    m = helper.make_model(g, opset_imports=[helper.make_opsetid("", 11)])
    m.ir_version = 6
    quelle = tmp_path / "rec.onnx"
    onnx.save(m, str(quelle))
    neu = modelle.rec_ohne_flache_batchnorm(quelle)
    assert neu.name == "rec_dml.onnx"
    assert not any(n.op_type == "BatchNormalization" for n in onnx.load(str(neu)).graph.node)
    x = rng.normal(size=(3, 4)).astype(np.float32)
    a = ort.InferenceSession(str(quelle), providers=["CPUExecutionProvider"]).run(None, {"x": x})[0]
    b = ort.InferenceSession(str(neu), providers=["CPUExecutionProvider"]).run(None, {"x": x})[0]
    assert np.allclose(a, b, atol=1e-5)
