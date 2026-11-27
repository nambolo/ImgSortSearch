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
function frage(titel, html, jaText = "OK", drittesText = "", viertesText = "") {
  return new Promise((ok) => {
    $("#dialog-titel").textContent = titel;
    $("#dialog-text").innerHTML = html;
    $("#dialog-ja").textContent = jaText;
    $("#dialog-drittes").textContent = drittesText;
    $("#dialog-drittes").hidden = !drittesText;
    $("#dialog-viertes").textContent = viertesText;
    $("#dialog-viertes").hidden = !viertesText;
    $("#dialog").hidden = false;
    const fertig = (w) => {
      $("#dialog").hidden = true;
      $("#dialog-ja").onclick = $("#dialog-nein").onclick = $("#dialog-drittes").onclick = $("#dialog-viertes").onclick = null;
      ok(w);
    };
    $("#dialog-ja").onclick = () => fertig(true);
    $$("#dialog-text input").forEach((i) => i.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); fertig(true); } }));
    $("#dialog-nein").onclick = () => fertig(false);
    $("#dialog-drittes").onclick = () => fertig("drittes");
    $("#dialog-viertes").onclick = () => fertig("viertes");
  });
}

const S = {
  einst: null, kategorien: [], zusatz: [], farben: {},
  bilder: [], gesamt: 0, gewaehlt: new Set(), letzter: null, fokus: null,
  modus: localStorage.getItem("modus") || "raster",
  person: null, personName: "", aehnlich: null, bildSuche: null, bereich: null,
  laedt: false,
};

function farbe(kat) { return S.farben[kat] || "#475069"; }

/* ------------------------------------------------------------ Navigation */

function zeige(ansicht) {
  $$("nav button").forEach((b) => b.classList.toggle("aktiv", b.dataset.ansicht === ansicht));
  $$(".ansicht").forEach((a) => a.classList.toggle("aktiv", a.id === "ansicht-" + ansicht));
  const laden = { themen: ladeThemen, personen: ladePersonen, sortieren: ladeSortieren, dubletten: ladeDubletten, einstellungen: ladeEinstellungen, karte: window.ladeKarte }[ansicht];
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
    const offenLang = s.ausfuehrlich && s.ausfuehrlich.aktiv ? s.ausfuehrlich.offen : 0;
    const fortsetzen = i.phase === "angehalten" && (s.offen > 0 || offenLang > 0);
    knopf.textContent = i.laeuft ? (S.wirdAngehalten ? "Wird angehalten …" : "Anhalten")
      : (fortsetzen ? "Fortsetzen" : (s.gesamt ? "Index aktualisieren" : "Index erstellen"));
    knopf.disabled = i.laeuft && S.wirdAngehalten;
    knopf.classList.toggle("primaer", !i.laeuft);
    $("#index-phase").textContent = { suchen: "Suche Dateien", metadaten: "Metadaten", analyse: "Analyse", beschreibung_lang: "Beschreibungen", einordnen: "Einordnen", fertig: "Index", angehalten: "Angehalten", fehler: "Fehler", bereit: "Index" }[i.phase] || "Index";
    $("#index-zahl").textContent = i.laeuft && i.gesamt ? `${i.fertig.toLocaleString("de")} / ${i.gesamt.toLocaleString("de")}` : `${s.gesamt.toLocaleString("de")} Bilder`;
    $("#index-balken").style.width = i.gesamt ? `${(100 * i.fertig) / i.gesamt}%` : (i.laeuft ? "5%" : "0");
    const meldung = i.phase === "angehalten" && s.offen ? `Angehalten. ${s.offen.toLocaleString("de")} Bilder noch nicht analysiert.`
      : (i.phase === "angehalten" && offenLang ? `Angehalten. ${offenLang.toLocaleString("de")} Bilder noch ohne ausführliche Beschreibung.` : i.meldung);
    const fehlen = (s.quellen_fehlen || []).length ? ` ${s.quellen_fehlen.length} Bilder-Ordner nicht gefunden.` : "";
    $("#index-meldung").textContent = (meldung || (s.offen ? `${s.offen.toLocaleString("de")} Bilder noch nicht analysiert.` : (s.gesamt ? "Index ist aktuell." : "Noch kein Index erstellt."))) + fehlen;
    S.quellenFehlen = s.quellen_fehlen || [];
    const bild = $("#index-bild");
    bild.classList.toggle("laeuft", i.laeuft && (i.phase === "analyse" || i.phase === "beschreibung_lang"));
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
    zeigeAusfuehrlich(s.ausfuehrlich);
  } catch (e) { /* Server startet noch */ }
  statusTimer = setTimeout(status, S.indexLief ? 1000 : 2500);
}

function statusText(s) {
  const f = Object.entries(s.modell_fehler || {});
  return [
    `Bilder im Index: <b>${s.gesamt.toLocaleString("de")}</b>`,
    `Noch zu analysieren: <b>${s.offen.toLocaleString("de")}</b>`,
    `exiftool: ${s.exiftool ? "gefunden" : "nicht gefunden (Lesen über Pillow, Einbetten nicht möglich)"}`,
    `Rechnet auf: ${esc(Object.entries(s.geraete || {}).filter(([n]) => n !== "gesichter").map(([, g]) => g)[0] || "wird beim ersten Indexlauf ermittelt")}`,
    ...((s.geraete || {}).gesichter ? [`Gesichter: ${esc(s.geraete.gesichter)}`] : []),
    ...f.map(([n, t]) => `<span style="color:var(--schlecht)">Modell ${esc(n)} nicht verfügbar: ${esc(t)}</span>`),
    ...(s.index.laeuft && s.index.zeiten ? [`<span style="color:var(--text-2, inherit);font-size:12px">Zeiten: ${esc(s.index.zeiten)}</span>`] : []),
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
  const o = $("#f-ort"), altOrt = o.value;
  const n = (x) => x.anzahl.toLocaleString("de");
  const orte = [];
  (f.orte || []).forEach((x) => { const da = orte.find((y) => y.name === x.name); if (da) da.anzahl += x.anzahl; else orte.push({ ...x }); });
  o.innerHTML = `<option value="">Alle Orte</option>`
    + ((f.laender || []).length ? `<optgroup label="Länder">${f.laender.map((l) => `<option value="land:${esc(l.name)}">${esc(l.name)} (${n(l)})</option>`).join("")}</optgroup>` : "")
    + (orte.length ? `<optgroup label="Orte">${orte.map((x) => `<option value="ort:${esc(x.name)}">${esc(x.name)} (${n(x)})</option>`).join("")}</optgroup>` : "");
  o.value = altOrt;
  o.hidden = !(f.laender || []).length;
}

function sortierungAnpassen() {
  // "Beste Treffer zuerst" gibt es nur bei einer Suche; beim Start einer Suche ist es die Vorgabe
  const s = $("#f-sort"), suche = !!($("#q").value.trim() || S.aehnlich), opt = s.querySelector('[value="relevanz"]');
  if (suche && !opt.hidden) return;
  opt.hidden = !suche;
  if (suche && s.value === "datum_ab") s.value = "relevanz";
  else if (!suche && s.value === "relevanz") s.value = "datum_ab";
}

function suchParameter(offset) {
  sortierungAnpassen();
  const p = new URLSearchParams({
    q: $("#q").value, art: $("#art").value, kategorie: $("#f-kategorie").value, thema: $("#f-thema").value, jahr: $("#f-jahr").value,
    kamera: $("#f-kamera").value, ort: $("#f-ort").value, sortierung: $("#f-sort").value, offset, limit: 240,
  });
  if (S.bereich) p.set("bereich", S.bereich);
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
    if (weiter) {  // läuft der Index nebenher, verschiebt sich die Liste: keine Bilder doppelt anzeigen
      const schon = new Set(S.bilder.map((b) => b.id));
      S.bilder = S.bilder.concat(r.bilder.filter((b) => !schon.has(b.id)));
    } else S.bilder = r.bilder;
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
  if (!S.bilder.length) {
    // Keine Treffer: zeigen, welche Filter die Suche einschränken, und sie mit einem Klick entfernen
    const filter = aktiveFilter();
    const text = filter.length
      ? `Keine Bilder gefunden. Aktive Filter: <b>${filter.map(esc).join(", ")}</b>. <button class="knopf leicht" id="filter-weg">Alle Filter entfernen</button>`
      : `Keine Bilder gefunden. ${S.gesamt === 0 && !$("#q").value ? "Lege unter Einstellungen einen Bilder-Ordner an und starte den Index." : ""}`;
    if (S.modus === "raster") $("#raster").innerHTML = `<div class="mehr hinweis">${text}</div>`;
    else $("#detail").innerHTML = `<div class="leer">${text}</div>`;
    const weg = $("#filter-weg");
    if (weg) weg.onclick = filterEntfernen;
  }
  auswahlLeiste();
}

const FILTER_FELDER = [["#f-kategorie", "Kategorie"], ["#f-thema", "Thema"], ["#f-jahr", "Jahr"], ["#f-kamera", "Kamera"], ["#f-ort", "Ort"]];
function aktiveFilter() {
  const erg = FILTER_FELDER.filter(([s]) => $(s) && $(s).value).map(([s, n]) => `${n}: ${$(s).selectedOptions[0]?.textContent || $(s).value}`);
  if (S.person) erg.push(`Person: ${S.personName || "gewählt"}`);
  if (S.aehnlich) erg.push("Ähnlich wie ein Bild");
  if (S.bereich) erg.push("Kartenbereich");
  if ($("#q").value.trim() && $("#art").value !== "beides") erg.push(`Suchart: ${$("#art").selectedOptions[0].textContent}`);
  return erg;
}
function filterEntfernen() {
  FILTER_FELDER.forEach(([s]) => { if ($(s)) $(s).value = ""; });
  $("#art").value = "beides";
  S.person = null; S.aehnlich = null;
  $("#f-person-chip").hidden = true; $("#f-aehnlich-chip").hidden = true;
  if (typeof bereichEntfernen === "function") bereichEntfernen();
  suchen();
}

function groesseText(n) {
  n = n || 0;
  if (n < 1024 * 1024) return `${Math.max(1, Math.round(n / 1024)).toLocaleString("de")} KB`;
  if (n >= 1024 ** 3) return `${(n / 1024 ** 3).toLocaleString("de", { maximumFractionDigits: 1 })} GB`;
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
  const lang = !!(S.einst.beschreibung.ausfuehrlich && S.einst.beschreibung.ausfuehrlich.aktiv);
  const w = await frage("Neu bewerten",
    `<p>${ids.length.toLocaleString("de-DE")} Bild(er) neu bewerten.</p>
     <p><b>Neu einordnen</b> wendet Themen, Uhrzeit und Regeln erneut an. Das geht in Sekunden.</p>
     ${lang ? `<p><b>Beschreibung neu</b> erzeugt nur die ausführliche Beschreibung noch einmal (z. B. nach einem Modellwechsel). Alles andere bleibt.</p>` : ""}
     <p><b>Komplett neu analysieren</b> erkennt Bildinhalt, Gesichter und Text noch einmal. Das dauert je Bild etwas.</p>
     <p>Kategorien, die du von Hand gesetzt hast, bleiben in allen Fällen erhalten.</p>`,
    "Komplett neu analysieren", "Neu einordnen", lang ? "Beschreibung neu" : "");
  if (w === false) return;
  try {
    if (w === true || w === "viertes") {
      const r = await api(w === true ? "/api/bilder/neu-analysieren" : "/api/bilder/neu-beschreiben", { json: { ids } });
      toast(r.gestartet ? `${r.vorgemerkt} Bild(er) werden neu bearbeitet …` : `${r.vorgemerkt} Bild(er) vorgemerkt. Sie kommen im nächsten Indexlauf dran.`);
    } else {
      await api("/api/einordnen", { method: "POST" });
      toast("Neu eingeordnet");
      if (S.bildSuche) await bilderNachladen(ids);  // Foto- oder Personensuche nicht verwerfen
      else suchen(false, true);
    }
    status();
  } catch (e) { toast(e.message, true); }
}
$("#auswahl-neu").onclick = () => neuBewerten([...S.gewaehlt]);

/* ------------------------------------------------------------ Ausführliche Beschreibungen */

// Grobe Schätzung je Bild (Sekunden) auf einer Mittelklasse-Grafikkarte wie der RX 5600 XT;
// der Prozessor braucht etwa 8-mal so lange. Nach den ersten Bildern zeigt das Programm den gemessenen Wert.
const LANG_SEK = { "microsoft/Florence-2-large": { sehr_detailliert: 3, detailliert: 2 },
                   "microsoft/Florence-2-base": { sehr_detailliert: 1.1, detailliert: 0.8 } };
function dauerText(sek) {
  if (sek < 90) return `${Math.round(sek)} Sekunden`;
  if (sek < 5400) return `${Math.round(sek / 60)} Minuten`;
  if (sek < 172800) return `${(sek / 3600).toLocaleString("de", { maximumFractionDigits: 1 })} Stunden`;
  return `${(sek / 86400).toLocaleString("de", { maximumFractionDigits: 1 })} Tage`;
}
function zeigeAusfuehrlich(a) {
  if (!a) return;
  const box = $("#e-lang-box");
  box.hidden = !$("#e-lang").checked;
  const d = a.download || {};
  // eingeschaltet, aber das gewählte Modell fehlt: Fortschritt, Fehler oder ein Knopf zum Laden
  const fehlt = a.aktiv && !a.vorhanden;
  const zeigeDl = d.laeuft || fehlt;
  $("#e-lang-download").hidden = !zeigeDl;
  if (zeigeDl) {
    const mb = (x) => Math.round(x / 1048576).toLocaleString("de");
    const name = $(`#e-lang-modell option[value="${d.modell}"]`)?.textContent.split(":")[0] || "Modell";
    const eigenerFehler = d.fehler && d.modell === a.modell;
    $("#e-lang-download-text").textContent = d.laeuft
      ? `${name} wird heruntergeladen: ${mb(d.fertig)} von ${d.gesamt ? mb(d.gesamt) : "?"} MB`
      : eigenerFehler
        ? `Download ${d.fehler === "abgebrochen" ? "abgebrochen" : `fehlgeschlagen: ${d.fehler}`}. Bereits Geladenes bleibt und wird beim nächsten Versuch fortgesetzt.`
        : "Das gewählte Modell ist noch nicht geladen.";
    $("#e-lang-balken").style.width = d.laeuft && d.gesamt ? `${(100 * d.fertig) / d.gesamt}%` : (d.laeuft ? "3%" : "0");
    $("#e-lang-abbrechen").hidden = !d.laeuft;
    $("#e-lang-neu-laden").hidden = !!d.laeuft;
    $("#e-lang-neu-laden").textContent = eigenerFehler ? "Erneut versuchen" : "Jetzt laden";
  }
  const n = a.offen + a.fertig;
  const stufe = $("#e-lang-stufe").value;
  const geschaetzt = (LANG_SEK[a.modell] || {})[stufe] || 3;
  const je = a.sek_pro_bild || geschaetzt;
  const teile = [];
  if (!a.vorhanden && !d.laeuft) teile.push("Das Modell wird beim Einschalten einmalig in den Datenordner geladen, danach geht alles offline.");
  if (a.fertig) teile.push(`${a.fertig.toLocaleString("de")} Bilder haben eine ausführliche Beschreibung.`);
  if (a.offen) teile.push(`Für die übrigen ${a.offen.toLocaleString("de")} Bilder braucht es etwa ${dauerText(a.offen * je)}`
    + (a.sek_pro_bild ? ` (gemessen: ${je.toLocaleString("de", { maximumFractionDigits: 1 })} s je Bild).` : ` mit der Grafikkarte (Schätzung, ohne Grafikkarte etwa 8-mal so lange).`));
  else if (!n) teile.push(`Richtwert: 10.000 Bilder brauchen mit der Grafikkarte etwa ${dauerText(10000 * geschaetzt)}.`);
  teile.push("Läuft nach der normalen Analyse, lässt sich jederzeit anhalten und später fortsetzen.");
  $("#e-lang-info").textContent = teile.join(" ");
  $("#e-lang-alle").hidden = !a.vorhanden || !a.fertig;
}
async function langSpeichern() {
  try {
    S.einst = await api("/api/einstellungen", { json: { beschreibung: { ausfuehrlich: {
      aktiv: $("#e-lang").checked, modell: $("#e-lang-modell").value, stufe: $("#e-lang-stufe").value } } } });
    $("#e-lang-box").hidden = !$("#e-lang").checked;
    status();
  } catch (e) { toast(e.message, true); }
}
// Gleich speichern: das Modell soll erst geladen werden, wenn man es hier auswählt
$("#e-lang").onchange = langSpeichern;
$("#e-lang-modell").onchange = langSpeichern;
$("#e-lang-stufe").onchange = langSpeichern;
$("#e-lang-abbrechen").onclick = async () => { try { await api("/api/ausfuehrlich/abbrechen", { method: "POST" }); status(); } catch (e) { toast(e.message, true); } };
$("#e-lang-neu-laden").onclick = async () => { try { await api("/api/ausfuehrlich/download", { method: "POST" }); status(); } catch (e) { toast(e.message, true); } };
$("#e-lang-alle").onclick = async () => {
  const w = await frage("Beschreibungen neu erzeugen",
    `<p>Alle ausführlichen Beschreibungen werden mit den aktuellen Einstellungen neu erzeugt. Das dauert wieder so lange wie beim ersten Mal.
     Bildinhalt, Gesichter, Text und Kategorien bleiben, wie sie sind.</p>`, "Neu erzeugen");
  if (w !== true) return;
  try {
    const r = await api("/api/bilder/neu-beschreiben", { json: { alle: true } });
    toast(r.gestartet ? "Beschreibungen werden neu erzeugt …" : "Vorgemerkt für den nächsten Indexlauf");
    status();
  } catch (e) { toast(e.message, true); }
};

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
      if (S.bildSuche) await bilderNachladen(ids);  // Fotosuche nicht verwerfen, nur die betroffenen Bilder neu laden
      else suchen(false, true);
    }
  } catch (e) { toast(e.message, true); }
}

// Kategorie und Zusatzkategorien einzelner Bilder neu vom Server holen (Treffer der Foto- oder Personensuche
// lassen sich nicht neu abfragen, ohne die Suche zu verwerfen)
async function bilderNachladen(ids) {
  for (const id of ids) {
    const x = S.bilder.find((y) => y.id === id);
    if (!x) continue;
    const b = await api(`/api/bild/${id}`);
    x.kategorie = b.kategorie_manuell || b.kategorie_auto || "Unklar";
    x.manuell = !!b.kategorie_manuell;
    if (b.zusatz) x.zusatz = b.zusatz;
  }
  zeichneErgebnis();
  if (S.modus === "liste" && S.fokus) zeigeDetail(S.fokus);
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
        ${b.beschreibung_lang ? `<div class="lang-beschreibung" title="${esc(b.beschreibung_lang_en || "")}"><span class="hinweis">Ausführliche Beschreibung</span>${esc(b.beschreibung_lang)}</div>` : ""}
        ${b.tags ? `<div class="tags">${b.tags.split(",").map((t) => `<span class="tag">${esc(t.trim())}</span>`).join("")}</div>` : ""}
        <dl class="meta">
          <dt>Aufgenommen</dt><dd>${esc(datumText(b.datum))} <span class="hinweis">(${esc(b.datum_quelle || "")})</span></dd>
          <dt>Kamera</dt><dd>${esc([b.hersteller, b.modell].filter(Boolean).join(" ") || "unbekannt")}</dd>
          ${b.gps_lat != null ? `<dt>Ort</dt><dd>${b.ort_stadt ? `<span class="ort-link" id="d-ort" title="Alle Fotos aus ${esc(b.ort_stadt)}">${esc(b.ort || b.ort_stadt)}</span><br>` : (b.ort ? esc(b.ort) + "<br>" : "")}<span class="hinweis">${b.gps_lat.toFixed(5)}, ${b.gps_lon.toFixed(5)}</span> · <span class="ort-link" id="d-karte">auf der Karte</span></dd>` : ""}
          <dt>Größe</dt><dd>${b.breite || "?"} × ${b.hoehe || "?"} · ${groesseText(b.groesse)}</dd>
          <dt>Personen</dt><dd>${(b.gesichter || []).length ? b.gesichter.map((g) => esc(g.name || "unbenannt")).join(", ") : "keine erkannt"}</dd>
          <dt>Einordnung</dt><dd>${esc(b.gruende || "–")}</dd>
          <dt>Pfad</dt><dd>${esc(b.pfad)}</dd>
        </dl>
        ${b.gps_lat != null && S.einst.karte && S.einst.karte.detail ? `<div class="mini-karte" id="mini-karte"></div>` : ""}
        ${b.ocr_text ? `<div><div class="hinweis">Erkannter Text</div><div class="ocr">${esc(b.ocr_text)}</div></div>` : ""}
      </div>`;
    if (window.miniKarteZeigen) window.miniKarteZeigen($("#mini-karte"), b.gps_lat, b.gps_lon, id);
    if ($("#d-karte")) $("#d-karte").onclick = () => window.karteZeigen(b.gps_lat, b.gps_lon, id);
    if ($("#d-ort")) $("#d-ort").onclick = () => ortFilter("ort:" + b.ort_stadt);
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

async function ortFilter(wert) {
  await ladeFilter().catch(() => {});
  $("#f-ort").value = wert;
  if ($("#f-ort").value !== wert) return;
  bereichEntfernen();
  $("#q").value = "";
  suchen();
}

function bereichEntfernen() { S.bereich = null; $("#f-bereich-chip").hidden = true; }

// Von der Karte: Fotos in einem Rechteck (süd,west,nord,ost) zeigen, mit den Filtern der Karte
window.bereichFilter = (bereich, text, filter = {}) => {
  S.bereich = bereich;
  const c = $("#f-bereich-chip");
  c.hidden = false;
  c.innerHTML = `⌖ ${esc(text)} <button title="Filter entfernen">✕</button>`;
  $("button", c).onclick = () => { bereichEntfernen(); suchen(); };
  $("#q").value = "";
  $("#f-thema").value = $("#f-kamera").value = $("#f-ort").value = "";
  $("#f-kategorie").value = filter.kategorie || "";
  $("#f-jahr").value = filter.jahr || "";
  S.person = S.aehnlich = null;
  $("#f-person-chip").hidden = $("#f-aehnlich-chip").hidden = true;
  zeige("suche");
  suchen();
};

function personFilter(id, name) {
  S.person = id;
  S.personName = name || "";
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
["#art", "#f-kategorie", "#f-thema", "#f-jahr", "#f-kamera", "#f-ort", "#f-sort"].forEach((s) => $(s).addEventListener("change", () => suchen()));
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
let themenGeaendert = false;  // ungespeicherte Änderungen bleiben beim Wechsel der Ansicht erhalten
async function ladeThemen() {
  if (!themenGeaendert || !themenDaten) themenDaten = await api("/api/themen");
  zeichneThemen();
}
function themenAenderung() {
  themenGeaendert = true;
  zeigeThemenStand();
}
function zeigeThemenStand() {
  const k = $("#themen-speichern");
  k.textContent = themenGeaendert ? "● Speichern & neu einordnen" : "Speichern & neu einordnen";
  k.title = themenGeaendert ? "Es gibt ungespeicherte Änderungen" : "";
}

function zeichneThemen() {
  const kats = themenDaten.kategorien;
  $("#kat-liste").innerHTML = kats.map((k, i) =>
    `<div class="kat-eintrag ${k.zusatz ? "zusatz" : ""}"><input type="color" value="${esc(k.farbe || FARBEN[i % FARBEN.length])}" data-i="${i}"><span>${esc(k.name)}</span>
      <label class="kat-art" title="Zusatzkategorie: schließt andere Kategorien nicht aus, ein Bild kann mehrere davon haben"><input type="checkbox" data-zusatz="${i}" ${k.zusatz ? "checked" : ""}>zusätzlich</label>
      <button data-weg="${i}" title="Kategorie entfernen">✕</button></div>`).join("");
  $$("#kat-liste input[type=color]").forEach((c) => c.oninput = () => { kats[c.dataset.i].farbe = c.value; themenAenderung(); });
  $$("#kat-liste [data-zusatz]").forEach((c) => c.onchange = () => { kats[c.dataset.zusatz].zusatz = c.checked; themenAenderung(); zeichneThemen(); });
  $$("#kat-liste [data-weg]").forEach((b) => b.onclick = () => {
    const [weg] = kats.splice(+b.dataset.weg, 1);
    // Themen, die auf die entfernte Kategorie zeigten, werden wieder reine Schlagwörter (sonst bliebe sie gespeichert)
    themenDaten.themen.forEach((t) => { if (t.kategorie === weg.name) t.kategorie = ""; });
    themenAenderung();
    zeichneThemen();
  });
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
  $$("[data-prompt]").forEach((el) => el.onchange = () => { themenDaten.themen[el.dataset.prompt].prompt = el.value; themenAenderung(); });
  $$("[data-kat]", $("#themen-gitter")).forEach((el) => el.onchange = () => { themenDaten.themen[el.dataset.kat].kategorie = el.value; themenAenderung(); zeichneThemen(); });
  $$(".thema [data-weg]").forEach((el) => el.onclick = () => { themenDaten.themen.splice(+el.dataset.weg, 1); themenAenderung(); zeichneThemen(); });
  zeigeThemenStand();
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
  if (!n) return;
  if (n === "Unklar" || themenDaten.kategorien.some((k) => k.name.toLowerCase() === n.toLowerCase())) { toast(`Die Kategorie „${n}“ gibt es schon.`, true); return; }
  const neu = { name: n, farbe: FARBEN[themenDaten.kategorien.length % FARBEN.length] };
  if ($("#neue-kat-zusatz").checked) neu.zusatz = true;
  themenDaten.kategorien.push(neu);
  $("#neue-kat").value = "";
  themenAenderung();
  zeichneThemen();
};
$("#neue-kat").addEventListener("keydown", (e) => { if (e.key === "Enter") $("#kat-neu").click(); });
$("#thema-neu").onclick = () => {
  const n = $("#neues-thema").value.trim();
  if (!n) return;
  if (themenDaten.themen.some((t) => t.name.toLowerCase() === n.toLowerCase())) { toast(`Das Thema „${n}“ gibt es schon.`, true); return; }
  themenDaten.themen.unshift({ name: n, prompt: $("#neues-thema-prompt").value.trim() || n, kategorie: "", anzahl: 0, beispiele: [] });
  $("#neues-thema").value = $("#neues-thema-prompt").value = "";
  themenAenderung();
  zeichneThemen();
};
["#neues-thema", "#neues-thema-prompt"].forEach((s) => $(s).addEventListener("keydown", (e) => { if (e.key === "Enter") $("#thema-neu").click(); }));
$("#themen-speichern").onclick = async () => {
  const themen = themenDaten.themen.map(({ name, prompt, kategorie }) => ({ name, prompt, kategorie }));
  try {
    await api("/api/einstellungen", { json: { themen, kategorien: themenDaten.kategorien } });
    themenGeaendert = false;
    zeigeThemenStand();
    await ladeGrundlagen();
    toast("Gespeichert. Bilder werden neu eingeordnet …");
    setTimeout(() => { ladeThemen(); ladeFilter().catch(() => {}); suchen(); }, 1500);
  } catch (e) { toast(e.message, true); }
};

/* ------------------------------------------------------------ Personen */

const personenWahl = new Set();
let personenDaten = [];
async function ladePersonen() {
  const r = await api("/api/personen");
  personenDaten = r.personen;
  ladeVorschlaege().catch(() => {});
  personenWahl.clear();
  $("#personen-zusammen").disabled = true;
  const h = $("#personen-hinweis");
  h.hidden = !r.gesichter_offen;
  h.innerHTML = r.gesichter_offen ? `<strong>Gesichter werden mit dem verbesserten Modell neu erkannt.</strong> Noch ${r.gesichter_offen.toLocaleString("de-DE")} Bilder offen.
    Dafür links unten „Index erstellen“ starten (nur der Gesichtsschritt läuft, deine Namen bleiben). Bis dahin können Gruppen noch Fehler enthalten.` : "";
  $("#personen-gitter").innerHTML = r.personen.length ? r.personen.map((p) =>
    `<div class="person" data-id="${p.id}"><input type="checkbox" class="wahl" title="Zum Zusammenführen auswählen">
      <img src="/api/gesicht/${p.titel_gesicht}" alt="" title="Gesichter ansehen und korrigieren">
      <input value="${esc(p.name || "")}" placeholder="Name eingeben">
      <small><a href="#" class="bilder-link" title="Alle Bilder dieser Person zeigen">${p.bilder} Bilder</a></small></div>`).join("")
    : `<div class="hinweis">Noch keine Gesichter gefunden. Sie werden beim Erstellen des Index erkannt.</div>`;
  $$(".person").forEach((el) => {
    const id = +el.dataset.id;
    const name = $("input:not(.wahl)", el);
    $("img", el).onclick = () => personOeffnen(id, name.value).catch((e) => toast(e.message, true));
    $(".bilder-link", el).onclick = (e) => { e.preventDefault(); personFilter(id, name.value); };
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

/* Vorschläge: Gruppen, die zu einer Person gehören könnten (je Person auf einmal entscheiden) */
let vorschlaegeAus = false;
try { vorschlaegeAus = sessionStorage.getItem("vorschlaegeAus") === "1"; } catch { /* egal */ }
async function ladeVorschlaege() {
  if (vorschlaegeAus) { $("#personen-vorschlaege").hidden = true; return; }
  const r = await api("/api/personen/vorschlaege");
  $("#personen-vorschlaege").hidden = !r.gruppen.length;
  $("#pv-anzahl").textContent = r.offen ? `noch ${r.offen} Gruppen offen` : "";
  const name = (p) => p.name ? esc(p.name) : "unbenannt";
  $("#pv-liste").innerHTML = r.gruppen.map((g, i) => `<div class="vorschlag" data-i="${i}">
    <div class="pv-person pv-anker"><img src="/api/gesicht/${g.person.titel_gesicht}" alt="" data-p="${g.person.id}">
      <span>${name(g.person)}</span><small>${g.person.bilder} Bilder</small></div>
    <div class="pv-kandidaten">${g.kandidaten.map((k) => `<label class="pv-person pv-kandidat" title="Ähnlichkeit ${Math.round(k.aehnlichkeit * 100)} %">
      <img src="/api/gesicht/${k.titel_gesicht}" alt="" data-p="${k.id}"><input type="checkbox" checked data-k="${k.id}">
      <small>${k.bilder} Bilder</small></label>`).join("")}</div>
    <div class="pv-knoepfe"><button class="knopf" data-ok>Übernehmen</button><button class="knopf leicht" data-keine>Keine davon</button></div></div>`).join("");
  $$("#pv-liste .vorschlag").forEach((el) => {
    const g = r.gruppen[+el.dataset.i];
    const senden = async (alleNein) => {
      const ja = [], nein = [];
      $$("input[data-k]", el).forEach((c) => ((c.checked && !alleNein) ? ja : nein).push(+c.dataset.k));
      try {
        await api("/api/personen/vorschlag", { json: { ziel: g.person.id, ja, nein } });
        toast(ja.length ? `${ja.length} Gruppe(n) zu ${g.person.name || "dieser Person"} hinzugefügt` : "Gemerkt: andere Personen");
        await ladePersonen();
      } catch (e) { toast(e.message, true); }
    };
    $("[data-ok]", el).onclick = () => senden(false);
    $("[data-keine]", el).onclick = () => senden(true);
    // Klick aufs Gesicht: oben vergleichen (feste Höhe, die Liste springt nicht)
    $$("img[data-p]", el).forEach((img) => {
      img.onclick = (e) => {
        e.preventDefault();
        const k = g.kandidaten.find((x) => x.id === +img.dataset.p) || g.kandidaten[0];
        vergleichZeigen(g.person, k).catch((err) => toast(err.message, true));
      };
    });
  });
  if (r.gruppen.length) vergleichZeigen(r.gruppen[0].person, r.gruppen[0].kandidaten[0]).catch(() => {});
}
async function vergleichZeigen(a, b) {
  const [ga, gb] = await Promise.all([api(`/api/personen/${a.id}/typisch`), api(`/api/personen/${b.id}/typisch`)]);
  const reihe = (liste) => liste.gesichter.map((id) => `<img src="/api/gesicht/${id}" alt="" loading="lazy">`).join("");
  $("#pv-v-a").textContent = `${a.name || "Gruppe"} (${a.bilder} Bilder)`;
  $("#pv-v-b").textContent = `Vorschlag (${b.bilder} Bilder, ${Math.round(b.aehnlichkeit * 100)} % ähnlich)`;
  $("#pv-v-a-bilder").innerHTML = reihe(ga);
  $("#pv-v-b-bilder").innerHTML = reihe(gb);
  $$("#pv-liste .pv-kandidat").forEach((l) => l.classList.toggle("im-vergleich", +$("input", l).dataset.k === b.id));
}
$("#pv-spaeter").onclick = () => {
  vorschlaegeAus = true;
  try { sessionStorage.setItem("vorschlaegeAus", "1"); } catch { /* egal */ }
  $("#personen-vorschlaege").hidden = true;
};

/* Gesichter einer Person: Fehler finden und korrigieren */
const PD = { id: null, name: "", gesichter: [], wahl: new Set(), letzter: null };
async function personOeffnen(id, name) {
  const r = await api(`/api/personen/${id}/gesichter`);
  Object.assign(PD, { id, name, gesichter: r.gesichter, letzter: null });
  PD.wahl.clear();
  $("#pd-titel").textContent = `${name || "Unbenannte Person"} · ${r.gesichter.length} Gesichter`;
  $("#pd-gesichter").innerHTML = r.gesichter.map((g, i) => {
    const s = g.aehnlichkeit;
    const klasse = g.fest ? "fest" : s == null ? "" : s < 0.45 ? "unsicher" : "";
    const titel = g.fest ? "von dir bestätigt" : s == null ? "noch nicht mit dem neuen Modell erkannt" : `Ähnlichkeit ${Math.round(s * 100)} %`;
    return `<div class="gesicht ${klasse}" data-i="${i}" title="${titel}"><img src="/api/gesicht/${g.id}" alt="" loading="lazy">
      <button class="lupe" title="Ganzes Bild ansehen">⤢</button></div>`;
  }).join("") || `<div class="hinweis">Keine Gesichter.</div>`;
  $$("#pd-gesichter .gesicht").forEach((el) => {
    const i = +el.dataset.i;
    el.onclick = (e) => {
      if (e.target.classList.contains("lupe")) {
        $("#vollbild-img").src = `/api/ansicht/${PD.gesichter[i].bild_id}?kante=2000`;
        $("#vollbild").hidden = false;
        return;
      }
      if (e.shiftKey && PD.letzter != null) {
        const [a, b] = [Math.min(PD.letzter, i), Math.max(PD.letzter, i)];
        for (let k = a; k <= b; k++) PD.wahl.add(PD.gesichter[k].id);
      } else {
        const gid = PD.gesichter[i].id;
        PD.wahl.has(gid) ? PD.wahl.delete(gid) : PD.wahl.add(gid);
      }
      PD.letzter = i;
      pdAuswahlZeigen();
    };
  });
  const andere = personenDaten.filter((p) => p.id !== id)
    .sort((a, b) => (!a.name - !b.name) || (a.name || "").localeCompare(b.name || "") || b.bilder - a.bilder);
  $("#pd-gruppe").innerHTML = `<option value="">Ganze Gruppe zusammenführen mit …</option>` +
    andere.map((p) => `<option value="${p.id}">${p.name ? esc(p.name) : `unbenannte Gruppe (${p.bilder} Bilder)`}</option>`).join("");
  $("#pd-ziel").innerHTML = `<option value="">Anderer Person zuweisen …</option>` +
    personenDaten.filter((p) => p.name && p.id !== id).map((p) => `<option value="${p.id}">${esc(p.name)}</option>`).join("") +
    `<option value="neu">Neue Person …</option>`;
  pdAuswahlZeigen();
  $("#person-detail").hidden = false;
  $("#person-detail").scrollIntoView({ behavior: "smooth", block: "start" });
}
function pdAuswahlZeigen() {
  $$("#pd-gesichter .gesicht").forEach((el) => el.classList.toggle("gewaehlt", PD.wahl.has(PD.gesichter[+el.dataset.i].id)));
  const n = PD.wahl.size;
  $("#pd-anzahl").textContent = n ? `${n} ausgewählt` : "Nichts ausgewählt";
  ["#pd-nicht", "#pd-kein", "#pd-ziel"].forEach((s) => { $(s).disabled = !n; });
}
async function pdAktion(pfad, daten, meldung) {
  try {
    await api(pfad, { json: { gesichter: [...PD.wahl], ...daten } });
    toast(meldung);
    await ladePersonen();
    if (personenDaten.some((p) => p.id === PD.id)) await personOeffnen(PD.id, PD.name);
    else $("#person-detail").hidden = true;
  } catch (e) { toast(e.message, true); }
}
$("#pd-nicht").onclick = () => pdAktion("/api/gesichter/nicht-diese-person", {}, "Entfernt. Ähnliche Gesichter werden dieser Person nicht mehr zugeordnet.");
$("#pd-kein").onclick = () => pdAktion("/api/gesichter/kein-gesicht", {}, "Als „kein Gesicht“ markiert");
$("#pd-ziel").onchange = async (e) => {
  const wert = e.target.value;
  e.target.value = "";
  if (!wert) return;
  if (wert === "neu") {
    const ok = frage("Neue Person", `<input id="pd-neuer-name" placeholder="Name" style="width:100%">`, "Anlegen");
    setTimeout(() => $("#pd-neuer-name")?.focus(), 50);
    const ja = await ok;  // das Eingabefeld bleibt nach dem Schließen im Dialog stehen
    const name = ($("#pd-neuer-name")?.value || "").trim();
    if (ja !== true || !name) return;
    return pdAktion("/api/gesichter/zuweisen", { name }, `${PD.wahl.size} Gesicht(er) zu ${name}`);
  }
  const p = personenDaten.find((x) => x.id === +wert);
  return pdAktion("/api/gesichter/zuweisen", { person: +wert }, `${PD.wahl.size} Gesicht(er) zu ${p ? p.name : "Person"} verschoben`);
};
$("#pd-schliessen").onclick = () => { $("#person-detail").hidden = true; };
$("#pd-bilder").onclick = () => personFilter(PD.id, PD.name);
$("#pd-gruppe").onchange = async (e) => {
  const ziel = +e.target.value;
  e.target.value = "";
  if (!ziel) return;
  const p = personenDaten.find((x) => x.id === ziel);
  const zielName = p && p.name ? p.name : "die andere Gruppe";
  const ok = await frage("Gruppen zusammenführen",
    `Alle ${PD.gesichter.length} Gesichter von <b>${esc(PD.name || "dieser Gruppe")}</b> kommen zu <b>${esc(zielName)}</b>. Hat nur eine der beiden Gruppen einen Namen, bleibt dieser Name.`,
    "Zusammenführen");
  if (ok !== true) return;
  try {
    const r = await api("/api/personen/zusammenfuehren", { json: { ziel, quellen: [PD.id] } });
    toast(`Zusammengeführt mit ${zielName}`);
    await ladePersonen();
    const neu = personenDaten.find((x) => x.id === r.ziel);
    if (neu) await personOeffnen(neu.id, neu.name || "");
    else $("#person-detail").hidden = true;
  } catch (err) { toast(err.message, true); }
};
$("#personen-gruppieren").onclick = async () => {
  try {
    const r = await api("/api/personen/neu-gruppieren", { method: "POST" });
    toast(`${r.personen} Personen, ${r.zugeordnet} Gesichter zugeordnet`);
    await ladePersonen();
  } catch (e) { toast(e.message, true); }
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

let sortierFormular = false;  // schon ausgefüllt: Eingaben beim Wechsel der Ansicht behalten
async function ladeSortieren() {
  if (sortierFormular) { sortierNurZeichnen(); ladeProtokolle(); return; }
  sortierFormular = true;
  const s = S.einst.sortieren;
  $(`input[name=modus][value=${s.modus}]`).checked = true;
  $("#s-ziel").value = s.ziel || "";
  $("#s-ordner").value = s.ordner;
  $("#s-umbenennen").checked = !!s.umbenennen;
  $("#s-name").value = s.name;
  $("#s-tags").value = s.tags;
  sortierNurZeichnen();
  sortierFelder();
  ladeProtokolle();
}
// Kategorien können sich inzwischen geändert haben; die Haken bleiben, neue Hauptkategorien sind angehakt
function sortierNurZeichnen() {
  const alt = new Map($$("#s-nur input").map((i) => [i.value, i.checked]));
  const an = (k, standard) => (alt.has(k) ? alt.get(k) : standard) ? "checked" : "";
  $("#s-nur").innerHTML = [...S.kategorien, "Unklar"].map((k) => `<label><input type="checkbox" value="${esc(k)}" ${an(k, k !== "Unklar")}>${esc(k)}</label>`).join("")
    + S.zusatz.map((k) => `<label title="Zusatzkategorie: Bilder damit zusätzlich mitnehmen"><input type="checkbox" value="${esc(k)}" ${an(k, false)}>+ ${esc(k)}</label>`).join("");
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
    S.einst.sortieren = { ...S.einst.sortieren, ...s };  // der Server merkt sich die Einstellungen ebenfalls
    S.sortiertLaeuft = true;
    $("#s-balken-box").hidden = false;
    toast("Läuft …");
  } catch (e) { toast(e.message, true); }
};

async function ladeProtokolle() {
  const r = await api("/api/protokolle");
  $("#protokolle").innerHTML = r.protokolle.length ? r.protokolle.map((p) =>
    `<div class="protokoll ${p.rueckgaengig ? "zurueck" : ""}"><div><b>${esc(datumText(p.zeit))}</b><br><span class="hinweis">${esc(p.beschreibung)}</span></div>
      ${p.rueckgaengig ? "<span class='hinweis'>rückgängig gemacht</span>" : `<button class="knopf leicht" data-p="${p.id}" data-art="${esc(p.beschreibung)}">Rückgängig</button>`}</div>`).join("")
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
      if (/^Dubletten/.test(b.dataset.art || "")) {
        // aussortierte Bilder wurden aus dem Index genommen: zurückgelegt nimmt der Index sie wieder auf
        await api("/api/index/start", { method: "POST" }).catch(() => {});
        toast("Rückgängig gemacht. Die Bilder werden wieder in den Index aufgenommen …");
        status();
      }
      ladeProtokolle();
      suchen();
    } catch (e) { b.disabled = false; toast(e.message, true); }
  });
}

/* ------------------------------------------------------------ Dubletten */

const D = { gruppen: [], wahl: new Map(), timer: null, geladen: false };

function gbText(n) {
  return n >= 1073741824 ? `${(n / 1073741824).toLocaleString("de", { maximumFractionDigits: 1 })} GB` : groesseText(n);
}

async function ladeDubletten() {
  const d = await api("/api/dubletten");
  if (!D.geladen) {
    $(`input[name=d-ziel][value=${d.ziel === "papierkorb" && d.papierkorb ? "papierkorb" : "quarantaene"}]`).checked = true;
    $("#d-quarantaene").value = d.quarantaene;
    D.geladen = true;
  }
  $("#d-papierkorb-wahl").hidden = !d.papierkorb;
  dublettenFelder();
  zeigeDublettenStatus(d);
}

function dublettenFelder() { $("#d-quarantaene-feld").hidden = $("input[name=d-ziel]:checked").value !== "quarantaene"; }
$$("input[name=d-ziel]").forEach((r) => r.onchange = dublettenFelder);
$("#d-quarantaene-waehlen").onclick = () => ordnerWaehlen("#d-quarantaene");

function zeigeDublettenStatus(d) {
  const st = d.status;
  clearTimeout(D.timer);
  $("#d-suchen").textContent = st.laeuft ? "Anhalten" : (d.ergebnis ? "Erneut suchen" : "Dubletten suchen");
  $("#d-balken-box").hidden = !st.laeuft;
  $("#d-balken").style.width = st.gesamt ? `${(100 * st.fertig) / st.gesamt}%` : "4%";
  if (st.laeuft) {
    $("#d-meldung").textContent = `${st.meldung} ${st.gesamt ? `${st.fertig.toLocaleString("de")} / ${st.gesamt.toLocaleString("de")}` : ""}`;
    D.timer = setTimeout(() => ladeDubletten().catch(() => {}), 800);
    return;
  }
  if (st.phase === "fehler") $("#d-meldung").textContent = st.meldung;
  if (d.ergebnis) {
    if (st.phase !== "fehler") $("#d-meldung").textContent = `Stand ${datumText(d.ergebnis.zeit)}.${st.phase === "angehalten" ? " Die letzte Suche wurde angehalten." : ""}`;
    D.gruppen = d.ergebnis.gruppen;
    // Auswahl merken, solange die Gruppe noch da ist
    const neu = new Map();
    D.gruppen.forEach((g) => {
      const alt = D.wahl.get(g.nr);
      const ids = new Set(g.bilder.map((b) => b.id));
      const k = alt ? new Set([...alt].filter((i) => ids.has(i))) : null;
      neu.set(g.nr, k && k.size ? k : new Set(g.bilder.filter((b) => b.behalten).map((b) => b.id)));
    });
    D.wahl = neu;
    zeichneDubletten();
  }
}

function dubAngezeigt() { const f = $("#d-filter").value; return D.gruppen.filter((g) => !f || g.art === f); }

function dubWeg(g) { const k = D.wahl.get(g.nr); return g.bilder.filter((b) => !k.has(b.id)); }

function zeichneDubletten() {
  const gr = dubAngezeigt();
  const wegAlle = gr.flatMap(dubWeg);
  const exakt = D.gruppen.filter((g) => g.art === "exakt").length;
  $("#d-zaehler").innerHTML = D.gruppen.length ? `
    <div><b>${D.gruppen.length.toLocaleString("de")}</b>Gruppen</div>
    <div><b>${exakt.toLocaleString("de")}</b>exakt gleich</div>
    <div><b>${(D.gruppen.length - exakt).toLocaleString("de")}</b>ähnlich</div>
    <div><b>${gbText(wegAlle.reduce((s, b) => s + b.groesse, 0))}</b>frei durch Aussortieren</div>` : "";
  $("#d-sammel").hidden = !D.gruppen.length;
  $("#d-alle").textContent = `Alle angezeigten bereinigen (${wegAlle.length.toLocaleString("de")} Dateien)`;
  $("#d-alle").disabled = !wegAlle.length;
  if (!D.gruppen.length) {
    $("#d-gruppen").innerHTML = `<div class="karte hinweis">Keine Dubletten gefunden.</div>`;
    return;
  }
  // bei sehr vielen Gruppen nur die ersten zeichnen; „Alle angezeigten“ gilt trotzdem für alle
  const zeigen = gr.slice(0, 150);
  $("#d-gruppen").innerHTML = zeigen.map((g) => {
    const k = D.wahl.get(g.nr);
    const maxPx = Math.max(...g.bilder.map((b) => (b.breite || 0) * (b.hoehe || 0)));
    const weg = dubWeg(g);
    return `<div class="d-gruppe" data-nr="${g.nr}">
      <div class="d-kopf"><div><span class="d-art ${g.art}">${g.art === "exakt" ? "Exakt gleich" : "Ähnlich"}</span>
        <span class="hinweis">Vorschlag behalten: ${esc(g.gruende.join(", "))}</span></div>
        <div class="zeile"><span class="hinweis">${weg.length} aussortieren · ${gbText(weg.reduce((s, b) => s + b.groesse, 0))}</span>
          <button class="knopf" data-bereinigen="${g.nr}" ${weg.length && k.size ? "" : "disabled"}>Gruppe bereinigen</button></div></div>
      <div class="d-bilder">${g.bilder.map((b) => {
        const beh = k.has(b.id);
        const px = (b.breite || 0) * (b.hoehe || 0);
        return `<div class="d-bild ${beh ? "behalten" : "weg"}" data-id="${b.id}" title="Klick: behalten / aussortieren · Doppelklick: groß ansehen">
          <img src="/api/vorschau/${b.id}" loading="lazy" alt="">
          <div class="d-info"><b>${esc(b.name)}</b><span class="hinweis">${esc(b.ordner)}</span>
            <span class="${px === maxPx && px ? "d-besser" : ""}">${b.breite && b.hoehe ? `${b.breite} × ${b.hoehe}` : "Größe unbekannt"} · ${groesseText(b.groesse)}</span>
            <span>${esc(datumText(b.datum))} <span class="hinweis">(${b.datum_quelle === "exif" ? "Kamera" : esc(b.datum_quelle || "")})</span></span>
            ${b.kamera ? `<span class="hinweis">${esc(b.kamera)}</span>` : ""}</div>
          <div class="d-status"><span>${beh ? "✓ Behalten" : "✕ Aussortieren"}</span><button data-explorer="${b.id}" title="Im Explorer zeigen">Ordner öffnen</button></div>
        </div>`;
      }).join("")}</div></div>`;
  }).join("") + (gr.length > zeigen.length ? `<div class="hinweis">… und ${gr.length - zeigen.length} weitere Gruppen. „Alle angezeigten bereinigen“ gilt auch für diese.</div>` : "");
}

$("#d-filter").onchange = zeichneDubletten;

$("#d-gruppen").addEventListener("click", (ev) => {
  const ex = ev.target.closest("[data-explorer]");
  if (ex) { ev.stopPropagation(); api(`/api/bilder/${ex.dataset.explorer}/explorer`, { method: "POST" }).catch((e) => toast(e.message, true)); return; }
  const knopf = ev.target.closest("[data-bereinigen]");
  if (knopf) { dubBereinigen(D.gruppen.filter((g) => g.nr === +knopf.dataset.bereinigen)); return; }
  const kachel = ev.target.closest(".d-bild");
  if (!kachel) return;
  const g = D.gruppen.find((x) => x.nr === +kachel.closest(".d-gruppe").dataset.nr);
  const k = D.wahl.get(g.nr), id = +kachel.dataset.id;
  if (k.has(id)) {
    if (k.size === 1) { toast("Mindestens ein Bild der Gruppe bleibt erhalten."); return; }
    k.delete(id);
  } else k.add(id);
  zeichneDubletten();
});
$("#d-gruppen").addEventListener("dblclick", (ev) => {
  const kachel = ev.target.closest(".d-bild");
  if (!kachel) return;
  $("#vollbild-img").src = `/api/ansicht/${kachel.dataset.id}?kante=3000`;
  $("#vollbild").hidden = false;
});

async function dubBereinigen(gruppen) {
  const auftrag = gruppen.map((g) => ({ nr: g.nr, behalten: [...D.wahl.get(g.nr)], weg: dubWeg(g).map((b) => b.id) }))
    .filter((g) => g.weg.length && g.behalten.length);
  const n = auftrag.reduce((s, g) => s + g.weg.length, 0);
  if (!n) return;
  const ziel = $("input[name=d-ziel]:checked").value;
  const quarantaene = $("#d-quarantaene").value.trim();
  const bytes = gruppen.flatMap(dubWeg).reduce((s, b) => s + b.groesse, 0);
  const ort = ziel === "papierkorb" ? "in den Windows-Papierkorb gelegt." : `in den Quarantäne-Ordner verschoben:<br><span class="hinweis">${esc(quarantaene)}</span>`;
  const aehnlich = gruppen.filter((g) => g.art === "aehnlich").length;
  const ok = await frage(gruppen.length > 1 ? `${gruppen.length.toLocaleString("de")} Gruppen bereinigen?` : "Gruppe bereinigen?",
    `<b>${n.toLocaleString("de")}</b> ${n === 1 ? "Datei" : "Dateien"} (${gbText(bytes)}) ${n === 1 ? "wird" : "werden"} ${ort}<br><br>Je Gruppe bleibt das grün markierte Bild an seinem Platz.`
    + (aehnlich && gruppen.length > 1 ? `<br>${aehnlich.toLocaleString("de")} Gruppen sind nur ähnlich, nicht exakt gleich. Am besten vorher kurz durchsehen.` : "")
    + `<br>Rückgängig unter „Sortieren → Bisherige Läufe“.`, "Bereinigen");
  if (!ok) return;
  try {
    const r = await api("/api/dubletten/bereinigen", { json: { ziel, quarantaene, gruppen: auftrag } });
    toast(`${r.erledigt.length.toLocaleString("de")} Dateien aussortiert (${gbText(r.bytes)})`);
    if (r.fehler.length) await frage("Nicht alles hat geklappt", r.fehler.slice(0, 15).map(esc).join("<br>"), "OK");
    await ladeDubletten();
  } catch (e) { toast(e.message, true); }
}
$("#d-alle").onclick = () => dubBereinigen(dubAngezeigt());

$("#d-suchen").onclick = async () => {
  try {
    const d = await api("/api/dubletten");
    if (d.status.laeuft) await api("/api/dubletten/stop", { method: "POST" });
    else { await api("/api/dubletten/suchen", { method: "POST" }); D.wahl = new Map(); }
    ladeDubletten();
  } catch (e) { toast(e.message, true); }
};

/* ------------------------------------------------------------ Einstellungen */

const TAGE = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"];
let quellen = [];
let einstGeaendert = false;  // ungespeicherte Eingaben beim Wechsel der Ansicht nicht verwerfen
function einstStand() {
  $("#e-speichern").textContent = einstGeaendert ? "● Speichern" : "Speichern";
  $("#e-speichern").title = einstGeaendert ? "Es gibt ungespeicherte Änderungen" : "";
}
// Bilder-Ordner und ausführliche Beschreibungen werden sofort gespeichert, alles andere mit „Speichern“
["input", "change"].forEach((art) => $("#ansicht-einstellungen").addEventListener(art, (ev) => {
  if (ev.target.closest(".quellen, #e-quelle-neu, #e-lang, #e-lang-box, #e-modelle")) return;
  einstGeaendert = true;
  einstStand();
}));
function ladeEinstellungen() {
  modelleZeigen();
  if (einstGeaendert) { zeichneQuellen(); kartenSpeicher(); return; }
  const e = S.einst;
  quellen = [...e.quellen];
  zeichneQuellen();
  const az = e.arbeitszeit;
  $("#e-az-aktiv").checked = az.aktiv;
  $("#e-tage").innerHTML = TAGE.map((t, i) => `<button data-t="${i + 1}" class="${az.wochentage.includes(i + 1) ? "an" : ""}">${t}</button>`).join("");
  $$("#e-tage button").forEach((b) => b.onclick = () => { b.classList.toggle("an"); einstGeaendert = true; einstStand(); });
  $("#e-von").value = az.von;
  $("#e-bis").value = az.bis;
  const opt = (wert) => `<option value="">nichts</option>` + S.kategorien.map((k) => `<option ${k === wert ? "selected" : ""}>${esc(k)}</option>`).join("");
  $("#e-az-kat").innerHTML = opt(az.kategorie);
  $("#e-fz-kat").innerHTML = opt(az.kategorie_freizeit);
  $("#e-beschreibung").checked = e.beschreibung.aktiv;
  const lang = e.beschreibung.ausfuehrlich || {};
  $("#e-lang").checked = !!lang.aktiv;
  $("#e-lang-modell").value = lang.modell || "microsoft/Florence-2-large";
  $("#e-lang-stufe").value = lang.stufe || "sehr_detailliert";
  $("#e-lang-box").hidden = !lang.aktiv;
  $("#e-gesichter").checked = e.gesichter.aktiv;
  $("#e-ocr").value = e.ocr.modus;
  $("#e-beschl").value = e.beschleunigung || "auto";
  $("#e-schwelle").value = e.schwelle;
  $("#e-schwelle-wert").textContent = e.schwelle;
  $("#e-karte-detail").checked = !!(e.karte && e.karte.detail);
  $("#e-karte-online").checked = !!(e.karte && e.karte.online);
  kartenSpeicher();
}
async function kartenSpeicher() {
  try {
    const i = await api("/api/karte/info");
    $("#e-karte-speicher").textContent = i.kacheln ? ` Gespeichert: ${i.kacheln.toLocaleString("de")} Kartenbilder, ${groesseText(i.bytes)}.` : "";
    $("#e-karte-leeren").hidden = !i.kacheln;
  } catch { /* egal */ }
}
$("#e-karte-leeren").onclick = async () => {
  try { await api("/api/karte/leeren", { method: "POST" }); toast("Gespeicherte Kartenbilder gelöscht"); kartenSpeicher(); }
  catch (e) { toast(e.message, true); }
};
$("#e-index-zuruecksetzen").onclick = async () => {
  const ok = await frage("Index zurücksetzen?",
    "Der Suchindex wird komplett gelöscht: alle Bilder im Index, erkannte Gesichter, Personen mit ihren Namen, " +
    "von Hand gesetzte Kategorien und Vorschaubilder. Das lässt sich nicht rückgängig machen.<br><br>" +
    "Deine Bilddateien, die Einstellungen, heruntergeladene Modelle und die Protokolle für „Rückgängig“ bleiben. " +
    "Danach wird der Index neu aufgebaut, das dauert je nach Anzahl der Bilder einige Zeit.<br><br>" +
    "Zum Bestätigen bitte <b>LÖSCHEN</b> eintippen:<br><input id=\"zurueck-wort\" autocomplete=\"off\">",
    "Index löschen");
  if (!ok) return;
  if (($("#zurueck-wort")?.value || "").trim().toUpperCase() !== "LÖSCHEN") { toast("Nicht bestätigt, nichts gelöscht.", true); return; }
  try {
    const r = await api("/api/index/zuruecksetzen", { json: { bestaetigt: true } });
    toast(`Index zurückgesetzt (${r.entfernt.toLocaleString("de")} Bilder entfernt).`);
    location.reload();
  } catch (e) { toast(e.message, true); }
};
// Heruntergeladene Modelle: Größe und ob sie gebraucht werden (nach den gespeicherten Einstellungen)
let modellListe = null;
async function modelleZeigen() {
  try { modellListe = await api("/api/modelle"); } catch { $("#e-modelle").innerHTML = ""; return; }
  const m = [...modellListe.modelle].sort((a, b) => a.gebraucht - b.gebraucht);  // ungenutzte oben
  const frei = m.filter((x) => !x.gebraucht);
  const zeile = (x) => `<label class="modell ${x.gebraucht ? "" : "ungenutzt"}">
      ${x.gebraucht ? `<span class="modell-punkt" title="wird gebraucht">✓</span>` : `<input type="checkbox" data-id="${esc(x.id)}" checked>`}
      <span class="modell-name"><b>${esc(x.name)}</b><small>${esc(x.grund)}</small></span>
      <span class="modell-groesse">${groesseText(x.groesse)}</span></label>`;
  $("#e-modelle").innerHTML = !m.length ? `<div class="hinweis">Noch keine Modelle heruntergeladen.</div>` :
    m.map(zeile).join("") +
    `<div class="hinweis modelle-summe">Zusammen ${groesseText(modellListe.gesamt)}` +
    (frei.length ? `, davon <b>${groesseText(modellListe.frei)}</b> ungenutzt.` : `. Alle Modelle werden gebraucht.`) +
    (einstGeaendert ? ` Die Liste richtet sich nach den gespeicherten Einstellungen: bitte erst speichern.` : "") + `</div>`;
  $("#e-modelle-entfernen").hidden = !frei.length;
  $$("#e-modelle input").forEach((i) => i.onchange = modelleKnopf);
  modelleKnopf();
}
function modelleAuswahl() {
  const ids = new Set($$("#e-modelle input:checked").map((i) => i.dataset.id));
  return (modellListe ? modellListe.modelle : []).filter((x) => !x.gebraucht && ids.has(x.id));
}
function modelleKnopf() {
  const w = modelleAuswahl();
  const summe = w.reduce((s, x) => s + x.groesse, 0);
  $("#e-modelle-entfernen").disabled = !w.length;
  $("#e-modelle-entfernen").textContent = w.length ? `Ungenutzte Modelle entfernen (${groesseText(summe)}) …` : "Ungenutzte Modelle entfernen …";
}
$("#e-modelle-entfernen").onclick = async () => {
  if (einstGeaendert) { toast("Bitte erst die Einstellungen speichern, danach wird angezeigt, welche Modelle ungenutzt sind.", true); return; }
  if (modellListe && modellListe.gesperrt) { toast(modellListe.gesperrt, true); return; }
  const w = modelleAuswahl();
  if (!w.length) return;
  const summe = w.reduce((s, x) => s + x.groesse, 0);
  const ok = await frage("Ungenutzte Modelle löschen?",
    `<p>Diese Modelle werden aus dem Datenordner gelöscht:</p><ul>${w.map((x) => `<li>${esc(x.name)}: ${groesseText(x.groesse)}</li>`).join("")}</ul>
     <p>Dadurch werden <b>${groesseText(summe)}</b> frei. Deine Bilder und der Suchindex bleiben unverändert.</p>
     <p>Schaltest du eine dieser Funktionen später wieder ein, wird das Modell automatisch neu heruntergeladen (braucht Internet).</p>`,
    "Löschen");
  if (!ok) return;
  try {
    const r = await api("/api/modelle/entfernen", { json: { ids: w.map((x) => x.id), bestaetigt: true } });
    if (r.fehler.length) toast(`${groesseText(r.frei)} frei. Nicht gelöscht: ${r.fehler.map((f) => `${f.name} (${f.meldung})`).join("; ")}`, true);
    else toast(`${r.entfernt.length} ${r.entfernt.length === 1 ? "Modell" : "Modelle"} gelöscht, ${groesseText(r.frei)} frei.`);
  } catch (e) { toast(e.message, true); }
  modelleZeigen();
};
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
    beschreibung: { aktiv: $("#e-beschreibung").checked, ausfuehrlich: { aktiv: $("#e-lang").checked,
      modell: $("#e-lang-modell").value, stufe: $("#e-lang-stufe").value } },
    gesichter: { aktiv: $("#e-gesichter").checked },
    ocr: { modus: $("#e-ocr").value },
    beschleunigung: $("#e-beschl").value,
    schwelle: +$("#e-schwelle").value,
    karte: { detail: $("#e-karte-detail").checked, online: $("#e-karte-online").checked },
  };
  try {
    S.einst = await api("/api/einstellungen", { json: neu });
    einstGeaendert = false;
    einstStand();
    modelleZeigen();
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
