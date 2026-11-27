"""Dubletten: finden (exakt und ähnlich), Vorschlag, aussortieren und rückgängig."""
import shutil
import struct
import time
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from imgsortsearch import dubletten, papierkorb
from imgsortsearch.server import erstelle_app

from conftest import FakeClip


def foto(pfad: Path, seed: int, groesse=(800, 600), datum=None, modell=None, quality=92) -> Path:
    """Bild mit Struktur (weiche Flecken), damit der Fingerabdruck etwas zu vergleichen hat."""
    pfad.parent.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(seed)
    klein = rng.integers(0, 255, (6, 8, 3), dtype=np.uint8)
    img = Image.fromarray(klein).resize(groesse, Image.Resampling.BICUBIC)
    exif = Image.Exif()
    if modell:
        exif[271], exif[272] = "Samsung", modell
    if datum:
        exif.get_ifd(0x8769)[36867] = datum
    img.save(pfad, quality=quality, exif=exif)
    return pfad


class FarbClip(FakeClip):
    """Bildvektor aus dem Bildinhalt: gleiche Bilder liegen nah beieinander."""

    def bilder(self, bilder):
        erg = []
        for b in bilder:
            v = np.asarray(b.convert("RGB").resize((4, 4)), dtype=np.float32).ravel()[:32] - 128
            erg.append(v / (np.linalg.norm(v) + 1e-9))
        return np.stack(erg)


@pytest.fixture
def dub(zustand, heim):
    zustand.m._clip = FarbClip()
    q = heim / "fotos"
    foto(q / "urlaub" / "IMG_0001.jpg", 1, datum="2023:07:01 12:00:00", modell="SM-A546B")
    (q / "sicherung").mkdir()
    shutil.copy2(q / "urlaub" / "IMG_0001.jpg", q / "sicherung" / "IMG_0001 - Kopie.jpg")
    # WhatsApp-Fassung: kleiner, stärker komprimiert, ohne EXIF
    (q / "WhatsApp").mkdir()
    with Image.open(q / "urlaub" / "IMG_0001.jpg") as img:
        img.resize((400, 300)).save(q / "WhatsApp" / "IMG-20230702-WA0003.jpg", quality=60)
    # Serienbild: gleicher Inhalt, aber 10 s später aufgenommen
    foto(q / "urlaub" / "IMG_0002.jpg", 1, datum="2023:07:01 12:00:10", modell="SM-A546B", quality=85)
    with Image.open(q / "urlaub" / "IMG_0002.jpg") as img:  # etwas bewegt
        exif = img.getexif()
        img.crop((6, 0, 806, 600)).save(q / "urlaub" / "IMG_0002.jpg", quality=85, exif=exif)
    foto(q / "anderes.jpg", 2, datum="2023:07:03 09:00:00")
    zustand.e.aktualisieren({"quellen": [str(q)]})
    c = TestClient(erstelle_app(zustand), headers={"X-ImgSortSearch": "1"})
    c.post("/api/index/start")
    warte(lambda: not c.get("/api/status").json()["index"]["laeuft"])
    return c, q


def warte(bedingung, max_s=30):
    for _ in range(max_s * 20):
        if bedingung():
            return
        time.sleep(0.05)
    raise AssertionError("Zeitüberschreitung")


def suchen(c):
    assert c.post("/api/dubletten/suchen").json()["gestartet"]
    warte(lambda: not c.get("/api/dubletten").json()["status"]["laeuft"])
    return c.get("/api/dubletten").json()


def test_findet_exakte_und_aehnliche_aber_keine_serienbilder(dub):
    c, q = dub
    d = suchen(c)
    assert d["status"]["phase"] == "fertig"
    gruppen = d["ergebnis"]["gruppen"]
    assert len(gruppen) == 1, gruppen
    g = gruppen[0]
    namen = {b["name"] for b in g["bilder"]}
    assert namen == {"IMG_0001.jpg", "IMG_0001 - Kopie.jpg", "IMG-20230702-WA0003.jpg"}
    assert g["art"] == "aehnlich"
    behalten = [b for b in g["bilder"] if b["behalten"]]
    assert [b["name"] for b in behalten] == ["IMG_0001.jpg"]
    assert "höchste Auflösung" in g["gruende"]
    assert g["bilder"][0]["behalten"]  # Vorschlag steht vorn


def test_nur_exakte_kopie_ist_exakt(zustand, heim):
    q = heim / "nur"
    foto(q / "a.jpg", 5)
    (q / "b").mkdir()
    shutil.copy2(q / "a.jpg", q / "b" / "a.jpg")
    zustand.e.aktualisieren({"quellen": [str(q)]})
    c = TestClient(erstelle_app(zustand), headers={"X-ImgSortSearch": "1"})
    c.post("/api/index/start")
    warte(lambda: not c.get("/api/status").json()["index"]["laeuft"])
    g = suchen(c)["ergebnis"]["gruppen"]
    assert len(g) == 1 and g[0]["art"] == "exakt"
    # Gleiche Datei: der kürzere Pfad wird behalten
    assert [b["pfad"] for b in g[0]["bilder"] if b["behalten"]] == [str(q / "a.jpg")]


def test_zweiter_lauf_nutzt_zwischenspeicher(dub, monkeypatch):
    c, _ = dub
    suchen(c)
    aufrufe = []
    monkeypatch.setattr(dubletten, "_hash", lambda p: aufrufe.append(p) or "x")
    monkeypatch.setattr(dubletten, "dhash", lambda img: aufrufe.append(img) or "0" * 32)
    assert len(suchen(c)["ergebnis"]["gruppen"]) == 1
    assert aufrufe == []


def test_quarantaene_und_rueckgaengig(dub, heim):
    c, q = dub
    g = suchen(c)["ergebnis"]["gruppen"][0]
    behalten = [b["id"] for b in g["bilder"] if b["behalten"]]
    weg = [b["id"] for b in g["bilder"] if not b["behalten"]]
    qu = heim / "quarantaene"
    # Behalten-Bild darf nie angefasst werden, auch wenn es in "weg" mitgeschickt wird
    r = c.post("/api/dubletten/bereinigen", json={"ziel": "quarantaene", "quarantaene": str(qu),
                                                   "gruppen": [{"nr": g["nr"], "behalten": behalten,
                                                                "weg": weg + behalten}]})
    assert r.status_code == 200, r.text
    erg = r.json()
    assert sorted(erg["erledigt"]) == sorted(weg) and not erg["fehler"]
    assert (q / "urlaub" / "IMG_0001.jpg").exists()
    assert not (q / "sicherung" / "IMG_0001 - Kopie.jpg").exists()
    assert not (q / "WhatsApp" / "IMG-20230702-WA0003.jpg").exists()
    verschoben = sorted(p.name for p in qu.rglob("*.jpg"))
    assert verschoben == ["IMG-20230702-WA0003.jpg", "IMG_0001 - Kopie.jpg"]
    # Ordnerstruktur bleibt erkennbar
    assert any(p.parent.name == "WhatsApp" for p in qu.rglob("*.jpg"))
    # aus Index und Liste verschwunden
    assert c.get("/api/dubletten").json()["ergebnis"]["gruppen"] == []
    namen = {b["name"] for b in c.get("/api/bilder").json()["bilder"]}
    assert "IMG_0001 - Kopie.jpg" not in namen and "IMG_0001.jpg" in namen
    # Indexlauf nimmt die Quarantäne nicht als neue Bilder auf (auch wenn sie in der Quelle läge)
    c.post("/api/index/start")
    warte(lambda: not c.get("/api/status").json()["index"]["laeuft"])
    assert "IMG_0001 - Kopie.jpg" not in {b["name"] for b in c.get("/api/bilder").json()["bilder"]}

    # Rückgängig über die bisherigen Läufe
    pid = erg["protokoll_id"]
    assert "Dubletten" in c.get("/api/protokolle").json()["protokolle"][0]["beschreibung"]
    r = c.post(f"/api/protokolle/{pid}/rueckgaengig", params={"echt": True}).json()
    assert r["anzahl"] == 2 and not r["probleme"]
    assert (q / "sicherung" / "IMG_0001 - Kopie.jpg").exists()
    assert (q / "WhatsApp" / "IMG-20230702-WA0003.jpg").exists()
    assert not list(qu.rglob("*.jpg"))
    c.post("/api/index/start")
    warte(lambda: not c.get("/api/status").json()["index"]["laeuft"])
    assert "IMG_0001 - Kopie.jpg" in {b["name"] for b in c.get("/api/bilder").json()["bilder"]}


def test_quarantaene_in_der_quelle_wird_nicht_indiziert(dub, heim):
    c, q = dub
    g = suchen(c)["ergebnis"]["gruppen"][0]
    qu = q / "_Dubletten"
    c.post("/api/dubletten/bereinigen", json={"quarantaene": str(qu), "gruppen": [
        {"nr": g["nr"], "behalten": [g["behalten"]], "weg": [b["id"] for b in g["bilder"] if not b["behalten"]]}]})
    assert len(list(qu.rglob("*.jpg"))) == 2
    c.post("/api/index/start")
    warte(lambda: not c.get("/api/status").json()["index"]["laeuft"])
    assert not [b for b in c.get("/api/bilder").json()["bilder"] if "_Dubletten" in b["pfad"]]


def test_gruppe_ohne_behalten_abgelehnt(dub):
    c, _ = dub
    g = suchen(c)["ergebnis"]["gruppen"][0]
    r = c.post("/api/dubletten/bereinigen", json={"gruppen": [{"behalten": [], "weg": [b["id"] for b in g["bilder"]]}]})
    assert r.status_code == 400
    assert all(Path(b["pfad"]).exists() for b in g["bilder"])


def test_xmp_wandert_mit(dub, heim):
    c, q = dub
    g = suchen(c)["ergebnis"]["gruppen"][0]
    kopie = next(b for b in g["bilder"] if b["name"] == "IMG_0001 - Kopie.jpg")
    xmp = Path(kopie["pfad"] + ".xmp")
    xmp.write_text("<x/>", "utf-8")
    qu = heim / "q2"
    c.post("/api/dubletten/bereinigen", json={"quarantaene": str(qu), "gruppen": [
        {"behalten": [g["behalten"]], "weg": [kopie["id"]]}]})
    assert not xmp.exists()
    assert [p.name for p in qu.rglob("*.xmp")] == ["IMG_0001 - Kopie.jpg.xmp"]


def test_einfarbige_bilder_nur_exakt(tmp_path):
    assert dubletten.dhash(Image.new("RGB", (100, 80), (0, 0, 0))) == dubletten.KEIN_HASH
    img = Image.open(foto(tmp_path / "x.jpg", 3))
    h = dubletten.dhash(img)
    assert len(h) == 32
    a, b = dubletten._bits([h, dubletten.dhash(img.resize((200, 150)))])
    assert int(np.abs(a - b).sum()) <= dubletten.GRENZE_SICHER


def test_quarantaene_pfad_windows():
    ziel = dubletten._quarantaene_ziel(Path("Q"), "lauf", Path("D:\\Fotos\\2023\\a.jpg"))
    assert ziel.parts[:2] == ("Q", "lauf")
    assert ziel.name.endswith("a.jpg")


def _info_datei(pfad: Path, original: str, zeit: int):
    roh = original.encode("utf-16-le") + b"\0\0"
    pfad.write_bytes(struct.pack("<qqqi", 2, 123, zeit, len(roh) // 2) + roh)


def test_papierkorb_herausholen(tmp_path):
    korb = tmp_path / "korb"
    korb.mkdir()
    ziel = tmp_path / "fotos" / "a.jpg"
    _info_datei(korb / "$IABC.jpg", str(ziel), 10_000_000)
    (korb / "$RABC.jpg").write_bytes(b"alt")
    _info_datei(korb / "$IDEF.jpg", str(ziel), 20_000_000)  # später gelöscht: das ist die gesuchte
    (korb / "$RDEF.jpg").write_bytes(b"neu")
    assert papierkorb.lies_info(korb / "$IDEF.jpg") == (str(ziel), 2.0)
    assert papierkorb.herausholen(ziel, [korb])
    assert ziel.read_bytes() == b"neu"
    assert not (korb / "$IDEF.jpg").exists()
    assert not papierkorb.herausholen(ziel, [korb])  # Platz belegt


def test_papierkorb_ausserhalb_windows_verstaendlicher_fehler(dub, monkeypatch):
    c, _ = dub
    monkeypatch.setattr(papierkorb, "verfuegbar", lambda: False)
    g = suchen(c)["ergebnis"]["gruppen"][0]
    r = c.post("/api/dubletten/bereinigen", json={"ziel": "papierkorb", "gruppen": [
        {"behalten": [g["behalten"]], "weg": [b["id"] for b in g["bilder"] if not b["behalten"]]}]})
    assert r.status_code == 400 and "Windows" in r.json()["detail"]
    c.post("/api/einstellungen", json={"dubletten": {"ziel": "quarantaene"}})


def test_papierkorb_rueckgaengig(tmp_path, monkeypatch):
    from imgsortsearch import sortieren
    prot = tmp_path / "p.jsonl"
    ziel = tmp_path / "a.jpg"
    prot.write_text('{"op": "papierkorb", "quelle": "%s", "ziel": "%s"}\n' % (ziel.as_posix(), ziel.as_posix()), "utf-8")
    geholt = []
    monkeypatch.setattr(papierkorb, "herausholen", lambda p: geholt.append(p) or True)
    r = sortieren.rueckgaengig(prot, echt=True)
    assert r["anzahl"] == 1 and geholt == [Path(ziel.as_posix())]


def test_kopie_ohne_exif_verbindet_keine_serienbilder(zustand, heim):
    q = heim / "serie"
    foto(q / "s1.jpg", 9, datum="2023:01:01 10:00:00")
    foto(q / "s2.jpg", 9, datum="2023:01:01 10:00:20")  # gleicher Inhalt, andere Aufnahme
    with Image.open(q / "s1.jpg") as img:
        img.resize((400, 300)).save(q / "wa.jpg", quality=60)
    zustand.m._clip = FarbClip()
    zustand.e.aktualisieren({"quellen": [str(q)]})
    c = TestClient(erstelle_app(zustand), headers={"X-ImgSortSearch": "1"})
    c.post("/api/index/start")
    warte(lambda: not c.get("/api/status").json()["index"]["laeuft"])
    for g in suchen(c)["ergebnis"]["gruppen"]:
        assert not {"s1.jpg", "s2.jpg"} <= {b["name"] for b in g["bilder"]}
