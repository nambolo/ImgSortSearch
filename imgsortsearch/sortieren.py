"""Sortieren: kopieren / verschieben / nur taggen, optional umbenennen, Tags schreiben, rückgängig.

Jede echte Änderung landet in einem Protokoll (JSON Lines). Damit lässt sich
ein Lauf vollständig zurücknehmen. Originale werden nie gelöscht.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from xml.sax.saxutils import escape

RAW_ENDUNGEN = {".dng", ".cr2", ".cr3", ".nef", ".arw", ".orf", ".rw2", ".raf"}
_KEIN_FENSTER = getattr(subprocess, "CREATE_NO_WINDOW", 0)


@dataclass
class Auftrag:
    pfad: Path
    kategorie: str
    datum: datetime | None
    kamera: str
    tags: list[str]
    personen: list[str]
    beschreibung: str = ""
    bild_id: int | None = None


class _Datum:
    def __init__(self, dt):
        self.dt = dt

    def __format__(self, spec):
        if self.dt is None:
            return "ohne-Datum"
        return self.dt.strftime(spec) if spec else self.dt.isoformat()


def _sauber(text: str) -> str:
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "", str(text)).strip().rstrip(".")
    return re.sub(r"\s+", "-", text) or "_"


def _fuellen(vorlage: str, werte: dict) -> str:
    try:
        return vorlage.format(**werte)
    except (KeyError, IndexError, ValueError) as ex:
        erlaubt = ", ".join("{" + k + "}" for k in werte)
        raise ValueError(f"Ungültige Vorlage '{vorlage}' ({ex}). Erlaubt: {erlaubt}") from None


def ziel_relativ(a: Auftrag, s: dict) -> Path:
    """Relativer Zielpfad nach Namens- und Ordnerschema."""
    werte = {"datum": _Datum(a.datum), "kategorie": _sauber(a.kategorie), "kamera": _sauber(a.kamera or "Unbekannt"),
             "original": _sauber(a.pfad.stem)}
    vorlage = s.get("name", "{original}") if a.datum else "{original}"  # ohne Datum: alten Namen behalten
    name = (_sauber(_fuellen(vorlage, werte)) + a.pfad.suffix.lower()) if s.get("umbenennen") else a.pfad.name
    if s.get("modus") == "nur_taggen":
        return Path(name)
    ordner = _fuellen(s.get("ordner", "{kategorie}"), werte)
    return Path(*[_sauber(t) for t in ordner.replace("\\", "/").split("/") if t.strip()]) / name


def _hash(p: Path) -> str:
    h = hashlib.sha1()
    with open(p, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _gleich(a: Path, b: Path) -> bool:
    return a.stat().st_size == b.stat().st_size and _hash(a) == _hash(b)


def _freier_pfad(ziel: Path, quelle: Path, belegt: set) -> tuple[Path, bool]:
    kandidat, n = ziel, 1
    while kandidat in belegt or kandidat.exists():
        if kandidat.exists() and kandidat != quelle and _gleich(kandidat, quelle):
            return kandidat, True
        if kandidat == quelle:
            return kandidat, False
        n += 1
        kandidat = ziel.with_name(f"{ziel.stem}_{n}{ziel.suffix}")
    return kandidat, False


def alle_tags(a: Auftrag) -> list[str]:
    return list(dict.fromkeys([a.kategorie] + a.tags + a.personen))


def xmp_sidecar(a: Auftrag) -> str:
    bag = "".join(f"<rdf:li>{escape(t)}</rdf:li>" for t in alle_tags(a))
    hier = [f"ImgSortSearch|Kategorie|{a.kategorie}"] + [f"ImgSortSearch|Thema|{t}" for t in a.tags] \
        + [f"Personen|{p}" for p in a.personen]
    hier_xml = "".join(f"<rdf:li>{escape(x)}</rdf:li>" for x in hier)
    beschr = (f'<dc:description><rdf:Alt><rdf:li xml:lang="x-default">{escape(a.beschreibung)}'
              f"</rdf:li></rdf:Alt></dc:description>") if a.beschreibung else ""
    return ('<?xpacket begin="﻿" id="W5M0MpCehiHzreSzNTczkc9d"?>\n'
            '<x:xmpmeta xmlns:x="adobe:ns:meta/">\n'
            ' <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">\n'
            '  <rdf:Description rdf:about=""\n'
            '    xmlns:dc="http://purl.org/dc/elements/1.1/"\n'
            '    xmlns:lr="http://ns.adobe.com/lightroom/1.0/">\n'
            f"   <dc:subject><rdf:Bag>{bag}</rdf:Bag></dc:subject>\n"
            f"   <lr:hierarchicalSubject><rdf:Bag>{hier_xml}</rdf:Bag></lr:hierarchicalSubject>\n"
            f"   {beschr}\n"
            "  </rdf:Description>\n"
            " </rdf:RDF>\n"
            "</x:xmpmeta>\n"
            '<?xpacket end="w"?>\n')


def _einbetten_argumente(datei: Path, a: Auftrag, sicherung: bool) -> list[str]:
    args = ["-P", "-charset", "filename=utf8", "-charset", "iptc=utf8", "-codedcharacterset=utf8"]
    if not sicherung:
        args.append("-overwrite_original")
    # erst entfernen, dann hinzufügen: so entstehen bei einem zweiten Lauf keine doppelten Einträge
    for t in alle_tags(a):
        args += [f"-XMP-dc:Subject-={t}", f"-XMP-dc:Subject+={t}", f"-IPTC:Keywords-={t}", f"-IPTC:Keywords+={t}"]
    hier = [f"ImgSortSearch|Kategorie|{a.kategorie}"] + [f"Personen|{p}" for p in a.personen]
    for h in hier:
        args += [f"-XMP-lr:HierarchicalSubject-={h}", f"-XMP-lr:HierarchicalSubject+={h}"]
    if a.beschreibung:
        args.append(f"-XMP-dc:Description={a.beschreibung}")
    args.append(str(datei))
    return [" ".join(x.splitlines()) for x in args]  # eine Zeile pro Argument


# Formate, in die exiftool Metadaten schreiben kann (BMP z. B. nicht: dafür XMP-Datei daneben)
EINBETTEN_ENDUNGEN = {".jpg", ".jpeg", ".tif", ".tiff", ".png", ".heic", ".heif", ".webp", ".gif"}


class _Exiftool:
    """Ein dauerhaft laufendes exiftool (-stay_open) für alle Dateien eines Laufs.

    Ein eigener Aufruf pro Bild kostet unter Windows fast eine Sekunde (Start des Programms);
    bei 10.000 Bildern wären das Stunden. Die Argumente gehen als UTF-8 über stdin
    (wie eine Argumentdatei), damit Umlaute in Namen und Pfaden heil ankommen."""

    ZEITLIMIT = 120

    def __init__(self):
        self.exe = shutil.which("exiftool")
        if not self.exe:
            raise RuntimeError("exiftool nicht gefunden")
        self.proc = None

    def _starten(self):
        import queue
        import threading
        self.proc = subprocess.Popen([self.exe, "-stay_open", "True", "-@", "-"], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, creationflags=_KEIN_FENSTER)
        self.zeilen = queue.Queue()
        proc = self.proc

        def lesen():
            for zeile in iter(proc.stdout.readline, b""):
                self.zeilen.put(zeile)
            self.zeilen.put(None)  # Prozess beendet

        threading.Thread(target=lesen, daemon=True).start()

    def schreiben(self, args: list[str]) -> None:
        import queue
        if self.proc is None or self.proc.poll() is not None:
            self._starten()
        self.proc.stdin.write(("\n".join(args) + "\n-execute\n").encode("utf-8"))
        self.proc.stdin.flush()
        ausgabe = []
        while True:
            try:
                zeile = self.zeilen.get(timeout=self.ZEITLIMIT)
            except queue.Empty:
                self.schliessen(hart=True)  # hängt an dieser Datei: beim nächsten Bild neu starten
                raise RuntimeError("exiftool reagiert nicht") from None
            if zeile is None:
                self.proc = None
                raise RuntimeError("exiftool wurde unerwartet beendet: " + " ".join(ausgabe))
            text = zeile.decode("utf-8", "replace").strip()
            if text == "{ready}":
                break
            if text:
                ausgabe.append(text)
        meldung = " ".join(ausgabe)
        if "Error" in meldung or "weren't updated" in meldung or "updated" not in meldung and "unchanged" not in meldung:
            raise RuntimeError(meldung or "exiftool: keine Rückmeldung")

    def schliessen(self, hart: bool = False):
        proc, self.proc = self.proc, None
        if proc is None or proc.poll() is not None:
            return
        try:
            if not hart:
                proc.stdin.write(b"-stay_open\nFalse\n")
                proc.stdin.flush()
                proc.wait(timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            pass
        if proc.poll() is None:
            proc.kill()


def _einbetten(datei: Path, a: Auftrag, sicherung: bool, exiftool: _Exiftool | None = None) -> None:
    eigenes = exiftool is None
    exiftool = exiftool or _Exiftool()
    try:
        exiftool.schreiben(_einbetten_argumente(datei, a, sicherung))
    finally:
        if eigenes:
            exiftool.schliessen()


def _sidecars(pfad: Path) -> list[Path]:
    """Vorhandene XMP-Dateien zu einem Bild (Lightroom: IMG.xmp, andere: IMG.jpg.xmp)."""
    kandidaten = [pfad.with_name(pfad.name + ".xmp")]
    if pfad.suffix.lower() in RAW_ENDUNGEN:  # bei JPEG könnte IMG.xmp zur RAW-Datei daneben gehören
        kandidaten += [pfad.with_suffix(".xmp"), pfad.with_suffix(".XMP")]
    erg = []
    for k in kandidaten:
        if k.exists() and not any(k.samefile(e) for e in erg):
            erg.append(k)
    return erg


def ausfuehren(auftraege: list[Auftrag], s: dict, protokoll_ordner: Path, echt: bool = False,
               fortschritt=None) -> dict:
    """Führt die Aufträge aus (oder simuliert sie mit echt=False).

    ``s``: Sortier-Einstellungen (ziel, modus, umbenennen, name, ordner, tags).
    Liefert Zusammenfassung, Vorschau-Liste und bei echten Läufen Protokolldatei
    und Pfadänderungen (für den Index)."""
    modus = s.get("modus", "kopieren")
    if modus not in ("kopieren", "verschieben", "nur_taggen"):
        raise ValueError("Unbekannter Modus")
    ziel = Path(s["ziel"]) if s.get("ziel") else None
    if modus != "nur_taggen" and not ziel:
        raise ValueError("Bitte einen Zielordner wählen.")
    if modus == "nur_taggen" and s.get("tags") == "keine" and not s.get("umbenennen"):
        raise ValueError("Nichts zu tun: weder umbenennen noch Tags schreiben.")
    tags_modus = s.get("tags", "sidecar")
    if tags_modus == "einbetten" and not shutil.which("exiftool"):
        raise ValueError("Für eingebettete Tags wird exiftool gebraucht. Alternative: XMP-Datei daneben.")

    belegt: set[Path] = set()
    zaehler = {"ok": 0, "duplikat": 0, "fehlt": 0, "fehler": 0, "unveraendert": 0}
    vorschau, fehler, pfad_aenderungen = [], [], []
    prot_datei = prot = exif = None
    if echt:
        protokoll_ordner.mkdir(parents=True, exist_ok=True)
        prot_datei = protokoll_ordner / f"protokoll-{datetime.now():%Y%m%d-%H%M%S}.jsonl"
        prot = open(prot_datei, "a", encoding="utf-8")

    def schreibe(e: dict):
        if prot:
            e["zeit"] = datetime.now().isoformat(timespec="seconds")
            prot.write(json.dumps(e, ensure_ascii=False) + "\n")
            prot.flush()

    try:
        for n, a in enumerate(auftraege, 1):
            if fortschritt:
                fortschritt(n, len(auftraege))
            if not a.pfad.exists():
                zaehler["fehlt"] += 1
                continue
            rel = ziel_relativ(a, s)
            soll = (a.pfad.parent / rel) if modus == "nur_taggen" else (ziel / rel)
            neu, duplikat = _freier_pfad(soll, a.pfad, belegt)
            if duplikat:
                zaehler["duplikat"] += 1
                vorschau.append({"von": str(a.pfad), "nach": str(neu), "art": "duplikat"})
                continue
            belegt.add(neu)
            if neu == a.pfad and tags_modus == "keine":
                zaehler["unveraendert"] += 1
                continue
            if len(vorschau) < 500:
                vorschau.append({"von": str(a.pfad), "nach": str(neu), "art": modus})
            if not echt:
                zaehler["ok"] += 1
                continue
            try:
                neu.parent.mkdir(parents=True, exist_ok=True)
                if modus == "kopieren":
                    shutil.copy2(a.pfad, neu)
                    schreibe({"op": "kopie", "quelle": str(a.pfad), "ziel": str(neu)})
                elif neu != a.pfad:
                    alte_xmp = _sidecars(a.pfad)
                    shutil.move(str(a.pfad), str(neu))
                    schreibe({"op": "verschoben", "quelle": str(a.pfad), "ziel": str(neu), "bild_id": a.bild_id})
                    pfad_aenderungen.append((a.bild_id, str(neu)))
                    # vorhandene XMP-Dateien (z. B. Lightroom-Bearbeitungen) mitnehmen, sonst gehen sie verloren
                    for x in alte_xmp:
                        x_neu = neu.with_name(neu.name + ".xmp") if x.name.lower().endswith(
                            a.pfad.name.lower() + ".xmp") else neu.with_suffix(x.suffix)
                        if not x_neu.exists():
                            shutil.move(str(x), str(x_neu))
                            schreibe({"op": "verschoben", "quelle": str(x), "ziel": str(x_neu)})
                art = tags_modus
                if art == "einbetten" and neu.suffix.lower() not in EINBETTEN_ENDUNGEN:
                    art = "sidecar"  # RAW-Dateien nie direkt verändern; BMP u. a. kann exiftool nicht schreiben
                if art == "sidecar":
                    xmp = neu.with_name(neu.name + ".xmp")
                    if not xmp.exists():
                        xmp.write_text(xmp_sidecar(a), encoding="utf-8")
                        schreibe({"op": "sidecar", "ziel": str(xmp)})
                elif art == "einbetten":
                    sicherung = modus != "kopieren"  # Kopien dürfen direkt geändert werden
                    if sicherung and Path(str(neu) + "_original").exists():
                        raise RuntimeError("Sicherung von einem früheren Lauf vorhanden, bitte erst prüfen")
                    if exif is None:
                        exif = _Exiftool()
                    _einbetten(neu, a, sicherung, exif)
                    if sicherung:
                        schreibe({"op": "eingebettet", "ziel": str(neu), "sicherung": str(neu) + "_original",
                                  "bild_id": a.bild_id})
                        # Dateigröße hat sich geändert: Index nachführen, sonst wird neu analysiert
                        pfad_aenderungen.append((a.bild_id, str(neu)))
                zaehler["ok"] += 1
            except Exception as ex:
                zaehler["fehler"] += 1
                fehler.append(f"{a.pfad}: {ex}")
    finally:
        if prot:
            prot.close()
        if exif is not None:
            exif.schliessen()
    return {"zaehler": zaehler, "vorschau": vorschau, "fehler": fehler[:100],
            "protokoll": str(prot_datei) if prot_datei else None, "pfad_aenderungen": pfad_aenderungen}


def rueckgaengig(protokoll: Path, echt: bool = False) -> dict:
    eintraege = [json.loads(z) for z in protokoll.read_text("utf-8").splitlines() if z.strip()]
    ordner, schritte, probleme, pfad_aenderungen = set(), [], [], []
    for e in reversed(eintraege):
        op, ziel = e["op"], Path(e["ziel"])
        try:
            if op == "kopie":
                if ziel.exists() and Path(e["quelle"]).exists():  # Original muss noch da sein
                    schritte.append(f"Kopie löschen: {ziel}")
                    if echt:
                        ziel.unlink()
                    ordner.add(ziel.parent)
                else:
                    probleme.append(f"Übersprungen (Kopie oder Original fehlt): {ziel}")
            elif op == "verschoben":
                quelle = Path(e["quelle"])
                if ziel.exists() and not quelle.exists():
                    schritte.append(f"Zurück: {ziel} -> {quelle}")
                    if echt:
                        quelle.parent.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(ziel), str(quelle))
                        pfad_aenderungen.append((e.get("bild_id"), str(quelle)))
                    ordner.add(ziel.parent)
                else:
                    probleme.append(f"Übersprungen (Ziel fehlt oder Quelle belegt): {ziel}")
            elif op == "papierkorb":
                from . import papierkorb
                if ziel.exists():
                    probleme.append(f"Übersprungen (Platz schon belegt): {ziel}")
                else:
                    schritte.append(f"Aus dem Papierkorb zurück: {ziel}")
                    if echt and not papierkorb.herausholen(ziel):
                        probleme.append(f"Nicht mehr im Papierkorb: {ziel}")
            elif op == "sidecar":
                if ziel.exists():
                    schritte.append(f"XMP löschen: {ziel}")
                    if echt:
                        ziel.unlink()
            elif op == "eingebettet":
                sicherung = Path(e["sicherung"])
                if sicherung.exists():
                    schritte.append(f"Metadaten wiederherstellen: {ziel}")
                    if echt:
                        sicherung.replace(ziel)
                        pfad_aenderungen.append((e.get("bild_id"), str(ziel)))
        except OSError as ex:
            probleme.append(f"Fehler {ziel}: {ex}")
    if echt:
        for o in sorted(ordner, key=lambda p: len(p.parts), reverse=True):
            while o.exists() and o.is_dir() and not any(o.iterdir()) and len(o.parts) > 2:
                o.rmdir()
                o = o.parent
    return {"schritte": schritte[:500], "anzahl": len(schritte), "probleme": probleme[:100],
            "pfad_aenderungen": pfad_aenderungen}
