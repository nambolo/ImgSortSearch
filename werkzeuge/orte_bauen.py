"""Baut die Ortsdatei für die Ortsnamen ohne Internet (imgsortsearch/orte/).

Nur für Entwickler, die Datei liegt fertig im Repository. Quellen (beide von PyPI):
  - geonamescache (GeoNames cities1000, CC BY 4.0): Orte ab 1.000 Einwohnern mit Koordinaten
  - reverse_geocoder (GeoNames, CC BY 4.0): Namen der Bundesländer/Regionen
  - all-the-cities (npm, GeoNames): Ortsart, damit Stadtteile (Bogenhausen) nicht als eigene Stadt gelten
  - pycountry: deutsche Ländernamen

Aufruf: python werkzeuge/orte_bauen.py <cities1000.json> <rg_cities1000.csv> <cities.pbf>
"""
import csv
import gettext
import gzip
import json
import sys
from collections import Counter
from pathlib import Path

import pycountry

ZIEL = Path(__file__).resolve().parent.parent / "imgsortsearch" / "orte"

# Deutsche Namen bekannter Orte, die GeoNames englisch oder in Landessprache führt
DEUTSCH = {
    ("Munich", "DE"): "München", ("Nuremberg", "DE"): "Nürnberg", ("Cologne", "DE"): "Köln",
    ("Hanover", "DE"): "Hannover", ("Brunswick", "DE"): "Braunschweig", ("Frankfurt (Oder)", "DE"): "Frankfurt (Oder)",
    ("Vienna", "AT"): "Wien", ("Zurich", "CH"): "Zürich", ("Geneva", "CH"): "Genf", ("Lucerne", "CH"): "Luzern",
    ("Bern", "CH"): "Bern", ("Neuchâtel", "CH"): "Neuenburg", ("Fribourg", "CH"): "Freiburg im Üechtland",
    ("Sion", "CH"): "Sitten", ("Biel/Bienne", "CH"): "Biel",
    ("Rome", "IT"): "Rom", ("Milan", "IT"): "Mailand", ("Naples", "IT"): "Neapel", ("Turin", "IT"): "Turin",
    ("Venice", "IT"): "Venedig", ("Florence", "IT"): "Florenz", ("Genoa", "IT"): "Genua", ("Padua", "IT"): "Padua",
    ("Syracuse", "IT"): "Syrakus", ("Bolzano", "IT"): "Bozen", ("Merano", "IT"): "Meran", ("Brixen", "IT"): "Brixen",
    ("Bressanone", "IT"): "Brixen", ("Trento", "IT"): "Trient", ("Mantova", "IT"): "Mantua",
    ("Prague", "CZ"): "Prag", ("Pilsen", "CZ"): "Pilsen", ("Plzeň", "CZ"): "Pilsen", ("Karlovy Vary", "CZ"): "Karlsbad",
    ("Brno", "CZ"): "Brünn", ("České Budějovice", "CZ"): "Budweis",
    ("Warsaw", "PL"): "Warschau", ("Kraków", "PL"): "Krakau", ("Wrocław", "PL"): "Breslau", ("Gdańsk", "PL"): "Danzig",
    ("Szczecin", "PL"): "Stettin", ("Poznań", "PL"): "Posen",
    ("Copenhagen", "DK"): "Kopenhagen", ("Brussels", "BE"): "Brüssel", ("Antwerp", "BE"): "Antwerpen",
    ("Bruges", "BE"): "Brügge", ("Ghent", "BE"): "Gent", ("Liège", "BE"): "Lüttich",
    ("The Hague", "NL"): "Den Haag", ("Lisbon", "PT"): "Lissabon", ("Athens", "GR"): "Athen",
    ("Thessaloníki", "GR"): "Thessaloniki", ("Moscow", "RU"): "Moskau", ("Saint Petersburg", "RU"): "Sankt Petersburg",
    ("Kyiv", "UA"): "Kiew", ("Belgrade", "RS"): "Belgrad", ("Bucharest", "RO"): "Bukarest", ("Sofia", "BG"): "Sofia",
    ("Gothenburg", "SE"): "Göteborg", ("Strasbourg", "FR"): "Straßburg", ("Nice", "FR"): "Nizza",
    ("Mulhouse", "FR"): "Mülhausen", ("Metz", "FR"): "Metz", ("Seville", "ES"): "Sevilla",
    ("Sevilla", "ES"): "Sevilla", ("Majorca", "ES"): "Mallorca", ("Palma", "ES"): "Palma",
    ("Bratislava", "SK"): "Bratislava", ("Ljubljana", "SI"): "Laibach", ("Luxembourg", "LU"): "Luxemburg",
    ("Istanbul", "TR"): "Istanbul", ("İzmir", "TR"): "Izmir", ("New York City", "US"): "New York",
    ("Cairo", "EG"): "Kairo", ("Mexico City", "MX"): "Mexiko-Stadt", ("Beijing", "CN"): "Peking",
    ("Tokyo", "JP"): "Tokio", ("Jerusalem", "IL"): "Jerusalem", ("Dubrovnik", "HR"): "Dubrovnik",
    ("Rijeka", "HR"): "Rijeka", ("Split", "HR"): "Split", ("Pula", "HR"): "Pula",
}
# Bundesländer und Kantone mit deutschem Namen (Schlüssel: englischer Name aus GeoNames)
REGIONEN = {
    "Bavaria": "Bayern", "North Rhine-Westphalia": "Nordrhein-Westfalen", "Lower Saxony": "Niedersachsen",
    "Hesse": "Hessen", "Rheinland-Pfalz": "Rheinland-Pfalz", "Rhineland-Palatinate": "Rheinland-Pfalz",
    "Saxony": "Sachsen", "Saxony-Anhalt": "Sachsen-Anhalt", "Thuringia": "Thüringen",
    "Mecklenburg-Vorpommern": "Mecklenburg-Vorpommern", "Mecklenburg-Western Pomerania": "Mecklenburg-Vorpommern",
    "Baden-Wuerttemberg": "Baden-Württemberg", "Baden-Württemberg": "Baden-Württemberg",
    "Schleswig-Holstein": "Schleswig-Holstein", "Brandenburg": "Brandenburg", "Saarland": "Saarland",
    "Land Berlin": "Berlin", "Berlin": "Berlin", "Hamburg": "Hamburg", "Bremen": "Bremen",
    "Lower Austria": "Niederösterreich", "Upper Austria": "Oberösterreich", "Styria": "Steiermark",
    "Carinthia": "Kärnten", "Tyrol": "Tirol", "Salzburg": "Salzburg", "Vorarlberg": "Vorarlberg",
    "Burgenland": "Burgenland", "Vienna": "Wien",
    "Zurich": "Zürich", "Bern": "Bern", "Lucerne": "Luzern", "Geneva": "Genf", "Grisons": "Graubünden",
    "Valais": "Wallis", "Ticino": "Tessin", "Vaud": "Waadt", "Fribourg": "Freiburg", "Neuchatel": "Neuenburg",
    "Saint Gallen": "St. Gallen", "Aargau": "Aargau", "Thurgau": "Thurgau", "Solothurn": "Solothurn",
    "Basel-City": "Basel-Stadt", "Basel-Landschaft": "Basel-Landschaft", "Schwyz": "Schwyz", "Zug": "Zug",
    "Trentino-Alto Adige": "Trentino-Südtirol", "Lombardy": "Lombardei", "Tuscany": "Toskana",
    "Piedmont": "Piemont", "Sicily": "Sizilien", "Sardinia": "Sardinien", "Apulia": "Apulien",
    "Veneto": "Venetien", "Latium": "Latium", "Campania": "Kampanien", "Liguria": "Ligurien",
    "Balearic Islands": "Balearen", "Canary Islands": "Kanarische Inseln", "Andalusia": "Andalusien",
    "Catalonia": "Katalonien", "Bohemia": "Böhmen", "South Tyrol": "Südtirol",
}


def _varint(b: bytes, i: int) -> tuple[int, int]:
    erg = schieb = 0
    while True:
        x = b[i]
        i += 1
        erg |= (x & 0x7F) << schieb
        if x < 0x80:
            return erg, i
        schieb += 7


def ortsarten(pbf_datei: str) -> dict[int, str]:
    """GeoNames-Nummer -> Ortsart (PPL, PPLA, PPLX = Stadtteil ...) aus der Protobuf-Datei von all-the-cities."""
    b = Path(pbf_datei).read_bytes()
    i, erg = 0, {}
    while i < len(b):
        laenge, i = _varint(b, i)
        ende, nr, art = i + laenge, 0, ""
        while i < ende:
            schluessel, i = _varint(b, i)
            feld, typ = schluessel >> 3, schluessel & 7
            if typ == 0:
                wert, i = _varint(b, i)
                if feld == 1:
                    nr = (wert >> 1) ^ -(wert & 1)
            elif typ == 2:
                n, i = _varint(b, i)
                if feld == 7:
                    art = b[i:i + n].decode("utf-8")
                i += n
            else:
                raise ValueError(f"unerwarteter Typ {typ}")
        erg[nr] = art
    return erg


def main(gnc_datei: str, rg_datei: str, pbf_datei: str):
    de = gettext.translation("iso3166-1", pycountry.LOCALES_DIR, languages=["de"])
    orte = json.load(open(gnc_datei, encoding="utf-8"))
    # Regionsnamen: über gleiche Koordinaten aus reverse_geocoder übernehmen, Mehrheit je Regionscode
    rg = {}
    with open(rg_datei, encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            rg[(round(float(r["lat"]), 2), round(float(r["lon"]), 2), r["cc"].strip())] = r["admin1"]
    stimmen: dict[str, Counter] = {}
    for o in orte.values():
        name = rg.get((round(o["latitude"], 2), round(o["longitude"], 2), o["countrycode"]))
        if name:
            stimmen.setdefault(f'{o["countrycode"]}.{o["admin1code"]}', Counter())[name] += 1
    regionen = {k: REGIONEN.get(c.most_common(1)[0][0], c.most_common(1)[0][0]) for k, c in stimmen.items()}

    arten = ortsarten(pbf_datei)
    print(len(arten), "Ortsarten,", Counter(arten.values()).most_common(6))
    ZIEL.mkdir(parents=True, exist_ok=True)
    zeilen = []
    for o in orte.values():
        cc = o["countrycode"]
        name = DEUTSCH.get((o["name"], cc), o["name"])
        # englischer Name bleibt suchbar ("Munich" findet auch München)
        andere = o["name"] if o["name"] != name else ""
        zeilen.append("\t".join([f'{o["latitude"]:.4f}', f'{o["longitude"]:.4f}', name, cc,
                                 o["admin1code"] or "", str(o["population"]), andere,
                                 "1" if arten.get(o["geonameid"], "").startswith("PPLX") else ""]))
    zeilen.sort()
    with gzip.open(ZIEL / "orte.tsv.gz", "wt", encoding="utf-8", compresslevel=9) as f:
        f.write("\n".join(zeilen) + "\n")
    laender = {}
    for c in pycountry.countries:
        laender[c.alpha_2] = de.gettext(c.name)
    laender["XK"] = "Kosovo"
    (ZIEL / "regionen.json").write_text(json.dumps({"laender": laender, "regionen": regionen},
                                                   ensure_ascii=False, sort_keys=True), "utf-8")
    print(len(zeilen), "Orte,", len(regionen), "Regionen")


if __name__ == "__main__":
    main(*sys.argv[1:4])
