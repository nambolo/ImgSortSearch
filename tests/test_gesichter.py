"""Gesichter: Gruppieren, Korrekturen, Modellwechsel (mit künstlichen ArcFace-ähnlichen Vektoren)."""
import os
from pathlib import Path

import numpy as np
import pytest

from imgsortsearch import modelle, personen
from imgsortsearch.datenbank import Datenbank, vektor_zu_blob
from imgsortsearch.modelle import GESICHT_KENNUNG

DIM = 512


def _person(rng, n, streuung=0.8):
    """n Gesichter einer Person: paarweise Ähnlichkeit etwa 0,5-0,75 wie bei ArcFace."""
    zentrum = rng.normal(size=DIM)
    zentrum /= np.linalg.norm(zentrum)
    erg = []
    for _ in range(n):
        s = streuung * rng.uniform(0.75, 1.25)
        v = zentrum + rng.normal(size=DIM) * s / np.sqrt(DIM)
        erg.append(v / np.linalg.norm(v))
    return erg


@pytest.fixture
def db(tmp_path):
    return Datenbank(tmp_path / "t.sqlite")


def _gesicht(v, db, vektor, person=None, fest=0, q=0.9, modell=GESICHT_KENNUNG):
    bid = v.execute("INSERT INTO bilder (pfad) VALUES (?)", (f"b{np.random.rand()}",)).lastrowid
    return v.execute("INSERT INTO gesichter (bild_id, x, y, w, h, score, qualitaet, modell, vektor, person_id, "
                     "person_fest) VALUES (?,0,0,0.1,0.1,0.9,?,?,?,?,?)",
                     (bid, q, modell, vektor_zu_blob(vektor), person, fest)).lastrowid


def _gruppen(db):
    erg = {}
    for z in db.abfrage("SELECT id, person_id FROM gesichter"):
        erg.setdefault(z["person_id"], set()).add(z["id"])
    return erg


def test_gruppieren_trennt_personen_und_haelt_sie_zusammen(db):
    rng = np.random.default_rng(1)
    ids = {}
    with db.verbindung() as v:
        for name, n in (("a", 40), ("b", 25), ("c", 8)):
            ids[name] = {_gesicht(v, db, x) for x in _person(rng, n)}
        # Einzelgesichter fremder Leute bilden keine Gruppen
        fremde = {_gesicht(v, db, x) for k in range(15) for x in _person(rng, 1)}
    personen.neu_gruppieren(db)
    g = _gruppen(db)
    for name, menge in ids.items():
        pids = {pid for pid, m in g.items() if m & menge}
        assert len(pids) == 1 and None not in pids, f"Person {name} ist auf {pids} verteilt"
        assert g[pids.pop()] == menge, f"fremde Gesichter bei Person {name}"
    assert fremde <= g.get(None, set())


def test_unscharfe_gesichter_bilden_keine_neuen_gruppen(db):
    rng = np.random.default_rng(2)
    with db.verbindung() as v:
        for x in _person(rng, 5):
            _gesicht(v, db, x, q=0.1)
    personen.neu_gruppieren(db)
    assert set(_gruppen(db)) == {None}


def test_benannte_person_wird_von_fremden_gesichtern_bereinigt(db):
    """Das alte Modell hat zu Marlena fremde Gesichter gepackt: der Name bleibt, die Fremden fliegen raus."""
    rng = np.random.default_rng(3)
    with db.verbindung() as v:
        pid = v.execute("INSERT INTO personen (name) VALUES ('Marlena')").lastrowid
        marlena = {_gesicht(v, db, x, person=pid) for x in _person(rng, 6)}
        fremd = {_gesicht(v, db, x, person=pid) for x in _person(rng, 4)}
        einzeln = {_gesicht(v, db, x, person=pid) for k in range(3) for x in _person(rng, 1)}
    personen.neu_gruppieren(db)
    g = _gruppen(db)
    assert g[pid] == marlena
    # die fremde Person bildet eine eigene Gruppe, Einzelne bleiben frei
    andere = [m for p, m in g.items() if p not in (pid, None)]
    assert andere == [fremd]
    assert einzeln <= g[None]


def test_nicht_diese_person_wirkt_auch_auf_aehnliche_gesichter(db):
    rng = np.random.default_rng(4)
    a = _person(rng, 10)
    # b ähnelt a stark (Geschwister): Zentrum zwischen beiden
    b = [x / np.linalg.norm(x) for x in (0.75 * np.array(a[:4]) + 0.25 * np.array(_person(rng, 4)))]
    with db.verbindung() as v:
        pid = v.execute("INSERT INTO personen (name, fest) VALUES ('Anna', 1)").lastrowid
        anna = [_gesicht(v, db, x, person=pid, fest=1) for x in a[4:]]
        geschwister = [_gesicht(v, db, x) for x in b]
    personen.neu_gruppieren(db)
    zugeordnet = [g for g in geschwister if db.eins("SELECT person_id FROM gesichter WHERE id=?", (g,))["person_id"] == pid]
    assert zugeordnet, "Testaufbau: ähnliche Gesichter sollten zunächst bei Anna landen"
    personen.nicht_diese_person(db, zugeordnet[:1])
    personen.neu_gruppieren(db)
    # das abgelehnte Gesicht kommt nie wieder zu Anna
    assert db.eins("SELECT person_id FROM gesichter WHERE id=?", (zugeordnet[0],))["person_id"] != pid
    assert all(db.eins("SELECT person_id FROM gesichter WHERE id=?", (g,))["person_id"] == pid for g in anna)


def test_kein_gesicht_und_zuweisen(db):
    rng = np.random.default_rng(5)
    with db.verbindung() as v:
        ids = [_gesicht(v, db, x) for x in _person(rng, 6)]
        statue = _gesicht(v, db, _person(rng, 1)[0])
    personen.neu_gruppieren(db)
    pid = db.eins("SELECT person_id FROM gesichter WHERE id=?", (ids[0],))["person_id"]
    personen.kein_gesicht(db, [ids[0]])
    assert db.eins("SELECT ignoriert, person_id FROM gesichter WHERE id=?", (ids[0],))["ignoriert"] == 1
    personen.neu_gruppieren(db)
    assert db.eins("SELECT person_id FROM gesichter WHERE id=?", (ids[0],))["person_id"] is None
    assert personen.personen_liste(db, min_anzahl=1)[0]["bilder"] == 5
    # Einzelgesicht von Hand einer neuen benannten Person zuweisen: bleibt auch nach Neugruppieren
    neu = personen.zuweisen(db, [statue], name="Opa")
    personen.neu_gruppieren(db)
    assert db.eins("SELECT person_id, person_fest FROM gesichter WHERE id=?", (statue,))["person_id"] == neu
    assert {p["name"] for p in personen.personen_liste(db, min_anzahl=1)} >= {"Opa"}
    # und dieselbe Person anderer Gruppe: Zuweisen in die bestehende Gruppe
    personen.zuweisen(db, [statue], person_id=pid)
    assert db.eins("SELECT person_id FROM gesichter WHERE id=?", (statue,))["person_id"] == pid


def test_personen_ansicht_zeigt_unsicherste_zuerst(db):
    rng = np.random.default_rng(6)
    a = _person(rng, 8)
    with db.verbindung() as v:
        pid = v.execute("INSERT INTO personen (name) VALUES ('Anna')").lastrowid
        for x in a:
            _gesicht(v, db, x, person=pid)
        fremd = _gesicht(v, db, _person(rng, 1)[0], person=pid)
    erste = personen.person_gesichter(db, pid)[0]
    assert erste["id"] == fremd and erste["aehnlichkeit"] < 0.3


def test_gruppen_ids_bleiben_beim_neugruppieren_stabil(db):
    rng = np.random.default_rng(7)
    with db.verbindung() as v:
        for x in _person(rng, 10) + _person(rng, 10):
            _gesicht(v, db, x)
    personen.neu_gruppieren(db)
    vorher = _gruppen(db)
    personen.neu_gruppieren(db)
    assert _gruppen(db) == vorher


def test_suche_ignoriert_alte_modelle_und_fremde(db):
    rng = np.random.default_rng(8)
    a, b = _person(rng, 3), _person(rng, 1)
    with db.verbindung() as v:
        treffer = [_gesicht(v, db, x) for x in a[1:]]
        _gesicht(v, db, b[0])
        v.execute("INSERT INTO bilder (pfad) VALUES ('alt')")
        v.execute("INSERT INTO gesichter (bild_id, vektor, modell) VALUES (last_insert_rowid(), ?, NULL)",
                  (vektor_zu_blob(np.ones(128)),))
    bilder = {b for b, _ in personen.aehnliche_gesichter(db, a[0])}
    erwartet = {db.eins("SELECT bild_id FROM gesichter WHERE id=?", (t,))["bild_id"] for t in treffer}
    assert bilder == erwartet


# ------------------------------------------------------------------ Modellwechsel und Neuanalyse

def test_modellwechsel_plant_nur_den_gesichtsschritt_neu(db):
    with db.verbindung() as v:
        v.execute("INSERT INTO bilder (pfad, schritt_clip, schritt_ocr, schritt_gesichter, schritt_beschreibung) "
                  "VALUES ('x', 1, 1, 1, 1)")
    assert personen.modellwechsel_pruefen(db) == 1
    r = db.eins("SELECT * FROM bilder")
    assert (r["schritt_clip"], r["schritt_ocr"], r["schritt_gesichter"], r["schritt_beschreibung"]) == (1, 1, 0, 1)
    with db.verbindung() as v:
        v.execute("UPDATE bilder SET schritt_gesichter=1")
    assert personen.modellwechsel_pruefen(db) == 0  # nur einmal


def test_neuanalyse_behaelt_namen_korrekturen_und_kein_gesicht(zustand):
    from conftest import _einheit
    idx = zustand.indexer
    db = zustand.db
    with db.verbindung() as v:
        bid = v.execute("INSERT INTO bilder (pfad) VALUES ('p.jpg')").lastrowid
        anna = v.execute("INSERT INTO personen (name) VALUES ('Anna')").lastrowid
        auto = v.execute("INSERT INTO personen (name) VALUES (NULL)").lastrowid
        alt = [v.execute("INSERT INTO gesichter (bild_id, x, y, w, h, vektor, person_id, person_fest, ignoriert) "
                         "VALUES (?,?,?,?,?,?,?,?,?)", (bid, *box, vektor_zu_blob(np.ones(128)), pid, 0, ign)).lastrowid
               for box, pid, ign in (((0.1, 0.1, 0.2, 0.2), anna, 0), ((0.5, 0.1, 0.2, 0.2), auto, 0),
                                     ((0.1, 0.6, 0.2, 0.2), None, 1))]
        v.execute("INSERT INTO gesicht_abgelehnt VALUES (?, ?)", (alt[1], anna))
    neu = [{"x": 105, "y": 95, "w": 200, "h": 210, "score": 0.9, "qualitaet": 0.9, "vektor": _einheit("a")},
           {"x": 500, "y": 100, "w": 190, "h": 200, "score": 0.9, "qualitaet": 0.9, "vektor": _einheit("b")},
           {"x": 100, "y": 600, "w": 200, "h": 200, "score": 0.9, "qualitaet": 0.9, "vektor": _einheit("c")},
           {"x": 800, "y": 800, "w": 100, "h": 100, "score": 0.9, "qualitaet": 0.9, "vektor": _einheit("d")}]
    idx._gesichter_speichern(bid, neu, 1000, 1000, GESICHT_KENNUNG)
    g = db.abfrage("SELECT * FROM gesichter WHERE bild_id=? ORDER BY y, x", (bid,))
    assert [(r["person_id"], r["ignoriert"], r["modell"]) for r in g] == [
        (anna, 0, GESICHT_KENNUNG), (None, 0, GESICHT_KENNUNG), (None, 1, GESICHT_KENNUNG), (None, 0, GESICHT_KENNUNG)]
    # Ablehnung wandert mit; automatische Gruppe nicht (wird neu gebildet)
    b_neu = [r for r in g if abs(r["x"] - 0.5) < 0.01][0]
    assert db.eins("SELECT person_id FROM gesicht_abgelehnt WHERE gesicht_id=?", (b_neu["id"],))["person_id"] == anna
    assert db.eins("SELECT anzahl_gesichter FROM bilder WHERE id=?", (bid,))["anzahl_gesichter"] == 3


# ------------------------------------------------------------------ Modellbausteine

def test_ausrichtung_findet_bekannte_transformation():
    winkel, skala, versatz = np.radians(17), 1.7, np.array([12.0, -5.0])
    r = skala * np.array([[np.cos(winkel), -np.sin(winkel)], [np.sin(winkel), np.cos(winkel)]])
    quelle = (modelle.ARCFACE_PUNKTE - versatz) @ np.linalg.inv(r).T
    m = modelle._aehnlichkeits_transformation(quelle.astype(np.float32), modelle.ARCFACE_PUNKTE)
    zurueck = quelle @ m[:, :2].T + m[:, 2]
    assert np.allclose(zurueck, modelle.ARCFACE_PUNKTE, atol=1e-3)


def test_qualitaet_bestraft_profil_klein_unscharf():
    gut = modelle.gesichts_qualitaet(0.85, 150, 0.95, 300)
    assert gut > 0.9
    assert modelle.gesichts_qualitaet(0.85, 150, 0.1, 300) < 0.4      # Profil
    assert modelle.gesichts_qualitaet(0.85, 20, 0.95, 300) < 0.45     # winzig
    assert modelle.gesichts_qualitaet(0.85, 150, 0.95, 10) < 0.6      # unscharf
    assert modelle.gesichts_qualitaet(0.55, 150, 0.95, 300) < 0.2     # unsicher
    frontal = np.array([[38, 52], [74, 52], [56, 72], [42, 92], [70, 92]], dtype=float)
    profil = frontal.copy()
    profil[2, 0] = 80
    assert modelle.GesichtsModell._frontal(frontal) > 0.9 > 0.2 > modelle.GesichtsModell._frontal(profil)


ORDNER = os.environ.get("IMGSORTSEARCH_GESICHT_TESTBILDER")


@pytest.mark.skipif(not ORDNER, reason="echte Testfotos/Modelle nur lokal (IMGSORTSEARCH_GESICHT_TESTBILDER)")
def test_echtes_modell_trennt_personen():
    """Ordner mit Unterordnern je Person (Fotos). Gleiche Person > 0,45, verschiedene < 0,3."""
    m = modelle.GesichtsModell()
    vek = {}
    for p in sorted(Path(ORDNER).iterdir()):
        if p.is_dir():
            for f in sorted(p.iterdir()):
                gef = m.finde(modelle.bild_laden(f, max_kante=2000))
                if gef:
                    vek.setdefault(p.name, []).append(max(gef, key=lambda g: g["w"])["vektor"])
    namen = list(vek)
    for i, a in enumerate(namen):
        for x in range(len(vek[a])):
            for y in range(x + 1, len(vek[a])):
                assert vek[a][x] @ vek[a][y] > 0.45
        for b in namen[i + 1:]:
            assert max(float(x @ y) for x in vek[a] for y in vek[b]) < 0.3


def test_zusammengefuehrte_personen_bleiben_beim_neugruppieren_zusammen(db):
    """Dieselbe Person sieht in zwei Gruppen verschieden aus (Kind/Erwachsen): nach dem Zusammenführen
    darf der nächste Indexlauf (Neugruppieren) die kleinere Gruppe nicht wieder abtrennen."""
    rng = np.random.default_rng(11)
    with db.verbindung() as v:
        a = {_gesicht(v, db, x) for x in _person(rng, 12)}
        b = {_gesicht(v, db, x) for x in _person(rng, 7)}
    personen.neu_gruppieren(db)
    g = _gruppen(db)
    pa = next(p for p, m in g.items() if m == a)
    pb = next(p for p, m in g.items() if m == b)
    personen.umbenennen(db, pa, "Zoë")
    personen.zusammenfuehren(db, pa, [pb])
    for _ in range(2):
        personen.neu_gruppieren(db)
        assert _gruppen(db)[pa] == a | b
    # neue Gesichter beider Erscheinungsbilder kommen zur Person
    rng3 = np.random.default_rng(11)  # gleiches Zentrum wie Gruppe a
    zentrum_a = rng3.normal(size=DIM)
    with db.verbindung() as v:
        neu = _gesicht(v, db, zentrum_a / np.linalg.norm(zentrum_a))
    personen.neu_gruppieren(db)
    assert neu in _gruppen(db)[pa]
    # Umbenennen auf einen vorhandenen Namen führt ebenfalls dauerhaft zusammen
    rng4 = np.random.default_rng(12)
    with db.verbindung() as v:
        c = {_gesicht(v, db, x) for x in _person(rng4, 6)}
    personen.neu_gruppieren(db)
    pc = next(p for p, m in _gruppen(db).items() if m == c)
    assert personen.umbenennen(db, pc, "Zoë") == pa
    personen.neu_gruppieren(db)
    assert c <= _gruppen(db)[pa]


def test_zusammenfuehren_mit_fehler_bleibt_korrigierbar(db):
    rng = np.random.default_rng(13)
    with db.verbindung() as v:
        a = {_gesicht(v, db, x) for x in _person(rng, 8)}
        b = {_gesicht(v, db, x) for x in _person(rng, 8)}
    personen.neu_gruppieren(db)
    g = _gruppen(db)
    pa = next(p for p, m in g.items() if m == a)
    pb = next(p for p, m in g.items() if m == b)
    personen.zusammenfuehren(db, pa, [pb])
    personen.nicht_diese_person(db, sorted(b))  # versehentlich zusammengeführt: alles von B wieder raus
    personen.neu_gruppieren(db)
    g = _gruppen(db)
    assert g[pa] == a and not (b & g[pa])


def test_zusammenfuehren_behaelt_den_namen(db):
    """Unbenannte Gruppe als Ziel gewählt: der Name der anderen Gruppe darf nicht verloren gehen."""
    rng = np.random.default_rng(11)
    a = _person(rng, 6)
    with db.verbindung() as v:
        anna = v.execute("INSERT INTO personen (name) VALUES ('Anna')").lastrowid
        unbenannt = v.execute("INSERT INTO personen (name) VALUES (NULL)").lastrowid
        for x in a[:3]:
            _gesicht(v, db, x, person=anna)
        for x in a[3:]:
            _gesicht(v, db, x, person=unbenannt)
    ziel = personen.zusammenfuehren(db, unbenannt, [anna])
    assert ziel == anna
    assert db.eins("SELECT COUNT(*) n FROM gesichter WHERE person_id=?", (anna,))["n"] == 6
    assert db.eins("SELECT id FROM personen WHERE id=?", (unbenannt,)) is None


def _zwei_ansichten(rng, n, naehe=0.7):
    """Eine Person in zwei Varianten (z. B. mit/ohne Brille): Zentren mit Kosinus `naehe`."""
    c1 = rng.normal(size=DIM)
    c1 /= np.linalg.norm(c1)
    r = rng.normal(size=DIM)
    r -= (r @ c1) * c1
    r /= np.linalg.norm(r)
    c2 = naehe * c1 + np.sqrt(1 - naehe ** 2) * r
    erg = []
    for c in (c1, c2):
        for _ in range(n):
            v = c + rng.normal(size=DIM) * 0.8 / np.sqrt(DIM)
            erg.append(v / np.linalg.norm(v))
    return erg


def test_teilgruppen_derselben_person_werden_vereint(db):
    rng = np.random.default_rng(12)
    with db.verbindung() as v:
        eine = {_gesicht(v, db, x) for x in _zwei_ansichten(rng, 12)}
        andere = {_gesicht(v, db, x) for x in _person(rng, 12)}
    # ohne Vereinen zerfiele die Person (Kontrolle des Testaufbaus)
    mat = np.stack([blob for blob in _zwei_ansichten(np.random.default_rng(12), 12)])
    assert len(personen.cluster(mat, 0.50)) >= 2
    personen.neu_gruppieren(db)
    g = _gruppen(db)
    assert eine in g.values() and andere in g.values()


def test_neue_teilgruppe_kommt_zur_benannten_person(db):
    rng = np.random.default_rng(13)
    faces = _zwei_ansichten(rng, 10)
    with db.verbindung() as v:
        anna = v.execute("INSERT INTO personen (name) VALUES ('Anna')").lastrowid
        for x in faces[:10]:
            _gesicht(v, db, x, person=anna, fest=1)
        neu = {_gesicht(v, db, x) for x in faces[10:]}
    personen.neu_gruppieren(db)
    assert neu <= _gruppen(db)[anna]


def test_vorschlag_und_nein_merken(db):
    rng = np.random.default_rng(14)
    faces = _zwei_ansichten(rng, 8, naehe=0.52)  # zu unsicher für automatisch, aber vorschlagswürdig
    with db.verbindung() as v:
        a = v.execute("INSERT INTO personen (name, fest) VALUES ('Anna', 1)").lastrowid
        b = v.execute("INSERT INTO personen (name, fest) VALUES (NULL, 1)").lastrowid
        for x in faces[:8]:
            _gesicht(v, db, x, person=a)
        for x in faces[8:]:
            _gesicht(v, db, x, person=b)
        for x in _person(rng, 8):
            _gesicht(v, db, x)
    personen.neu_gruppieren(db)
    vs = personen.vorschlaege(db)
    assert vs["offen"] == 1
    assert vs["gruppen"][0]["person"]["id"] == a  # die benannte Person ist der Anker
    assert [k["id"] for k in vs["gruppen"][0]["kandidaten"]] == [b]
    # Häkchen weg = "Nein": nie wieder fragen
    personen.vorschlag_anwenden(db, a, [], [b])
    assert personen.vorschlaege(db)["gruppen"] == []
    # von Hand trotzdem zusammenführen geht
    personen.zusammenfuehren(db, a, [b])
    assert db.eins("SELECT COUNT(*) n FROM gesichter WHERE person_id=?", (a,))["n"] == 16


def test_vorschlaege_je_person_gebuendelt_und_ohne_winzlinge(db):
    """Drei Teilgruppen einer Person: eine Frage mit zwei Kandidaten statt drei Paaren.
    Zwei Mini-Gruppen (2 Bilder) von Fremden werden gar nicht erst vorgeschlagen."""
    rng = np.random.default_rng(16)
    c = rng.normal(size=DIM)
    c /= np.linalg.norm(c)
    teile = []
    for _ in range(3):
        r = rng.normal(size=DIM)
        r -= (r @ c) * c
        r /= np.linalg.norm(r)
        teile.append(0.72 * c + np.sqrt(1 - 0.72 ** 2) * r)
    mini = _zwei_ansichten(rng, 2, naehe=0.6)
    with db.verbindung() as v:
        pids = []
        for k, t in enumerate(teile):
            pid = v.execute("INSERT INTO personen (name, fest) VALUES (NULL, 1)").lastrowid
            pids.append(pid)
            for _ in range(6 + k):
                x = t + rng.normal(size=DIM) * 0.8 / np.sqrt(DIM)
                _gesicht(v, db, x / np.linalg.norm(x), person=pid)
        for k in range(2):
            pid = v.execute("INSERT INTO personen (name, fest) VALUES (NULL, 1)").lastrowid
            for x in mini[2 * k:2 * k + 2]:
                _gesicht(v, db, x, person=pid)
    vs = personen.vorschlaege(db)
    assert len(vs["gruppen"]) == 1 and vs["offen"] == 2
    g = vs["gruppen"][0]
    assert g["person"]["id"] == pids[2]  # größte Teilgruppe ist der Anker
    assert {k["id"] for k in g["kandidaten"]} == set(pids[:2])
    personen.vorschlag_anwenden(db, pids[2], pids[:2], [])
    assert personen.vorschlaege(db)["gruppen"] == []
    assert db.eins("SELECT COUNT(*) n FROM gesichter WHERE person_id=?", (pids[2],))["n"] == 21


def test_vereinen_respektiert_verboten():
    rng = np.random.default_rng(15)
    mat = np.stack(_zwei_ansichten(rng, 5))
    gruppen = [np.arange(5), np.arange(5, 10)]
    assert len(personen.gruppen_vereinen(mat, gruppen, 0.55)) == 1
    assert len(personen.gruppen_vereinen(mat, gruppen, 0.55, lambda x, y: True)) == 2
