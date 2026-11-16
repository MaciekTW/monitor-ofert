"use strict";
/* Raport o mieszkaniu — otwierany przyciskiem w szczegółach oferty (openReport).
   Powstaje w przeglądarce, dopiero po kliknięciu i tylko dla tej jednej oferty, z danych,
   które strona już ma: ofert (ceny na tle rynku), przystanków i tras z rozkładów GTFS,
   punktów i terenów zielonych z OpenStreetMap (osm.py → #osmdata), sieci ulic i chodników
   (#walkdata), warstw mapy (#pois) oraz — jedyne zapytanie do sieci — poziomu hałasu
   z miejskiej mapy akustycznej dla punktu, w którym stoi pinezka.
   Czytelnik to ktoś, kto ma tam zamieszkać. Układ jest pod szybkie czytanie: na górze
   ocena i kafelki „w skrócie”, niżej sekcje zwinięte do jednego zdania z wnioskiem —
   rozwija się tylko to, co kogoś obchodzi — a obok stale widoczna mapa, na której
   najechanie na wiersz rysuje trasę pieszą albo przejazd.
   Odległości są liczone po sieci pieszej (Dijkstra od mieszkania), dojazdy komunikacją
   z przesiadkami — po trasach linii z czasami z rozkładu (journeys). Zawartość ustawia się
   w panelu „Dostosuj”: sekcje, profil wag, zasięg spaceru, cele dojazdów, rodzaje obiektów
   z mapy; ustawienia zostają w localStorage.
   Korzysta z pomocników app.js ($, esc, fmtP, fmtN, median, plural, OFFERS, GEN, TRANSIT,
   TRANSIT_POINTS, transitDay, loadRoutes, uniqueOnly, copies, portalName, zl, chartInit,
   CHART_BASE, CH, carto, CARTO_OPTS, NoiseLayer, KRAKOW_BOUNDARY). Poza openReport wszystko jest lokalne. */
(() => {
const OSM = JSON.parse($("#osmdata").textContent);    // null, gdy nie udało się pobrać danych okolicy
const WALK = JSON.parse($("#walkdata").textContent);  // null, gdy nie ma sieci pieszej
const RCFG = JSON.parse($("#reportcfg").textContent);
const CATS = OSM ? Object.fromEntries(OSM.cats.map(c => [c.id, c])) : {};

/* ---------- założenia (opisane też w stopce raportu) ---------- */
const WALK_M_MIN = 80;    // 4,8 km/h
const BIKE_M_MIN = 250;   // 15 km/h
const DETOUR = 1.3;       // tylko awaryjnie, bez sieci pieszej: droga jest o ~30% dłuższa niż linia prosta
const WALK_LIMIT_M = 5000;  // tak daleko od mieszkania szukamy tras pieszych; dalej szacunek
const SNAP_M = 250;       // punkt dalej niż tyle od najbliższej drogi nie jest do niej przypinany
const SIMILAR_M = 1500;   // „w okolicy” przy porównaniu cen (w linii prostej)
const SERVICE_MIN = 18 * 60;  // dzienny czas kursowania, z którego liczony jest średni odstęp
// dojazdy komunikacją
const ACCESS_M = {bus: 1000, tram: 1000, rail: 2000};  // najdalsze dojście na przystanek
const EXIT_M = 1200;      // najdalsze dojście z przystanku do celu
const TRANSFER_M = 250;   // przesiadka pieszo między przystankami (w linii prostej)
const MAX_WAIT = 12;      // średnie czekanie to połowa odstępu między kursami, ale najwyżej tyle minut
const SLACK = 2;          // zapas na każdą przesiadkę
const MAX_RIDES = 3;      // najwyżej dwie przesiadki
// adres i warstwy miejskiej mapy hałasu są w html_map.py (NOISE_SERVICE, NOISE_LAYERS),
// żeby test kontraktu sprawdzał dokładnie to, o co pyta raport
const NOISE_SERVICE = RCFG.noise.url;
const NOISE_LAYERS = RCFG.noise.layers;  // [źródło, warstwa L_DWN (cała doba), warstwa L_N (noc)]
const NOISE_LIMIT = {day: 65, night: 56};  // dopuszczalne dla zabudowy wielorodzinnej [dB]

const SEC_COLOR = {transit: "#2456c7", daily: "#eb6834", health: "#c92a2a", education: "#7c3aed",
  leisure: "#178a4c", fun: "#b4690e", spots: "#0e7490", noise: "#68758a"};
// profile: wagi sekcji w ocenie ogólnej (sekcja wyłączona w ustawieniach wypada z oceny)
const PROFILES = {
  uni: {label: "uniwersalny", w: {price: 2, transit: 2, daily: 2, health: 1, education: 1, leisure: 1.5, fun: 1, noise: 1.5}},
  family: {label: "rodzina z dziećmi", w: {price: 2, transit: 1.5, daily: 2, health: 1.5, education: 3, leisure: 2, fun: .5, noise: 2}},
  single: {label: "singiel, para, student", w: {price: 2, transit: 3, daily: 1.5, health: .5, education: 0, leisure: 1, fun: 2.5, noise: 1}},
  senior: {label: "senior", w: {price: 1.5, transit: 2, daily: 3, health: 3, education: 0, leisure: 2, fun: .5, noise: 2.5}},
};
const WALKS = [5, 10, 15, 20];

/* ---------- obiekty z mapy: rodzaje (najbliższy z rodzaju) i katalog celów dojazdu ---------- */
// rodzaj to warstwa mapy; wszystkie centra handlowe (osobne warstwy, żeby dało się je
// przełączać pojedynczo) są jednym rodzajem
const KINDS = [];
for(const def of JSON.parse($("#pois").textContent)){
  const mall = def.group === "Centra handlowe", id = mall ? "mall" : def.id;
  let kind = KINDS.find(k => k.id === id);
  if(!kind) KINDS.push(kind = {id, label: mall ? "Centrum handlowe" : def.label, icon: def.icon, pts: []});
  def.pts.forEach(([lat, lon, name, addr], i) =>
    kind.pts.push({id: `${def.id}:${i}`, lat, lon, name: name || def.label, addr: addr || ""}));
}
// cel dojazdu: miejsce wpisane w html_map.py (Rynek, dworzec, lotnisko) albo dowolny punkt z warstw mapy
const CATALOG = [
  ...RCFG.places.map(p => ({...p, sub: "miejsce"})),
  ...KINDS.flatMap(k => k.pts.map(p => ({id: p.id, name: p.name, lat: p.lat, lon: p.lon,
    sub: [k.label, p.addr].filter(Boolean).join(" · ")}))),
];

/* ---------- ustawienia ---------- */
const STORE = "monitor-ofert:raport";
const DEFAULTS = {off: [], open: [], profile: "uni", walk: 10, places: [],
  dest: RCFG.destinations, kindsOff: RCFG.kindsOff,
  loan: {down: 20, rate: 7, years: 25, other: 1, first: false}};
function loadSettings(){
  // localStorage bywa niedostępny (tryb prywatny, zablokowane dane witryn) — raport działa i bez niego
  try {
    const saved = JSON.parse(localStorage.getItem(STORE) || "{}");
    return {...structuredClone(DEFAULTS), ...saved, loan: {...DEFAULTS.loan, ...saved.loan}};
  } catch { return structuredClone(DEFAULTS); }
}
let S = loadSettings();
const saveSettings = () => { try { localStorage.setItem(STORE, JSON.stringify(S)); } catch { /* bez zapisu */ } };
// cele dojazdów: wybrane z katalogu, potem własne (wskazane na mapie)
const destinations = () => [
  ...S.dest.map(id => CATALOG.find(c => c.id === id)).filter(Boolean),
  ...S.places.map((p, i) => ({...p, id: `own:${i}`, sub: "własne miejsce"})),
];

/* ---------- geometria i formatowanie ---------- */
function distM(lat1, lon1, lat2, lon2){
  const dy = (lat1 - lat2) * 111320;
  const dx = (lon1 - lon2) * 111320 * Math.cos((lat1 + lat2) / 2 * Math.PI / 180);
  return Math.hypot(dx, dy);
}
const walkMin = d => Math.max(1, Math.round(d / WALK_M_MIN));
const bikeMin = d => Math.max(1, Math.round(d * DETOUR / BIKE_M_MIN));
const fmtD = d => d < 950 ? Math.round(d / 10) * 10 + " m" : (d / 1000).toFixed(1).replace(".", ",") + " km";
const fmt1 = v => v.toFixed(1).replace(".", ",");
const pct = v => (v > 0 ? "+" : v < 0 ? "−" : "") + Math.abs(v * 100).toFixed(0) + "%";
const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
// pełna ocena do odległości „near”, dalej liniowo do zera przy trzykrotności
const distScore = (d, near) => d == null ? 0 : d <= near ? 1 : Math.max(0, 1 - (d - near) / (2 * near));
function inRing(lat, lon, ring){
  let inside = false;
  for(let i = 0, j = ring.length - 1; i < ring.length; j = i++){
    const [lat1, lon1] = ring[i], [lat2, lon2] = ring[j];
    if((lat1 > lat) !== (lat2 > lat) && lon < (lon2 - lon1) * (lat - lat1) / (lat2 - lat1) + lon1) inside = !inside;
  }
  return inside;
}
const scoreCls = s => s == null ? "bg-page text-mut" : s >= 7.5 ? "bg-[#e3f4ea] text-good"
  : s >= 5 ? "bg-[#fdf3d8] text-[#8a6100]" : "bg-[#fdecec] text-bad";
const scoreBar = s => s == null ? "#c3cad6" : s >= 7.5 ? CH.good : s >= 5 ? "#d9a514" : CH.bad;
const scoreBadge = s => `<span class="rounded-md px-2 py-0.5 text-[13px] font-bold whitespace-nowrap tabular-nums ${scoreCls(s)}"
  title="Ocena 0–10; sposób liczenia jest opisany na końcu raportu">${s == null ? "—" : fmt1(s)}<span class="font-normal"> / 10</span></span>`;
const distCls = (d, near) => { const s = distScore(d, near); return s >= 1 ? "text-good" : s >= .4 ? "text-ink" : "text-bad"; };
const departures = n => `${n} ${plural(n, "odjazd", "odjazdy", "odjazdów")}`;
const note = text => `<p class="mt-2 text-xs text-mut">${text}</p>`;
// „6 min” po sieci pieszej, „≈ 6 min”, gdy to tylko szacunek z linii prostej
const walkText = x => `${x.approx ? "≈ " : ""}${walkMin(x.d)} min`;

/* ---------- sieć piesza: graf, przypinanie punktów, najkrótsze trasy ---------- */
const CELL = .002;  // oczko siatki do wyszukiwania najbliższego wierzchołka (~220 × 140 m)
const cellOf = (lat, lon) => Math.floor(lat / CELL) * 1e6 + Math.floor(lon / CELL);
let G;  // graf budowany przy pierwszym raporcie: współrzędne, listy sąsiedztwa (CSR), siatka wierzchołków
function graph(){
  if(G !== undefined) return G;
  if(!WALK || !WALK.nodes.length) return G = null;
  const n = WALK.nodes.length / 2, lat = new Float64Array(n), lon = new Float64Array(n);
  for(let i = 0, a = 0, b = 0; i < n; i++){
    lat[i] = (a += WALK.nodes[2 * i]) / 1e5;
    lon[i] = (b += WALK.nodes[2 * i + 1]) / 1e5;
  }
  const E = WALK.edges, m = E.length / 3;
  const head = new Int32Array(n + 1), to = new Int32Array(2 * m), len = new Float32Array(2 * m);
  for(let e = 0; e < m; e++){ head[E[3 * e] + 1]++; head[E[3 * e + 1] + 1]++; }
  for(let i = 0; i < n; i++) head[i + 1] += head[i];
  const fill = head.slice(0, n);
  for(let e = 0; e < m; e++){
    const u = E[3 * e], v = E[3 * e + 1], w = E[3 * e + 2];
    to[fill[u]] = v; len[fill[u]++] = w;
    to[fill[v]] = u; len[fill[v]++] = w;
  }
  const cells = new Map();
  for(let i = 0; i < n; i++){
    const key = cellOf(lat[i], lon[i]);
    cells.has(key) ? cells.get(key).push(i) : cells.set(key, [i]);
  }
  return G = {n, lat, lon, head, to, len, cells};
}
// najbliższy wierzchołek sieci w promieniu SNAP_M: {node, d} albo null
function snap(lat, lon){
  const g = graph();
  if(!g) return null;
  let best = -1, bd = SNAP_M;
  const ci = Math.floor(lat / CELL), cj = Math.floor(lon / CELL);
  for(let i = ci - 2; i <= ci + 2; i++) for(let j = cj - 2; j <= cj + 2; j++){
    for(const node of g.cells.get(i * 1e6 + j) || []){
      const d = distM(lat, lon, g.lat[node], g.lon[node]);
      if(d < bd){ bd = d; best = node; }
    }
  }
  return best < 0 ? null : {node: best, d: bd};
}
// Dijkstra od punktu (lat, lon) do wszystkiego w zasięgu limit metrów. Wynik to „szukanie”:
// {lat, lon, off: dojście do sieci, dist[], prev[]}; bez sieci albo z dala od niej — samo {lat, lon}
function walkSearch(lat, lon, limit){
  const g = graph(), start = g && snap(lat, lon);
  if(!start) return {lat, lon};
  const dist = new Float32Array(g.n).fill(Infinity), prev = new Int32Array(g.n).fill(-1);
  const hd = [0], hn = [start.node];  // kopiec binarny: odległości i wierzchołki
  dist[start.node] = 0;
  while(hn.length){
    const d = hd[0], u = hn[0], ld = hd.pop(), ln = hn.pop();
    if(hn.length){
      let i = 0;
      for(;;){
        let c = 2 * i + 1;
        if(c >= hn.length) break;
        if(c + 1 < hn.length && hd[c + 1] < hd[c]) c++;
        if(hd[c] >= ld) break;
        hd[i] = hd[c]; hn[i] = hn[c]; i = c;
      }
      hd[i] = ld; hn[i] = ln;
    }
    if(d > dist[u]) continue;
    for(let e = g.head[u]; e < g.head[u + 1]; e++){
      const v = g.to[e], nd = d + g.len[e];
      if(nd >= dist[v] || nd > limit) continue;
      dist[v] = nd; prev[v] = u;
      let i = hn.length;
      hd.push(nd); hn.push(v);
      while(i > 0){
        const p = (i - 1) >> 1;
        if(hd[p] <= nd) break;
        hd[i] = hd[p]; hn[i] = hn[p]; i = p;
      }
      hd[i] = nd; hn[i] = v;
    }
  }
  return {lat, lon, off: start.d, dist, prev};
}
// droga pieszo z początku szukania do punktu: {d, node} po sieci albo {d, approx: true} — szacunek
// z linii prostej, gdy punkt jest poza zasięgiem szukania lub z dala od sieci
function walkTo(search, lat, lon){
  const air = distM(search.lat, search.lon, lat, lon);
  if(search.dist){
    const s = snap(lat, lon);
    if(s && search.dist[s.node] < Infinity) return {d: Math.max(air, search.off + search.dist[s.node] + s.d), node: s.node};
  }
  return {d: air * DETOUR, approx: true};
}
// przebieg trasy z szukania do wierzchołka — do narysowania na mapie
function walkPath(search, node, lat, lon){
  const g = graph(), line = [[lat, lon]];
  for(let u = node; u >= 0; u = search.prev[u]) line.push([g.lat[u], g.lon[u]]);
  line.push([search.lat, search.lon]);
  return line;
}

/* ---------- okolica: punkty i tereny zielone z OpenStreetMap ---------- */
// Najbliższe pieszo: droga jest liczona dla punktów w zasięgu spaceru w linii prostej
// (tylko one mogą być w zasięgu pieszo) i dla ośmiu najbliższych — to wystarcza,
// żeby nie pominąć miejsca, do którego pieszo jest bliżej, niż sugeruje mapa.
function ranked(ctx, items){
  const all = items.map(p => ({...p, air: distM(ctx.o.lat, ctx.o.lon, p.lat, p.lon)})).sort((a, b) => a.air - b.air);
  const near = all.filter((x, i) => i < 8 || x.air <= ctx.R).map(x => ({...x, ...walkTo(ctx.walk, x.lat, x.lon)}))
    .sort((a, b) => a.d - b.d);
  return {list: near.slice(0, 5), count: near.filter(x => x.d <= ctx.R).length, d: near[0]?.d ?? null};
}
const nearby = (cat, ctx) => ({cat, ...ranked(ctx, (OSM.pts[cat.id] || []).map(p => ({lat: p[0], lon: p[1], name: p[2]})))});
// do parku idzie się do najbliższego punktu jego granicy, nie do środka
function greenNearby(ctx){
  const {o} = ctx;
  const areas = OSM.green.filter(g => g[1] >= 1).map(([name, ha, outline]) => {
    let d = Infinity, at = null;
    for(const [lat, lon] of outline){
      const x = distM(o.lat, o.lon, lat, lon);
      if(x < d){ d = x; at = [lat, lon]; }
    }
    return at && {name: `${name} (${ha >= 10 ? Math.round(ha) : fmt1(ha)} ha)`, ha, lat: at[0], lon: at[1]};
  }).filter(Boolean);
  const big = ranked(ctx, areas.filter(a => a.ha >= 10)).list[0] || null;
  return {cat: CATS.green, ...ranked(ctx, areas), big};
}
// wiersz, po którego najechaniu mapa pokazuje punkt i trasę (focusOn); zwraca atrybut data-f
function focusAttr(ctx, x, label){
  if(x.lat == null) return "";
  ctx.focus.push({lat: x.lat, lon: x.lon, node: x.node, label});
  return `data-f="${ctx.focus.length - 1}"`;
}
function amenityRow(ctx, a){
  const first = a.list[0];
  if(!first) return `<div class="flex gap-2 border-b border-line py-1.5 last:border-0">
    <span class="w-44 shrink-0 font-semibold">${esc(a.cat.label)}</span><span class="text-mut">brak w danych</span></div>`;
  return `<details class="border-b border-line last:border-0">
    <summary ${focusAttr(ctx, first, `${a.cat.label}: ${first.name || "bez nazwy"}`)}
      class="flex cursor-pointer flex-wrap items-baseline gap-x-2 py-1.5 hover:bg-[#f7f9fc]">
      <span class="w-44 shrink-0 font-semibold">${esc(a.cat.label)}</span>
      <span class="min-w-24 flex-1 truncate text-mut">${esc(first.name || "bez nazwy")}</span>
      <span class="w-16 text-right font-semibold whitespace-nowrap tabular-nums ${distCls(first.d, a.cat.near)}">${walkText(first)}</span>
      <span class="w-14 text-right text-xs whitespace-nowrap text-mut tabular-nums">${fmtD(first.d)}</span>
      <span class="w-20 text-right text-xs whitespace-nowrap text-mut"
        title="Liczba miejsc, do których dojdziesz w ${S.walk} min">w zasięgu: ${a.count}</span>
    </summary>
    <ol class="mb-2 ml-44 list-decimal pl-5 text-[12.5px] text-mut">${a.list.map(x =>
      `<li ${focusAttr(ctx, x, `${a.cat.label}: ${x.name || "bez nazwy"}`)} class="cursor-default hover:text-ink">${
        esc(x.name || "bez nazwy")} — ${walkText(x)} pieszo, ${fmtD(x.d)}</li>`).join("")}</ol></details>`;
}
// sekcja z listą kategorii: ocena to średnia ważona ocen dojścia do najbliższego miejsca
function amenitySection(ctx, sec){
  if(!ctx.located) return {html: noLocation(), take: "brak lokalizacji"};
  if(!OSM) return {html: noOsm(), take: "brak danych o okolicy"};
  // poza prostokątem danych najbliższy sklep byłby „6 km stąd” tylko dlatego, że bliższych nie pobrano
  if(!ctx.covered) return {take: "poza obszarem danych", html: `<p class="text-mut">Mieszkanie leży poza obszarem,
    dla którego pobrano dane o okolicy (prostokąt wokół większości ofert) — tej sekcji nie da się rzetelnie policzyć.</p>`};
  const rows = ctx.amen.filter(a => a.cat.sec === sec && a.cat.w > 0);
  const weight = rows.reduce((s, a) => s + a.cat.w, 0);
  const score = rows.reduce((s, a) => s + a.cat.w * distScore(a.d, a.cat.near), 0) / weight * 10;
  for(const a of rows){
    if(a.cat.w < 2) continue;
    const first = a.list[0];
    if(first && first.d <= a.cat.near)
      ctx.plus.push({w: a.cat.w, text: `${a.cat.label}: ${walkMin(first.d)} min pieszo${first.name ? ` (${first.name})` : ""}`});
    else if(!first || first.d > 2 * a.cat.near)
      ctx.minus.push({w: a.cat.w, text: `Daleko: ${a.cat.label.toLowerCase()}${first ? ` — ${walkMin(first.d)} min pieszo` : " — brak w okolicy"}`});
  }
  // wniosek: ile kategorii jest w zasięgu spaceru i co jest najdalej
  const inReach = rows.filter(a => a.d != null && a.d <= ctx.R), far = [...rows].sort((a, b) => (b.d ?? 1e9) - (a.d ?? 1e9))[0];
  const take = `${inReach.length} z ${rows.length} w zasięgu ${S.walk} min` + (inReach.length < rows.length && far
    ? ` · najdalej: ${far.cat.label.toLowerCase()}${far.d == null ? "" : ` ${walkMin(far.d)} min`}` : "");
  return {score, take, html: rows.map(a => amenityRow(ctx, a)).join("") +
    note("Czas to dojście pieszo. Najedź na wiersz, żeby zobaczyć trasę na mapie; kliknij, żeby rozwinąć pięć najbliższych.")};
}
const noLocation = () => `<p class="text-mut">Ogłoszenie nie ma lokalizacji na mapie — tej części nie da się policzyć.</p>`;
const noOsm = () => `<p class="text-mut">Brak danych o okolicy — przy generowaniu mapy nie udało się ich pobrać
  z OpenStreetMap. Wygeneruj mapę ponownie (bez --offline), żeby uzupełnić tę sekcję.</p>`;

/* ---------- wybrane obiekty z mapy: najbliższy z każdego rodzaju ---------- */
function spotsSection(ctx){
  if(!ctx.located) return {html: noLocation(), take: "brak lokalizacji"};
  const kinds = KINDS.filter(k => !S.kindsOff.includes(k.id) && k.pts.length);
  if(!kinds.length) return {take: "nic nie wybrano", html: `<p class="text-mut">Nie wybrano żadnego rodzaju obiektów —
    zaznacz je w panelu „Dostosuj”.</p>`};
  ctx.spots = kinds.map(k => ({kind: k, ...ranked(ctx, k.pts)}));
  const row = s => {
    const x = s.list[0];
    return `<details class="border-b border-line last:border-0">
      <summary ${focusAttr(ctx, x, `${s.kind.label}: ${x.name}`)} class="flex cursor-pointer flex-wrap items-center gap-x-2 py-1.5 hover:bg-[#f7f9fc]">
        <img src="${s.kind.icon}" alt="" class="size-[18px] object-contain">
        <span class="w-40 shrink-0 font-semibold">${esc(s.kind.label)}</span>
        <span class="min-w-24 flex-1 truncate text-mut">${esc(x.name)}${x.addr ? `, ${esc(x.addr)}` : ""}</span>
        <span class="w-16 text-right font-semibold whitespace-nowrap tabular-nums">${walkText(x)}</span>
        <span class="w-14 text-right text-xs whitespace-nowrap text-mut tabular-nums">${fmtD(x.d)}</span>
        <span class="w-28 text-right text-xs whitespace-nowrap text-mut" data-spot="${esc(s.kind.id)}"></span>
      </summary>
      <ol class="mb-2 ml-[212px] list-decimal pl-5 text-[12.5px] text-mut">${s.list.map(p =>
        `<li ${focusAttr(ctx, p, `${s.kind.label}: ${p.name}`)} class="cursor-default hover:text-ink">${esc(p.name)}${
          p.addr ? `, ${esc(p.addr)}` : ""} — ${walkText(p)} pieszo, ${fmtD(p.d)}</li>`).join("")}</ol></details>`;
  };
  const take = [...ctx.spots].sort((a, b) => a.d - b.d).slice(0, 3)
    .map(s => `${s.kind.label.toLowerCase()} ${walkMin(s.d)} min`).join(" · ");
  return {take, html: ctx.spots.map(row).join("") +
    note("Najbliższy obiekt każdego rodzaju z warstw mapy; czas to dojście pieszo, a dla dalszych — także dojazd komunikacją. Rodzaje wybierasz w panelu „Dostosuj”.")};
}

/* ---------- komunikacja: przystanki w zasięgu ---------- */
// kolej jest warta dłuższego dojścia niż autobus
const reach = (t, R) => t.id === "rail" ? Math.max(2 * R, 1200) : R;
function transitInfo(ctx){
  return TRANSIT.map(t => {
    const lim = reach(t, ctx.R);
    const all = t.stops.map(([lat, lon, name, code, deps]) => ({lat, lon, name, code, deps, air: distM(ctx.o.lat, ctx.o.lon, lat, lon)}))
      .sort((a, b) => a.air - b.air);
    const stops = all.filter((s, i) => i < 4 || s.air <= lim).map(s => ({...s, ...walkTo(ctx.walk, s.lat, s.lon)}))
      .sort((a, b) => a.d - b.d);
    const inReach = stops.filter(s => s.d <= lim);
    // linia liczy się raz, z przystanku, na którym ma najwięcej odjazdów
    const lines = new Map();
    for(const s of inReach) for(const [i, n] of s.deps) lines.set(i, Math.max(lines.get(i) ?? 0, n));
    return {t, nearest: stops[0] || null, inReach, lines,
      deps: [...lines.values()].reduce((a, b) => a + b, 0),
      night: [...lines.keys()].filter(i => t.night.includes(i))};
  });
}
const KIND_WORD = {bus: "autobus", tram: "tramwaj", rail: "kolej"};
function transitSection(ctx){
  if(!ctx.located) return {html: noLocation(), take: "brak lokalizacji"};
  if(!ctx.transit.length) return {html: `<p class="text-mut">Brak rozkładów jazdy w tej mapie.</p>`, take: "brak rozkładów"};
  const by = Object.fromEntries(ctx.transit.map(x => [x.t.id, x]));
  const NEAR = {bus: [400, 2], tram: [500, 3], rail: [1200, 1.5]};
  let sum = 0, weight = 0;
  for(const x of ctx.transit){
    const [near, w] = NEAR[x.t.id] || [500, 1];
    sum += w * distScore(x.nearest?.d ?? null, near); weight += w;
  }
  const city = ctx.transit.filter(x => x.t.id !== "rail");
  const deps = city.reduce((s, x) => s + x.deps, 0), lines = city.reduce((s, x) => s + x.lines.size, 0);
  const night = city.some(x => x.night.length);
  sum += 3 * Math.min(1, deps / 1000) + Math.min(1, lines / 10) + .5 * night; weight += 4.5;
  const score = sum / weight * 10;
  if(by.tram?.nearest?.d <= 500) ctx.plus.push({w: 3, text: `Tramwaj ${walkMin(by.tram.nearest.d)} min pieszo (${by.tram.nearest.name})`});
  if(by.rail?.nearest?.d <= 1000) ctx.plus.push({w: 2, text: `Kolej ${walkMin(by.rail.nearest.d)} min pieszo (${by.rail.nearest.name})`});
  if(deps >= 800) ctx.plus.push({w: 2.5, text: `${departures(deps)} dziennie w zasięgu spaceru (${lines} ${plural(lines, "linia", "linie", "linii")})`});
  if(deps < 150) ctx.minus.push({w: 3, text: deps ? `Rzadka komunikacja: ${departures(deps)} dziennie w zasięgu spaceru` : "Brak przystanku w zasięgu spaceru"});
  if(!night && deps) ctx.minus.push({w: 1, text: "Brak linii nocnej w zasięgu spaceru"});
  const block = x => {
    const {t, nearest} = x;
    const chips = [...x.lines].sort((a, b) => b[1] - a[1]).map(([i, n]) =>
      `<span title="${n ? `${departures(n)} w dniu odniesienia (oba kierunki)` : "nie kursuje w dniu odniesienia"}"
        class="rounded px-1.5 py-px text-[12.5px] font-semibold ${!n ? "bg-page text-mut" : t.night.includes(i)
          ? "bg-ink text-white" : "bg-acc-soft text-acc"}">${esc(t.lines[i])}</span>`).join(" ");
    return `<div class="border-b border-line py-2 last:border-0">
      <div ${nearest ? focusAttr(ctx, nearest, `${t.label}: ${nearest.name}`) : ""} class="flex flex-wrap items-baseline gap-x-2">
        <img src="${t.iconUrl("")}" alt="" class="h-[18px] w-3 self-center"><b>${esc(t.label)}</b>
        ${nearest ? `<span class="text-mut">najbliżej: ${esc(nearest.name)}</span>
          <span class="font-semibold tabular-nums ${distCls(nearest.d, (NEAR[t.id] || [500])[0])}">${walkText(nearest)} pieszo</span>
          <span class="text-xs text-mut">${fmtD(nearest.d)}</span>` : `<span class="text-mut">brak w danych</span>`}</div>
      ${x.inReach.length ? `<div class="mt-1 text-[12.5px] text-mut">w zasięgu ${walkMin(reach(t, ctx.R))} min:
          ${x.inReach.length} ${plural(x.inReach.length, "przystanek", "przystanki", "przystanków")},
          ${x.lines.size} ${plural(x.lines.size, "linia", "linie", "linii")}, ${departures(x.deps)} dziennie</div>
        <div class="mt-1 flex flex-wrap gap-1">${chips}</div>`
        : `<div class="mt-1 text-[12.5px] text-mut">nic w zasięgu ${walkMin(reach(t, ctx.R))} min</div>`}</div>`;
  };
  const take = ctx.transit.filter(x => x.nearest && x.nearest.d <= reach(x.t, ctx.R))
    .map(x => `${KIND_WORD[x.t.id] || x.t.label} ${walkMin(x.nearest.d)} min`).join(", ") || "nic w zasięgu spaceru";
  return {score, take: `${take} · ${departures(deps)} dziennie`, html: ctx.transit.map(block).join("") +
    note(`Odjazdy z dnia odniesienia (${transitDay || "brak"}), łącznie w obu kierunkach. Ciemne numery to linie nocne.`)};
}

/* ---------- dojazdy komunikacją, z przesiadkami ---------- */
// Sieć połączeń budowana raz, z tras linii (routes.js): każdy wariant trasy to ciąg
// przystanków z minutami jazdy od początku. Na przystanku czeka się średnio połowę
// odstępu między kursami (najwyżej MAX_WAIT); linie nocne i niekursujące w dniu
// odniesienia są pomijane. Między przystankami bliżej niż TRANSFER_M można przejść pieszo.
let NET = null;
function network(routes){
  if(NET) return NET;
  const stops = new Map();
  for(const t of TRANSIT) for(const [lat, lon, name, code, deps] of t.stops)
    stops.set(`${t.id}:${code}`, {key: `${t.id}:${code}`, t, lat, lon, name, deps: new Map(deps.map(([i, n]) => [t.lines[i], n]))});
  const variants = [], byStop = new Map();
  for(const t of TRANSIT) for(const [line, list] of Object.entries(routes[t.id] || {})){
    if(t.night.includes(t.lines.indexOf(line))) continue;
    for(const v of list){
      if(v.stops.some(s => s[4] == null)) continue;  // trasa bez czasów (stary plik) — nie da się nią planować
      const keys = v.stops.map(s => `${t.id}:${String(s[3]).split("-")[0]}`);
      // przystanek trasy spoza mapy (np. dalsza stacja kolejowa): można na nim wysiąść, wsiąść nie
      keys.forEach((key, i) => stops.has(key) || stops.set(key,
        {key, t, lat: v.stops[i][0], lon: v.stops[i][1], name: v.stops[i][2], deps: new Map()}));
      const every = keys.map(key => { const n = stops.get(key).deps.get(line) || 0; return n ? SERVICE_MIN / (n / 2) : Infinity; });
      const vi = variants.push({t, line, to: v.to, keys, time: v.stops.map(s => s[4]), every,
        wait: every.map(e => Math.min(e / 2, MAX_WAIT) + (e === Infinity ? Infinity : 0))}) - 1;
      for(const key of new Set(keys)) byStop.has(key) ? byStop.get(key).push(vi) : byStop.set(key, [vi]);
    }
  }
  // przejścia piesze między pobliskimi przystankami (siatka ~ TRANSFER_M)
  const grid = new Map(), cell = s => `${Math.floor(s.lat / .003)}:${Math.floor(s.lon / .004)}`;
  for(const s of stops.values()) grid.has(cell(s)) ? grid.get(cell(s)).push(s) : grid.set(cell(s), [s]);
  const links = new Map();
  for(const s of stops.values()){
    const out = [], ci = Math.floor(s.lat / .003), cj = Math.floor(s.lon / .004);
    for(let i = ci - 1; i <= ci + 1; i++) for(let j = cj - 1; j <= cj + 1; j++) for(const q of grid.get(`${i}:${j}`) || []){
      const d = q === s ? Infinity : distM(s.lat, s.lon, q.lat, q.lon);
      if(d <= TRANSFER_M) out.push({key: q.key, m: d * DETOUR, min: d * DETOUR / WALK_M_MIN});
    }
    if(out.length) links.set(s.key, out);
  }
  return NET = {stops, variants, byStop, links};
}
// Najszybsze dotarcie z mieszkania na każdy przystanek (rundy jak w algorytmie RAPTOR:
// runda = jeden przejazd, po niej przejścia piesze). Etykieta przystanku: {t: minuty od
// wyjścia z domu, rides: liczba przejazdów, prev: poprzednia etykieta, leg: jak tu dotarto}.
function journeys(ctx, net){
  const best = new Map();
  let marked = new Set(), direct = null;
  for(const s of net.stops.values()){
    const lim = ACCESS_M[s.t.id] ?? 1000;
    if(distM(ctx.o.lat, ctx.o.lon, s.lat, s.lon) > lim) continue;
    const w = walkTo(ctx.walk, s.lat, s.lon);
    if(w.d > lim) continue;
    best.set(s.key, {t: w.d / WALK_M_MIN, key: s.key, rides: 0, prev: null, leg: {walk: w.d, node: w.node, search: ctx.walk}});
    marked.add(s.key);
  }
  for(let round = 1; round <= MAX_RIDES && marked.size; round++){
    const before = new Map(best), next = new Set(), scan = new Set();
    for(const key of marked) for(const vi of net.byStop.get(key) || []) scan.add(vi);
    for(const vi of scan){
      const v = net.variants[vi];
      let dep = Infinity, bi = -1, from = null;  // dep: odjazd z przystanku bi, na którym najlepiej wsiąść
      for(let i = 0; i < v.keys.length; i++){
        const key = v.keys[i];
        if(bi >= 0){
          const at = dep + v.time[i] - v.time[bi], cur = best.get(key);
          if(!cur || at < cur.t - .01){
            best.set(key, {t: at, key, rides: from.rides + 1, prev: from, leg: {v, bi, ai: i, wait: dep - from.t}});
            next.add(key);
          }
        }
        const lab = marked.has(key) && before.get(key);
        if(lab && v.wait[i] < Infinity){
          const d2 = lab.t + (lab.rides ? SLACK : 0) + v.wait[i];
          if(bi < 0 || d2 < dep + v.time[i] - v.time[bi]){ dep = d2; bi = i; from = lab; }
        }
      }
    }
    for(const key of [...next]){
      const lab = best.get(key);
      for(const link of net.links.get(key) || []){
        const cur = best.get(link.key);
        if(!cur || lab.t + link.min < cur.t - .01){
          best.set(link.key, {t: lab.t + link.min, key: link.key, rides: lab.rides, prev: lab, leg: {walk: link.m, transfer: true}});
          next.add(link.key);
        }
      }
    }
    if(round === 1) direct = new Map(best);
    marked = next;
  }
  return {best, direct: direct || best};
}
// szukanie piesze od celu (do przystanków wokół niego) — liczone raz na cel
function placeSearch(ctx, place){
  const key = `${place.lat},${place.lon}`;
  if(!ctx.placeWalk.has(key)) ctx.placeWalk.set(key, walkSearch(place.lat, place.lon, EXIT_M));
  return ctx.placeWalk.get(key);
}
// najlepszy przejazd do miejsca wśród etykiet: {min, rides, wait, legs} albo null
function tripTo(ctx, net, labels, place){
  const search = placeSearch(ctx, place);
  let best = null;
  for(const lab of labels.values()){
    if(!lab.rides || (best && lab.t >= best.t)) continue;
    const s = net.stops.get(lab.key);
    if(distM(place.lat, place.lon, s.lat, s.lon) > EXIT_M) continue;
    const w = walkTo(search, s.lat, s.lon);
    if(w.d > EXIT_M) continue;
    const t = lab.t + w.d / WALK_M_MIN;
    if(!best || t < best.t) best = {t, lab, exit: {walk: w.d, node: w.node, search, exit: true}};
  }
  if(!best) return null;
  const legs = [best.exit];
  for(let lab = best.lab; lab; lab = lab.prev) legs.unshift({...lab.leg, at: net.stops.get(lab.key)});
  // dojście na pierwszy przystanek i z ostatniego mają swój koniec na przystanku — do rysowania
  legs.at(-1).at = net.stops.get(best.lab.key);
  return {min: Math.round(best.t), rides: best.lab.rides, legs,
    wait: Math.round(legs.reduce((s, l) => s + (l.v ? l.wait : 0), 0))};
}
function tripHtml(ctx, trip){
  ctx.trips.push(trip);
  const part = l => l.v
    ? `<span class="inline-flex items-center gap-1 whitespace-nowrap"><img src="${l.v.t.iconUrl("")}" alt="" class="h-[15px] w-2.5">
        <span class="rounded bg-acc-soft px-1.5 py-px text-[12.5px] font-semibold text-acc"
          title="kierunek: ${esc(l.v.to)}; kursuje średnio co ${Math.round(l.v.every[l.bi])} min">${esc(l.v.line)}</span>
        <span>${esc(net().stops.get(l.v.keys[l.bi]).name)} → ${esc(net().stops.get(l.v.keys[l.ai]).name)}</span>
        <span class="text-xs text-mut">${Math.round(l.v.time[l.ai] - l.v.time[l.bi])} min jazdy, co ~${Math.round(l.v.every[l.bi])} min</span></span>`
    : `<span class="whitespace-nowrap text-mut" title="pieszo">🚶 ${walkMin(l.walk)} min</span>`;
  // kolejne odcinki piesze (przejście między przystankami i dojście do celu) jako jeden; przejścia poniżej minuty pomijamy
  const shown = [];
  for(const l of trip.legs){
    if(l.v) shown.push(l);
    else if(shown.length && !shown.at(-1).v) shown.push({walk: shown.pop().walk + l.walk});
    else if(!l.transfer || walkMin(l.walk) > 1) shown.push({walk: l.walk});
  }
  return `<div data-trip="${ctx.trips.length - 1}" class="mt-1 flex flex-wrap items-center gap-x-1.5 gap-y-1 rounded px-1 py-0.5 hover:bg-[#f7f9fc]">
    <b class="mr-1 whitespace-nowrap">ok. ${trip.min} min</b>
    ${shown.map(part).join(`<span class="text-mut">›</span>`)}
    <span class="text-xs whitespace-nowrap text-mut">· ${trip.rides > 1 ? `${trip.rides - 1} ${plural(trip.rides - 1, "przesiadka", "przesiadki", "przesiadek")}` : "bez przesiadki"},
      w tym ok. ${trip.wait} min czekania</span></div>`;
}
const net = () => NET;
function commuteSection(ctx){
  if(!ctx.located) return {html: noLocation(), take: "brak lokalizacji"};
  const list = destinations();
  if(!list.length) return {take: "nie wybrano celów", html: `<p class="text-mut">Nie wybrano żadnego celu dojazdu —
    dodaj je w panelu „Dostosuj”.</p>`};
  return {take: "liczę połączenia…", html: `<div id="r-commute" class="text-[13px]"><p class="text-mut">wczytuję trasy linii…</p></div>` +
    note(`Czas od drzwi do drzwi: dojście, średnie czekanie (połowa odstępu między kursami, najwyżej ${MAX_WAIT} min), jazda
      według rozkładu, ${SLACK} min zapasu na każdą przesiadkę i dojście do celu. Najwyżej dwie przesiadki, według głównego
      wariantu trasy każdej linii, bez linii nocnych. Najedź na połączenie, żeby zobaczyć je na mapie.
      Cele wybierasz w panelu „Dostosuj”.`)};
}
// dojazd do każdego wybranego celu: {p, walk, trip, direct, onFoot, min}; labels to wynik journeys (albo null bez tras)
function commuteList(ctx, labels){
  const tripFor = (place, set) => labels && tripTo(ctx, NET, set, place);
  return destinations().map(p => {
    const walk = walkTo(ctx.walk, p.lat, p.lon);
    let trip = tripFor(p, labels?.best);
    // pieszo, gdy to realna alternatywa: do 45 min albo nie wolniej niż komunikacją —
    // a przejazd, który nie jest szybszy od spaceru, nie jest wart pokazywania
    const onFoot = walkMin(walk.d) <= 45 || (trip && walkMin(walk.d) <= trip.min);
    if(trip && onFoot && trip.min >= walkMin(walk.d)) trip = null;
    const direct = trip && trip.rides > 1 ? tripFor(p, labels.direct) : null;
    return {p, walk, trip, direct, onFoot, min: Math.min(trip?.min ?? Infinity, onFoot ? walkMin(walk.d) : Infinity)};
  });
}
// trasy linii wczytują się z osobnego pliku — po nich uzupełniają się dojazdy, kafelek „w skrócie” i obiekty z mapy
async function fillTransit(ctx){
  if(!ctx.located) return;
  let routes = null;
  try { routes = await loadRoutes(); } catch { /* brak pliku z trasami — niżej komunikat */ }
  if(ctx !== current) return;
  const labels = routes && journeys(ctx, network(routes));
  const tripFor = (place, set) => labels && tripTo(ctx, NET, set, place);
  ctx.commute = commuteList(ctx, labels);
  const box = $("#r-commute");
  if(box) box.innerHTML = ctx.commute.map(c => `<div class="border-b border-line py-2 last:border-0">
      <div ${focusAttr(ctx, {...c.p, node: c.walk.node}, c.p.name)} class="flex flex-wrap items-baseline gap-x-2"><b>${esc(c.p.name)}</b>
        <span class="text-mut">${c.onFoot ? `pieszo ${walkText(c.walk)} · ` : ""}rowerem ≈ ${bikeMin(distM(ctx.o.lat, ctx.o.lon, c.p.lat, c.p.lon))} min
          · ${fmtD(distM(ctx.o.lat, ctx.o.lon, c.p.lat, c.p.lon))} w linii prostej</span></div>
      ${c.trip ? tripHtml(ctx, c.trip) + (c.direct && c.direct.min <= c.trip.min + 15 ? tripHtml(ctx, c.direct) : "")
        : `<div class="mt-1 text-mut">${routes ? (c.onFoot ? "najszybciej pieszo — komunikacja nic tu nie daje"
            : "nie znalazłem połączenia z najwyżej dwiema przesiadkami")
          : "brak pliku z trasami linii (.cache/gtfs/routes.js) — wygeneruj mapę ponownie, żeby zobaczyć połączenia"}</div>`}</div>`).join("");
  ctx.takes.commute = ctx.commute.filter(c => c.min < Infinity).slice(0, 3)
    .map(c => `${c.p.name} ${c.min} min`).join(" · ") || "brak połączeń";
  // obiekty z mapy, do których pieszo jest ponad 20 min: dopisz czas dojazdu komunikacją
  for(const s of ctx.spots || []){
    const x = s.list[0], slot = $(`[data-spot="${CSS.escape(s.kind.id)}"]`);
    if(!slot || walkMin(x.d) <= 20) continue;
    const trip = tripFor(x, labels?.best);
    if(trip && trip.min < walkMin(x.d)) slot.textContent = `komunikacją ${trip.min} min`;
  }
  refreshSummary(ctx);
}

/* ---------- cena na tle rynku ---------- */
function priceInfo(o, located){
  if(o.pm == null) return null;
  const pool = uniqueOnly(OFFERS).filter(x => x.pm != null && x !== o && (o.g == null || x.g !== o.g));
  const district = o.d ? pool.filter(x => x.d === o.d) : [];
  const similar = pool.filter(x => (o._rb == null || x._rb === o._rb)
    && (o.a == null || (x.a != null && Math.abs(x.a - o.a) <= .2 * o.a))
    && (located ? x.lat != null && distM(o.lat, o.lon, x.lat, x.lon) <= SIMILAR_M : o.d && x.d === o.d));
  const base = similar.length >= 5 ? similar : district.length >= 5 ? district : null;
  const baseMed = base ? median(base.map(x => x.pm)) : null;
  return {district, similar, base, usesSimilar: base === similar, baseMed,
    diff: baseMed ? o.pm / baseMed - 1 : null,
    districtMed: district.length ? median(district.map(x => x.pm)) : null,
    cheaper: district.length ? district.filter(x => x.pm < o.pm).length / district.length : null};
}
function priceSection(ctx){
  const {o, price: p} = ctx;
  if(!p) return {html: `<p class="text-mut">Ogłoszenie nie podaje ceny albo metrażu — nie ma czego porównać.</p>`};
  const score = p.diff == null ? null : clamp(5 - p.diff * 20, 0, 10);
  const area = o.a ? `${Math.round(o.a * .8)}–${Math.round(o.a * 1.2)} m²` : "dowolny metraż";
  const what = p.usesSimilar
    ? `podobnych mieszkań (${o.r || "dowolna liczba pokoi"}, ${area}) ${ctx.located ? `w promieniu ${fmtD(SIMILAR_M)}` : "w tej dzielnicy"}`
    : `ofert w dzielnicy ${o.d}`;
  if(p.diff != null && p.diff <= -.07) ctx.plus.push({w: 3, text: `Cena za m² o ${Math.abs(p.diff * 100).toFixed(0)}% niższa od mediany ${what}`});
  if(p.diff != null && p.diff >= .07) ctx.minus.push({w: 3, text: `Cena za m² o ${(p.diff * 100).toFixed(0)}% wyższa od mediany ${what}`});
  const days = o.c || o.fs ? Math.floor((GEN - new Date(o.c || o.fs)) / 864e5) : null;
  const all = copies(o), prices = [...new Set(all.map(zl))].filter(v => v != null);
  const facts = [
    p.baseMed ? `Cena za m² to <b>${fmtN(o.pm)} zł</b> — <b class="${p.diff <= 0 ? "text-good" : "text-bad"}">${pct(p.diff)}</b>
      względem mediany ${esc(what)} (<b>${fmtN(p.baseMed)} zł/m²</b>, ${p.base.length} ${plural(p.base.length, "oferta", "oferty", "ofert")}).`
      : `Cena za m² to <b>${fmtN(o.pm)} zł</b>. Za mało podobnych ofert w okolicy, żeby wiarygodnie ją porównać.`,
    p.districtMed && p.usesSimilar ? `Mediana całej dzielnicy ${esc(o.d)}: ${fmtN(p.districtMed)} zł/m²
      (${p.district.length} ofert); ${(p.cheaper * 100).toFixed(0)}% z nich jest tańszych w przeliczeniu na metr.` : null,
    days != null ? `Ogłoszenie jest na rynku od <b>${days} ${plural(days, "dnia", "dni", "dni")}</b>${
      days > 120 ? " — długo, jest pole do negocjacji" : ""}.` : null,
    o.h ? `Cena zmieniała się ${o.h.length - 1} ${plural(o.h.length - 1, "raz", "razy", "razy")}: z ${fmtP(o.h[0][1])}
      (${o.h[0][0]}) na ${fmtP(o.h.at(-1)[1])}${o._drop > 0 ? `; łączna obniżka od najwyższej ceny: ${(o._drop * 100).toFixed(1).replace(".", ",")}%` : ""}.`
      : "Od pierwszego skanu cena się nie zmieniła.",
    prices.length > 1 ? `<span class="text-bad">Portale podają różne ceny tego samego ogłoszenia:
      ${all.map(x => `${esc(portalName(x))} ${fmtP(x.p)}`).join(", ")}.</span>` : null,
    o.ng ? "Sprzedający oznaczył cenę jako „do negocjacji”." : null,
  ].filter(Boolean);
  if(days > 120) ctx.minus.push({w: 1, text: `Na rynku od ${days} dni — warto dopytać, dlaczego się nie sprzedaje`});
  return {score, html: `<ul class="list-disc space-y-1 pl-5">${facts.map(f => `<li>${f}</li>`).join("")}</ul>` +
    (p.base ? `<div id="r-pricechart" class="mt-2 h-[190px]"></div>` : "") +
    note(`Porównanie z aktywnymi ofertami z tej mapy (po zwinięciu duplikatów). To ceny ofertowe, nie transakcyjne.
      Ocena: 5 przy medianie, 10 przy cenie o 25% niższej, 0 przy cenie o 25% wyższej.`),
    mount: () => p.base && priceChart($("#r-pricechart"), p.base.map(x => x.pm), o.pm, p.baseMed)};
}
function priceChart(el, values, mine, med){
  const sorted = [...values, mine].sort((a, b) => a - b);
  const lo = sorted[Math.floor(sorted.length * .02)], hi = sorted[Math.min(sorted.length - 1, Math.ceil(sorted.length * .98))];
  const N = 22, step = (hi - lo) / N || 1, bins = new Array(N).fill(0);
  for(const v of values) bins[clamp(Math.floor((v - lo) / step), 0, N - 1)]++;
  const at = v => clamp((v - lo) / step, 0, N) - .5;  // pozycja wartości na osi kategorii
  const chart = chartInit(el);
  charts.push(chart);
  const mark = (v, name, color) => ({xAxis: at(v), lineStyle: {color, width: 2, type: "solid"},
    label: {formatter: `${name}\n${fmtN(v)} zł/m²`, color, fontSize: 11, fontWeight: 700}});
  chart.setOption({
    animationDuration: 300, textStyle: CHART_BASE.textStyle,
    grid: {left: 46, right: 46, top: 38, bottom: 22},  // boki: miejsce na podpisy linii przy skraju wykresu
    tooltip: {...CHART_BASE.tooltip, trigger: "axis", formatter: ([b]) =>
      `${fmtN(lo + b.dataIndex * step)}–${fmtN(lo + (b.dataIndex + 1) * step)} zł/m²: <b>${b.value}</b> ofert`},
    xAxis: {type: "category", data: bins.map((_, i) => fmtN(lo + (i + .5) * step)), axisTick: {show: false},
      axisLine: {lineStyle: {color: CH.line}}, axisLabel: {color: CH.mut, fontSize: 10.5, interval: 3}},
    yAxis: {type: "value", show: false},
    series: [{type: "bar", data: bins, barCategoryGap: "12%", itemStyle: {color: "#c9d6f2", borderRadius: [2, 2, 0, 0]},
      markLine: {symbol: "none", silent: true,
        data: [mark(med, "mediana", CH.mut), mark(mine, "to mieszkanie", CH.acc)]}}],
  });
}

/* ---------- koszty ---------- */
// czynsz administracyjny wyłuskany z opisu („czynsz … 650 zł”), jeśli sprzedający go podał
function rentFromText(o){
  const m = (o.dsc || "").match(/czynsz[^.\n]{0,60}?(\d[\d\s.,]{1,7})\s*(?:zł|pln)/i);
  if(!m) return null;
  const v = Math.round(+m[1].replace(/\s/g, "").replace(",", "."));
  return v >= 100 && v <= 5000 ? v : null;
}
function costsSection(ctx){
  const {o} = ctx;
  if(o.p == null) return {html: `<p class="text-mut">Ogłoszenie nie podaje ceny.</p>`};
  const secondary = o.mk !== "Pierwotny";
  const found = rentFromText(o);
  const field = (id, label, value, step, unit) => `<label class="block"><span class="f-title mb-0.5 block">${label}</span>
    <span class="flex items-center gap-1"><input id="${id}" type="number" value="${value}" step="${step}" min="0"
      class="field w-24"><span class="text-mut">${unit}</span></span></label>`;
  return {html: `<div class="noprint mb-3 flex flex-wrap items-end gap-x-4 gap-y-2">
      ${field("rc-down", "wkład własny", S.loan.down, 5, "%")}
      ${field("rc-rate", "oprocentowanie", S.loan.rate, .1, "%")}
      ${field("rc-years", "okres kredytu", S.loan.years, 1, "lat")}
      ${field("rc-other", "notariusz, sąd, pośrednik", S.loan.other, .1, "% ceny")}
      ${field("rc-rent", "czynsz administracyjny", found ?? "", 50, "zł/mies.")}
      ${secondary ? `<label class="chk pb-1.5"><input id="rc-first" type="checkbox" ${S.loan.first ? "checked" : ""}>
        pierwsze mieszkanie (bez PCC)</label>` : ""}</div>
    <div id="rc-out"></div>` +
    note(`${found ? `Czynsz ${fmtN(found)} zł odczytany z opisu ogłoszenia — sprawdź, co obejmuje. ` : "Czynszu nie ma w opisie — wpisz go po rozmowie ze sprzedającym. "}
      Rata równa, bez prowizji i ubezpieczeń banku. ${secondary ? "PCC to 2% ceny na rynku wtórnym (kupujący pierwsze mieszkanie są z niego zwolnieni). "
        : "Rynek pierwotny: bez PCC, ale cena zwykle dotyczy stanu deweloperskiego — dolicz wykończenie. "}
      Pozostałe koszty transakcji to założenie do zmiany, nie wyliczenie.`),
    mount: () => {
      const calc = () => {
        const num = id => Math.max(0, +$(id).value || 0);
        S.loan = {down: Math.min(100, num("#rc-down")), rate: num("#rc-rate"), years: Math.max(1, num("#rc-years")),
          other: num("#rc-other"), first: !!$("#rc-first")?.checked};
        saveSettings();
        const {down, rate, years, other, first} = S.loan, rent = num("#rc-rent");
        const own = o.p * down / 100, loan = o.p - own, r = rate / 1200, n = years * 12;
        const pay = !loan ? 0 : r ? loan * r / (1 - (1 + r) ** -n) : loan / n;
        const pcc = secondary && !first ? o.p * .02 : 0, fees = o.p * other / 100;
        const tile = (v, label, sub = "") => `<div class="rounded-lg bg-acc-soft px-3 py-2"><b class="block text-[17px]">${v}</b>
          <span class="text-xs text-mut">${label}</span>${sub ? `<div class="text-[11px] text-mut">${sub}</div>` : ""}</div>`;
        $("#rc-out").innerHTML = `<div class="grid grid-cols-[repeat(auto-fit,minmax(170px,1fr))] gap-2">
          ${tile(fmtP(own + pcc + fees), "gotówka na start", `wkład ${fmtP(own)}${pcc ? ` + PCC ${fmtP(pcc)}` : ""} + opłaty ${fmtP(fees)}`)}
          ${tile(fmtP(pay), "rata miesięczna", `kredyt ${fmtP(loan)} na ${years} ${plural(years, "rok", "lata", "lat")}, ${fmt1(rate)}%`)}
          ${tile(fmtP(pay + rent), "miesięcznie razem", rent ? `rata + czynsz ${fmtP(rent)}; bez prądu, gazu, internetu` : "sama rata — uzupełnij czynsz")}
          ${tile(fmtP(pay * n - loan), "odsetki przez cały okres", `łącznie do spłaty ${fmtP(pay * n)}`)}</div>`;
        setTake("costs", `rata ok. ${fmtP(pay)} miesięcznie · ${fmtP(own + pcc + fees)} gotówki na start`);
      };
      $("#rs-costs").addEventListener("input", calc);
      calc();
    }};
}

/* ---------- hałas i uciążliwości ---------- */
let jsonpN = 0;
// JSONP zamiast fetch: strona otwarta z pliku (file://) nie zawsze może czytać odpowiedzi innych serwerów
function jsonp(url){
  return new Promise((resolve, reject) => {
    const cb = "reportCb" + jsonpN++, script = document.createElement("script");
    const done = () => { clearTimeout(timer); delete window[cb]; script.remove(); };
    const timer = setTimeout(() => { done(); reject(new Error("timeout")); }, 20000);
    window[cb] = data => { done(); resolve(data); };
    script.onerror = () => { done(); reject(new Error("network")); };
    script.src = `${url}&callback=${cb}`;
    document.head.append(script);
  });
}
const noiseCache = new Map();  // id oferty → obietnica wyniku
function noiseAt(o){
  if(!noiseCache.has(o.id)){
    const band = layer => jsonp(`${NOISE_SERVICE}/${layer}/query?geometry=${o.lon},${o.lat}&geometryType=esriGeometryPoint` +
      "&inSR=4326&spatialRel=esriSpatialRelIntersects&outFields=ISOV1,ISOV2&returnGeometry=false&f=json")
      .then(d => {
        if(d.error) throw new Error(d.error.message);
        // przedział decybeli [od, do] strefy, w której leży punkt; null = poniżej progu mapy
        const top = (d.features || []).map(f => f.attributes).sort((a, b) => b.ISOV1 - a.ISOV1)[0];
        return top && top.ISOV1 >= 50 ? [Math.round(top.ISOV1), Math.round(top.ISOV2)] : null;
      });
    const all = Promise.all(NOISE_LAYERS.map(([name, day, night]) =>
      Promise.all([band(day), band(night)]).then(([d, n]) => ({name, day: d, night: n}))));
    all.catch(() => noiseCache.delete(o.id));  // nieudane zapytanie można powtórzyć
    noiseCache.set(o.id, all);
  }
  return noiseCache.get(o.id);
}
const NOISE_SCORE = [[50, 9.5], [55, 8], [60, 6.5], [65, 4.5], [70, 2.5], [75, 1], [Infinity, 0]];
const NOISE_WORD = [[55, "cicho"], [60, "umiarkowanie"], [65, "głośno — ruch słychać przy otwartym oknie"],
  [70, "bardzo głośno — powyżej poziomu dopuszczalnego dla osiedli"], [Infinity, "skrajnie głośno"]];
const bandText = b => b ? (b[1] >= 100 ? `ponad ${b[0]} dB` : `${b[0]}–${b[1]} dB`) : "poniżej 50 dB";
function noiseSection(ctx){
  if(!ctx.located) return {html: noLocation()};
  const {o} = ctx, items = [];
  const near = id => ctx.amen.find(a => a.cat.id === id);
  const clubs = near("nightclub")?.list.filter(x => x.d <= 200) || [];
  if(clubs.length) items.push(`Klub nocny ${fmtD(clubs[0].d)} drogi od mieszkania (${esc(clubs[0].name || "bez nazwy")}) — możliwy hałas w nocy.`);
  const bars = ctx.covered ? (OSM.pts.bar || []).filter(p => distM(o.lat, o.lon, p[0], p[1]) <= 100).length : 0;
  if(bars >= 2) items.push(`${bars} bary lub puby w promieniu 100 m — wieczorami może być gwarno.`);
  const tram = ctx.transit.find(x => x.t.id === "tram")?.nearest;
  if(tram && tram.d <= 70) items.push(`Przystanek tramwajowy ${fmtD(tram.d)} od mieszkania — wygodnie, ale tramwaj słychać.`);
  const rail = ctx.transit.find(x => x.t.id === "rail")?.nearest;
  if(rail && rail.d <= 250) items.push(`Stacja kolejowa ${fmtD(rail.d)} od mieszkania (${esc(rail.name)}) — sprawdź, czy okna wychodzą na tory.`);
  const airport = TRANSIT_POINTS.find(p => p.id === "airport")?.pts[0];
  if(airport && distM(o.lat, o.lon, airport[0], airport[1]) <= 5000)
    items.push(`Lotnisko ${fmtD(distM(o.lat, o.lon, airport[0], airport[1]))} stąd — możliwe przeloty nad budynkiem.`);
  if(clubs.length) ctx.minus.push({w: 2, text: `Klub nocny ${fmtD(clubs[0].d)} od mieszkania`});
  ctx.waiting.add("noise");
  return {html: `<div id="r-noise"><p class="text-mut">pobieram poziom hałasu z miejskiej mapy akustycznej…</p></div>
    <h3 class="f-title mt-3">Możliwe uciążliwości w sąsiedztwie</h3>` +
    (items.length ? `<ul class="list-disc space-y-1 pl-5">${items.map(i => `<li>${i}</li>`).join("")}</ul>`
      : `<p class="text-mut">W danych nie ma nic niepokojącego w bezpośrednim sąsiedztwie (kluby, bary, tory, lotnisko).</p>`) +
    note(`Hałas: strategiczna mapa akustyczna Krakowa 2022 (MSIP), wartość dla punktu, w którym stoi pinezka ogłoszenia.
      L<sub>DWN</sub> to średnia z całej doby z karą za wieczór i noc, L<sub>N</sub> — sama noc. Dopuszczalne dla zabudowy
      wielorodzinnej: ${NOISE_LIMIT.day} dB (doba) i ${NOISE_LIMIT.night} dB (noc). Mapę hałasu można włączyć jako warstwę na mapie obok.`),
    mount: () => noiseAt(o).then(rows => {
      if(ctx !== current || !$("#r-noise")) return;
      ctx.waiting.delete("noise");
      const inCity = inRing(o.lat, o.lon, KRAKOW_BOUNDARY);
      const loud = Math.max(0, ...rows.map(r => r.day?.[0] ?? 0)), night = Math.max(0, ...rows.map(r => r.night?.[0] ?? 0));
      if(!inCity && !loud){
        $("#r-noise").innerHTML = `<p class="text-mut">Mieszkanie leży poza Krakowem — mapa akustyczna miasta go nie obejmuje.</p>`;
        ctx.takes.noise = "poza zasięgiem mapy akustycznej";
        refreshSummary(ctx);
        return;
      }
      ctx.scores.noise = NOISE_SCORE.find(([lim]) => loud < lim)[1];
      const word = NOISE_WORD.find(([lim]) => loud < lim)[1];
      const band = bandText(rows.find(r => (r.day?.[0] ?? 0) === loud)?.day ?? null);
      ctx.noise = {loud, word: word.split(" —")[0], db: band, band: `${band} w ciągu doby`};
      ctx.takes.noise = `${ctx.noise.word} · ${ctx.noise.band}${items.length ? ` · ${items.length} ${plural(items.length, "uwaga", "uwagi", "uwag")} o sąsiedztwie` : ""}`;
      if(loud >= 65) ctx.minus.push({w: 3, text: `Hałas ${bandText(rows.find(r => r.day?.[0] === loud).day)} w ciągu doby (${word.split(" —")[0]})`});
      else if(loud < 55) ctx.plus.push({w: 2.5, text: "Cicha lokalizacja: poniżej 55 dB według mapy akustycznej"});
      const cell = (b, limit) => `<td class="border-b border-line px-2 py-1 tabular-nums ${
        b && b[0] >= limit ? "font-semibold text-bad" : b ? "" : "text-mut"}">${bandText(b)}</td>`;
      $("#r-noise").innerHTML = `<p>W tym miejscu jest <b class="${loud >= 65 ? "text-bad" : loud < 55 ? "text-good" : ""}">${word}</b>${
        night >= NOISE_LIMIT.night - 1 ? `; w nocy ${night} dB lub więcej — spanie przy otwartym oknie może być trudne` : ""}.</p>
        <table class="mt-2 text-[13px]"><tr class="text-left text-xs text-mut"><th class="px-2 font-normal">źródło</th>
          <th class="px-2 font-normal">cała doba (L<sub>DWN</sub>)</th><th class="px-2 font-normal">noc (L<sub>N</sub>)</th></tr>
        ${rows.map(r => `<tr><td class="border-b border-line px-2 py-1">hałas ${r.name}</td>
          ${cell(r.day, NOISE_LIMIT.day)}${cell(r.night, NOISE_LIMIT.night)}</tr>`).join("")}</table>`;
      refreshSummary(ctx);
    }).catch(() => {
      ctx.waiting.delete("noise");
      ctx.takes.noise = "nie udało się pobrać poziomu hałasu";
      refreshSummary(ctx);
      if(ctx === current && $("#r-noise")) $("#r-noise").innerHTML = `<p class="text-mut">Nie udało się pobrać poziomu hałasu
        (brak sieci albo serwer miejskiej mapy akustycznej nie odpowiada). Otwórz raport ponownie, żeby spróbować jeszcze raz.</p>`;
    })};
}

/* ---------- budynek i mieszkanie: co wynika z ogłoszenia ---------- */
const WORD_END = "(?![a-ząćęłńóśźż])";  // koniec słowa (\b nie rozumie polskich liter)
const FEATURES = [
  ["balkon, loggia lub taras", `balkon|loggi|taras`, true],
  ["ogródek", `ogr[oó]d(ek|ka|kiem|ku)?${WORD_END}`, false],
  ["winda", `wind(a|ą|ę|y|zie)${WORD_END}`, true],
  ["piwnica lub komórka lokatorska", `piwnic|kom[oó]rk\\S* lokatorsk`, true],
  ["garaż lub miejsce postojowe", `gara[żz]|miejsc\\S* postojow|miejsc\\S* parkingow|parking`, true],
  ["klimatyzacja", `klimatyzac`, false],
  ["umeblowane lub wyposażone", `umeblowan|wyposa[żz]on`, false],
  ["ogrzewanie miejskie", `ogrzewanie miejskie|mpec|sieci miejskiej|c\\.?o\\.? miejskie`, false],
  ["ogrzewanie gazowe", `ogrzewanie gazowe|piec\\S* gazow|koci[oó]ł\\S* gazow|junkers`, false],
  ["ogrzewanie podłogowe", `ogrzewanie pod[łl]ogowe|pod[łl]og[oó]wk`, false],
  ["po remoncie", `po (generalnym |kapitalnym )?remoncie|wyremontowan|odświeżon`, false],
  ["do remontu", `do (generalnego |kapitalnego )?remontu|do odświeżenia|wymaga remontu`, false],
  ["stan deweloperski", `stan\\S* dewelopersk`, false],
  ["księga wieczysta", `ksi[ęe]g\\S* wieczyst`, true],
  ["spółdzielcze własnościowe prawo", `sp[oó]łdzielcz`, false],
  ["monitoring lub ochrona", `monitoring|ochron(a|ą|y|ę)${WORD_END}|portier`, false],
  ["układ dwustronny lub rozkładowy", `dwustronn|rozkładow`, false],
  ["rowerownia lub wózkownia", `rowerowni|w[oó]zkowni`, false],
].map(([label, re, key]) => ({label, re: new RegExp(re, "i"), key}));
function features(o){
  const text = `${o.t || ""}\n${o.dsc || ""}`;
  return FEATURES.map(f => {
    const m = f.re.exec(text);
    if(!m) return {...f, state: "none"};
    // „brak windy”, „bez balkonu” — wzmianka jest, ale o braku
    const before = text.slice(Math.max(0, m.index - 22), m.index).toLowerCase();
    const snippet = text.slice(Math.max(0, m.index - 50), m.index + 70).replace(/\s+/g, " ").trim();
    return {...f, state: /(brak|bez|nie ma|nie posiada)\s+(\S+\s+)?$/.test(before) ? "no" : "yes", snippet};
  });
}
const ERAS = [
  [1945, "kamienica lub budynek przedwojenny", "wysokie pomieszczenia i grube mury, ale często stare instalacje, drewniane stropy i brak windy"],
  [1966, "wczesne budownictwo powojenne", "zwykle cegła, niewysokie bloki, małe kuchnie; instalacje mogą wymagać wymiany"],
  [1991, "blok z czasów PRL, najpewniej wielka płyta", "ustawne mieszkania i dużo zieleni między blokami; sprawdź docieplenie, piony i stan klatki"],
  [2006, "budownictwo z lat 90. i początku 2000.", "różna jakość wykonania; zwykle cegła lub pustak, częściej miejsca postojowe"],
  [Infinity, "nowe budownictwo", "winda, garaż i dobra izolacja cieplna, ale gęstsza zabudowa i cieńsze ściany między lokalami"],
];
function buildingSection(ctx){
  const {o} = ctx, year = new Date(GEN).getFullYear();
  const f = ctx.features, yes = f.filter(x => x.state === "yes"), no = f.filter(x => x.state === "no");
  const silent = f.filter(x => x.state === "none" && x.key);
  const chip = (x, cls) => `<span class="rounded-full px-2.5 py-0.5 text-[12.5px] ${cls}" ${
    x.snippet ? `title="„…${esc(x.snippet)}…”"` : ""}>${esc(x.label)}</span>`;
  const era = o.by ? (o.by > year ? ["budynek w budowie", `planowane oddanie w ${o.by} r. — mieszkania nie da się jeszcze obejrzeć w gotowym budynku`]
    : ERAS.find(([limit]) => o.by < limit).slice(1)) : null;
  const floor = /^parter/i.test(o.f || "") ? "Parter: łatwy dostęp i czasem ogródek, ale mniej prywatności i światła."
    : +o.f >= 4 && !yes.some(x => x.label === "winda") ? `Piętro ${o.f}, a ogłoszenie nie wspomina o windzie — warto zapytać.` : null;
  if(yes.some(x => x.label.startsWith("balkon"))) ctx.plus.push({w: 1.5, text: "Balkon, loggia lub taras (według opisu)"});
  if(yes.some(x => x.label.startsWith("garaż"))) ctx.plus.push({w: 1.5, text: "Garaż lub miejsce postojowe (według opisu)"});
  if(yes.some(x => x.label === "do remontu")) ctx.minus.push({w: 2, text: "Mieszkanie do remontu (według opisu)"});
  if(o.rad > 0) ctx.minus.push({w: 2, text: "Lokalizacja przybliżona — odległości w raporcie są orientacyjne"});
  const row = (label, body) => body ? `<div class="mt-2"><div class="f-title mb-1">${label}</div>
    <div class="flex flex-wrap gap-1.5">${body}</div></div>` : "";
  return {html: `<ul class="list-disc space-y-1 pl-5">
      ${era ? `<li>Rok budowy <b>${o.by}</b> — ${era[0]}: ${era[1]}.</li>` : `<li>Ogłoszenie nie podaje roku budowy — zapytaj o niego i o ostatnie remonty budynku.</li>`}
      ${floor ? `<li>${floor}</li>` : ""}
      <li>${o.b ? "Sprzedaje firma (biuro lub deweloper) — ustal, kto płaci prowizję pośrednika." : "Sprzedaje osoba prywatna — zwykle bez prowizji pośrednika."}</li>
      ${o.mk ? `<li>Rynek ${esc(o.mk.toLowerCase())}.</li>` : ""}</ul>
    ${row("W ogłoszeniu wspomniano", yes.map(x => chip(x, "bg-[#e3f4ea] text-good")).join(""))}
    ${row("Wspomniano jako brak", no.map(x => chip(x, "bg-[#fdecec] text-bad")).join(""))}
    ${row("Nie ma o tym słowa — zapytaj", silent.map(x => chip(x, "bg-page text-mut")).join(""))}` +
    note("Cechy są wyszukiwane w tytule i opisie ogłoszenia po słowach kluczowych — najedź na etykietę, żeby zobaczyć fragment opisu. To podpowiedź, co sprawdzić, a nie potwierdzenie stanu faktycznego.")};
}

/* ---------- podobne oferty ---------- */
// osiem najtańszych (w przeliczeniu na metr) podobnych ofert: [{x: oferta, n: ile ogłoszeń tego samego mieszkania}]
function similarRows(ctx){
  // to samo mieszkanie bywa wystawione przez kilka biur (ta sama cena i metraż) — w tabeli raz, z licznikiem
  const same = new Map();
  for(const x of [...(ctx.price?.similar || [])].sort((a, b) => a.pm - b.pm)){
    const key = `${zl(x)}|${x.a}`;
    same.has(key) ? same.get(key).n++ : same.set(key, {x, n: 1});
  }
  return [...same.values()].slice(0, 8);
}
function similarSection(ctx){
  const {o, price: p} = ctx;
  if(!p || !p.similar.length) return {html: `<p class="text-mut">Brak podobnych ofert w okolicy.</p>`};
  const rows = similarRows(ctx);
  // do porównania zaznaczone są na starcie trzy pierwsze; najwyżej tyle, bo czwartą kolumną jest ta oferta
  ctx.simPick = rows.slice(0, 3).map(r => r.x.id);
  const td = "border-b border-line px-2 py-1.5";
  return {html: `<div class="overflow-x-auto"><table class="w-full text-[13px] tabular-nums">
    <tr class="text-left text-xs text-mut"><th class="noprint px-2 font-normal" title="Zaznacz oferty do porównania">por.</th><th class="px-2 font-normal">ogłoszenie</th><th class="px-2 font-normal">cena</th>
      <th class="px-2 font-normal">metraż</th><th class="px-2 font-normal">zł/m²</th><th class="px-2 font-normal">względem tego</th>
      ${ctx.located ? `<th class="px-2 font-normal">odległość</th>` : ""}</tr>
    ${rows.map(({x, n}, i) => `<tr data-open="${esc(x.id)}" class="cursor-pointer hover:bg-[#f7f9fc]" title="Otwórz raport tej oferty">
      <td class="noprint ${td}" title="Zaznacz do porównania"><input type="checkbox" data-pick="${esc(x.id)}" class="accent-acc" ${i < 3 ? "checked" : ""}></td>
      <td class="${td} max-w-72 truncate font-semibold text-acc">${esc(x.t)}${
        n > 1 ? ` <span class="font-normal text-mut">(${n} ogłoszenia)</span>` : ""}</td><td class="${td}">${fmtP(x.p)}</td>
      <td class="${td}">${x.a} m²</td><td class="${td}">${fmtN(x.pm)}</td>
      <td class="${td} ${x.pm < o.pm ? "text-good" : "text-bad"}">${pct(x.pm / o.pm - 1)}</td>
      ${ctx.located ? `<td class="${td}">${fmtD(distM(o.lat, o.lon, x.lat, x.lon))}</td>` : ""}</tr>`).join("")}</table></div>
    <button type="button" id="rsimgo" class="noprint mt-2 cursor-pointer rounded-lg bg-acc px-3 py-1.5 text-[13px] font-semibold text-white
      hover:bg-acc/90 disabled:cursor-default disabled:opacity-40"></button>` +
    note(`Najtańsze w przeliczeniu na metr z ${p.similar.length} ofert o tej samej liczbie pokoi i metrażu ±20%${
      ctx.located ? ` w promieniu ${fmtD(SIMILAR_M)}` : " w tej dzielnicy"}. Kliknięcie wiersza otwiera raport tej oferty; zaznaczone (najwyżej trzy) trafiają do porównania z tą ofertą.`),
    mount: () => {
      const sync = () => {
        const n = ctx.simPick.length, go = $("#rsimgo");
        go.disabled = !n;
        go.textContent = n ? `Porównaj tę ofertę z ${n} ${plural(n, "zaznaczoną", "zaznaczonymi", "zaznaczonymi")}` : "Zaznacz oferty do porównania";
      };
      $("#rs-similar").addEventListener("click", e => {
        if(e.target.id === "rsimgo") return compareSimilar(ctx);
        if(e.target.closest("td")?.querySelector("[data-pick]")) return;  // pole wyboru nie otwiera raportu
        const row = e.target.closest("[data-open]");
        if(row) openReport(row.dataset.open);
      });
      $("#rs-similar").addEventListener("change", e => {
        const id = e.target.dataset.pick;
        if(!id) return;
        if(e.target.checked && ctx.simPick.length >= 3){
          e.target.checked = false;
          return toast("Do porównania z tą ofertą można zaznaczyć najwyżej trzy.");
        }
        ctx.simPick = e.target.checked ? [...ctx.simPick, id] : ctx.simPick.filter(x => x !== id);
        sync();
      });
      sync();
    }};
}

/* ---------- przed oględzinami ---------- */
function checklistSection(ctx){
  const {o, price: p} = ctx, year = new Date(GEN).getFullYear();
  const has = label => ctx.features.some(x => x.label.startsWith(label) && x.state === "yes");
  const items = [
    [true, "Poproś o numer księgi wieczystej i sprawdź w niej właściciela oraz działy III i IV (roszczenia, służebności, hipoteki)."],
    [true, "Zapytaj o czynsz administracyjny i co obejmuje (ogrzewanie, woda, fundusz remontowy) oraz o rachunki za media z ostatniego roku."],
    [true, "Obejrzyj mieszkanie także wieczorem i w dzień roboczy: hałas, nasłonecznienie, parkowanie, sąsiedzi."],
    [o.rad > 0, "Lokalizacja w ogłoszeniu jest przybliżona — poproś o dokładny adres przed wizytą, bo od niego zależy większość tego raportu."],
    [o.by && o.by > year, "Budynek w budowie: sprawdź termin oddania, rachunek powierniczy, prospekt informacyjny i standard wykończenia części wspólnych."],
    [o.mk === "Pierwotny", "Rynek pierwotny: ustal, co dokładnie jest w cenie (miejsce postojowe, komórka, wykończenie) i ile kosztuje reszta."],
    [o.by && o.by < 1970, "Stary budynek: zapytaj o wymianę instalacji elektrycznej i pionów wodno-kanalizacyjnych, stan dachu i stropów."],
    [o.by >= 1966 && o.by < 1991, "Blok z wielkiej płyty: zapytaj o docieplenie, wymianę wind i planowane remonty (oraz podwyżki funduszu remontowego)."],
    [!o.by, "Ustal rok budowy i technologię — wpływają na koszty ogrzewania i przyszłe remonty."],
    [!has("winda") && +o.f >= 3, "Zapytaj o windę — ogłoszenie o niej nie wspomina, a mieszkanie jest na wyższym piętrze."],
    [!has("garaż"), "Sprawdź, gdzie się parkuje: strefa płatnego parkowania, miejsca dla mieszkańców, cena miejsca w garażu."],
    [!has("piwnica"), "Zapytaj o piwnicę lub komórkę lokatorską i czy jest w cenie."],
    [ctx.features.some(x => x.label === "spółdzielcze własnościowe prawo" && x.state === "yes"),
      "Spółdzielcze własnościowe prawo do lokalu: sprawdź, czy ma księgę wieczystą i jaki jest stan prawny gruntu."],
    [ctx.scores.noise != null && ctx.scores.noise <= 4.5, "Głośna okolica: sprawdź, na którą stronę wychodzą sypialnie i jakie są okna."],
    [p && p.diff != null && p.diff <= -.2, "Cena wyraźnie niższa niż w podobnych ofertach — dopytaj o powód (stan prawny, udział, remont, pilna sprzedaż)."],
    [o.h && o.h.length > 2, "Cena była zmieniana kilka razy — sprzedający może być otwarty na negocjacje."],
    [o.b === 1, "Ustal na piśmie, kto płaci prowizję biura i ile ona wynosi."],
    [copies(o).length > 1 && new Set(copies(o).map(zl)).size > 1, "To samo ogłoszenie ma różne ceny na różnych portalach — powołaj się na niższą."],
  ].filter(([when]) => when).map(([, text]) => text);
  return {html: `<ul class="space-y-1.5">${items.map(text => `<li><label class="flex cursor-pointer items-start gap-2">
    <input type="checkbox" class="mt-1 shrink-0 accent-acc"><span>${text}</span></label></li>`).join("")}</ul>` +
    note("Lista jest dobrana do tego ogłoszenia; zaznaczenia nie są zapisywane.")};
}

/* ---------- mapa obok raportu ---------- */
let rmap = null, hilite = null, picking = null;
function mountMap(ctx){
  const {o} = ctx, here = [o.lat, o.lon];
  rmap = L.map("r-map", {preferCanvas: true}).setView(here, 15);
  L.tileLayer(carto("voyager"), CARTO_OPTS).addTo(rmap);
  // zasięg spaceru: odcinki sieci, do których da się dojść w wybranym czasie (bez sieci — okrąg)
  let reachArea;
  if(ctx.walk.dist){
    const g = graph(), segs = [], lim = ctx.R - ctx.walk.off;
    for(let u = 0; u < g.n; u++){
      if(!(ctx.walk.dist[u] <= lim)) continue;
      for(let e = g.head[u]; e < g.head[u + 1]; e++){
        const v = g.to[e];
        if(v > u && ctx.walk.dist[v] <= lim) segs.push([[g.lat[u], g.lon[u]], [g.lat[v], g.lon[v]]]);
      }
    }
    reachArea = L.polyline(segs.length ? segs : [here, here], {color: CH.acc, weight: 3, opacity: .28, interactive: false});
  } else {
    reachArea = L.circle(here, {radius: ctx.R / DETOUR, color: CH.acc, weight: 1.5, dashArray: "6 6", fillOpacity: .04, interactive: false});
  }
  reachArea.addTo(rmap);
  // warstwy przełączane przyciskami nad mapą (własnymi — kontrolka Leafleta potrzebuje obrazków z CDN)
  const overlays = [{name: `Zasięg ${S.walk} min`, color: CH.acc, layer: reachArea, on: true}];
  const group = (name, color, markers, on = true) => {
    if(!markers.length) return;
    const layer = L.layerGroup(markers);
    if(on) layer.addTo(rmap);
    overlays.push({name, color, layer, on});
  };
  const dotAt = (x, color, label) => L.circleMarker([x.lat, x.lon], {radius: 6, color: "#fff", weight: 1.5, fillColor: color, fillOpacity: .95})
    .bindTooltip(label, {direction: "top"});
  for(const sec of SECTIONS.filter(s => s.amenities)){
    group(sec.short || sec.title, SEC_COLOR[sec.id], ctx.amen.filter(a => a.cat.sec === sec.id && a.cat.w > 0 && a.list[0]).map(a =>
      dotAt(a.list[0], SEC_COLOR[sec.id], `<b>${esc(a.cat.label)}</b><br>${esc(a.list[0].name || "bez nazwy")} · ${walkText(a.list[0])} pieszo`)));
  }
  group("Obiekty z mapy", SEC_COLOR.spots, (ctx.spots || []).map(s =>
    dotAt(s.list[0], SEC_COLOR.spots, `<b>${esc(s.kind.label)}</b><br>${esc(s.list[0].name)} · ${walkText(s.list[0])} pieszo`)));
  group("Przystanki", SEC_COLOR.transit, ctx.transit.filter(x => x.nearest).map(x =>
    L.marker([x.nearest.lat, x.nearest.lon], {icon: L.divIcon({className: "", iconSize: [12, 12],
      html: `<i class="block size-3 rounded-[2px] border-2 border-white" style="background:${SEC_COLOR.transit}"></i>`})})
      .bindTooltip(`<b>${esc(x.t.label)}</b><br>${esc(x.nearest.name)} · ${walkText(x.nearest)} pieszo`, {direction: "top"})));
  group("Cele", CH.ink, destinations().map(p => L.circleMarker([p.lat, p.lon],
    {radius: 7, color: CH.ink, weight: 2, fillColor: "#fff", fillOpacity: 1}).bindTooltip(esc(p.name), {direction: "top"})));
  group("Hałas", SEC_COLOR.noise, [new NoiseLayer("", {tileSize: 512, opacity: .6, maxZoom: 20,
    bounds: L.latLngBounds(KRAKOW_BOUNDARY).pad(.05),
    attribution: 'mapa hałasu 2022 &copy; <a href="https://msip.krakow.pl">MSIP Kraków</a>'})], false);
  $("#r-layers").innerHTML = overlays.map((v, i) => `<button type="button" data-layer="${i}"
    class="chip ${v.on ? "on" : ""} px-2 py-0.5 text-[12px]"><i class="mr-1 inline-block size-2.5 rounded-full border border-white"
    style="background:${v.color}"></i>${esc(v.name)}</button>`).join("");
  $("#r-layers").onclick = e => {
    const b = e.target.closest("[data-layer]");
    if(!b) return;
    const v = overlays[+b.dataset.layer];
    v.on = !v.on;
    b.classList.toggle("on", v.on);
    v.on ? v.layer.addTo(rmap) : v.layer.remove();
  };
  hilite = L.layerGroup().addTo(rmap);
  L.circleMarker(here, {radius: 10, color: "#fff", weight: 3, fillColor: CH.ink, fillOpacity: 1})
    .bindTooltip("to mieszkanie", {direction: "top"}).addTo(rmap);
  // rozmiar panelu ustala się chwilę po wstawieniu do strony i zmienia przy otwieraniu ustawień
  const home = reachArea.getBounds().pad(.15);
  ctx.home = () => rmap.fitBounds(home);
  let fitted = false;
  mapWatch = new ResizeObserver(() => {
    if(!rmap) return;
    rmap.invalidateSize();
    if(!fitted && $("#r-map").clientHeight > 50){ fitted = true; ctx.home(); }
  });
  mapWatch.observe($("#r-map"));
  // wskazywanie własnego miejsca dojazdu (panel „Dostosuj”)
  rmap.on("click", e => {
    if(!picking) return;
    S.places.push({name: picking, lat: +e.latlng.lat.toFixed(6), lon: +e.latlng.lng.toFixed(6)});
    picking = null;
    S.open = [...new Set([...S.open, "commute"])];
    saveSettings();
    render(ctx.o, "commute");
  });
  showPicking();
}
let mapWatch = null;
function showPicking(){
  const bar = $("#r-pick");
  bar.hidden = !picking;
  if(picking) bar.textContent = `Kliknij na mapie miejsce „${picking}”.`;
  $("#r-map").style.cursor = picking ? "crosshair" : "";
}
const HI = {color: "#e0a800", casing: "#6b4f00"};  // jak trasa linii na głównej mapie: odcina się od kafelków
// punkt z wiersza raportu: znacznik i — gdy znamy trasę pieszą — jej przebieg
function focusOn(ctx, f, fly){
  if(!rmap) return;
  hilite.clearLayers();
  const line = f.node != null && ctx.walk.dist ? walkPath(ctx.walk, f.node, f.lat, f.lon) : [[f.lat, f.lon], [ctx.o.lat, ctx.o.lon]];
  hilite.addLayer(L.polyline(line, {color: HI.casing, weight: 7, opacity: .5, interactive: false}));
  hilite.addLayer(L.polyline(line, {color: HI.color, weight: 4, dashArray: f.node == null ? "6 8" : null, interactive: false}));
  hilite.addLayer(L.circleMarker([f.lat, f.lon], {radius: 8, color: HI.casing, weight: 2, fillColor: HI.color, fillOpacity: 1})
    .bindTooltip(esc(f.label), {permanent: true, direction: "top", offset: [0, -6]}));
  if(fly || !rmap.getBounds().pad(-.08).contains([f.lat, f.lon])) rmap.fitBounds(L.latLngBounds(line).pad(.2), {maxZoom: 17});
}
// przejazd z sekcji dojazdów: dojścia po sieci pieszej, jazda od przystanku do przystanku
function showTrip(ctx, trip){
  if(!rmap) return;
  hilite.clearLayers();
  const all = [];
  let from = {lat: ctx.o.lat, lon: ctx.o.lon};
  for(const leg of trip.legs){
    let line;
    if(leg.v){
      line = leg.v.keys.slice(leg.bi, leg.ai + 1).map(key => { const s = NET.stops.get(key); return [s.lat, s.lon]; });
      hilite.addLayer(L.polyline(line, {color: HI.casing, weight: 8, opacity: .5, interactive: false}));
      hilite.addLayer(L.polyline(line, {color: HI.color, weight: 5, interactive: false}));
      for(const [i, text] of [[leg.bi, "wsiądź"], [leg.ai, "wysiądź"]]){
        const s = NET.stops.get(leg.v.keys[i]);
        hilite.addLayer(L.circleMarker([s.lat, s.lon], {radius: 6, color: HI.casing, weight: 2, fillColor: "#fff", fillOpacity: 1})
          .bindTooltip(`${text}: ${esc(s.name)} (${esc(leg.v.line)})`, {direction: "top"}));
      }
    } else {
      // dojście: z domu na przystanek albo z przystanku do celu — po sieci; przesiadka — prosto
      const end = leg.exit ? {lat: leg.search.lat, lon: leg.search.lon} : leg.at;
      line = leg.node != null && leg.search?.dist
        ? walkPath(leg.search, leg.node, leg.at.lat, leg.at.lon)
        : [[from.lat, from.lon], [end.lat, end.lon]];
      hilite.addLayer(L.polyline(line, {color: CH.ink, weight: 3, dashArray: "2 7", interactive: false}));
      if(leg.exit) hilite.addLayer(L.circleMarker([end.lat, end.lon], {radius: 8, color: HI.casing, weight: 2, fillColor: HI.color, fillOpacity: 1}));
    }
    all.push(...line);
    from = leg.at || from;
  }
  rmap.fitBounds(L.latLngBounds(all).pad(.12));
}

/* ---------- podsumowanie: ocena, kafelki „w skrócie”, plusy i minusy ---------- */
function overall(ctx){
  const w = PROFILES[S.profile].w;
  let sum = 0, weight = 0;
  for(const [id, s] of Object.entries(ctx.scores)){
    if(s == null || !w[id]) continue;
    sum += w[id] * s; weight += w[id];
  }
  return weight ? sum / weight : null;
}
// sześć liczb, po które sięga się najpierw; kliknięcie kafelka rozwija sekcję, z której pochodzi
function glance(ctx){
  const {o, price: p} = ctx, tiles = [];
  const tone = good => good == null ? "" : good ? "text-good" : "text-bad";
  if(p?.diff != null) tiles.push({go: "price", icon: "💰", label: "Cena za m²", value: pct(p.diff), sub: "względem podobnych ofert",
    cls: tone(Math.abs(p.diff) < .03 ? null : p.diff < 0)});
  if(ctx.located){
    const first = destinations()[0], c = ctx.commute?.[0];
    if(first) tiles.push({go: "commute", icon: "🧭", label: first.name, value: !ctx.commute ? "…" : c.min < Infinity ? `${c.min} min` : "—",
      sub: !ctx.commute ? "liczę dojazd" : c.min === c.trip?.min ? (c.trip.rides > 1 ? "komunikacją, z przesiadką" : "komunikacją, bez przesiadki") : "pieszo"});
    const stop = ctx.transit.filter(x => x.nearest && x.t.id !== "rail").sort((a, b) => a.nearest.d - b.nearest.d)[0];
    if(stop) tiles.push({go: "transit", icon: "🚏", label: "Przystanek", value: walkText(stop.nearest),
      sub: `${KIND_WORD[stop.t.id]}: ${stop.nearest.name}`, cls: tone(stop.nearest.d <= 400 ? true : stop.nearest.d > 800 ? false : null)});
    for(const [id, go, icon, label] of [["supermarket", "daily", "🛒", "Supermarket"], ["green", "leisure", "🌳", "Park lub las"]]){
      const a = ctx.amen.find(x => x.cat.id === id), x = a?.list[0];
      if(x) tiles.push({go, icon, label, value: walkText(x), sub: x.name || "bez nazwy",
        cls: tone(x.d <= a.cat.near ? true : x.d > 2 * a.cat.near ? false : null)});
    }
    if(ctx.noise || ctx.waiting.has("noise")) tiles.push({go: "noise", icon: "🔊", label: "Hałas",
      value: ctx.noise ? ctx.noise.db : "…", sub: ctx.noise ? ctx.noise.word : "pobieram z mapy akustycznej",
      cls: tone(!ctx.noise ? null : ctx.noise.loud >= 65 ? false : ctx.noise.loud < 55 ? true : null)});
  }
  return tiles.map(t => `<button type="button" data-go="${t.go}" class="rtile">
    <span class="flex items-center gap-1.5 text-[11.5px] text-mut"><span>${t.icon}</span><span class="truncate">${esc(t.label)}</span></span>
    <b class="block text-[19px]/[1.25] ${t.cls || ""}">${esc(t.value)}</b>
    <span class="block truncate text-[11.5px] text-mut">${esc(t.sub)}</span></button>`).join("");
}
function summaryHtml(ctx){
  const {o} = ctx, total = overall(ctx);
  const top = list => [...list].sort((a, b) => b.w - a.w).slice(0, 4);
  // pierścień oceny: obwód koła o promieniu 42 to ~264
  const ring = `<svg viewBox="0 0 100 100" class="size-[104px] shrink-0"><circle cx="50" cy="50" r="42" fill="none" stroke="#e9edf3" stroke-width="9"/>
    <circle cx="50" cy="50" r="42" fill="none" stroke="${scoreBar(total)}" stroke-width="9" stroke-linecap="round"
      stroke-dasharray="${(total ?? 0) * 26.39} 264" transform="rotate(-90 50 50)"/>
    <text x="50" y="50" text-anchor="middle" dominant-baseline="central" font-size="27" font-weight="800"
      fill="${CH.ink}">${total == null ? "—" : fmt1(total)}</text><text x="50" y="72" text-anchor="middle" font-size="9" fill="${CH.mut}">na 10</text></svg>`;
  const facts = [fmtP(o.p), o.pm ? fmtN(o.pm) + " zł/m²" : null, o.a ? String(o.a).replace(".", ",") + " m²" : null, o.r,
    o.f ? (/^\d+$/.test(o.f) ? `piętro ${o.f}` : o.f.toLowerCase()) : null, o.by ? `rok ${o.by}` : null,
    o.mk ? `rynek ${o.mk.toLowerCase()}` : null, o.d].filter(Boolean);
  const bars = SECTIONS.filter(s => s.id in ctx.scores).map(s => {
    const v = ctx.scores[s.id], weight = PROFILES[S.profile].w[s.id];
    return `<button type="button" data-go="${s.id}" class="flex w-full cursor-pointer items-center gap-2 rounded py-0.5 text-left hover:bg-[#f7f9fc]"
      title="${weight ? `waga w ocenie ogólnej: ${weight}` : "nie wchodzi do oceny ogólnej w tym profilu"}">
      <span class="w-40 shrink-0 truncate ${weight ? "" : "text-mut"}">${s.icon} ${esc(s.short || s.title)}</span>
      <span class="h-1.5 flex-1 overflow-hidden rounded-full bg-page"><i class="block h-full rounded-full"
        style="width:${(v ?? 0) * 10}%;background:${scoreBar(v)}"></i></span>
      <span class="w-7 text-right font-semibold tabular-nums">${v != null ? fmt1(v) : ctx.waiting.has(s.id) ? "…" : "—"}</span></button>`;
  }).join("");
  const warn = [
    o.rad > 0 ? "Sprzedający podał tylko przybliżoną lokalizację. Wszystkie odległości, hałas i dojazdy są liczone od pinezki, więc traktuj je orientacyjnie." : null,
    !ctx.located ? "Ogłoszenie nie ma lokalizacji na mapie — raport ogranicza się do ceny, kosztów i opisu." : null,
    ctx.located && OSM && !ctx.covered
      ? "Mieszkanie leży poza obszarem, dla którego pobrano dane o okolicy — sekcje o sklepach, zdrowiu, szkołach i zieleni są pominięte i nie wchodzą do oceny." : null,
    ctx.located && !ctx.walk.dist ? "W tym miejscu nie ma pobranej sieci ulic — czasy dojścia są szacowane z odległości w linii prostej (oznaczone „≈”)." : null,
  ].filter(Boolean);
  const list = (items, mark, cls) => items.length ? `<ul class="space-y-1">${items.map(x =>
    `<li class="flex gap-2"><span class="${cls} font-bold">${mark}</span><span>${esc(x.text)}</span></li>`).join("")}</ul>`
    : `<p class="text-mut">nic szczególnego</p>`;
  return `${warn.map(w => `<p class="mb-2 rounded-lg bg-[#fdf3d8] px-3 py-2 text-[13px] text-[#6b4c00]">${w}</p>`).join("")}
    <div class="flex flex-wrap items-center gap-x-5 gap-y-3">
      <div class="text-center">${ring}<div class="mt-0.5 text-[11px] text-mut">profil: ${esc(PROFILES[S.profile].label)}</div></div>
      <div class="min-w-64 flex-1">
        <div class="mb-2 flex flex-wrap gap-1.5">${facts.map((f, i) => `<span class="rounded-md px-2 py-0.5 text-[12.5px] ${
          i ? "bg-page" : "bg-ink font-bold text-white"}">${esc(f)}</span>`).join("")}</div>
        <div class="grid grid-cols-1 gap-x-5 text-[12.5px] sm:grid-cols-2">${bars}</div></div></div>
    <div class="mt-3 grid grid-cols-2 gap-2 sm:grid-cols-3">${glance(ctx)}</div>
    <div class="mt-3 grid gap-x-5 gap-y-3 text-[13px] sm:grid-cols-2">
      <div><h3 class="f-title">Mocne strony</h3>${list(top(ctx.plus), "+", "text-good")}</div>
      <div><h3 class="f-title">Na co uważać</h3>${list(top(ctx.minus), "−", "text-bad")}</div></div>`;
}
// wniosek sekcji (jedno zdanie w jej nagłówku) — część przychodzi później: hałas z sieci, dojazdy po wczytaniu tras
function setTake(id, text){
  if(!current) return;
  current.takes[id] = text;
  const slot = $(`#rs-${id} .rtake`);
  if(slot) slot.textContent = text;
}
// podsumowanie i plakietki ocen odświeżają się, gdy dojdą dane z sieci
function refreshSummary(ctx){
  if(ctx !== current) return;
  const body = $("#rs-summary .rbody");
  if(body) body.innerHTML = summaryHtml(ctx);
  for(const [id, s] of Object.entries(ctx.scores)){
    const slot = $(`#rs-${id} .rscore`);
    if(slot) slot.innerHTML = scoreBadge(s);
  }
  for(const [id, text] of Object.entries(ctx.takes)){
    const slot = $(`#rs-${id} .rtake`);
    if(slot) slot.textContent = text;
  }
}

/* ---------- sekcje i składanie raportu ---------- */
// take: wniosek w nagłówku zwiniętej sekcji; amenities: sekcja z punktami na mapie
const SECTIONS = [
  {id: "summary", title: "Podsumowanie", fixed: true},
  {id: "price", icon: "💰", title: "Cena na tle rynku", short: "Cena", scored: true, render: ctx => ({...priceSection(ctx),
    take: !ctx.price ? "brak ceny lub metrażu" : `${fmtN(ctx.o.pm)} zł/m²` + (ctx.price.diff == null ? " · za mało ofert do porównania"
      : ` · ${pct(ctx.price.diff)} względem podobnych ofert`)})},
  {id: "costs", icon: "🧮", title: "Koszty zakupu i miesięczne", render: ctx => ({take: "rata i gotówka na start", ...costsSection(ctx)})},
  {id: "commute", icon: "🧭", title: "Dojazdy do wybranych miejsc", render: commuteSection},
  {id: "transit", icon: "🚏", title: "Przystanki i linie", short: "Komunikacja", scored: true, render: transitSection},
  {id: "daily", icon: "🛒", title: "Zakupy i codzienne sprawy", short: "Zakupy i usługi", scored: true, amenities: true,
    render: ctx => amenitySection(ctx, "daily")},
  {id: "health", icon: "🩺", title: "Zdrowie", scored: true, amenities: true, render: ctx => amenitySection(ctx, "health")},
  {id: "education", icon: "🎒", title: "Dzieci i edukacja", scored: true, amenities: true, render: ctx => amenitySection(ctx, "education")},
  {id: "leisure", icon: "🌳", title: "Zieleń, sport i rekreacja", short: "Zieleń i sport", scored: true, amenities: true, render: ctx => {
    const out = amenitySection(ctx, "leisure");
    const big = ctx.amen.find(a => a.cat.id === "green")?.big;
    if(big && out.score != null) out.html = `<p class="mb-1 text-[13px]">Najbliższy duży teren zielony (od 10 ha): <b>${esc(big.name)}</b>,
      ${walkText(big)} pieszo do granicy (${fmtD(big.d)}).</p>` + out.html;
    return out;
  }},
  {id: "fun", icon: "🍽️", title: "Gastronomia i życie towarzyskie", short: "Gastronomia", scored: true, amenities: true,
    render: ctx => amenitySection(ctx, "fun")},
  {id: "spots", icon: "📍", title: "Wybrane obiekty z mapy", render: spotsSection},
  {id: "noise", icon: "🔊", title: "Hałas i uciążliwości", short: "Hałas", scored: true,
    render: ctx => ({take: ctx.located ? "sprawdzam poziom hałasu…" : "brak lokalizacji", ...noiseSection(ctx)})},
  {id: "building", icon: "🏢", title: "Budynek i mieszkanie", render: ctx => {
    const yes = ctx.features.filter(x => x.state === "yes").slice(0, 3).map(x => x.label.split(/,| lub /)[0]);
    return {...buildingSection(ctx), take: [ctx.o.by ? `rok budowy ${ctx.o.by}` : "rok budowy nieznany", ...yes].join(" · ")};
  }},
  {id: "similar", icon: "🏘️", title: "Podobne oferty w pobliżu", render: ctx => {
    const n = ctx.price?.similar.length || 0, cheaper = n ? ctx.price.similar.filter(x => x.pm < ctx.o.pm).length : 0;
    return {...similarSection(ctx), take: n ? `${n} ${plural(n, "podobna oferta", "podobne oferty", "podobnych ofert")} · ${cheaper} tańszych w przeliczeniu na m²`
      : "brak podobnych ofert"};
  }},
  {id: "checklist", icon: "✅", title: "Przed oględzinami", render: ctx => {
    const out = checklistSection(ctx), n = (out.html.match(/<li>/g) || []).length;
    return {...out, take: `${n} ${plural(n, "rzecz", "rzeczy", "rzeczy")} do sprawdzenia i zapytania`};
  }},
];
let current = null;        // kontekst otwartego raportu; wyniki zapytań do sieci sprawdzają, czy nadal aktualny
const charts = [];
function cleanup(){
  charts.splice(0).forEach(c => c.isDisposed() || c.dispose());
  mapWatch?.disconnect();
  if(rmap){ rmap.remove(); rmap = null; }
}
const chevron = `<svg viewBox="0 0 24 24" class="rchev size-4 shrink-0 text-mut" fill="none" stroke="currentColor" stroke-width="2.5"
  stroke-linecap="round" stroke-linejoin="round"><path d="m6 9 6 6 6-6"/></svg>`;
// Wszystko, co raport wie o ofercie, bez rysowania: kontekst (odległości, przystanki, cena,
// oceny i wnioski sekcji) oraz gotowe sekcje. Korzysta z tego raport (render) i porównanie ofert.
function build(o){
  const located = o.lat != null && o.lon != null;
  // R: zasięg spaceru w metrach drogi; focus i trips: to, co wiersze raportu pokazują na mapie
  const ctx = {o, R: S.walk * WALK_M_MIN, located, scores: {}, plus: [], minus: [], waiting: new Set(),
    takes: {}, focus: [], trips: [], placeWalk: new Map()};
  ctx.walk = located ? walkSearch(o.lat, o.lon, WALK_LIMIT_M) : null;
  ctx.covered = located && !!OSM && o.lat >= OSM.bounds[0] && o.lat <= OSM.bounds[2] && o.lon >= OSM.bounds[1] && o.lon <= OSM.bounds[3];
  ctx.amen = ctx.covered ? [...OSM.cats.filter(c => c.id !== "green").map(c => nearby(c, ctx)), greenNearby(ctx)] : [];
  ctx.transit = located ? transitInfo(ctx) : [];
  ctx.price = priceInfo(o, located);
  ctx.features = features(o);
  const shown = SECTIONS.filter(s => s.fixed || !S.off.includes(s.id));
  const parts = shown.filter(s => s.render).map(s => {
    const out = s.render(ctx);
    if(s.scored) ctx.scores[s.id] = out.score ?? null;
    ctx.takes[s.id] = out.take || "";
    return {s, out};
  });
  return {ctx, parts};
}
function render(o, scrollTo){
  cleanup();
  const {ctx, parts} = build(o), {located} = ctx;
  current = ctx;
  const card = ({s, out}) => {
    const open = S.open.includes(s.id);
    return `<section id="rs-${s.id}" class="rcard ${open ? "open" : ""}">
      <button type="button" class="rhead" data-toggle="${s.id}" aria-expanded="${open}">
        <span class="text-[19px]/none">${s.icon}</span>
        <span class="min-w-0 flex-1"><b class="block text-[14.5px]">${esc(s.title)}</b><span class="rtake">${esc(ctx.takes[s.id])}</span></span>
        <span class="rscore">${s.scored ? scoreBadge(ctx.scores[s.id]) : ""}</span>${chevron}</button>
      <div class="rbody" ${open ? "" : "hidden"}>${out.html}</div></section>`;
  };
  $("#rtitle").textContent = o.t || "Mieszkanie";
  $("#rsub").textContent = [o.d, copies(o).map(portalName).join(" + ")].filter(Boolean).join(" · ");
  $("#rsections").innerHTML = `<section id="rs-summary" class="rcard"><div class="rbody border-t-0!">${summaryHtml(ctx)}</div></section>
    <div class="noprint mb-2 flex items-center justify-between px-1 text-xs text-mut">
      <span>Kliknij sekcję, żeby ją rozwinąć. Najechanie na wiersz pokazuje trasę na mapie.</span>
      <button type="button" id="rall" class="cursor-pointer whitespace-nowrap text-acc hover:underline"></button></div>` +
    parts.map(card).join("") +
    `<section class="px-1 pt-2 pb-6 text-xs/[1.5] text-mut"><b>Skąd te dane i jak czytać oceny.</b>
      ${WALK ? `Czasy dojścia są liczone po sieci ulic, chodników i ścieżek z OpenStreetMap, od pinezki ogłoszenia, przy ${fmt1(WALK_M_MIN * 60 / 1000)} km/h;
        znak „≈” oznacza szacunek z linii prostej (+${Math.round((DETOUR - 1) * 100)}%) tam, gdzie sieci nie ma albo cel jest dalej niż ${WALK_LIMIT_M / 1000} km.`
        : `Sieci ulic nie udało się pobrać, więc czasy dojścia to odległość w linii prostej powiększona o ${Math.round((DETOUR - 1) * 100)}%, przy ${fmt1(WALK_M_MIN * 60 / 1000)} km/h.`}
      Sklepy, placówki i tereny zielone pochodzą z OpenStreetMap ${OSM ? `(stan z ${esc(OSM.fetched)})` : "(brak danych)"} — mapę tworzą
      wolontariusze, więc pojedynczych miejsc może brakować. Przystanki, linie i czasy przejazdu są z rozkładów ZTP Kraków
      i z ogólnopolskiego rozkładu kolejowego (mkuran.pl, dane PKP PLK). Ocena sekcji to średnia ważona: każda kategoria dostaje
      10 punktów, gdy do najbliższego miejsca jest „pod ręką” (np. 500 m drogi do supermarketu), i liniowo mniej aż do zera przy
      trzykrotnie dłuższej drodze. Ocena ogólna waży sekcje według wybranego profilu. Raport jest pomocą w porównywaniu ofert,
      a nie wyceną ani opinią o stanie prawnym czy technicznym mieszkania.</section>`;
  syncAll();
  $("#r-map").innerHTML = "";
  $("#rmappane").hidden = !located;
  // błąd w jednej sekcji (np. brak sieci przy mapie) nie może zatrzymać pozostałych
  for(const p of parts){
    try { p.out.mount?.(); } catch(err){ console.error(`raport, sekcja ${p.s.id}:`, err); }
  }
  if(located){
    try { mountMap(ctx); } catch(err){ console.error("raport, mapa:", err); }
  }
  fillTransit(ctx);
  renderSettings();
  const target = scrollTo && $(`#rs-${scrollTo}`);
  if(target) target.scrollIntoView({block: "start"}); else $("#rbody").scrollTop = 0;
}
// rozwinięcie i zwinięcie sekcji bez przerysowywania raportu; stan zostaje w ustawieniach
function setOpen(id, open){
  const card = $(`#rs-${id}`);
  if(!card || id === "summary") return;
  card.classList.toggle("open", open);
  card.querySelector(".rhead").setAttribute("aria-expanded", open);
  card.querySelector(".rbody").hidden = !open;
  S.open = open ? [...new Set([...S.open, id])] : S.open.filter(x => x !== id);
  saveSettings();
  syncAll();
}
const cards = () => [...document.querySelectorAll("#rsections .rcard[id]")].filter(c => c.id !== "rs-summary");
function syncAll(){
  const all = cards().every(c => c.classList.contains("open"));
  $("#rall").textContent = all ? "zwiń wszystkie" : "rozwiń wszystkie";
}

/* ---------- panel „Dostosuj” ---------- */
const fold = s => s.toLowerCase().normalize("NFD").replace(/[̀-ͯ]/g, "").replace(/ł/g, "l");
function destResults(){
  const q = fold($("#rdsearch").value.trim()), chosen = new Set(S.dest);
  const hits = q.length < 2 ? [] : CATALOG.filter(c => !chosen.has(c.id) && fold(`${c.name} ${c.sub}`).includes(q)).slice(0, 8);
  $("#rdresults").innerHTML = hits.map(c => `<button type="button" data-adddest="${esc(c.id)}"
      class="block w-full cursor-pointer truncate rounded px-1.5 py-1 text-left hover:bg-acc-soft">
      <b class="font-semibold">${esc(c.name)}</b> <span class="text-mut">${esc(c.sub)}</span></button>`).join("")
    || (q.length < 2 ? "" : `<p class="px-1.5 py-1 text-mut">nic nie pasuje</p>`);
}
function renderSettings(){
  const chip = (on, attrs, text) => `<button type="button" ${attrs} class="chip ${on ? "on" : ""} px-2.5 py-0.5 text-[12.5px]">${text}</button>`;
  const dest = destinations();
  $("#rset").innerHTML = `
    <h3 class="f-title">Dla kogo</h3>
    <div class="mb-1 flex flex-wrap gap-1.5">${Object.entries(PROFILES).map(([id, p]) =>
      chip(S.profile === id, `data-profile="${id}"`, esc(p.label))).join("")}</div>
    <p class="mb-3 text-[11.5px] text-mut">Profil zmienia wagi sekcji w ocenie ogólnej, nie ich zawartość.</p>
    <h3 class="f-title">Zasięg spaceru</h3>
    <div class="mb-3 flex flex-wrap gap-1.5">${WALKS.map(m => chip(S.walk === m, `data-walk="${m}"`, `${m} min`)).join("")}</div>
    <h3 class="f-title">Dojazdy do</h3>
    <div class="mb-1.5">${dest.map(p => `<div class="flex items-center gap-1 py-0.5"><span class="min-w-0 flex-1 truncate"
        title="${esc(p.sub || "")}">${esc(p.name)}</span><button type="button" data-undest="${esc(p.id)}" title="Usuń z raportu"
        class="cursor-pointer px-1 text-mut hover:text-bad">✕</button></div>`).join("")
      || `<p class="text-[12.5px] text-mut">Nie wybrano żadnego celu.</p>`}</div>
    <input id="rdsearch" type="search" placeholder="dodaj: galeria, kino, teatr, lotnisko…" class="field">
    <div id="rdresults" class="mb-1.5 text-[12.5px]"></div>
    <div class="mb-3 flex gap-1"><input id="rplace" type="text" maxlength="40" placeholder="własne, np. praca" class="field min-w-0 flex-1">
      <button type="button" id="rpick" class="cursor-pointer rounded-md border border-acc bg-white px-2 text-[12.5px] whitespace-nowrap
        text-acc hover:bg-acc-soft">wskaż na mapie</button></div>
    <h3 class="f-title">Obiekty z mapy</h3>
    <p class="mb-1 text-[11.5px] text-mut">Raport pokaże najbliższy obiekt każdego zaznaczonego rodzaju.</p>
    <div class="mb-3">${KINDS.map(k => `<label class="chk py-0.5 text-ink"><input type="checkbox" data-kind="${esc(k.id)}"
      ${S.kindsOff.includes(k.id) ? "" : "checked"}><img src="${k.icon}" alt="" class="size-4 object-contain">${esc(k.label)}</label>`).join("")}</div>
    <h3 class="f-title">Sekcje raportu</h3>
    <div class="mb-3">${SECTIONS.filter(s => !s.fixed).map(s => `<label class="chk py-0.5 text-ink whitespace-normal">
      <input type="checkbox" data-sec="${s.id}" ${S.off.includes(s.id) ? "" : "checked"}> ${esc(s.title)}</label>`).join("")}</div>
    <button type="button" id="rreset" class="w-full cursor-pointer rounded-lg border border-line bg-white p-1.5 text-[12.5px]
      hover:bg-acc-soft">Przywróć ustawienia domyślne</button>
    <p class="mt-2 text-[11.5px] text-mut">Ustawienia są zapamiętywane w tej przeglądarce i dotyczą wszystkich raportów.</p>`;
}
function settingsChanged(scrollTo){
  saveSettings();
  if(current) render(current.o, scrollTo);
}

/* ---------- okno raportu ---------- */
const ICON = {
  compare: `<svg viewBox="0 0 24 24" class="size-4" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"><rect x="3" y="4" width="7" height="16" rx="1.5"/><rect x="14" y="4" width="7" height="16" rx="1.5"/></svg>`,
  tune: `<svg viewBox="0 0 24 24" class="size-4" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M4 7h10M18 7h2M4 17h4M12 17h8"/><circle cx="16" cy="7" r="2"/><circle cx="10" cy="17" r="2"/></svg>`,
  print: `<svg viewBox="0 0 24 24" class="size-4" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"><path d="M7 8V3h10v5M7 17H4V9h16v8h-3"/><path d="M7 14h10v7H7z"/></svg>`,
};
const hbtn = "flex cursor-pointer items-center gap-1.5 rounded-lg border border-line bg-white px-2.5 py-1.5 text-[13px] font-semibold hover:bg-acc-soft on:border-acc on:bg-acc on:text-white";
document.body.insertAdjacentHTML("beforeend", `<section id="report" hidden class="fixed inset-0 z-1300 flex-col bg-page">
  <header class="flex flex-wrap items-center gap-x-3 gap-y-1.5 border-b border-line bg-white px-3.5 py-2">
    <div class="min-w-56 flex-1"><div class="text-[11px] tracking-wider text-mut uppercase">Raport o mieszkaniu i okolicy</div>
      <h1 id="rtitle" class="truncate text-base font-bold"></h1><div id="rsub" class="truncate text-xs text-mut"></div></div>
    <button type="button" id="rsimilar" class="noprint ${hbtn}" title="Zestaw tę ofertę z podobnymi z okolicy">${ICON.compare} Porównaj z podobnymi</button>
    <button type="button" id="rtune" class="noprint ${hbtn}" aria-expanded="false">${ICON.tune} Dostosuj</button>
    <button type="button" id="rprint" class="noprint ${hbtn}">${ICON.print} Drukuj / PDF</button>
    <button type="button" id="rclose" title="Zamknij raport" class="noprint ${hbtn}">✕ Zamknij</button>
  </header>
  <div id="rmain">
    <div id="rbody"><div id="rsections" class="mx-auto max-w-[900px] px-3 pt-3 text-sm"></div></div>
    <div id="rmappane">
      <div id="r-layers" class="noprint flex flex-wrap gap-1 border-b border-line px-2 py-1.5"></div>
      <div id="r-pick" hidden class="bg-acc px-3 py-1.5 text-[13px] font-semibold text-white"></div>
      <div id="r-map"></div>
      <button type="button" id="rhome" class="noprint absolute right-2.5 bottom-6 z-1000 cursor-pointer rounded-lg bg-white px-2.5 py-1.5
        text-[12.5px] font-semibold shadow-[0_1px_5px_rgba(0,0,0,.3)] hover:bg-acc-soft">⌂ wróć do mieszkania</button>
    </div>
    <aside id="rset" hidden class="noprint"></aside>
  </div></section>`);
const box = $("#report");
// która nakładka jest na wierzchu — po tym druk wie, co ma trafić na papier (style.css)
function syncOverlay(){
  const top = !$("#compare").hidden ? "compare" : !box.hidden ? "report" : null;
  top ? document.body.dataset.overlay = top : delete document.body.dataset.overlay;
}
let reportBack = null;  // co zrobić po zamknięciu raportu otwartego z porównania
function openReport(id, back = null){
  const o = OFFERS.find(x => x.id === id);
  if(!o) return;
  picking = null;
  reportBack = back;
  box.hidden = false;
  box.classList.add("flex");
  syncOverlay();
  render(o);
}
function closeReport(){
  if(box.hidden) return;
  cleanup();
  current = picking = null;
  box.hidden = true;
  box.classList.remove("flex");
  $("#rsections").innerHTML = "";
  const back = reportBack;
  reportBack = null;
  back?.();
  syncOverlay();
}
function toggleSettings(open){
  $("#rset").hidden = !open;
  $("#rtune").classList.toggle("on", open);
  $("#rtune").setAttribute("aria-expanded", open);
}
$("#rclose").onclick = closeReport;
$("#rsimilar").onclick = () => current && compareSimilar(current);
$("#rtune").onclick = () => toggleSettings($("#rset").hidden);
// drukuje się to, co widać: sekcje rozwinięte na ekranie są rozwinięte także na papierze
$("#rprint").onclick = () => print();
$("#rhome").onclick = () => { hilite?.clearLayers(); current?.home?.(); };
// Esc zamyka to, co na wierzchu: porównanie, raport, a dopiero potem panel oferty pod spodem
addEventListener("keydown", e => {
  const compare = !$("#compare").hidden;
  if(e.key !== "Escape" || (box.hidden && !compare)) return;
  e.stopImmediatePropagation();
  compare ? closeCompare() : closeReport();
}, true);
$("#rsections").addEventListener("click", e => {
  const go = e.target.closest("[data-go]"), head = e.target.closest("[data-toggle]"), row = e.target.closest("[data-f]");
  if(go){
    setOpen(go.dataset.go, true);
    $(`#rs-${go.dataset.go}`)?.scrollIntoView({behavior: "smooth", block: "start"});
  } else if(head) setOpen(head.dataset.toggle, !$(`#rs-${head.dataset.toggle}`).classList.contains("open"));
  else if(e.target.id === "rall"){
    const open = !cards().every(c => c.classList.contains("open"));
    cards().forEach(c => setOpen(c.id.slice(3), open));
  } else if(row && current) focusOn(current, current.focus[+row.dataset.f], true);
});
// najechanie na wiersz: trasa piesza do miejsca albo przebieg przejazdu na mapie obok
let hovered = null;
$("#rsections").addEventListener("mouseover", e => {
  const el = e.target.closest("[data-f], [data-trip]");
  if(!el || el === hovered || !current) return;
  hovered = el;
  if(el.dataset.trip != null) showTrip(current, current.trips[+el.dataset.trip]);
  else focusOn(current, current.focus[+el.dataset.f], false);
});
$("#rset").addEventListener("click", e => {
  const t = e.target.closest("button");
  if(!t) return;
  if(t.dataset.profile){ S.profile = t.dataset.profile; settingsChanged(); }
  else if(t.dataset.walk){ S.walk = +t.dataset.walk; settingsChanged(); }
  else if(t.dataset.adddest){ S.dest = [...S.dest, t.dataset.adddest]; S.open = [...new Set([...S.open, "commute"])]; settingsChanged("commute"); }
  else if(t.dataset.undest){
    const id = t.dataset.undest;
    if(id.startsWith("own:")) S.places.splice(+id.slice(4), 1); else S.dest = S.dest.filter(x => x !== id);
    settingsChanged("commute");
  }
  else if(t.id === "rreset"){ S = structuredClone(DEFAULTS); settingsChanged(); }
  else if(t.id === "rpick"){
    const name = $("#rplace").value.trim();
    if(!name){ $("#rplace").focus(); return; }
    if(!current?.located) return;
    picking = name;
    $("#rplace").value = "";
    showPicking();
  }
});
$("#rset").addEventListener("input", e => { if(e.target.id === "rdsearch") destResults(); });
$("#rset").addEventListener("change", e => {
  const {sec, kind} = e.target.dataset;
  if(sec) S.off = e.target.checked ? S.off.filter(id => id !== sec) : [...S.off, sec];
  else if(kind){
    S.kindsOff = e.target.checked ? S.kindsOff.filter(id => id !== kind) : [...S.kindsOff, kind];
    S.open = [...new Set([...S.open, "spots"])];
  } else return;
  settingsChanged(kind ? "spots" : undefined);
});
/* ---------- porównanie ofert ---------- */
/* Zestawienie 2–4 ofert w kolumnach: te same wyliczenia co w raporcie (build), ułożone
   w wiersze. W każdym wierszu, w którym da się powiedzieć „lepiej”, najlepsza wartość jest
   wyróżniona, a nagłówek kolumny mówi, w ilu wierszach oferta wygrywa. Otwierane z listy
   na mapie (app.js, startCompare) albo z raportu — dla oferty i wybranych podobnych.
   Dojazdy i hałas dochodzą po chwili (trasy linii, mapa akustyczna) i tabela się odświeża. */
const CMP_COLORS = ["#2456c7", "#eb6834", "#178a4c", "#7c3aed"], CMP_LETTERS = "ABCD";
// kategorie z OpenStreetMap pokazywane w wierszach „pieszo do najbliższego”
const CMP_CATS = ["supermarket", "convenience", "pharmacy", "clinic", "kindergarten", "school", "playground", "green", "restaurant"];
const CMP_FEATURES = ["balkon", "winda", "piwnica", "garaż", "ogródek", "do remontu"];
let cmp = null;  // otwarte porównanie: {ids, base, list, cs: konteksty ofert, map}
function loanPay(price){
  const {down, rate, years} = S.loan, loan = price * (1 - down / 100), r = rate / 1200, n = years * 12;
  return !loan ? 0 : r ? loan * r / (1 - (1 + r) ** -n) : loan / n;
}
// poziom hałasu z odpowiedzi mapy akustycznej; null, gdy punkt jest poza jej zasięgiem
function noiseSummary(o, rows){
  const loud = Math.max(0, ...rows.map(r => r.day?.[0] ?? 0));
  if(!loud && !inRing(o.lat, o.lon, KRAKOW_BOUNDARY)) return null;
  return {loud, word: NOISE_WORD.find(([lim]) => loud < lim)[1].split(" —")[0],
    db: bandText(rows.find(r => (r.day?.[0] ?? 0) === loud)?.day ?? null), score: NOISE_SCORE.find(([lim]) => loud < lim)[1]};
}
// wiersz liczbowy: get → wartość (null = brak), fmt → napis, better: "low" | "high" | brak (bez wyróżniania),
// sub → dopisek pod wartością, wait → czy wartość jeszcze się liczy
const cmpNum = (label, get, fmt, better, sub, wait) => ({label, better, cell: c => {
  const v = get(c), ok = v != null && isFinite(v);
  return {v: ok ? v : null, html: ok ? esc(fmt(v, c)) : wait?.(c) ? "…" : "—", sub: ok ? sub?.(c) : null};
}});
const cmpText = (label, get) => ({label, cell: c => { const t = get(c); return {v: null, html: t == null || t === "" ? "—" : esc(t)}; }});
const scoreCell = v => `<span class="inline-flex items-center gap-2"><b class="w-7 tabular-nums">${fmt1(v)}</b>
  <span class="h-1.5 w-20 overflow-hidden rounded-full bg-page"><i class="block h-full rounded-full" style="width:${v * 10}%;background:${scoreBar(v)}"></i></span></span>`;
function cmpRows(){
  const rows = [{group: "Oceny (0–10)"},
    {label: "Ocena ogólna", better: "high", cell: c => { const v = overall(c); return {v, html: v == null ? "—"
      : `<b class="text-[19px]" style="color:${scoreBar(v)}">${fmt1(v)}</b>`}; }}];
  for(const s of SECTIONS.filter(s => s.scored && !S.off.includes(s.id)))
    rows.push({label: `${s.icon} ${s.short || s.title}`, better: "high", cell: c => {
      const v = c.scores[s.id];
      return {v: v ?? null, html: v != null ? scoreCell(v) : c.waiting.has(s.id) ? "…" : "—"};
    }});
  rows.push({group: "Cena i mieszkanie"},
    cmpNum("Cena", c => c.o.p, fmtP, "low"),
    cmpNum("Cena za m²", c => c.o.pm, v => fmtN(v) + " zł", "low"),
    cmpNum("Na tle podobnych ofert w okolicy", c => c.price?.diff, pct, "low"),
    cmpNum("Metraż", c => c.o.a, v => String(v).replace(".", ",") + " m²", "high"),
    cmpText("Pokoje", c => c.o.r), cmpText("Piętro", c => c.o.f), cmpText("Rok budowy", c => c.o.by),
    cmpText("Rynek", c => c.o.mk), cmpText("Dzielnica", c => c.o.d),
    cmpNum(`Rata kredytu (${S.loan.down}% wkładu, ${fmt1(S.loan.rate)}%, ${S.loan.years} lat)`, c => c.o.p && loanPay(c.o.p), fmtP, "low"),
    cmpNum("Na rynku od", c => c.o.c || c.o.fs ? Math.floor((GEN - new Date(c.o.c || c.o.fs)) / 864e5) : null,
      v => `${v} ${plural(v, "dnia", "dni", "dni")}`),
    cmpText("Zmiany ceny", c => !c.o.h ? "bez zmian" : `${c.o.h.length - 1} ${plural(c.o.h.length - 1, "zmiana", "zmiany", "zmian")}` +
      (c.o._drop > 0 ? `, obniżka ${(c.o._drop * 100).toFixed(1).replace(".", ",")}%` : "")));
  const dests = destinations();
  if(dests.length && !S.off.includes("commute")){
    rows.push({group: "Dojazdy od drzwi do drzwi"});
    dests.forEach((p, i) => rows.push(cmpNum(p.name, c => c.commute?.[i]?.min, v => `${v} min`, "low", c => {
      const t = c.commute[i].trip;
      return !t || c.commute[i].min < t.min ? "pieszo" : t.legs.filter(l => l.v).map(l => l.v.line).join(" → ") +
        (t.rides > 1 ? ` · ${t.rides - 1} ${plural(t.rides - 1, "przesiadka", "przesiadki", "przesiadek")}` : " · bez przesiadki");
    }, c => c.located && !c.commute)));
  }
  rows.push({group: "Pieszo do najbliższego"});
  for(const t of TRANSIT) rows.push(cmpNum(t.label, c => c.transit.find(x => x.t === t)?.nearest?.d, (v, c) =>
    walkText(c.transit.find(x => x.t === t).nearest), "low", c => c.transit.find(x => x.t === t).nearest.name));
  for(const id of CMP_CATS.filter(id => CATS[id])){
    const first = c => c.amen.find(a => a.cat.id === id)?.list[0];
    rows.push(cmpNum(CATS[id].label, c => first(c)?.d, (v, c) => walkText(first(c)), "low", c => first(c).name || "bez nazwy"));
  }
  if(!S.off.includes("spots")){
    const kinds = KINDS.filter(k => !S.kindsOff.includes(k.id) && k.pts.length);
    if(kinds.length) rows.push({group: "Obiekty z mapy (pieszo)"});
    for(const k of kinds){
      const first = c => c.spots?.find(s => s.kind === k)?.list[0];
      rows.push(cmpNum(k.label, c => first(c)?.d, (v, c) => walkText(first(c)), "low", c => first(c).name));
    }
  }
  rows.push({group: "Hałas"}, cmpNum("W ciągu doby (mapa akustyczna)", c => c.noise?.loud, (v, c) => c.noise.db, "low",
    c => c.noise.word, c => c.waiting.has("noise")));
  rows.push({group: "Z opisu ogłoszenia"});
  for(const name of CMP_FEATURES) rows.push({label: name[0].toUpperCase() + name.slice(1), cell: c => {
    const f = c.features.find(x => x.label.startsWith(name));
    return {v: null, html: f.state === "yes" ? `<span class="font-semibold text-good">✓ jest w opisie</span>`
      : f.state === "no" ? `<span class="font-semibold text-bad">✗ wspomniano brak</span>` : `<span class="text-mut">nie wspomniano</span>`};
  }});
  const points = (list, mark, cls) => ({v: null, html: [...list].sort((a, b) => b.w - a.w).slice(0, 3).map(x =>
    `<div class="flex gap-1.5"><span class="${cls} font-bold">${mark}</span><span>${esc(x.text)}</span></div>`).join("") || "—"});
  rows.push({group: "Wnioski"}, {label: "Mocne strony", cell: c => points(c.plus, "+", "text-good")},
    {label: "Na co uważać", cell: c => points(c.minus, "−", "text-bad")});
  return rows;
}
function drawCompare(){
  const cs = cmp.cs, rows = cmpRows();
  const wins = cs.map(() => 0);
  let judged = 0;
  const body = rows.map(row => {
    if(row.group) return `<tr><th colspan="${cs.length + 1}" class="cgroup">${esc(row.group)}</th></tr>`;
    const cells = cs.map(c => row.cell(c));
    // najlepsza wartość w wierszu (przy remisie każda z najlepszych) — tylko gdy jest co porównać
    const values = cells.map(x => x.v).filter(v => v != null);
    const best = row.better && values.length > 1 && new Set(values).size > 1
      ? (row.better === "low" ? Math.min(...values) : Math.max(...values)) : null;
    if(best != null){ judged++; cells.forEach((x, i) => x.v === best && wins[i]++); }
    return `<tr><th class="clabel">${esc(row.label)}</th>${cells.map(x => `<td class="ccell ${x.v != null && x.v === best ? "cbest" : ""}">
      <div class="cval">${x.html}</div>${x.sub ? `<div class="truncate text-[11.5px] text-mut" title="${esc(x.sub)}">${esc(x.sub)}</div>` : ""}</td>`).join("")}</tr>`;
  }).join("");
  const totals = cs.map(c => overall(c)), top = Math.max(...totals.filter(v => v != null), -1);
  const letter = i => `<span class="grid size-6 shrink-0 place-items-center rounded-full text-[12.5px] font-bold text-white"
    style="background:${CMP_COLORS[i]}">${CMP_LETTERS[i]}</span>`;
  const head = cs.map((c, i) => {
    const {o} = c;
    return `<th class="ccell align-top font-normal">
      <div class="mb-1.5 flex items-center gap-1.5">${letter(i)}
        ${o.id === cmp.base ? `<span class="rounded bg-ink px-1.5 py-px text-[11px] font-semibold text-white">ta oferta</span>` : ""}
        ${totals[i] != null && totals[i] === top && cs.length > 1 ? `<span class="rounded bg-[#e3f4ea] px-1.5 py-px text-[11px] font-semibold text-good">najwyższa ocena</span>` : ""}
        <button type="button" data-cdrop="${esc(o.id)}" title="Usuń z porównania" class="noprint ml-auto cursor-pointer px-1 text-mut hover:text-bad">✕</button></div>
      ${o.ph.length ? `<img src="${esc(o.ph[0])}" alt="" class="mb-1.5 h-28 w-full rounded-lg bg-[#e8ebf0] object-cover" onerror="this.remove()">` : ""}
      <div class="line-clamp-2 text-[13px]/[1.3] font-semibold">${esc(o.t)}</div>
      <div class="text-[11.5px] text-mut">${[o.d, copies(o).map(portalName).join(" + ")].filter(Boolean).map(esc).join(" · ")}</div>
      <div class="mt-1 text-[11.5px] text-mut">${judged ? `najlepsza w <b class="text-ink">${wins[i]}</b> z ${judged} porównań` : ""}</div>
      <div class="noprint mt-1.5 flex gap-1.5 text-[12.5px]">
        <button type="button" data-creport="${esc(o.id)}" class="cursor-pointer rounded-md border border-acc bg-acc-soft px-2 py-1 font-semibold text-acc
          hover:bg-acc hover:text-white">Raport</button>
        <a href="${esc(o.u)}" target="_blank" rel="noopener" class="rounded-md border border-line bg-white px-2 py-1 hover:bg-acc-soft">Ogłoszenie ↗</a></div></th>`;
  }).join("");
  // drugi wiersz nagłówka zostaje przy przewijaniu: litera, cena i skrócony tytuł
  const sticky = cs.map((c, i) => `<th class="cstick"><span class="flex items-center gap-1.5">${letter(i)}
    <b class="whitespace-nowrap">${fmtP(c.o.p)}</b><span class="truncate font-normal text-mut">${esc(c.o.t)}</span></span></th>`).join("");
  $("#ctable").innerHTML = `<thead><tr><th class="clabel align-bottom font-normal"><span class="text-[11.5px] text-mut">Zielone tło — najlepsza
      wartość w wierszu.</span></th>${head}</tr><tr><th class="cstick clabel"></th>${sticky}</tr></thead><tbody>${body}</tbody>`;
  $("#csub").textContent = `${cs.length} ${plural(cs.length, "oferta", "oferty", "ofert")} · profil ocen: ${PROFILES[S.profile].label}
    · zasięg spaceru ${S.walk} min · cele dojazdów i resztę ustawień zmienisz w raporcie, w panelu „Dostosuj”`;
}
function mountCompareMap(){
  cmp.map?.remove();
  cmp.map = null;
  const placed = cmp.cs.map((c, i) => [c, i]).filter(([c]) => c.located);
  $("#cmap").hidden = !placed.length;
  if(!placed.length) return;
  const map = cmp.map = L.map("cmap", {scrollWheelZoom: false});
  L.tileLayer(carto("voyager"), CARTO_OPTS).addTo(map);
  for(const [c, i] of placed) L.marker([c.o.lat, c.o.lon], {icon: L.divIcon({className: "", iconSize: [26, 26], iconAnchor: [13, 13],
    html: `<span class="grid size-[26px] place-items-center rounded-full border-2 border-white text-[13px] font-bold text-white
      shadow-[0_1px_4px_rgba(0,0,0,.45)]" style="background:${CMP_COLORS[i]}">${CMP_LETTERS[i]}</span>`})})
    .bindTooltip(`${esc(c.o.t)}<br><b>${fmtP(c.o.p)}</b>`, {direction: "top", offset: [0, -10]}).addTo(map);
  for(const p of destinations()) L.circleMarker([p.lat, p.lon], {radius: 6, color: CH.ink, weight: 2, fillColor: "#fff", fillOpacity: 1})
    .bindTooltip(esc(p.name), {direction: "top"}).addTo(map);
  // widok obejmuje oferty (cele dojazdów są na mapie, ale nie rozciągają kadru); rozmiar mapy
  // ustala się chwilę po wstawieniu do strony, więc kadr ustawiamy, gdy już go znamy
  const frame = L.latLngBounds(placed.map(([c]) => [c.o.lat, c.o.lon])).pad(.3);
  map.setView(frame.getCenter(), 12);
  cmp.watch?.disconnect();
  let fitted = false;
  cmp.watch = new ResizeObserver(() => {
    if(cmp?.map !== map) return;
    map.invalidateSize();
    if(!fitted && $("#cmap").clientWidth > 50){ fitted = true; map.fitBounds(frame, {maxZoom: 15}); }
  });
  cmp.watch.observe($("#cmap"));
}
function renderCompare(){
  const mine = cmp;
  cmp.cs = cmp.ids.map(id => OFFERS.find(o => o.id === id)).filter(Boolean).map(o => build(o).ctx);
  drawCompare();
  mountCompareMap();
  $("#cbody").scrollTop = 0;
  const fresh = () => cmp === mine && !cbox.hidden;
  loadRoutes().catch(() => null).then(routes => {
    if(cmp !== mine) return;
    for(const c of mine.cs) c.commute = c.located ? commuteList(c, routes && journeys(c, network(routes))) : [];
    if(fresh()) drawCompare();
  });
  for(const c of mine.cs){
    if(!c.waiting.has("noise")) continue;
    noiseAt(c.o).then(rows => {
      c.noise = noiseSummary(c.o, rows);
      if(c.noise){
        c.scores.noise = c.noise.score;
        if(c.noise.loud >= 65) c.minus.push({w: 3, text: `Hałas ${c.noise.db} w ciągu doby (${c.noise.word})`});
        else if(c.noise.loud < 55) c.plus.push({w: 2.5, text: "Cicha lokalizacja: poniżej 55 dB według mapy akustycznej"});
      }
    }).catch(() => { /* bez sieci wiersz hałasu zostaje pusty */ }).finally(() => {
      c.waiting.delete("noise");
      if(fresh()) drawCompare();
    });
  }
}
document.body.insertAdjacentHTML("beforeend", `<section id="compare" hidden class="fixed inset-0 z-1300 flex-col bg-page">
  <header class="flex flex-wrap items-center gap-x-3 gap-y-1.5 border-b border-line bg-white px-3.5 py-2">
    <div class="min-w-56 flex-1"><h1 class="text-base font-bold">Porównanie ofert</h1>
      <div id="csub" class="text-xs text-mut"></div></div>
    <button type="button" id="cprint" class="noprint ${hbtn}">${ICON.print} Drukuj / PDF</button>
    <button type="button" id="cclose" title="Zamknij porównanie" class="noprint ${hbtn}">✕ Zamknij</button>
  </header>
  <div id="cbody"><div id="cmap"></div><table id="ctable"></table></div></section>`);
const cbox = $("#compare");
// ids: oferty w kolejności kolumn; base: oferta, z której raportu przyszło porównanie;
// list: to porównanie listy z mapy, więc usunięcie kolumny usuwa ofertę także z niej
function openCompare(ids, {base = null, list = false} = {}){
  cmp = {ids: ids.slice(0, CMP_LETTERS.length), base, list};
  cbox.hidden = false;
  cbox.classList.add("flex");
  syncOverlay();
  renderCompare();
}
function closeCompare(){
  if(!cmp) return;
  cmp.watch?.disconnect();
  cmp.map?.remove();
  cmp = null;
  cbox.hidden = true;
  cbox.classList.remove("flex");
  $("#ctable").innerHTML = "";
  syncOverlay();
}
$("#cclose").onclick = closeCompare;
$("#cprint").onclick = () => print();
cbox.addEventListener("click", e => {
  const drop = e.target.closest("[data-cdrop]"), report = e.target.closest("[data-creport]");
  if(drop){
    const id = drop.dataset.cdrop;
    cmp.ids = cmp.ids.filter(x => x !== id);
    if(cmp.list && compareIds.includes(id)) toggleCompare(id);
    cmp.ids.length ? renderCompare() : closeCompare();
  } else if(report){
    // raport otwiera się zamiast porównania; po jego zamknięciu porównanie wraca tam, gdzie było
    cbox.hidden = true;
    openReport(report.dataset.creport, () => {
      if(!cmp) return;
      cbox.hidden = false;
      cmp.map?.invalidateSize();
    });
  }
});
// porównanie tej oferty z zaznaczonymi podobnymi (sekcja „Podobne oferty”); gdy sekcja jest wyłączona — z trzema pierwszymi
function compareSimilar(ctx){
  const picks = ctx.simPick || similarRows(ctx).slice(0, 3).map(r => r.x.id);
  if(!picks.length) return toast(ctx.simPick ? "Zaznacz w sekcji „Podobne oferty” co najmniej jedną ofertę." : "W okolicy nie ma podobnych ofert do porównania.");
  openCompare([ctx.o.id, ...picks], {base: ctx.o.id});
}
window.openCompare = openCompare;
window.openReport = openReport;
})();
