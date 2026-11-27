"""Heruntergeladene Modelle anzeigen und ungenutzte löschen, um Speicherplatz frei zu geben.

Ob ein Modell gebraucht wird, richtet sich nach den gespeicherten Einstellungen. Gelöscht wird nur, was
sicher als ungenutzt erkannt ist (ausgeschaltete Funktion, anderes gewähltes Modell, alte Version).
Alles, was nicht eindeutig zuzuordnen ist, bleibt liegen. Wird eine Funktion später wieder eingeschaltet,
lädt das Programm ihr Modell wie beim ersten Mal neu herunter.
"""
from __future__ import annotations

import functools
import logging
import os
import shutil
import stat
import sys
from pathlib import Path

from . import modelle

log = logging.getLogger(__name__)

# Dateien der Gesichtserkennung bis Version mit ArcFace (liegen direkt in daten/modelle)
ALTE_DATEIEN = {
    "face_detection_yunet_2023mar.onnx": "Alte Gesichtserkennung (YuNet)",
    "face_recognition_sface_2021dec.onnx": "Alte Gesichtserkennung (SFace)",
}


def repo_ordnername(repo: str) -> str:
    """Ordnername im HuggingFace-Zwischenspeicher: 'org/name' -> 'models--org--name'."""
    return "models--" + repo.strip("/").replace("/", "--")


def _repo_aus_ordner(name: str) -> str:
    return name[len("models--"):].replace("--", "/")


def groesse(pfad: Path) -> int:
    """Belegter Platz in Bytes (Verknüpfungen werden nicht doppelt gezählt)."""
    try:
        if pfad.is_symlink():
            return 0
        if pfad.is_file():
            return pfad.stat().st_size
    except OSError:
        return 0
    n = 0
    for wurzel, _, dateien in os.walk(pfad):
        for d in dateien:
            p = os.path.join(wurzel, d)
            try:
                if not os.path.islink(p):
                    n += os.stat(p).st_size
            except OSError:
                pass
    return n


@functools.lru_cache(maxsize=8)
def _clip_repos(modell: str, pretrained: str) -> frozenset[str] | None:
    """Ordner in modelle/clip, die das gewählte Bildmodell braucht (Gewichte und Tokenizer).
    None, wenn es sich nicht feststellen lässt: dann wird dort nichts gelöscht."""
    try:
        import open_clip
        cfg = open_clip.get_pretrained_cfg(modell, pretrained) or {}
        text = (open_clip.get_model_config(modell) or {}).get("text_cfg", {})
    except Exception:
        log.exception("Bildmodell-Dateien nicht feststellbar")
        return None
    repos = {cfg.get("hf_hub", ""), text.get("hf_tokenizer_name", ""), text.get("hf_model_name", "")}
    return frozenset(repo_ordnername(r) for r in repos if r)


def _kurzname(repo: str) -> str:
    r = repo.lower()
    if "blip" in r:
        return f"Kurze Bildbeschreibung ({repo})"
    if "opus-mt" in r:
        return f"Übersetzer Englisch-Deutsch ({repo})"
    return repo


def bestand(werte: dict, download: dict | None = None) -> list[dict]:
    """Alle Modelle im Modellordner mit Größe und ob sie gebraucht werden.

    Jeder Eintrag: id (Pfad relativ zu daten/modelle), name, groesse (Bytes), gebraucht, grund, entladen
    (Namen der Modelle im Speicher, die vor dem Löschen freigegeben werden)."""
    basis = modelle.modell_ordner()
    download = download or {}
    b = werte.get("beschreibung", {})
    a = b.get("ausfuehrlich", {})
    kurz_an, lang_an = bool(b.get("aktiv")), bool(a.get("aktiv"))
    uebersetzer = b.get("uebersetzer") or ""
    erg: list[dict] = []

    def eintrag(pfad: Path, name: str, gebraucht: bool, grund: str, entladen=()):
        g = groesse(pfad)
        if g == 0 and pfad.is_dir() and not any(pfad.iterdir()):
            return  # leere Ordner nicht anzeigen
        erg.append({"id": pfad.relative_to(basis).as_posix(), "name": name, "groesse": g,
                    "gebraucht": gebraucht, "grund": grund, "entladen": list(entladen)})

    for p in sorted(basis.iterdir(), key=lambda x: x.name.lower()):
        if p.name.startswith("."):
            continue
        if p.name == "clip" and p.is_dir():
            c = werte.get("clip", {})
            noetig = _clip_repos(c.get("modell", ""), c.get("pretrained", ""))
            for q in sorted(p.iterdir()):
                if q.name.startswith("."):
                    continue
                repo = _repo_aus_ordner(q.name) if q.name.startswith("models--") else q.name
                ist_clip = "clip" in q.name.lower()
                if noetig is None or q.name in noetig or not ist_clip:
                    eintrag(q, f"Bildmodell ({repo})", True, "Wird für Einordnen und Suche gebraucht.")
                else:
                    eintrag(q, f"Bildmodell ({repo})", False, "Anderes Bildmodell gewählt (alte Version).", ("clip",))
        elif p.name == "hf" and p.is_dir():
            for q in sorted(p.iterdir()):
                if q.name.startswith("."):
                    continue
                if not q.name.startswith("models--"):
                    eintrag(q, f"Zwischenspeicher ({q.name})", True, "Wird vom Bildmodell oder den Beschreibungen genutzt.")
                    continue
                repo = _repo_aus_ordner(q.name)
                if repo == b.get("modell"):
                    if kurz_an:
                        eintrag(q, _kurzname(repo), True, "Bildbeschreibungen sind eingeschaltet.")
                    else:
                        eintrag(q, _kurzname(repo), False, "Bildbeschreibungen sind ausgeschaltet.", ("beschreiber",))
                elif uebersetzer and repo == uebersetzer:
                    if kurz_an or lang_an:
                        eintrag(q, _kurzname(repo), True, "Übersetzt die Bildbeschreibungen ins Deutsche.")
                    else:
                        eintrag(q, _kurzname(repo), False, "Bildbeschreibungen sind ausgeschaltet.",
                                ("beschreiber", "ausfuehrlich"))
                else:
                    eintrag(q, _kurzname(repo), False, "Wird in den Einstellungen nicht mehr verwendet.",
                            ("beschreiber", "ausfuehrlich"))
        elif p.name == "ausfuehrlich" and p.is_dir():
            for q in sorted(p.iterdir()):
                if q.name.startswith("."):
                    continue
                repo = _repo_aus_ordner("models--" + q.name)
                name = modelle.AUSFUEHRLICH_MODELLE.get(repo, {}).get("name", repo)
                name = f"Ausführliche Beschreibungen ({name})"
                if download.get("laeuft") and download.get("modell") == repo:
                    eintrag(q, name, True, "Wird gerade heruntergeladen.")
                elif lang_an and a.get("modell") == repo:
                    eintrag(q, name, True, "Ausführliche Beschreibungen sind eingeschaltet.")
                elif lang_an:
                    eintrag(q, name, False, "Anderes Modell für ausführliche Beschreibungen gewählt.", ("ausfuehrlich",))
                else:
                    eintrag(q, name, False, "Ausführliche Beschreibungen sind ausgeschaltet.", ("ausfuehrlich",))
        elif p.name == "buffalo_l" and p.is_dir():
            name = "Gesichtserkennung (InsightFace buffalo_l)"
            if werte.get("gesichter", {}).get("aktiv", True):
                eintrag(p, name, True, "Gesichtserkennung ist eingeschaltet.")
            else:
                eintrag(p, name, False, "Gesichtserkennung ist ausgeschaltet.", ("gesichter",))
        elif p.name in ALTE_DATEIEN or p.name.removesuffix(".part") in ALTE_DATEIEN:
            eintrag(p, ALTE_DATEIEN[p.name.removesuffix(".part")], False,
                    "Alte Version, wird nicht mehr verwendet.")
        else:
            eintrag(p, f"Sonstiges ({p.name})", True, "Nicht zuzuordnen, wird nicht angefasst.")
    return erg


def _entfernen(pfad: Path) -> None:
    def schreibbar_machen(funktion, p, _):  # Windows: schreibgeschützte Dateien
        os.chmod(p, stat.S_IWRITE)
        funktion(p)

    if pfad.is_dir() and not pfad.is_symlink():
        if sys.version_info >= (3, 12):
            shutil.rmtree(pfad, onexc=schreibbar_machen)
        else:
            shutil.rmtree(pfad, onerror=schreibbar_machen)
    else:
        try:
            pfad.unlink()
        except PermissionError:
            os.chmod(pfad, stat.S_IWRITE)
            pfad.unlink()


def entfernen(ids: list[str], werte: dict, download: dict | None = None, entladen=None) -> dict:
    """Löscht die genannten Modelle, aber nur, wenn sie (jetzt noch) ungenutzt sind.
    ``entladen(*namen)`` gibt Modelle im Speicher frei, damit ihre Dateien nicht gesperrt sind."""
    basis = modelle.modell_ordner()
    liste = {e["id"]: e for e in bestand(werte, download)}
    entfernt, fehler, frei = [], [], 0
    for i in dict.fromkeys(ids):
        e = liste.get(i)
        if e is None:
            fehler.append({"id": i, "name": i, "meldung": "Nicht gefunden."})
            continue
        if e["gebraucht"]:
            fehler.append({"id": i, "name": e["name"], "meldung": "Wird gebraucht, nicht gelöscht."})
            continue
        if entladen and e["entladen"]:
            entladen(*e["entladen"])
            import gc
            gc.collect()
        pfad = basis / i
        try:
            _entfernen(pfad)
            if pfad.parent.name == "hf":  # Sperrdateien des Zwischenspeichers gleich mit
                shutil.rmtree(pfad.parent / ".locks" / pfad.name, ignore_errors=True)
            entfernt.append(i)
            frei += e["groesse"]
            log.info("Modell gelöscht: %s (%d MB)", i, e["groesse"] // 2**20)
        except OSError as ex:
            log.exception("Modell %s nicht gelöscht", i)
            rest = groesse(pfad)
            frei += max(0, e["groesse"] - rest)
            fehler.append({"id": i, "name": e["name"],
                           "meldung": f"Nicht vollständig gelöscht ({ex.strerror or ex}). "
                                      "Wahrscheinlich noch in Benutzung: nach einem Neustart erneut versuchen."})
    return {"entfernt": entfernt, "frei": frei, "fehler": fehler}
