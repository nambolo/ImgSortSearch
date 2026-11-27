"""ImgSortSearch: Foto-Backup lokal durchsuchen, ordnen und sortieren."""
import os as _os

# Hinweise von huggingface_hub abstellen, bevor es geladen wird (gilt für Programm und Installation):
# - Windows ohne Entwicklermodus kann keine Symlinks anlegen. Dann legt huggingface_hub jede Datei direkt
#   in den Modellordner (verschoben, nicht kopiert), es entsteht also nichts doppelt. Die Warnung je Modell
#   ist nur verwirrend.
# - Xet: schnelleres Downloadverfahren, braucht ein Zusatzpaket. Ohne lädt es normal über HTTP.
_os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
_os.environ.setdefault("HF_HUB_DISABLE_XET", "1")

__version__ = "0.2.0"
