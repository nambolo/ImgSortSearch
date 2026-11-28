"""Orte und Karte.

* Ortsnamen ohne Internet: Zu den GPS-Koordinaten eines Fotos wird der nächste Ort aus einer mitgelieferten
  Liste gesucht (GeoNames, alle Orte ab 1.000 Einwohnern). So lässt sich nach „München“ oder „Italien“ suchen.
* Kartenkacheln: Der Server holt sie nur, wenn die Karte in den Einstellungen eingeschaltet ist, von
  OpenStreetMap (mit eigener Kennung, höchstens zwei Verbindungen gleichzeitig, wie es die Nutzungsregeln
  verlangen) und legt sie im Datenordner ab. Einmal geladene Kacheln funktionieren danach auch offline.
"""
from __future__ import annotations

import gzip
import json
import logging
import math
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import numpy as np

from . import __version__
from .datenbank import Datenbank
from .einstellungen import daten_ordner

log = logging.getLogger(__name__)
ORTE_ORDNER = Path(__file__).with_name("orte")

MAX_ABSTAND_ORT_KM = 30       # weiter weg (Meer, Wüste): kein Ortsname
MAX_ABSTAND_LAND_KM = 150     # aber das Land noch, z. B. auf einer Fähre
STADT_EINWOHNER = 50_000      # „bei Hamburg“: nächste größere Stadt im Umkreis
MAX_ABSTAND_STADT_KM = 25

KACHEL_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
KACHEL_ALTER_TAGE = 30        # danach bei Verbindung neu laden, sonst die alte weiter zeigen
BENUTZER_KENNUNG = f"ImgSortSearch/{__version__} (+https://github.com/nambolo/imgsortsearch)"


# ------------------------------------------------------------------ Ortsnamen

class Ortsliste:
    """Die mitgelieferte Ortsliste, nach Breitengrad sortiert für eine schnelle Umkreissuche."""

    def __init__(self, ordner: Path = ORTE_ORDNER):
        lat, lon, ew, namen, cc, region, alt, teil = [], [], [], [], [], [], [], []
        with gzip.open(ordner / "orte.tsv.gz", "rt", encoding="utf-8") as f:
            for zeile in f:
                t = zeile.rstrip("\n").split("\t")
                if len(t) < 7:
                    continue
                lat.append(float(t[0]))
                lon.append(float(t[1]))
                namen.append(t[2])
                cc.append(t[3])
                region.append(t[4])
                ew.append(int(t[5] or 0))
                alt.append(t[6])
                teil.append(len(t) > 7 and t[7] == "1")  # Stadtteil (Bogenhausen, Kreuzberg)
        tabellen = json.loads((ordner / "regionen.json").read_text("utf-8"))
        self.laender: dict[str, str] = tabellen["laender"]
        self.regionen: dict[str, str] = tabellen["regionen"]
        reihe = np.argsort(np.array(lat))
        self.lat = np.array(lat, dtype=np.float64)[reihe]
        self.lon = np.array(lon, dtype=np.float64)[reihe]
        self.ew = np.array(ew, dtype=np.int64)[reihe]
        # als „Stadt“ für „bei …“ und den Ortsfilter zählen nur größere Orte, keine Stadtteile
        self.stadt = (self.ew >= STADT_EINWOHNER) & ~np.array(teil, dtype=bool)[reihe]
        # nicht als Stadtteil markiert, aber einer: viel kleiner und nah an einer Großstadt (Billstedt, Hamburg)
        for i in np.flatnonzero(self.stadt):
            n = self._naechster(self.lat[i], self.lon[i], 15, nur_staedte=True, groesser_als=self.ew[i] * 10)
            if n is not None:
                self.stadt[i] = False
        self.namen = [namen[i] for i in reihe]
        self.cc = [cc[i] for i in reihe]
        self.region = [region[i] for i in reihe]
        self.alt = [alt[i] for i in reihe]

    def _naechster(self, lat: float, lon: float, max_km: float, nur_staedte: bool = False,
                   groesser_als: int = 0) -> tuple[int, float] | None:
        grad = max_km / 111.0
        a, b = np.searchsorted(self.lat, [lat - grad, lat + grad])
        if a >= b:
            return None
        la, lo = self.lat[a:b], self.lon[a:b]
        dlon = (lo - lon + 180) % 360 - 180
        km = 111.2 * np.sqrt((la - lat) ** 2 + (dlon * math.cos(math.radians(lat))) ** 2)
        if nur_staedte:
            km = np.where(self.stadt[a:b], km, np.inf)
        if groesser_als:
            km = np.where(self.ew[a:b] > groesser_als, km, np.inf)
        if nur_staedte:
            # die größere Stadt gewinnt, wenn sie nicht viel weiter weg ist (Heumaden: Stuttgart, nicht Esslingen)
            rang = km - 5 * np.log10(np.maximum(self.ew[a:b], 1) / STADT_EINWOHNER)
            i = int(np.argmin(np.where(np.isfinite(km), rang, np.inf)))
        else:
            i = int(np.argmin(km))
        if not np.isfinite(km[i]) or km[i] > max_km:
            return None
        return a + i, float(km[i])

    def ort(self, lat: float, lon: float) -> dict:
        """{"name", "stadt", "region", "land", "text", "such"}; leere Texte, wenn nichts in der Nähe ist."""
        leer = {"name": "", "stadt": "", "region": "", "land": "", "text": "", "such": ""}
        if lat is None or lon is None or not (-90 <= lat <= 90 and -180 <= lon <= 180):
            return leer
        n = self._naechster(lat, lon, MAX_ABSTAND_LAND_KM)
        if n is None:
            return leer
        i, km = n
        land = self.laender.get(self.cc[i], self.cc[i])
        if km > MAX_ABSTAND_ORT_KM:
            return dict(leer, land=land, text=land, such=land)
        name = self.namen[i]
        region = self.regionen.get(f"{self.cc[i]}.{self.region[i]}", "")
        stadt = name if self.stadt[i] else ""
        such = [name, self.alt[i]]
        if not stadt:
            s = self._naechster(lat, lon, MAX_ABSTAND_STADT_KM, nur_staedte=True)
            if s is not None:
                stadt = self.namen[s[0]]
                such += [stadt, self.alt[s[0]]]
        teile = [name + (f" bei {stadt}" if stadt and stadt != name and " bei " not in name else "")]
        teile += [t for t in (region, land) if t and t != name]
        return {"name": name, "stadt": stadt or name, "region": region, "land": land,
                "text": ", ".join(teile), "such": " ".join(t for t in such + [region, land] if t)}


_ortsliste: Ortsliste | None = None
_orte_lock = threading.Lock()


def ortsliste() -> Ortsliste:
    global _ortsliste
    with _orte_lock:
        if _ortsliste is None:
            _ortsliste = Ortsliste()
        return _ortsliste


def _gps_schluessel(lat, lon) -> str:
    return f"{lat:.4f},{lon:.4f}"


def orte_zuordnen(db: Datenbank, stop: threading.Event | None = None, fortschritt=None) -> int:
    """Ortsnamen für alle Bilder mit (neuen oder geänderten) GPS-Daten eintragen. Liefert die Anzahl."""
    offen = db.abfrage("SELECT id, gps_lat, gps_lon, ort_fuer FROM bilder WHERE "
                       "(gps_lat IS NOT NULL AND gps_lon IS NOT NULL) OR ort_fuer IS NOT NULL")
    offen = [r for r in offen if (None if r["gps_lat"] is None or r["gps_lon"] is None
                                  else _gps_schluessel(r["gps_lat"], r["gps_lon"])) != r["ort_fuer"]]
    if not offen:
        return 0
    liste = ortsliste()
    cache: dict[str, dict] = {}
    geaendert = []
    for start in range(0, len(offen), 500):
        if stop is not None and stop.is_set():
            break
        updates = []
        for r in offen[start:start + 500]:
            if r["gps_lat"] is None or r["gps_lon"] is None:  # GPS weggefallen
                updates.append(("", "", "", "", "", None, r["id"]))
                continue
            k = _gps_schluessel(r["gps_lat"], r["gps_lon"])
            if k not in cache:
                cache[k] = liste.ort(r["gps_lat"], r["gps_lon"])
            o = cache[k]
            updates.append((o["text"], o["name"], o["stadt"], o["land"], o["such"], k, r["id"]))
        with db.verbindung() as v:
            v.executemany("UPDATE bilder SET ort=?, ort_name=?, ort_stadt=?, ort_land=?, ort_such=?, ort_fuer=? "
                          "WHERE id=?", updates)
        geaendert += [u[-1] for u in updates]
        if fortschritt is not None:
            fortschritt(len(geaendert), len(offen))
    db.suchtext_aktualisieren_viele(geaendert)
    ortsliste_freigeben()  # rund 40 MB, wird erst beim nächsten Indexlauf wieder gebraucht
    return len(geaendert)


def ortsliste_freigeben() -> None:
    global _ortsliste
    with _orte_lock:
        _ortsliste = None


def ort_bedingung(wert: str) -> tuple[str, list]:
    """Filter „Ort“: ``land:Italien`` oder ``ort:München`` (der Ort selbst oder Orte bei München)."""
    art, _, name = (wert or "").partition(":")
    if not name:
        return "1=1", []
    if art == "land":
        return "ort_land=?", [name]
    return "(ort_name=? OR ort_stadt=?)", [name, name]


def bereich_bedingung(wert: str) -> tuple[str, list]:
    """Filter „Kartenbereich“: ``süd,west,nord,ost`` in Grad. Über die Datumsgrenze hinweg ist west > ost."""
    try:
        s, w, n, o = (float(x) for x in wert.split(","))
    except (ValueError, AttributeError):
        return "1=1", []
    if s > n:
        s, n = n, s
    if o - w >= 360:
        return "(gps_lat BETWEEN ? AND ?)", [s, n]
    w = (w + 180) % 360 - 180
    o = (o + 180) % 360 - 180
    if w <= o:
        return "(gps_lat BETWEEN ? AND ? AND gps_lon BETWEEN ? AND ?)", [s, n, w, o]
    return "(gps_lat BETWEEN ? AND ? AND (gps_lon >= ? OR gps_lon <= ?))", [s, n, w, o]


# ------------------------------------------------------------------ Kartenkacheln

class Kacheln:
    """Kartenkacheln aus dem Zwischenspeicher im Datenordner, fehlende auf Wunsch aus dem Internet."""

    def __init__(self, ordner: Path | None = None, holen=None):
        self.ordner = ordner or daten_ordner() / "kacheln"
        self._holen = holen or self._aus_dem_netz
        self._gleichzeitig = threading.BoundedSemaphore(2)  # OSM: höchstens 2 Verbindungen
        self.offline_bis = 0.0       # nach einem Netzfehler eine Weile nicht erneut versuchen
        self.letzter_fehler = ""

    def pfad(self, z: int, x: int, y: int) -> Path:
        return self.ordner / str(z) / str(x) / f"{y}.png"

    @staticmethod
    def gueltig(z: int, x: int, y: int) -> bool:
        return 0 <= z <= 19 and 0 <= x < 2 ** z and 0 <= y < 2 ** z

    def _aus_dem_netz(self, url: str) -> bytes:
        anfrage = urllib.request.Request(url, headers={"User-Agent": BENUTZER_KENNUNG})
        with urllib.request.urlopen(anfrage, timeout=10) as antwort:
            daten = antwort.read()
        if not daten.startswith(b"\x89PNG") and not daten.startswith(b"\xff\xd8"):
            raise ValueError("Antwort ist kein Bild")
        return daten

    def kachel(self, z: int, x: int, y: int, online: bool, url: str = KACHEL_URL) -> bytes | None:
        if not self.gueltig(z, x, y):
            return None
        p = self.pfad(z, x, y)
        alt = None
        if p.exists():
            alt = p.read_bytes()
            if not online or time.time() - p.stat().st_mtime < KACHEL_ALTER_TAGE * 86400:
                return alt
        if not online or time.time() < self.offline_bis:
            return alt
        with self._gleichzeitig:
            if p.exists() and time.time() - p.stat().st_mtime < KACHEL_ALTER_TAGE * 86400:
                return p.read_bytes()  # inzwischen von einer parallelen Anfrage geholt
            try:
                daten = self._holen(url.format(z=z, x=x, y=y))
            except urllib.error.HTTPError as ex:
                self.letzter_fehler = f"Kartenserver: {ex.code}"
                if ex.code in (403, 429):  # gesperrt oder zu viele Anfragen: länger Pause
                    self.offline_bis = time.time() + 600
                return alt
            except Exception as ex:  # keine Verbindung
                self.letzter_fehler = str(getattr(ex, "reason", ex))
                self.offline_bis = time.time() + 60
                return alt
        self.offline_bis = 0.0
        self.letzter_fehler = ""
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_bytes(daten)
        tmp.replace(p)
        return daten

    def groesse(self) -> tuple[int, int]:
        """(Anzahl, Bytes) der gespeicherten Kacheln."""
        if not self.ordner.exists():
            return 0, 0
        n = b = 0
        for p in self.ordner.rglob("*.png"):
            n += 1
            try:
                b += p.stat().st_size
            except OSError:
                pass
        return n, b

    def leeren(self) -> None:
        import shutil
        if self.ordner.exists():
            shutil.rmtree(self.ordner, ignore_errors=True)
