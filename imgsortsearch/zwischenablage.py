"""Dateien in die Windows-Zwischenablage legen (im Explorer mit Strg+V einfügbar)."""
from __future__ import annotations

import logging
import struct
import subprocess
import sys
import time
from pathlib import Path

log = logging.getLogger(__name__)

CF_HDROP = 15
GMEM_MOVEABLE = 0x0002
DROPEFFECT_COPY = 1


def dateien_kopieren(pfade: list[str]) -> None:
    pfade = [str(Path(p).resolve()) for p in pfade if Path(p).exists()]
    if not pfade:
        raise ValueError("Keine vorhandenen Dateien ausgewählt.")
    if sys.platform != "win32":
        raise RuntimeError("Dateien kopieren geht nur unter Windows.")
    try:
        _cf_hdrop_setzen(pfade)
    except Exception as ex:  # z. B. Zwischenablage gerade von einem anderen Programm belegt
        log.warning("Zwischenablage direkt nicht möglich (%s), versuche PowerShell", ex)
        _per_powershell(pfade)


def _dropfiles_daten(pfade: list[str]) -> bytes:
    """DROPFILES-Struktur + UTF-16-Pfadliste (doppelt nullterminiert), wie der Explorer sie erwartet."""
    kopf = struct.pack("<IiiII", 20, 0, 0, 0, 1)   # pFiles=20, pt=(0,0), fNC=0, fWide=1
    liste = "".join(p + "\0" for p in pfade) + "\0"
    return kopf + liste.encode("utf-16-le")


def _cf_hdrop_setzen(pfade: list[str]) -> None:
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    u32 = ctypes.WinDLL("user32", use_last_error=True)
    # 64-bit: Handles und Zeiger dürfen nicht auf int gekürzt werden
    k32.GlobalAlloc.restype = wintypes.HGLOBAL
    k32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    k32.GlobalLock.restype = wintypes.LPVOID
    k32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    k32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    k32.GlobalFree.restype = wintypes.HGLOBAL
    k32.GlobalFree.argtypes = [wintypes.HGLOBAL]
    u32.OpenClipboard.argtypes = [wintypes.HWND]
    u32.SetClipboardData.restype = wintypes.HANDLE
    u32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    u32.RegisterClipboardFormatW.restype = wintypes.UINT
    u32.RegisterClipboardFormatW.argtypes = [wintypes.LPCWSTR]

    def speicher(daten: bytes):
        h = k32.GlobalAlloc(GMEM_MOVEABLE, len(daten))
        if not h:
            raise ctypes.WinError(ctypes.get_last_error())
        p = k32.GlobalLock(h)
        if not p:
            k32.GlobalFree(h)
            raise ctypes.WinError(ctypes.get_last_error())
        ctypes.memmove(p, daten, len(daten))
        k32.GlobalUnlock(h)
        return h

    for _ in range(10):  # andere Programme halten die Zwischenablage manchmal kurz offen
        if u32.OpenClipboard(None):
            break
        time.sleep(0.05)
    else:
        raise RuntimeError("Zwischenablage ist gerade belegt")
    try:
        u32.EmptyClipboard()
        h = speicher(_dropfiles_daten(pfade))
        if not u32.SetClipboardData(CF_HDROP, h):
            k32.GlobalFree(h)
            raise ctypes.WinError(ctypes.get_last_error())
        # "Kopieren" statt "Verschieben" beim Einfügen
        fmt = u32.RegisterClipboardFormatW("Preferred DropEffect")
        if fmt:
            h2 = speicher(struct.pack("<I", DROPEFFECT_COPY))
            if not u32.SetClipboardData(fmt, h2):
                k32.GlobalFree(h2)
    finally:
        u32.CloseClipboard()


def _per_powershell(pfade: list[str]) -> None:
    # Pfade über eine Datei übergeben, nie in den Befehl hineinbauen
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8-sig") as f:
        f.write("\n".join(pfade))
        liste = f.name
    skript = Path(__file__).with_name("zwischenablage.ps1")
    try:
        proc = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-STA", "-ExecutionPolicy", "Bypass",
                               "-File", str(skript), liste], text=True, capture_output=True, timeout=60,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    finally:
        Path(liste).unlink(missing_ok=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "Zwischenablage nicht verfügbar")


def im_explorer_zeigen(pfad: str) -> None:
    if sys.platform != "win32":
        raise RuntimeError("Nur unter Windows verfügbar.")
    p = Path(pfad)
    if p.exists():
        # explorer erwartet /select,"Pfad" als EIN Argument; eigene Anführungszeichen, damit Kommas im Namen gehen
        subprocess.Popen(f'explorer.exe /select,"{p}"')
    elif p.parent.exists():
        subprocess.Popen(["explorer.exe", str(p.parent)])
    else:
        raise FileNotFoundError(f"Datei nicht gefunden: {p}")
