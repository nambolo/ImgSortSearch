"""Ungenutzte Modelle anzeigen und löschen (Einstellungen > Modelle und Speicherplatz)."""
import pytest
from fastapi.testclient import TestClient

from imgsortsearch import modellpflege
from imgsortsearch.modelle import modell_ordner
from imgsortsearch.server import erstelle_app

CLIP = "models--laion--CLIP-ViT-B-32-xlm-roberta-base-laion5B-s13B-b90k"


@pytest.fixture
def client(zustand, monkeypatch):
    # open_clip nicht laden: die Ordner, die das gewählte Bildmodell braucht
    monkeypatch.setattr(modellpflege, "_clip_repos", lambda m, p: frozenset({CLIP, "models--xlm-roberta-base"}))
    return TestClient(erstelle_app(zustand), headers={"X-ImgSortSearch": "1"})


def datei(rel: str, groesse: int = 1000):
    p = modell_ordner() / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x" * groesse)
    return p


@pytest.fixture
def bestand():
    datei(f"clip/{CLIP}/blobs/gewichte", 5000)
    datei("clip/models--xlm-roberta-base/blobs/tok", 100)
    datei("clip/models--laion--CLIP-ViT-L-14-alt/blobs/gewichte", 7000)   # früher gewähltes Bildmodell
    datei("hf/models--Salesforce--blip-image-captioning-base/blobs/w", 3000)
    datei("hf/models--Helsinki-NLP--opus-mt-en-de/blobs/w", 2000)
    datei("hf/.locks/models--Salesforce--blip-image-captioning-base/x.lock", 0)
    datei("hf/hub/models--xlm-roberta-base/blobs/c", 50)
    datei("buffalo_l/det_10g.onnx", 4000)
    datei("buffalo_l/w600k_r50.onnx", 4000)
    datei("ausfuehrlich/microsoft--Florence-2-large/model.safetensors", 9000)
    datei("ausfuehrlich/microsoft--Florence-2-base/model.safetensors", 6000)
    datei("face_detection_yunet_2023mar.onnx", 300)
    datei("face_recognition_sface_2021dec.onnx", 400)
    datei("unbekannt.bin", 10)


def liste(client):
    return {m["id"]: m for m in client.get("/api/modelle").json()["modelle"]}


def test_standard_einstellungen(client, bestand):
    """Kurze Beschreibungen und Gesichter an, ausführliche aus."""
    r = client.get("/api/modelle").json()
    m = {x["id"]: x for x in r["modelle"]}
    gebraucht = {i for i, x in m.items() if x["gebraucht"]}
    assert gebraucht == {f"clip/{CLIP}", "clip/models--xlm-roberta-base", "hf/hub",
                         "hf/models--Salesforce--blip-image-captioning-base", "hf/models--Helsinki-NLP--opus-mt-en-de",
                         "buffalo_l", "unbekannt.bin"}
    assert set(m) - gebraucht == {"clip/models--laion--CLIP-ViT-L-14-alt", "ausfuehrlich/microsoft--Florence-2-large",
                                  "ausfuehrlich/microsoft--Florence-2-base", "face_detection_yunet_2023mar.onnx",
                                  "face_recognition_sface_2021dec.onnx"}
    assert m["buffalo_l"]["groesse"] == 8000
    assert r["frei"] == 7000 + 9000 + 6000 + 300 + 400
    assert r["gesamt"] == sum(x["groesse"] for x in m.values())
    assert "ausgeschaltet" in m["ausfuehrlich/microsoft--Florence-2-large"]["grund"]


def test_ausgeschaltete_funktionen(client, zustand, bestand):
    zustand.e.aktualisieren({"gesichter": {"aktiv": False}, "beschreibung": {"aktiv": False}})
    m = liste(client)
    assert not m["buffalo_l"]["gebraucht"]
    assert not m["hf/models--Salesforce--blip-image-captioning-base"]["gebraucht"]
    assert not m["hf/models--Helsinki-NLP--opus-mt-en-de"]["gebraucht"]
    # Übersetzer wird auch für die ausführlichen Beschreibungen gebraucht
    zustand.e.aktualisieren({"beschreibung": {"ausfuehrlich": {"aktiv": True, "modell": "microsoft/Florence-2-base"}}})
    m = liste(client)
    assert m["hf/models--Helsinki-NLP--opus-mt-en-de"]["gebraucht"]
    assert m["ausfuehrlich/microsoft--Florence-2-base"]["gebraucht"]
    assert not m["ausfuehrlich/microsoft--Florence-2-large"]["gebraucht"]
    assert "Anderes Modell" in m["ausfuehrlich/microsoft--Florence-2-large"]["grund"]


def test_laufender_download_wird_nicht_geloescht(client, zustand, bestand):
    zustand.m.download.status.update(laeuft=True, modell="microsoft/Florence-2-large")
    try:
        assert liste(client)["ausfuehrlich/microsoft--Florence-2-large"]["gebraucht"]
    finally:
        zustand.m.download.status.update(laeuft=False)


def test_entfernen_nur_ungenutzte(client, zustand, bestand):
    zustand.e.aktualisieren({"gesichter": {"aktiv": False}})
    basis = modell_ordner()
    ids = ["buffalo_l", "ausfuehrlich/microsoft--Florence-2-large", "face_detection_yunet_2023mar.onnx",
           f"clip/{CLIP}", "hf/models--Salesforce--blip-image-captioning-base", "../einstellungen.json", "gibtsnicht"]
    assert client.post("/api/modelle/entfernen", json={"ids": ids}).status_code == 400  # ohne Bestätigung nichts
    assert (basis / "buffalo_l").exists()
    zustand.m._gesichter = object()
    r = client.post("/api/modelle/entfernen", json={"ids": ids, "bestaetigt": True}).json()
    assert r["entfernt"] == ["buffalo_l", "ausfuehrlich/microsoft--Florence-2-large", "face_detection_yunet_2023mar.onnx"]
    assert r["frei"] == 8000 + 9000 + 300
    assert {f["id"] for f in r["fehler"]} == {f"clip/{CLIP}", "hf/models--Salesforce--blip-image-captioning-base",
                                              "../einstellungen.json", "gibtsnicht"}
    assert not (basis / "buffalo_l").exists() and not (basis / "face_detection_yunet_2023mar.onnx").exists()
    assert (basis / "clip" / CLIP).exists() and (basis / "hf/models--Salesforce--blip-image-captioning-base").exists()
    assert (basis.parent / "einstellungen.json").exists()
    assert zustand.m._gesichter is None  # vorher aus dem Speicher genommen (Windows sperrt sonst die Dateien)
    m = liste(client)
    assert "buffalo_l" not in m and "ausfuehrlich/microsoft--Florence-2-base" in m


def test_sperrdateien_mit_geloescht(client, zustand, bestand):
    zustand.e.aktualisieren({"beschreibung": {"aktiv": False}})
    r = client.post("/api/modelle/entfernen", json={"ids": ["hf/models--Salesforce--blip-image-captioning-base"],
                                                     "bestaetigt": True}).json()
    assert r["entfernt"] and not (modell_ordner() / "hf/.locks/models--Salesforce--blip-image-captioning-base").exists()


def test_waehrend_indexlauf_gesperrt(client, zustand, bestand):
    zustand.indexer.status["laeuft"] = True
    try:
        assert client.get("/api/modelle").json()["gesperrt"]
        r = client.post("/api/modelle/entfernen", json={"ids": ["face_detection_yunet_2023mar.onnx"], "bestaetigt": True})
        assert r.status_code == 409
        assert (modell_ordner() / "face_detection_yunet_2023mar.onnx").exists()
    finally:
        zustand.indexer.status["laeuft"] = False


def test_wieder_eingeschaltet_laedt_neu(client, zustand, bestand, monkeypatch):
    """Nach dem Löschen und Wiedereinschalten startet der Download wie beim ersten Mal."""
    client.post("/api/modelle/entfernen", json={"ids": ["ausfuehrlich/microsoft--Florence-2-large"], "bestaetigt": True})
    gestartet = []
    monkeypatch.setattr(zustand.m.download, "starten", lambda modell: gestartet.append(modell) or True)
    client.post("/api/einstellungen", json={"beschreibung": {"ausfuehrlich": {"aktiv": True,
                                                                              "modell": "microsoft/Florence-2-large"}}})
    assert gestartet == ["microsoft/Florence-2-large"]


def test_clip_unklar_nichts_loeschen(zustand, bestand, monkeypatch):
    """Lässt sich nicht feststellen, was das Bildmodell braucht, bleibt dort alles."""
    monkeypatch.setattr(modellpflege, "_clip_repos", lambda m, p: None)
    m = {x["id"]: x for x in modellpflege.bestand(zustand.e.werte)}
    assert all(x["gebraucht"] for i, x in m.items() if i.startswith("clip/"))


def test_leerer_modellordner(client):
    r = client.get("/api/modelle").json()
    assert r["frei"] == 0 and all(x["gebraucht"] for x in r["modelle"])


def test_keine_huggingface_warnungen():
    """Windows ohne Symlinks und ohne hf_xet: keine Warnung je Modell bei Installation und Start."""
    import subprocess
    import sys
    code = ("import imgsortsearch, huggingface_hub.constants as c; "
            "print(c.HF_HUB_DISABLE_SYMLINKS_WARNING, c.HF_HUB_DISABLE_XET)")
    env = {k: v for k, v in __import__("os").environ.items() if not k.startswith("HF_HUB_DISABLE")}
    aus = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, check=True)
    assert aus.stdout.split() == ["True", "True"]
