"""Rechner schonen: Einlesen und Analysieren lassen Prozessor und Grafikkarte Luft für anderes.

Drei Hebel, alle über die Einstellung ``drosselung`` (Standard: an, Stufe "leicht"):

1. Niedrigere Priorität des Programms, solange ein Lauf arbeitet (Windows "Niedriger als normal").
   Andere Programme kommen damit immer zuerst an den Prozessor.
2. Weniger Threads für PyTorch, onnxruntime, OpenCV, Bild-Laden und exiftool.
3. Pausen: Nach jedem Arbeitsschritt (ein Stapel auf der Grafikkarte, ein Bild öffnen, ein Gesicht suchen ...)
   wartet der jeweilige Thread im Verhältnis zur Arbeitszeit. Bei "leicht" (etwa 75 %) ein Drittel der
   Arbeitszeit, bei "stark" (etwa 50 %) genauso lange, wie er gearbeitet hat. Das gilt für Prozessor und
   Grafikkarte, weil die Grafikkarte in der Pause nichts zu rechnen bekommt.

Pausen und Priorität greifen sofort beim Ändern der Einstellung, die Thread-Zahlen beim nächsten Lauf.
"""
from __future__ import annotations

import logging
import os
import sys
import threading
import time

log = logging.getLogger(__name__)

# Anteil der Zeit, in der gerechnet wird
STUFEN = {"leicht": 0.75, "stark": 0.5}
STANDARD = {"aktiv": True, "stufe": "leicht"}
LAENGSTE_PAUSE = 10.0  # Sekunden; Anhalten unterbricht die Pause sofort

# Windows-Prioritätsklassen
NORMAL_PRIORITY_CLASS = 0x20
BELOW_NORMAL_PRIORITY_CLASS = 0x4000

_lock = threading.Lock()
_aktive_laeufe = 0


class Drossel:
    def __init__(self, einstellungen=None):
        self.e = einstellungen

    # ---------------------------------------------------------- Werte

    def _wert(self) -> dict:
        d = self.e.get("drosselung") if self.e is not None else None
        return d if isinstance(d, dict) else STANDARD

    @property
    def aktiv(self) -> bool:
        return bool(self._wert().get("aktiv", True))

    @property
    def anteil(self) -> float:
        """Anteil der Zeit, in der gerechnet wird (1.0 = volle Leistung)."""
        if not self.aktiv:
            return 1.0
        return STUFEN.get(self._wert().get("stufe", "leicht"), STUFEN["leicht"])

    def threads(self, voll: int) -> int:
        """Wie viele Threads statt ``voll`` (mindestens 1)."""
        return max(1, int(voll * self.anteil + 0.5))

    def rechen_threads(self) -> int:
        """Threads für die Rechenbibliotheken (PyTorch, onnxruntime): ohne Drosselung alle Kerne bis auf einen."""
        return self.threads(max(1, (os.cpu_count() or 2) - 1))

    def pausendauer(self, arbeit_sek: float) -> float:
        a = self.anteil
        if a >= 1.0 or arbeit_sek <= 0:
            return 0.0
        return min(LAENGSTE_PAUSE, arbeit_sek * (1.0 / a - 1.0))

    # ---------------------------------------------------------- Pausen

    def pause(self, arbeit_sek: float, stop: threading.Event | None = None) -> None:
        t = self.pausendauer(arbeit_sek)
        if t <= 0:
            return
        if stop is not None:
            stop.wait(t)
        else:
            time.sleep(t)

    def arbeit(self, stop: threading.Event | None = None):
        """``with drossel.arbeit(stop): ...`` misst die Arbeit und pausiert danach passend."""
        d = self

        class _Takt:
            def __enter__(self):
                self.t = time.perf_counter()

            def __exit__(self, *a):
                d.pause(time.perf_counter() - self.t, stop)
                return False
        return _Takt()

    # ---------------------------------------------------------- Priorität und Threads

    def hintergrund(self):
        """Für die Dauer eines Laufs: Priorität senken und Thread-Zahlen der Bibliotheken setzen.
        Laufen mehrere (Indexlauf und Dublettensuche), gilt die normale Priorität erst wieder nach dem letzten."""
        d = self

        class _Lauf:
            def __enter__(self):
                global _aktive_laeufe
                with _lock:
                    _aktive_laeufe += 1
                d.anwenden()

            def __exit__(self, *a):
                global _aktive_laeufe
                with _lock:
                    _aktive_laeufe = max(0, _aktive_laeufe - 1)
                    zuletzt = _aktive_laeufe == 0
                if zuletzt:
                    prioritaet_setzen(False)
                return False
        return _Lauf()

    def anwenden(self) -> None:
        """Priorität und Thread-Zahlen nach der aktuellen Einstellung (auch mitten im Lauf aufrufbar)."""
        with _lock:
            laeuft = _aktive_laeufe > 0
        if laeuft:
            prioritaet_setzen(self.aktiv)
        n = self.rechen_threads()
        try:
            import torch
            torch.set_num_threads(n)
        except Exception:  # torch fehlt (Tests) oder schon in Benutzung
            pass
        try:
            import cv2
            cv2.setNumThreads(n)
        except Exception:
            pass

    def creationflags(self) -> int:
        """Für Unterprozesse (exiftool) unter Windows: niedrige Priorität, wenn gedrosselt."""
        if os.name != "nt" or not self.aktiv:
            return 0
        return BELOW_NORMAL_PRIORITY_CLASS


def prioritaet_setzen(niedrig: bool) -> bool:
    """Priorität des eigenen Prozesses (nur Windows; anderswo lässt sie sich nicht wieder anheben)."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        k32.GetCurrentProcess.restype = ctypes.c_void_p
        k32.SetPriorityClass.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        ok = bool(k32.SetPriorityClass(k32.GetCurrentProcess(),
                                       BELOW_NORMAL_PRIORITY_CLASS if niedrig else NORMAL_PRIORITY_CLASS))
        return ok
    except Exception as e:
        log.info("Priorität nicht änderbar: %s", e)
        return False


# Für Stellen ohne Zugriff auf die Einstellungen (Tests, Werkzeuge): volle Leistung
OHNE = Drossel({"drosselung": {"aktiv": False}})
