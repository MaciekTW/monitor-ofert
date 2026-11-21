"use strict";
/* ---------- zakładka: śledzone oferty ----------
   Korzysta z pomocników app.js ($, esc, fmtP, fmtN, PORTAL, OFFERS, GONE, EDITABLE, STATUSES,
   toggleTrack, setNote, statusOptions, openDetail, closeDetail) i history.js (tile, showTab).
   Dane (server.tracked_report): każda śledzona oferta w tej samej postaci co oferty na mapie —
   także taka, która zniknęła z portali — z listą zdarzeń "ev": [[czas, rodzaj, cena,
   poprzednia cena], …]. Z lokalnego serwera (--serve) dane są pobierane na bieżąco, w pliku
   otwartym z dysku jest stan z chwili wygenerowania.
   Układ: kafelek na ofertę (zdjęcie, cena, wykres ceny, status), a z boku zwijany panel
   z historią zmian wszystkich ofert albo jednej wybranej. */
let TRACKED = window.TRACKED || [];
let trFeedOnly = null;  // id oferty, do której zawężono historię zmian
const TR_SIDE_STORE = "monitor-ofert:historia-zmian";
let trSideOpen = true;
try { trSideOpen = localStorage.getItem(TR_SIDE_STORE) !== "0"; } catch { /* bez zapamiętanego stanu */ }

const trDay = ts => ts ? new Date(ts).toLocaleDateString("pl-PL") : "—";
const trFirst = t => t.ev.find(e => e[2] != null)?.[2];
// względna zmiana ceny od pierwszej zapisanej; dla kupującego obniżka jest dobra, jak w reszcie strony
const trChange = t => t.p != null && trFirst(t) ? t.p / trFirst(t) - 1 : 0;
function trDiff(now, before){
  if(now == null || !before || now === before) return `<span class="text-mut">bez zmian</span>`;
  const d = now - before, pct = Math.abs(d) / before * 100;
  return `<span class="font-semibold whitespace-nowrap ${d < 0 ? "text-good" : "text-bad"}">${d < 0 ? "▼ −" : "▲ +"}${fmtP(Math.abs(d))}
    (${d < 0 ? "−" : "+"}${pct < .05 ? "<0,1" : pct.toFixed(1).replace(".", ",")}%)</span>`;
}
const TR_BADGE = {
  new: ["pierwszy raz widziana", "bg-page text-mut"],
  price: ["zmiana ceny", "bg-acc-soft text-acc"],
  removed: ["zniknęła", "bg-[#fdecec] text-bad"],
  returned: ["wróciła", "bg-[#e3f4ea] text-good"],
  active: ["aktywna", "bg-[#e3f4ea] text-good"],
};
const trBadge = kind => `<span class="badge ml-0! whitespace-nowrap ${TR_BADGE[kind][1]}">${TR_BADGE[kind][0]}</span>`;
/* ---------- oś czasu w bocznym panelu ----------
   Zdarzenia pogrupowane dniami; każde to kropka na pionowej linii w kolorze rodzaju zdarzenia,
   godzina, tytuł oferty i — przy zmianie ceny — stara cena, nowa i różnica w procentach. */
const TR_EVENT = {
  new: {label: "Pierwszy raz widziana", color: CH.mut},
  down: {label: "Obniżka ceny", color: CH.good},
  up: {label: "Podwyżka ceny", color: CH.bad},
  removed: {label: "Zniknęła z portalu", color: CH.bad},
  returned: {label: "Wróciła na portal", color: CH.acc},
};
const trKind = e => e[1] !== "price" ? e[1] : e[2] < e[3] ? "down" : "up";
function trDayLabel(ts){
  const d = new Date(ts), days = Math.round((new Date().setHours(0, 0, 0, 0) - new Date(d).setHours(0, 0, 0, 0)) / 864e5);
  return days === 0 ? "dziś" : days === 1 ? "wczoraj" : d.toLocaleDateString("pl-PL", {day: "numeric", month: "long", year: "numeric"});
}
function trEventBody([, kind, price, before]){
  if(kind === "new") return price == null ? "" : `<span class="text-mut">cena</span> <b>${fmtP(price)}</b>`;
  if(kind !== "price") return "";
  const pct = Math.abs(price - before) / before * 100, down = price < before;
  return `<span class="text-mut line-through">${fmtP(before)}</span> <span class="text-mut">→</span> <b>${fmtP(price)}</b>
    <span class="ml-1 rounded-full px-1.5 py-px text-[11px] font-bold whitespace-nowrap ${down ? "bg-[#e3f4ea] text-good" : "bg-[#fdecec] text-bad"}"
      title="${down ? "−" : "+"}${fmtP(Math.abs(price - before))}">${down ? "−" : "+"}${pct < .05 ? "<0,1" : pct.toFixed(1).replace(".", ",")}%</span>`;
}
function trTimeline(feed, withTitle){
  let day = null, html = "";
  for(const {t, e} of feed){
    const label = trDayLabel(e[0]), k = TR_EVENT[trKind(e)], body = trEventBody(e);
    if(label !== day){
      html += `${day == null ? "" : "</ol>"}<h3 class="sticky top-0 z-1 -mx-3.5 bg-white/95 px-3.5 pt-2.5 pb-1.5 text-[11px] font-bold
        tracking-wide text-mut uppercase">${label}</h3><ol class="ml-1.5 border-l-2 border-line">`;
      day = label;
    }
    html += `<li class="relative pb-3 pl-4 last:pb-1">
      <i class="absolute top-[5px] -left-[6px] size-2.5 rounded-full ring-3 ring-white" style="background:${k.color}"></i>
      <div class="flex items-baseline justify-between gap-2">
        <span class="text-[12.5px] font-bold" style="color:${k.color}">${k.label}</span>
        <time class="text-[11.5px] whitespace-nowrap text-mut tabular-nums">${new Date(e[0]).toLocaleTimeString("pl-PL", {hour: "2-digit", minute: "2-digit"})}</time></div>
      ${withTitle ? `<button type="button" data-tr-show="${esc(t.id)}" class="line-clamp-2 cursor-pointer text-left text-[13px]/[1.35]
        hover:text-acc">${esc(t.t)}</button>` : ""}
      ${body ? `<div class="mt-0.5 text-[12.5px] tabular-nums">${body}</div>` : ""}</li>`;
  }
  return html + (day == null ? "" : "</ol>");
}
/* Schodkowy wykres ceny bez osi: od pierwszej zapisanej ceny do dziś (albo do zniknięcia oferty).
   Kolor mówi, czy oferta jest dziś tańsza (zielony), czy droższa (czerwony) niż na początku. */
function trSpark(t){
  const pts = t.ev.filter(e => e[2] != null).map(e => [new Date(e[0]).getTime(), e[2]]);
  if(!pts.length) return "";
  const gone = t.active ? null : t.ev.findLast(e => e[1] === "removed")?.[0];
  const t0 = pts[0][0], t1 = Math.max(gone ? new Date(gone).getTime() : Date.now(), pts.at(-1)[0] + 1);
  const lo = Math.min(...pts.map(p => p[1])), hi = Math.max(...pts.map(p => p[1]));
  const x = v => ((v - t0) / (t1 - t0) * 100).toFixed(2);
  const y = v => (hi === lo ? 14 : 24 - (v - lo) / (hi - lo) * 20).toFixed(2);
  const path = pts.map(([at, v], i) => i ? `H${x(at)}V${y(v)}` : `M0,${y(v)}`).join("") + "H100";
  const d = pts.at(-1)[1] - pts[0][1], color = d < 0 ? CH.good : d > 0 ? CH.bad : CH.mut;
  return `<div title="Przebieg ceny od ${trDay(t.ev.find(e => e[2] != null)[0])}${pts.length > 1 ? ` — ${pts.length - 1} ${
      plural(pts.length - 1, "zmiana", "zmiany", "zmian")}, od ${fmtP(lo)} do ${fmtP(hi)}` : " — bez zmian"}">
    <svg viewBox="0 0 100 28" preserveAspectRatio="none" class="block h-9 w-full">
      <path d="${path}V28H0Z" fill="${color}" opacity=".09"/>
      <path d="${path}" fill="none" stroke="${color}" stroke-width="2" stroke-linejoin="round" vector-effect="non-scaling-stroke"/></svg></div>`;
}
const trState = t => !t.active ? "removed" : t.ev.some(e => e[1] === "returned") ? "returned" : "active";
function trCard(t){
  const last = t.ev.at(-1), id = esc(t.id);
  const sub = [PORTAL[t.s] || t.s, t.a ? t.a + " m²" : null, t.r, t.d].filter(Boolean).map(esc).join(" · ");
  const noimg = `<div class="grid h-40 place-items-center bg-[#e8ebf0] text-4xl">🏠</div>`;
  const btn = "cursor-pointer rounded-lg border border-line bg-white px-2.5 py-1.5 text-center text-[12.5px] font-semibold hover:bg-acc-soft";
  return `<article class="flex flex-col overflow-hidden rounded-xl border border-line bg-white">
    <div class="relative">
      <button type="button" data-tr-show="${id}" title="Otwórz szczegóły oferty" class="block w-full cursor-pointer">${t.ph?.length
        ? `<img loading="lazy" src="${esc(t.ph[0])}" class="h-40 w-full bg-[#e8ebf0] object-cover ${t.active ? "" : "opacity-60 grayscale"}"
            onerror="this.outerHTML='<div class=&quot;grid h-40 place-items-center bg-[#e8ebf0] text-4xl&quot;>🏠</div>'">` : noimg}</button>
      <div class="absolute top-2 left-2">${trBadge(trState(t))}</div>
      <button type="button" data-tr-drop="${id}" title="Przestań śledzić" class="absolute top-2 right-2 grid size-7 cursor-pointer
        place-items-center rounded-full bg-[rgba(20,28,40,.55)] text-[13px] text-white hover:bg-bad">✕</button>
    </div>
    <div class="flex flex-1 flex-col gap-1.5 p-3">
      <div><button type="button" data-tr-show="${id}" class="line-clamp-2 cursor-pointer text-left text-[13.5px]/[1.3] font-semibold
          hover:text-acc">${esc(t.t)}</button>
        <div class="text-xs text-mut">${sub}</div></div>
      <div class="flex flex-wrap items-baseline justify-between gap-x-2">
        <b class="text-[17px] font-extrabold ${t.active ? "" : "text-mut"}">${fmtP(t.p)}</b>
        <span class="text-xs">${trDiff(t.p, trFirst(t))}</span></div>
      ${trSpark(t)}
      <div class="text-xs text-mut">${trBadge(last[1])} ${trDay(last[0])} · widziana od ${trDay(t.fs)}</div>
      <select data-tr-status="${id}" title="Status" class="field py-1! text-[12.5px]">${statusOptions(t)}</select>
      ${t.nt ? `<button type="button" data-tr-show="${id}" title="Notatka — edycja w szczegółach oferty"
        class="line-clamp-3 cursor-pointer rounded-md bg-[#fff8e1] px-2 py-1 text-left text-xs whitespace-pre-line">✎ ${esc(t.nt)}</button>` : ""}
      <div class="mt-auto flex gap-1.5 pt-1.5">
        <button type="button" data-tr-show="${id}" class="${btn} flex-1 border-acc! bg-acc-soft! text-acc">Szczegóły</button>
        ${t.gone ? `<a href="${esc(t.u)}" target="_blank" rel="noopener" class="${btn}" title="Dawny adres ogłoszenia">Ogłoszenie ↗</a>`
          : `<button type="button" data-tr-map="${id}" class="${btn}">Na mapie</button>`}
        <button type="button" data-tr-hist="${id}" class="${btn}" title="Pokaż historię tej oferty w panelu z boku">Historia</button>
      </div></div></article>`;
}

function loadTracked(){
  if(!EDITABLE) return renderTracked();
  return fetch("api/tracked").then(r => r.json()).then(d => { TRACKED = d; }).catch(() => {}).then(renderTracked);
}
function renderTracked(){
  // oferty, których nie ma już na mapie, szczegóły biorą stąd (offerById w app.js)
  GONE.clear();
  for(const t of TRACKED) if(!OFFERS.some(o => o.id === t.id)){ t.gone = true; t.tr = true; GONE.set(t.id, t); }
  const shownId = $("#detail").classList.contains("open") && $("#dtrack")?.dataset.id;
  if(shownId && !offerById(shownId)) closeDetail();  // przestała być śledzona, a na mapie jej nie ma
  if(trFeedOnly && !TRACKED.some(t => t.id === trFeedOnly)) trFeedOnly = null;

  $("#trcount").textContent = TRACKED.length ? `(${TRACKED.length})` : "";
  const changes = TRACKED.flatMap(t => t.ev.map(e => ({t, e})));
  const real = changes.filter(x => x.e[1] !== "new"), prices = real.filter(x => x.e[1] === "price");
  const weekAgo = Date.now() - 7 * 864e5;
  $("#trtiles").innerHTML =
    tile(TRACKED.length, "śledzonych ofert") +
    tile(TRACKED.filter(t => t.active).length, "aktywnych") +
    tile(`<span class="${TRACKED.some(t => !t.active) ? "text-bad" : ""}">${TRACKED.filter(t => !t.active).length}</span>`, "zniknęło z portali") +
    tile(`<span class="text-good">▼${prices.filter(x => x.e[2] < x.e[3]).length}</span>
      <span class="text-bad">▲${prices.filter(x => x.e[2] > x.e[3]).length}</span>`, "obniżek · podwyżek cen") +
    tile(real.filter(x => new Date(x.e[0]).getTime() >= weekAgo).length, "zmian w ostatnich 7 dniach");

  // filtr statusu: lista z tych statusów, które faktycznie są w użyciu
  const used = [...new Set(TRACKED.map(t => t.st).filter(Boolean))];
  const pick = $("#trstatus").value;
  $("#trstatus").innerHTML = `<option value="">każdy status</option><option value="-">bez statusu</option>` +
    used.map(s => `<option>${esc(s)}</option>`).join("");
  $("#trstatus").value = pick === "-" || used.includes(pick) ? pick : "";
  const state = $("#trstate .chip.on").dataset.state, status = $("#trstatus").value;
  const lastTs = t => t.ev.at(-1)[0] || "";
  const rank = t => { const i = STATUSES.indexOf(t.st); return t.st ? (i < 0 ? STATUSES.length : i) : 99; };
  const sorts = {
    event: (a, b) => lastTs(b).localeCompare(lastTs(a)),
    drop: (a, b) => trChange(a) - trChange(b),
    "p-asc": (a, b) => (a.p ?? 9e12) - (b.p ?? 9e12),
    "p-desc": (a, b) => (b.p ?? -1) - (a.p ?? -1),
    status: (a, b) => rank(a) - rank(b) || lastTs(b).localeCompare(lastTs(a)),
  };
  const list = TRACKED.filter(t => (!state || (state === "active") === !!t.active)
    && (!status || (status === "-" ? !t.st : t.st === status))).sort(sorts[$("#trsort").value]);
  const empty = text => `<p class="col-span-full rounded-md bg-white px-3 py-2.5 text-[13px] text-mut ring-1 ring-line">${text}</p>`;
  $("#trgrid").innerHTML = !TRACKED.length
    ? empty(`Nie śledzisz jeszcze żadnej oferty — użyj „☆ śledź” na liście ofert albo „Śledź ofertę” w szczegółach.${
        EDITABLE ? "" : " Zapis działa po uruchomieniu mapy z opcją --serve."}`)
    : list.map(trCard).join("") || empty("Żadna śledzona oferta nie pasuje do wybranych filtrów.");

  // boczny panel: bez zawężenia same zmiany, dla jednej oferty cała jej historia
  $("#trside").hidden = !trSideOpen;
  $("#trfeedopen").hidden = trSideOpen;
  const FEED_CAP = 300;
  const only = trFeedOnly && TRACKED.find(t => t.id === trFeedOnly);
  const feed = (only ? changes.filter(x => x.t === only) : real).sort((a, b) => (b.e[0] || "").localeCompare(a.e[0] || ""));
  $("#trfeed").innerHTML =
    (only ? `<div class="mt-2 flex items-start gap-2 rounded-md bg-acc-soft px-2.5 py-1.5 text-[12.5px]">
        <span class="min-w-0 flex-1">tylko: <b>${esc(only.t)}</b></span>
        <button type="button" data-tr-hist="" class="cursor-pointer whitespace-nowrap text-acc hover:underline">✕ wszystkie</button></div>`
      : "") +
    (!feed.length ? `<p class="mt-2 rounded-md bg-page px-3 py-2 text-[13px] text-mut">Śledzone oferty nie miały jeszcze żadnych zmian.</p>`
      : trTimeline(feed.slice(0, FEED_CAP), !only) +
        (feed.length > FEED_CAP ? `<p class="mt-2 text-xs text-mut">Pokazano ${FEED_CAP} najnowszych z ${feed.length} zmian.</p>` : ""));
}
function trSide(open){
  trSideOpen = open;
  try { localStorage.setItem(TR_SIDE_STORE, open ? "1" : "0"); } catch { /* bez zapisu */ }
  renderTracked();
}
$("#tab-tracked").addEventListener("click", e => {
  const b = e.target.closest("button");
  if(!b) return;
  const d = b.dataset;
  // szczegóły otwierają się nad tą zakładką; „Na mapie” przenosi do mapy z ofertą na środku
  if(d.trShow) openDetail(d.trShow, true);
  else if(d.trMap){ showTab("map"); openDetail(d.trMap, false); }
  else if(d.trDrop) toggleTrack(d.trDrop);
  else if("trHist" in d){ trFeedOnly = d.trHist || null; trSide(true); }
  else if(d.state != null){
    $("#trstate").querySelectorAll(".chip").forEach(x => x.classList.toggle("on", x === b));
    renderTracked();
  }
  else if(b.id === "trfeedclose") trSide(false);
  else if(b.id === "trfeedopen") trSide(true);
});
$("#tab-tracked").addEventListener("change", e => {
  if(e.target.dataset.trStatus) setNote(e.target.dataset.trStatus, {status: e.target.value || null});
  else renderTracked();  // filtr statusu albo sortowanie
});
renderTracked();
if(EDITABLE) loadTracked();
