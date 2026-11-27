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


def lade_datei(urls: list[str], ziel: Path) -> Path:
    if ziel.exists() and ziel.stat().st_size > 0:
        return ziel
    letzter = None
    for url in urls:
        try:
            log.info("Lade %s", url)
            tmp = ziel.with_suffix(ziel.suffix + ".part")
            with urllib.request.urlopen(url, timeout=120) as r, open(tmp, "wb") as f:
                shutil.copyfileobj(r, f)
            tmp.replace(ziel)
            return ziel
        except Exception as e:  # nächste Quelle probieren
            letzter = e
    raise RuntimeError(f"Modell konnte nicht geladen werden ({ziel.name}): {letzter}")


# ------------------------------------------------------------------ Bilder

RAW_ENDUNGEN = {".dng", ".cr2", ".cr3", ".nef", ".arw", ".orf", ".rw2", ".raf"}


def bild_laden(pfad: Path, max_kante: int | None = None) -> Image.Image:
    """Öffnet ein Bild (auch HEIC, RAW über die eingebettete Vorschau), richtig gedreht, RGB."""
    pfad = Path(pfad)
    img = None
    if pfad.suffix.lower() not in RAW_ENDUNGEN:
        try:
            img = Image.open(pfad)
            if max_kante:
                img.draft("RGB", (max_kante, max_kante))  # schnelles JPEG-Verkleinern
            img.load()
        except Exception:
            img = None
    if img is None:
        exiftool = shutil.which("exiftool")
        if not exiftool:
            raise ValueError(f"Bildformat wird nicht unterstützt: {pfad.name}")
        for tag in ("-JpgFromRaw", "-PreviewImage", "-ThumbnailImage"):
            daten = subprocess.run([exiftool, "-b", tag, str(pfad)], capture_output=True,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
            if daten:
                img = Image.open(io.BytesIO(daten))
                img.load()
                break
        else:
            raise ValueError(f"Keine Vorschau in {pfad.name}")
    img = ImageOps.exif_transpose(img)
    if img.mode != "RGB":
        img = img.convert("RGB")
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
                return torch_directml.device(), f"DirectML ({torch_directml.device_name(torch_directml.default_device())})"
        except Exception as e:  # Paket fehlt oder Treiber zu alt
            if wunsch == "directml":
                log.warning("DirectML nicht verfügbar: %s", e)
    return "cpu", "CPU (Prozessor)"


class _GeraetMitRueckfall:
    """Führt Berechnungen auf der Grafikkarte aus und weicht bei Fehlern einmalig auf die CPU aus.

    Manche Grafiktreiber oder DirectML können einzelne Operationen nicht. Dann
    läuft der Rest des Laufs eben auf dem Prozessor statt abzubrechen."""

    geraet = "cpu"
    geraet_name = "CPU (Prozessor)"
    _modelle: tuple = ()

    def _auf_cpu(self, fehler: Exception):
        log.warning("Grafikkarte (%s) fehlgeschlagen, weiter auf der CPU: %s", self.geraet_name, fehler)
        for name in self._modelle:
            m = getattr(self, name, None)
            if m is not None:
                setattr(self, name, m.to("cpu"))
        self.geraet = "cpu"
        self.geraet_name = f"CPU (Grafikkarte fehlgeschlagen: {str(fehler)[:120]})"

    def _mit_rueckfall(self, funktion):
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

YUNET = ["https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx",
         "https://huggingface.co/opencv/face_detection_yunet/resolve/main/face_detection_yunet_2023mar.onnx"]
SFACE = ["https://github.com/opencv/opencv_zoo/raw/main/models/face_recognition_sface/face_recognition_sface_2021dec.onnx",
         "https://huggingface.co/opencv/face_recognition_sface/resolve/main/face_recognition_sface_2021dec.onnx"]


class GesichtsModell:
    """Gesichtserkennung mit OpenCV (YuNet findet Gesichter, SFace erzeugt Merkmale)."""

    def __init__(self, min_groesse: int = 40):
        import cv2
        self.cv2 = cv2
        det = lade_datei(YUNET, modell_ordner() / "face_detection_yunet_2023mar.onnx")
        rec = lade_datei(SFACE, modell_ordner() / "face_recognition_sface_2021dec.onnx")
        self.detektor = cv2.FaceDetectorYN.create(str(det), "", (320, 320), 0.85, 0.3, 5000)
        self.erkenner = cv2.FaceRecognizerSF.create(str(rec), "")
        self.min_groesse = min_groesse
        self._lock = threading.Lock()

    def finde(self, img: Image.Image, max_kante: int = 1600) -> list[dict]:
        """Liefert Gesichter mit Box (in Original-Pixeln des übergebenen Bildes) und 128er-Vektor."""
        cv2 = self.cv2
        faktor = min(1.0, max_kante / max(img.size))
        klein = img if faktor >= 1 else img.resize((int(img.width * faktor), int(img.height * faktor)))
        bgr = cv2.cvtColor(np.asarray(klein), cv2.COLOR_RGB2BGR)
        with self._lock:
            self.detektor.setInputSize((bgr.shape[1], bgr.shape[0]))
            _, faces = self.detektor.detect(bgr)
            erg = []
            if faces is None:
                return erg
            for f in faces:
                x, y, w, h = f[:4]
                if min(w, h) < self.min_groesse * faktor:
                    continue
                ausgerichtet = self.erkenner.alignCrop(bgr, f)
                v = self.erkenner.feature(ausgerichtet).flatten().astype(np.float32)
                v /= (np.linalg.norm(v) + 1e-9)
                erg.append({"x": int(x / faktor), "y": int(y / faktor), "w": int(w / faktor),
                            "h": int(h / faktor), "score": float(f[14]), "vektor": v})
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
