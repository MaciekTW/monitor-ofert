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
  const div = L.DomUtil.create("div","legend");
  div.innerHTML = "<b>cena za m²</b><br>" + PAL.map((c,i)=>{
    const lo = i ? fmtN(BR[i-1]) : null, hi = BR[i] ? fmtN(BR[i]) : null;
    const lbl = i === 0 ? "do " + hi : i === 4 ? "od " + lo : lo + "–" + hi;
    return `<i style="background:${c}"></i>${lbl}`;
  }).join("<br>");
  return div;
};
legend.addTo(map);
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
  .map(([d,n]) => `<label><input type="checkbox" class="dbox" value="${esc(d)}"
     checked> ${esc(d)} <span class="cnt">${n}</span></label>`).join("");
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
  "pm-asc": (a,b) => (a.pm ?? 9e9) - (b.pm ?? 9e9),
  "pm-desc": (a,b) => (b.pm ?? -1) - (a.pm ?? -1),
  "p-asc": (a,b) => (a.p ?? 9e9) - (b.p ?? 9e9),
  "p-desc": (a,b) => (b.p ?? -1) - (a.p ?? -1),
  "a-desc": (a,b) => (b.a ?? -1) - (a.a ?? -1),
  "a-asc": (a,b) => (a.a ?? 9e9) - (b.a ?? 9e9),
  "drop": (a,b) => b._drop - a._drop,
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
  $("#stats").innerHTML =
    `<div><b>${filtered.length}</b><span>ofert po filtrach</span></div>` +
    `<div><b>${fmtN(median(ms))}</b><span>mediana zł/m²</span></div>` +
    `<div><b>${fmtN(median(ps))}</b><span>mediana ceny [zł]</span></div>` +
    `<div><b>${drops}</b><span>z obniżką ceny</span></div>`;
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
        onerror="this.outerHTML='<div class=noimg>🏠</div>'">`
    : `<div class="noimg">🏠</div>`;
  const badges = (o._new ? `<span class="badge new">NOWA</span>` : "") +
    (o._drop > 0 ? `<span class="badge drop">-${(o._drop*100).toFixed(0)}%</span>` : "");
  const meta = [PORTAL[o.s] || o.s, o.a ? o.a + " m²" : null,
    o.pm ? fmtN(o.pm) + " zł/m²" : null,
    o.r || null, o.d || null].filter(Boolean).join(" · ");
  return `<div class="card" data-id="${o.id}">${img}<div>
    <h4>${esc(o.t)}${badges}</h4>
    <div class="pr">${fmtP(o.p)}</div>
    <div class="meta">${esc(meta)}</div></div></div>`;
}
function renderMore(){
  const slice = filtered.slice(shown, shown + CHUNK);
  shown += slice.length;
  const sent = $("#more"); if(sent) sent.remove();
  $("#cards").insertAdjacentHTML("beforeend", slice.map(cardHTML).join(""));
  if(shown < filtered.length)
    $("#cards").insertAdjacentHTML("beforeend",
      `<div id="more">… wczytuję (${shown}/${filtered.length})</div>`);
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
  const gal = o.ph.length ? `<div id="gal">
      <img class="main" id="gmain" src="${esc(o.ph[0])}"
       onerror="this.closest('#gal').style.display='none'"></div>` +
    (o.ph.length > 1 ? `<div id="thumbs">` + o.ph.map((u,i) =>
      `<img src="${esc(u)}" data-i="${i}" class="${i?"":"on"}"
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
   .map(x => `<div><span>${x[0]}</span><b>${esc(x[1])}</b></div>`).join("");
  let hist = "";
  if(o.h){
    hist = `<div id="dhist"><h3>Historia cen</h3><table>` + o.h.map((x,i) => {
      const prev = i ? o.h[i-1][1] : null;
      const diff = prev == null ? "" :
        `<span class="${x[1] > prev ? "up" : "down"}">
          ${x[1] > prev ? "▲" : "▼"} ${fmtN(Math.abs(x[1]-prev))}</span> `;
      return `<tr><td>${esc(x[0])}</td><td>${diff}${fmtP(x[1])}</td></tr>`;
    }).join("") + `</table></div>`;
  }
  $("#dbody").innerHTML = gal +
    `<h2>${esc(o.t)}</h2>
     <div id="dprice">${fmtP(o.p)}${o.ng ? " <small>do negocjacji</small>" : ""}</div>
     <div id="dgrid">${grid}</div>` + hist +
    (o.rad > 0 ? `<div class="approx">📍 Sprzedający podał lokalizację
       przybliżoną (±${fmtN(o.rad)} m) — pinezka wskazuje okolicę.</div>` : "") +
    (o.dsc ? `<div id="ddesc">${esc(o.dsc)}</div>` : "") +
    `<a class="olxbtn" href="${esc(o.u)}" target="_blank" rel="noopener">
       Otwórz ogłoszenie na ${PORTAL[o.s] || "portalu"} ↗</a>`;
  const th = $("#thumbs");
  if(th) th.addEventListener("click", e => {
    if(e.target.tagName !== "IMG") return;
    $("#gmain").src = o.ph[+e.target.dataset.i];
    th.querySelectorAll("img").forEach(x => x.classList.remove("on"));
    e.target.classList.add("on");
  });
  $("#detail").classList.add("open");
  $("#backdrop").classList.add("open");
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
