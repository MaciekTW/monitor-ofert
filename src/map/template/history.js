"use strict";
/* ---------- zakładka: historia rynku ----------
   Korzysta z pomocników app.js ($, esc, fmtN, median, PORTAL, map, closeDetail, renderHisto,
   CH, CHART_BASE, chartInit).
   Dane: HIST.scans — znaczniki czasu skanów, HIST.offers — każda oferta (także wycofana)
   jako [portal, rynek, dzielnica, firma, metraż, pierwszy skan, ostatni skan, aktywna,
   [[nr skanu, cena], …]]. */
const HIST = JSON.parse($("#histdata").textContent);
const SCANS = HIST.scans.map(t => new Date(t));
const LAST = SCANS.length - 1;
const HOFF = HIST.offers.map(([s, mk, d, b, a, fs, ls, active, h]) => ({
  s, mk, d: d || "(nie podano)", b, a, fs, h,
  ls: active ? LAST : ls,
  // wycofanie wychodzi na jaw w pierwszym skanie, w którym oferty już nie było
  gone: !active && ls < LAST ? ls + 1 : null,
}));
const DAYKEYS = [...new Set(HIST.scans.map(t => t.slice(0, 10)))];
const TD0 = new Date(DAYKEYS[0] + "T00:00:00").getTime();
const TD1 = new Date(DAYKEYS.at(-1) + "T00:00:00").getTime() + 864e5;
const SERIES = {all: CH.acc, orange: CH.orange, aqua: CH.aqua};

const fmtDay = d => d.toLocaleDateString("pl-PL", {day: "2-digit", month: "2-digit"});
const fmtScan = d => fmtDay(d) + ", " + d.toLocaleTimeString("pl-PL", {hour: "2-digit", minute: "2-digit"});
const fmtK = v => v >= 1e6 ? (v / 1e6).toFixed(2).replace(".", ",") + " mln"
  : v >= 1e4 ? (v / 1e3).toFixed(1).replace(".", ",").replace(",0", "") + " tys." : fmtN(v);
const fmtPct = v => v == null ? "—"
  : (v > 0 ? "+" : v < 0 ? "−" : "±") + Math.abs(v * 100).toFixed(1).replace(".", ",") + "%";
const fmtDiff = v => (v > 0 ? "+" : v < 0 ? "−" : "±") + fmtN(Math.abs(v));

function priceAt(o, i){
  let p = null;
  for(const [j, v] of o.h){ if(j > i) break; p = v; }
  return p;
}

/* ---------- agregacja ---------- */
function histFilter(){
  const src = [...document.querySelectorAll("#hportals .chip.on")].map(b => b.dataset.s);
  const mk = $("#hmarket").value, sel = $("#hseller").value, d = $("#hdistrict").value;
  return o => src.includes(o.s) && (!mk || o.mk === mk)
    && (sel === "" || String(o.b) === sel) && (!d || o.d === d);
}
function aggregate(list){
  const n = SCANS.length;
  const zeros = () => new Array(n).fill(0), buckets = () => Array.from({length: n}, () => []);
  const A = {act: zeros(), src: {}, nw: zeros(), gone: zeros(), down: zeros(), up: zeros(),
    price: buckets(), ppm: {"": buckets(), "Pierwotny": buckets(), "Wtórny": buckets()}};
  for(const o of list){
    if(o.fs > 0) A.nw[o.fs]++;
    if(o.gone != null) A.gone[o.gone]++;
    const bySrc = A.src[o.s] ??= zeros();
    let k = -1;
    for(let i = o.fs; i <= o.ls; i++){
      while(k + 1 < o.h.length && o.h[k + 1][0] <= i) k++;
      A.act[i]++; bySrc[i]++;
      if(k < 0) continue;
      const p = o.h[k][1];
      A.price[i].push(p);
      if(o.a > 0){ A.ppm[""][i].push(p / o.a); A.ppm[o.mk]?.[i].push(p / o.a); }
    }
    for(let j = 1; j < o.h.length; j++)
      if(o.h[j][1] !== o.h[j - 1][1]) (o.h[j][1] < o.h[j - 1][1] ? A.down : A.up)[o.h[j][0]]++;
  }
  return A;
}
const perDay = arr => DAYKEYS.map(k => HIST.scans.reduce((s, t, i) => t.startsWith(k) ? s + arr[i] : s, 0));

/* ---------- wykresy (ECharts, wspólna konfiguracja w app.js) ---------- */
// wszystkie wykresy zakładki w jednej grupie: celownik i zoom osi czasu działają na nich razem
const HIST_GROUP = "hist";
echarts.connect(HIST_GROUP);
const tipRow = (color, value, name, bar) => `<div style="display:flex;align-items:center;gap:8px">
  <i style="display:inline-block;width:${bar ? "10px" : "12px"};height:${bar ? "10px" : "2px"};
    border-radius:2px;background:${color || "transparent"}"></i><b>${value}</b>
  <span style="color:${CH.mut}">${esc(name)}</span></div>`;
const tipHead = text => `<div style="color:${CH.mut};font-size:11.5px;margin-bottom:2px">${text}</div>`;
function histChart(el, option){
  const chart = chartInit(el);
  chart.group = HIST_GROUP;
  chart.setOption({
    ...CHART_BASE,
    grid: {left: 4, right: 12, top: 34, bottom: 4},
    xAxis: {...CHART_BASE.xAxis, min: TD0, max: TD1},
    toolbox: {right: 0, top: 0, itemSize: 13, iconStyle: {borderColor: CH.mut},
      feature: {dataZoom: {yAxisIndex: "none"}, restore: {}, saveAsImage: {pixelRatio: 2}}},
    legend: {show: false},
    ...option,
  }, true);
}

// linie w czasie (punkt = skan) z podpisanymi wartościami końcowymi
function lineChart(el, series, {fmt, fmtAxis = fmtK, fmtEnd = fmtAxis, extra = () => []}){
  histChart(el, {
    grid: {left: 4, right: 72, top: 34, bottom: 4},
    legend: {show: series.length > 1, left: 0, top: 2, itemWidth: 14, itemHeight: 3, icon: "roundRect",
      textStyle: {color: CH.mut, fontSize: 12}},
    tooltip: {...CHART_BASE.tooltip, trigger: "axis", formatter: ps => {
      const i = ps[0].dataIndex;
      return tipHead("skan " + fmtScan(SCANS[i])) +
        ps.map(p => tipRow(p.color, p.value[1] == null ? "—" : fmt(p.value[1]), p.seriesName)).join("") +
        extra(i).map(r => tipRow(null, r.value, r.name)).join("");
    }},
    yAxis: {type: "value", scale: true, splitNumber: 4, splitLine: {lineStyle: {color: CH.line}},
      axisLabel: {color: CH.mut, fontSize: 10.5, formatter: fmtAxis}},
    series: series.map(s => ({type: "line", name: s.name, color: s.color,
      data: s.vals.map((v, i) => [SCANS[i].getTime(), v]),
      showSymbol: false, symbolSize: 7, lineStyle: {width: 2},
      endLabel: {show: true, formatter: p => fmtEnd(p.value[1]), color: CH.ink, fontSize: 11, fontWeight: 600},
      labelLayout: {moveOverlap: "shiftY"}})),
  });
}

// słupki dzienne: jedna seria w górę, druga w dół od zera
function divergingBars(el, up, down){
  const u = perDay(up.vals), d = perDay(down.vals);
  const noon = k => new Date(k + "T12:00:00").getTime();
  const bar = (s, vals, sign, radius) => ({type: "bar", name: s.name, color: s.color, stack: "flow",
    barMaxWidth: 26, itemStyle: {borderRadius: radius},
    data: DAYKEYS.map((k, i) => [noon(k), sign * vals[i]])});
  histChart(el, {
    legend: {show: true, left: 0, top: 2, itemWidth: 10, itemHeight: 10, textStyle: {color: CH.mut, fontSize: 12}},
    tooltip: {...CHART_BASE.tooltip, trigger: "axis", formatter: ps => {
      const i = ps[0].dataIndex;
      return tipHead(new Date(noon(DAYKEYS[i])).toLocaleDateString("pl-PL",
          {weekday: "short", day: "numeric", month: "long"})) +
        ps.map(p => tipRow(p.color, fmtN(Math.abs(p.value[1])), p.seriesName, true)).join("");
    }},
    yAxis: {type: "value", splitNumber: 4, splitLine: {lineStyle: {color: CH.line}},
      axisLabel: {color: CH.mut, fontSize: 10.5, formatter: v => fmtK(Math.abs(v))}},
    series: [bar(up, u, 1, [3, 3, 0, 0]), bar(down, d, -1, [0, 0, 3, 3])],
  });
}

/* ---------- kafelki i tabela ---------- */
function tile(value, label, sub = ""){
  return `<div class="rounded-lg bg-white px-3 py-2 ring-1 ring-line">
    <b class="block text-[19px]/tight font-extrabold">${value}</b>
    <span class="text-[11.5px] text-mut">${label}</span>
    ${sub ? `<div class="mt-0.5 text-xs">${sub}</div>` : ""}</div>`;
}
// dla cen spadek jest „dobry” (z perspektywy kupującego), jak w reszcie strony
const pctSpan = v => v == null ? `<span class="text-mut">—</span>`
  : `<span class="${v < 0 ? "text-good" : v > 0 ? "text-bad" : "text-mut"}">${v < 0 ? "▼" : v > 0 ? "▲" : ""} ${fmtPct(v)}</span>`;
const rel = (a, b) => a != null && b ? a / b - 1 : null;

let distSort = {key: "now", dir: -1};
const DIST_COLS = [
  ["d", "Dzielnica"], ["now", "Aktywne"], ["delta", "zmiana"], ["nw", "Nowe"], ["gone", "Wycofane"],
  ["m", "Mediana zł/m²"], ["mch", "zmiana mediany"], ["down", "Obniżki"], ["up", "Podwyżki"]];
function districtRows(list){
  const byD = new Map();
  for(const o of list){
    if(!byD.has(o.d)) byD.set(o.d, []);
    byD.get(o.d).push(o);
  }
  const ppmAt = (os, i) => os.map(o => { const p = priceAt(o, i); return p != null && o.a > 0 ? p / o.a : null; })
    .filter(v => v != null);
  return [...byD].map(([d, os]) => {
    const nowL = os.filter(o => o.ls === LAST), firstL = os.filter(o => o.fs === 0);
    const mNow = ppmAt(nowL, LAST), mFirst = ppmAt(firstL, 0);
    let down = 0, up = 0;
    for(const o of os) for(let j = 1; j < o.h.length; j++)
      o.h[j][1] < o.h[j - 1][1] ? down++ : o.h[j][1] > o.h[j - 1][1] && up++;
    return {d, now: nowL.length, delta: nowL.length - firstL.length,
      nw: os.filter(o => o.fs > 0).length, gone: os.filter(o => o.gone != null).length,
      m: median(mNow),
      // przy kilku ofertach mediana skacze od przypadku — nie pokazujemy zmiany
      mch: mNow.length >= 10 && mFirst.length >= 10 ? rel(median(mNow), median(mFirst)) : null,
      down, up};
  });
}
function renderDistricts(rows){
  const {key, dir} = distSort;
  rows.sort((a, b) => key === "d" ? dir * a.d.localeCompare(b.d, "pl")
    : dir * ((a[key] ?? -Infinity) - (b[key] ?? -Infinity)));
  const th = "cursor-pointer border-b border-line px-2 py-1.5 text-left text-[11.5px] font-semibold whitespace-nowrap text-mut hover:text-ink";
  const td = "border-b border-dashed border-line px-2 py-1 whitespace-nowrap";
  $("#hdist").innerHTML = `<thead><tr>` + DIST_COLS.map(([k, label], i) =>
    `<th data-k="${k}" class="${th} ${i ? "text-right" : ""}">${label}${k === key ? (dir < 0 ? " ▾" : " ▴") : ""}</th>`).join("") +
    `</tr></thead><tbody>` + rows.map(r => `<tr class="hover:bg-[#f7f9fc]">
      <td class="${td} font-semibold">${esc(r.d)}</td>
      <td class="${td} text-right">${fmtN(r.now)}</td>
      <td class="${td} text-right text-mut">${fmtDiff(r.delta)}</td>
      <td class="${td} text-right">${fmtN(r.nw)}</td>
      <td class="${td} text-right">${fmtN(r.gone)}</td>
      <td class="${td} text-right">${fmtN(r.m)}</td>
      <td class="${td} text-right">${pctSpan(r.mch)}</td>
      <td class="${td} text-right">${fmtN(r.down)}</td>
      <td class="${td} text-right">${fmtN(r.up)}</td></tr>`).join("") + `</tbody>`;
}

/* ---------- składanie ---------- */
let histRows = [];
function renderHistory(){
  if(SCANS.length < 2){
    $("#htiles").innerHTML = `<p class="text-mut">Za mało danych — historia pojawi się po co najmniej dwóch skanach.</p>`;
    return;
  }
  const list = HOFF.filter(histFilter()), A = aggregate(list);
  const ppmMed = Object.fromEntries(Object.entries(A.ppm).map(([k, v]) => [k, v.map(median)]));
  const priceMed = A.price.map(median);
  const first = fmtDay(SCANS[0]);
  // ta sama oferta w pierwszym i ostatnim skanie — zmiana ceny bez wpływu składu ofert
  const stay = list.filter(o => o.fs === 0 && o.ls === LAST && o.h.length && o.h[0][0] === 0);
  const stayCh = stay.map(o => priceAt(o, LAST) / o.h[0][1] - 1);
  const sum = arr => arr.reduce((s, v) => s + v, 0);

  $("#hnote").textContent = `${SCANS.length} skanów · ${first} – ${fmtScan(SCANS[LAST])}`;
  $("#htiles").innerHTML =
    tile(fmtN(A.act[LAST]), "aktywnych ofert",
      `<span class="text-mut">${fmtDiff(A.act[LAST] - A.act[0])} od ${first}</span>`) +
    tile(fmtN(ppmMed[""][LAST]), "mediana zł/m²", pctSpan(rel(ppmMed[""][LAST], ppmMed[""][0])) + ` <span class="text-mut">od ${first}</span>`) +
    tile(fmtK(priceMed[LAST]), "mediana ceny", pctSpan(rel(priceMed[LAST], priceMed[0])) + ` <span class="text-mut">od ${first}</span>`) +
    tile(fmtN(sum(A.nw)), "nowych ofert", `<span class="text-mut">od ${first}</span>`) +
    tile(fmtN(sum(A.gone)), "wycofanych ofert", `<span class="text-mut">od ${first}</span>`) +
    tile(`<span class="text-good">▼${fmtN(sum(A.down))}</span> <span class="text-bad">▲${fmtN(sum(A.up))}</span>`,
      "obniżek · podwyżek cen") +
    // mediana, nie średnia: pojedyncze ceny-zaślepki (np. 1 zł → właściwa cena) rozwalają średnią
    tile(pctSpan(median(stayCh.filter(v => v))), "mediana zmiany ceny",
      `<span class="text-mut">wśród ${fmtN(stayCh.filter(v => v).length)} z ${fmtN(stay.length)} ofert obecnych
        od ${first}, które zmieniły cenę (▼${fmtN(stayCh.filter(v => v < 0).length)}
        ▲${fmtN(stayCh.filter(v => v > 0).length)})</span>`);

  lineChart($("#c-active"), [{name: "aktywne oferty", color: SERIES.all, vals: A.act}], {
    fmt: fmtN, fmtEnd: fmtN,
    extra: i => Object.entries(A.src).map(([s, v]) => ({name: PORTAL[s] || s, value: fmtN(v[i])}))});
  $("#c-flow-sub").textContent = `według dnia skanu, który zmianę wykrył; pierwszy skan (${fmtN(A.act[0])} ofert) to punkt startowy`;
  divergingBars($("#c-flow"), {name: "nowe", color: SERIES.all, vals: A.nw},
    {name: "wycofane", color: SERIES.orange, vals: A.gone});
  const mk = $("#hmarket").value;
  lineChart($("#c-ppm"), mk
    ? [{name: mk.toLowerCase(), color: mk === "Pierwotny" ? SERIES.orange : SERIES.aqua, vals: ppmMed[mk]}]
    : [{name: "wszystkie", color: SERIES.all, vals: ppmMed[""]},
       {name: "pierwotny", color: SERIES.orange, vals: ppmMed["Pierwotny"]},
       {name: "wtórny", color: SERIES.aqua, vals: ppmMed["Wtórny"]}],
    {fmt: v => fmtN(v) + " zł/m²", fmtEnd: fmtN});
  lineChart($("#c-price"), [{name: "mediana ceny", color: SERIES.all, vals: priceMed}], {fmt: v => fmtN(v) + " zł"});
  divergingBars($("#c-changes"), {name: "podwyżki", color: CH.bad, vals: A.up},
    {name: "obniżki", color: CH.good, vals: A.down});
  histRows = districtRows(list);
  renderDistricts(histRows);
}

/* ---------- zakładki i zdarzenia ---------- */
const districtsAll = [...new Set(HOFF.map(o => o.d))].sort((a, b) => a.localeCompare(b, "pl"));
$("#hdistrict").insertAdjacentHTML("beforeend",
  districtsAll.map(d => `<option value="${esc(d)}">${esc(d)}</option>`).join(""));
function showTab(name){
  const hist = name === "hist";
  $("#tab-map").hidden = hist;
  $("#mapctl").hidden = hist;
  $("#tab-hist").hidden = !hist;
  document.querySelectorAll("#tabs .tab").forEach(b => b.classList.toggle("on", b.dataset.tab === name));
  window.history.replaceState(null, "", hist ? "#historia" : location.pathname + location.search);
  if(hist){ closeDetail(); renderHistory(); }
  else { map.invalidateSize(); renderHisto(); }
}
document.querySelectorAll("#tabs .tab").forEach(b => b.addEventListener("click", () => showTab(b.dataset.tab)));
document.querySelectorAll("#hportals .chip").forEach(b =>
  b.addEventListener("click", () => { b.classList.toggle("on"); renderHistory(); }));
["#hmarket", "#hseller", "#hdistrict"].forEach(s => $(s).addEventListener("change", renderHistory));
$("#hdist").addEventListener("click", e => {
  const k = e.target.closest("th")?.dataset.k;
  if(!k) return;
  distSort = {key: k, dir: distSort.key === k ? -distSort.dir : k === "d" ? 1 : -1};
  renderDistricts(histRows);
});
if(location.hash === "#historia") showTab("hist");
