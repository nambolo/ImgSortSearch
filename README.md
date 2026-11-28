# ImgSortSearch

Lokales Programm für Windows, das ein großes Foto-Backup durchsuchbar macht und sortiert.
**Alles läuft auf dem eigenen Rechner.** Es werden keine Bilder ins Internet geschickt.

- **Suchindex:** Metadaten (Datum, Kamera, GPS), Bildinhalt, Bildbeschreibung, erkannter Text (OCR) und Personen
- **Suche:** nach Text („Rechnung 4711“), nach Bedeutung („Hund am Strand“, „Whiteboard“), nach ähnlichen Bildern, nach einem Foto einer Person
- **Ergebnisse:** als Raster mit Vorschaubildern oder als Liste mit großer Ansicht; markierte Bilder mit **Strg+C** kopieren und im Explorer mit **Strg+V** einfügen
- **Kategorien selbst festlegen** (z. B. Arbeit, Privat, Natur, Tiere): automatisch über Themen („Whiteboard“, „Technische Zeichnung“ → Arbeit), Aufnahmezeit und eigene Regeln, oder per Hand für ganze Suchergebnisse
- **Sortieren:** kopieren, verschieben oder an Ort und Stelle taggen, **Umbenennen optional**, Tags als XMP-Datei oder in die Datei; jeder Lauf ist **rückgängig** zu machen, Originale werden nie gelöscht
- **Dubletten:** findet exakt gleiche Dateien und ähnliche Fassungen (verkleinert, neu komprimiert, WhatsApp-Kopien), zeigt sie nebeneinander mit Auflösung, Größe und Aufnahmedaten und schlägt vor, welche Datei bleibt; Aussortiertes kommt in einen Quarantäne-Ordner oder den Papierkorb, rückgängig machbar
- **Karte:** Fotos mit Standort (GPS) auf einer Karte, nach Ort gruppiert; Klick auf eine Gruppe zeigt alle Fotos von dort, ein Rechteck aufziehen zeigt alle Fotos in diesem Bereich. Beim einzelnen Bild eine kleine Karte. Suche nach Orten („München“, „Italien“), auch ohne Internet
- **Index fortsetzen:** Start per Knopf, jederzeit anhalten; beim nächsten Mal werden nur neue oder geänderte Bilder analysiert

## Installation (portabel)

1. Diesen Ordner an einen Ort mit genug Platz legen (ca. 6–8 GB inklusive Modelle), z. B. `D:\ImgSortSearch`.
2. **`install.bat`** doppelklicken.
   Das Skript installiert Python, alle Pakete, exiftool und die KI-Modelle **ausschließlich in diesen Ordner**
   (Unterordner `umgebung` und `daten`). Es wird nichts systemweit installiert und nichts in die Registry geschrieben.
   Die Grafikkarte wird erkannt und genutzt (siehe unten).
3. **`start.bat`** doppelklicken. Das Programm öffnet sich in einem eigenen Fenster.

Aktualisieren: **`update.bat`** doppelklicken. Holt den neuesten Stand von GitHub (wie `git pull`; ohne installiertes git
wird ein portables git nach `umgebung\git` geladen) und zieht neue Pakete nach. `umgebung` und `daten`
(Index, Einstellungen, Modelle) bleiben erhalten. Funktioniert auch mit einem Ordner aus dem ZIP-Download.

Hat exiftool bei der Installation nicht geklappt: **`exiftool-nachladen.bat`** doppelklicken. Das lädt nur exiftool, alles andere bleibt.

Deinstallieren: den Ordner löschen.

## Grafikkarte

| Grafik | Beschleunigung |
|---|---|
| NVIDIA | CUDA |
| kleine NVIDIA-Karte (unter 3,5 GB, z. B. GeForce 940MX) plus Prozessorgrafik | DirectML für beide |
| AMD Radeon (z. B. RX 5600 XT) | DirectML |
| Intel-Grafik im Laptop (Iris Xe, UHD, Arc) | DirectML, nutzt den gemeinsamen Arbeitsspeicher |
| keine passende | Prozessor |

`install.bat` erkennt die Grafik selbst. Erzwingen lässt sich das mit `install.bat -Beschleunigung directml`
(oder `nvidia`, `cpu`). In den Einstellungen kann man jederzeit auf „Nur Prozessor“ umstellen. Kann die
Grafikkarte einen Rechenschritt nicht, wechselt das Programm von selbst auf den Prozessor und zeigt das an.
Bildmodell, Beschreibungen und Übersetzung laufen auf der Grafikkarte; Gesichter und Texterkennung auf dem
Prozessor (die sind auch so schnell). Sind Prozessorgrafik und Grafikkarte beide aktiv, nimmt DirectML die Grafikkarte.

Bei „Automatisch“ zählt auch der Grafikspeicher: Passt ein Modell nicht in den eigenen Speicher der Karte, rechnet es
auf der Prozessorgrafik (die den Arbeitsspeicher nutzt) oder, ohne Prozessorgrafik, auf dem Prozessor. Beim Bildmodell
kommt dann nur der Bildteil auf die Karte, der Textteil für die Suchbegriffe bleibt beim Prozessor. Beispiel Laptop mit
GeForce 940MX (2 GB) und Intel HD 620: Bildteil und Gesichter auf der 940MX, Beschreibungen auf der Intel-Grafik.
Ältere NVIDIA-Karten, auf denen die aktuelle PyTorch-Version nicht rechnet, bekommen bei der Installation
PyTorch 2.5.1; klappt CUDA gar nicht (meist alter Treiber), rechnet das Programm auf dem Prozessor.

## Rechner schonen

Standardmäßig an (Einstellungen > „Rechner schonen“). Beim Einlesen, Analysieren und bei der Dublettensuche läuft
das Programm dann mit niedriger Priorität, nimmt weniger Threads und macht nach jedem Arbeitsschritt eine kurze
Pause, damit Prozessor und Grafikkarte Zeit für anderes haben:

| Stufe | rechnet etwa | Pause nach jedem Schritt |
|---|---|---|
| Etwas (Standard) | drei Viertel der Zeit | ein Drittel der Arbeitszeit |
| Viel | die Hälfte der Zeit | so lang wie die Arbeitszeit |
| aus (kein Häkchen) | volle Leistung | keine |

Die Einstellung gilt sofort, auch während eines laufenden Indexlaufs (die Thread-Zahl ab dem nächsten Lauf).

Öffnet sich statt des eigenen Fensters der Browser, fehlt die „Microsoft Edge WebView2 Runtime“ (bei Windows 10/11
normalerweise dabei, sonst kostenlos bei microsoft.com). Das Programm funktioniert im Browser genauso.

Papierkorb: Nur auf Festplatten (auch externen USB-Festplatten). Auf USB-Sticks, Speicherkarten und Netzlaufwerken
gibt es keinen Papierkorb; dort bleibt die Datei liegen (mit Hinweis), statt endgültig gelöscht zu werden. Für solche
Laufwerke beim Aussortieren den Quarantäne-Ordner wählen.

## Erste Schritte

1. **Einstellungen:** Ordner des Bilder-Backups hinzufügen, Arbeitszeiten prüfen.
2. Links unten **„Index erstellen“**. Rechts unten in der Karte sieht man das Bild, das gerade analysiert wird.
   Suchen geht schon, während der Index läuft.
3. **Themen & Kategorien:** Für jedes Thema sieht man Beispielbilder und wie viele Bilder es betrifft. Festlegen, welches
   Thema zu welcher Kategorie zählt, eigene Themen und Kategorien anlegen, speichern. Die Einordnung wird sofort neu
   berechnet, ohne die Bilder neu zu analysieren.
4. **Personen:** Gruppen benennen, gleiche Personen zusammenführen. Oder ein Foto hineinziehen, um alle Bilder dieser Person zu finden.
   Ein Klick aufs Gesicht zeigt alle Gesichter der Person, die unsichersten zuerst. Falsche markieren und
   **Nicht diese Person** (wird gemerkt, auch für ähnliche Gesichter), **Kein Gesicht** oder **Anderer Person zuweisen**.
   Benannte und korrigierte Personen bleiben beim Neugruppieren erhalten, unbearbeitete Gruppen werden neu gebildet.
5. **Suche:** prüfen, korrigieren (Bilder markieren → Kategorie anklicken).
6. **Sortieren:** Modus wählen, Vorschau ansehen, ausführen. Unter „Bisherige Läufe“ lässt sich jeder Lauf zurücknehmen.
7. **Karte:** zeigt alle Fotos mit Standort. Die Zahl an einer Gruppe ist die Anzahl Fotos dort; Klick darauf listet sie
   rechts auf, „In der Suche zeigen“ übernimmt sie in die Suche (mit allen Funktionen: Kategorie setzen, kopieren ...).
   „Bereich auswählen“: Rechteck mit der Maus aufziehen. In der Suche gibt es dazu den Filter „Alle Orte“ (Länder und Städte).
   Ohne weitere Einstellung zeigt die Karte nur Ländergrenzen und braucht kein Internet, siehe unten.
8. **Dubletten:** „Dubletten suchen“. Nutzt den vorhandenen Index, es wird nichts neu analysiert. Pro Gruppe ist das Bild
   grün markiert, das bleiben soll (höchste Auflösung, Aufnahmedaten der Kamera, größte Datei, Name ohne „Kopie“,
   älteste Datei). Per Klick umstellen, dann die Gruppe oder alle angezeigten Gruppen bereinigen. Serienbilder
   (andere Aufnahmezeit laut Kamera) gelten nicht als Dubletten.

## Karte und Internet

Ortsnamen („Garching bei München, Bayern, Deutschland“) bestimmt das Programm ohne Internet aus einer mitgelieferten
Ortsliste. Die Kartenansicht zeigt ohne Internet die Ländergrenzen mit den Fotos darauf.

Für Straßen, Orte und Landschaft: **Einstellungen → Karte → „Kartenmaterial aus dem Internet laden“** einschalten
(standardmäßig aus). Dann holt das Programm Kartenbilder von OpenStreetMap. Dabei sieht der OpenStreetMap-Server die
IP-Adresse und welche Kartenausschnitte angesehen werden, also ungefähr, wo die Fotos entstanden sind. Fotos und
Koordinaten werden nicht gesendet. Geladene Kartenbilder bleiben in `daten\kacheln` und gehen danach auch offline;
löschen lassen sie sich in den Einstellungen. Das Programm hält sich an die
[Nutzungsregeln der OpenStreetMap-Kartenserver](https://operations.osmfoundation.org/policies/tiles/)
(eigene Kennung, höchstens zwei Verbindungen gleichzeitig, Zwischenspeicher, kein Vorausladen).

## Wie lange dauert der Index?

Grobe Werte für 10.000 Bilder auf einem normalen PC ohne Grafikkarte:

| Schritt | Dauer |
|---|---|
| Metadaten und Vorschaubilder | wenige Minuten |
| Bildinhalt (für Suche und Einordnung) | ca. 30–60 min |
| Gesichter | ca. 30 min |
| Texterkennung (nur Bilder mit Text) | je nach Anteil |
| Bildbeschreibungen | mehrere Stunden (in den Einstellungen abschaltbar) |
| Ausführliche Beschreibungen (optional) | Florence-2 large: ca. 6–10 h mit Grafikkarte (RX 5600 XT), base etwa ein Drittel; ohne Grafikkarte etwa 8-mal so lange |

Mit Grafikkarte geht es deutlich schneller. Der Lauf kann jederzeit angehalten und später fortgesetzt werden.

## Verwendete Modelle

| Aufgabe | Modell |
|---|---|
| Bildinhalt, Bedeutungssuche (versteht Deutsch) | CLIP `xlm-roberta-base-ViT-B-32` (LAION, open_clip) |
| Bildbeschreibung | BLIP `Salesforce/blip-image-captioning-base`, übersetzt mit `Helsinki-NLP/opus-mt-en-de` |
| Ausführliche Beschreibung (optional, Standard aus) | Florence-2 `microsoft/Florence-2-large` oder `-base` (MIT-Lizenz), übersetzt mit Marian. Wird erst geladen, wenn man die Option in den Einstellungen einschaltet (1,6 GB bzw. 0,5 GB, nach `daten/modelle/ausfuehrlich`). Auf der Grafikkarte in halber Genauigkeit, ca. 1,6 GB Grafikspeicher |
| Gesichter | InsightFace buffalo_l: SCRFD-10G (Erkennung, 5 Merkmalspunkte) + ArcFace R50/WebFace600K (Wiedererkennung), über onnxruntime. Modelllizenz: nur nicht-kommerziell |
| Texterkennung | Windows-Texterkennung (Deutsch), sonst RapidOCR |

Karte: [Leaflet](https://leafletjs.com) und Leaflet.markercluster (liegen im Programmordner, kein Nachladen aus dem Netz).
Ortsliste: [GeoNames](https://www.geonames.org) (CC BY 4.0), Ländergrenzen: [Natural Earth](https://www.naturalearthdata.com)
über world-atlas, Kartenbilder: © OpenStreetMap-Mitwirkende. Die Daten-Dateien erzeugen `werkzeuge/orte_bauen.py` und
`werkzeuge/welt_bauen.py`.

## Daten

Alles liegt im Ordner `daten`: `index.sqlite` (Suchindex), `vorschau` (Vorschaubilder), `modelle`,
`protokolle` (für Rückgängig), `kacheln` (Kartenbilder), `einstellungen.json` und `imgsortsearch.log`.

Speicherplatz: Unter Einstellungen > „Modelle und Speicherplatz“ stehen alle heruntergeladenen Modelle mit Größe.
Modelle für ausgeschaltete Funktionen oder alte Versionen lassen sich dort löschen (mit Bestätigung). Schaltet man die
Funktion später wieder ein, wird das Modell automatisch neu geladen. Den Download-Zwischenspeicher der Pakete
(`umgebung\cache`) leeren `install.bat` und `update.bat` am Ende selbst.

## Entwicklung

```bash
pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu
pip install -r requirements-dev.txt
python -m pytest            # Tests nutzen Ersatzmodelle, kein Download nötig
python -m imgsortsearch --browser
```
