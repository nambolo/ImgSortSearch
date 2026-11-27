"""Personen: Gesichter gruppieren, benennen, per Referenzbild suchen."""
from __future__ import annotations

import numpy as np

from .datenbank import Datenbank, blob_zu_vektor

# SFace: gleiche Person meist ab Kosinus-Ähnlichkeit ~0,36
SUCH_SCHWELLE = 0.36


def _summen(db: Datenbank) -> dict[int, np.ndarray]:
    """Summe der Gesichtsvektoren je Person (nicht normiert, damit neue Gesichter richtig gewichtet werden)."""
    summen: dict[int, np.ndarray] = {}
    for z in db.abfrage("SELECT person_id, vektor FROM gesichter WHERE person_id IS NOT NULL"):
        v = blob_zu_vektor(z["vektor"])
        summen[z["person_id"]] = summen.get(z["person_id"], 0) + v
    return summen


def _zentren(db: Datenbank) -> tuple[list[int], np.ndarray]:
    summen = _summen(db)
    if not summen:
        return [], np.zeros((0, 128), dtype=np.float32)
    ids = list(summen)
    return ids, np.stack([_norm(summen[i]) for i in ids])


def _norm(v: np.ndarray) -> np.ndarray:
    return v / (np.linalg.norm(v) + 1e-9)


def neue_gesichter_zuordnen(db: Datenbank, schwelle: float = 0.40, min_score: float = 0.9) -> int:
    """Ordnet Gesichter ohne Person der ähnlichsten Person zu oder legt neue Gruppen an.

    Einfaches, schrittweises Verfahren: ausreichend für einige zehntausend Gesichter.
    Nur gut erkannte Gesichter (score) bilden neue Gruppen, damit nicht jede
    unscharfe Hintergrundperson eine eigene Gruppe wird."""
    offen = db.abfrage("SELECT id, bild_id, vektor, score FROM gesichter WHERE person_id IS NULL "
                       "ORDER BY score DESC")
    if not offen:
        return 0
    summen = _summen(db)
    ids = list(summen)
    mat = np.stack([_norm(summen[i]) for i in ids]) if ids else np.zeros((0, 128), dtype=np.float32)
    zuordnung = []
    for g in offen:
        v = blob_zu_vektor(g["vektor"])
        if len(ids):
            sims = mat @ v
            j = int(np.argmax(sims))
            if sims[j] >= schwelle:
                pid = ids[j]
                zuordnung.append((pid, g["id"]))
                summen[pid] = summen[pid] + v
                mat[j] = _norm(summen[pid])
                continue
        if (g["score"] or 0) < min_score:
            continue
        with db.verbindung() as con:
            pid = con.execute("INSERT INTO personen (name) VALUES (NULL)").lastrowid
        ids.append(pid)
        summen[pid] = v.copy()
        mat = np.vstack([mat, _norm(v)[None, :]]) if len(mat) else _norm(v)[None, :]
        zuordnung.append((pid, g["id"]))
    with db.verbindung() as con:
        con.executemany("UPDATE gesichter SET person_id=? WHERE id=?", zuordnung)
    # benannte Personen sollen sofort über die Textsuche gefunden werden
    gesicht_bild = {g["id"]: g["bild_id"] for g in offen}
    db.suchtext_aktualisieren_viele(gesicht_bild[gid] for _, gid in zuordnung)
    return len(zuordnung)


def _bilder_von(db: Datenbank, person_ids) -> list[int]:
    person_ids = list(person_ids)
    if not person_ids:
        return []
    return [z["bild_id"] for z in db.abfrage(
        f"SELECT DISTINCT bild_id FROM gesichter WHERE person_id IN ({','.join('?' * len(person_ids))})",
        person_ids)]


def personen_liste(db: Datenbank, min_anzahl: int = 2) -> list[dict]:
    zeilen = db.abfrage("""
        SELECT p.id, p.name, COUNT(DISTINCT g.bild_id) AS bilder,
               (SELECT g2.id FROM gesichter g2 WHERE g2.person_id = p.id ORDER BY g2.score DESC, g2.w DESC LIMIT 1)
               AS titel_gesicht
        FROM personen p JOIN gesichter g ON g.person_id = p.id
        GROUP BY p.id HAVING bilder >= ? OR p.name IS NOT NULL
        ORDER BY (p.name IS NULL), bilder DESC""", (min_anzahl,))
    return [dict(z) for z in zeilen]


def umbenennen(db: Datenbank, person_id: int, name: str) -> None:
    name = name.strip() or None
    with db.verbindung() as con:
        # gibt es schon eine Person mit diesem Namen: zusammenführen
        vorhanden = con.execute("SELECT id FROM personen WHERE name=? AND id<>?", (name, person_id)).fetchone() \
            if name else None
        if vorhanden:
            con.execute("UPDATE gesichter SET person_id=? WHERE person_id=?", (vorhanden[0], person_id))
            con.execute("DELETE FROM personen WHERE id=?", (person_id,))
            person_id = vorhanden[0]
        else:
            con.execute("UPDATE personen SET name=? WHERE id=?", (name, person_id))
    for z in db.abfrage("SELECT DISTINCT bild_id FROM gesichter WHERE person_id=?", (person_id,)):
        db.suchtext_aktualisieren(z["bild_id"])


def zusammenfuehren(db: Datenbank, ziel_id: int, quell_ids: list[int]) -> None:
    with db.verbindung() as con:
        for q in quell_ids:
            if q != ziel_id:
                con.execute("UPDATE gesichter SET person_id=? WHERE person_id=?", (ziel_id, q))
                con.execute("DELETE FROM personen WHERE id=?", (q,))
    db.suchtext_aktualisieren_viele(_bilder_von(db, [ziel_id]))  # Name gilt jetzt auch für diese Bilder


def gesicht_entfernen(db: Datenbank, gesicht_id: int) -> None:
    """Gesicht passt nicht zur Person: in eine eigene Gruppe verschieben."""
    r = db.eins("SELECT bild_id FROM gesichter WHERE id=?", (gesicht_id,))
    if not r:
        return
    with db.verbindung() as con:
        pid = con.execute("INSERT INTO personen (name) VALUES (NULL)").lastrowid
        con.execute("UPDATE gesichter SET person_id=?, person_fest=1 WHERE id=?", (pid, gesicht_id))
    db.suchtext_aktualisieren(r["bild_id"])  # alter Name passt nicht mehr


def aehnliche_gesichter(db: Datenbank, referenz: np.ndarray, schwelle: float = SUCH_SCHWELLE,
                        limit: int = 2000) -> list[tuple[int, float]]:
    """Bilder (bild_id, Ähnlichkeit), auf denen ein Gesicht der Referenz ähnelt. Beste zuerst."""
    gids, bids, mat = db.gesicht_matrix()
    if not len(gids):
        return []
    referenz = referenz / (np.linalg.norm(referenz) + 1e-9)
    sims = mat @ referenz
    beste: dict[int, float] = {}
    for b, s in zip(bids, sims):
        if s >= schwelle and s > beste.get(int(b), -1):
            beste[int(b)] = float(s)
    return sorted(beste.items(), key=lambda kv: -kv[1])[:limit]
