"use strict";
/* Karte: Fotos mit GPS-Daten auf einer Karte (Leaflet, liegt im Programmordner, kein CDN).
   Kartenbilder kommen über den eigenen Server: aus dem Zwischenspeicher oder, wenn in den Einstellungen
   erlaubt, von OpenStreetMap. Ohne Internet bleiben die Ländergrenzen aus welt.geojson. */

const KACHEL_URL = "/api/karte/kachel/{z}/{x}/{y}.png";
const LEERE_KACHEL = "data:image/gif;base64,R0lGODlhAQABAAAAACH5BAEKAAEALAAAAAABAAEAAAICTAEAOw==";
const OSM_HINWEIS = "© OpenStreetMap-Mitwirkende";

const K = {
  karte: null, gruppe: null, punkte: [], orte: [], marker: new Map(), geladen: false, info: null,
  auswahlModus: false, rechteck: null, gezeigt: null, ziel: null,
};

let weltDaten = null;
function welt() {
  if (!weltDaten) weltDaten = fetch("vendor/welt.geojson").then((r) => r.json()).catch(() => null);
  return weltDaten;
}

// Grundkarte: Ländergrenzen darunter (immer da, auch offline), Kartenbilder darüber
function grundkarte(karte) {
  karte.createPane("welt").style.zIndex = 150;
  const leinwand = L.canvas({ pane: "welt" });
  welt().then((geo) => {
    if (geo) L.geoJSON(geo, { pane: "welt", renderer: leinwand, interactive: false,
      style: { color: "#46506b", weight: 1, fillColor: "#2b3346", fillOpacity: 1 } }).addTo(karte);
  });
  const kacheln = L.tileLayer(KACHEL_URL, { maxZoom: 19, errorTileUrl: LEERE_KACHEL, attribution: OSM_HINWEIS });
  kacheln.on("tileerror", () => { clearTimeout(K.infoTimer); K.infoTimer = setTimeout(kartenHinweis, 800); });
  kacheln.addTo(karte);
  karte.attributionControl.setPrefix("Leaflet");
  return kacheln;
}

function fotoIcon(id) {
  return L.divIcon({ className: "", iconSize: [38, 38], iconAnchor: [19, 19],
    html: `<div class="foto-punkt" style="width:38px;height:38px;background-image:url(/api/vorschau/${id})"></div>` });
}

function gruppenIcon(gruppe) {
  const n = gruppe.getChildCount();
  const groesse = n < 10 ? 46 : n < 100 ? 54 : 62;
  const erstes = gruppe.getAllChildMarkers()[0];
  return L.divIcon({ className: "", iconSize: [groesse, groesse], iconAnchor: [groesse / 2, groesse / 2],
    html: `<div class="foto-gruppe" style="width:${groesse}px;height:${groesse}px;background-image:url(/api/vorschau/${erstes.bildId})"><b>${n.toLocaleString("de")}</b></div>` });
}

function karteAnlegen() {
  K.karte = L.map("landkarte", { worldCopyJump: true, minZoom: 2, zoomSnap: 0.5, boxZoom: false });
  K.karte.setView([30, 10], 2);
  K.kacheln = grundkarte(K.karte);
  K.online = !!(S.einst.karte && S.einst.karte.online);
  K.gruppe = L.markerClusterGroup({
    chunkedLoading: true, showCoverageOnHover: false, zoomToBoundsOnClick: false, spiderfyOnMaxZoom: false,
    maxClusterRadius: 64, iconCreateFunction: gruppenIcon,
  });
  K.gruppe.on("clusterclick", (e) => zeigeFotos(e.layer.getAllChildMarkers(), e.layer.getBounds()));
  K.gruppe.on("click", (e) => {
    // ein einzelnes Foto: alle Fotos an derselben Stelle (etwa 30 m) mitnehmen
    const p = e.layer.getLatLng();
    const nah = [...K.marker.values()].filter((m) => K.gruppe.hasLayer(m) && m.getLatLng().distanceTo(p) < 30);
    zeigeFotos(nah.length ? nah : [e.layer]);
  });
  K.gruppe.addTo(K.karte);
  bereichAuswahl();
}

async function kartenHinweis() {
  try { K.info = await api("/api/karte/info"); } catch { return; }
  const h = $("#k-hinweis");
  if (!K.info.online) {
    h.innerHTML = `Offline-Karte: nur Ländergrenzen${K.info.kacheln ? " und schon geladene Kartenbilder" : ""}. Für Straßen und Orte
      unter <a data-einst>Einstellungen → Karte</a> „Kartenmaterial aus dem Internet laden“ einschalten.`;
    h.hidden = false;
  } else if (K.info.offline) {
    h.innerHTML = `Keine Verbindung zum Kartenserver${K.info.fehler ? ` (${esc(K.info.fehler)})` : ""}. Es werden nur schon geladene Kartenbilder gezeigt.`;
    h.hidden = false;
  } else h.hidden = true;
  const a = $("[data-einst]", h);
  if (a) a.onclick = () => zeige("einstellungen");
}

function kartenParameter() {
  return new URLSearchParams({ kategorie: $("#k-kategorie").value, jahr: $("#k-jahr").value });
}

async function ladePunkte(anpassen) {
  const r = await api("/api/karte/punkte?" + kartenParameter());
  K.punkte = r.punkte;
  K.orte = r.orte;
  K.gruppe.clearLayers();
  K.marker.clear();
  const neu = r.punkte.map(([id, lat, lon, ort]) => {
    const m = L.marker([lat, lon], { icon: fotoIcon(id), title: K.orte[ort] || "" });
    m.bildId = id;
    m.ort = ort;
    K.marker.set(id, m);
    return m;
  });
  K.gruppe.addLayers(neu);
  $("#k-zahl").textContent = `${r.punkte.length.toLocaleString("de")} Fotos mit Standort`
    + (r.ohne_gps ? ` · ${r.ohne_gps.toLocaleString("de")} ohne` : "");
  if (anpassen && neu.length) K.karte.fitBounds(K.gruppe.getBounds(), { padding: [40, 40], maxZoom: 14 });
}

async function ladeKarte() {
  if (!K.karte) karteAnlegen();
  // Filterauswahl wie in der Suche
  for (const [von, nach] of [["#f-kategorie", "#k-kategorie"], ["#f-jahr", "#k-jahr"]]) {
    const alt = $(nach).value;
    $(nach).innerHTML = $(von).innerHTML;
    $(nach).value = alt;
  }
  setTimeout(() => K.karte.invalidateSize(), 0);
  const online = !!(S.einst.karte && S.einst.karte.online);
  if (online !== K.online) { K.online = online; K.kacheln.redraw(); }  // Einstellung geändert  // war die Ansicht versteckt, kennt Leaflet die Größe nicht
  kartenHinweis();
  await ladePunkte(!K.geladen && !K.ziel);
  K.geladen = true;
  if (K.ziel) { const z = K.ziel; K.ziel = null; springeZu(z); }
}
window.ladeKarte = ladeKarte;
["#k-kategorie", "#k-jahr"].forEach((s) => $(s).addEventListener("change", () => ladePunkte(true).catch((e) => toast(e.message, true))));

function springeZu({ lat, lon, id }) {
  const m = K.marker.get(id);
  if (m) K.gruppe.zoomToShowLayer(m, () => {
    const p = m.getLatLng();
    zeigeFotos([...K.marker.values()].filter((x) => x.getLatLng().distanceTo(p) < 30));
  });
  else K.karte.setView([lat, lon], 16);
}

// aus der Detailansicht: Karte öffnen und zum Foto springen
window.karteZeigen = (lat, lon, id) => {
  K.ziel = { lat, lon, id };
  zeige("karte");
};

/* ------------------------------------------------------------ Fotos zu einer Stelle */

// „Palma (46) · Es Molinar bei Palma (3)“ und darunter „Balearen, Spanien“
function ortsText(marker) {
  const n = new Map();
  let rest = "";
  marker.forEach((m) => {
    const o = K.orte[m.ort];
    if (!o) return;
    const [kurz, ...weiter] = o.split(", ");
    n.set(kurz, (n.get(kurz) || 0) + 1);
    if (!rest) rest = weiter.join(", ");
  });
  const liste = [...n.entries()].sort((a, b) => b[1] - a[1]);
  if (!liste.length) return { kurz: "", text: "" };
  const teile = liste.slice(0, 4).map(([o, z]) => liste.length > 1 ? `${o} (${z.toLocaleString("de")})` : o);
  return { kurz: liste[0][0], mehrere: liste.length > 1,
    text: teile.join(" · ") + (liste.length > 4 ? ` · und ${liste.length - 4} weitere` : "") + (rest ? `\n${rest}` : "") };
}

function zeigeFotos(marker, grenzen) {
  if (!marker.length) { $("#k-fotos").hidden = true; return; }
  const b = grenzen || L.latLngBounds(marker.map((m) => m.getLatLng()));
  K.gezeigt = { ids: marker.map((m) => m.bildId), grenzen: b, ort: ortsText(marker) };
  $("#k-fotos-titel").textContent = `${marker.length.toLocaleString("de")} ${marker.length === 1 ? "Foto" : "Fotos"}`;
  $("#k-fotos-ort").textContent = K.gezeigt.ort.text || "Ort unbekannt";
  const max = 300;
  $("#k-fotos-gitter").innerHTML = marker.slice(0, max).map((m) =>
    `<img loading="lazy" src="/api/vorschau/${m.bildId}" data-id="${m.bildId}" title="${esc(K.orte[m.ort] || "")}" alt="">`).join("")
    + (marker.length > max ? `<div class="hinweis">… und ${(marker.length - max).toLocaleString("de")} weitere. „In der Suche zeigen“ zeigt alle.</div>` : "");
  $("#k-fotos").hidden = false;
}

$("#k-fotos-gitter").addEventListener("click", (e) => {
  const img = e.target.closest("img[data-id]");
  if (!img) return;
  $("#vollbild-img").src = `/api/ansicht/${img.dataset.id}?kante=3000`;
  $("#vollbild").hidden = false;
});
$("#k-fotos-zu").onclick = () => {
  $("#k-fotos").hidden = true;
  if (K.rechteck) { K.rechteck.remove(); K.rechteck = null; }
};

function grenzenText(b) {
  // etwas Rand, damit Fotos genau auf der Kante (Rundung) dabei sind
  const r = 0.00005;
  return [b.getSouth() - r, b.getWest() - r, b.getNorth() + r, b.getEast() + r].map((x) => x.toFixed(5)).join(",");
}

function inSucheZeigen(grenzen, text) {
  window.bereichFilter(grenzenText(grenzen), text, { kategorie: $("#k-kategorie").value, jahr: $("#k-jahr").value });
}
$("#k-fotos-suche").onclick = () => {
  if (!K.gezeigt) return;
  const o = K.gezeigt.ort;
  inSucheZeigen(K.gezeigt.grenzen, o.kurz ? `Karte: ${o.kurz}${o.mehrere ? " und Umgebung" : ""}` : "Kartenbereich");
};
$("#k-ausschnitt").onclick = () => inSucheZeigen(K.karte.getBounds(), "Kartenausschnitt");

/* ------------------------------------------------------------ Bereich aufziehen */

function bereichAuswahl() {
  let start = null;
  const ende = () => {
    K.auswahlModus = false;
    $("#k-bereich").classList.remove("aktiv");
    $("#landkarte").classList.remove("auswahl");
    K.karte.dragging.enable();
  };
  $("#k-bereich").onclick = () => {
    if (K.auswahlModus) { ende(); return; }
    K.auswahlModus = true;
    $("#k-bereich").classList.add("aktiv");
    $("#landkarte").classList.add("auswahl");
    K.karte.dragging.disable();
    toast("Mit gedrückter Maustaste ein Rechteck aufziehen");
  };
  K.karte.on("mousedown", (e) => {
    if (!K.auswahlModus) return;
    start = e.latlng;
    if (K.rechteck) K.rechteck.remove();
    K.rechteck = L.rectangle(L.latLngBounds(start, start), { color: "#22d3ee", weight: 2, fillOpacity: 0.12, interactive: false }).addTo(K.karte);
  });
  K.karte.on("mousemove", (e) => { if (start && K.rechteck) K.rechteck.setBounds(L.latLngBounds(start, e.latlng)); });
  K.karte.on("mouseup", () => {
    if (!start || !K.rechteck) return;
    start = null;
    ende();
    const b = K.rechteck.getBounds();
    const drin = [...K.marker.values()].filter((m) => b.contains(m.getLatLng()));
    if (!drin.length) toast("In diesem Bereich sind keine Fotos.");
    zeigeFotos(drin, b);
  });
}

/* ------------------------------------------------------------ kleine Karte beim Bild */

let miniKarte = null;
window.miniKarteZeigen = (el, lat, lon, id) => {
  if (miniKarte) { miniKarte.remove(); miniKarte = null; }
  if (!el) return;
  const online = !!(S.einst.karte && S.einst.karte.online);
  miniKarte = L.map(el, { zoomControl: true, scrollWheelZoom: false, attributionControl: true });
  miniKarte.setView([lat, lon], online ? 14 : 5);
  grundkarte(miniKarte);
  L.circleMarker([lat, lon], { radius: 7, color: "#fff", weight: 2, fillColor: "#8b5cf6", fillOpacity: 1 })
    .addTo(miniKarte).bindTooltip("Auf der großen Karte zeigen").on("click", () => window.karteZeigen(lat, lon, id));
};
