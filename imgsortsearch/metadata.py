"""Metadaten lesen: bevorzugt über exiftool, sonst über Pillow.

Liefert pro Bild ein ``BildInfo`` mit Aufnahmedatum (und woher es stammt),
Kamera, GPS-Koordinaten, vorhandenen Schlagwörtern und Software.
"""
from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

BILD_ENDUNGEN = {
    ".jpg", ".jpeg", ".png", ".heic", ".heif", ".tif", ".tiff", ".webp", ".gif",
    ".bmp", ".dng", ".cr2", ".cr3", ".nef", ".arw", ".orf", ".rw2", ".raf",
}
VIDEO_ENDUNGEN = {".mov", ".mp4", ".m4v", ".avi", ".3gp", ".mts"}


@dataclass
class BildInfo:
    pfad: Path
    groesse: int = 0
    datum: datetime | None = None
    datum_quelle: str = ""          # exif | dateiname | dateidatum
    hersteller: str = ""
    modell: str = ""
    gps_lat: float | None = None
    gps_lon: float | None = None
    software: str = ""
    breite: int | None = None
    hoehe: int | None = None
    schlagwoerter: list[str] = field(default_factory=list)
    haengt: bool = False            # exiftool blieb an der Datei hängen (nicht lesbar)

    @property
    def kamera(self) -> str:
        """Kurzname der Kamera, z. B. 'Apple iPhone 15' -> 'iPhone15'."""
        m = self.modell.strip()
        h = self.hersteller.strip()
        if h and m.lower().startswith(h.split()[0].lower()):
            name = m
        else:
            name = m or h
        return re.sub(r"[^A-Za-z0-9]+", "", name) or "Unbekannt"


def exiftool_verfuegbar() -> bool:
    return shutil.which("exiftool") is not None


def finde_bilder(quelle: Path, videos: bool = False) -> list[Path]:
    return [p for p, _, _ in finde_bilder_mit_stat(quelle, videos)]


def finde_bilder_mit_stat(quelle: Path, videos: bool = False, gefunden=None) -> list[tuple[Path, int, float]]:
    """Wie ``finde_bilder``, liefert aber gleich Größe und Änderungszeit mit: (Pfad, Größe, mtime).

    Unter Windows stehen diese Angaben schon im Ordnerverzeichnis (os.scandir), dann muss nicht jede
    Datei einzeln abgefragt werden. Bei 100.000+ Bildern auf einer externen Platte oder einem NAS
    spart das viele Minuten. ``gefunden(n)`` wird ab und zu mit der bisherigen Anzahl aufgerufen."""
    endungen = BILD_ENDUNGEN | (VIDEO_ENDUNGEN if videos else set())
    treffer = []
    stapel = [str(quelle)]
    while stapel:
        ordner = stapel.pop()
        try:
            eintraege = list(os.scandir(ordner))
        except OSError:  # keine Rechte, Laufwerk weg: wie os.walk überspringen
            continue
        for d in eintraege:
            name = d.name
            try:
                if d.is_dir(follow_symlinks=False):
                    # versteckte Ordner (.thumbnails, .Trash, @eaDir ...) überspringen,
                    # Verknüpfungen/Junctions nicht verfolgen (wie os.walk)
                    if not name.startswith((".", "@")) and not getattr(d, "is_junction", lambda: False)():
                        stapel.append(d.path)
                    continue
                if name.startswith("._") or os.path.splitext(name)[1].lower() not in endungen:
                    continue
                st = d.stat()
            except OSError:
                continue
            treffer.append((Path(d.path), st.st_size, st.st_mtime))
            if gefunden is not None and len(treffer) % 2000 == 0:
                gefunden(len(treffer))
    treffer.sort(key=lambda t: t[0])
    return treffer


# ---------------------------------------------------------------- Datum

_EXIF_DATUM = re.compile(r"(\d{4})[:\-](\d{2})[:\-](\d{2})[ T](\d{2}):(\d{2}):(\d{2})")
_NAME_MUSTER = [
    # IMG_20230514_123456, PXL_20230514_123456789, 20230514_123456
    re.compile(r"(?<!\d)(20\d{2}|19\d{2})(\d{2})(\d{2})[_\-T ]?(\d{2})(\d{2})(\d{2})"),
    # 2023-05-14 12.34.56 / 2023-05-14_12-34-56 / "Bildschirmfoto 2023-05-14 um 12.34.56" (Mac) /
    # "WhatsApp Image 2023-05-14 at 12.34.56" / "Screenshot 2023-05-14 123456" (Windows 11)
    re.compile(r"(20\d{2}|19\d{2})-(\d{2})-(\d{2})(?:[ _\-T]|\s+(?:um|at)\s+)"
               r"(\d{2})[.\-:]?(\d{2})[.\-:]?(\d{2})(?!\d)"),
    # IMG-20230514-WA0001 (WhatsApp, nur Tag)
    re.compile(r"(?<!\d)(20\d{2})(\d{2})(\d{2})(?!\d)"),
    re.compile(r"(20\d{2})-(\d{2})-(\d{2})"),
]


def parse_exif_datum(wert) -> datetime | None:
    if not wert or not isinstance(wert, str):
        return None
    m = _EXIF_DATUM.search(wert)
    if not m:
        return None
    try:
        dt = datetime(*map(int, m.groups()))
    except ValueError:
        return None
    if dt.year < 1980:  # 0000:00:00 oder Kamera ohne gestellte Uhr
        return None
    return dt


def datum_aus_dateiname(name: str) -> datetime | None:
    for muster in _NAME_MUSTER:
        m = muster.search(name)
        if not m:
            continue
        teile = list(map(int, m.groups())) + [0, 0, 0]
        try:
            dt = datetime(*teile[:6])
        except ValueError:
            continue
        if 1990 <= dt.year <= datetime.now().year + 1:
            return dt
    return None


# ---------------------------------------------------------------- exiftool

_EXIF_FELDER = [
    "-DateTimeOriginal", "-CreateDate", "-MediaCreateDate", "-Make", "-Model",
    "-GPSLatitude", "-GPSLongitude", "-Software", "-ImageWidth", "-ImageHeight",
    "-Subject", "-Keywords",
]


class ExiftoolLeser:
    """Ein dauerhaft laufendes exiftool (-stay_open) zum Lesen vieler Dateien.

    - Kein Programmstart je Stapel (unter Windows fast eine Sekunde, bei 180.000 Bildern sonst Minuten).
    - Mit ``-progress`` meldet exiftool jede Datei, bevor es sie liest. Kommt eine Zeit lang keine Meldung,
      hängt es an genau dieser Datei (z. B. eine OneDrive-Datei, die nur in der Cloud liegt, oder ein
      Netzlaufwerk, das nicht antwortet). Dann wird exiftool neu gestartet, die Datei übersprungen und der
      Rest weitergelesen. Früher blieb der ganze Indexlauf an so einer Datei für immer stehen."""

    ZEITLIMIT = 60.0     # Sekunden ohne jede Meldung von exiftool, bis eine Datei als hängend gilt
    MAX_HAENGER = 20     # je Stapel; danach den Rest ohne exiftool lesen

    def __init__(self, exe: str | None = None):
        self.exe = exe or shutil.which("exiftool") or "exiftool"
        self.proc = None

    def _starten(self) -> None:
        import queue
        import threading
        self.proc = subprocess.Popen([self.exe, "-stay_open", "True", "-@", "-"], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.zeilen = queue.Queue()
        proc, zeilen = self.proc, self.zeilen

        def lesen(strom, art):
            for zeile in iter(strom.readline, b""):
                zeilen.put((art, zeile))
            zeilen.put((art, None))  # Prozess beendet

        threading.Thread(target=lesen, args=(proc.stdout, "aus"), daemon=True).start()
        threading.Thread(target=lesen, args=(proc.stderr, "err"), daemon=True).start()

    def schliessen(self, hart: bool = False) -> None:
        proc, self.proc = self.proc, None
        if proc is None or proc.poll() is not None:
            return
        try:
            if not hart:
                proc.stdin.write(b"-stay_open\nFalse\n")
                proc.stdin.flush()
                proc.wait(timeout=10)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            pass
        if proc.poll() is None:
            if os.name == "nt":
                # exiftool.exe unter Windows startet selbst ein Perl: den ganzen Prozessbaum beenden
                try:
                    subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True, timeout=15,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                except (OSError, subprocess.TimeoutExpired):
                    pass
            if proc.poll() is None:
                proc.kill()

    def _befehl(self, pfade: list[Path]) -> tuple[list | None, int]:
        """Ein Durchgang. Liefert (Einträge, -1) oder bei Hänger/Absturz (None, Index der hängenden Datei)."""
        import queue
        if self.proc is None or self.proc.poll() is not None:
            self._starten()
        self.nr = getattr(self, "nr", 0) + 1
        marke = f"{{start{self.nr}}}".encode()
        # -echo2: Marke auf stderr, damit späte Meldungen des vorigen Durchgangs nicht mitzählen
        args = ["-echo2", marke.decode(), "-json", "-n", "-fast", "-progress", "-charset", "filename=utf8", *_EXIF_FELDER,
                *(str(p) for p in pfade), "-execute"]
        try:
            self.proc.stdin.write(("\n".join(args) + "\n").encode("utf-8"))
            self.proc.stdin.flush()
        except (OSError, ValueError):
            self.schliessen(hart=True)
            return None, -1
        ausgabe: list[bytes] = []
        aktuell, gestartet = -1, False
        while True:
            try:
                art, zeile = self.zeilen.get(timeout=self.ZEITLIMIT)
            except queue.Empty:
                self.schliessen(hart=True)
                return None, aktuell
            if zeile is None:  # exiftool ist abgestürzt
                self.schliessen(hart=True)
                return None, aktuell
            if art == "err":
                gestartet = gestartet or zeile.strip() == marke
                m = _FORTSCHRITT.search(zeile)
                if gestartet and m and int(m.group(2)) == len(pfade):
                    aktuell = int(m.group(1)) - 1
                continue
            if zeile.strip() == b"{ready}":
                break
            ausgabe.append(zeile)
        text = b"".join(ausgabe).decode("utf-8", "replace")
        try:
            return (json.loads(text) if text.strip() else []), -1
        except ValueError:
            return [], -1

    def lesen(self, pfade: list[Path]) -> tuple[dict[Path, dict], list[Path]]:
        """(Ergebnisse je Pfad, Dateien, an denen exiftool hängen blieb)."""
        rest, haenger = list(pfade), []
        ergebnis: dict[Path, dict] = {}
        while rest:
            eintraege, idx = self._befehl(rest)
            if eintraege is not None:
                for e in eintraege:
                    ergebnis[Path(e.get("SourceFile", ""))] = e
                break
            if not 0 <= idx < len(rest) or len(haenger) >= self.MAX_HAENGER:
                break  # unklar, woran es lag: der Rest wird über Pillow gelesen
            haenger.append(rest.pop(idx))
        return ergebnis, haenger


_FORTSCHRITT = re.compile(rb"\[(\d+)/(\d+)\]\s*$")


def lies_mit_exiftool(pfade: list[Path], stapel: int = 500,
                      leser: ExiftoolLeser | None = None) -> tuple[dict[Path, dict], list[Path]]:
    eigener = leser is None
    leser = leser or ExiftoolLeser()
    ergebnis: dict[Path, dict] = {}
    haenger: list[Path] = []
    try:
        for i in range(0, len(pfade), stapel):
            try:
                erg, h = leser.lesen(pfade[i:i + stapel])
            except OSError:
                continue  # exiftool lässt sich nicht starten: Rest wird über Pillow gelesen
            ergebnis.update(erg)
            haenger += h
    finally:
        if eigener:
            leser.schliessen()
    return ergebnis, haenger


def _als_liste(wert) -> list[str]:
    if wert is None:
        return []
    if isinstance(wert, list):
        return [str(w) for w in wert]
    return [s.strip() for s in str(wert).split(",") if s.strip()]


def _aus_exiftool(info: BildInfo, d: dict) -> None:
    for feld in ("DateTimeOriginal", "CreateDate", "MediaCreateDate"):
        dt = parse_exif_datum(d.get(feld))
        if dt:
            info.datum, info.datum_quelle = dt, "exif"
            break
    info.hersteller = str(d.get("Make", "") or "")
    info.modell = str(d.get("Model", "") or "")
    info.software = str(d.get("Software", "") or "")
    lat, lon = d.get("GPSLatitude"), d.get("GPSLongitude")
    if isinstance(lat, (int, float)) and isinstance(lon, (int, float)) and _gps_gueltig(lat, lon):
        info.gps_lat, info.gps_lon = float(lat), float(lon)
    info.breite = d.get("ImageWidth")
    info.hoehe = d.get("ImageHeight")
    info.schlagwoerter = sorted(set(_als_liste(d.get("Subject")) + _als_liste(d.get("Keywords"))))


# ---------------------------------------------------------------- Pillow

def _gps_gueltig(lat: float, lon: float) -> bool:
    """0/0 (kein Empfang), NaN und Werte außerhalb der Erde gelten als „kein Standort“.
    NaN würde sonst die ganze Karte lahmlegen (lässt sich nicht als JSON senden)."""
    return (math.isfinite(lat) and math.isfinite(lon) and -90 <= lat <= 90 and -180 <= lon <= 180
            and bool(lat or lon))


def _gps_zu_grad(werte, ref) -> float | None:
    try:
        g, m, s = (float(x) for x in werte)
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    # manche Kameras schreiben 0/0, solange sie keinen Empfang haben (ergibt NaN)
    if math.isnan(g):
        return None
    m = 0.0 if math.isnan(m) else m
    s = 0.0 if math.isnan(s) else s
    grad = g + m / 60 + s / 3600
    if str(ref).upper() in ("S", "W"):
        grad = -grad
    return grad


def _aus_pillow(info: BildInfo) -> None:
    try:
        from PIL import Image
    except ImportError:
        return
    try:
        # Alles lesen, solange die Datei offen ist: get_ifd() liest bei manchen Formaten
        # (TIFF, einige JPEG/HEIC) erst beim Aufruf nach -> "seek of closed file".
        with Image.open(info.pfad) as img:
            info.breite, info.hoehe = img.size
            _exif_auswerten(info, img.getexif())
    except Exception:
        pass  # kaputte oder unlesbare EXIF-Daten: Datum kommt dann aus Dateiname/Dateidatum


def _exif_auswerten(info: BildInfo, exif) -> None:
    if not exif:
        return
    info.hersteller = str(exif.get(271, "") or "").strip("\x00 ")
    info.modell = str(exif.get(272, "") or "").strip("\x00 ")
    info.software = str(exif.get(305, "") or "").strip("\x00 ")
    sub = exif.get_ifd(0x8769)
    for tag in (36867, 36868):  # DateTimeOriginal, DateTimeDigitized
        dt = parse_exif_datum(sub.get(tag))
        if dt:
            info.datum, info.datum_quelle = dt, "exif"
            break
    if not info.datum:
        dt = parse_exif_datum(exif.get(306))
        if dt:
            info.datum, info.datum_quelle = dt, "exif"
    gps = exif.get_ifd(0x8825)
    if gps and 2 in gps and 4 in gps:
        lat = _gps_zu_grad(gps.get(2), gps.get(1, "N"))
        lon = _gps_zu_grad(gps.get(4), gps.get(3, "E"))
        if lat is not None and lon is not None and _gps_gueltig(lat, lon):
            info.gps_lat, info.gps_lon = lat, lon


# ---------------------------------------------------------------- gesamt

def lies_metadaten(pfade: list[Path], exiftool: bool | None = None,
                   leser: ExiftoolLeser | None = None) -> list[BildInfo]:
    if exiftool is None:
        exiftool = exiftool_verfuegbar()
    roh, haenger = lies_mit_exiftool(pfade, leser=leser) if exiftool else ({}, [])
    haenger = set(haenger)
    ergebnis = []
    for p in pfade:
        if p in haenger:
            # Nicht noch einmal mit Pillow öffnen, das bliebe genauso hängen
            info = BildInfo(pfad=p, haengt=True)
            dt = datum_aus_dateiname(p.name)
            if dt:
                info.datum, info.datum_quelle = dt, "dateiname"
            ergebnis.append(info)
            continue
        try:
            ergebnis.append(_eine_datei(p, roh))
        except Exception:  # ein einzelnes Bild darf nie den ganzen Lauf abbrechen
            info = BildInfo(pfad=p)
            dt = datum_aus_dateiname(p.name)
            if dt:
                info.datum, info.datum_quelle = dt, "dateiname"
            ergebnis.append(info)
    return ergebnis


def _eine_datei(p: Path, roh: dict) -> BildInfo:
    info = BildInfo(pfad=p)
    try:
        st = p.stat()
        info.groesse = st.st_size
    except OSError:
        st = None
    d = roh.get(p) or roh.get(Path(str(p).replace("\\", "/")))
    if d:
        _aus_exiftool(info, d)
        if info.gps_lat is None and d.get("GPSLatitude") == "" and d.get("GPSLongitude") == "":
            # exiftool gibt bei Sekunden 0/0 (manche Handys) nichts aus, Grad und Minuten stimmen aber
            ersatz = BildInfo(pfad=p)
            _aus_pillow(ersatz)
            info.gps_lat, info.gps_lon = ersatz.gps_lat, ersatz.gps_lon
    else:
        _aus_pillow(info)
    if not info.datum:
        dt = datum_aus_dateiname(p.name)
        if dt:
            info.datum, info.datum_quelle = dt, "dateiname"
    if not info.datum and st:
        info.datum, info.datum_quelle = datetime.fromtimestamp(st.st_mtime), "dateidatum"
    return info
