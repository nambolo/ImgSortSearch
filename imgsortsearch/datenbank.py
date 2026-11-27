"""SQLite-Suchindex: Bilder, Gesichter, Personen, Bildvektoren."""
from __future__ import annotations

import sqlite3
import threading
import unicodedata
from contextlib import contextmanager
from pathlib import Path

import numpy as np

SCHEMA = """
CREATE TABLE IF NOT EXISTS bilder (
    id INTEGER PRIMARY KEY,
    pfad TEXT UNIQUE NOT NULL,
    groesse INTEGER, mtime REAL,
    datum TEXT, datum_quelle TEXT,
    hersteller TEXT, modell TEXT, kamera TEXT,
    gps_lat REAL, gps_lon REAL,
    breite INTEGER, hoehe INTEGER,
    schlagwoerter TEXT DEFAULT '',
    kategorie_auto TEXT, sicherheit REAL, gruende TEXT,
    kategorie_manuell TEXT,
    inhalt_arbeit REAL,           -- 0..1 laut Bildanalyse
    textlastig REAL,              -- 0..1: wie sehr das Bild nach Text aussieht
    tags TEXT DEFAULT '',
    ocr_text TEXT,
    beschreibung TEXT, beschreibung_en TEXT,
    anzahl_gesichter INTEGER DEFAULT 0,
    suchtext TEXT DEFAULT '',     -- normalisiert, für die Textsuche
    clip BLOB,
    schritt_clip INTEGER DEFAULT 0,
    schritt_ocr INTEGER DEFAULT 0,
    schritt_gesichter INTEGER DEFAULT 0,
    schritt_beschreibung INTEGER DEFAULT 0,
    fehler TEXT
);
CREATE INDEX IF NOT EXISTS ix_bilder_datum ON bilder(datum);
CREATE TABLE IF NOT EXISTS personen (
    id INTEGER PRIMARY KEY,
    name TEXT
);
CREATE TABLE IF NOT EXISTS gesichter (
    id INTEGER PRIMARY KEY,
    bild_id INTEGER NOT NULL REFERENCES bilder(id) ON DELETE CASCADE,
    x REAL, y REAL, w REAL, h REAL, score REAL,   -- relativ zur Bildgröße (0..1)
    vektor BLOB NOT NULL,
    person_id INTEGER REFERENCES personen(id) ON DELETE SET NULL,
    person_fest INTEGER DEFAULT 0  -- vom Nutzer bestätigt
);
CREATE INDEX IF NOT EXISTS ix_gesichter_bild ON gesichter(bild_id);
CREATE INDEX IF NOT EXISTS ix_gesichter_person ON gesichter(person_id);
CREATE TABLE IF NOT EXISTS textvektoren (
    text TEXT, kennung TEXT, vektor BLOB, PRIMARY KEY (text, kennung)
);
CREATE TABLE IF NOT EXISTS protokolle (
    id INTEGER PRIMARY KEY,
    datei TEXT, zeit TEXT, beschreibung TEXT, rueckgaengig INTEGER DEFAULT 0
);
"""


def normalisiere(text: str) -> str:
    """Kleinbuchstaben, Umlaute und Akzente vereinfacht (ä->a, ß->ss).

    Hilft auch gegen OCR, die Umlaute oft ohne Punkte erkennt."""
    text = (text or "").lower().replace("ß", "ss")
    text = unicodedata.normalize("NFKD", text)
    return "".join(c for c in text if not unicodedata.combining(c))


def vektor_zu_blob(v: np.ndarray) -> bytes:
    return np.asarray(v, dtype=np.float16).tobytes()


def blob_zu_vektor(b: bytes) -> np.ndarray:
    return np.frombuffer(b, dtype=np.float16).astype(np.float32)


class Datenbank:
    def __init__(self, datei: Path):
        self.datei = Path(datei)
        self._lokal = threading.local()
        self.schreib_lock = threading.RLock()
        with self.verbindung() as v:
            v.executescript(SCHEMA)

    def _con(self) -> sqlite3.Connection:
        con = getattr(self._lokal, "con", None)
        if con is None:
            con = sqlite3.connect(self.datei, timeout=30, check_same_thread=False)
            con.row_factory = sqlite3.Row
            con.execute("PRAGMA journal_mode=WAL")
            con.execute("PRAGMA foreign_keys=ON")
            con.execute("PRAGMA synchronous=NORMAL")
            self._lokal.con = con
        return con

    @contextmanager
    def verbindung(self):
        con = self._con()
        with self.schreib_lock:
            try:
                yield con
                con.commit()
            except Exception:
                con.rollback()
                raise

    def abfrage(self, sql: str, params=()) -> list[sqlite3.Row]:
        return self._con().execute(sql, params).fetchall()

    def eins(self, sql: str, params=()):
        return self._con().execute(sql, params).fetchone()

    # ---------------------------------------------------------- Suchtext

    def suchtext_aktualisieren(self, bild_id: int) -> None:
        r = self.eins("SELECT pfad, kamera, tags, ocr_text, schlagwoerter, beschreibung, beschreibung_en "
                     "FROM bilder WHERE id=?", (bild_id,))
        if not r:
            return
        personen = [p["name"] for p in self.abfrage(
            "SELECT DISTINCT p.name FROM gesichter g JOIN personen p ON p.id=g.person_id "
            "WHERE g.bild_id=? AND p.name IS NOT NULL", (bild_id,))]
        teile = [r["pfad"], r["kamera"], r["tags"], r["ocr_text"], r["schlagwoerter"], r["beschreibung"],
                 r["beschreibung_en"], " ".join(personen)]
        with self.verbindung() as v:
            v.execute("UPDATE bilder SET suchtext=? WHERE id=?",
                      (normalisiere(" ".join(t or "" for t in teile)), bild_id))

    # ---------------------------------------------------------- Vektoren

    def clip_matrix(self) -> tuple[np.ndarray, np.ndarray]:
        """(ids, Matrix N x D) aller Bilder mit Bildvektor."""
        zeilen = self.abfrage("SELECT id, clip FROM bilder WHERE clip IS NOT NULL")
        if not zeilen:
            return np.zeros(0, dtype=np.int64), np.zeros((0, 1), dtype=np.float32)
        ids = np.array([z["id"] for z in zeilen], dtype=np.int64)
        mat = np.stack([blob_zu_vektor(z["clip"]) for z in zeilen])
        return ids, mat

    def textvektoren(self, texte: list[str], kennung: str, clip=None) -> np.ndarray | None:
        """Textvektoren aus dem Zwischenspeicher, fehlende über ``clip`` berechnen.
        Ohne geladenes Modell und mit fehlenden Texten wird None geliefert."""
        vorhanden = {}
        for i in range(0, len(texte), 500):
            teil = texte[i:i + 500]
            zeilen = self.abfrage(
                f"SELECT text, vektor FROM textvektoren WHERE kennung=? AND text IN ({','.join('?' * len(teil))})",
                (kennung, *teil))
            vorhanden.update({z["text"]: blob_zu_vektor(z["vektor"]) for z in zeilen})
        fehlend = [t for t in texte if t not in vorhanden]
        if fehlend:
            if clip is None:
                return None
            neu = clip.texte(fehlend)
            with self.verbindung() as v:
                v.executemany("INSERT OR REPLACE INTO textvektoren VALUES (?,?,?)",
                              [(t, kennung, vektor_zu_blob(x)) for t, x in zip(fehlend, neu)])
            vorhanden.update(dict(zip(fehlend, neu.astype(np.float32))))
        return np.stack([vorhanden[t] for t in texte])

    def gesicht_matrix(self) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """(gesicht_ids, bild_ids, Matrix N x 128)."""
        zeilen = self.abfrage("SELECT id, bild_id, vektor FROM gesichter")
        if not zeilen:
            return (np.zeros(0, dtype=np.int64),) * 2 + (np.zeros((0, 128), dtype=np.float32),)
        return (np.array([z["id"] for z in zeilen], dtype=np.int64),
                np.array([z["bild_id"] for z in zeilen], dtype=np.int64),
                np.stack([blob_zu_vektor(z["vektor"]) for z in zeilen]))
