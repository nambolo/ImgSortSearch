"""Vorgegebene Themen. In der Oberfläche änder- und erweiterbar."""

# kategorie: in welche Kategorie das Thema zählt ("" = nur als Schlagwort)
STANDARD_THEMEN = [
    # eher Arbeit
    {"name": "Whiteboard", "prompt": "ein Whiteboard oder Flipchart mit Notizen", "kategorie": "Arbeit"},
    {"name": "Technische Zeichnung", "prompt": "eine technische Zeichnung oder ein Schaltplan", "kategorie": "Arbeit"},
    {"name": "Technik", "prompt": "technische Geräte, Elektronik und Kabel", "kategorie": "Arbeit"},
    {"name": "Maschine", "prompt": "eine Industriemaschine in einer Werkhalle", "kategorie": "Arbeit"},
    {"name": "Schaltschrank", "prompt": "ein Schaltschrank mit Verkabelung", "kategorie": "Arbeit"},
    {"name": "Bauteil", "prompt": "ein technisches Bauteil auf einem Tisch", "kategorie": "Arbeit"},
    {"name": "Baustelle", "prompt": "eine Baustelle", "kategorie": "Arbeit"},
    {"name": "Screenshot", "prompt": "ein Screenshot einer Software", "kategorie": "Arbeit"},
    {"name": "Bildschirm", "prompt": "ein Foto von einem Computerbildschirm", "kategorie": "Arbeit"},
    {"name": "Präsentation", "prompt": "eine Präsentationsfolie", "kategorie": "Arbeit"},
    {"name": "Diagramm", "prompt": "ein Diagramm oder eine Grafik mit Zahlen", "kategorie": "Arbeit"},
    {"name": "Dokument", "prompt": "ein Dokument oder Formular aus Papier", "kategorie": "Arbeit"},
    {"name": "Beleg", "prompt": "ein Kassenbon oder eine Rechnung", "kategorie": "Arbeit"},
    {"name": "Büro", "prompt": "ein Büro mit Schreibtischen", "kategorie": "Arbeit"},
    {"name": "Besprechung", "prompt": "eine Besprechung im Konferenzraum", "kategorie": "Arbeit"},
    {"name": "Messestand", "prompt": "ein Messestand", "kategorie": "Arbeit"},
    # eher privat
    {"name": "Familie", "prompt": "ein Familienfoto", "kategorie": "Privat"},
    {"name": "Kinder", "prompt": "Kinder beim Spielen", "kategorie": "Privat"},
    {"name": "Baby", "prompt": "ein Baby", "kategorie": "Privat"},
    {"name": "Freunde", "prompt": "Freunde, die zusammen lachen", "kategorie": "Privat"},
    {"name": "Selfie", "prompt": "ein Selfie", "kategorie": "Privat"},
    {"name": "Feier", "prompt": "eine Geburtstagsfeier oder Party", "kategorie": "Privat"},
    {"name": "Hochzeit", "prompt": "eine Hochzeit", "kategorie": "Privat"},
    {"name": "Weihnachten", "prompt": "Weihnachten mit Weihnachtsbaum", "kategorie": "Privat"},
    {"name": "Urlaub", "prompt": "ein Urlaubsfoto", "kategorie": "Privat"},
    {"name": "Strand", "prompt": "ein Strand am Meer", "kategorie": "Privat"},
    {"name": "Berge", "prompt": "Berge und Wandern", "kategorie": "Privat"},
    {"name": "Landschaft", "prompt": "eine Landschaft in der Natur", "kategorie": "Privat"},
    {"name": "Sonnenuntergang", "prompt": "ein Sonnenuntergang", "kategorie": "Privat"},
    {"name": "Schnee", "prompt": "eine Winterlandschaft mit Schnee", "kategorie": "Privat"},
    {"name": "Garten", "prompt": "ein Garten mit Blumen", "kategorie": "Privat"},
    {"name": "Haustier", "prompt": "ein Haustier wie Hund oder Katze", "kategorie": "Privat"},
    {"name": "Essen", "prompt": "ein Teller mit Essen", "kategorie": "Privat"},
    {"name": "Sport", "prompt": "Sport und Freizeit", "kategorie": "Privat"},
    {"name": "Zuhause", "prompt": "ein gemütliches Wohnzimmer", "kategorie": "Privat"},
    # neutral (nur als Schlagwort)
    {"name": "Stadt", "prompt": "eine Straße in einer Stadt", "kategorie": ""},
    {"name": "Gebäude", "prompt": "ein Gebäude von außen", "kategorie": ""},
    {"name": "Auto", "prompt": "ein Auto", "kategorie": ""},
    {"name": "Personen", "prompt": "eine Gruppe von Menschen", "kategorie": ""},
    {"name": "Porträt", "prompt": "ein Porträt einer Person", "kategorie": ""},
    {"name": "Schild", "prompt": "ein Schild mit Schrift", "kategorie": ""},
    {"name": "Nacht", "prompt": "eine Aufnahme bei Nacht", "kategorie": ""},
]

STANDARD_KATEGORIEN = [
    {"name": "Arbeit", "farbe": "#2563eb"},
    {"name": "Privat", "farbe": "#16a34a"},
]
