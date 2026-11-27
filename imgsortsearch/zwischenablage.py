"""Dateien in die Windows-Zwischenablage legen (im Explorer mit Strg+V einfügbar)."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def dateien_kopieren(pfade: list[str]) -> None:
    pfade = [str(Path(p)) for p in pfade if Path(p).exists()]
    if not pfade:
        raise ValueError("Keine vorhandenen Dateien ausgewählt.")
    if sys.platform != "win32":
        raise RuntimeError("Dateien kopieren geht nur unter Windows.")
    # Pfade über eine Datei übergeben, nie in den Befehl hineinbauen
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8-sig") as f:
        f.write("\n".join(pfade))
        liste = f.name
    skript = Path(__file__).with_name("zwischenablage.ps1")
    try:
        proc = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                               "-File", str(skript), liste], text=True, capture_output=True,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    finally:
        Path(liste).unlink(missing_ok=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "Zwischenablage nicht verfügbar")


def im_explorer_zeigen(pfad: str) -> None:
    if sys.platform == "win32":
        subprocess.Popen(["explorer", "/select,", str(Path(pfad))])
