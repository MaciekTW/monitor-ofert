"use strict";
const META = JSON.parse(document.getElementById("meta").textContent);
const OFFERS = JSON.parse(document.getElementById("data").textContent);
const GEN = new Date(META.gen);
const PORTAL = {olx: "OLX", otodom: "Otodom"};
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
   styl Voyager) nie mają tego wymogu i działają z pliku lokalnego. */
L.tileLayer("https://{s}.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}{r}.png",
  {maxZoom:20, subdomains:"abcd",
   attribution:'&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>'
              +' &middot; &copy; <a href="https://carto.com/attributions">CARTO</a>'}
).addTo(map);
// skala kolorów: kwintyle ceny za m² liczone raz, z całego zbioru
const PAL = ["#1a9850","#8fce54","#f5c53c","#f2803a","#d73027"];
const ppmAll = OFFERS.map(o=>o.pm).filter(v=>v!=null).sort((a,b)=>a-b);
const BR = [1,2,3,4].map(i => ppmAll[Math.floor(ppmAll.length*i/5)] || 0);
const colorOf = v => v == null ? "#8a93a3"
  : PAL[BR.findIndex(b => v <= b) === -1 ? 4 : BR.findIndex(b => v <= b)];
const legend = L.control({position:"bottomleft"});
legend.onAdd = () => {
  const div = L.DomUtil.create("div",
    "rounded-lg bg-white px-2.5 py-2 text-[11.5px]/[1.55] shadow-[0_1px_5px_rgba(0,0,0,.25)]");
  div.innerHTML = "<b>cena za m²</b><br>" + PAL.map((c,i)=>{
    const lo = i ? fmtN(BR[i-1]) : null, hi = BR[i] ? fmtN(BR[i]) : null;
    const lbl = i === 0 ? "do " + hi : i === 4 ? "od " + lo : lo + "–" + hi;
    return `<i class="mr-1.5 inline-block size-[11px] rounded-full align-[-1px]"
      style="background:${c}"></i>${lbl}`;
  }).join("<br>");
  return div;
};
legend.addTo(map);
// granice Krakowa: sam obwód, nieklikalny, żeby nie zasłaniał markerów
const boundary = L.polygon(KRAKOW_BOUNDARY,
  {color:"#2563eb", weight:2.5, opacity:.85, fill:false, interactive:false}).addTo(map);
const boundaryToggle = L.control({position:"topright"});
boundaryToggle.onAdd = () => {
  const div = L.DomUtil.create("label",
    "chk m-0! rounded-lg bg-white px-2.5 py-1.5 text-[12px] shadow-[0_1px_5px_rgba(0,0,0,.25)]");
  div.innerHTML = '<input type="checkbox" checked> granice Krakowa';
  L.DomEvent.disableClickPropagation(div);
  div.querySelector("input").addEventListener("change", e =>
    e.target.checked ? boundary.addTo(map) : boundary.remove());
  return div;
};
boundaryToggle.addTo(map);
const layer = L.layerGroup().addTo(map);
const markers = new Map();
let selId = null;
function baseStyle(o){
  return {radius:6, weight:1, color:"#fff", fillColor:colorOf(o.pm),
          fillOpacity:.88};
}
function rebuildMarkers(list){
  layer.clearLayers(); markers.clear();
  for(const o of list){
    if(o.lat == null) continue;
    const m = L.circleMarker([o.lat, o.lon], baseStyle(o))
      .on("click", () => openDetail(o.id, true))
      .bindTooltip(`${esc(o.t)}<br><b>${fmtP(o.p)}</b>` +
        (o.pm ? ` · ${fmtN(o.pm)} zł/m²` : ""), {direction:"top", opacity:.94});
    m.addTo(layer);
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
function currentFilter(){
  const roomsOn = [...document.querySelectorAll("#rooms .chip.on")]
    .map(b => +b.dataset.r);
  const boxes = [...document.querySelectorAll(".dbox")];
  const dsel = new Set(boxes.filter(b => b.checked).map(b => b.value));
  const allD = dsel.size === boxes.length;
  const srcOn = [...document.querySelectorAll("#portals .chip.on")]
    .map(b => b.dataset.s);
  const q = $("#q").value.trim().toLowerCase();
  const inDesc = $("#qdesc").checked;
  const freshDays = $("#fresh").value ? +$("#fresh").value : null;
  const bounds = $("#bounds").checked ? map.getBounds() : null;
  return o => {
    if(srcOn.length < 2 && !srcOn.includes(o.s)) return false;
    if(q && !(o._txt.includes(q) || (inDesc && o._dtxt.includes(q)))) return false;
    const pmin=num("#pmin"), pmax=num("#pmax");
    if(pmin != null && (o.p == null || o.p < pmin)) return false;
    if(pmax != null && (o.p == null || o.p > pmax)) return false;
    const amin=num("#amin"), amax=num("#amax");
    if(amin != null && (o.a == null || o.a < amin)) return false;
    if(amax != null && (o.a == null || o.a > amax)) return false;
    const mmin=num("#mmin"), mmax=num("#mmax");
    if(mmin != null && (o.pm == null || o.pm < mmin)) return false;
    if(mmax != null && (o.pm == null || o.pm > mmax)) return false;
    if(roomsOn.length && (o._rb == null || !roomsOn.includes(o._rb))) return false;
    if($("#market").value && o.mk !== $("#market").value) return false;
    if($("#seller").value !== "" && String(o.b) !== $("#seller").value) return false;
    if(freshDays != null){
      const t = o.c || o.fs;
      if(!t || (GEN - new Date(t)) > freshDays*864e5) return false;
    }
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
    ["Rynek", o.mk], ["Dzielnica", o.d],
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
      <div id="pchart" class="relative cursor-pointer select-none" title="Kliknij, żeby pokazać wszystkie zmiany"></div>
      <table id="ptable" hidden class="mt-2 w-full text-[13px]">` + o.h.map((x,i) => {
      const prev = i ? o.h[i-1][1] : null;
      const diff = prev == null ? "" :
        `<span class="${x[1] > prev ? "text-bad" : "text-good"}">
          ${x[1] > prev ? "▲" : "▼"} ${fmtN(Math.abs(x[1]-prev))}</span> `;
      return `<tr><td class="${td}">${esc(x[0])}</td>
        <td class="${td} text-right">${diff}${fmtP(x[1])}</td></tr>`;
    }).join("") + `</table></div>`;
  }
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
function renderPriceChart(el, h){
  const W = el.clientWidth, H = 110, pad = {l:4, r:4, t:18, b:18};
  const day = s => Date.parse(s + "T00:00:00Z") / 864e5;
  const xs = h.map(x => day(x[0])), t0 = xs[0];
  const t1 = Math.max(day(META.gen.slice(0,10)), xs[xs.length-1] + 1);
  const ps = h.map(x => x[1]);
  let lo = Math.min(...ps), hi = Math.max(...ps);
  // minimalna rozpiętość osi 3% ceny — zmiana o 1 zł nie może wyglądać jak przepaść
  if(hi - lo < hi * .03){ const m = (hi + lo) / 2, s = hi * .015; lo = m - s; hi = m + s; }
  const X = t => pad.l + (t - t0) / (t1 - t0) * (W - pad.l - pad.r);
  const Y = p => pad.t + (hi - p) / (hi - lo) * (H - pad.t - pad.b);
  const fmtK = v => v >= 1e6 ? (v/1e6).toFixed(2).replace(".", ",") + " mln" : Math.round(v/1000) + " tys.";
  const fmtD = s => s.slice(8,10) + "." + s.slice(5,7);

  const first = ps[0], last = ps[ps.length-1];
  const col = last < first ? "var(--color-good)" : last > first ? "var(--color-bad)" : "var(--color-mut)";
  const line = h.map((x,i) => i ? `H${X(xs[i])}V${Y(x[1])}` : `M${X(xs[0])},${Y(x[1])}`).join("") + `H${X(t1)}`;
  const dots = h.map((x,i) => {
    const c = !i ? "var(--color-mut)" : x[1] < ps[i-1] ? "var(--color-good)" : "var(--color-bad)";
    return `<circle cx="${X(xs[i])}" cy="${Y(x[1])}" r="3.2" fill="#fff" stroke="${c}" stroke-width="2"/>`;
  }).join("");
  el.innerHTML = `<svg width="${W}" height="${H}" class="block overflow-visible">
    <defs><linearGradient id="pgrad" x1="0" x2="0" y1="0" y2="1">
      <stop offset="0" stop-color="${col}" stop-opacity=".18"/>
      <stop offset="1" stop-color="${col}" stop-opacity="0"/></linearGradient></defs>
    <line x1="${pad.l}" x2="${W-pad.r}" y1="${H-pad.b}" y2="${H-pad.b}" stroke="var(--color-line)"/>
    <path d="${line}V${H-pad.b}H${X(t0)}Z" fill="url(#pgrad)"/>
    <path d="${line}" fill="none" stroke="${col}" stroke-width="2" stroke-linejoin="round"/>
    ${dots}
    <text x="${X(t0)}" y="${Y(first) - 7}" font-size="11" fill="var(--color-mut)">${fmtK(first)}</text>
    <text x="${W-pad.r}" y="${Y(last) - 7}" font-size="11" font-weight="700" fill="var(--color-ink)"
      text-anchor="end">${fmtK(last)}</text>
    <text x="${pad.l}" y="${H-3}" font-size="10.5" fill="var(--color-mut)">${fmtD(h[0][0])}</text>
    <text x="${W-pad.r}" y="${H-3}" font-size="10.5" fill="var(--color-mut)" text-anchor="end">
      teraz · pokaż zmiany (${h.length}) ▾</text>
    <line id="pxh" y1="${pad.t-6}" y2="${H-pad.b}" stroke="var(--color-mut)" stroke-dasharray="2 2" visibility="hidden"/>
  </svg>
  <div id="ptip" hidden class="pointer-events-none absolute -translate-x-1/2 -translate-y-[120%] rounded-md
    bg-ink px-1.5 py-0.5 text-xs whitespace-nowrap text-white"></div>`;

  const xh = $("#pxh"), tip = $("#ptip"), label = el.querySelector("text:last-of-type");
  el.onmousemove = e => {
    const sx = e.clientX - el.getBoundingClientRect().left;
    const t = Math.min(t1, Math.max(t0, t0 + (sx - pad.l) / (W - pad.l - pad.r) * (t1 - t0)));
    let k = 0;
    xs.forEach((x,i) => { if(x <= t) k = i; });
    xh.setAttribute("x1", X(t)); xh.setAttribute("x2", X(t));
    xh.setAttribute("visibility", "visible");
    tip.textContent = `${fmtD(new Date(t * 864e5).toISOString())} · ${fmtP(ps[k])}`;
    tip.style.left = X(t) + "px";
    tip.style.top = Y(ps[k]) + "px";
    tip.hidden = false;
  };
  el.onmouseleave = () => { xh.setAttribute("visibility", "hidden"); tip.hidden = true; };
  el.onclick = () => {
    const tb = $("#ptable");
    tb.hidden = !tb.hidden;
    label.textContent = `teraz · ${tb.hidden ? "pokaż" : "ukryj"} zmiany (${h.length}) ${tb.hidden ? "▾" : "▴"}`;
  };
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
["#q","#pmin","#pmax","#amin","#amax","#mmin","#mmax"]
  .forEach(s => $(s).addEventListener("input", soon));
["#qdesc","#market","#seller","#fresh","#sort","#onlydrop","#onlygeo"]
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
  ["#market","#seller","#fresh"].forEach(s => $(s).value = "");
  ["#onlydrop","#onlygeo","#bounds"].forEach(s => $(s).checked = false);
  $("#qdesc").checked = true;
  document.querySelectorAll("#rooms .chip").forEach(b => b.classList.remove("on"));
  document.querySelectorAll("#portals .chip").forEach(b => b.classList.add("on"));
  document.querySelectorAll(".dbox").forEach(b => b.checked = true);
  apply();
};
addEventListener("resize", renderHisto);
apply();
