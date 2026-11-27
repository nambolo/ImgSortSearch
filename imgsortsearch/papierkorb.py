"""Windows-Papierkorb: Dateien hineinlegen und wieder herausholen.

Hineinlegen geht über die Windows-Shell (wie „Löschen“ im Explorer, mit Rückgängig).
Herausholen liest die Einträge des Papierkorbs direkt: Zu jeder Datei ``$R…`` gibt es
eine kleine Datei ``$I…`` mit dem ursprünglichen Pfad. So lässt sich eine Datei gezielt
an ihren alten Platz zurücklegen, ohne die Explorer-Oberfläche zu bemühen.
"""
from __future__ import annotations

import os
import shutil
import struct
import sys
from pathlib import Path


def verfuegbar() -> bool:
    return sys.platform == "win32"


def hineinlegen(pfad: Path) -> None:
    """Datei in den Papierkorb legen. Wirft OSError, wenn Windows das ablehnt
    (z. B. Netzlaufwerk ohne Papierkorb: dort würde Windows sonst endgültig löschen)."""
    if not verfuegbar():
        raise OSError("Den Papierkorb gibt es nur unter Windows.")
    import ctypes
    from ctypes import wintypes

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT), ("pFrom", wintypes.LPCWSTR),
                    ("pTo", wintypes.LPCWSTR), ("fFlags", ctypes.c_ushort), ("fAnyOperationsAborted", wintypes.BOOL),
                    ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", wintypes.LPCWSTR)]

    if not _hat_papierkorb(pfad):
        raise OSError(f"Laufwerk ohne Papierkorb, Datei bleibt liegen: {pfad}")
    FO_DELETE, FOF_SILENT, FOF_NOCONFIRMATION, FOF_ALLOWUNDO, FOF_NOERRORUI = 3, 0x4, 0x10, 0x40, 0x400
    # Ohne diese Kennung löscht Windows stillschweigend endgültig, wenn die Datei nicht in den Papierkorb
    # passt oder der Papierkorb abgeschaltet ist. So fragt Windows in diesem Fall nach (Nein = bleibt liegen).
    FOF_WANTNUKEWARNING = 0x4000
    op = SHFILEOPSTRUCTW(wFunc=FO_DELETE, pFrom=str(Path(pfad).resolve()) + "\0",
                         fFlags=FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI | FOF_WANTNUKEWARNING)
    erg = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    if erg != 0 or op.fAnyOperationsAborted or Path(pfad).exists():
        raise OSError(f"Konnte nicht in den Papierkorb gelegt werden (Code {erg}): {pfad}")


def _hat_papierkorb(pfad: Path) -> bool:
    """Nur Festplatten haben einen Papierkorb. Auf Netzlaufwerken, USB-Sticks und Speicherkarten
    löscht Windows sofort endgültig (externe USB-Festplatten melden sich als Festplatte)."""
    import ctypes
    laufwerk = os.path.splitdrive(str(Path(pfad).resolve()))[0]
    if not laufwerk or laufwerk.startswith("\\\\"):
        return False
    art = ctypes.windll.kernel32.GetDriveTypeW(laufwerk + "\\")
    return art == 3  # DRIVE_FIXED; 2 = Wechseldatenträger (Stick, SD-Karte) hat keinen Papierkorb


def _ordner(laufwerk: str) -> list[Path]:
    """Papierkorb-Ordner des Laufwerks, die das Programm lesen darf (in der Regel der eigene)."""
    basis = Path(laufwerk + "\\") / "$Recycle.Bin" if laufwerk else None
    if basis is None or not basis.is_dir():
        return []
    erg = []
    try:
        for o in basis.iterdir():
            try:
                if o.is_dir():
                    next(iter(o.iterdir()), None)
                    erg.append(o)
            except OSError:
                pass  # Papierkörbe anderer Benutzer
    except OSError:
        pass
    return erg


def lies_info(datei: Path) -> tuple[str, float]:
    """Ursprünglicher Pfad und Löschzeit aus einer ``$I``-Datei (Windows Vista bis 11)."""
    daten = datei.read_bytes()
    version, _groesse, zeit = struct.unpack_from("<qqq", daten, 0)
    if version == 2:
        (laenge,) = struct.unpack_from("<i", daten, 24)
        pfad = daten[28:28 + 2 * laenge].decode("utf-16-le")
    else:
        pfad = daten[24:24 + 520].decode("utf-16-le")
    return pfad.rstrip("\0"), zeit / 1e7


def herausholen(pfad: Path, ordner: list[Path] | None = None) -> bool:
    """Die zuletzt gelöschte Datei mit diesem ursprünglichen Pfad zurücklegen.
    Liefert False, wenn sie nicht (mehr) im Papierkorb liegt oder der Platz belegt ist."""
    pfad = Path(pfad)
    if pfad.exists():
        return False
    if ordner is None:
        ordner = _ordner(os.path.splitdrive(str(pfad))[0])
    ziel = os.path.normcase(str(pfad))
    beste = None
    for o in ordner:
        for info in o.glob("$I*"):
            try:
                orig, zeit = lies_info(info)
            except (OSError, struct.error, UnicodeDecodeError):
                continue
            if os.path.normcase(orig) == ziel and (beste is None or zeit > beste[1]):
                beste = (info, zeit)
    if beste is None:
        return False
    info = beste[0]
    inhalt = info.with_name("$R" + info.name[2:])
    if not inhalt.exists():
        return False
    pfad.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(inhalt), str(pfad))
    info.unlink(missing_ok=True)
    return True
