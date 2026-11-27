"""Personen: Gesichter gruppieren, benennen, korrigieren, per Referenzbild suchen.

Ablauf beim Gruppieren (``neu_gruppieren``):

1. Personen, die der Nutzer bearbeitet hat (Name, Zusammenführen, Korrektur), bleiben. Ihre
   automatisch zugeordneten Gesichter werden geprüft: Was nicht zum Kern der Person passt, fliegt raus.
2. Automatisch gebildete, unbearbeitete Gruppen werden aufgelöst und neu gebildet.
3. Freie Gesichter kommen zur ähnlichsten bekannten Person, wenn sie klar genug passen und nicht
   abgelehnt wurden ("Nicht diese Person").
4. Der Rest wird geclustert (Chinese Whispers auf einem Ähnlichkeitsgraphen). Nur gute Gesichter
   (scharf, groß genug, nicht im Profil) dürfen neue Gruppen bilden.

Vom Nutzer bestätigte Zuordnungen (``person_fest``) werden nie verändert.
"""
from __future__ import annotations

import numpy as np

from .datenbank import Datenbank, blob_zu_vektor
from .modelle import GESICHT_KENNUNG

# Kosinus-Ähnlichkeit für ArcFace (w600k_r50). Gemessen: gleiche Person meist 0,55–0,9,
# verschiedene Personen fast immer unter 0,2.
STANDARD = {
    "gruppe": 0.50,        # Kante im Graphen: zwei Gesichter zeigen dieselbe Person
    "zuordnen": 0.45,      # Gesicht -> Personenzentrum (gute Gesichter)
    "zuordnen_schwach": 0.55,  # dasselbe für unscharfe/kleine/seitliche Gesichter
    "abstand": 0.08,       # so viel besser als die zweitbeste Person muss es passen
    "bleiben": 0.35,       # darunter fliegt ein automatisch zugeordnetes Gesicht aus einer Person
    "suche": 0.40,         # Suche per Referenzfoto
    "min_qualitaet": 0.35,  # ab hier darf ein Gesicht neue Gruppen bilden
    "vereinen": 0.55,      # Gruppenzentren so ähnlich: automatisch dieselbe Person
    "vorschlag": 0.45,     # ab hier "Ist das dieselbe Person?" vorschlagen
}
# alter Name des Werts, als die Grenze noch fürs alte Modell (SFace) galt
SUCH_SCHWELLE = STANDARD["suche"]


def schwellen(einstellungen: dict | None = None) -> dict:
    s = dict(STANDARD)
    for k, v in ((einstellungen or {}).get("gesichter") or {}).items():
        if k in s and isinstance(v, (int, float)):
            s[k] = float(v)
    return s


def _norm(v: np.ndarray) -> np.ndarray:
    return v / (np.linalg.norm(v, axis=-1, keepdims=True) + 1e-9)


# ------------------------------------------------------------------ Modellwechsel

def modellwechsel_pruefen(db: Datenbank, kennung: str = GESICHT_KENNUNG) -> int:
    """Wurden Gesichter mit einem anderen Modell berechnet, nur den Gesichtsschritt neu einplanen.

    Bildinhalt, Text und Beschreibungen bleiben. Die alten Gesichter (und ihre Personen) bleiben
    bis zur Neuberechnung des jeweiligen Bildes sichtbar. Liefert die Zahl eingeplanter Bilder."""
    r = db.eins("SELECT wert FROM meta WHERE schluessel='gesicht_modell'")
    if r and r["wert"] == kennung:
        return 0
    with db.verbindung() as v:
        n = v.execute("UPDATE bilder SET schritt_gesichter=0 WHERE schritt_gesichter=1").rowcount
        v.execute("INSERT OR REPLACE INTO meta VALUES ('gesicht_modell', ?)", (kennung,))
    return n


def _iou(a, b) -> float:
    x1, y1 = max(a[0], b[0]), max(a[1], b[1])
    x2, y2 = min(a[0] + a[2], b[0] + b[2]), min(a[1] + a[3], b[1] + b[3])
    schnitt = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    return schnitt / (a[2] * a[3] + b[2] * b[3] - schnitt + 1e-9)


def uebernahme_finden(alte: list[dict], neue_boxen: list[tuple]) -> list[dict | None]:
    """Für jede neue Box das alte Gesicht an derselben Stelle (oder None).

    So bleiben Namen, Korrekturen und "kein Gesicht"-Markierungen erhalten, wenn ein Bild neu
    analysiert wird."""
    paare = sorted(((_iou((a["x"], a["y"], a["w"], a["h"]), b), i, j)
                    for i, b in enumerate(neue_boxen) for j, a in enumerate(alte)), reverse=True)
    erg: list[dict | None] = [None] * len(neue_boxen)
    benutzt = set()
    for iou, i, j in paare:
        if iou < 0.3:
            break
        if erg[i] is None and j not in benutzt:
            erg[i] = alte[j]
            benutzt.add(j)
    return erg


# ------------------------------------------------------------------ Clustering

def _nachbarn(mat: np.ndarray, schwelle: float, k: int = 30, block: int = 1024):
    """Für jedes Gesicht die bis zu k ähnlichsten anderen über der Schwelle: Liste von (j, sim)."""
    n = len(mat)
    erg: list[list[tuple[int, float]]] = [[] for _ in range(n)]
    for s in range(0, n, block):
        sims = mat[s:s + block] @ mat.T
        for r in range(len(sims)):
            sims[r, s + r] = -1
        kk = min(k, n - 1)
        if kk <= 0:
            break
        top = np.argpartition(-sims, kk - 1, axis=1)[:, :kk]
        for r in range(len(sims)):
            js = top[r][sims[r, top[r]] >= schwelle]
            erg[s + r] = [(int(j), float(sims[r, j])) for j in js]
    return erg


def chinese_whispers(nachbarn, runden: int = 20, seed: int = 0) -> np.ndarray:
    """Graph-Clustering: jeder Knoten übernimmt die Gruppe mit dem höchsten Kantengewicht unter
    seinen Nachbarn. Neigt weniger als einfache Zusammenhangskomponenten dazu, zwei Personen über
    ein einzelnes zweideutiges Gesicht zu verketten."""
    n = len(nachbarn)
    label = np.arange(n)
    rng = np.random.default_rng(seed)
    # Kanten symmetrisch machen
    sym: list[dict[int, float]] = [dict() for _ in range(n)]
    for i, liste in enumerate(nachbarn):
        for j, w in liste:
            sym[i][j] = max(sym[i].get(j, 0.0), w)
            sym[j][i] = max(sym[j].get(i, 0.0), w)
    for _ in range(runden):
        geaendert = 0
        for i in rng.permutation(n):
            if not sym[i]:
                continue
            gewicht: dict[int, float] = {}
            for j, w in sym[i].items():
                gewicht[label[j]] = gewicht.get(label[j], 0.0) + w
            bestes = max(gewicht.items(), key=lambda kv: (kv[1], -kv[0]))[0]
            if bestes != label[i]:
                label[i] = bestes
                geaendert += 1
        if not geaendert:
            break
    return label


def cluster(mat: np.ndarray, schwelle: float, min_groesse: int = 2) -> list[list[int]]:
    """Gruppen (Indizes in mat) mit mindestens min_groesse Gesichtern, größte zuerst.
    Gesichter, die nicht zum Zentrum ihrer Gruppe passen, werden aussortiert."""
    if not len(mat):
        return []
    label = chinese_whispers(_nachbarn(mat, schwelle))
    gruppen: dict[int, list[int]] = {}
    for i, l in enumerate(label):
        gruppen.setdefault(int(l), []).append(i)
    erg = []
    for idx in gruppen.values():
        if len(idx) < min_groesse:
            continue
        zentrum = _norm(mat[idx].mean(0))
        idx = [i for i in idx if float(mat[i] @ zentrum) >= schwelle - 0.1]
        if len(idx) >= min_groesse:
            erg.append(idx)
    return sorted(erg, key=len, reverse=True)


def gruppen_vereinen(mat: np.ndarray, gruppen: list[np.ndarray], schwelle: float,
                     verboten=lambda a, b: False) -> list[np.ndarray]:
    """Vereint Gruppen, deren Zentren sich ähneln (ähnlichstes Paar zuerst, average linkage über Zentren).

    Das Graph-Clustering ist absichtlich streng, damit es keine verschiedenen Personen verkettet.
    Dadurch zerfällt dieselbe Person oft in Teilgruppen (Brille, Licht, Alter, Bart). Zentren
    mitteln das Rauschen einzelner Fotos heraus und sind deshalb viel verlässlicher vergleichbar."""
    gruppen = [np.asarray(g) for g in gruppen]
    if len(gruppen) < 2:
        return gruppen
    summen = np.stack([mat[g].sum(0) for g in gruppen])
    zentren = _norm(summen)
    aktiv = np.ones(len(gruppen), dtype=bool)
    mitglieder: list[list[int]] = [[k] for k in range(len(gruppen))]  # ursprüngliche Gruppen je Cluster
    sims = zentren @ zentren.T
    np.fill_diagonal(sims, -1)
    while True:
        k = int(np.argmax(sims))
        a, b = divmod(k, len(gruppen))
        if sims[a, b] < schwelle:
            break
        if any(verboten(x, y) for x in mitglieder[a] for y in mitglieder[b]):
            sims[a, b] = sims[b, a] = -1
            continue
        gruppen[a] = np.concatenate([gruppen[a], gruppen[b]])
        mitglieder[a] += mitglieder[b]
        summen[a] += summen[b]
        zentren[a] = _norm(summen[a])
        aktiv[b] = False
        neu = zentren @ zentren[a]
        neu[~aktiv] = -1
        neu[a] = -1
        sims[a, :] = neu
        sims[:, a] = neu
        sims[b, :] = -1
        sims[:, b] = -1
    return sorted([g for g, ok in zip(gruppen, aktiv) if ok], key=len, reverse=True)


def _verschieden(db: Datenbank) -> set[tuple[int, int]]:
    return {(z["a"], z["b"]) for z in db.abfrage("SELECT a, b FROM personen_verschieden")}


def _paar(a: int, b: int) -> tuple[int, int]:
    return (a, b) if a < b else (b, a)


# ------------------------------------------------------------------ Gruppieren

def _lade(db: Datenbank):
    zeilen = db.abfrage("SELECT id, bild_id, person_id, person_fest, COALESCE(qualitaet, score, 0) AS q, vektor "
                        "FROM gesichter WHERE ignoriert=0 AND modell=?", (GESICHT_KENNUNG,))
    if not zeilen:
        return None
    return {
        "id": np.array([z["id"] for z in zeilen], dtype=np.int64),
        "bild": np.array([z["bild_id"] for z in zeilen], dtype=np.int64),
        "person": np.array([z["person_id"] or 0 for z in zeilen], dtype=np.int64),
        "fest": np.array([bool(z["person_fest"]) for z in zeilen]),
        "q": np.array([z["q"] for z in zeilen], dtype=np.float32),
        "mat": _norm(np.stack([blob_zu_vektor(z["vektor"]) for z in zeilen])),
    }


def _abgelehnt(db: Datenbank) -> dict[int, set[int]]:
    erg: dict[int, set[int]] = {}
    for z in db.abfrage("SELECT gesicht_id, person_id FROM gesicht_abgelehnt"):
        erg.setdefault(z["gesicht_id"], set()).add(z["person_id"])
    return erg


def _kern(mat: np.ndarray, fest: np.ndarray, s: dict) -> tuple[np.ndarray, list[np.ndarray]]:
    """Maske der Gesichter einer Person, die zu ihrem Kern passen, und die Zentren des Kerns.

    Kern = bestätigte Gesichter, sonst die größte Untergruppe. So bleibt "Marlena" Marlena, auch
    wenn das alte Modell ein paar fremde Gesichter dazugepackt hat. Bestätigte Gesichter können
    mehrere Untergruppen bilden (z. B. zusammengeführt: dieselbe Person als Kind und als Erwachsene),
    dann zählt jede davon als Zentrum."""
    if fest.any():
        sub = mat[fest]
        gruppen = cluster(sub, s["gruppe"], min_groesse=1) if len(sub) > 1 else [[0]]
        zentren = [_norm(sub[g].mean(0)) for g in gruppen] or [_norm(sub.mean(0))]
    else:
        gruppen = cluster(mat, s["gruppe"], min_groesse=1) if len(mat) > 1 else [[0]]
        zentren = [_norm(mat[gruppen[0]].mean(0)) if gruppen else _norm(mat.mean(0))]
    naechstes = (mat @ np.stack(zentren).T)
    ok = fest | (naechstes.max(axis=1) >= s["bleiben"])
    if len(zentren) == 1:
        return ok, [_norm(mat[ok].mean(0))]
    zuordnung = naechstes.argmax(axis=1)
    return ok, [_norm(mat[ok & (zuordnung == k)].mean(0)) if (ok & (zuordnung == k)).any() else z
                for k, z in enumerate(zentren)]


def neu_gruppieren(db: Datenbank, einstellungen: dict | None = None) -> dict:
    """Ordnet alle Gesichter (des aktuellen Modells) Personen zu. Siehe Modulbeschreibung."""
    s = schwellen(einstellungen)
    d = _lade(db)
    if d is None:
        return {"zugeordnet": 0, "personen": 0}
    mat, person, fest, q = d["mat"], d["person"].copy(), d["fest"], d["q"]
    vorher = d["person"].copy()
    abgelehnt = _abgelehnt(db)
    index_von = {int(g): i for i, g in enumerate(d["id"])}

    bleibende = {z["id"] for z in db.abfrage("SELECT id FROM personen WHERE fest=1 OR name IS NOT NULL")}
    # 1./2. unbearbeitete Gruppen auflösen, bearbeitete bereinigen
    for i in np.where(~fest & (person > 0))[0]:
        if int(person[i]) not in bleibende:
            person[i] = 0
    zentren: dict[int, list[np.ndarray]] = {}
    for pid in bleibende:
        idx = np.where(person == pid)[0]
        if not len(idx):
            continue
        ok, zentren[pid] = _kern(mat[idx], fest[idx], s)
        person[idx[~ok]] = 0

    # Negativbeispiele: Gesichter, die der Nutzer bei einer Person abgelehnt hat
    negativ: dict[int, list[int]] = {}
    for gid, pids in abgelehnt.items():
        if gid in index_von:
            for pid in pids:
                negativ.setdefault(pid, []).append(index_von[gid])

    def zuordnen(kandidaten: np.ndarray, zentren: dict[int, list[np.ndarray]]) -> None:
        if not zentren or not len(kandidaten):
            return
        pids = list(zentren)
        # eine Person kann mehrere Zentren haben: es zählt das nächste
        spalte = np.array([k for k, p in enumerate(pids) for _ in zentren[p]])
        alle = mat[kandidaten] @ np.stack([z for p in pids for z in zentren[p]]).T
        sims = np.full((len(kandidaten), len(pids)), -1.0, dtype=np.float32)
        for k in range(len(pids)):
            sims[:, k] = alle[:, spalte == k].max(axis=1)
        for r, i in enumerate(kandidaten):
            gid = int(d["id"][i])
            zeile = sims[r].copy()
            for c, pid in enumerate(pids):
                if pid in abgelehnt.get(gid, ()):
                    zeile[c] = -1
            reihenfolge = np.argsort(-zeile)
            beste = reihenfolge[0]
            zweite = zeile[reihenfolge[1]] if len(reihenfolge) > 1 else -1
            grenze = s["zuordnen"] if q[i] >= s["min_qualitaet"] else s["zuordnen_schwach"]
            if zeile[beste] < grenze or zeile[beste] - zweite < s["abstand"]:
                continue
            pid = pids[beste]
            neg = negativ.get(pid)
            # ähnelt es einem abgelehnten Gesicht mehr als der Person selbst: nicht zuordnen
            if neg and float((mat[neg] @ mat[i]).max()) >= zeile[beste]:
                continue
            person[i] = pid

    # 3. freie Gesichter zu bekannten Personen
    zuordnen(np.where((person == 0) & ~fest)[0], zentren)

    # 4. Rest clustern: nur gute Gesichter bilden neue Gruppen
    frei = np.where((person == 0) & ~fest & (q >= s["min_qualitaet"]))[0]
    neue: list[np.ndarray] = [frei[g] for g in cluster(mat[frei], s["gruppe"])]
    verschieden = _verschieden(db)

    def alte_gruppe(idx: np.ndarray) -> int:
        alt = [int(p) for p in vorher[idx] if p]
        return max(set(alt), key=alt.count) if alt else 0

    # Teilgruppen derselben Person zusammenfassen (außer der Nutzer hat "verschieden" gesagt)
    alte = [alte_gruppe(g) for g in neue]
    neue = gruppen_vereinen(mat, neue, s["vereinen"],
                            lambda x, y: bool(alte[x] and alte[y] and _paar(alte[x], alte[y]) in verschieden))
    # passt eine ganze neue Gruppe klar zu einer bekannten Person: dazu
    if zentren and neue:
        pids = list(zentren)
        rest = []
        for idx in neue:
            z = _norm(mat[idx].mean(0))
            sim = np.array([max(float(z @ c) for c in zentren[p]) for p in pids])
            alt = alte_gruppe(idx)
            for c, pid in enumerate(pids):
                if alt and _paar(alt, pid) in verschieden:
                    sim[c] = -1
            reihe = np.argsort(-sim)
            zweite = sim[reihe[1]] if len(reihe) > 1 else -1
            if sim[reihe[0]] >= s["vereinen"] and sim[reihe[0]] - zweite >= s["abstand"]:
                person[idx] = pids[reihe[0]]
            else:
                rest.append(idx)
        neue = rest
    # bestehende automatische Gruppen-IDs weiterverwenden, wenn eine neue Gruppe überwiegend aus
    # ihnen besteht: so springen Gruppen beim Neugruppieren nicht ohne Grund um
    benutzt: set[int] = set(bleibende)
    neue_ids: list[int] = []
    for idx in neue:
        alt = [int(p) for p in vorher[idx] if p and int(p) not in benutzt]
        pid = max(set(alt), key=alt.count) if alt else 0
        if pid and alt.count(pid) * 2 < len(idx):
            pid = 0
        neue_ids.append(pid)
        if pid:
            benutzt.add(pid)
    with db.verbindung() as v:
        for k, (idx, pid) in enumerate(zip(neue, neue_ids)):
            if not pid:
                pid = v.execute("INSERT INTO personen (name) VALUES (NULL)").lastrowid
                neue_ids[k] = pid
            person[idx] = pid
    # schwache Gesichter dürfen sich neuen Gruppen anschließen
    neue_zentren = {pid: [_norm(mat[idx].mean(0))] for idx, pid in zip(neue, neue_ids)}
    zuordnen(np.where((person == 0) & ~fest)[0], neue_zentren)

    # speichern
    geaendert = np.where(person != vorher)[0]
    with db.verbindung() as v:
        v.executemany("UPDATE gesichter SET person_id=? WHERE id=?",
                      [(int(person[i]) or None, int(d["id"][i])) for i in geaendert])
        # leere, unbenannte Personen entfernen (Gesichter alter Modelle zählen noch mit)
        v.execute("DELETE FROM personen WHERE name IS NULL AND id NOT IN "
                  "(SELECT DISTINCT person_id FROM gesichter WHERE person_id IS NOT NULL)")
    db.suchtext_aktualisieren_viele({int(d["bild"][i]) for i in geaendert})
    return {"zugeordnet": int((person > 0).sum()), "geaendert": int(len(geaendert)),
            "personen": int(len(set(person[person > 0].tolist())))}


def vorschlaege(db: Datenbank, einstellungen: dict | None = None, limit: int = 8,
                je_person: int = 12) -> dict:
    """"Gehören diese Gruppen auch zu X?": je Person die Gruppen, die ihr ähneln.

    Jede Gruppe wird nur einmal gezeigt, bei der Person, der sie am meisten ähnelt (benannte und
    größere Personen bevorzugt). So muss man nicht Paar für Paar klicken. Nicht vorgeschlagen:
    zwei Personen mit Namen, Paare mit "Nein", und zwei winzige Gruppen (meist Fremde im Hintergrund)."""
    s = schwellen(einstellungen)
    zeilen = db.abfrage("SELECT g.person_id, g.vektor FROM gesichter g "
                        "WHERE g.person_id IS NOT NULL AND g.ignoriert=0 AND g.modell=?", (GESICHT_KENNUNG,))
    summen: dict[int, np.ndarray] = {}
    for z in zeilen:
        v = blob_zu_vektor(z["vektor"])
        summen[z["person_id"]] = summen.get(z["person_id"], 0) + v / (np.linalg.norm(v) + 1e-9)
    liste = {p["id"]: p for p in personen_liste(db, min_anzahl=1)}
    pids = [p for p in summen if p in liste and (liste[p]["bilder"] >= 2 or liste[p]["name"])]
    leer = {"gruppen": [], "offen": 0}
    if len(pids) < 2:
        return leer
    zentren = _norm(np.stack([summen[p] for p in pids]))
    sims = zentren @ zentren.T
    np.fill_diagonal(sims, -1)
    verschieden = _verschieden(db)

    def rang(p):  # wer ist "Anker": benannt vor unbenannt, dann mehr Bilder
        return (liste[p]["name"] is not None, liste[p]["bilder"], -p)

    anker: dict[int, list[tuple[float, int]]] = {}
    for j, b in enumerate(pids):
        if liste[b]["name"]:
            continue  # benannte Gruppen werden nicht anderen zugeschlagen, sie sind selbst Anker
        beste = None
        for i in np.argsort(-sims[:, j]):
            sim = float(sims[i, j])
            if sim < s["vorschlag"]:
                break
            a = pids[i]
            if rang(a) <= rang(b) or _paar(a, b) in verschieden:
                continue
            if max(liste[a]["bilder"], liste[b]["bilder"]) < 3:
                continue
            beste = (sim, a)
            break
        if beste:
            anker.setdefault(beste[1], []).append((beste[0], b))
    gruppen = []
    for a, kandidaten in anker.items():
        kandidaten.sort(reverse=True)
        gruppen.append({"person": liste[a],
                        "kandidaten": [dict(liste[b], aehnlichkeit=round(sim, 3)) for sim, b in kandidaten[:je_person]]})
    gruppen.sort(key=lambda g: (g["person"]["name"] is None, -len(g["kandidaten"]), -g["person"]["bilder"]))
    return {"gruppen": gruppen[:limit], "offen": sum(len(k) for k in anker.values())}


def vorschlag_anwenden(db: Datenbank, ziel: int, ja: list[int], nein: list[int]) -> int:
    """Angehakte Gruppen zur Person, die übrigen als "verschieden" merken (wird nie wieder gefragt)."""
    for b in nein:
        verschieden_merken(db, ziel, b)
    return zusammenfuehren(db, ziel, ja) if ja else ziel


def verschieden_merken(db: Datenbank, a: int, b: int) -> None:
    """Nutzer sagt: verschiedene Personen. Nie wieder vorschlagen oder automatisch vereinen."""
    with db.verbindung() as con:
        con.execute("INSERT OR IGNORE INTO personen_verschieden VALUES (?,?)", _paar(a, b))
        _festhalten(con, a)
        _festhalten(con, b)


def neue_gesichter_zuordnen(db: Datenbank, einstellungen: dict | None = None) -> int:
    """Nach einem Indexlauf: alles neu gruppieren (schnell genug für einige zehntausend Gesichter)."""
    return neu_gruppieren(db, einstellungen)["zugeordnet"]


# ------------------------------------------------------------------ Anzeigen

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
               (SELECT g2.id FROM gesichter g2 WHERE g2.person_id = p.id AND g2.ignoriert = 0
                ORDER BY g2.person_fest DESC, COALESCE(g2.qualitaet, 0) DESC, g2.score DESC, g2.w DESC LIMIT 1)
               AS titel_gesicht
        FROM personen p JOIN gesichter g ON g.person_id = p.id AND g.ignoriert = 0
        GROUP BY p.id HAVING bilder >= ? OR p.name IS NOT NULL
        ORDER BY (p.name IS NULL), p.name COLLATE NOCASE, bilder DESC""", (min_anzahl,))
    return [dict(z) for z in zeilen]


def person_gesichter(db: Datenbank, person_id: int, limit: int = 600) -> list[dict]:
    """Gesichter einer Person, die unsichersten zuerst (dort stecken die Fehler)."""
    zeilen = db.abfrage("SELECT id, bild_id, person_fest, modell, vektor FROM gesichter "
                        "WHERE person_id=? AND ignoriert=0", (person_id,))
    if not zeilen:
        return []
    aktuell = [z for z in zeilen if z["modell"] == GESICHT_KENNUNG]
    sims = {}
    if aktuell:
        mat = _norm(np.stack([blob_zu_vektor(z["vektor"]) for z in aktuell]))
        fest = np.array([bool(z["person_fest"]) for z in aktuell])
        zentrum = _norm((mat[fest] if fest.any() else mat).mean(0))
        sims = {z["id"]: float(s) for z, s in zip(aktuell, mat @ zentrum)}
    erg = [{"id": z["id"], "bild_id": z["bild_id"], "fest": bool(z["person_fest"]),
            "aehnlichkeit": round(sims[z["id"]], 3) if z["id"] in sims else None} for z in zeilen]
    erg.sort(key=lambda g: (g["fest"], g["aehnlichkeit"] if g["aehnlichkeit"] is not None else -2))
    return erg[:limit]


# ------------------------------------------------------------------ Korrekturen

def _festhalten(con, person_id: int) -> None:
    con.execute("UPDATE personen SET fest=1 WHERE id=?", (person_id,))


def umbenennen(db: Datenbank, person_id: int, name: str) -> int:
    name = name.strip() or None
    # gibt es schon eine Person mit diesem Namen: zusammenführen
    vorhanden = db.eins("SELECT id FROM personen WHERE name=? AND id<>?", (name, person_id)) if name else None
    if vorhanden:
        zusammenfuehren(db, vorhanden[0], [person_id])
        return vorhanden[0]
    with db.verbindung() as con:
        con.execute("UPDATE personen SET name=? WHERE id=?", (name, person_id))
        _festhalten(con, person_id)
    db.suchtext_aktualisieren_viele(_bilder_von(db, [person_id]))
    return person_id


def _vertreter(db: Datenbank, person_id: int, anzahl: int = 5) -> list[int]:
    """Die Gesichter, die eine Person am besten darstellen (nahe am Zentrum, gute Qualität)."""
    zeilen = db.abfrage("SELECT id, vektor, person_fest, COALESCE(qualitaet, score, 0) AS q FROM gesichter "
                        "WHERE person_id=? AND ignoriert=0 AND modell=?", (person_id, GESICHT_KENNUNG))
    if not zeilen:
        return []
    mat = _norm(np.stack([blob_zu_vektor(z["vektor"]) for z in zeilen]))
    ok, zentren = _kern(mat, np.array([bool(z["person_fest"]) for z in zeilen]), schwellen())
    naehe = (mat @ np.stack(zentren).T).max(axis=1) + 0.2 * np.array([z["q"] for z in zeilen])
    reihe = [i for i in np.argsort(-naehe) if ok[i]]
    return [int(zeilen[i]["id"]) for i in reihe[:anzahl]]


def zusammenfuehren(db: Datenbank, ziel_id: int, quell_ids: list[int]) -> int:
    """Personen vereinen. Ein paar typische Gesichter jeder Gruppe gelten danach als bestätigt:
    sonst würde das nächste Neugruppieren die kleinere Gruppe wieder abtrennen, wenn sie anders
    aussieht (anderes Alter, Brille ...)."""
    # Der Name soll nicht verloren gehen: ist das Ziel unbenannt, eine benannte Quelle als Ziel nehmen
    alle = [ziel_id, *[q for q in quell_ids if q != ziel_id]]
    benannt = {z["id"] for z in db.abfrage(
        f"SELECT id FROM personen WHERE name IS NOT NULL AND id IN ({','.join('?' * len(alle))})", alle)}
    if ziel_id not in benannt and benannt:
        ziel_id = next(i for i in alle if i in benannt)
    quell_ids = [i for i in alle if i != ziel_id]
    vertreter = [g for p in [ziel_id, *quell_ids] for g in _vertreter(db, p)]
    with db.verbindung() as con:
        con.executemany("UPDATE gesichter SET person_fest=1 WHERE id=?", [(g,) for g in vertreter])
        for q in quell_ids:
            if q != ziel_id:
                con.execute("UPDATE gesichter SET person_id=? WHERE person_id=?", (ziel_id, q))
                con.execute("UPDATE OR IGNORE gesicht_abgelehnt SET person_id=? WHERE person_id=?", (ziel_id, q))
                for a, b in con.execute("SELECT a, b FROM personen_verschieden WHERE a=? OR b=?", (q, q)).fetchall():
                    andere = b if a == q else a
                    if andere != ziel_id:
                        con.execute("INSERT OR IGNORE INTO personen_verschieden VALUES (?,?)", _paar(ziel_id, andere))
                con.execute("DELETE FROM personen WHERE id=?", (q,))
        _festhalten(con, ziel_id)
    db.suchtext_aktualisieren_viele(_bilder_von(db, [ziel_id]))  # Name gilt jetzt auch für diese Bilder
    return ziel_id


def _bilder_der_gesichter(db: Datenbank, gesicht_ids: list[int]) -> list[int]:
    if not gesicht_ids:
        return []
    return [z["bild_id"] for z in db.abfrage(
        f"SELECT DISTINCT bild_id FROM gesichter WHERE id IN ({','.join('?' * len(gesicht_ids))})", gesicht_ids)]


def nicht_diese_person(db: Datenbank, gesicht_ids: list[int]) -> None:
    """Gesichter passen nicht zu ihrer Person: herausnehmen und merken, damit sie (und sehr ähnliche
    Gesichter) dieser Person nicht wieder zugeordnet werden."""
    bilder = _bilder_der_gesichter(db, gesicht_ids)
    with db.verbindung() as con:
        for gid in gesicht_ids:
            r = con.execute("SELECT person_id FROM gesichter WHERE id=?", (gid,)).fetchone()
            if not r or not r[0]:
                continue
            con.execute("INSERT OR IGNORE INTO gesicht_abgelehnt VALUES (?,?)", (gid, r[0]))
            con.execute("UPDATE gesichter SET person_id=NULL, person_fest=0 WHERE id=?", (gid,))
            _festhalten(con, r[0])
    db.suchtext_aktualisieren_viele(bilder)  # alter Name passt nicht mehr


def gesicht_entfernen(db: Datenbank, gesicht_id: int) -> None:
    """Älterer Name für "Nicht diese Person"."""
    nicht_diese_person(db, [gesicht_id])


def kein_gesicht(db: Datenbank, gesicht_ids: list[int]) -> None:
    """Fehlerkennung (Muster, Statue, Plakat ...): wird ignoriert, auch beim Neuanalysieren."""
    bilder = _bilder_der_gesichter(db, gesicht_ids)
    with db.verbindung() as con:
        for gid in gesicht_ids:
            r = con.execute("SELECT person_id FROM gesichter WHERE id=?", (gid,)).fetchone()
            if r and r[0]:
                _festhalten(con, r[0])
            con.execute("UPDATE gesichter SET ignoriert=1, person_id=NULL, person_fest=0 WHERE id=?", (gid,))
    db.suchtext_aktualisieren_viele(bilder)


def zuweisen(db: Datenbank, gesicht_ids: list[int], person_id: int | None = None, name: str = "") -> int:
    """Gesichter fest einer Person zuordnen (vorhandene Person oder neue mit Namen). Liefert die Person."""
    bilder = _bilder_der_gesichter(db, gesicht_ids)
    name = (name or "").strip()
    with db.verbindung() as con:
        if not person_id and name:
            r = con.execute("SELECT id FROM personen WHERE name=?", (name,)).fetchone()
            person_id = r[0] if r else con.execute("INSERT INTO personen (name) VALUES (?)", (name,)).lastrowid
        if not person_id:
            person_id = con.execute("INSERT INTO personen (name) VALUES (NULL)").lastrowid
        for gid in gesicht_ids:
            con.execute("UPDATE gesichter SET person_id=?, person_fest=1, ignoriert=0 WHERE id=?", (person_id, gid))
            con.execute("DELETE FROM gesicht_abgelehnt WHERE gesicht_id=? AND person_id=?", (gid, person_id))
        _festhalten(con, person_id)
        con.execute("DELETE FROM personen WHERE name IS NULL AND fest=0 AND id NOT IN "
                    "(SELECT DISTINCT person_id FROM gesichter WHERE person_id IS NOT NULL)")
    db.suchtext_aktualisieren_viele(bilder)
    return person_id


# ------------------------------------------------------------------ Suche

def namen_treffer(db: Datenbank, anfrage: str) -> tuple[list[int], str, bool]:
    """Personen, deren Name in der Suchanfrage steckt, und der Rest der Anfrage.

    Groß-/Kleinschreibung und Umlaute egal ("Jürgen" = "jurgen"), Wortanfänge ab 3 Buchstaben
    reichen ("Marl" findet Marlena). Liefert (person_ids, übrige Wörter, alle Wörter exakt ein Name)."""
    from .datenbank import normalisiere
    woerter = normalisiere(anfrage).split()
    if not woerter:
        return [], "", False
    treffer, verbraucht, exakt = [], set(), set()
    for z in db.abfrage("SELECT id, name FROM personen WHERE name IS NOT NULL"):
        name = normalisiere(z["name"]).split()
        if not name:
            continue
        passend = {i for i, w in enumerate(woerter)
                   if any(n == w or (len(w) >= 3 and n.startswith(w)) for n in name)}
        if passend:
            treffer.append(z["id"])
            verbraucht |= passend
            exakt |= {i for i, w in enumerate(woerter) if w in name}
    rest = " ".join(w for i, w in enumerate(woerter) if i not in verbraucht)
    return treffer, rest, len(exakt) == len(woerter)


def bilder_mit_personen(db: Datenbank, person_ids: list[int]) -> list[int]:
    return _bilder_von(db, person_ids) if person_ids else []


def aehnliche_gesichter(db: Datenbank, referenz: np.ndarray, schwelle: float = SUCH_SCHWELLE,
                        limit: int = 2000) -> list[tuple[int, float]]:
    """Bilder (bild_id, Ähnlichkeit), auf denen ein Gesicht der Referenz ähnelt. Beste zuerst."""
    gids, bids, mat = db.gesicht_matrix(GESICHT_KENNUNG)
    if not len(gids) or mat.shape[1] != len(referenz):
        return []
    sims = _norm(mat) @ _norm(np.asarray(referenz, dtype=np.float32))
    beste: dict[int, float] = {}
    for b, s in zip(bids, sims):
        if s >= schwelle and s > beste.get(int(b), -1):
            beste[int(b)] = float(s)
    return sorted(beste.items(), key=lambda kv: -kv[1])[:limit]
