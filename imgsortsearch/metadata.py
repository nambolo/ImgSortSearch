"""Metadaten lesen: bevorzugt über exiftool, sonst über Pillow.

Liefert pro Bild ein ``BildInfo`` mit Aufnahmedatum (und woher es stammt),
Kamera, GPS-Koordinaten, vorhandenen Schlagwörtern und Software.
"""
from __future__ import annotations

import json
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
    endungen = BILD_ENDUNGEN | (VIDEO_ENDUNGEN if videos else set())
    treffer = []
    for wurzel, ordner, dateien in os.walk(quelle):
        # versteckte Ordner (.thumbnails, .Trash, @eaDir ...) überspringen
        ordner[:] = [o for o in ordner if not o.startswith((".", "@"))]
        for d in dateien:
            if d.startswith("._"):
                continue
            p = Path(wurzel) / d
            if p.suffix.lower() in endungen:
                treffer.append(p)
    return sorted(treffer)


# ---------------------------------------------------------------- Datum

_EXIF_DATUM = re.compile(r"(\d{4})[:\-](\d{2})[:\-](\d{2})[ T](\d{2}):(\d{2}):(\d{2})")
_NAME_MUSTER = [
    # IMG_20230514_123456, PXL_20230514_123456789, 20230514_123456
    re.compile(r"(?<!\d)(20\d{2}|19\d{2})(\d{2})(\d{2})[_\-T ]?(\d{2})(\d{2})(\d{2})"),
    # 2023-05-14 12.34.56 / 2023-05-14_12-34-56
    re.compile(r"(20\d{2}|19\d{2})-(\d{2})-(\d{2})[ _\-T](\d{2})[.\-:](\d{2})[.\-:](\d{2})"),
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


def lies_mit_exiftool(pfade: list[Path], stapel: int = 500) -> dict[Path, dict]:
    ergebnis: dict[Path, dict] = {}
    for i in range(0, len(pfade), stapel):
        teil = pfade[i:i + stapel]
        cmd = [shutil.which("exiftool") or "exiftool", "-json", "-n", "-fast", "-charset", "filename=utf8",
               *_EXIF_FELDER, "-@", "-"]
        eingabe = "\n".join(str(p) for p in teil)
        try:
            # ohne CREATE_NO_WINDOW blitzt unter Windows bei jedem Stapel ein Konsolenfenster auf
            proc = subprocess.run(cmd, input=eingabe.encode("utf-8"), capture_output=True,
                                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            eintraege = json.loads(proc.stdout.decode("utf-8", "replace")) if proc.stdout.strip() else []
        except (OSError, ValueError):
            continue  # Rest wird über Pillow gelesen
        for eintrag in eintraege:
            ergebnis[Path(eintrag.get("SourceFile", ""))] = eintrag
    return ergebnis


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
    if isinstance(lat, (int, float)) and isinstance(lon, (int, float)) and (lat or lon):
        info.gps_lat, info.gps_lon = float(lat), float(lon)
    info.breite = d.get("ImageWidth")
    info.hoehe = d.get("ImageHeight")
    info.schlagwoerter = sorted(set(_als_liste(d.get("Subject")) + _als_liste(d.get("Keywords"))))


# ---------------------------------------------------------------- Pillow

def _gps_zu_grad(werte, ref) -> float | None:
    try:
        g, m, s = (float(x) for x in werte)
    except (TypeError, ValueError):
        return None
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
        with Image.open(info.pfad) as img:
            info.breite, info.hoehe = img.size
            exif = img.getexif()
    except Exception:
        return
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
        if lat is not None and lon is not None and (lat or lon):
            info.gps_lat, info.gps_lon = lat, lon


# ---------------------------------------------------------------- gesamt

def lies_metadaten(pfade: list[Path], exiftool: bool | None = None) -> list[BildInfo]:
    if exiftool is None:
        exiftool = exiftool_verfuegbar()
    roh = lies_mit_exiftool(pfade) if exiftool else {}
    ergebnis = []
    for p in pfade:
        info = BildInfo(pfad=p)
        try:
            st = p.stat()
            info.groesse = st.st_size
        except OSError:
            st = None
        d = roh.get(p) or roh.get(Path(str(p).replace("\\", "/")))
        if d:
            _aus_exiftool(info, d)
        else:
            _aus_pillow(info)
        if not info.datum:
            dt = datum_aus_dateiname(p.name)
            if dt:
                info.datum, info.datum_quelle = dt, "dateiname"
        if not info.datum and st:
            info.datum, info.datum_quelle = datetime.fromtimestamp(st.st_mtime), "dateidatum"
        ergebnis.append(info)
    return ergebnis
