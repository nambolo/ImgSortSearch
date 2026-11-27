"use strict";
/* ImgSortSearch Oberfläche: ohne Frameworks, läuft komplett offline. */

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (t) => String(t ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const FARBEN = ["#2563eb", "#16a34a", "#f59e0b", "#db2777", "#0891b2", "#7c3aed", "#ea580c", "#65a30d"];

async function api(pfad, opt = {}) {
  // Kennung, ohne die der Server ändernde Anfragen ablehnt (Schutz vor fremden Webseiten)
  const o = { ...opt, headers: { "X-ImgSortSearch": "1", ...(opt.headers || {}) } };
  if (o.json !== undefined) { o.body = JSON.stringify(o.json); o.headers["Content-Type"] = "application/json"; o.method = o.method || "POST"; }
  const r = await fetch(pfad, o);
  let daten = null;
  try { daten = await r.json(); } catch { /* leer */ }
  if (!r.ok) throw new Error((daten && daten.detail) || `Fehler ${r.status}`);
  return daten;
}

function toast(text, fehler = false) {
  const t = $("#toast");
  t.textContent = text;
  t.classList.toggle("fehler", fehler);
  t.classList.add("zeigen");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => t.classList.remove("zeigen"), fehler ? 6000 : 2800);
}

// Liefert true (Ja), false (Abbrechen) oder "drittes", wenn ein dritter Knopf angegeben ist
function frage(titel, html, jaText = "OK", drittesText = "") {
  return new Promise((ok) => {
    $("#dialog-titel").textContent = titel;
    $("#dialog-text").innerHTML = html;
    $("#dialog-ja").textContent = jaText;
    $("#dialog-drittes").textContent = drittesText;
    $("#dialog-drittes").hidden = !drittesText;
    $("#dialog").hidden = false;
    const fertig = (w) => {
      $("#dialog").hidden = true;
      $("#dialog-ja").onclick = $("#dialog-nein").onclick = $("#dialog-drittes").onclick = null;
      ok(w);
    };
    $("#dialog-ja").onclick = () => fertig(true);
    $("#dialog-nein").onclick = () => fertig(false);
    $("#dialog-drittes").onclick = () => fertig("drittes");
  });
}

const S = {
  einst: null, kategorien: [], zusatz: [], farben: {},
  bilder: [], gesamt: 0, gewaehlt: new Set(), letzter: null, fokus: null,
  modus: localStorage.getItem("modus") || "raster",
  person: null, personName: "", aehnlich: null, bildSuche: null,
  laedt: false,
};

function farbe(kat) { return S.farben[kat] || "#475069"; }

/* ------------------------------------------------------------ Navigation */

function zeige(ansicht) {
  $$("nav button").forEach((b) => b.classList.toggle("aktiv", b.dataset.ansicht === ansicht));
  $$(".ansicht").forEach((a) => a.classList.toggle("aktiv", a.id === "ansicht-" + ansicht));
  const laden = { themen: ladeThemen, personen: ladePersonen, sortieren: ladeSortieren, einstellungen: ladeEinstellungen }[ansicht];
  if (laden) Promise.resolve().then(laden).catch((e) => toast(e.message, true));
}
$$("nav button").forEach((b) => b.addEventListener("click", () => zeige(b.dataset.ansicht)));

/* ------------------------------------------------------------ Status */

let letzteAktuell = null;
let statusTimer = null;
async function status() {
  clearTimeout(statusTimer);
  try {
    const s = await api("/api/status");
    const i = s.index;
    const knopf = $("#index-knopf");
    if (!i.laeuft) S.wirdAngehalten = false;
    const fortsetzen = i.phase === "angehalten" && s.offen > 0;
    knopf.textContent = i.laeuft ? (S.wirdAngehalten ? "Wird angehalten …" : "Anhalten")
      : (fortsetzen ? "Fortsetzen" : (s.gesamt ? "Index aktualisieren" : "Index erstellen"));
    knopf.disabled = i.laeuft && S.wirdAngehalten;
    knopf.classList.toggle("primaer", !i.laeuft);
    $("#index-phase").textContent = { suchen: "Suche Dateien", metadaten: "Metadaten", analyse: "Analyse", einordnen: "Einordnen", fertig: "Index", angehalten: "Angehalten", fehler: "Fehler", bereit: "Index" }[i.phase] || "Index";
    $("#index-zahl").textContent = i.laeuft && i.gesamt ? `${i.fertig.toLocaleString("de")} / ${i.gesamt.toLocaleString("de")}` : `${s.gesamt.toLocaleString("de")} Bilder`;
    $("#index-balken").style.width = i.gesamt ? `${(100 * i.fertig) / i.gesamt}%` : (i.laeuft ? "5%" : "0");
    const meldung = i.phase === "angehalten" && s.offen ? `Angehalten. ${s.offen.toLocaleString("de")} Bilder noch nicht analysiert.` : i.meldung;
    const fehlen = (s.quellen_fehlen || []).length ? ` ${s.quellen_fehlen.length} Bilder-Ordner nicht gefunden.` : "";
    $("#index-meldung").textContent = (meldung || (s.offen ? `${s.offen.toLocaleString("de")} Bilder noch nicht analysiert.` : (s.gesamt ? "Index ist aktuell." : "Noch kein Index erstellt."))) + fehlen;
    S.quellenFehlen = s.quellen_fehlen || [];
    const bild = $("#index-bild");
    bild.classList.toggle("laeuft", i.laeuft && i.phase === "analyse");
    if (i.laeuft && i.aktuell && i.aktuell !== letzteAktuell) {
      letzteAktuell = i.aktuell;
      bild.style.backgroundImage = `url(/api/vorschau/${i.aktuell})`;
      bild.title = i.aktuell_pfad || "";
      bild.innerHTML = "";
    }
    bild.hidden = !i.laeuft && !letzteAktuell;
    const kz = $("#kat-zahlen");
    kz.innerHTML = Object.entries(s.kategorien).sort((a, b) => b[1] - a[1]).map(([k, n]) =>
      `<div data-kat="${esc(k)}"><span><span class="punkt" style="background:${farbe(k)}"></span>${esc(k)}</span><b>${n.toLocaleString("de")}</b></div>`).join("");
    $$("div", kz).forEach((d) => d.onclick = () => { $("#f-kategorie").value = d.dataset.kat; zeige("suche"); suchen(); });
    if (s.sortieren && s.sortieren.laeuft) {
      $("#s-balken-box").hidden = false;
      $("#s-balken").style.width = s.sortieren.gesamt ? `${(100 * s.sortieren.fertig) / s.sortieren.gesamt}%` : "3%";
    } else if (S.sortiertLaeuft) {
      S.sortiertLaeuft = false;
      $("#s-balken-box").hidden = true;
      zeigeSortierErgebnis(s.sortieren.ergebnis, true);
      ladeProtokolle();
    }
    if (S.indexLief && !i.laeuft) { if (!S.bildSuche) suchen(false, true); ladeFilter(); }
    S.indexLief = i.laeuft;
    $("#e-status").innerHTML = statusText(s);
  } catch (e) { /* Server startet noch */ }
  statusTimer = setTimeout(status, S.indexLief ? 1000 : 2500);
}

function statusText(s) {
  const f = Object.entries(s.modell_fehler || {});
  return [
    `Bilder im Index: <b>${s.gesamt.toLocaleString("de")}</b>`,
    `Noch zu analysieren: <b>${s.offen.toLocaleString("de")}</b>`,
    `exiftool: ${s.exiftool ? "gefunden" : "nicht gefunden (Lesen über Pillow, Einbetten nicht möglich)"}`,
    `Rechnet auf: ${esc(Object.values(s.geraete || {})[0] || "wird beim ersten Indexlauf ermittelt")}`,
    ...f.map(([n, t]) => `<span style="color:var(--schlecht)">Modell ${esc(n)} nicht verfügbar: ${esc(t)}</span>`),
    ...(s.index.fehler || []).slice(-5).map((t) => `<span style="color:var(--warn)">${esc(t)}</span>`),
  ].join("<br>");
}

$("#index-knopf").onclick = async () => {
  try {
    if (S.indexLief) {
      S.wirdAngehalten = true;
      $("#index-knopf").textContent = "Wird angehalten …";
      $("#index-knopf").disabled = true;
      await api("/api/index/stop", { method: "POST" });
      toast("Wird nach dem aktuellen Stapel angehalten …");
    } else {
      await api("/api/index/start", { method: "POST" });
      S.indexLief = true;
      toast("Index wird erstellt. Du kannst währenddessen schon suchen.");
    }
  } catch (e) { S.wirdAngehalten = false; toast(e.message, true); if (/Ordner/.test(e.message)) zeige("einstellungen"); }
  status();
};

/* ------------------------------------------------------------ Suche */

async function ladeFilter() {
  const f = await api("/api/filter");
  const fill = (sel, werte, leer) => {
    const alt = sel.value;
    sel.innerHTML = `<option value="">${leer}</option>` + werte.map((w) => `<option>${esc(w)}</option>`).join("");
    sel.value = alt;
  };
  fill($("#f-kategorie"), f.kategorien, "Alle Kategorien");
  fill($("#f-jahr"), f.jahre, "Alle Jahre");
  const th = $("#f-thema"), altThema = th.value;
  th.innerHTML = `<option value="">Alle Themen</option>` + (f.themen || []).map((t) =>
    `<option value="${esc(t.name)}">${esc(t.name)} (${t.anzahl.toLocaleString("de")})</option>`).join("");
  th.value = altThema;
  fill($("#f-kamera"), f.kameras.filter(Boolean), "Alle Kameras");
}

function suchParameter(offset) {
  const p = new URLSearchParams({
    q: $("#q").value, art: $("#art").value, kategorie: $("#f-kategorie").value, thema: $("#f-thema").value, jahr: $("#f-jahr").value,
    kamera: $("#f-kamera").value, sortierung: $("#f-sort").value, offset, limit: 240,
  });
  if (S.person) p.set("person", S.person);
  if (S.aehnlich) p.set("aehnlich_zu", S.aehnlich);
  return p;
}

let suchNr = 0;
function fotoSucheBeenden() {
  S.bildSuche = null;
  $("#f-foto-chip").hidden = true;
}

async function suchen(weiter = false, behalten = false) {
  if (S.bildSuche && weiter) return;  // Fotosuche liefert alle Treffer auf einmal
  if (!weiter) fotoSucheBeenden();
  const nr = ++suchNr;
  const offset = weiter ? S.bilder.length : 0;
  try {
    const r = await api("/api/bilder?" + suchParameter(offset));
    if (nr !== suchNr) return;
    S.gesamt = r.gesamt;
    S.bilder = weiter ? S.bilder.concat(r.bilder) : r.bilder;
    const da = new Set(S.bilder.map((b) => b.id));
    if (!weiter && behalten) {
      S.gewaehlt = new Set([...S.gewaehlt].filter((i) => da.has(i)));
      if (!da.has(S.fokus)) S.fokus = S.bilder[0]?.id ?? null;
    } else if (!weiter) { S.gewaehlt.clear(); S.fokus = S.bilder[0]?.id ?? null; }
    zeichneErgebnis();
    if (!weiter && S.modus === "liste" && S.fokus) zeigeDetail(S.fokus);
  } catch (e) { toast(e.message, true); }
}

function zeigeTreffer(gesamt, bilder, text) {
  ++suchNr;  // eine noch laufende normale Suche darf das Ergebnis nicht mehr überschreiben
  S.bildSuche = text;
  const c = $("#f-foto-chip");
  c.hidden = false;
  c.innerHTML = `${esc(text)} <button title="Zurück zur normalen Suche">✕</button>`;
  $("button", c).onclick = () => suchen();
  S.gesamt = gesamt; S.bilder = bilder; S.gewaehlt.clear(); S.fokus = bilder[0]?.id ?? null;
  zeichneErgebnis();
  if (S.modus === "liste" && S.fokus) zeigeDetail(S.fokus);
}

function marke(b) {
  const zusatz = (b.zusatz || []).map((k) => `<span class="zusatz-punkt" style="background:${farbe(k)}" title="${esc(k)}"></span>`).join("");
  return `<span class="marke ${b.manuell ? "hand" : ""}" style="box-shadow: inset 3px 0 0 ${farbe(b.kategorie)}">${esc(b.kategorie)}${zusatz}</span>`;
}
function zusatzText(b) {
  return (b.zusatz || []).map((k) => ` · <span style="color:${farbe(k)}">${esc(k)}</span>`).join("");
}

function zeichneErgebnis() {
  $("#treffer").textContent = `${S.gesamt.toLocaleString("de")} Treffer`;
  const mehr = S.bilder.length < S.gesamt ? `<div class="mehr"><button class="knopf" id="mehr-laden">Weitere ${Math.min(240, S.gesamt - S.bilder.length)} laden</button></div>` : "";
  if (S.modus === "raster") {
    $("#raster").innerHTML = S.bilder.map((b) =>
      `<div class="kachel ${S.gewaehlt.has(b.id) ? "gewaehlt" : ""}" data-id="${b.id}" title="${esc(b.name)}${b.beschreibung ? "\n" + esc(b.beschreibung) : ""}">
        <img loading="lazy" src="/api/vorschau/${b.id}" alt="">${marke(b)}</div>`).join("") + mehr;
  } else {
    $("#liste").innerHTML = S.bilder.map((b) =>
      `<div class="eintrag ${S.gewaehlt.has(b.id) ? "gewaehlt" : ""} ${S.fokus === b.id ? "fokus" : ""}" data-id="${b.id}">
        <img loading="lazy" src="/api/vorschau/${b.id}" alt="">
        <div><div class="n">${esc(b.name)}</div><div class="m">${esc(datumText(b.datum))} · <span style="color:${farbe(b.kategorie)}">${esc(b.kategorie)}</span>${zusatzText(b)}${b.tags ? " · " + esc(b.tags) : ""}</div></div></div>`).join("") + mehr;
    if (!S.bilder.length) $("#detail").innerHTML = `<div class="leer">Keine Treffer</div>`;
  }
  const m = $("#mehr-laden");
  if (m) m.onclick = () => suchen(true);
  if (!S.bilder.length && S.modus === "raster") $("#raster").innerHTML = `<div class="mehr hinweis">Keine Bilder gefunden. ${S.gesamt === 0 && !$("#q").value ? "Lege unter Einstellungen einen Bilder-Ordner an und starte den Index." : ""}</div>`;
  auswahlLeiste();
}

function groesseText(n) {
  n = n || 0;
  if (n < 1024 * 1024) return `${Math.max(1, Math.round(n / 1024)).toLocaleString("de")} KB`;
  return `${(n / 1048576).toLocaleString("de", { maximumFractionDigits: 1 })} MB`;
}

function datumText(d) {
  if (!d) return "ohne Datum";
  const x = new Date(d.replace(" ", "T"));
  return isNaN(x) ? d : x.toLocaleString("de-DE", { dateStyle: "medium", timeStyle: "short" });
}

function waehle(id, ev) {
  const ids = S.bilder.map((b) => b.id);
  if (ev.shiftKey && S.letzter != null) {
    const [a, b] = [ids.indexOf(S.letzter), ids.indexOf(id)].sort((x, y) => x - y);
    ids.slice(a, b + 1).forEach((i) => S.gewaehlt.add(i));
  } else if (ev.ctrlKey || ev.metaKey) {
    S.gewaehlt.has(id) ? S.gewaehlt.delete(id) : S.gewaehlt.add(id);
  } else {
    S.gewaehlt = new Set([id]);
  }
  S.letzter = id;
  S.fokus = id;
  aktualisiereAuswahl();
  if (S.modus === "liste") zeigeDetail(id);
}

function aktualisiereAuswahl() {
  $$(".kachel, .eintrag").forEach((el) => {
    const id = +el.dataset.id;
    el.classList.toggle("gewaehlt", S.gewaehlt.has(id));
    el.classList.toggle("fokus", S.fokus === id);
  });
  auswahlLeiste();
}

function auswahlLeiste() {
  const n = S.gewaehlt.size;
  $("#auswahl-leiste").hidden = n === 0;
  $("#auswahl-zahl").textContent = `${n} ausgewählt`;
  $("#auswahl-kategorien").innerHTML = S.kategorien.map((k) =>
    `<button class="kat-knopf" data-kat="${esc(k)}" style="border-color:${farbe(k)}">${esc(k)}</button>`).join("");
  $$("#auswahl-kategorien button").forEach((b) => b.onclick = () => kategorieSetzen([...S.gewaehlt], b.dataset.kat));
  // Zusatzkategorien: Haben alle markierten Bilder sie schon, nimmt ein Klick sie wieder weg
  const gewaehlt = S.bilder.filter((b) => S.gewaehlt.has(b.id));
  $("#auswahl-zusatz").hidden = !S.zusatz.length;
  $("#auswahl-zusatz").innerHTML = S.zusatz.map((k) => {
    const alle = gewaehlt.length > 0 && gewaehlt.every((b) => (b.zusatz || []).includes(k));
    const einige = !alle && gewaehlt.some((b) => (b.zusatz || []).includes(k));
    return `<button class="kat-knopf zusatz ${alle ? "aktiv" : ""} ${einige ? "teils" : ""}" data-kat="${esc(k)}" data-an="${alle ? 0 : 1}"
      title="${alle ? "Bei allen markierten Bildern entfernen" : "Allen markierten Bildern hinzufügen"}"
      style="${alle ? `background:${farbe(k)}` : `border-color:${farbe(k)}`}">${alle ? "✓ " : "+ "}${esc(k)}</button>`;
  }).join("");
  $$("#auswahl-zusatz button").forEach((b) => b.onclick = () => zusatzSetzen([...S.gewaehlt], b.dataset.kat, b.dataset.an === "1"));
}

async function zusatzSetzen(ids, kat, an) {
  try {
    await api("/api/bilder/zusatz", { json: { ids, kategorie: kat, an } });
    S.bilder.forEach((b) => {
      if (!ids.includes(b.id)) return;
      const z = new Set(b.zusatz || []);
      if (an) z.add(kat); else z.delete(kat);
      b.zusatz = S.zusatz.filter((k) => z.has(k));
    });
    zeichneErgebnis();
    if (S.modus === "liste" && S.fokus) zeigeDetail(S.fokus);
    status();
    toast(an ? `${ids.length} Bild(er) + ${kat}` : `${ids.length} Bild(er) ohne ${kat}`);
  } catch (e) { toast(e.message, true); }
}

async function neuBewerten(ids) {
  const w = await frage("Neu bewerten",
    `<p>${ids.length.toLocaleString("de-DE")} Bild(er) neu bewerten.</p>
     <p><b>Neu einordnen</b> wendet Themen, Uhrzeit und Regeln erneut an. Das geht in Sekunden.</p>
     <p><b>Komplett neu analysieren</b> erkennt Bildinhalt, Gesichter und Text noch einmal. Das dauert je Bild etwas.</p>
     <p>Kategorien, die du von Hand gesetzt hast, bleiben in beiden Fällen erhalten.</p>`,
    "Komplett neu analysieren", "Neu einordnen");
  if (w === false) return;
  try {
    if (w === true) {
      const r = await api("/api/bilder/neu-analysieren", { json: { ids } });
      toast(r.gestartet ? `${r.vorgemerkt} Bild(er) werden neu analysiert …` : `${r.vorgemerkt} Bild(er) vorgemerkt. Sie kommen im nächsten Indexlauf dran.`);
    } else {
      await api("/api/einordnen", { method: "POST" });
      toast("Neu eingeordnet");
      suchen(false, true);
    }
    status();
  } catch (e) { toast(e.message, true); }
}
$("#auswahl-neu").onclick = () => neuBewerten([...S.gewaehlt]);

$("#raster").addEventListener("click", (e) => { const k = e.target.closest(".kachel"); if (k) waehle(+k.dataset.id, e); });
$("#raster").addEventListener("dblclick", (e) => { const k = e.target.closest(".kachel"); if (k) { setzeModus("liste"); S.fokus = +k.dataset.id; zeichneErgebnis(); zeigeDetail(S.fokus); scrolleZuFokus(); } });
$("#liste").addEventListener("click", (e) => { const k = e.target.closest(".eintrag"); if (k) waehle(+k.dataset.id, e); });

function scrolleZuFokus() { const el = $(`.eintrag[data-id="${S.fokus}"]`); if (el) el.scrollIntoView({ block: "nearest" }); }

async function kategorieSetzen(ids, kat) {
  try {
    await api("/api/bilder/kategorie", { json: { ids, kategorie: kat } });
    S.bilder.forEach((b) => { if (ids.includes(b.id)) { b.kategorie = kat || b.kategorie; b.manuell = !!kat; } });
    zeichneErgebnis();
    if (S.modus === "liste" && S.fokus) zeigeDetail(S.fokus);
    toast(kat ? `${ids.length} Bild(er) → ${kat}` : `${ids.length} Bild(er) wieder automatisch`);
    if (!kat) {
      if (S.bildSuche) {  // Fotosuche nicht verwerfen, nur die betroffenen Bilder neu laden
        for (const id of ids) {
          const b = await api(`/api/bild/${id}`);
          const x = S.bilder.find((y) => y.id === id);
          if (x) { x.kategorie = b.kategorie_auto || "Unklar"; x.manuell = false; }
        }
        zeichneErgebnis();
        if (S.modus === "liste" && S.fokus) zeigeDetail(S.fokus);
      } else suchen(false, true);
    }
  } catch (e) { toast(e.message, true); }
}
$("#auswahl-auto").onclick = () => kategorieSetzen([...S.gewaehlt], null);
$("#auswahl-weg").onclick = () => { S.gewaehlt.clear(); aktualisiereAuswahl(); };
$("#auswahl-kopieren").onclick = () => kopieren();

async function kopieren() {
  const ids = S.gewaehlt.size ? [...S.gewaehlt] : (S.fokus ? [S.fokus] : []);
  if (!ids.length) return;
  try {
    const r = await api("/api/bilder/kopieren", { json: { ids } });
    toast(`${r.kopiert} Datei(en) kopiert. Im Explorer mit Strg+V einfügen.`);
  } catch (e) { toast(e.message, true); }
}

async function zeigeDetail(id) {
  const d = $("#detail");
  try {
    const b = await api(`/api/bild/${id}`);
    if (S.fokus !== id) return;
    const kat = b.kategorie_manuell || b.kategorie_auto || "Unklar";
    const boxen = (b.gesichter || []).map((g) =>
      `<div class="box" data-g="${g.id}" style="left:${g.x * 100}%;top:${g.y * 100}%;width:${g.w * 100}%;height:${g.h * 100}%">${g.name ? `<span>${esc(g.name)}</span>` : ""}</div>`).join("");
    d.innerHTML = `
      <div class="detail-bild"><div style="position:relative;display:inline-block;max-width:100%" id="bildrahmen">
        <img id="detail-img" src="/api/ansicht/${id}?kante=1600" alt="">${boxen}</div></div>
      <div class="detail-info">
        <div class="zeile zwischen"><h3>${esc(b.name)}</h3>
          <div class="zeile"><button class="knopf leicht" id="d-aehnlich">Ähnliche</button><button class="knopf leicht" id="d-explorer">Im Explorer</button><button class="knopf" id="d-kopieren">Kopieren</button></div></div>
        <div class="kat-knoepfe">${S.kategorien.map((k) => `<button class="kat-knopf ${k === kat ? "aktiv" : ""}" data-kat="${esc(k)}" style="${k === kat ? `background:${farbe(k)}` : `border-color:${farbe(k)}`}">${esc(k)}</button>`).join("")}
          ${b.kategorie_manuell ? `<button class="kat-knopf" data-kat="">↺ automatisch</button>` : ""}</div>
        ${S.zusatz.length ? `<div class="kat-knoepfe">${S.zusatz.map((k) => {
          const an = (b.zusatz || []).includes(k);
          return `<button class="kat-knopf zusatz ${an ? "aktiv" : ""}" data-zusatz="${esc(k)}" data-an="${an ? 0 : 1}"
            title="${k in (b.zusatz_hand || {}) ? "von Hand gesetzt" : "automatisch"}"
            style="${an ? `background:${farbe(k)}` : `border-color:${farbe(k)}`}">${an ? "✓ " : "+ "}${esc(k)}</button>`;
        }).join("")}</div>` : ""}
        ${b.beschreibung ? `<div>${esc(b.beschreibung)}</div>` : ""}
        ${b.tags ? `<div class="tags">${b.tags.split(",").map((t) => `<span class="tag">${esc(t.trim())}</span>`).join("")}</div>` : ""}
        <dl class="meta">
          <dt>Aufgenommen</dt><dd>${esc(datumText(b.datum))} <span class="hinweis">(${esc(b.datum_quelle || "")})</span></dd>
          <dt>Kamera</dt><dd>${esc([b.hersteller, b.modell].filter(Boolean).join(" ") || "unbekannt")}</dd>
          ${b.gps_lat != null ? `<dt>Ort</dt><dd>${b.gps_lat.toFixed(5)}, ${b.gps_lon.toFixed(5)}</dd>` : ""}
          <dt>Größe</dt><dd>${b.breite || "?"} × ${b.hoehe || "?"} · ${groesseText(b.groesse)}</dd>
          <dt>Personen</dt><dd>${(b.gesichter || []).length ? b.gesichter.map((g) => esc(g.name || "unbenannt")).join(", ") : "keine erkannt"}</dd>
          <dt>Einordnung</dt><dd>${esc(b.gruende || "–")}</dd>
          <dt>Pfad</dt><dd>${esc(b.pfad)}</dd>
        </dl>
        ${b.ocr_text ? `<div><div class="hinweis">Erkannter Text</div><div class="ocr">${esc(b.ocr_text)}</div></div>` : ""}
      </div>`;
    $("#detail-img").onclick = () => { $("#vollbild-img").src = `/api/ansicht/${id}?kante=3000`; $("#vollbild").hidden = false; };
    $("#d-aehnlich").onclick = () => aehnlicheZeigen(id, b.name);
    $("#d-explorer").onclick = () => api(`/api/bilder/${id}/explorer`, { method: "POST" }).catch((e) => toast(e.message, true));
    $("#d-kopieren").onclick = () => { S.gewaehlt = new Set([id]); aktualisiereAuswahl(); kopieren(); };
    $$(".detail-info .kat-knopf[data-kat]").forEach((k) => k.onclick = () => kategorieSetzen([id], k.dataset.kat || null));
    $$(".detail-info .kat-knopf[data-zusatz]").forEach((k) => k.onclick = () => zusatzSetzen([id], k.dataset.zusatz, k.dataset.an === "1"));
  } catch (e) { d.innerHTML = `<div class="leer">${esc(e.message)}</div>`; }
}

function aehnlicheZeigen(id, name) {
  S.aehnlich = id;
  const c = $("#f-aehnlich-chip");
  c.hidden = false;
  c.innerHTML = `Ähnlich wie ${esc(name)} <button title="Filter entfernen">✕</button>`;
  $("button", c).onclick = () => { S.aehnlich = null; c.hidden = true; suchen(); };
  zeige("suche");
  suchen();
}

function personFilter(id, name) {
  S.person = id;
  const c = $("#f-person-chip");
  c.hidden = false;
  c.innerHTML = `Person: ${esc(name || "unbenannt")} <button title="Filter entfernen">✕</button>`;
  $("button", c).onclick = () => { S.person = null; c.hidden = true; suchen(); };
  zeige("suche");
  suchen();
}

function setzeModus(m) {
  S.modus = m;
  localStorage.setItem("modus", m);
  $("#ergebnis").className = "ergebnis " + m;
  $$("#ansicht-umschalter button").forEach((b) => b.classList.toggle("aktiv", b.dataset.modus === m));
}
$$("#ansicht-umschalter button").forEach((b) => b.onclick = () => { setzeModus(b.dataset.modus); zeichneErgebnis(); if (S.modus === "liste" && S.fokus) zeigeDetail(S.fokus); });

let tipp;
$("#q").addEventListener("input", () => { clearTimeout(tipp); tipp = setTimeout(() => suchen(), 350); });
$("#q").addEventListener("keydown", (e) => { if (e.key === "Enter") { clearTimeout(tipp); suchen(); } });
["#art", "#f-kategorie", "#f-thema", "#f-jahr", "#f-kamera", "#f-sort"].forEach((s) => $(s).addEventListener("change", () => suchen()));
$("#groesse").addEventListener("input", (e) => document.documentElement.style.setProperty("--kachel", e.target.value + "px"));

$("#bild-suche").addEventListener("change", async (e) => {
  const f = e.target.files[0];
  if (!f) return;
  const fd = new FormData();
  fd.append("datei", f);
  toast("Suche ähnliche Bilder …");
  try {
    const r = await api("/api/suche-mit-bild", { method: "POST", body: fd });
    zeigeTreffer(r.gesamt, r.bilder, `Ähnlich wie ${f.name}`);
    toast(`${r.gesamt} ähnliche Bilder gefunden`);
  } catch (err) { toast(err.message, true); }
  e.target.value = "";
});

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") {
    if (!$("#vollbild").hidden) { $("#vollbild").hidden = true; return; }
    if (!$("#dialog").hidden) { $("#dialog-nein").click(); return; }
  }
  if (!$("#ansicht-suche").classList.contains("aktiv")) return;
  if (!$("#vollbild").hidden || !$("#dialog").hidden) return;  // nichts darunter verändern
  const imFeld = ["INPUT", "SELECT", "TEXTAREA"].includes(document.activeElement.tagName);
  if (imFeld) return;
  // markierten Text (Pfad, erkannter Text ...) normal kopieren statt der Bilddateien
  const textMarkiert = String(window.getSelection() || "").trim() !== "";
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "c" && !textMarkiert) { e.preventDefault(); kopieren(); }
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "a") { e.preventDefault(); S.bilder.forEach((b) => S.gewaehlt.add(b.id)); aktualisiereAuswahl(); }
  if (["ArrowDown", "ArrowUp", "ArrowLeft", "ArrowRight"].includes(e.key) && S.bilder.length) {
    e.preventDefault();
    const ids = S.bilder.map((b) => b.id);
    let i = Math.max(0, ids.indexOf(S.fokus));
    const spalten = S.modus === "raster" ? Math.max(1, Math.round($("#raster").clientWidth / ($(".kachel")?.offsetWidth + 8 || 190))) : 1;
    const schritt = { ArrowDown: spalten, ArrowUp: -spalten, ArrowRight: 1, ArrowLeft: -1 }[e.key];
    i = Math.min(ids.length - 1, Math.max(0, i + schritt));
    waehle(ids[i], e);
    const el = $(`[data-id="${ids[i]}"]`);
    if (el) el.scrollIntoView({ block: "nearest" });
  }
  if (e.key === "Enter" && S.fokus && S.modus === "raster") { setzeModus("liste"); zeichneErgebnis(); zeigeDetail(S.fokus); scrolleZuFokus(); }
});
$("#vollbild").onclick = () => { $("#vollbild").hidden = true; };
// Eine Datei, die neben die Ablagefläche fällt, würde sonst das Programmfenster durch das Bild ersetzen
["dragover", "drop"].forEach((art) => window.addEventListener(art, (e) => {
  if (!(e.target instanceof Element) || !e.target.closest("#ablage")) e.preventDefault();
}));

/* ------------------------------------------------------------ Themen & Kategorien */

let themenDaten = null;
async function ladeThemen() {
  themenDaten = await api("/api/themen");
  zeichneThemen();
}

function zeichneThemen() {
  const kats = themenDaten.kategorien;
  $("#kat-liste").innerHTML = kats.map((k, i) =>
    `<div class="kat-eintrag ${k.zusatz ? "zusatz" : ""}"><input type="color" value="${esc(k.farbe || FARBEN[i % FARBEN.length])}" data-i="${i}"><span>${esc(k.name)}</span>
      <label class="kat-art" title="Zusatzkategorie: schließt andere Kategorien nicht aus, ein Bild kann mehrere davon haben"><input type="checkbox" data-zusatz="${i}" ${k.zusatz ? "checked" : ""}>zusätzlich</label>
      <button data-weg="${i}" title="Kategorie entfernen">✕</button></div>`).join("");
  $$("#kat-liste input[type=color]").forEach((c) => c.oninput = () => { kats[c.dataset.i].farbe = c.value; });
  $$("#kat-liste [data-zusatz]").forEach((c) => c.onchange = () => { kats[c.dataset.zusatz].zusatz = c.checked; zeichneThemen(); });
  $$("#kat-liste [data-weg]").forEach((b) => b.onclick = () => { kats.splice(+b.dataset.weg, 1); zeichneThemen(); });
  const opt = (k, wert) => `<option value="${esc(k.name)}" ${k.name === wert ? "selected" : ""}>${esc(k.name)}</option>`;
  const optionen = (wert) => `<option value="">nur Schlagwort</option>`
    + `<optgroup label="Kategorie">${kats.filter((k) => !k.zusatz).map((k) => opt(k, wert)).join("")}</optgroup>`
    + (kats.some((k) => k.zusatz) ? `<optgroup label="Zusätzlich">${kats.filter((k) => k.zusatz).map((k) => opt(k, wert)).join("")}</optgroup>` : "");
  $("#themen-gitter").innerHTML = themenDaten.themen.map((t, i) =>
    `<div class="thema" style="border-left-color:${farbeAus(kats, t.kategorie)}">
      <div class="kopf"><b>${esc(t.name)}</b><span class="anzahl" data-zeige="${i}" title="Bilder zu diesem Thema anzeigen">${t.anzahl.toLocaleString("de")} Bilder</span></div>
      <input value="${esc(t.prompt || "")}" data-prompt="${i}" placeholder="Beschreibung für das Bildmodell">
      <div class="zeile zwischen"><select data-kat="${i}">${optionen(t.kategorie)}</select><button class="loeschen" data-weg="${i}" title="Thema löschen">Löschen</button></div>
      ${t.beispiele.length ? `<div class="bsp">${t.beispiele.map((id) => `<img loading="lazy" src="/api/vorschau/${id}" alt="">`).join("")}</div>` : ""}
    </div>`).join("");
  $$("[data-prompt]").forEach((el) => el.onchange = () => { themenDaten.themen[el.dataset.prompt].prompt = el.value; });
  $$("[data-kat]", $("#themen-gitter")).forEach((el) => el.onchange = () => { themenDaten.themen[el.dataset.kat].kategorie = el.value; zeichneThemen(); });
  $$(".thema [data-weg]").forEach((el) => el.onclick = () => { themenDaten.themen.splice(+el.dataset.weg, 1); zeichneThemen(); });
  $$("[data-zeige]").forEach((el) => el.onclick = async () => {
    const t = themenDaten.themen[el.dataset.zeige];
    await ladeFilter().catch(() => {});
    $("#q").value = ""; $("#f-kategorie").value = "";
    $("#f-thema").value = t.name;
    if ($("#f-thema").value !== t.name) { $("#q").value = t.name; $("#art").value = "bedeutung"; }  // noch keinem Bild zugeordnet
    zeige("suche"); suchen();
  });
}
function farbeAus(kats, name) { const i = kats.findIndex((k) => k.name === name); return i < 0 ? "#2a3042" : (kats[i].farbe || FARBEN[i % FARBEN.length]); }

$("#kat-neu").onclick = () => {
  const n = $("#neue-kat").value.trim();
  if (!n || themenDaten.kategorien.some((k) => k.name === n)) return;
  const neu = { name: n, farbe: FARBEN[themenDaten.kategorien.length % FARBEN.length] };
  if ($("#neue-kat-zusatz").checked) neu.zusatz = true;
  themenDaten.kategorien.push(neu);
  $("#neue-kat").value = "";
  zeichneThemen();
};
$("#thema-neu").onclick = () => {
  const n = $("#neues-thema").value.trim();
  if (!n) return;
  themenDaten.themen.unshift({ name: n, prompt: $("#neues-thema-prompt").value.trim() || n, kategorie: "", anzahl: 0, beispiele: [] });
  $("#neues-thema").value = $("#neues-thema-prompt").value = "";
  zeichneThemen();
};
$("#themen-speichern").onclick = async () => {
  const themen = themenDaten.themen.map(({ name, prompt, kategorie }) => ({ name, prompt, kategorie }));
  try {
    await api("/api/einstellungen", { json: { themen, kategorien: themenDaten.kategorien } });
    await ladeGrundlagen();
    toast("Gespeichert. Bilder werden neu eingeordnet …");
    setTimeout(() => { ladeThemen(); ladeFilter().catch(() => {}); suchen(); }, 1500);
  } catch (e) { toast(e.message, true); }
};

/* ------------------------------------------------------------ Personen */

const personenWahl = new Set();
async function ladePersonen() {
  const r = await api("/api/personen");
  personenWahl.clear();
  $("#personen-zusammen").disabled = true;
  $("#personen-gitter").innerHTML = r.personen.length ? r.personen.map((p) =>
    `<div class="person" data-id="${p.id}"><input type="checkbox" class="wahl" title="Zum Zusammenführen auswählen">
      <img src="/api/gesicht/${p.titel_gesicht}" alt="" title="Alle Bilder dieser Person zeigen">
      <input value="${esc(p.name || "")}" placeholder="Name eingeben">
      <small>${p.bilder} Bilder</small></div>`).join("")
    : `<div class="hinweis">Noch keine Gesichter gefunden. Sie werden beim Erstellen des Index erkannt.</div>`;
  $$(".person").forEach((el) => {
    const id = +el.dataset.id;
    const name = $("input:not(.wahl)", el);
    $("img", el).onclick = () => personFilter(id, name.value);
    name.onchange = async () => {
      try { await api(`/api/personen/${id}/name`, { json: { name: name.value } }); toast("Name gespeichert"); await ladePersonen(); }
      catch (e) { toast(e.message, true); }
    };
    $(".wahl", el).onchange = (e) => {
      e.target.checked ? personenWahl.add(id) : personenWahl.delete(id);
      el.classList.toggle("gewaehlt", e.target.checked);
      $("#personen-zusammen").disabled = personenWahl.size < 2;
    };
  });
}
$("#personen-gruppieren").onclick = async () => {
  try { const r = await api("/api/personen/neu-gruppieren", { method: "POST" }); toast(`${r.zugeordnet} Gesichter zugeordnet`); await ladePersonen(); }
  catch (e) { toast(e.message, true); }
};
$("#personen-zusammen").onclick = async () => {
  const ids = [...personenWahl];
  try {
    await api("/api/personen/zusammenfuehren", { json: { ziel: ids[0], quellen: ids.slice(1) } });
    toast("Zusammengeführt");
    await ladePersonen();
  } catch (e) { toast(e.message, true); }
};
async function personSuchen(datei) {
  const fd = new FormData();
  fd.append("datei", datei);
  toast("Suche nach dieser Person …");
  try {
    const r = await api("/api/personensuche", { method: "POST", body: fd });
    if (!r.gesichter.length) { toast("Auf dem Foto wurde kein Gesicht gefunden.", true); return; }
    zeige("suche");
    zeigeTreffer(r.gesamt, r.bilder, `Person aus ${datei.name || "Foto"}`);
    toast(`${r.gesamt} Bilder mit dieser Person gefunden${r.gesichter.length > 1 ? " (größtes Gesicht auf dem Foto verwendet)" : ""}`);
  } catch (e) { toast(e.message, true); }
}
$("#person-foto").onchange = (e) => { if (e.target.files[0]) personSuchen(e.target.files[0]); e.target.value = ""; };
const ablage = $("#ablage");
ablage.addEventListener("dragover", (e) => { e.preventDefault(); ablage.classList.add("drueber"); });
ablage.addEventListener("dragleave", () => ablage.classList.remove("drueber"));
ablage.addEventListener("drop", (e) => { e.preventDefault(); ablage.classList.remove("drueber"); const f = e.dataTransfer.files[0]; if (f) personSuchen(f); });

/* ------------------------------------------------------------ Sortieren */

function sortierEinstellungen() {
  return {
    modus: $("input[name=modus]:checked").value, ziel: $("#s-ziel").value.trim(), ordner: $("#s-ordner").value,
    umbenennen: $("#s-umbenennen").checked, name: $("#s-name").value, tags: $("#s-tags").value,
  };
}
function sortierNur() { return $$("#s-nur input:checked").map((i) => i.value); }

function sortierFelder() {
  const modus = $("input[name=modus]:checked").value;
  $("#s-ziel-feld").hidden = $("#s-ordner-feld").hidden = modus === "nur_taggen";
  $("#s-name-feld").hidden = !$("#s-umbenennen").checked;
}
$$("input[name=modus]").forEach((r) => r.onchange = sortierFelder);
$("#s-umbenennen").onchange = sortierFelder;

async function ladeSortieren() {
  const s = S.einst.sortieren;
  $(`input[name=modus][value=${s.modus}]`).checked = true;
  $("#s-ziel").value = s.ziel || "";
  $("#s-ordner").value = s.ordner;
  $("#s-umbenennen").checked = !!s.umbenennen;
  $("#s-name").value = s.name;
  $("#s-tags").value = s.tags;
  $("#s-nur").innerHTML = [...S.kategorien, "Unklar"].map((k) => `<label><input type="checkbox" value="${esc(k)}" ${k !== "Unklar" ? "checked" : ""}>${esc(k)}</label>`).join("")
    + S.zusatz.map((k) => `<label title="Zusatzkategorie: Bilder damit zusätzlich mitnehmen"><input type="checkbox" value="${esc(k)}">+ ${esc(k)}</label>`).join("");
  sortierFelder();
  ladeProtokolle();
}

async function ordnerWaehlen(feld) {
  try { const r = await api("/api/ordner-waehlen", { method: "POST" }); if (r.pfad) $(feld).value = r.pfad; }
  catch (e) { toast(e.message, true); }
}
$("#s-ziel-waehlen").onclick = () => ordnerWaehlen("#s-ziel");

function zeigeSortierErgebnis(r, echt) {
  if (!r) return;
  const z = r.zaehler || {};
  const namen = { ok: echt ? "erledigt" : "geplant", duplikat: "schon vorhanden", fehlt: "fehlen", fehler: "Fehler", unveraendert: "unverändert" };
  $("#s-ergebnis").innerHTML = `
    <div class="zaehler">${Object.entries(z).filter(([, v]) => v).map(([k, v]) => `<div><b>${v.toLocaleString("de")}</b>${namen[k] || k}</div>`).join("") || "<div>Nichts zu tun</div>"}</div>
    ${(r.fehler || []).length ? `<div class="vorschau-liste" style="color:var(--schlecht)">${r.fehler.map((f) => `<div>${esc(f)}</div>`).join("")}</div>` : ""}
    ${(r.vorschau || []).length ? `<div class="vorschau-liste">${r.vorschau.slice(0, 300).map((v) => `<div>${esc(v.von)}<br><span class="pfeil">→</span> ${esc(v.nach)}${v.art === "duplikat" ? " <i>(schon vorhanden)</i>" : ""}</div>`).join("")}</div>` : ""}`;
}

$("#s-vorschau").onclick = async () => {
  try {
    const r = await api("/api/sortieren/vorschau", { json: { einstellungen: sortierEinstellungen(), nur: sortierNur() } });
    zeigeSortierErgebnis(r, false);
  } catch (e) { toast(e.message, true); }
};
$("#s-ausfuehren").onclick = async () => {
  const s = sortierEinstellungen();
  try {
    const v = await api("/api/sortieren/vorschau", { json: { einstellungen: s, nur: sortierNur() } });
    zeigeSortierErgebnis(v, false);
    const wort = { kopieren: "kopiert", verschieben: "verschoben", nur_taggen: s.umbenennen ? "umbenannt/getaggt" : "getaggt" }[s.modus];
    const ok = await frage("Wirklich ausführen?", `<b>${v.zaehler.ok.toLocaleString("de")}</b> Dateien werden ${wort}.<br>Der Lauf kann anschließend unter „Bisherige Läufe“ rückgängig gemacht werden.`, "Ausführen");
    if (!ok) return;
    await api("/api/sortieren/ausfuehren", { json: { einstellungen: s, nur: sortierNur() } });
    S.sortiertLaeuft = true;
    $("#s-balken-box").hidden = false;
    toast("Läuft …");
  } catch (e) { toast(e.message, true); }
};

async function ladeProtokolle() {
  const r = await api("/api/protokolle");
  $("#protokolle").innerHTML = r.protokolle.length ? r.protokolle.map((p) =>
    `<div class="protokoll ${p.rueckgaengig ? "zurueck" : ""}"><div><b>${esc(datumText(p.zeit))}</b><br><span class="hinweis">${esc(p.beschreibung)}</span></div>
      ${p.rueckgaengig ? "<span class='hinweis'>rückgängig gemacht</span>" : `<button class="knopf leicht" data-p="${p.id}">Rückgängig</button>`}</div>`).join("")
    : `<div class="hinweis">Noch keine Läufe.</div>`;
  $$("#protokolle [data-p]").forEach((b) => b.onclick = async () => {
    try {
      const v = await api(`/api/protokolle/${b.dataset.p}/rueckgaengig`, { method: "POST" });
      const probleme = v.probleme || [];
      const ok = await frage("Lauf rückgängig machen?", `${v.anzahl} Schritte werden zurückgenommen.${probleme.length ? `<br><br>${probleme.length} Einträge werden übersprungen:<br>${probleme.slice(0, 10).map(esc).join("<br>")}` : ""}`, "Rückgängig machen");
      if (!ok) return;
      b.disabled = true;  // kein doppelter Klick während des Laufs
      await api(`/api/protokolle/${b.dataset.p}/rueckgaengig?echt=true`, { method: "POST" });
      toast("Rückgängig gemacht");
      ladeProtokolle();
      suchen();
    } catch (e) { b.disabled = false; toast(e.message, true); }
  });
}

/* ------------------------------------------------------------ Einstellungen */

const TAGE = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"];
let quellen = [];
function ladeEinstellungen() {
  const e = S.einst;
  quellen = [...e.quellen];
  zeichneQuellen();
  const az = e.arbeitszeit;
  $("#e-az-aktiv").checked = az.aktiv;
  $("#e-tage").innerHTML = TAGE.map((t, i) => `<button data-t="${i + 1}" class="${az.wochentage.includes(i + 1) ? "an" : ""}">${t}</button>`).join("");
  $$("#e-tage button").forEach((b) => b.onclick = () => b.classList.toggle("an"));
  $("#e-von").value = az.von;
  $("#e-bis").value = az.bis;
  const opt = (wert) => `<option value="">nichts</option>` + S.kategorien.map((k) => `<option ${k === wert ? "selected" : ""}>${esc(k)}</option>`).join("");
  $("#e-az-kat").innerHTML = opt(az.kategorie);
  $("#e-fz-kat").innerHTML = opt(az.kategorie_freizeit);
  $("#e-beschreibung").checked = e.beschreibung.aktiv;
  $("#e-gesichter").checked = e.gesichter.aktiv;
  $("#e-ocr").value = e.ocr.modus;
  $("#e-beschl").value = e.beschleunigung || "auto";
  $("#e-schwelle").value = e.schwelle;
  $("#e-schwelle-wert").textContent = e.schwelle;
}
$("#e-schwelle").oninput = (ev) => { $("#e-schwelle-wert").textContent = ev.target.value; };
function zeichneQuellen() {
  $("#e-quellen").innerHTML = quellen.length ? quellen.map((q, i) => `<div class="quelle ${(S.quellenFehlen || []).includes(q) ? "fehlt" : ""}"><span>${esc(q)}</span>${(S.quellenFehlen || []).includes(q) ? `<small>nicht gefunden</small>` : ""}<button data-i="${i}" title="Entfernen">✕</button></div>`).join("")
    : `<div class="hinweis">Noch kein Ordner. Füge den Ordner deines Bilder-Backups hinzu.</div>`;
  $$("#e-quellen button").forEach((b) => b.onclick = () => quelleEntfernen(quellen[+b.dataset.i]));
}
// Beim Entfernen fragen, ob die Bilder dieses Ordners auch aus dem Index (und damit aus Suche und Sortieren) sollen
async function quelleEntfernen(pfad) {
  try {
    const { anzahl } = await api(`/api/quellen/anzahl?pfad=${encodeURIComponent(pfad)}`);
    let ausIndex = false;
    if (anzahl > 0) {
      const w = await frage("Bilder-Ordner entfernen",
        `<p><b>${esc(pfad)}</b></p><p>Im Index sind ${anzahl.toLocaleString("de-DE")} Bilder aus diesem Ordner.
         Sollen sie auch aus dem Index entfernt werden? Dann tauchen sie in Suche und Sortieren nicht mehr auf.</p>
         <p>Die Dateien selbst werden nicht angefasst. Behältst du sie im Index, müssen sie beim erneuten
         Hinzufügen nicht neu analysiert werden.</p>`,
        "Aus dem Index entfernen", "Im Index behalten");
      if (w === false) return;
      ausIndex = w === true;
    }
    const erg = await api("/api/quellen/entfernen", { json: { pfad, aus_index: ausIndex } });
    S.einst = erg.einstellungen;
    quellen = [...S.einst.quellen];
    await status();
    zeichneQuellen();
    toast(ausIndex ? `Ordner entfernt, ${erg.entfernt.toLocaleString("de-DE")} Bilder aus dem Index genommen` : "Ordner entfernt");
  } catch (e) { toast(e.message, true); }
}
// Bilder-Ordner werden sofort gespeichert, sonst meldet "Index erstellen" trotzdem "kein Ordner"
async function quellenSpeichern() {
  try {
    S.einst = await api("/api/einstellungen", { json: { quellen } });
    await status();
    zeichneQuellen();
    const fehlt = quellen.filter((q) => S.quellenFehlen.includes(q));
    if (fehlt.length) toast(`Ordner nicht gefunden: ${fehlt.join(", ")}`, true); else toast("Bilder-Ordner gespeichert");
  }
  catch (e) { toast(e.message, true); }
}
function quelleHinzufuegen() {
  const p = $("#e-quelle-neu").value.trim();
  $("#e-quelle-neu").value = "";
  if (!p || quellen.includes(p)) return;
  quellen.push(p);
  zeichneQuellen();
  quellenSpeichern();
}
$("#e-quelle-waehlen").onclick = async () => { await ordnerWaehlen("#e-quelle-neu"); quelleHinzufuegen(); };
$("#e-quelle-add").onclick = quelleHinzufuegen;
$("#e-quelle-neu").addEventListener("keydown", (e) => { if (e.key === "Enter") quelleHinzufuegen(); });
$("#e-speichern").onclick = async () => {
  const neu = {
    quellen,
    arbeitszeit: {
      aktiv: $("#e-az-aktiv").checked, wochentage: $$("#e-tage button.an").map((b) => +b.dataset.t),
      von: $("#e-von").value || "07:30", bis: $("#e-bis").value || "17:30",
      kategorie: $("#e-az-kat").value, kategorie_freizeit: $("#e-fz-kat").value,
    },
    beschreibung: { aktiv: $("#e-beschreibung").checked },
    gesichter: { aktiv: $("#e-gesichter").checked },
    ocr: { modus: $("#e-ocr").value },
    beschleunigung: $("#e-beschl").value,
    schwelle: +$("#e-schwelle").value,
  };
  try {
    S.einst = await api("/api/einstellungen", { json: neu });
    toast("Gespeichert");
  } catch (e) { toast(e.message, true); }
};

/* ------------------------------------------------------------ Start */

async function ladeGrundlagen() {
  S.einst = await api("/api/einstellungen");
  // Hauptkategorien: genau eine pro Bild (Ordner beim Sortieren). Zusatzkategorien: beliebig viele pro Bild.
  S.kategorien = S.einst.kategorien.filter((k) => !k.zusatz).map((k) => k.name);
  S.zusatz = S.einst.kategorien.filter((k) => k.zusatz).map((k) => k.name);
  S.farben = { Unklar: "#64748b" };
  S.einst.kategorien.forEach((k, i) => { S.farben[k.name] = k.farbe || FARBEN[i % FARBEN.length]; });
}

(async function start() {
  setzeModus(S.modus);
  for (let i = 0; i < 50; i++) {
    try { await ladeGrundlagen(); break; } catch { await new Promise((r) => setTimeout(r, 200)); }
  }
  if (!S.einst) { toast("Verbindung zum Programm fehlgeschlagen. Bitte neu starten.", true); return; }
  await ladeFilter().catch(() => {});
  suchen();
  status();
  if (!S.einst.quellen.length) { zeige("einstellungen"); toast("Willkommen! Füge zuerst den Ordner deines Bilder-Backups hinzu."); }
})();
