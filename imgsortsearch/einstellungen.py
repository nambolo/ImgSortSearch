"""Einstellungen und Datenordner.

Alles (Datenbank, Vorschaubilder, Modelle, Protokolle) liegt in einem Ordner.
Standard: ``daten`` neben dem Programm. Über die Umgebungsvariable
``IMGSORTSEARCH_HOME`` lässt er sich verlegen (das Startskript setzt sie).
"""
from __future__ import annotations

import copy
import json
import os
import threading
from pathlib import Path

from .themen_standard import STANDARD_KATEGORIEN, STANDARD_THEMEN

PROGRAMM_ORDNER = Path(__file__).resolve().parent.parent

STANDARD = {
    "quellen": [],                       # Ordner mit dem Bilder-Backup
    "videos": False,
    "kategorien": STANDARD_KATEGORIEN,
    "arbeitszeit": {
        "aktiv": True,
        "kategorie": "Arbeit",           # Kategorie für Bilder in der Arbeitszeit
        "kategorie_freizeit": "Privat",  # und außerhalb
        "wochentage": [1, 2, 3, 4, 5],   # 1 = Montag
        "von": "07:30",
        "bis": "17:30",
        "gewicht": 1.5,
        "gewicht_freizeit": 1.0,
    },
    "inhalt": {
        "aktiv": True,
        "gewicht": 3.0,                  # Punkte für das Urteil der Bildanalyse (mal Sicherheit)
    },
    "themen": STANDARD_THEMEN,
    "schwelle": 1.5,                     # nötiger Punktevorsprung, sonst "Unklar"
    "regeln": [],                        # zusätzliche Regeln (Kamera, GPS, Ordner ...)
    "ocr": {
        "modus": "text_bilder",          # aus | text_bilder | alle
        "backend": "auto",               # auto | windows | rapidocr
        "sprache": "de",
    },
    "beschreibung": {
        "aktiv": True,
        "modell": "Salesforce/blip-image-captioning-base",
        "uebersetzer": "Helsinki-NLP/opus-mt-en-de",   # leer = englisch lassen
        # Ausführliche Beschreibungen (mehrere Sätze). Aus: das Modell wird erst geladen, wenn man es einschaltet.
        "ausfuehrlich": {
            "aktiv": False,
            "modell": "microsoft/Florence-2-large",     # oder microsoft/Florence-2-base (ca. 3x schneller)
            "stufe": "sehr_detailliert",                # detailliert (2-3 Sätze) | sehr_detailliert (Absatz)
            "genauigkeit": "auto",                      # auto (Grafikkarte: halbe) | fp16 | fp32
            "strahlen": 3,                              # Suchbreite beim Formulieren: 1 = schneller, 3 = besser
            "stapel": 2,
            "such_strenge": 3.0,                        # Bedeutungssuche: so weit über dem Durchschnitt (Std.-Abw.)
        },
    },
    "gesichter": {"aktiv": True, "min_groesse": 40},  # Schwellen: siehe personen.STANDARD (überschreibbar)
    "clip": {
        "modell": "xlm-roberta-base-ViT-B-32",
        "pretrained": "laion5b_s13b_b90k",
        "stapel": 16,
    },
    "sortieren": {
        "ziel": "",
        "modus": "kopieren",             # kopieren | verschieben | nur_taggen
        "umbenennen": False,
        "name": "{datum:%Y-%m-%d_%H%M%S}_{kategorie}_{kamera}",
        "ordner": "{kategorie}/{datum:%Y}/{datum:%Y-%m}",
        "tags": "sidecar",               # sidecar | einbetten | keine
    },
    "dubletten": {
        "ziel": "quarantaene",           # quarantaene | papierkorb
        "quarantaene": "",               # leer = Ordner "dubletten-quarantaene" im Datenordner
    },
    "karte": {
        "online": False,                 # Kartenkacheln von OpenStreetMap laden (braucht Internet)
        "detail": True,                  # kleine Karte in der Detailansicht
        "kachel_url": "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
    },
    "beschleunigung": "auto",            # auto | cuda | directml | xpu | cpu
    "port": 8765,
}


def daten_ordner() -> Path:
    pfad = Path(os.environ.get("IMGSORTSEARCH_HOME") or (PROGRAMM_ORDNER / "daten"))
    pfad.mkdir(parents=True, exist_ok=True)
    return pfad


def _mische(basis: dict, neu: dict) -> dict:
    erg = copy.deepcopy(basis)
    for k, v in (neu or {}).items():
        if isinstance(v, dict) and isinstance(erg.get(k), dict):
            erg[k] = _mische(erg[k], v)
        else:
            erg[k] = v
    return erg


class Einstellungen:
    def __init__(self, datei: Path | None = None):
        self.datei = datei or daten_ordner() / "einstellungen.json"
        self._lock = threading.Lock()
        self.werte = copy.deepcopy(STANDARD)
        if self.datei.exists():
            try:
                self.werte = _mische(STANDARD, json.loads(self.datei.read_text("utf-8-sig")))
            except (json.JSONDecodeError, UnicodeDecodeError):
                # beschädigte Datei sichern, sonst wäre sie beim nächsten Speichern weg
                try:
                    self.datei.replace(self.datei.with_name(self.datei.name + ".defekt"))
                except OSError:
                    pass

    def __getitem__(self, k):
        return self.werte[k]

    def get(self, k, default=None):
        return self.werte.get(k, default)

    def aktualisieren(self, neu: dict) -> dict:
        with self._lock:
            self.werte = _mische(self.werte, neu)
            tmp = self.datei.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.werte, ensure_ascii=False, indent=2), "utf-8")
            tmp.replace(self.datei)
            return self.werte
