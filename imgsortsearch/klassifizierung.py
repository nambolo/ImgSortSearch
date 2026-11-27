"""Arbeit / Privat bestimmen: aus Themen (Bildinhalt), Uhrzeit und eigenen Regeln.

Themen sind kurze Beschreibungen wie "Whiteboard" oder "Familie". Das
Bildmodell (CLIP) schätzt, wie gut jedes Thema zu einem Bild passt. In der
Oberfläche legt man fest, welche Themen für Arbeit und welche für Privat
sprechen. Weil dafür nur die gespeicherten Bildvektoren gebraucht werden,
lässt sich nach jeder Änderung sofort neu einordnen, ohne neu zu analysieren.
"""
from __future__ import annotations

import math
import re
from datetime import datetime, time
from pathlib import Path

import numpy as np

from .modelle import softmax

UNKLAR = "Unklar"

def thema_prompt(t: dict) -> str:
    return f"ein Foto: {t.get('prompt') or t['name']}"


class Themen:
    """Themenliste mit Textvektoren."""

    def __init__(self, themen: list[dict], vektoren: np.ndarray):
        self.themen = themen
        self.vektoren = vektoren  # T x D
        self.kategorie = np.array([t.get("kategorie") or "" for t in themen])

    def wahrscheinlichkeiten(self, bild_vek: np.ndarray) -> np.ndarray:
        """N x T, jede Zeile summiert sich zu 1."""
        if bild_vek.ndim == 1:
            bild_vek = bild_vek[None, :]
        return softmax(bild_vek @ self.vektoren.T)

    def auswerten(self, bild_vek: np.ndarray, tag_schwelle: float = 0.15, max_tags: int = 3) -> list[dict]:
        p = self.wahrscheinlichkeiten(bild_vek)
        kats = sorted(set(self.kategorie) - {""})
        summen = {k: p[:, self.kategorie == k].sum(axis=1) for k in kats}
        erg = []
        for i in range(len(p)):
            ordnung = np.argsort(-p[i])
            tags = [self.themen[j]["name"] for j in ordnung[:max_tags] if p[i, j] >= tag_schwelle]
            if not tags:
                tags = [self.themen[ordnung[0]]["name"]]
            erg.append({"kategorien": {k: float(summen[k][i]) for k in kats}, "tags": tags,
                        "top": self.themen[ordnung[0]]["name"]})
        return erg


# ------------------------------------------------------------------ Regeln

def abstand_m(lat1, lon1, lat2, lon2) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _enthaelt(text, liste) -> bool:
    text = (text or "").lower()
    return any(str(s).lower() in text for s in liste)


def _datum(zeile) -> datetime | None:
    if not zeile.get("datum") or zeile.get("datum_quelle") == "dateidatum":
        return None  # Dateidatum ist für Zeitregeln zu unzuverlässig
    try:
        return datetime.fromisoformat(zeile["datum"])
    except ValueError:
        return None


def _im_zeitfenster(t: time, von: time, bis: time) -> bool:
    """Auch Fenster über Mitternacht (Nachtschicht 22:00-06:00)."""
    return von <= t <= bis if von <= bis else (t >= von or t <= bis)


def in_arbeitszeit(dt: datetime, az: dict) -> bool:
    von, bis = time.fromisoformat(az.get("von", "07:30")), time.fromisoformat(az.get("bis", "17:30"))
    tag = dt.isoweekday()
    if von > bis and dt.time() <= bis:  # Nachtschicht nach Mitternacht zählt zum Vortag
        tag = tag - 1 or 7
    if tag not in az.get("wochentage", [1, 2, 3, 4, 5]):
        return False
    return _im_zeitfenster(dt.time(), von, bis)


def regel_trifft(regel: dict, z: dict) -> bool:
    """Alle Bedingungen einer Regel müssen erfüllt sein. ``z`` ist eine Bildzeile (dict)."""
    n = 0
    if "kamera_enthaelt" in regel:
        n += 1
        if not _enthaelt(f"{z.get('hersteller')} {z.get('modell')}", regel["kamera_enthaelt"]):
            return False
    if "pfad_enthaelt" in regel:
        n += 1
        if not _enthaelt(z.get("pfad"), regel["pfad_enthaelt"]):
            return False
    if "dateiname_regex" in regel:
        n += 1
        try:
            if not re.search(regel["dateiname_regex"], Path(z.get("pfad") or "").name):
                return False
        except re.error:  # Tippfehler in der Regel soll nicht das Einordnen aller Bilder abbrechen
            return False
    if "gps" in regel:
        n += 1
        orte = regel["gps"] if isinstance(regel["gps"], list) else [regel["gps"]]
        if z.get("gps_lat") is None or not any(
                abstand_m(z["gps_lat"], z["gps_lon"], o["lat"], o["lon"]) <= o.get("radius_m", 300) for o in orte):
            return False
    if "wochentage" in regel or "uhrzeit" in regel:
        dt = _datum(z)
        if dt is None:
            return False
        if "wochentage" in regel:
            n += 1
            if dt.isoweekday() not in regel["wochentage"]:
                return False
        if "uhrzeit" in regel:
            n += 1
            von, bis = (time.fromisoformat(s.strip()) for s in regel["uhrzeit"].split("-"))
            if not _im_zeitfenster(dt.time(), von, bis):
                return False
    if "mit_gesichtern" in regel:
        n += 1
        if (int(z.get("anzahl_gesichter") or 0) > 0) != bool(regel["mit_gesichtern"]):
            return False
    if "beschreibung_enthaelt" in regel:
        n += 1
        if not _enthaelt(z.get("suchtext"), regel["beschreibung_enthaelt"]):
            return False
    return n > 0


def einordnen(z: dict, inhalt: dict | None, e: dict) -> dict:
    """Bewertet ein Bild und wählt die Kategorie mit den meisten Punkten.

    ``z``: Bildzeile, ``inhalt``: Ergebnis von ``Themen.auswerten``, ``e``: Einstellungen."""
    namen = [k["name"] for k in e.get("kategorien", [])]
    punkte = {k: 0.0 for k in namen}
    gruende = []

    if inhalt and e["inhalt"].get("aktiv", True):
        gewicht = float(e["inhalt"].get("gewicht", 3))
        werte = {k: v for k, v in inhalt.get("kategorien", {}).items() if k in punkte}
        if werte:
            # nur der Vorsprung des Inhalts zählt: ein Bild, das zu allem ein bisschen passt, sagt wenig
            mittel = sum(werte.values()) / max(1, len(punkte))
            for k, v in werte.items():
                g = gewicht * max(0.0, v - mittel)
                if g >= 0.05:
                    punkte[k] += g
                    gruende.append(f"Inhalt {', '.join(inhalt['tags'])} (+{g:.1f} {k})")

    az = e["arbeitszeit"]
    dt = _datum(z)
    if az.get("aktiv", True) and dt is not None:
        if in_arbeitszeit(dt, az):
            k, g, wort = az.get("kategorie", "Arbeit"), float(az.get("gewicht", 1.5)), "Arbeitszeit"
        else:
            k, g, wort = az.get("kategorie_freizeit", "Privat"), float(az.get("gewicht_freizeit", 1.0)), "Freizeit"
        if k in punkte and g:
            punkte[k] += g
            gruende.append(f"{wort} {dt:%d.%m. %H:%M} (+{g:g} {k})")

    for regel in e.get("regeln", []):
        if regel.get("kategorie") in punkte and regel_trifft(regel, z):
            g = float(regel.get("gewicht", 1))
            punkte[regel["kategorie"]] += g
            gruende.append(f"{regel.get('name', 'Regel')} (+{g:g} {regel['kategorie']})")

    schwelle = float(e.get("schwelle", 1.5))
    rang = sorted(punkte.items(), key=lambda kv: kv[1], reverse=True)
    if not rang:
        return {"kategorie": UNKLAR, "sicherheit": 0.0, "gruende": "keine Kategorien angelegt"}
    vorsprung = rang[0][1] - (rang[1][1] if len(rang) > 1 else 0.0)
    if vorsprung < schwelle:
        kat, sicherheit = UNKLAR, 0.0
    else:
        kat, sicherheit = rang[0][0], min(1.0, vorsprung / (2 * schwelle))
    return {"kategorie": kat, "sicherheit": round(sicherheit, 2), "gruende": " | ".join(gruende)}
