"""Lokale Modelle: Bilder laden, CLIP (Inhalt + Suche), Gesichter, OCR.

Alle Modelle laufen auf dem eigenen Rechner. Beim ersten Start werden die
Modelldateien einmalig in ``daten/modelle`` geladen, danach geht alles offline.
"""
from __future__ import annotations

import io
import logging
import os
import shutil
import subprocess
import sys
import threading
import urllib.request
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps

from .einstellungen import daten_ordner

log = logging.getLogger(__name__)

try:  # HEIC/HEIF (iPhone)
    import pillow_heif
    pillow_heif.register_heif_opener()
except ImportError:
    pass

Image.MAX_IMAGE_PIXELS = 300_000_000


def modell_ordner() -> Path:
    p = daten_ordner() / "modelle"
    p.mkdir(parents=True, exist_ok=True)
    return p


def lade_datei(urls: list[str], ziel: Path, stopp: threading.Event | None = None) -> Path:
    """Lädt eine Datei (erste Quelle, die klappt). Ein abgebrochener Download bleibt als ``.part``
    liegen und wird beim nächsten Versuch fortgesetzt statt von vorn begonnen."""
    if ziel.exists() and ziel.stat().st_size > 0:
        return ziel
    letzter = None
    tmp = ziel.with_name(ziel.name + ".part")
    for url in urls:
        try:
            log.info("Lade %s", url)
            _teil_laden(url, tmp, stopp=stopp)
            tmp.replace(ziel)
            return ziel
        except InterruptedError:
            raise
        except Exception as e:  # nächste Quelle probieren
            letzter = e
    raise RuntimeError(f"Modell konnte nicht geladen werden ({ziel.name}): {letzter}")


def _teil_laden(url: str, teil: Path, groesse: int = 0, fortschritt=None, stopp: threading.Event | None = None):
    """Lädt ``url`` in die Datei ``teil`` und setzt dabei einen früheren Teil-Download fort (HTTP-Range).

    ``groesse`` (falls bekannt) wird geprüft; ``fortschritt(bytes)`` meldet den Stand."""
    import urllib.error
    start = teil.stat().st_size if teil.exists() else 0
    if groesse and start > groesse:
        teil.unlink()
        start = 0
    if groesse and start == groesse:
        return  # schon vollständig, nur noch nicht umbenannt
    kopf = {"User-Agent": "ImgSortSearch/1.0"}
    if start:
        kopf["Range"] = f"bytes={start}-"
    try:
        r = urllib.request.urlopen(urllib.request.Request(url, headers=kopf), timeout=120)
    except urllib.error.HTTPError as e:
        if e.code != 416 or not start:
            raise
        # Bereich nicht erfüllbar: der Teil ist schon komplett (Größe unbekannt) oder kaputt -> von vorn
        teil.unlink()
        return _teil_laden(url, teil, groesse, fortschritt, stopp)
    with r:
        if start and r.status != 206:  # Server kann nicht fortsetzen: von vorn
            start = 0
        if not groesse:
            laenge = r.headers.get("Content-Length")
            groesse = start + int(laenge) if laenge and laenge.isdigit() else 0
        with open(teil, "ab" if start else "wb") as f:
            geladen = start
            while True:
                if stopp is not None and stopp.is_set():
                    raise InterruptedError
                block = r.read(1 << 20)
                if not block:
                    break
                f.write(block)
                geladen += len(block)
                if fortschritt:
                    fortschritt(geladen)
    if groesse and teil.stat().st_size != groesse:
        raise RuntimeError(f"{teil.name} unvollständig ({teil.stat().st_size} von {groesse} Bytes)")


# ------------------------------------------------------------------ Bilder

RAW_ENDUNGEN = {".dng", ".cr2", ".cr3", ".nef", ".arw", ".orf", ".rw2", ".raf"}


def _als_rgb(img: Image.Image) -> Image.Image:
    """RGB ohne Überraschungen: Transparenz auf Weiß (sonst schwarzer Text auf schwarzem Grund
    bei PNG-Screenshots), 16-Bit-Graustufen (TIFF/PNG) richtig skaliert statt fast weiß."""
    if img.mode in ("I;16", "I;16L", "I;16B", "I;16N", "I", "F"):
        arr = np.asarray(img, dtype=np.float32)
        hoch = float(arr.max()) or 1.0
        return Image.fromarray((arr / hoch * 255).clip(0, 255).astype(np.uint8), "L").convert("RGB")
    if img.mode in ("RGBA", "LA", "PA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        weiss = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
        return Image.alpha_composite(weiss, rgba).convert("RGB")
    return img.convert("RGB")


def bild_laden(pfad: Path, max_kante: int | None = None) -> Image.Image:
    """Öffnet ein Bild (auch HEIC, RAW über die eingebettete Vorschau), richtig gedreht, RGB."""
    pfad = Path(pfad)
    img = None
    if pfad.suffix.lower() not in RAW_ENDUNGEN:
        try:
            img = Image.open(pfad)
            if max_kante:
                # Schnelles JPEG-Verkleinern schon beim Dekodieren (1/2, 1/4, 1/8). draft() liefert mindestens
                # die gewünschte Größe in BEIDEN Richtungen; mit einem Quadrat würde ein 4000x3000-Foto deshalb
                # voll dekodiert (Höhe 1500 < 2000). Darum die Zielgröße im Seitenverhältnis angeben.
                w, h = img.size
                f = max_kante / max(w, h, 1)
                if f < 1:
                    img.draft("RGB", (max(1, int(w * f)), max(1, int(h * f))))
            img.load()
        except Exception:
            img = None
    if img is None:
        exiftool = shutil.which("exiftool")
        if not exiftool:
            raise ValueError(f"Bildformat wird nicht unterstützt: {pfad.name}")
        for tag in ("-JpgFromRaw", "-PreviewImage", "-ThumbnailImage"):
            try:
                daten = subprocess.run([exiftool, "-b", tag, str(pfad)], capture_output=True, timeout=60,
                                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
            except subprocess.TimeoutExpired:
                daten = b""  # hängende Datei darf den Indexlauf nicht blockieren
            if daten:
                img = Image.open(io.BytesIO(daten))
                img.load()
                break
        else:
            raise ValueError(f"Keine Vorschau in {pfad.name}")
    img = ImageOps.exif_transpose(img)
    if img.mode != "RGB":
        img = _als_rgb(img)
    if max_kante:
        img.thumbnail((max_kante, max_kante))
    return img


# ------------------------------------------------------------------ Grafikkarte

def waehle_geraet(wunsch: str = "auto"):
    """Liefert (torch-Gerät, Beschreibung).

    auto: NVIDIA (CUDA) > Intel (XPU) > DirectML (AMD/Intel/alle DirectX-12-Grafikkarten unter Windows) > CPU.
    DirectML nutzt bei Intel-Grafik im Laptop automatisch den gemeinsamen Arbeitsspeicher."""
    import torch
    wunsch = (wunsch or "auto").lower()
    if wunsch in ("auto", "cuda") and torch.cuda.is_available():
        return "cuda", f"NVIDIA CUDA ({torch.cuda.get_device_name(0)})"
    if wunsch in ("auto", "xpu") and hasattr(torch, "xpu") and torch.xpu.is_available():
        return "xpu", f"Intel XPU ({torch.xpu.get_device_name(0)})"
    if wunsch in ("auto", "directml"):
        try:
            import torch_directml
            if torch_directml.is_available():
                namen = [torch_directml.device_name(i) for i in range(torch_directml.device_count())]
                i = dml_karte_waehlen(namen)
                return torch_directml.device(i), f"DirectML ({namen[i]})"
        except Exception as e:  # Paket fehlt oder Treiber zu alt
            if wunsch == "directml":
                log.warning("DirectML nicht verfügbar: %s", e)
    return "cpu", "CPU (Prozessor)"


def dml_karte_waehlen(namen: list[str]) -> int:
    """Index der DirectML-Grafikkarte, die am meisten taugt.

    Ist neben der eigentlichen Grafikkarte noch die Prozessorgrafik aktiv (Intel UHD, AMD Radeon Graphics),
    zählt Windows oft diese zuerst. Dedizierte Karten (Radeon RX/Pro, NVIDIA, Intel Arc) gehen vor,
    der Software-Treiber "Microsoft Basic Render Driver" zuletzt."""
    import re

    def wert(name: str) -> int:
        if re.search(r"Microsoft|Basic Render", name, re.I):
            return 0
        if re.search(r"NVIDIA|Radeon\s*(\(TM\)\s*)?(RX|Pro|VII)|\bArc\b|FirePro", name, re.I):
            return 2
        return 1
    if not namen:
        return 0
    return max(range(len(namen)), key=lambda i: (wert(namen[i]), -i))


class _GeraetMitRueckfall:
    """Führt Berechnungen auf der Grafikkarte aus und weicht bei Fehlern einmalig auf die CPU aus.

    Manche Grafiktreiber oder DirectML können einzelne Operationen nicht. Dann
    läuft der Rest des Laufs eben auf dem Prozessor statt abzubrechen."""

    geraet = "cpu"
    geraet_name = "CPU (Prozessor)"
    _modelle: tuple = ()
    _rechen_lock = threading.RLock()  # wird je Modell in __init__ ersetzt

    def _auf_cpu(self, fehler: Exception):
        log.warning("Grafikkarte (%s) fehlgeschlagen, weiter auf der CPU: %s", self.geraet_name, fehler)
        for name in self._modelle:
            m = getattr(self, name, None)
            if m is not None:
                setattr(self, name, m.to("cpu"))
        self.geraet = "cpu"
        self.geraet_name = f"CPU (Grafikkarte fehlgeschlagen: {str(fehler)[:120]})"

    def _mit_rueckfall(self, funktion):
        # Indexlauf und Suche nutzen dasselbe Modell aus verschiedenen Threads. DirectML/CUDA
        # sind dafür nicht ausgelegt, und der Wechsel auf die CPU darf nicht mitten in einer
        # anderen Berechnung passieren.
        with self._rechen_lock:
            try:
                return funktion()
            except (RuntimeError, NotImplementedError) as e:
                if str(self.geraet) == "cpu":
                    raise
                self._auf_cpu(e)
                return funktion()


# ------------------------------------------------------------------ CLIP

PROMPTS_TEXT = [
    "ein Screenshot mit Text", "ein Foto von einem Dokument mit Text", "ein Foto von einem Schild mit Schrift",
    "ein Whiteboard mit Schrift", "eine Präsentationsfolie mit Text", "ein Foto einer Seite in einem Buch",
]
PROMPTS_KEIN_TEXT = [
    "ein Foto von Menschen", "ein Foto von einer Landschaft", "ein Foto von einem Tier",
    "ein Foto von Essen", "ein Foto von einem Gebäude", "ein Foto von einem Auto", "ein Foto von Pflanzen",
]


class ClipModell(_GeraetMitRueckfall):
    """Mehrsprachiges CLIP (versteht deutsche Suchbegriffe)."""

    _modelle = ("model",)

    def __init__(self, modell: str, pretrained: str, beschleunigung: str = "auto"):
        import open_clip
        import torch
        self.torch = torch
        self._rechen_lock = threading.RLock()
        torch.set_num_threads(max(1, (os.cpu_count() or 2) - 1))
        self.geraet, self.geraet_name = waehle_geraet(beschleunigung)
        self.model, _, self.preprocess = open_clip.create_model_and_transforms(
            modell, pretrained=pretrained, cache_dir=str(modell_ordner() / "clip"))
        self.model.eval()
        try:
            self.model = self.model.to(self.geraet)
        except Exception as e:
            self._auf_cpu(e)
        self.tokenizer = open_clip.get_tokenizer(modell, cache_dir=str(modell_ordner() / "clip"))
        self._text_cache: dict[str, np.ndarray] = {}
        self.kennung = f"{modell}/{pretrained}"

    def bilder(self, bilder: list[Image.Image]) -> np.ndarray:
        return self._mit_rueckfall(lambda: self._bilder(bilder))

    def _bilder(self, bilder):
        torch = self.torch
        with torch.no_grad():
            x = torch.stack([self.preprocess(b) for b in bilder]).to(self.geraet)
            f = self.model.encode_image(x).float()
            f = f / f.norm(dim=-1, keepdim=True)
        return f.cpu().numpy()

    def texte(self, texte: list[str]) -> np.ndarray:
        fehlend = [t for t in texte if t not in self._text_cache]
        if fehlend:
            for t, v in zip(fehlend, self._mit_rueckfall(lambda: self._texte(fehlend))):
                self._text_cache[t] = v
        return np.stack([self._text_cache[t] for t in texte])

    def _texte(self, texte):
        torch = self.torch
        with torch.no_grad():
            f = self.model.encode_text(self.tokenizer(texte).to(self.geraet)).float()
            f = f / f.norm(dim=-1, keepdim=True)
        return f.cpu().numpy()


def softmax(x: np.ndarray, temperatur: float = 100.0) -> np.ndarray:
    z = x * temperatur
    z = z - z.max(axis=-1, keepdims=True)
    e = np.exp(z)
    return e / e.sum(axis=-1, keepdims=True)


# ------------------------------------------------------------------ Gesichter

# InsightFace "buffalo_l": SCRFD-10G findet Gesichter samt 5 Merkmalspunkten (Augen, Nase, Mundwinkel),
# ArcFace (ResNet-50, trainiert auf WebFace600K) erzeugt 512er-Merkmale. Deutlich genauer als
# YuNet/SFace, besonders bei Licht, Alter, Brillen und leicht gedrehten Köpfen.
# Lizenz der Modelle: nur für nicht-kommerzielle Nutzung (privates Fotoarchiv passt).
BUFFALO = ["https://github.com/deepinsight/insightface/releases/download/v0.7/buffalo_l.zip"]
GESICHT_DATEIEN = ("det_10g.onnx", "w600k_r50.onnx")
GESICHT_KENNUNG = "buffalo_l/scrfd10g+w600k_r50"

# Zielpositionen der 5 Merkmalspunkte im 112x112-Ausschnitt, auf den ArcFace trainiert ist
ARCFACE_PUNKTE = np.array([[38.2946, 51.6963], [73.5318, 51.5014], [56.0252, 71.7366],
                           [41.5493, 92.3655], [70.7299, 92.2041]], dtype=np.float32)


def gesichtsmodelle_laden() -> tuple[Path, Path]:
    """Lädt buffalo_l einmalig und behält nur die beiden benötigten Dateien (ca. 190 MB)."""
    ordner = modell_ordner() / "buffalo_l"
    ordner.mkdir(parents=True, exist_ok=True)
    ziele = [ordner / n for n in GESICHT_DATEIEN]
    if all(z.exists() and z.stat().st_size > 0 for z in ziele):
        return ziele[0], ziele[1]
    import zipfile
    zip_pfad = lade_datei(BUFFALO, ordner / "buffalo_l.zip")
    try:
        with zipfile.ZipFile(zip_pfad) as zf:
            for ziel in ziele:
                eintrag = next(n for n in zf.namelist() if n.rsplit("/", 1)[-1] == ziel.name)
                tmp = ziel.with_suffix(".part")
                with zf.open(eintrag) as quelle, open(tmp, "wb") as f:
                    shutil.copyfileobj(quelle, f)
                tmp.replace(ziel)
    except (zipfile.BadZipFile, StopIteration) as e:
        zip_pfad.unlink(missing_ok=True)  # beim nächsten Start neu laden
        raise RuntimeError(f"Gesichtsmodell-Archiv unbrauchbar: {e}")
    zip_pfad.unlink(missing_ok=True)
    return ziele[0], ziele[1]


def _aehnlichkeits_transformation(quelle: np.ndarray, ziel: np.ndarray) -> np.ndarray:
    """Drehung + Skalierung + Verschiebung (Umeyama), die die Punkte quelle bestmöglich auf ziel legt. 2x3."""
    mq, mz = quelle.mean(0), ziel.mean(0)
    q, z = quelle - mq, ziel - mz
    kov = z.T @ q / len(quelle)
    u, s, vt = np.linalg.svd(kov)
    d = np.ones(2)
    if np.linalg.det(u) * np.linalg.det(vt) < 0:
        d[1] = -1
    r = u @ np.diag(d) @ vt
    skala = (s * d).sum() / ((q ** 2).sum() / len(quelle))
    m = np.zeros((2, 3), dtype=np.float32)
    m[:, :2] = skala * r
    m[:, 2] = mz - skala * r @ mq
    return m


def _nms(boxen: np.ndarray, scores: np.ndarray, schwelle: float = 0.4) -> list[int]:
    x1, y1, x2, y2 = boxen.T
    flaeche = (x2 - x1) * (y2 - y1)
    reihenfolge = scores.argsort()[::-1]
    behalten = []
    while reihenfolge.size:
        i = reihenfolge[0]
        behalten.append(int(i))
        xx1 = np.maximum(x1[i], x1[reihenfolge[1:]])
        yy1 = np.maximum(y1[i], y1[reihenfolge[1:]])
        xx2 = np.minimum(x2[i], x2[reihenfolge[1:]])
        yy2 = np.minimum(y2[i], y2[reihenfolge[1:]])
        schnitt = np.maximum(0, xx2 - xx1) * np.maximum(0, yy2 - yy1)
        iou = schnitt / (flaeche[i] + flaeche[reihenfolge[1:]] - schnitt + 1e-9)
        reihenfolge = reihenfolge[1:][iou <= schwelle]
    return behalten


def gesichts_qualitaet(score: float, kante: float, frontal: float, schaerfe: float) -> float:
    """0..1: wie verlässlich das Merkmal ist. Kleine, unscharfe, seitliche oder unsichere
    Gesichter bekommen wenig. Nur gute Gesichter dürfen neue Personen bilden."""
    q_score = np.clip((score - 0.5) / 0.3, 0, 1)
    q_groesse = np.clip((kante - 24) / (80 - 24), 0, 1)
    q_frontal = np.clip((frontal - 0.25) / 0.5, 0, 1)
    q_schaerfe = np.clip((np.log10(max(schaerfe, 1)) - 1.3) / 1.0, 0, 1)
    return float(q_score * (0.4 + 0.6 * q_groesse) * (0.3 + 0.7 * q_frontal) * (0.5 + 0.5 * q_schaerfe))


class GesichtsModell:
    """Gesichter finden (SCRFD), ausrichten und Merkmale berechnen (ArcFace). Läuft mit onnxruntime."""

    kennung = GESICHT_KENNUNG
    dimension = 512

    def __init__(self, min_groesse: int = 32, min_score: float = 0.6, eingabe: int = 640,
                 beschleunigung: str = "cpu"):
        import cv2
        self.cv2 = cv2
        self._dateien = gesichtsmodelle_laden()
        self.min_groesse = min_groesse
        self.min_score = min_score
        self.eingabe = eingabe
        self._anker: dict = {}
        self._lock = threading.Lock()
        self.geraet_name = "CPU (Prozessor)"
        if (beschleunigung or "auto").lower() in ("auto", "directml") and self._dml_versuchen():
            return
        self._sitzungen(["CPUExecutionProvider"])

    def _sitzungen(self, anbieter: list) -> None:
        import onnxruntime as ort
        det, rec = self._dateien
        opt = ort.SessionOptions()
        opt.log_severity_level = 3
        if anbieter[0] == "CPUExecutionProvider":
            opt.intra_op_num_threads = max(1, (os.cpu_count() or 2) - 1)
        else:  # DirectML verlangt das so
            opt.enable_mem_pattern = False
            opt.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        self.det = ort.InferenceSession(str(det), opt, providers=anbieter)
        self.rec = ort.InferenceSession(str(rec), opt, providers=anbieter)
        self.det_in = self.det.get_inputs()[0].name
        self.rec_in = self.rec.get_inputs()[0].name

    def _dml_versuchen(self) -> bool:
        """Gesichter auf der Grafikkarte (onnxruntime-directml, nur AMD/Intel-Installation). Klappt das nicht
        oder rechnet es falsch, bleibt alles auf dem Prozessor."""
        try:
            import onnxruntime as ort
            if "DmlExecutionProvider" not in ort.get_available_providers():
                return False
            for optionen in ({"performance_preference": "high_performance", "device_filter": "gpu"}, {}):
                try:
                    self._sitzungen([("DmlExecutionProvider", optionen), "CPUExecutionProvider"])
                    if self.det.get_providers()[0] == "DmlExecutionProvider":
                        break
                except Exception as e:
                    log.info("Gesichter mit DirectML (%s) nicht möglich: %s", optionen or "Standard", e)
            else:
                return False
            # Probe: dieselben Eingaben auf Grafikkarte und Prozessor müssen dasselbe ergeben
            rng = np.random.default_rng(0)
            det_x = rng.uniform(-1, 1, (1, 3, self.eingabe, self.eingabe)).astype(np.float32)
            rec_x = rng.uniform(-1, 1, (2, 3, 112, 112)).astype(np.float32)
            gpu = (self.det.run(None, {self.det_in: det_x})[0], self.rec.run(None, {self.rec_in: rec_x})[0])
            dml_det, dml_rec = self.det, self.rec
            self._sitzungen(["CPUExecutionProvider"])
            cpu = (self.det.run(None, {self.det_in: det_x})[0], self.rec.run(None, {self.rec_in: rec_x})[0])
            if not all(np.allclose(a, b, atol=2e-2, rtol=2e-2) for a, b in zip(gpu, cpu)):
                log.warning("Gesichter mit DirectML rechnen abweichend, bleibe auf dem Prozessor")
                return False
            self.det, self.rec = dml_det, dml_rec
            self.geraet_name = "DirectML (Grafikkarte)"
            log.info("Gesichtserkennung läuft auf der Grafikkarte (DirectML)")
            return True
        except Exception as e:
            log.warning("Gesichter mit DirectML nicht möglich, nutze den Prozessor: %s", e)
            return False

    def _ausfuehren(self, sitzung: str, eingabe: np.ndarray):
        s = getattr(self, sitzung)
        name = self.det_in if sitzung == "det" else self.rec_in
        try:
            return s.run(None, {name: eingabe})
        except Exception as e:
            if self.geraet_name.startswith("CPU"):
                raise
            log.warning("Gesichter auf der Grafikkarte fehlgeschlagen, weiter auf dem Prozessor: %s", e)
            self._sitzungen(["CPUExecutionProvider"])
            self.geraet_name = f"CPU (Grafikkarte fehlgeschlagen: {str(e)[:120]})"
            return getattr(self, sitzung).run(None, {name: eingabe})

    # --- Erkennung
    def _anker_punkte(self, h: int, w: int, schritt: int) -> np.ndarray:
        k = (h, w, schritt)
        if k not in self._anker:
            gy, gx = np.mgrid[:h, :w]
            p = (np.stack([gx, gy], axis=-1).astype(np.float32) * schritt).reshape(-1, 2)
            self._anker[k] = np.repeat(p, 2, axis=0)  # 2 Anker je Position
        return self._anker[k]

    def _erkennen(self, rgb: np.ndarray, schwelle: float):
        cv2 = self.cv2
        h, w = rgb.shape[:2]
        n = self.eingabe
        faktor = n / max(h, w)
        nh, nw = max(1, round(h * faktor)), max(1, round(w * faktor))
        leinwand = np.zeros((n, n, 3), dtype=np.float32)
        leinwand[:nh, :nw] = cv2.resize(rgb, (nw, nh)).astype(np.float32)
        blob = ((leinwand - 127.5) / 128.0).transpose(2, 0, 1)[None]
        aus = self._ausfuehren("det", np.ascontiguousarray(blob))
        boxen, scores, punkte = [], [], []
        for i, schritt in enumerate((8, 16, 32)):
            s = aus[i].reshape(-1)
            b = aus[i + 3].reshape(-1, 4) * schritt
            k = aus[i + 6].reshape(-1, 10) * schritt
            anker = self._anker_punkte(n // schritt, n // schritt, schritt)
            treffer = np.where(s >= schwelle)[0]
            if not len(treffer):
                continue
            a, b, k = anker[treffer], b[treffer], k[treffer]
            boxen.append(np.hstack([a - b[:, :2], a + b[:, 2:]]))
            punkte.append((np.tile(a, 5) + k).reshape(-1, 5, 2))
            scores.append(s[treffer])
        if not scores:
            return np.zeros((0, 4)), np.zeros(0), np.zeros((0, 5, 2))
        boxen, scores, punkte = np.vstack(boxen) / faktor, np.concatenate(scores), np.vstack(punkte) / faktor
        behalten = _nms(boxen, scores)
        return boxen[behalten], scores[behalten], punkte[behalten]

    # --- Merkmale
    def _ausrichten(self, rgb: np.ndarray, punkte: np.ndarray) -> np.ndarray:
        m = _aehnlichkeits_transformation(punkte.astype(np.float32), ARCFACE_PUNKTE)
        return self.cv2.warpAffine(rgb, m, (112, 112), borderValue=0.0)

    def _merkmale(self, ausschnitte: list[np.ndarray]) -> np.ndarray:
        x = ((np.stack(ausschnitte).astype(np.float32) - 127.5) / 127.5).transpose(0, 3, 1, 2)
        v = self._ausfuehren("rec", np.ascontiguousarray(x))[0].astype(np.float32)
        return v / (np.linalg.norm(v, axis=1, keepdims=True) + 1e-9)

    @staticmethod
    def _frontal(p: np.ndarray) -> float:
        """1 = frontal, 0 = Profil. Liegt die Nase nicht zwischen den Augen, ist der Kopf stark gedreht."""
        auge_l, auge_r, nase = p[0], p[1], p[2]
        abstand = np.linalg.norm(auge_r - auge_l) + 1e-6
        mitte = (auge_l + auge_r) / 2
        richtung = (auge_r - auge_l) / abstand
        seitlich = abs(float(np.dot(nase - mitte, richtung))) / (abstand / 2)
        return float(np.clip(1 - seitlich, 0, 1))

    def finde(self, img: Image.Image, max_kante: int = 2000) -> list[dict]:
        """Gesichter mit Box (Pixel des übergebenen Bildes), score, qualitaet und 512er-Vektor (normiert)."""
        cv2 = self.cv2
        faktor = min(1.0, max_kante / max(img.size))
        klein = img if faktor >= 1 else img.resize((int(img.width * faktor), int(img.height * faktor)))
        rgb = np.ascontiguousarray(np.asarray(klein.convert("RGB")))
        with self._lock:
            boxen, scores, punkte = self._erkennen(rgb, self.min_score)
            kandidaten = []
            for box, score, p in zip(boxen, scores, punkte):
                x1, y1, x2, y2 = box
                bw, bh = x2 - x1, y2 - y1
                if min(bw, bh) < self.min_groesse * faktor:
                    continue
                ausschnitt = self._ausrichten(rgb, p)
                grau = cv2.cvtColor(ausschnitt, cv2.COLOR_RGB2GRAY)
                schaerfe = float(cv2.Laplacian(grau[20:100, 20:92], cv2.CV_64F).var())
                frontal = self._frontal(p)
                q = gesichts_qualitaet(float(score), min(bw, bh) / faktor, frontal, schaerfe)
                kandidaten.append((box, float(score), q, ausschnitt))
            if not kandidaten:
                return []
            vektoren = self._merkmale([k[3] for k in kandidaten])
        erg = []
        for (box, score, q, _), v in zip(kandidaten, vektoren):
            x1, y1, x2, y2 = box / faktor
            x1, y1 = max(0.0, x1), max(0.0, y1)
            erg.append({"x": int(x1), "y": int(y1), "w": int(x2 - x1), "h": int(y2 - y1),
                        "score": score, "qualitaet": round(q, 3), "vektor": v})
        return erg


# ------------------------------------------------------------------ OCR

class OCR:
    """Texterkennung. Unter Windows wird die eingebaute Windows-Texterkennung
    genutzt (gut für Deutsch), sonst RapidOCR (läuft überall, Umlaute schwächer)."""

    def __init__(self, backend: str = "auto", sprache: str = "de"):
        self.sprache = sprache
        self.backend = None
        self._rapid = None
        self._auto = backend == "auto"
        reihenfolge = {"auto": ["windows", "rapidocr"], "windows": ["windows"],
                       "rapidocr": ["rapidocr"]}.get(backend, ["rapidocr"])
        fehler = []
        for name in reihenfolge:
            try:
                if name == "windows":
                    self._winocr, self.sprache = self._windows_vorbereiten(sprache)
                else:
                    self._rapid = self._rapid_laden()
                self.backend = name
                break
            except Exception as e:
                fehler.append(f"{name}: {e}")
        if not self.backend:
            raise RuntimeError("Keine Texterkennung verfügbar (" + "; ".join(fehler) + ")")

    @staticmethod
    def _rapid_laden():
        from rapidocr_onnxruntime import RapidOCR
        return RapidOCR()

    @staticmethod
    def _windows_vorbereiten(sprache: str):
        """Prüft, ob Windows für die Sprache eine Texterkennung installiert hat ("de" -> auch "de-DE" usw.).

        Ohne passendes Sprachpaket liefert Windows keine OCR-Engine; das soll beim Start auffallen
        (und auf RapidOCR ausweichen), nicht erst bei jedem einzelnen Bild."""
        if sys.platform != "win32":
            raise RuntimeError("nur unter Windows")
        import winocr
        probe = Image.new("RGB", (64, 32), "white")
        kandidaten = [sprache] + [f"{sprache}-{land}" for land in ("DE", "AT", "CH")] + ["en-US"]
        letzter = None
        for tag in dict.fromkeys(kandidaten):
            try:
                winocr.recognize_pil_sync(probe, tag)
                return winocr, tag
            except Exception as e:  # Sprache nicht installiert
                letzter = e
        raise RuntimeError(f"keine Windows-Texterkennung für '{sprache}' installiert ({letzter})")

    def lesen(self, img: Image.Image) -> str:
        if self.backend == "windows":
            try:
                erg = self._winocr.recognize_pil_sync(img.convert("RGB"), self.sprache)
                text = erg.get("text", "") if isinstance(erg, dict) else getattr(erg, "text", "")
            except Exception as e:
                if not self._auto:
                    raise
                # Windows-OCR versagt (z. B. Bild zu groß): für dieses Bild RapidOCR nehmen
                log.warning("Windows-Texterkennung fehlgeschlagen (%s), nutze RapidOCR", e)
                if self._rapid is None:
                    self._rapid = self._rapid_laden()
                text = self._rapid_lesen(img)
        else:
            text = self._rapid_lesen(img)
        return " ".join((text or "").split())

    def _rapid_lesen(self, img: Image.Image) -> str:
        res, _ = self._rapid(np.asarray(img.convert("RGB")))
        return " ".join(r[1] for r in (res or []))


# ------------------------------------------------------------------ Bildbeschreibung

class Beschreiber(_GeraetMitRueckfall):
    """Erzeugt kurze Bildbeschreibungen (BLIP, englisch) und übersetzt sie ins Deutsche (Marian)."""

    _modelle = ("model", "ue_model")

    def __init__(self, modell: str, uebersetzer: str = "", beschleunigung: str = "auto"):
        import torch
        from transformers import BlipForConditionalGeneration, BlipProcessor
        self.torch = torch
        self._rechen_lock = threading.RLock()
        self.geraet, self.geraet_name = waehle_geraet(beschleunigung)
        cache = str(modell_ordner() / "hf")
        self.proc = BlipProcessor.from_pretrained(modell, cache_dir=cache)
        self.model = BlipForConditionalGeneration.from_pretrained(modell, cache_dir=cache).eval()
        self.ue_tok = self.ue_model = None
        if uebersetzer:
            from transformers import MarianMTModel, MarianTokenizer
            self.ue_tok = MarianTokenizer.from_pretrained(uebersetzer, cache_dir=cache)
            self.ue_model = MarianMTModel.from_pretrained(uebersetzer, cache_dir=cache).eval()
        try:
            self.model = self.model.to(self.geraet)
            if self.ue_model is not None:
                self.ue_model = self.ue_model.to(self.geraet)
        except Exception as e:
            self._auf_cpu(e)

    def beschreiben(self, bilder: list[Image.Image]) -> list[tuple[str, str]]:
        """Liste von (deutsch, englisch)."""
        return self._mit_rueckfall(lambda: self._beschreiben(bilder))

    def _beschreiben(self, bilder):
        torch = self.torch
        with torch.no_grad():
            x = self.proc(images=bilder, return_tensors="pt").to(self.geraet)
            out = self.model.generate(**x, max_new_tokens=40, num_beams=3)
            englisch = [t.strip() for t in self.proc.batch_decode(out, skip_special_tokens=True)]
            if not self.ue_model:
                return [(e, e) for e in englisch]
            b = self.ue_tok(englisch, return_tensors="pt", padding=True, truncation=True).to(self.geraet)
            out = self.ue_model.generate(**b, max_new_tokens=80, num_beams=2)
            deutsch = [t.strip() for t in self.ue_tok.batch_decode(out, skip_special_tokens=True)]
        return list(zip(deutsch, englisch))


# ------------------------------------------------------------------ Ausführliche Bildbeschreibung

# Florence-2 (Microsoft, MIT-Lizenz): beschreibt Bilder in mehreren Sätzen. Läuft mit PyTorch auf
# CUDA, DirectML (AMD/Intel) und CPU. In halber Genauigkeit (fp16) braucht "large" etwa 1,6 GB
# Grafikspeicher, "base" etwa 0,5 GB: passt neben dem Bildmodell auch auf Karten mit 4-6 GB.
AUSFUEHRLICH_MODELLE = {
    "microsoft/Florence-2-large": {"name": "Florence-2 large", "download_mb": 1600},
    "microsoft/Florence-2-base": {"name": "Florence-2 base", "download_mb": 480},
}
STUFEN = {"detailliert": "<DETAILED_CAPTION>", "sehr_detailliert": "<MORE_DETAILED_CAPTION>"}
_STUFE_TOKENS = {"detailliert": 160, "sehr_detailliert": 320}
_UNNOETIG = (".md", ".gitattributes", ".png", ".jpg", ".jpeg", ".gif", ".ipynb", ".onnx", ".msgpack", ".h5")


def ausfuehrlich_ordner(modell: str) -> Path:
    """Modelldateien liegen im Datenordner des Programms (daten/modelle/ausfuehrlich/...)."""
    return modell_ordner() / "ausfuehrlich" / modell.replace("/", "--")


def ausfuehrlich_vorhanden(modell: str) -> bool:
    return (ausfuehrlich_ordner(modell) / "vollstaendig.json").exists()


def dateien_waehlen(baum: list[dict]) -> list[tuple[str, int]]:
    """Aus der Dateiliste eines HuggingFace-Modells die nötigen Dateien: Konfiguration, Code,
    Tokenizer und genau eine Gewichtsdatei (safetensors bevorzugt)."""
    dateien = [e for e in baum if e.get("type", "file") == "file"]
    namen = {e["path"] for e in dateien}
    erg = []
    for e in dateien:
        p = e["path"]
        if "/" in p or p.lower().endswith(_UNNOETIG):
            continue
        if p.endswith(".bin") and "model.safetensors" in namen:
            continue
        groesse = (e.get("lfs") or {}).get("size") or e.get("size") or 0
        erg.append((p, int(groesse)))
    return sorted(erg)


class ModellDownload:
    """Lädt ein Modell im Hintergrund, mit Fortschritt (Bytes), Abbrechen und Fortsetzen."""

    def __init__(self, endpunkt: str | None = None):
        self.endpunkt = (endpunkt or os.environ.get("HF_ENDPOINT") or "https://huggingface.co").rstrip("/")
        self.status = {"modell": "", "laeuft": False, "fertig": 0, "gesamt": 0, "datei": "", "fehler": "",
                       "abgeschlossen": False}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.nach_download: list = []  # Callbacks (modell) nach erfolgreichem Laden

    def starten(self, modell: str) -> bool:
        if modell not in AUSFUEHRLICH_MODELLE:
            raise ValueError(f"Unbekanntes Modell: {modell}")
        with self._lock:
            if self._thread and self._thread.is_alive():
                if self.status.get("modell") == modell:
                    return False
                # anderes Modell gewählt: den laufenden Download anhalten (der Teil bleibt und wird
                # später fortgesetzt) und das neu gewählte laden, sonst käme es nie an
                self._stop.set()
                self._thread.join(60)
                if self._thread.is_alive():
                    return False
            if ausfuehrlich_vorhanden(modell):
                self.status.update(modell=modell, laeuft=False, abgeschlossen=True, fehler="")
                return False
            self._stop.clear()
            self.status.update(modell=modell, laeuft=True, fertig=0, gesamt=0, datei="", fehler="",
                               abgeschlossen=False)
            self._thread = threading.Thread(target=self._lauf, args=(modell,), daemon=True)
            self._thread.start()
            return True

    def abbrechen(self) -> None:
        self._stop.set()

    def warten(self, timeout: float | None = None) -> None:
        if self._thread:
            self._thread.join(timeout)

    def _json(self, url: str):
        import json
        with urllib.request.urlopen(url, timeout=60) as r:
            return json.loads(r.read().decode("utf-8"))

    def _lauf(self, modell: str):
        import json
        try:
            ziel = ausfuehrlich_ordner(modell)
            ziel.mkdir(parents=True, exist_ok=True)
            # auf einen festen Stand des Modells festlegen, damit alle Dateien zusammenpassen
            sha = self._json(f"{self.endpunkt}/api/models/{modell}").get("sha") or "main"
            dateien = dateien_waehlen(self._json(f"{self.endpunkt}/api/models/{modell}/tree/{sha}"))
            if not any(p.endswith((".safetensors", ".bin")) for p, _ in dateien):
                raise RuntimeError("keine Gewichtsdatei im Modell gefunden")
            self.status["gesamt"] = sum(g for _, g in dateien)
            schon = 0
            for pfad, groesse in dateien:
                if self._stop.is_set():
                    raise InterruptedError
                self.status["datei"] = pfad
                datei = ziel / pfad
                if datei.exists() and (not groesse or datei.stat().st_size == groesse):
                    schon += datei.stat().st_size
                    self.status["fertig"] = schon
                    continue
                self._datei_laden(f"{self.endpunkt}/{modell}/resolve/{sha}/{pfad}", datei, groesse, schon)
                schon += datei.stat().st_size
                self.status["fertig"] = schon
            (ziel / "vollstaendig.json").write_text(json.dumps({"modell": modell, "stand": sha,
                                                                "dateien": [p for p, _ in dateien]}), "utf-8")
            self.status.update(abgeschlossen=True, datei="")
            for cb in self.nach_download:
                try:
                    cb(modell)
                except Exception:
                    log.exception("Nach dem Download")
        except InterruptedError:
            self.status["fehler"] = "abgebrochen"
        except Exception as e:
            log.exception("Download %s fehlgeschlagen", modell)
            self.status["fehler"] = f"{e}"[:300]
        finally:
            self.status["laeuft"] = False

    def _datei_laden(self, url: str, datei: Path, groesse: int, davor: int):
        """Lädt in eine .part-Datei und setzt einen abgebrochenen Download fort."""
        teil = datei.with_name(datei.name + ".part")

        def melden(geladen):
            self.status["fertig"] = davor + geladen
        _teil_laden(url, teil, groesse, fortschritt=melden, stopp=self._stop)
        teil.replace(datei)


def saetze_trennen(text: str) -> list[str]:
    import re
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", " ".join((text or "").split())) if s.strip()]


class AusfuehrlicherBeschreiber(_GeraetMitRueckfall):
    """Ausführliche Beschreibung (Florence-2, englisch), satzweise ins Deutsche übersetzt (Marian).

    Auf der Grafikkarte zuerst in halber Genauigkeit (spart die Hälfte des Speichers). Klappt das
    nicht (Treiber, DirectML, Speicher), wird es in voller Genauigkeit versucht, zuletzt auf der CPU."""

    _modelle = ("model", "ue_model")

    def __init__(self, modell: str, stufe: str = "sehr_detailliert", uebersetzer: str = "",
                 beschleunigung: str = "auto", genauigkeit: str = "auto", strahlen: int = 3):
        import torch
        from transformers import AutoModelForCausalLM, AutoProcessor
        if not ausfuehrlich_vorhanden(modell):
            raise RuntimeError("Modell ist noch nicht heruntergeladen")
        self.torch = torch
        self._rechen_lock = threading.RLock()
        self.aufgabe = STUFEN.get(stufe, STUFEN["sehr_detailliert"])
        self.max_tokens = _STUFE_TOKENS.get(stufe, 320)
        self.strahlen = max(1, int(strahlen))
        self.geraet, self.geraet_name = waehle_geraet(beschleunigung)
        halb = genauigkeit == "fp16" or (genauigkeit == "auto" and str(self.geraet) != "cpu")
        self.dtype = torch.float16 if halb else torch.float32
        ordner = str(ausfuehrlich_ordner(modell))
        with _ohne_flash_attn():
            self.model = AutoModelForCausalLM.from_pretrained(
                ordner, trust_remote_code=True, torch_dtype=self.dtype, attn_implementation="eager",
                local_files_only=True).eval()
            self.proc = AutoProcessor.from_pretrained(ordner, trust_remote_code=True, local_files_only=True)
        self.ue_tok = self.ue_model = None
        if uebersetzer:
            from transformers import MarianMTModel, MarianTokenizer
            cache = str(modell_ordner() / "hf")
            self.ue_tok = MarianTokenizer.from_pretrained(uebersetzer, cache_dir=cache)
            self.ue_model = MarianMTModel.from_pretrained(uebersetzer, cache_dir=cache).eval()
        self._geraet_name_basis = self.geraet_name
        self._name_setzen()
        try:
            self.model = self.model.to(self.geraet)
            if self.ue_model is not None:
                self.ue_model = self.ue_model.to(self.geraet)
        except Exception as e:
            self._ausweichen(e)

    def _name_setzen(self):
        if str(self.geraet) != "cpu":
            halb = self.dtype == self.torch.float16
            self.geraet_name = f"{self._geraet_name_basis}, {'halbe' if halb else 'volle'} Genauigkeit"

    def _ausweichen(self, fehler: Exception):
        torch = self.torch
        if str(self.geraet) != "cpu" and self.dtype == torch.float16:
            log.warning("Ausführliche Beschreibung in halber Genauigkeit fehlgeschlagen, versuche volle: %s", fehler)
            self.dtype = torch.float32
            self.model = self.model.float()
            try:
                self.model = self.model.to(self.geraet)
                self._name_setzen()
                return
            except Exception as e:  # z. B. zu wenig Grafikspeicher
                fehler = e
        self.model = self.model.float()
        self.dtype = torch.float32
        self._auf_cpu(fehler)

    def _mit_rueckfall(self, funktion):
        with self._rechen_lock:
            for _ in range(3):  # fp16 -> fp32 -> CPU
                try:
                    return funktion()
                except (RuntimeError, NotImplementedError) as e:
                    if str(self.geraet) == "cpu":
                        raise
                    self._ausweichen(e)
            return funktion()

    def beschreiben(self, bilder: list[Image.Image]) -> list[tuple[str, str, list[str]]]:
        """Liste von (deutsch, englisch, deutsche Sätze)."""
        return self._mit_rueckfall(lambda: self._beschreiben(bilder))

    def _beschreiben(self, bilder):
        torch = self.torch
        with torch.no_grad():
            x = self.proc(text=[self.aufgabe] * len(bilder), images=bilder, return_tensors="pt")
            out = self.model.generate(input_ids=x["input_ids"].to(self.geraet),
                                      pixel_values=x["pixel_values"].to(self.geraet, self.dtype),
                                      max_new_tokens=self.max_tokens, num_beams=self.strahlen, do_sample=False)
            roh = self.proc.batch_decode(out, skip_special_tokens=False)
            englisch = []
            for t, img in zip(roh, bilder):
                erg = self.proc.post_process_generation(t, task=self.aufgabe, image_size=img.size)
                englisch.append(" ".join(str(erg.get(self.aufgabe, "")).split()))
            if str(self.geraet) != "cpu" and self.dtype == torch.float16 and not any(englisch):
                # halbe Genauigkeit liefert auf manchen Treibern nur Leeres (Überlauf)
                raise RuntimeError("leere Ausgabe in halber Genauigkeit")
            saetze = [saetze_trennen(e) for e in englisch]
            if not self.ue_model:
                return [(e, e, s) for e, s in zip(englisch, saetze)]
            alle = [s for liste in saetze for s in liste]
            deutsch_alle = []
            for i in range(0, len(alle), 16):
                b = self.ue_tok(alle[i:i + 16], return_tensors="pt", padding=True, truncation=True).to(self.geraet)
                o = self.ue_model.generate(**b, max_new_tokens=160, num_beams=2)
                deutsch_alle += [t.strip() for t in self.ue_tok.batch_decode(o, skip_special_tokens=True)]
        erg, pos = [], 0
        for e, liste in zip(englisch, saetze):
            de = deutsch_alle[pos:pos + len(liste)]
            pos += len(liste)
            erg.append((" ".join(de), e, de))
        return erg


class _ohne_flash_attn:
    """Florence-2 nennt im Code das optionale Paket flash_attn; transformers verlangt es sonst
    beim Laden, obwohl es ohne NVIDIA gar nicht gebraucht wird."""

    def __enter__(self):
        from transformers import dynamic_module_utils as dmu
        self._dmu, self._alt = dmu, dmu.get_imports

        def ohne(datei):
            return [i for i in self._alt(datei) if i != "flash_attn"]
        dmu.get_imports = ohne
        return self

    def __exit__(self, *a):
        self._dmu.get_imports = self._alt
