"""Startet Server und Fenster. Ohne pywebview öffnet sich der Browser."""
from __future__ import annotations

import argparse
import logging
import socket
import sys
import threading
import time
import webbrowser

from .einstellungen import daten_ordner


def _freier_port(wunsch: int) -> int:
    for port in [wunsch] + list(range(wunsch + 1, wunsch + 20)):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError("Kein freier Port gefunden")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="imgsortsearch")
    ap.add_argument("--browser", action="store_true", help="im Browser statt im eigenen Fenster öffnen")
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--ohne-fenster", action="store_true", help="nur den Server starten")
    args = ap.parse_args(argv)

    logdatei = daten_ordner() / "imgsortsearch.log"
    handler: list[logging.Handler] = [logging.FileHandler(logdatei, encoding="utf-8")]
    if sys.stdout is None or sys.stderr is None:  # gestartet mit pythonw.exe (ohne Konsole)
        # print() und Tracebacks landen ebenfalls im Log; kein zweiter StreamHandler, sonst steht alles doppelt drin
        sys.stdout = sys.stderr = open(logdatei, "a", encoding="utf-8", buffering=1)
    else:
        handler.append(logging.StreamHandler())
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=handler)
    import uvicorn
    from .server import Zustand, erstelle_app

    zustand = Zustand()
    port = _freier_port(args.port or int(zustand.e.get("port", 8765)))
    app = erstelle_app(zustand)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", log_config=None))
    url = f"http://127.0.0.1:{port}/"

    if args.ohne_fenster:
        print(f"ImgSortSearch läuft auf {url}")
        server.run()
        return 0

    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(300):
        if server.started or not t.is_alive():
            break
        time.sleep(0.1)
    if not server.started:
        logging.getLogger(__name__).error("Server konnte nicht starten (Port %s)", port)
        return 1

    webview = None
    if not args.browser:
        try:
            import webview  # pywebview: eigenes Fenster (unter Windows mit Edge WebView2)
        except ImportError:
            webview = None
    if webview is not None:
        try:
            fenster = webview.create_window("ImgSortSearch", url, width=1440, height=900, min_size=(960, 640),
                                            background_color="#0f1115")
            zustand.fenster = fenster
            # Browserdaten (localStorage, Cache) im Datenordner statt im Benutzerprofil, und dauerhaft
            webview.start(private_mode=False, storage_path=str(daten_ordner() / "fenster"))
            return _beenden(zustand, server, t)
        except Exception:
            # z. B. fehlende WebView2-Laufzeit: dann eben im Browser
            logging.getLogger(__name__).exception("Eigenes Fenster nicht möglich, öffne den Browser")
            zustand.fenster = None

    webbrowser.open(url)
    print(f"ImgSortSearch läuft auf {url}  (Beenden mit Strg+C)")
    try:
        while t.is_alive():
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    return _beenden(zustand, server, t)


def _beenden(zustand, server, t) -> int:
    zustand.indexer.stoppen()
    server.should_exit = True
    t.join(timeout=5)
    return 0
