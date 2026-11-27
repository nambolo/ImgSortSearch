"""Ausführliche Bildbeschreibungen: Download erst bei Auswahl, eigener Schritt, Suche, Rückfall."""
import http.server
import json
import threading
import time

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from conftest import mache_bild
from imgsortsearch import modelle
from imgsortsearch.server import erstelle_app

MODELL = "microsoft/Florence-2-base"


@pytest.fixture
def client(zustand):
    return TestClient(erstelle_app(zustand), headers={"X-ImgSortSearch": "1"})


def warte(c, max_s=30):
    for _ in range(max_s * 20):
        s = c.get("/api/status").json()
        if not s["index"]["laeuft"]:
            return s
        time.sleep(0.05)
    raise AssertionError("läuft noch")


class FakeAusfuehrlich:
    geraet_name = "Testgerät"

    def __init__(self):
        self.aufrufe = 0

    def beschreiben(self, bilder):
        self.aufrufe += len(bilder)
        erg = []
        for b in bilder:
            r, g, bl = b.getpixel((0, 0))
            tier = "Ein brauner Hund liegt auf dem Sofa." if bl > 150 else "Eine Tafel hängt an der Wand."
            de = f"Das Bild zeigt einen Raum. {tier} Im Hintergrund steht ein Regal."
            erg.append((de, "The image shows a room.", modelle.saetze_trennen(de)))
        return erg


@pytest.fixture
def mit_modell(zustand, monkeypatch):
    fake = FakeAusfuehrlich()
    monkeypatch.setattr(modelle, "ausfuehrlich_vorhanden", lambda m: True)
    monkeypatch.setattr(type(zustand.m), "ausfuehrlich", lambda self: fake)
    zustand.e.aktualisieren({"beschreibung": {"ausfuehrlich": {"aktiv": True}}})
    return fake


# ------------------------------------------------------------------ Grundlagen

def test_standard_aus_und_kein_download(zustand):
    assert zustand.e["beschreibung"]["ausfuehrlich"]["aktiv"] is False
    assert zustand.m.download.status["laeuft"] is False
    # Modellordner liegt im Datenordner des Programms
    assert str(modelle.ausfuehrlich_ordner(MODELL)).startswith(str(modelle.modell_ordner()))


def test_dateien_waehlen():
    baum = [{"type": "file", "path": p, "size": 10} for p in
            ("README.md", ".gitattributes", "config.json", "modeling_florence2.py", "tokenizer.json")]
    baum += [{"type": "file", "path": "pytorch_model.bin", "size": 100, "lfs": {"size": 5000}},
             {"type": "file", "path": "model.safetensors", "size": 100, "lfs": {"size": 4000}},
             {"type": "file", "path": "bilder/beispiel.json", "size": 1}, {"type": "directory", "path": "bilder"}]
    namen = dict(modelle.dateien_waehlen(baum))
    assert set(namen) == {"config.json", "modeling_florence2.py", "tokenizer.json", "model.safetensors"}
    assert namen["model.safetensors"] == 4000


def test_saetze_trennen():
    assert modelle.saetze_trennen("Ein Hund.  Zwei Katzen!\nDrei?") == ["Ein Hund.", "Zwei Katzen!", "Drei?"]


# ------------------------------------------------------------------ Download

@pytest.fixture
def hf_server():
    """Kleiner Ersatz für huggingface.co (API + Dateien, mit Range-Anfragen)."""
    dateien = {"config.json": b'{"a": 1}', "model.safetensors": bytes(range(256)) * 4000}
    zaehler = {"range": 0}

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            if self.path == f"/api/models/{MODELL}":
                daten = json.dumps({"sha": "abc123"}).encode()
            elif self.path == f"/api/models/{MODELL}/tree/abc123":
                daten = json.dumps([{"type": "file", "path": p, "size": len(d)} for p, d in dateien.items()]
                                   + [{"type": "file", "path": "README.md", "size": 3}]).encode()
            elif self.path.startswith(f"/{MODELL}/resolve/abc123/"):
                daten = dateien[self.path.rsplit("/", 1)[1]]
                rng = self.headers.get("Range")
                if rng:
                    zaehler["range"] += 1
                    start = int(rng.split("=")[1].rstrip("-"))
                    self.send_response(206)
                    self.send_header("Content-Length", str(len(daten) - start))
                    self.end_headers()
                    self.wfile.write(daten[start:])
                    return
            else:
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Length", str(len(daten)))
            self.end_headers()
            self.wfile.write(daten)

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", dateien, zaehler
    srv.shutdown()


def test_download_mit_fortschritt_und_fortsetzen(heim, hf_server):
    url, dateien, zaehler = hf_server
    ziel = modelle.ausfuehrlich_ordner(MODELL)
    ziel.mkdir(parents=True)
    # abgebrochener Download von vorhin
    (ziel / "model.safetensors.part").write_bytes(dateien["model.safetensors"][:5000])
    d = modelle.ModellDownload(url)
    fertig = []
    d.nach_download.append(fertig.append)
    assert d.starten(MODELL)
    d.warten(30)
    assert d.status["fehler"] == "" and d.status["abgeschlossen"]
    assert d.status["fertig"] == d.status["gesamt"] == sum(len(x) for x in dateien.values())
    assert (ziel / "model.safetensors").read_bytes() == dateien["model.safetensors"]
    assert not (ziel / "README.md").exists()
    assert zaehler["range"] == 1
    assert modelle.ausfuehrlich_vorhanden(MODELL) and fertig == [MODELL]
    assert json.loads((ziel / "vollstaendig.json").read_text())["stand"] == "abc123"
    # schon vorhanden: kein neuer Download
    assert d.starten(MODELL) is False


def test_download_fehler_wird_gemeldet(heim):
    d = modelle.ModellDownload("http://127.0.0.1:9")  # dort hört niemand zu
    d.starten(MODELL)
    d.warten(30)
    assert d.status["fehler"] and not d.status["laeuft"]
    assert not modelle.ausfuehrlich_vorhanden(MODELL)


def test_unbekanntes_modell_wird_nicht_geladen(heim):
    with pytest.raises(ValueError):
        modelle.ModellDownload("http://127.0.0.1:9").starten("irgendwer/irgendwas")


def test_einschalten_startet_download(client, zustand, monkeypatch):
    gestartet = []
    monkeypatch.setattr(zustand.m.download, "starten", lambda m: gestartet.append(m) or True)
    client.post("/api/einstellungen", json={"karte": {"detail": True}})
    assert gestartet == []  # andere Einstellungen laden nichts
    client.post("/api/einstellungen", json={"beschreibung": {"ausfuehrlich": {"aktiv": True, "modell": MODELL}}})
    assert gestartet == [MODELL]
    client.post("/api/einstellungen", json={"beschreibung": {"ausfuehrlich": {"aktiv": True, "modell": MODELL}}})
    assert gestartet == [MODELL]  # unverändert: nicht noch einmal
    s = client.get("/api/status").json()["ausfuehrlich"]
    assert s["aktiv"] and s["vorhanden"] is False and "download" in s


def test_ohne_modell_laeuft_index_trotzdem(client, zustand, monkeypatch):
    gestartet = []
    monkeypatch.setattr(zustand.m.download, "starten", lambda m: gestartet.append(m) or True)
    zustand.e.aktualisieren({"beschreibung": {"ausfuehrlich": {"aktiv": True}}})
    client.post("/api/index/start")
    s = warte(client)
    assert s["index"]["phase"] == "fertig" and s["offen"] == 0
    assert gestartet and any("heruntergeladen" in f for f in s["index"]["fehler"])
    assert s["ausfuehrlich"]["offen"] == s["gesamt"]


# ------------------------------------------------------------------ Indexlauf und Suche

def test_ausfuehrlich_im_index_und_suche(client, zustand, mit_modell):
    client.post("/api/index/start")
    s = warte(client)
    assert s["ausfuehrlich"]["offen"] == 0 and s["ausfuehrlich"]["fertig"] == s["gesamt"] == 5
    r = zustand.db.eins("SELECT id, beschreibung, beschreibung_lang, beschreibung_lang_vek FROM bilder "
                        "WHERE pfad LIKE '%c.jpg'")
    assert "Hund" in r["beschreibung_lang"] and r["beschreibung"] == "Ein Bild"
    assert r["beschreibung_lang_vek"] is not None
    d = client.get(f"/api/bild/{r['id']}").json()
    assert "Sofa" in d["beschreibung_lang"] and "beschreibung_lang_vek" not in d
    # Volltextsuche findet Wörter aus der ausführlichen Beschreibung
    namen = {b["name"] for b in client.get("/api/bilder", params={"q": "sofa", "art": "text"}).json()["bilder"]}
    assert namen == {"c.jpg", "d.jpg"}
    ids, mat = zustand.db.beschreibung_matrix()
    assert len(ids) == 15 and mat.shape == (15, 32)  # 3 Sätze je Bild


def test_nachholen_ohne_alles_neu(client, zustand, mit_modell):
    zustand.e.aktualisieren({"beschreibung": {"ausfuehrlich": {"aktiv": False}}})
    client.post("/api/index/start")
    warte(client)
    assert mit_modell.aufrufe == 0
    vorher = {r["id"]: r["clip"] for r in zustand.db.abfrage("SELECT id, clip FROM bilder")}
    # später eingeschaltet: nur der neue Schritt läuft, für alle vorhandenen Bilder
    zustand.e.aktualisieren({"beschreibung": {"ausfuehrlich": {"aktiv": True}}})
    zustand.m._clip.bilder = lambda b: (_ for _ in ()).throw(AssertionError("Bildinhalt neu berechnet"))
    client.post("/api/index/start")
    s = warte(client)
    assert mit_modell.aufrufe == 5 and s["ausfuehrlich"]["fertig"] == 5
    assert vorher == {r["id"]: r["clip"] for r in zustand.db.abfrage("SELECT id, clip FROM bilder")}
    # einzelne Bilder neu beschreiben
    bid = zustand.db.eins("SELECT id FROM bilder WHERE pfad LIKE '%a.jpg'")["id"]
    r = client.post("/api/bilder/neu-beschreiben", json={"ids": [bid]}).json()
    assert r["vorgemerkt"] == 1
    warte(client)
    assert mit_modell.aufrufe == 6
    # alle neu
    assert client.post("/api/bilder/neu-beschreiben", json={"alle": True}).json()["vorgemerkt"] == 5
    warte(client)
    assert mit_modell.aufrufe == 11


def test_neu_beschreiben_nur_wenn_eingeschaltet(client):
    assert client.post("/api/bilder/neu-beschreiben", json={"alle": True}).status_code == 400


def test_anhalten_und_fortsetzen(client, zustand, mit_modell):
    alt = mit_modell.beschreiben

    def langsam(bilder):
        zustand.indexer.stoppen()  # nach dem ersten Stapel anhalten
        return alt(bilder)
    mit_modell.beschreiben = langsam
    client.post("/api/index/start")
    s = warte(client)
    assert s["index"]["phase"] == "angehalten" and 0 < s["ausfuehrlich"]["offen"] < 5
    mit_modell.beschreiben = alt
    client.post("/api/index/start")
    s = warte(client)
    assert s["ausfuehrlich"]["offen"] == 0 and mit_modell.aufrufe == 5


def test_bedeutungssuche_in_beschreibungen(client, zustand, mit_modell):
    q = zustand.e["quellen"][0]
    for i in range(20):  # genug Sätze für eine sinnvolle Statistik
        mache_bild(__import__("pathlib").Path(q) / f"x{i}.jpg", "iPhone 13", "2024:04:01 12:00:00", farbe=(200, 30, 30))
    client.post("/api/index/start")
    warte(client)
    # FakeClip: "hund" zeigt in eine feste Richtung, die nur die blauen Bilder und der Hunde-Satz haben.
    # Bildvektoren entfernen: dann kann nur noch die Beschreibung treffen
    with zustand.db.verbindung() as v:
        v.execute("UPDATE bilder SET clip=NULL")
    namen = {b["name"] for b in client.get("/api/bilder", params={"q": "hund", "art": "bedeutung"}).json()["bilder"]}
    assert namen == {"c.jpg", "d.jpg"}


# ------------------------------------------------------------------ Modell-Klasse (ohne echtes Florence-2)

class _FakeTensor:
    def __init__(self, n):
        self.n = n

    def to(self, *a):
        return self


class _FakeModell:
    def __init__(self, fehler_bei=()):
        self.fehler_bei, self.dtype_name, self.geraet = set(fehler_bei), "fp16", "gpu"

    def eval(self):
        return self

    def float(self):
        self.dtype_name = "fp32"
        return self

    def to(self, geraet, *a):
        self.geraet = geraet
        return self

    def generate(self, input_ids, pixel_values, **kw):
        if (self.geraet, self.dtype_name) in self.fehler_bei:
            raise RuntimeError(f"geht nicht auf {self.geraet}/{self.dtype_name}")
        return list(range(input_ids.n))


class _FakeProc:
    def __call__(self, text, images, return_tensors):
        return {"input_ids": _FakeTensor(len(images)), "pixel_values": _FakeTensor(len(images))}

    def batch_decode(self, out, skip_special_tokens):
        return [f"roh{i}" for i in out]

    def post_process_generation(self, t, task, image_size):
        return {task: f"A dog sits.  It is {t}. "}


def _beschreiber(monkeypatch, fehler_bei):
    import transformers
    modell = _FakeModell(fehler_bei)
    monkeypatch.setattr(modelle, "ausfuehrlich_vorhanden", lambda m: True)
    monkeypatch.setattr(modelle, "waehle_geraet", lambda w: ("gpu", "Testkarte"))
    monkeypatch.setattr(transformers.AutoModelForCausalLM, "from_pretrained", lambda *a, **k: modell)
    monkeypatch.setattr(transformers.AutoProcessor, "from_pretrained", lambda *a, **k: _FakeProc())
    return modelle.AusfuehrlicherBeschreiber(MODELL, "detailliert", "", "auto"), modell


def test_beschreiber_halbe_genauigkeit_auf_grafikkarte(heim, monkeypatch):
    b, m = _beschreiber(monkeypatch, ())
    bilder = [Image.new("RGB", (8, 8)), Image.new("RGB", (8, 8))]
    erg = b.beschreiben(bilder)
    assert erg[1] == ("A dog sits. It is roh1.", "A dog sits. It is roh1.", ["A dog sits.", "It is roh1."])
    assert m.geraet == "gpu" and "halbe" in b.geraet_name and b.aufgabe == "<DETAILED_CAPTION>"


def test_beschreiber_faellt_auf_volle_genauigkeit_und_cpu_zurueck(heim, monkeypatch):
    b, m = _beschreiber(monkeypatch, {("gpu", "fp16")})
    b.beschreiben([Image.new("RGB", (8, 8))])
    assert m.geraet == "gpu" and m.dtype_name == "fp32" and "volle" in b.geraet_name
    b, m = _beschreiber(monkeypatch, {("gpu", "fp16"), ("gpu", "fp32")})
    b.beschreiben([Image.new("RGB", (8, 8))])
    assert m.geraet == "cpu" and str(b.geraet) == "cpu"


def test_beschreiber_uebersetzt_satzweise(heim, monkeypatch):
    b, _ = _beschreiber(monkeypatch, ())

    class Tok:
        def __call__(self, saetze, **kw):
            self.saetze = saetze
            return _Batch()

        def batch_decode(self, out, skip_special_tokens):
            return [f"DE:{s}" for s in self.saetze]

    class _Batch(dict):
        def to(self, g):
            return self

    class Ue:
        def generate(self, **kw):
            return None

    b.ue_tok, b.ue_model = Tok(), Ue()
    de, en, saetze = b.beschreiben([Image.new("RGB", (8, 8))])[0]
    assert saetze == ["DE:A dog sits.", "DE:It is roh0."] and de == "DE:A dog sits. DE:It is roh0."
    assert en == "A dog sits. It is roh0."
