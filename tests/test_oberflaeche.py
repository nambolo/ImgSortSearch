"""Schutz des lokalen Servers und Hilfen für die Windows-Integration."""
import struct

from fastapi.testclient import TestClient

from imgsortsearch import zwischenablage
from imgsortsearch.server import erstelle_app


def test_aendernde_anfrage_ohne_kennung_abgelehnt(zustand):
    c = TestClient(erstelle_app(zustand))
    assert c.post("/api/index/stop").status_code == 403
    assert c.post("/api/index/stop", headers={"X-ImgSortSearch": "1"}).status_code == 200
    assert c.get("/api/status").status_code == 200  # Lesen bleibt ohne Kennung möglich (Bilder, Vorschau)


def test_fremder_host_abgelehnt(zustand):
    c = TestClient(erstelle_app(zustand), base_url="http://boese.example")
    assert c.get("/api/status").status_code == 403
    c2 = TestClient(erstelle_app(zustand), base_url="http://127.0.0.1:8765")
    assert c2.get("/api/status").status_code == 200


def test_dropfiles_aufbau():
    daten = zwischenablage._dropfiles_daten([r"C:\Bilder\a b.jpg", r"D:\ü.png"])
    p_files, x, y, nc, wide = struct.unpack("<IiiII", daten[:20])
    assert (p_files, wide) == (20, 1)
    liste = daten[20:].decode("utf-16-le")
    assert liste == "C:\\Bilder\\a b.jpg\0D:\\ü.png\0\0"
