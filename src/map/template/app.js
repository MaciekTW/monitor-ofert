"use strict";
const META = JSON.parse(document.getElementById("meta").textContent);
const OFFERS = JSON.parse(document.getElementById("data").textContent);
const GEN = new Date(META.gen);
const PORTAL = {olx: "OLX", otodom: "Otodom", gratka: "Gratka"};
const $ = s => document.querySelector(s);
const esc = s => (s == null ? "" : String(s))
  .replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;")
  .replace(/"/g,"&quot;");
const fmtP = v => v == null ? "brak ceny"
  : Math.round(v).toString().replace(/\B(?=(\d{3})+(?!\d))/g,"\u202f") + " zł";
const fmtN = v => v == null ? "—"
  : Math.round(v).toString().replace(/\B(?=(\d{3})+(?!\d))/g,"\u202f");
function fmtDate(iso){
  if(!iso) return "—";
  const d = new Date(iso), days = Math.floor((GEN - d)/864e5);
  if(days <= 0) return "dziś";
  if(days === 1) return "wczoraj";
  if(days < 14) return days + " dni temu";
  return d.toLocaleDateString("pl-PL");
}
function roomBucket(r){
  if(!r) return null;
  if(/kawaler/i.test(r)) return 1;
  const m = r.match(/\d+/);
  return m ? Math.min(+m[0], 4) : null;
}
// obniżka: cena aktualna vs najwyższa w historii
for(const o of OFFERS){
  o._rb = roomBucket(o.r);
  o._drop = 0;
  if(o.h && o.p != null){
    const top = Math.max(...o.h.map(x => x[1]));
    if(top > o.p) o._drop = (top - o.p) / top;
  }
  o._new = o.fs && (GEN - new Date(o.fs)) < 48*3600e3;
  o._txt = (o.t || "").toLowerCase();
  o._dtxt = (o.dsc || "").toLowerCase();
}
document.title = `Mieszkania ${META.city} — mapa ofert OLX`;
$("#hd").textContent = `Mieszkania — ${META.city}`;
const perSrc = OFFERS.reduce((m,o) => (m[o.s]=(m[o.s]||0)+1, m), {});
$("#hdsub").textContent = `stan z ${GEN.toLocaleString("pl-PL",
  {dateStyle:"medium", timeStyle:"short"})} · ` +
  Object.entries(perSrc).map(([s,n]) => `${PORTAL[s]||s}: ${n}`).join(" · ");

/* ---------- wykresy (Apache ECharts) ---------- */
// ECharts rysuje na canvasie, który nie rozumie var(--…) — kolory jak w @theme w style.css
const CH = {acc:"#2456c7", good:"#178a4c", bad:"#c92a2a", mut:"#68758a", ink:"#1c2733",
  line:"#e2e5ea", orange:"#eb6834", aqua:"#1baf7a"};
const CHART_BASE = {
  animationDuration: 300,
  textStyle: {fontFamily: '-apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif'},
  tooltip: {confine: true, backgroundColor: "#fff", borderColor: CH.line, padding: [6, 10],
    textStyle: {color: CH.ink, fontSize: 12.5},
    extraCssText: "box-shadow:0 4px 16px rgba(15,25,40,.14);border-radius:8px",
    axisPointer: {type: "line", lineStyle: {color: CH.mut, type: "dashed"}}},
  xAxis: {type: "time", axisTick: {show: false}, splitLine: {show: false},
    axisLine: {lineStyle: {color: CH.line}},
    axisLabel: {color: CH.mut, fontSize: 10.5, hideOverlap: true, formatter: "{dd}.{MM}"}},
};
// instancja na element + automatyczne dopasowanie do zmian rozmiaru kontenera
function chartInit(el){
  let chart = echarts.getInstanceByDom(el);
  if(!chart){
    chart = echarts.init(el, null, {locale: "PL"});
    new ResizeObserver(() => chart.isDisposed() || chart.resize()).observe(el);
  }
  return chart;
}

/* ---------- mapa ---------- */
const withGeo = OFFERS.filter(o => o.lat != null && o.lon != null);
const cLat = withGeo.length ?
  withGeo.reduce((s,o)=>s+o.lat,0)/withGeo.length : 50.0614;
const cLon = withGeo.length ?
  withGeo.reduce((s,o)=>s+o.lon,0)/withGeo.length : 19.9366;
const map = L.map("map", {preferCanvas:true}).setView([cLat, cLon], 12);
/* Kafelki OpenStreetMap.org wymagają od 2026 r. nagłówka Referer, którego
   przeglądarki nie wysyłają z plików lokalnych (file://) — każdy kafelek
   wracał jako "Access blocked". Publiczne kafelki CARTO (te same dane OSM,
   styl Voyager) nie mają tego wymogu i działają z pliku lokalnego. Od sierpnia 2026 CARTO
   wymaga klucza (CARTO_API_KEY) — bez niego kafelki mają napis „API KEY REQUIRED”. */
/* dwa podkłady do wyboru w panelu „Podkład i granice”; pierwszy jest włączony na starcie */
const BASEMAPS = [{
  label: "Mapa",
  url: "https://{s}.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}{r}.png" +
    (META.carto ? "?key=" + encodeURIComponent(META.carto) : ""),
  opts: {maxZoom:20, subdomains:"abcd",
    attribution:'&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>'
               +' &middot; &copy; <a href="https://carto.com/attributions">CARTO</a>'},
}, {
  /* Ortofotomapa GUGiK (WMTS w EPSG:3857, czyli siatka kafelków taka sama jak w OSM):
     darmowa, bez klucza i bez wymogu Referera, więc działa też z pliku lokalnego.
     Zdjęcia kończą się na zoomie 19 (~10 cm/px) — wyżej maxNativeZoom rozciąga
     ostatni poziom, zamiast prosić o kafelek, którego serwis nie ma. */
  label: "Zdjęcia lotnicze",
  url: "https://mapy.geoportal.gov.pl/wss/service/PZGIK/ORTO/WMTS/StandardResolution" +
    "?SERVICE=WMTS&REQUEST=GetTile&VERSION=1.0.0&LAYER=ORTOFOTOMAPA&STYLE=default" +
    "&FORMAT=image/jpeg&TILEMATRIXSET=EPSG:3857&TILEMATRIX=EPSG:3857:{z}&TILEROW={y}&TILECOL={x}",
  opts: {maxZoom:20, maxNativeZoom:19,
    attribution:'ortofotomapa &copy; <a href="https://www.geoportal.gov.pl">GUGiK</a>'},
}];
// miniatura podkładu to zwykły kafelek z tego samego źródła, wzięty z okolicy środka
// mapy — pokazuje dokładnie to, co się włączy, i nie trzeba trzymać obrazków w repo
const THUMB_Z = 14, thumbN = 2 ** THUMB_Z, rad = cLat * Math.PI / 180;
const THUMB = {z: THUMB_Z, r: "", s: "a",
  x: Math.floor((cLon + 180) / 360 * thumbN),
  y: Math.floor((1 - Math.log(Math.tan(rad) + 1 / Math.cos(rad)) / Math.PI) / 2 * thumbN)};
for(const b of BASEMAPS){
  b.layer = L.tileLayer(b.url, b.opts);
  b.thumb = L.Util.template(b.url, THUMB);
}
BASEMAPS[0].layer.addTo(map);
// skala kolorów: kwintyle ceny za m² liczone raz, z całego zbioru
const PAL = ["#1a9850","#8fce54","#f5c53c","#f2803a","#d73027"];
const ppmAll = OFFERS.map(o=>o.pm).filter(v=>v!=null).sort((a,b)=>a-b);
const BR = [1,2,3,4].map(i => ppmAll[Math.floor(ppmAll.length*i/5)] || 0);
const NOPRICE = "#8a93a3";
// przedział ceny za m²: 0–4 jak w legendzie, 5 = brak ceny za m²
const bucketOf = v => v == null ? 5 : BR.findIndex(b => v <= b) === -1 ? 4 : BR.findIndex(b => v <= b);
const colorOf = v => PAL[bucketOf(v)] ?? NOPRICE;
function bucketLabel(i){
  const lo = i ? fmtN(BR[i-1]) : null, hi = BR[i] ? fmtN(BR[i]) : null;
  return i === 0 ? "do " + hi : i === 4 ? "od " + lo : lo + "–" + hi;
}
const legend = L.control({position:"bottomleft"});
legend.onAdd = () => {
  const div = L.DomUtil.create("div",
    "rounded-lg bg-white px-2.5 py-2 text-[11.5px]/[1.55] shadow-[0_1px_5px_rgba(0,0,0,.25)]");
  div.innerHTML = "<b>cena za m²</b><br>" + PAL.map((c,i) =>
    `<i class="mr-1.5 inline-block size-[11px] rounded-full align-[-1px]"
      style="background:${c}"></i>${bucketLabel(i)}`).join("<br>");
  return div;
};
legend.addTo(map);
/* ---------- warstwy: granice miasta i dzielnic, ogłoszenia według ceny za m², punkty (lodziarnie, markety) ---------- */
// każda warstwa włączana i wyłączana w całości; ikona w panelu = ikona markerów
const dot = c => `<i class="inline-block size-[11px] rounded-full" style="background:${c}"></i>`;
// ogłoszenia rozłożone na warstwy według przedziału ceny za m² (rebuildMarkers)
const priceLayers = [...PAL, NOPRICE].map(() => L.layerGroup());
const OVERLAYS = [{
  label: "Granice Krakowa",
  visible: true, panel: "base",
  swatch: `<i class="inline-block h-0 w-[18px] border-t-[2.5px] border-[#2563eb]"></i>`,
  // sam obwód, nieklikalny, żeby nie zasłaniał markerów
  layer: L.polygon(KRAKOW_BOUNDARY,
    {color:"#2563eb", weight:2.5, opacity:.85, fill:false, interactive:false}),
}, ...priceLayers.map((layer, i) => ({
  label: i < 5 ? `${bucketLabel(i)} zł/m²` : "brak ceny za m²",
  group: "Ceny mieszkań", visible: true, layer, swatch: dot(PAL[i] ?? NOPRICE),
}))];
for(const def of JSON.parse(document.getElementById("pois").textContent)){
  // logo dostaje białą podkładkę z cieniem, rysunek ("plain") idzie na mapę bez tła,
  // z cieniem po obrysie, żeby nie zlewał się z kafelkami
  const plate = {circle: "rounded-full", square: "rounded-[5px]"}[def.shape];
  const icon = L.divIcon({className: "", iconSize: [26, 26], iconAnchor: [13, 13], popupAnchor: [0, -13],
    html: `<img src="${def.icon}" alt="" class="size-[26px] object-contain ${plate
      ? `${plate} border-2 border-white bg-white shadow-[0_1px_4px_rgba(0,0,0,.4)]`
      : "drop-shadow-[0_1px_2px_rgba(0,0,0,.5)]"}">`});
  // punkt bez nazwy (zdarza się w danych OSM) ma w dymku sam adres zamiast pustego nagłówka
  const group = L.layerGroup(def.pts.map(([lat, lon, name, addr, hours]) =>
    L.marker([lat, lon], {icon, title: name, riseOnHover: true})
      .bindPopup(`<b>${esc(name || addr || "bez nazwy")}</b>` + (name && addr ? `<br>${esc(addr)}` : "") +
        (hours ? `<br><span class="text-mut">godziny: ${esc(hours === "closed" ? "zamknięte" : hours)}</span>` : ""))));
  OVERLAYS.push({label: `${def.label} (${def.pts.length})`, group: def.group, visible: def.visible, layer: group,
    swatch: `<img src="${def.icon}" alt="" class="size-[18px] object-contain ${
      plate ? `bg-white ${def.shape === "square" ? "rounded-[3px]" : "rounded-full"}` : ""}">`});
}
// granice dzielnic (18 obrysów): własna sekcja panelu, domyślnie wszystkie wyłączone;
// obwód nieklikalny, żeby nie przechwytywał kliknięć w markery, a w środku dzielnicy
// jej nazwa (podpis też nieklikalny, z białą obwódką, żeby był czytelny na mapie)
for(const def of JSON.parse(document.getElementById("distdata").textContent)){
  const border = L.polygon(def.rings, {color:"#7c3aed", weight:2, opacity:.8,
    fillColor:"#7c3aed", fillOpacity:.05, interactive:false});
  const name = L.marker(def.c, {interactive:false, keyboard:false, icon: L.divIcon({
    className: "", iconSize: [0, 0], html: `<span class="dlabel">${esc(def.name)}</span>`})});
  OVERLAYS.push({label: `${def.nr} — ${def.name}`, group: "Granice dzielnic", visible: false,
    panel: "base", layer: L.layerGroup([border, name]),
    swatch: `<i class="inline-block h-0 w-[18px] border-t-2 border-[#7c3aed]"></i>`});
}
OVERLAYS.filter(o => o.visible).forEach(o => o.layer.addTo(map));
// w panelu najpierw warstwy bez grupy, potem sekcje grup w kolejności pojawienia się;
// pole przy nagłówku sekcji włącza lub wyłącza wszystkie jej warstwy naraz,
// a strzałka obok zwija i rozwija jej listę (pole zostaje, więc po zwinięciu
// nadal widać, czy sekcja jest włączona w całości, czy tylko częściowo)
const overlayGroups = [...new Set(OVERLAYS.map(o => o.group ?? null))]
  .sort((a, b) => (a !== null) - (b !== null));
// sekcja, w której wszystkie warstwy są wyłączone (jak granice dzielnic), zaczyna zwinięta
const panelOf = o => o.panel ?? "layers";
const overlayFolded = g => g !== null && OVERLAYS.filter(o => o.group === g).every(o => !o.visible);
const overlayRow = o => `<label class="chk py-0.5 text-ink">
  <input type="checkbox" data-i="${OVERLAYS.indexOf(o)}" ${o.visible ? "checked" : ""}>
  <span class="grid w-[18px] place-items-center">${o.swatch}</span>${esc(o.label)}</label>`;
const overlayHeader = (g, gi) => `<div class="mt-2 mb-1 flex items-center gap-1 border-t border-line pt-2">
  <label class="chk f-title mb-0 grow"><input type="checkbox" data-g="${gi}"> ${esc(g)}</label>
  <button type="button" data-fold="${gi}" title="Zwiń lub rozwiń" aria-expanded="${!overlayFolded(g)}"
    class="-mr-1 grid size-5 shrink-0 -rotate-90 cursor-pointer place-items-center rounded text-mut
      transition-transform hover:bg-acc-soft hover:text-ink aria-expanded:rotate-0">
    <svg viewBox="0 0 24 24" class="size-3.5" fill="none" stroke="currentColor" stroke-width="2.5"
      stroke-linecap="round" stroke-linejoin="round"><path d="m6 9 6 6 6-6"/></svg></button></div>`;
const baseRows = `<div class="flex gap-2 py-0.5">` + BASEMAPS.map((b, i) => `<label class="bmap">
  <input type="radio" name="basemap" data-b="${i}" class="sr-only" ${i ? "" : "checked"}>
  <img src="${b.thumb}" alt=""><span>${esc(b.label)}</span></label>`).join("") + `</div>`;
// warstwy panelu „Podkład i granice” (granice miasta i dzielnic) kontra reszta w „Warstwach”;
// numery grup zostają globalne, bo po nich panel odnajduje swoje pola i sekcje
const overlaySections = which => overlayGroups
  .map(g => [g, OVERLAYS.filter(o => (o.group ?? null) === g && panelOf(o) === which)])
  .filter(([, rows]) => rows.length)
  .map(([g, rows]) => (g === null ? "" : overlayHeader(g, overlayGroups.indexOf(g))) +
    `<div data-rows="${overlayGroups.indexOf(g)}" ${overlayFolded(g) ? "hidden" : ""}>` +
    rows.map(overlayRow).join("") + "</div>")
  .join("");
// przycisk w prawym górnym rogu mapy rozwijający panel; otwarcie jednego panelu
// zamyka pozostałe, kliknięcie w mapę zamyka wszystkie; setup(panel) podpina obsługę
const mapPanels = [];
function mapPanel(title, icon, body, setup){
  const ctl = L.control({position:"topright"});
  ctl.onAdd = () => {
    const div = L.DomUtil.create("div", "relative");
    div.innerHTML = `<button type="button" title="${esc(title)}" aria-expanded="false"
        class="grid size-10 cursor-pointer place-items-center rounded-full bg-white text-ink
          shadow-[0_1px_5px_rgba(0,0,0,.3)] hover:bg-acc-soft on:bg-acc on:text-white">${icon}</button>
      <div hidden class="absolute top-12 right-0 max-h-[min(70vh,calc(100vh-8rem))] w-max overflow-y-auto
        rounded-lg bg-white px-3 py-2 shadow-[0_1px_5px_rgba(0,0,0,.25)]">${body}</div>`;
    L.DomEvent.disableClickPropagation(div);
    L.DomEvent.disableScrollPropagation(div);
    const btn = div.querySelector("button"), panel = div.querySelector("div");
    const setOpen = open => {
      panel.hidden = !open;
      btn.classList.toggle("on", open);
      btn.setAttribute("aria-expanded", open);
      // otwarty panel nad przyciskami kolejnych kontrolek (Leaflet daje im ten sam z-index)
      div.style.zIndex = open ? 1000 : "";
    };
    mapPanels.push(setOpen);
    btn.addEventListener("click", () => {
      const open = panel.hidden;
      mapPanels.forEach(f => f(false));
      setOpen(open);
    });
    map.on("click", () => setOpen(false));
    setup(panel);
    return div;
  };
  ctl.addTo(map);
}
// obsługa wspólna dla obu paneli z warstwami; każda warstwa jest tylko w jednym z nich,
// więc panel dotyka wyłącznie swoich pól (pozostałe po prostu w nim nie istnieją)
const overlayPanel = panel => {
  const setVisible = (i, on) => {
    panel.querySelector(`input[data-i="${i}"]`).checked = on;
    on ? OVERLAYS[i].layer.addTo(map) : OVERLAYS[i].layer.remove();
  };
  // pole sekcji: zaznaczone, gdy wszystkie warstwy włączone, „częściowe”, gdy tylko niektóre
  const syncGroups = () => panel.querySelectorAll("input[data-g]").forEach(box => {
    const g = overlayGroups[+box.dataset.g];
    const on = OVERLAYS.filter(o => o.group === g)
      .map(o => panel.querySelector(`input[data-i="${OVERLAYS.indexOf(o)}"]`).checked);
    box.checked = on.every(Boolean);
    box.indeterminate = !box.checked && on.some(Boolean);
  });
  panel.addEventListener("change", e => {
    const {i, g, b} = e.target.dataset;
    if(b != null) BASEMAPS.forEach((m, j) => j === +b ? m.layer.addTo(map) : m.layer.remove());
    else if(g != null) OVERLAYS.forEach((o, j) => o.group === overlayGroups[+g] && setVisible(j, e.target.checked));
    else setVisible(+i, e.target.checked);
    syncGroups();
  });
  panel.addEventListener("click", e => {
    const btn = e.target.closest("[data-fold]");
    if(!btn) return;
    const open = btn.getAttribute("aria-expanded") === "false";
    btn.setAttribute("aria-expanded", open);
    panel.querySelector(`[data-rows="${btn.dataset.fold}"]`).hidden = !open;
  });
  syncGroups();
};
mapPanel("Warstwy na mapie",
  `<svg viewBox="0 0 24 24" class="size-5" fill="none" stroke="currentColor" stroke-width="2"
    stroke-linejoin="round"><path d="M12 3 2 8l10 5 10-5z"/><path d="m2 13 10 5 10-5"/></svg>`,
  `<h3 class="f-title">Warstwy</h3>${overlaySections("layers")}`,
  overlayPanel);
// podkład (mapa albo zdjęcia lotnicze) razem z granicami, bo to też rysunek tła
mapPanel("Podkład i granice",
  `<svg viewBox="0 0 24 24" class="size-5" fill="none" stroke="currentColor" stroke-width="2"
    stroke-linejoin="round"><path d="m3 6 6-3 6 3 6-3v15l-6 3-6-3-6 3z"/><path d="M9 3v15M15 6v15"/></svg>`,
  `<h3 class="f-title">Podkład</h3>${baseRows}
   <h3 class="f-title mt-2 border-t border-line pt-2">Granice</h3>${overlaySections("base")}`,
  overlayPanel);

/* ---------- komunikacja miejska: przystanki z rozkładów GTFS ---------- */
// każdy rodzaj (autobusy, tramwaje) ma dwie grupy markerów: przystanki zagregowane
// (jeden punkt na przystanek) i pojedyncze słupki; na mapie jest tylko wybrana,
// a przy „tylko po przybliżeniu” dopiero od wybranego poziomu przybliżenia
const TRANSIT_DATA = JSON.parse(document.getElementById("transit").textContent);
const TRANSIT = TRANSIT_DATA.layers;
// filtry przystanków: minLines — co najmniej tyle linii (liczba z ikony), minDepartures —
// co najmniej tyle odjazdów łącznie w dniu odniesienia (liczba z dymku)
const transitOpt = {grouped: true, zoomOnly: true, minZoom: 15, minLines: 1, minDepartures: 0};
// progi suwaka odjazdów: na przystankach jest od kilku do ponad 2000 odjazdów dziennie,
// więc liniowy suwak byłby nieużywalny przy małych wartościach
const DEPARTURE_STEPS = [0, 10, 20, 50, 100, 150, 200, 300, 500, 750, 1000, 1500, 2000];
// polska forma liczebnika: 1 linia; 2–4, 22–24… linie; 5–21, 25… linii
const plural = (n, one, few, many) =>
  n === 1 ? one : n % 10 >= 2 && n % 10 <= 4 && (n % 100 < 12 || n % 100 > 14) ? few : many;
const departuresText = n => `${n} ${plural(n, "odjazd", "odjazdy", "odjazdów")}`;
// odjazdy liczone dla jednego dnia roboczego z rozkładu (gtfs.reference_day)
const transitDay = TRANSIT_DATA.day
  ? new Date(TRANSIT_DATA.day + "T12:00").toLocaleDateString("pl-PL", {weekday: "long", day: "numeric", month: "numeric"})
  : "";
// grupy linii w popupie przystanku: od najczęściej kursujących; ostatnia to linie,
// które zatrzymują się tu według rozkładu, ale w dniu odniesienia nie kursują
const FREQ_GROUPS = [
  {min: 100, label: "100 i więcej odjazdów"},
  {min: 50, label: "50–99 odjazdów"},
  {min: 20, label: "20–49 odjazdów"},
  {min: 1, label: "1–19 odjazdów"},
  {min: 0, label: "nie kursuje tego dnia"},
];
for(const t of TRANSIT){
  const nightSet = new Set(t.night);
  t.iconUrl = n => "data:image/svg+xml;charset=utf-8," + encodeURIComponent(t.svg.replace("{n}", n));
  // jedna ikona na liczbę linii — tych liczb jest kilkadziesiąt, markerów kilka tysięcy
  const icons = new Map();
  const iconFor = n => icons.get(n) ?? icons.set(n, L.divIcon({className: "", iconSize: [28, 45],
    iconAnchor: [14, 44], popupAnchor: [0, -42], tooltipAnchor: [0, -42],
    html: `<img src="${t.iconUrl(n)}" alt="" class="h-[45px] w-7">`})).get(n);
  // numer linii z dymkiem po najechaniu: dokładna liczba odjazdów; kliknięcie rysuje trasę
  // (code: słupek albo numer przystanku, żeby wyróżnić kierunek, który się tu zatrzymuje)
  const chip = code => ([i, n]) => {
    const night = nightSet.has(i);
    const cls = !n ? "bg-page text-mut" : night ? "bg-ink text-white" : "bg-acc-soft text-acc";
    return `<span data-route="${esc(t.id)}" data-line="${esc(t.lines[i])}" data-code="${esc(code)}"
        class="group relative inline-block min-w-7 cursor-pointer rounded px-1.5 py-px text-center
        text-[12.5px] font-semibold hover:ring-1 hover:ring-current ${cls}">${esc(t.lines[i])}<span class="pointer-events-none absolute bottom-full
        left-1/2 z-10 mb-1 hidden -translate-x-1/2 rounded bg-ink px-1.5 py-0.5 text-[11.5px] font-normal
        whitespace-nowrap text-white shadow group-hover:block">linia ${esc(t.lines[i])}${night ? " (nocna)" : ""}:
        ${departuresText(n)}</span></span>`;
  };
  const marker = (lat, lon, name, code, post, deps) => {
    const night = deps.filter(([i]) => nightSet.has(i)).length, day = deps.length - night;
    const total = deps.reduce((sum, [, n]) => sum + n, 0);
    const title = `<b>${esc(name)}</b>` + (post ? ` <span class="text-mut">słupek ${esc(post)}</span>` : "");
    const sorted = [...deps].sort((a, b) => b[1] - a[1]);
    const groups = FREQ_GROUPS.map((g, k) => {
      const upper = k ? FREQ_GROUPS[k - 1].min : Infinity;
      const inGroup = sorted.filter(([, n]) => n >= g.min && n < upper);
      return inGroup.length ? `<div><div class="f-title mb-0.5">${g.label} (${inGroup.length})</div>
        <div class="flex flex-wrap gap-1">${inGroup.map(chip(code)).join("")}</div></div>` : "";
    }).join("");
    const m = L.marker([lat, lon], {icon: iconFor(deps.length), riseOnHover: true});
    m.lineCount = deps.length;
    m.departures = total;
    return m
      .bindTooltip(`${title}<br>${deps.length} ${plural(deps.length, "linia", "linie", "linii")}: ` +
        `${day} ${plural(day, "dzienna", "dzienne", "dziennych")}, ` +
        `${night} ${plural(night, "nocna", "nocne", "nocnych")}` +
        `<br><span class="text-mut">${departuresText(total)} (${transitDay})</span>`,
        {direction: "top"})
      .bindPopup(`${title}<div class="text-[11.5px] text-mut">odjazdy: ${transitDay}` +
        (post ? "" : ", wszystkie słupki") + `</div>
        <div class="mt-1.5 w-60 space-y-1.5">${groups}</div>
        <div class="mt-1.5 text-[11px] text-mut">kliknij numer linii, żeby zobaczyć trasę</div>`);
  };
  t.on = false;
  // wszystkie markery; do warstw na mapie trafiają tylko te, które przechodzą filtr (filterTransit)
  t.stopMarkers = t.stops.map(([lat, lon, name, number, deps]) => marker(lat, lon, name, number, null, deps));
  t.postMarkers = t.posts.map(([lat, lon, name, code, deps]) =>
    marker(lat, lon, name, code, code.split("-").pop(), deps));
  t.grouped = L.layerGroup();
  t.single = L.layerGroup();
}
const stopFilterOk = m => m.lineCount >= transitOpt.minLines && m.departures >= transitOpt.minDepartures;
function filterTransit(){
  for(const t of TRANSIT){
    for(const [layer, all] of [[t.grouped, t.stopMarkers], [t.single, t.postMarkers]]){
      layer.clearLayers();
      all.filter(stopFilterOk).forEach(m => layer.addLayer(m));
    }
  }
}
filterTransit();

/* ---------- trasa linii po kliknięciu jej numeru w popupie przystanku ---------- */
// przebiegi tras są w osobnym skrypcie w .cache/gtfs/ (gtfs.write_routes), dołączanym
// dopiero przy pierwszym kliknięciu — <script src>, bo z pliku file:// fetch() jest blokowany
let routesLoading = null;
function loadRoutes(){
  if(window.TRANSIT_ROUTES) return Promise.resolve(window.TRANSIT_ROUTES);
  routesLoading ??= new Promise((resolve, reject) => {
    if(!TRANSIT_DATA.routes) return reject();
    const script = document.createElement("script");
    script.src = TRANSIT_DATA.routes;
    script.onload = () => window.TRANSIT_ROUTES ? resolve(window.TRANSIT_ROUTES) : reject();
    script.onerror = () => { script.remove(); reject(); };
    document.head.append(script);
  }).catch(() => { routesLoading = null; throw new Error("brak danych tras"); });
  return routesLoading;
}
// złota trasa z ciemniejszym obrysem — odcina się od żółto-pomarańczowych dróg na kafelkach
const ROUTE_COLOR = "#e0a800", ROUTE_CASING = "#6b4f00", ROUTE_TEXT = "#9a7200";
const routeLayer = L.layerGroup().addTo(map);
let shownRoute = null;  // "rodzaj:linia"
const routeBox = L.control({position: "topleft"});
routeBox.onAdd = () => {
  const div = L.DomUtil.create("div", "hidden max-w-72 rounded-lg bg-white px-3 py-2 text-[12.5px] shadow-[0_1px_5px_rgba(0,0,0,.25)]");
  L.DomEvent.disableClickPropagation(div);
  div.addEventListener("click", e => e.target.closest("[data-close]") && clearRoute());
  return div;
};
routeBox.addTo(map);
function showRouteBox(html){
  const box = routeBox.getContainer();
  box.innerHTML = `<button type="button" data-close title="Zamknij" class="float-right ml-2 cursor-pointer text-mut
    hover:text-ink">✕</button>${html}`;
  box.classList.remove("hidden");
}
function clearRoute(){
  routeLayer.clearLayers();
  shownRoute = null;
  routeBox.getContainer().classList.add("hidden");
}
async function toggleRoute(kind, line, code){
  if(shownRoute === `${kind}:${line}`) return clearRoute();
  clearRoute();
  shownRoute = `${kind}:${line}`;
  showRouteBox(`<b>linia ${esc(line)}</b> <span class="text-mut">wczytuję trasę…</span>`);
  let routes;
  try {
    routes = await loadRoutes();
  } catch {
    showRouteBox(`<b class="text-bad">Brak danych tras</b><br><span class="text-mut">Nie ma pliku z trasami
      (.cache/gtfs/routes.js) — mapa otwarta na innym komputerze albo dane usunięto.
      Wygeneruj mapę ponownie (--html), żeby je pobrać.</span>`);
    return;
  }
  if(shownRoute !== `${kind}:${line}`) return;  // w międzyczasie kliknięto inną linię
  const variants = routes[kind]?.[line];
  if(!variants?.length){
    showRouteBox(`<b>linia ${esc(line)}</b><br><span class="text-bad">brak trasy w danych — wygeneruj mapę ponownie</span>`);
    return;
  }
  const t = TRANSIT.find(x => x.id === kind);
  const night = t.night.includes(t.lines.indexOf(line));
  // kierunki zatrzymujące się na klikniętym słupku (albo przystanku) — pozostałe bledsze
  const here = variants.map(v => v.stops.some(s => s[3] === code || s[3].split("-")[0] === code));
  const bounds = L.latLngBounds([]);
  variants.forEach((v, k) => {
    const main = here[k] || !here.some(Boolean);
    const opacity = main ? .95 : .4, weight = main ? 5 : 3;
    routeLayer.addLayer(L.polyline(v.shape, {color: ROUTE_CASING, weight: weight + 3, opacity: opacity * .6, interactive: false}));
    routeLayer.addLayer(L.polyline(v.shape, {color: ROUTE_COLOR, weight, opacity, interactive: false}));
    for(const [lat, lon] of v.stops) routeLayer.addLayer(L.circleMarker([lat, lon],
      {radius: main ? 3.5 : 2.5, color: ROUTE_CASING, weight: 2, fillColor: ROUTE_COLOR, fillOpacity: 1, opacity, interactive: false}));
    bounds.extend(v.shape);
  });
  showRouteBox(`<b style="color:${ROUTE_TEXT}">linia ${esc(line)}</b>${night ? ' <span class="text-mut">(nocna)</span>' : ""}` +
    variants.map((v, k) => `<div class="${here[k] || !here.some(Boolean) ? "" : "text-mut"}">→ ${esc(v.to)}
      <span class="text-mut">(${v.stops.length} przyst.)</span></div>`).join(""));
  map.closePopup();
  map.fitBounds(bounds, {padding: [40, 40]});
}
// klik w numer linii w popupie (popupy nie przepuszczają kliknięć do mapy, więc nasłuch na samym
// popupie — raz na element, bo ten sam popup otwiera się wielokrotnie)
const routePopups = new WeakSet();
map.on("popupopen", e => {
  const el = e.popup.getElement();
  if(routePopups.has(el)) return;
  routePopups.add(el);
  el.addEventListener("click", ev => {
    const chip = ev.target.closest("[data-route]");
    if(chip) toggleRoute(chip.dataset.route, chip.dataset.line, chip.dataset.code);
  });
});
function syncTransit(){
  const zoomOk = !transitOpt.zoomOnly || map.getZoom() >= transitOpt.minZoom;
  for(const t of TRANSIT){
    for(const [layer, grouped] of [[t.grouped, true], [t.single, false]]){
      const want = t.on && zoomOk && transitOpt.grouped === grouped;
      if(want !== map.hasLayer(layer)) want ? layer.addTo(map) : layer.remove();
    }
  }
}
map.on("zoomend", syncTransit);
if(TRANSIT.length) mapPanel("Komunikacja miejska",
  `<svg viewBox="0 0 24 24" class="size-5" fill="none" stroke="currentColor" stroke-width="2"
    stroke-linecap="round" stroke-linejoin="round"><rect x="5" y="3" width="14" height="15" rx="3"/>
    <path d="M5 11h14M9 6.5h6M7.5 18v2.5M16.5 18v2.5"/></svg>`,
  `<h3 class="f-title">Komunikacja miejska</h3>` +
  TRANSIT.map((t, i) => `<label class="chk py-0.5 text-ink"><input type="checkbox" data-t="${i}">
    <span class="grid w-[18px] place-items-center"><img src="${t.iconUrl("")}" alt="" class="h-[18px] w-3"></span>
    <span>${esc(t.label)} (<span data-count="${i}">${t.stops.length}</span>)</span></label>`).join("") +
  `<h3 class="f-title mt-2 border-t border-line pt-2">Pokazuj</h3>
  <nav class="flex gap-1 rounded-lg bg-page p-1">
    <button type="button" class="tab on grow" data-grouped="1">przystanki</button>
    <button type="button" class="tab grow" data-grouped="0">każdy słupek</button></nav>
  <label class="chk mt-2 text-ink"><input type="checkbox" data-opt="zoomOnly" checked>
    tylko po przybliżeniu</label>
  <label class="mt-1 flex items-center gap-2 text-[12.5px] text-mut">od
    <input type="range" data-opt="minZoom" min="11" max="18" step="1" value="${transitOpt.minZoom}"
      class="w-32 accent-acc disabled:opacity-40">
    <b class="w-4 text-ink" data-zoomval>${transitOpt.minZoom}</b></label>
  <p class="mt-0.5 text-[11.5px] text-mut">mapa teraz: <span data-zoomnow>${map.getZoom()}</span></p>
  <h3 class="f-title mt-2 border-t border-line pt-2">Filtr przystanków</h3>
  <label class="flex items-center gap-2 text-[12.5px] text-mut"><span class="w-24">min. linii</span>
    <input type="range" data-opt="minLines" min="1" step="1" value="1"
      max="${Math.max(1, ...TRANSIT.flatMap(t => t.stopMarkers.map(m => m.lineCount)))}" class="w-32 accent-acc">
    <b class="w-8 text-ink" data-val="minLines"></b></label>
  <label class="mt-1 flex items-center gap-2 text-[12.5px] text-mut"><span class="w-24">min. odjazdów</span>
    <input type="range" data-opt="minDepartures" min="0" max="${DEPARTURE_STEPS.length - 1}" step="1" value="0"
      class="w-32 accent-acc">
    <b class="w-8 text-ink" data-val="minDepartures"></b></label>
  <p class="mt-0.5 text-[11.5px] text-mut">odjazdy łącznie: ${esc(transitDay)}</p>`,
  panel => {
    const slider = panel.querySelector('[data-opt="minZoom"]');
    const refresh = () => {
      slider.disabled = !transitOpt.zoomOnly;
      panel.querySelector("[data-zoomval]").textContent = transitOpt.minZoom;
      panel.querySelectorAll("[data-grouped]").forEach(b =>
        b.classList.toggle("on", (b.dataset.grouped === "1") === transitOpt.grouped));
      panel.querySelector('[data-val="minLines"]').textContent = transitOpt.minLines;
      panel.querySelector('[data-val="minDepartures"]').textContent = transitOpt.minDepartures;
      TRANSIT.forEach((t, i) => panel.querySelector(`[data-count="${i}"]`).textContent =
        (transitOpt.grouped ? t.grouped : t.single).getLayers().length);
      syncTransit();
    };
    map.on("zoomend", () => panel.querySelector("[data-zoomnow]").textContent = map.getZoom());
    panel.addEventListener("click", e => {
      const b = e.target.closest("[data-grouped]");
      if(!b) return;
      transitOpt.grouped = b.dataset.grouped === "1";
      refresh();
    });
    panel.addEventListener("input", e => {
      const {t, opt} = e.target.dataset;
      if(t != null) TRANSIT[+t].on = e.target.checked;
      else if(opt === "zoomOnly") transitOpt.zoomOnly = e.target.checked;
      else if(opt === "minZoom") transitOpt.minZoom = +e.target.value;
      else if(opt === "minLines" || opt === "minDepartures"){
        transitOpt[opt] = opt === "minLines" ? +e.target.value : DEPARTURE_STEPS[+e.target.value];
        filterTransit();
      }
      refresh();
    });
    refresh();
  });
const markers = new Map();
let selId = null;
function baseStyle(o){
  return {radius:6, weight:1, color:"#fff", fillColor:colorOf(o.pm),
          fillOpacity:.88};
}
function rebuildMarkers(list){
  priceLayers.forEach(l => l.clearLayers()); markers.clear();
  for(const o of list){
    if(o.lat == null) continue;
    const m = L.circleMarker([o.lat, o.lon], baseStyle(o))
      .on("click", () => openDetail(o.id, true))
      .bindTooltip(`${esc(o.t)}<br><b>${fmtP(o.p)}</b>` +
        (o.pm ? ` · ${fmtN(o.pm)} zł/m²` : ""), {direction:"top", opacity:.94});
    m.addTo(priceLayers[bucketOf(o.pm)]);
    markers.set(o.id, m);
  }
  if(selId != null) highlight(selId);
}
function highlight(id){
  if(selId != null && markers.has(selId)){
    const prev = OFFERS.find(x => x.id === selId);
    markers.get(selId).setStyle(baseStyle(prev));
  }
  selId = id;
  const m = markers.get(id);
  if(m){ m.setStyle({radius:10, weight:3, color:"#17233b"}); m.bringToFront(); }
}

/* ---------- filtry ---------- */
const districts = {};
for(const o of OFFERS){
  const d = o.d || "(nie podano)";
  districts[d] = (districts[d] || 0) + 1;
}
$("#districts").innerHTML = Object.entries(districts)
  .sort((a,b) => b[1]-a[1])
  .map(([d,n]) => `<label class="flex cursor-pointer items-center gap-1.5 py-0.5">
     <input type="checkbox" class="dbox" value="${esc(d)}" checked> ${esc(d)}
     <span class="ml-auto text-[11.5px] text-mut">${n}</span></label>`).join("");
const num = id => { const v = $(id).value.trim(); return v === "" ? null : +v; };
/* wyszukiwarka: komórki z frazami, każda kolejna z łącznikiem I / LUB / NIE.
   I wiąże mocniej niż LUB: „a I b LUB c” = (a i b) albo c. Frazy z NIE wykluczają
   ogłoszenie niezależnie od pozostałych. Puste komórki są pomijane. */
function currentQuery(){
  const any = [[]], not = [];  // any: alternatywa grup, w których muszą wystąpić wszystkie frazy
  for(const row of document.querySelectorAll("#qrows .qrow")){
    const w = row.querySelector("input").value.trim().toLowerCase();
    const op = row.querySelector("select")?.value ?? "and";
    if(!w) continue;
    if(op === "not"){ not.push(w); continue; }
    if(op === "or" && any.at(-1).length) any.push([]);
    any.at(-1).push(w);
  }
  return {any: any.filter(g => g.length), not};
}
function addQueryRow(){
  const row = document.createElement("div");
  row.className = "qrow flex items-center";
  row.innerHTML = `<select title="Łącznik z poprzednimi frazami" class="field w-auto cursor-pointer
      rounded-r-none border-r-0 bg-page px-1 text-[12.5px] font-semibold">
      <option value="and">I</option><option value="or">LUB</option><option value="not">NIE</option></select>
    <input type="text" placeholder="fraza" class="field w-36 rounded-none bg-[#fbfcfd]">
    <button type="button" title="Usuń frazę" class="field w-auto cursor-pointer rounded-l-none border-l-0
      px-2 text-mut hover:text-bad">×</button>`;
  row.querySelector("button").onclick = () => { row.remove(); apply(); };
  $("#qadd").before(row);
  row.querySelector("input").focus();
}
/* ---------- suwak dwustronny: rok budowy ---------- */
/* Rok budowy podają tylko Otodom i Gratka — OLX go nie zbiera, a i tam trafiają
   się oferty bez niego. Krańce suwaka biorą się z ofert, które rok mają;
   dopóki stoi rozsunięty do końca, filtr nie działa. Po zawężeniu oferty bez
   roku odpadają — tak samo jak przy cenie czy metrażu. */
const YEARS = OFFERS.map(o => o.by).filter(v => v != null).sort((a,b) => a-b);
const YMAX = YEARS.length ? YEARS[YEARS.length-1] : 0;
/* Lewy kraniec to 1. percentyl zaokrąglony w dół do dziesięciolecia, a nie
   najstarsza oferta: na Starówce trafiają się kamienice z XIV w. i przy skali
   liniowej zjadłyby prawie całą długość suwaka, zostawiając ostatnie kilka
   procent na lata, w których leży 90% ofert. Uchwyt dosunięty do lewego krańca
   znaczy „bez dolnej granicy”, więc te najstarsze oferty i tak są w wynikach —
   znikają dopiero wtedy, gdy dolną granicę świadomie podniesiemy. */
const YTRUE = YEARS.length ? YEARS[0] : 0;
const YMIN = YEARS.length
  ? Math.max(YTRUE, Math.floor(YEARS[Math.floor(YEARS.length*.01)] / 10) * 10) : 0;
const yLoEl = $("#ymin"), yHiEl = $("#ymax");
const yearOn = () => YEARS.length && (+yLoEl.value > YMIN || +yHiEl.value < YMAX);
function yearPaint(){
  const lo = +yLoEl.value, hi = +yHiEl.value, span = (YMAX - YMIN) || 1;
  $("#yfill").style.left = (lo - YMIN) / span * 100 + "%";
  $("#yfill").style.width = (hi - lo) / span * 100 + "%";
  // na lewym krańcu podpis pokazuje rok najstarszej oferty, bo tyle wtedy obejmuje
  $("#ylo").textContent = lo > YMIN ? lo : YTRUE;
  $("#yhi").textContent = hi;
}
function yearReset(){
  yLoEl.value = YMIN; yHiEl.value = YMAX; yearPaint();
}
if(YEARS.length){
  $("#ybox").hidden = false;
  for(const el of [yLoEl, yHiEl]){ el.min = YMIN; el.max = YMAX; }
  yearReset();
  const without = OFFERS.length - YEARS.length;
  // o OLX wspominamy tylko wtedy, gdy faktycznie jest w tej bazie
  const olx = OFFERS.some(o => o.s === "olx" && o.by == null) ? ", w tym całe OLX" : "";
  $("#ynote").textContent = without
    ? `${without} z ${OFFERS.length} ofert nie podaje roku${olx} — po zawężeniu znikają z wyników`
    : "";
  for(const el of [yLoEl, yHiEl])
    el.addEventListener("input", () => {
      // uchwyty mogą się minąć — ten przesuwany spycha drugi przed sobą
      if(+yLoEl.value > +yHiEl.value)
        (el === yLoEl ? yHiEl : yLoEl).value = el.value;
      yearPaint(); soon();
    });
  // gdy uchwyty stoją na sobie, na wierzch idzie ten bliższy kliknięciu —
  // inaczej skrajnej wartości nie dałoby się już ruszyć
  $("#yrange").addEventListener("pointerdown", e => {
    const r = e.currentTarget.getBoundingClientRect();
    const at = YMIN + (e.clientX - r.left) / r.width * (YMAX - YMIN);
    const hi = Math.abs(at - +yHiEl.value) <= Math.abs(at - +yLoEl.value);
    yHiEl.style.zIndex = hi ? 3 : 2;
    yLoEl.style.zIndex = hi ? 2 : 3;
  });
}
function currentFilter(){
  const roomsOn = [...document.querySelectorAll("#rooms .chip.on")]
    .map(b => +b.dataset.r);
  const boxes = [...document.querySelectorAll(".dbox")];
  const dsel = new Set(boxes.filter(b => b.checked).map(b => b.value));
  const allD = dsel.size === boxes.length;
  const srcOn = [...document.querySelectorAll("#portals .chip.on")]
    .map(b => b.dataset.s);
  const q = currentQuery();
  const inDesc = $("#qdesc").checked;
  const freshDays = $("#fresh").value ? +$("#fresh").value : null;
  const chgDays = $("#pchg").value ? +$("#pchg").value : null;
  const bounds = $("#bounds").checked ? map.getBounds() : null;
  const yOn = yearOn(), yLo = +yLoEl.value, yHi = +yHiEl.value;
  return o => {
    if(srcOn.length < 2 && !srcOn.includes(o.s)) return false;
    const hit = w => o._txt.includes(w) || (inDesc && o._dtxt.includes(w));
    if(q.any.length && !q.any.some(g => g.every(hit))) return false;
    if(q.not.some(hit)) return false;
    const pmin=num("#pmin"), pmax=num("#pmax");
    if(pmin != null && (o.p == null || o.p < pmin)) return false;
    if(pmax != null && (o.p == null || o.p > pmax)) return false;
    const amin=num("#amin"), amax=num("#amax");
    if(amin != null && (o.a == null || o.a < amin)) return false;
    if(amax != null && (o.a == null || o.a > amax)) return false;
    const mmin=num("#mmin"), mmax=num("#mmax");
    if(mmin != null && (o.pm == null || o.pm < mmin)) return false;
    if(mmax != null && (o.pm == null || o.pm > mmax)) return false;
    if(yOn && (o.by == null || (yLo > YMIN && o.by < yLo) || (yHi < YMAX && o.by > yHi))) return false;
    if(roomsOn.length && (o._rb == null || !roomsOn.includes(o._rb))) return false;
    if($("#market").value && o.mk !== $("#market").value) return false;
    if($("#seller").value !== "" && String(o.b) !== $("#seller").value) return false;
    if(freshDays != null){
      const t = o.c || o.fs;
      if(!t || (GEN - new Date(t)) > freshDays*864e5) return false;
    }
    // oferta bez „pc” nigdy nie zmieniła ceny, więc odpada przy każdym oknie
    if(chgDays != null && (!o.pc || (GEN - new Date(o.pc)) > chgDays*864e5)) return false;
    if(!allD && !dsel.has(o.d || "(nie podano)")) return false;
    if($("#onlydrop").checked && !(o._drop > 0)) return false;
    if($("#onlygeo").checked && o.lat == null) return false;
    if(bounds && (o.lat == null || !bounds.contains([o.lat, o.lon]))) return false;
    return true;
  };
}
const SORTS = {
  "new": (a,b) => new Date(b.c || b.fs || 0) - new Date(a.c || a.fs || 0),
  "old": (a,b) => new Date(a.c || a.fs || 8.64e15) - new Date(b.c || b.fs || 8.64e15),
  "pm-asc": (a,b) => (a.pm ?? 9e9) - (b.pm ?? 9e9),
  "pm-desc": (a,b) => (b.pm ?? -1) - (a.pm ?? -1),
  "p-asc": (a,b) => (a.p ?? 9e9) - (b.p ?? 9e9),
  "p-desc": (a,b) => (b.p ?? -1) - (a.p ?? -1),
  "a-desc": (a,b) => (b.a ?? -1) - (a.a ?? -1),
  "a-asc": (a,b) => (a.a ?? 9e9) - (b.a ?? 9e9),
  "drop": (a,b) => b._drop - a._drop,
  // przy tej samej liczbie zmian wyżej te, których cena zmieniła się ostatnio
  "changes": (a,b) => (b.h?.length ?? 0) - (a.h?.length ?? 0)
    || (b.h?.at(-1)[0] ?? "").localeCompare(a.h?.at(-1)[0] ?? ""),
};
let filtered = [], shown = 0;
const CHUNK = 80;
function apply(refitMarkers = true){
  const pass = currentFilter();
  filtered = OFFERS.filter(pass).sort(SORTS[$("#sort").value]);
  $("#cnt").textContent = `${filtered.length} z ${OFFERS.length}`;
  renderStats(); renderHisto();
  if(refitMarkers) rebuildMarkers(filtered);
  $("#cards").scrollTop = 0;
  shown = 0; $("#cards").innerHTML = ""; renderMore();
}
function median(arr){
  if(!arr.length) return null;
  const s = [...arr].sort((a,b)=>a-b), m = s.length >> 1;
  return s.length % 2 ? s[m] : (s[m-1]+s[m])/2;
}
function renderStats(){
  const ps = filtered.map(o=>o.p).filter(v=>v!=null);
  const ms = filtered.map(o=>o.pm).filter(v=>v!=null);
  const drops = filtered.filter(o=>o._drop>0).length;
  const tile = (v, label) => `<div class="rounded-lg bg-acc-soft px-2 py-1.5">
    <b class="block text-[15px]">${v}</b><span class="text-[11px] text-mut">${label}</span></div>`;
  $("#stats").innerHTML =
    tile(filtered.length, "ofert po filtrach") +
    tile(fmtN(median(ms)), "mediana zł/m²") +
    tile(fmtN(median(ps)), "mediana ceny [zł]") +
    tile(drops, "z obniżką ceny");
}
function renderHisto(){
  const cv = $("#histo"), ctx = cv.getContext("2d");
  const W = cv.width = cv.clientWidth * devicePixelRatio;
  const H = cv.height = cv.clientHeight * devicePixelRatio;
  ctx.clearRect(0,0,W,H);
  const vals = filtered.map(o=>o.pm).filter(v=>v!=null);
  if(vals.length < 3) return;
  const lo = ppmAll[Math.floor(ppmAll.length*.02)] || Math.min(...vals);
  const hi = ppmAll[Math.floor(ppmAll.length*.98)] || Math.max(...vals);
  const NB = 28, bins = new Array(NB).fill(0);
  for(const v of vals){
    const i = Math.max(0, Math.min(NB-1, Math.floor((v-lo)/(hi-lo)*NB)));
    bins[i]++;
  }
  const top = Math.max(...bins), bw = W/NB;
  for(let i=0;i<NB;i++){
    const h = bins[i]/top*(H-14*devicePixelRatio);
    const mid = lo + (i+.5)*(hi-lo)/NB;
    ctx.fillStyle = colorOf(mid);
    ctx.fillRect(i*bw+1, H-h, bw-2, h);
  }
  ctx.fillStyle = "#68758a";
  ctx.font = `${10*devicePixelRatio}px sans-serif`;
  ctx.fillText(fmtN(lo), 2, 10*devicePixelRatio);
  const t = fmtN(hi);
  ctx.fillText(t, W - ctx.measureText(t).width - 2, 10*devicePixelRatio);
}
function cardHTML(o){
  const img = o.ph.length
    ? `<img loading="lazy" src="${esc(o.ph[0])}"
        class="h-[70px] w-[92px] shrink-0 rounded-md bg-[#e8ebf0] object-cover"
        onerror="this.outerHTML='<div class=noimg>🏠</div>'">`
    : `<div class="noimg">🏠</div>`;
  const badges = (o._new ? `<span class="badge bg-[#e3f4ea] text-good">NOWA</span>` : "") +
    (o._drop > 0 ? `<span class="badge bg-[#fdecec] text-bad">-${(o._drop*100).toFixed(0)}%</span>` : "");
  const meta = [PORTAL[o.s] || o.s, o.a ? o.a + " m²" : null,
    o.pm ? fmtN(o.pm) + " zł/m²" : null,
    o.r || null, o.d || null].filter(Boolean).join(" · ");
  return `<div class="card flex cursor-pointer gap-2.5 border-b border-line px-3 py-2.5
      hover:bg-[#f7f9fc] sel:bg-acc-soft" data-id="${o.id}">${img}<div>
    <h4 class="mb-0.5 line-clamp-2 text-[13px]/[1.3] font-semibold">${esc(o.t)}${badges}</h4>
    <div class="text-[14.5px] font-bold">${fmtP(o.p)}</div>
    <div class="text-xs text-mut">${esc(meta)}</div></div></div>`;
}
function renderMore(){
  const slice = filtered.slice(shown, shown + CHUNK);
  shown += slice.length;
  const sent = $("#more"); if(sent) sent.remove();
  $("#cards").insertAdjacentHTML("beforeend", slice.map(cardHTML).join(""));
  if(shown < filtered.length)
    $("#cards").insertAdjacentHTML("beforeend",
      `<div id="more" class="p-3 text-center text-mut">… wczytuję (${shown}/${filtered.length})</div>`);
  const m = $("#more");
  if(m) io.observe(m);
}
const io = new IntersectionObserver(es => {
  if(es.some(e => e.isIntersecting)) renderMore();
});
$("#cards").addEventListener("click", e => {
  const card = e.target.closest(".card");
  if(card) openDetail(card.dataset.id, false);
});

/* ---------- szczegóły oferty ---------- */
function openDetail(id, fromMap){
  const o = OFFERS.find(x => x.id === id);
  if(!o) return;
  highlight(id);
  document.querySelectorAll(".card.sel").forEach(c => c.classList.remove("sel"));
  const card = document.querySelector(`.card[data-id="${id}"]`);
  if(card){ card.classList.add("sel");
    if(fromMap) card.scrollIntoView({block:"nearest"}); }
  if(!fromMap && o.lat != null)
    map.flyTo([o.lat, o.lon], Math.max(map.getZoom(), 15), {duration:.5});
  // zdjęcia dociągane z serwerów OLX dopiero teraz — na żądanie
  const gal = o.ph.length ? `<div id="gal" class="relative overflow-hidden rounded-[10px] bg-[#0d1117]">
      <img id="gmain" src="${esc(o.ph[0])}" class="h-80 w-full object-contain"
       onerror="this.closest('#gal').style.display='none'"></div>` +
    (o.ph.length > 1 ? `<div id="thumbs" class="flex gap-1.5 overflow-x-auto px-0.5 pt-2 pb-0.5">` +
      o.ph.map((u,i) =>
      `<img src="${esc(u)}" data-i="${i}" class="${i?"":"on"} h-[54px] w-[72px] shrink-0 cursor-pointer
        rounded-md object-cover opacity-65 on:opacity-100 on:outline-2 on:outline-acc"
        onerror="this.remove()">`).join("") + `</div>` : "")
    : "";
  const grid = [
    ["Portal", PORTAL[o.s] || o.s],
    ["Metraż", o.a ? o.a + " m²" : null],
    ["Cena za m²", o.pm ? fmtN(o.pm) + " zł" : null],
    ["Pokoje", o.r], ["Piętro", o.f],
    ["Rynek", o.mk], ["Rok budowy", o.by], ["Dzielnica", o.d],
    ["Sprzedający", o.b ? "firma / deweloper" : "osoba prywatna"],
    ["Dodane", fmtDate(o.c)],
    ["Pierwszy raz widziana", fmtDate(o.fs)],
  ].filter(x => x[1] != null)
   .map(x => `<div><span class="block text-[11.5px] text-mut">${x[0]}</span>
     <b class="font-semibold">${esc(x[1])}</b></div>`).join("");
  let hist = "";
  if(o.h){
    const td = "border-b border-dashed border-line py-1";
    const first = o.h[0][1], last = o.h[o.h.length-1][1], d = last - first;
    const pct = Math.abs(d) / first * 100;
    const total = d === 0 ? "" :
      `<small class="text-[12.5px] font-semibold ${d > 0 ? "text-bad" : "text-good"}">
        ${d > 0 ? "+" : "−"}${fmtP(Math.abs(d))}
        (${d > 0 ? "+" : "−"}${pct < .05 ? "<0,1" : pct.toFixed(1).replace(".", ",")}%)</small>`;
    hist = `<div><h3 class="my-2 flex items-baseline justify-between text-base font-bold">Historia cen ${total}</h3>
      <div id="pchart" class="h-[130px] cursor-pointer" title="Kliknij, żeby pokazać wszystkie zmiany"></div>
      <button id="ptoggle" class="cursor-pointer text-xs text-mut hover:text-ink">pokaż wszystkie zmiany (${o.h.length}) ▾</button>
      <table id="ptable" hidden class="mt-2 w-full text-[13px]">` + o.h.map((x,i) => {
      const prev = i ? o.h[i-1][1] : null;
      const diff = prev == null ? "" :
        `<span class="${x[1] > prev ? "text-bad" : "text-good"}">
          ${x[1] > prev ? "▲" : "▼"} ${fmtN(Math.abs(x[1]-prev))}</span> `;
      return `<tr><td class="${td}">${esc(x[0])}</td>
        <td class="${td} text-right">${diff}${fmtP(x[1])}</td></tr>`;
    }).join("") + `</table></div>`;
  }
  if(priceChart){ priceChart.dispose(); priceChart = null; }
  $("#dbody").innerHTML = gal +
    `<h2 class="mt-3 mb-1 text-lg/[1.3] font-bold">${esc(o.t)}</h2>
     <div class="text-[22px] font-extrabold">${fmtP(o.p)}${o.ng ?
       ` <small class="text-[13px] font-normal text-mut">do negocjacji</small>` : ""}</div>
     <div class="my-3 grid grid-cols-2 gap-x-3.5 gap-y-2 rounded-[10px] bg-[#f7f8fa] p-3">${grid}</div>` +
    hist +
    (o.rad > 0 ? `<div class="mt-2 text-xs text-mut">📍 Sprzedający podał lokalizację
       przybliżoną (±${fmtN(o.rad)} m) — pinezka wskazuje okolicę.</div>` : "") +
    (o.dsc ? `<div class="mt-3 text-[13.5px] whitespace-pre-line">${esc(o.dsc)}</div>` : "") +
    `<a href="${esc(o.u)}" target="_blank" rel="noopener"
       class="mt-4 mb-1.5 block rounded-[9px] bg-acc p-3 text-center font-semibold text-white hover:bg-acc/90">
       Otwórz ogłoszenie na ${PORTAL[o.s] || "portalu"} ↗</a>`;
  const th = $("#thumbs");
  if(th) th.addEventListener("click", e => {
    if(e.target.tagName !== "IMG") return;
    $("#gmain").src = o.ph[+e.target.dataset.i];
    th.querySelectorAll("img").forEach(x => x.classList.remove("on"));
    e.target.classList.add("on");
  });
  if(o.h) renderPriceChart($("#pchart"), o.h);
  $("#detail").classList.add("open");
  $("#backdrop").classList.add("open");
}
// wykres schodkowy historii ceny: oś czasu według dat, ostatnia cena dociągnięta
// do chwili wygenerowania pliku; kliknięcie rozwija tabelkę ze wszystkimi zmianami
let priceChart = null;
function renderPriceChart(el, h){
  const pts = h.map(x => [new Date(x[0] + "T00:00:00").getTime(), x[1]]);
  const now = Math.max(GEN.getTime(), pts.at(-1)[0] + 864e5);
  const ps = h.map(x => x[1]), first = ps[0], last = ps.at(-1);
  let lo = Math.min(...ps), hi = Math.max(...ps);
  // minimalna rozpiętość osi 3% ceny — zmiana o 1 zł nie może wyglądać jak przepaść
  if(hi - lo < hi * .03){ const m = (hi + lo) / 2, s = hi * .015; lo = m - s; hi = m + s; }
  const col = last < first ? CH.good : last > first ? CH.bad : CH.mut;
  const fmtK = v => v >= 1e6 ? (v/1e6).toFixed(2).replace(".", ",") + " mln" : Math.round(v/1000) + " tys.";
  // podpis pierwszej ceny nad punktem, ostatniej (pogrubiony) na końcu linii
  const label = first => ({show: true, fontSize: 11, color: first ? CH.mut : CH.ink,
    fontWeight: first ? 400 : 700, formatter: p => fmtK(p.value[1])});
  const data = pts.map((p, i) => ({value: p,
    itemStyle: {borderColor: !i ? CH.mut : p[1] < ps[i-1] ? CH.good : CH.bad},
    label: i ? undefined : {...label(true), position: "top", align: "left"}}));
  data.push({value: [now, last], symbol: "none"});

  priceChart = chartInit(el);
  priceChart.setOption({
    ...CHART_BASE,
    grid: {left: 4, right: 58, top: 20, bottom: 20},
    tooltip: {...CHART_BASE.tooltip, trigger: "axis",
      formatter: ([p]) => `<span style="color:${CH.mut}">${new Date(p.value[0]).toLocaleDateString("pl-PL")}</span>
        <b>${fmtP(p.value[1])}</b>`},
    xAxis: {...CHART_BASE.xAxis, min: pts[0][0], max: now, splitNumber: 5},
    yAxis: {type: "value", show: false, min: lo - (hi - lo) * .15, max: hi + (hi - lo) * .2},
    series: [{type: "line", step: "end", data, symbol: "circle", symbolSize: 7,
      itemStyle: {color: "#fff", borderWidth: 2}, lineStyle: {color: col, width: 2},
      endLabel: {...label(), distance: 6},
      areaStyle: {origin: "start", color: new echarts.graphic.LinearGradient(0, 0, 0, 1,
        [{offset: 0, color: col + "30"}, {offset: 1, color: col + "00"}])}}],
  });
  const toggle = () => {
    const tb = $("#ptable");
    tb.hidden = !tb.hidden;
    $("#ptoggle").textContent = `${tb.hidden ? "pokaż" : "ukryj"} wszystkie zmiany (${h.length}) ${tb.hidden ? "▾" : "▴"}`;
  };
  priceChart.getZr().on("click", toggle);
  $("#ptoggle").onclick = toggle;
}
function closeDetail(){
  $("#detail").classList.remove("open");
  $("#backdrop").classList.remove("open");
}
$("#dclose").onclick = closeDetail;
$("#backdrop").onclick = closeDetail;
addEventListener("keydown", e => { if(e.key === "Escape") closeDetail(); });

/* ---------- zdarzenia ---------- */
let deb;
const soon = () => { clearTimeout(deb); deb = setTimeout(() => apply(), 220); };
["#qrows","#pmin","#pmax","#amin","#amax","#mmin","#mmax"]
  .forEach(s => $(s).addEventListener("input", soon));
$("#qrows").addEventListener("change", e => { if(e.target.tagName === "SELECT") apply(); });
$("#qadd").onclick = addQueryRow;
["#qdesc","#market","#seller","#fresh","#pchg","#sort","#onlydrop","#onlygeo"]
  .forEach(s => $(s).addEventListener("change", () => apply()));
$("#bounds").addEventListener("change", () => apply(false));
map.on("moveend", () => { if($("#bounds").checked) apply(false); });
document.querySelectorAll("#rooms .chip").forEach(b =>
  b.addEventListener("click", () => { b.classList.toggle("on"); apply(); }));
document.querySelectorAll("#portals .chip").forEach(b =>
  b.addEventListener("click", () => { b.classList.toggle("on"); apply(); }));
$("#districts").addEventListener("change", () => apply());
$("#dall").onclick = () => {
  document.querySelectorAll(".dbox").forEach(b => b.checked = true); apply(); };
$("#dnone").onclick = () => {
  document.querySelectorAll(".dbox").forEach(b => b.checked = false); apply(); };
$("#clear").onclick = () => {
  ["#q","#pmin","#pmax","#amin","#amax","#mmin","#mmax"]
    .forEach(s => $(s).value = "");
  document.querySelectorAll("#qrows .qrow:has(select)").forEach(r => r.remove());
  ["#market","#seller","#fresh","#pchg"].forEach(s => $(s).value = "");
  ["#onlydrop","#onlygeo","#bounds"].forEach(s => $(s).checked = false);
  $("#qdesc").checked = true;
  yearReset();
  document.querySelectorAll("#rooms .chip").forEach(b => b.classList.remove("on"));
  document.querySelectorAll("#portals .chip").forEach(b => b.classList.add("on"));
  document.querySelectorAll(".dbox").forEach(b => b.checked = true);
  apply();
};
addEventListener("resize", renderHisto);
apply();
