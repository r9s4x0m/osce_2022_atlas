/* Research viewer. Filtering is entirely local: no event data is sent to map providers. */
"use strict";

const $ = (selector) => document.querySelector(selector);
const fmt = (n) => new Intl.NumberFormat("ru-RU").format(n);
const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (ch) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"})[ch]);
const toMs = (time) => Date.parse(`${time}:00+02:00`);
const dayName = (time) => `${time.slice(8, 10)}.${time.slice(5, 7)} ${time.slice(11, 16)}`;
const chronological = (a,b) => a.startMs - b.startMs || a.id - b.id;

const METHODS = [
  {key: "recorded", label: "Камера зафиксировала"},
  {key: "heard", label: "Наблюдатели слышали"},
  {key: "both", label: "Слышали и видели"},
  {key: "other", label: "Другой / не определён"},
];
const INCIDENT_ORDER = ["взрыв", "Н/Д боеприпас", "очередь", "выстрел", "выстрелы и очереди", "вспышка дульного пламени", "осветительная ракета"];
const TERRITORIES = [
  {key:"gca_definite", label:"Подконтрольная — уверенно", color:"#2668b5"},
  {key:"gca_likely", label:"Подконтрольная — вероятно", color:"#6cb7dc"},
  {key:"unknown", label:"Принадлежность не установлена", color:"#9098a5"},
  {key:"ngca_likely", label:"Неподконтрольная — вероятно", color:"#e3a078"},
  {key:"ngca_definite", label:"Неподконтрольная — уверенно", color:"#c34b46"},
];
const territoryInfo = (key) => TERRITORIES.find(x=>x.key===key) || TERRITORIES.find(x=>x.key==="unknown");
const observerInfo = (key) => key === "П" ? {label:"Подконтрольная правительству",color:"#2668b5"} :
  key === "Н" ? {label:"Неподконтрольная правительству",color:"#c34b46"} :
  {label:"Статус не указан в официальном описании",color:"#9098a5"};
const kindClass = (kind) => {
  if (kind.includes("боеприпас")) return "projectile";
  if (kind.includes("ракета")) return "rocket";
  if (kind.includes("выстрел") || kind.includes("очередь")) return "shot";
  if (kind.includes("вспышка")) return "other";
  return "";
};

let source = null;
let geography = null;
let events = [];
let filtered = [];
let displayLimit = 60;
let selectedId = null;
let activeSiteId = null;
let leaflet = null;
let leafletLayer = null;
let yandex = null;
let yandexChildren = [];
let mapEngine = "osm";
let heatWeekStart = 0;

const state = {
  from: "2022-02-14T00:00", to: "2022-02-21T00:00",
  types: new Set(INCIDENT_ORDER), methods: new Set(METHODS.map(x => x.key)),
  territories: new Set(TERRITORIES.map(x=>x.key)),
  search: "", cameras: true, patrols: true, incidents:true, uncertainty:true, line: true, schematic:false, area: true,
};

const HASH_LAYER_KEYS = [
  ["incidents", "i"], ["uncertainty", "u"], ["cameras", "c"],
  ["patrols", "p"], ["line", "l"], ["schematic", "s"], ["area", "a"],
];
const HASH_DATE_RE = /^2022-(?:01|02)-\d{2}T\d{2}:\d{2}$/;

function decodeFiltersFromHash() {
  const raw=window.location.hash.slice(1);
  if(!raw)return false;
  const params=new URLSearchParams(raw);
  if(params.get("v")!=="1")return false;
  const bounds=source.period;
  const from=params.get("from"),to=params.get("to");
  if(from&&to&&HASH_DATE_RE.test(from)&&HASH_DATE_RE.test(to)
    &&toMs(from)>=toMs(bounds[0])&&toMs(to)<=toMs(bounds[1])&&toMs(from)<toMs(to)){
    state.from=from;state.to=to;
  }
  for(const [param,prop,allowed] of [
    ["types","types",source.summary.incidentTypes],
    ["methods","methods",METHODS.map(x=>x.key)],
    ["territories","territories",TERRITORIES.map(x=>x.key)],
  ]){
    if(params.has(param))state[prop]=new Set(params.get(param).split("|").filter(x=>allowed.includes(x)));
  }
  if(params.has("q"))state.search=params.get("q").trim().toLowerCase().slice(0,250);
  if(params.has("layers")){
    const enabled=new Set(params.get("layers").split(""));
    for(const [prop,code] of HASH_LAYER_KEYS)state[prop]=enabled.has(code);
  }
  return true;
}

function encodeFiltersInHash() {
  const params=new URLSearchParams();
  params.set("v","1");params.set("from",state.from);params.set("to",state.to);
  params.set("types",[...state.types].sort().join("|"));
  params.set("methods",[...state.methods].sort().join("|"));
  params.set("territories",[...state.territories].sort().join("|"));
  if(state.search)params.set("q",state.search);
  params.set("layers",HASH_LAYER_KEYS.filter(([prop])=>state[prop]).map(([,code])=>code).join(""));
  const hash="#"+params.toString();
  if(window.location.hash!==hash)history.replaceState(null,"",location.pathname+location.search+hash);
}

function applyFilterControlsFromState() {
  $("#date-from").value=state.from;$("#date-to").value=state.to;
  $("#search-events").value=state.search;
  document.querySelectorAll("[data-incident]").forEach(x=>x.checked=state.types.has(x.dataset.incident));
  document.querySelectorAll("[data-method]").forEach(x=>x.checked=state.methods.has(x.dataset.method));
  document.querySelectorAll("[data-territory]").forEach(x=>x.checked=state.territories.has(x.dataset.territory));
  $("#toggle-incidents").textContent=state.types.size===source.summary.incidentTypes.length?"Снять все":"Выбрать все";
  $("#toggle-territories").textContent=state.territories.size===TERRITORIES.length?"Снять все":"Выбрать все";
  const layerIds={incidents:"layer-incidents",uncertainty:"layer-uncertainty",cameras:"layer-cameras",
    patrols:"layer-patrols",line:"layer-line",schematic:"layer-schematic",area:"layer-area"};
  for(const [prop,id] of Object.entries(layerIds))$("#"+id).checked=state[prop];
}

const metadataUrl = (row) => `https://docs.google.com/spreadsheets/d/12UFpinZWtTaa69D3JMANVHEa7lkPqVYaUIII-nCtZGs/edit#gid=0&range=A${row}:L${row}`;

async function load() {
  try {
    const [r1, r2] = await Promise.all([fetch("./data/events.json"), fetch("./data/geography.json")]);
    if (!r1.ok || !r2.ok) throw new Error(`Сетевая ошибка: ${r1.status}, ${r2.status}`);
    [source, geography] = await Promise.all([r1.json(), r2.json()]);
    events = source.events.map(e => ({...e, startMs: toMs(e.start), endMs: toMs(e.end)}));
    state.from = source.period[0]; state.to = source.period[1];
    for (const id of ["date-from", "date-to"]) {
      $("#"+id).min = source.period[0]; $("#"+id).max = source.period[1];
    }
    const days = Math.round((toMs(source.period[1])-toMs(source.period[0]))/86400000);
    heatWeekStart = Math.max(0, days-7);
    $(".topbar-period").innerHTML = `<span class="live-dot"></span> ${source.sourceKind === "official_osce_pdf" ? "1 января — 20 февраля 2022" : "14–20 февраля 2022"}`;
    if (source.sourceKind === "official_osce_pdf") {
      $("#methodology-source").innerHTML = `В основе — <a href="https://www.osce.org/ukraine-smm/reports" target="_blank" rel="noopener">40 официальных ежедневных отчётов СММ ОБСЕ</a> (№ 1–40/2022) и их русскоязычные приложения. Наблюдения отобраны по времени самой записи: 1 января — 20 февраля 2022 года. Проверены полнота списка отчётов, извлечение строк и суммы. <a href="./data/provenance.json" target="_blank">Список отчётов с SHA-256 PDF</a> и <a href="./data/daily.csv" target="_blank">подневная статистика CSV</a>. Числа — сумма исходного поля «К-во», а не независимо подтверждённое число разных физических событий.`;
    }
    state.types = new Set(source.summary.incidentTypes);
    renderFilterControls();
    decodeFiltersFromHash();
    if (state.from !== source.period[0] || state.to !== source.period[1]) {
      heatWeekStart = Math.min(Math.max(0, days-7), Math.max(0, Math.floor((toMs(state.from)-toMs(source.period[0]))/86400000)));
    }
    applyFilterControlsFromState();
    attachControls();
    renderEverything();
    initLeaflet();
  } catch (error) {
    console.error(error);
    $("#stat-quantity").textContent = "Ошибка";
    $("#journal-body").innerHTML = `<tr><td colspan="5">Не удалось загрузить исходные данные. ${esc(error.message)}</td></tr>`;
    $("#map-unavailable").hidden = false;
  }
}

function renderFilterControls() {
  document.querySelectorAll(".preset").forEach(node => {
    node.hidden=toMs(node.dataset.from)<toMs(source.period[0])||toMs(node.dataset.to)>toMs(source.period[1]);
  });
  const typeCounts = Object.fromEntries(source.summary.incidentTypes.map(x => [x, events.filter(e => e.incident === x).length]));
  const orderedTypes = [...INCIDENT_ORDER.filter(x => typeCounts[x]), ...source.summary.incidentTypes.filter(x => !INCIDENT_ORDER.includes(x))];
  $("#incident-filters").innerHTML = orderedTypes.map(type => `
    <label class="check-option"><input type="checkbox" data-incident="${esc(type)}" checked>
      <span>${esc(type)}</span><small>${fmt(typeCounts[type])}</small>
    </label>`).join("");
  $("#method-filters").innerHTML = METHODS.filter(item => source.summary.methods[item.key]).map(item => `
    <label class="check-option"><input type="checkbox" data-method="${item.key}" checked>
      <span>${esc(item.label)}</span><small>${fmt(source.summary.methods[item.key] || 0)}</small>
    </label>`).join("");
  $("#territory-filters").innerHTML = TERRITORIES.map(item => `
    <label class="check-option"><input type="checkbox" data-territory="${item.key}" checked>
      <span class="swatch diamond" style="background:${item.color}"></span><span>${esc(item.label)}</span>
      <small>${fmt(source.summary.incidentTerritoryCounts?.[item.key] || 0)}</small>
    </label>`).join("");
  document.querySelectorAll("[data-incident]").forEach(node => node.addEventListener("change", () => {
    node.checked ? state.types.add(node.dataset.incident) : state.types.delete(node.dataset.incident);
    displayLimit = 60;
    renderEverything();
  }));
  document.querySelectorAll("[data-method]").forEach(node => node.addEventListener("change", () => {
    node.checked ? state.methods.add(node.dataset.method) : state.methods.delete(node.dataset.method);
    displayLimit = 60;
    renderEverything();
  }));
  document.querySelectorAll("[data-territory]").forEach(node => node.addEventListener("change", () => {
    node.checked ? state.territories.add(node.dataset.territory) : state.territories.delete(node.dataset.territory);
    displayLimit=60;renderEverything();
  }));
}

function attachControls() {
  for (const id of ["date-from", "date-to"]) $("#" + id).addEventListener("change", () => {
    const value = $("#" + id).value;
    if (value) state[id === "date-from" ? "from" : "to"] = value;
    if (toMs(state.to) <= toMs(state.from)) {
      $("#" + id).setCustomValidity("Конец интервала должен быть позднее начала");
      $("#" + id).reportValidity();
      return;
    }
    $("#date-from").setCustomValidity(""); $("#date-to").setCustomValidity("");
    displayLimit = 60; renderEverything();
  });
  $("#reset-dates").addEventListener("click", () => setPeriod(...source.period));
  document.querySelectorAll(".preset").forEach(node => node.addEventListener("click", () => setPeriod(node.dataset.from, node.dataset.to)));
  $("#heat-prev").addEventListener("click", () => {heatWeekStart=Math.max(0,heatWeekStart-7);renderHeatmap();});
  $("#heat-next").addEventListener("click", () => {heatWeekStart=Math.min(Math.max(0,Math.round((toMs(source.period[1])-toMs(source.period[0]))/86400000)-7),heatWeekStart+7);renderHeatmap();});
  $("#toggle-incidents").addEventListener("click", () => {
    const everything = state.types.size < source.summary.incidentTypes.length;
    state.types = new Set(everything ? source.summary.incidentTypes : []);
    document.querySelectorAll("[data-incident]").forEach(input => { input.checked = everything; });
    $("#toggle-incidents").textContent = everything ? "Снять все" : "Выбрать все";
    renderEverything();
  });
  $("#toggle-territories").addEventListener("click", () => {
    const everything=state.territories.size<TERRITORIES.length;
    state.territories=new Set(everything?TERRITORIES.map(x=>x.key):[]);
    document.querySelectorAll("[data-territory]").forEach(input=>{input.checked=everything;});
    $("#toggle-territories").textContent=everything?"Снять все":"Выбрать все";
    renderEverything();
  });
  for (const [id, prop] of [["layer-cameras", "cameras"], ["layer-patrols", "patrols"], ["layer-incidents", "incidents"], ["layer-uncertainty", "uncertainty"], ["layer-line", "line"], ["layer-schematic", "schematic"], ["layer-area", "area"]]) {
    $("#" + id).addEventListener("change", (e) => {state[prop] = e.target.checked; encodeFiltersInHash();renderMap();});
  }
  $("#search-events").addEventListener("input", (e) => {state.search = e.target.value.trim().toLowerCase(); displayLimit = 60; renderEverything();});
  $("#load-more").addEventListener("click", () => {displayLimit += 60; renderJournal();});
  $("#export-csv").addEventListener("click", downloadCsv);
  $("#show-methodology").addEventListener("click", () => $("#methodology").showModal());
  $("#open-settings").addEventListener("click", () => $("#settings").showModal());
  $("#close-settings").addEventListener("click", () => $("#settings").close());
  $("#choose-osm").addEventListener("click", () => {switchToLeaflet(); $("#settings").close();});
  $("#choose-yandex").addEventListener("click", requestYandex);
  $("#yandex-key").value = sessionStorage.getItem("osce_yandex_js_key") || "";
  $("#journal-body").addEventListener("click", (event) => {
    const row = event.target.closest("[data-id]");
    if (row) selectEvent(Number(row.dataset.id));
  });
  $("#selection-card").addEventListener("click", (event) => {
    if (event.target.closest(".selection-close")) clearSelection();
    const target = event.target.closest("[data-event]");
    if (target){event.preventDefault();selectEvent(Number(target.dataset.event));}
  });
  window.addEventListener("hashchange",()=>{
    if(decodeFiltersFromHash()){
      applyFilterControlsFromState();displayLimit=60;renderEverything();
    }
  });
}

function setPeriod(from, to) {
  state.from = from; state.to = to;
  $("#date-from").value = from; $("#date-to").value = to;
  $("#date-from").setCustomValidity(""); $("#date-to").setCustomValidity("");
  const days=Math.round((toMs(source.period[1])-toMs(source.period[0]))/86400000);
  if (from!==source.period[0] || to!==source.period[1]) {
    heatWeekStart=Math.min(Math.max(0,days-7),Math.max(0,Math.floor((toMs(from)-toMs(source.period[0]))/86400000)));
  }
  displayLimit = 60; renderEverything();
}

function passesCategories(e) { return state.types.has(e.incident) && state.methods.has(e.method) && state.territories.has(e.incidentTerritory); }

function renderTerritorySummary() {
  const tally=Object.fromEntries(TERRITORIES.map(item=>[item.key,0]));
  const mapTally=Object.fromEntries(TERRITORIES.map(item=>[item.key,0]));
  for(const e of filtered){
    tally[e.incidentTerritory]=(tally[e.incidentTerritory]||0)+(e.quantity||0);
    if(e.incidentPoint)mapTally[e.incidentTerritory]+=(e.quantity||0);
  }
  const total=Object.values(tally).reduce((sum,value)=>sum+value,0);
  const mapTotal=filtered.reduce((sum,e)=>sum+(e.incidentPoint?(e.quantity||0):0),0);
  $("#territory-summary").innerHTML=TERRITORIES.map(item=>{
    const count=tally[item.key];
    const percent=total?100*count/total:0;
    return `<div class="territory-total-row${state.territories.has(item.key)?"":" inactive"}" title="${esc(item.label)}: ${fmt(count)} из ${fmt(total)} (по «К-во»)">
      <span class="territory-total-dot" style="background:${item.color}"></span>
      <span class="territory-total-name">${esc(item.label)}${mapTally[item.key]!==count?`<small>На карте: ${fmt(mapTally[item.key])}; без координат: ${fmt(count-mapTally[item.key])}</small>`:""}</span>
      <strong class="territory-total-value">${fmt(count)}</strong>
      <div class="territory-total-track"><div class="territory-total-fill" style="background:${item.color};width:${percent.toFixed(2)}%"></div></div>
    </div>`;
  }).join("");
  $("#mapped-quantity").textContent=fmt(mapTotal);
  $("#unmapped-quantity").textContent=fmt(total-mapTotal);
}

function renderEverything() {
  const from = toMs(state.from), to = toMs(state.to);
  if (!(to > from)) return;
  const query = state.search;
  filtered = events.filter(e => passesCategories(e) && e.startMs < to && e.endMs >= from && (!query || [e.position,e.where,e.description,e.incident,e.rawDate].some(x => String(x || "").toLowerCase().includes(query))));
  filtered.sort((a, b) => b.startMs - a.startMs || b.id - a.id);
  if(selectedId!==null&&!filtered.some(e=>e.id===selectedId)){
    selectedId=null;$("#selection-card").hidden=true;
  }
  $("#stat-quantity").textContent = fmt(filtered.reduce((s,e) => s + (e.quantity || 0), 0));
  $("#stat-records").textContent = fmt(filtered.length);
  $("#stat-mapped").textContent = fmt(filtered.filter(e => !!e.incidentPoint).length);
  $("#journal-count").textContent = fmt(filtered.length);
  renderTerritorySummary();
  document.querySelectorAll(".preset").forEach(node => node.classList.toggle("active", node.dataset.from === state.from && node.dataset.to === state.to));
  encodeFiltersInHash();
  renderHeatmap();
  renderJournal();
  renderMap();
}

function renderHeatmap() {
  // Full-period daily overview plus a navigable seven-day hourly window.
  // Range rows are counted once at the start to avoid inflating violations.
  const baseline = toMs(source.period[0]);
  const days = Math.round((toMs(source.period[1])-baseline)/86400000);
  const hourly = Array.from({length:days*24}, () => 0);
  const dayCode = (i) => new Date(baseline+i*86400000+7200000).toISOString().slice(0,10);
  for (const e of events) {
    if (!passesCategories(e)) continue;
    const index = Math.floor((e.startMs-baseline)/3600000);
    if (index >= 0 && index < hourly.length) hourly[index] += e.quantity || 0;
  }
  const max = Math.max(1, ...hourly);
  const startMs = toMs(state.from), endMs = toMs(state.to);
  const daily=Array.from({length:days},(_,d)=>hourly.slice(d*24,(d+1)*24).reduce((a,b)=>a+b,0));
  const maxDay=Math.max(1,...daily);
  $("#heat-days").innerHTML=daily.map((value,d)=>{
    const date=dayCode(d);
    const selected=toMs(date+"T00:00")<endMs&&toMs(date+"T00:00")+86400000>startMs;
    const opacity=value ? 0.16+0.8*Math.log1p(value)/Math.log1p(maxDay) : 0.035;
    return `<button type="button" data-day="${date}" class="heat-day-button${selected?" selected":""}" style="background:rgba(199,100,76,${opacity.toFixed(3)})" title="${date}: ${fmt(value)} по «К-во»">${date.slice(8)}.${date.slice(5,7)}</button>`;
  }).join("");
  $("#heat-days").querySelectorAll("[data-day]").forEach(button=>button.addEventListener("click",()=>{
    const day=Math.round((toMs(button.dataset.day+"T00:00")-baseline)/86400000);
    setPeriod(button.dataset.day+"T00:00",dayCode(day+1)+"T00:00");
  }));
  heatWeekStart=Math.max(0,Math.min(heatWeekStart,Math.max(0,days-7)));
  $("#heat-week-label").textContent=`${dayCode(heatWeekStart).slice(8)}.${dayCode(heatWeekStart).slice(5,7)} — ${dayCode(Math.min(days-1,heatWeekStart+6)).slice(8)}.${dayCode(Math.min(days-1,heatWeekStart+6)).slice(5,7)}`;
  $("#heat-prev").disabled=heatWeekStart===0;
  $("#heat-next").disabled=heatWeekStart+7>=days;
  const elements = [];
  for (let d=heatWeekStart;d<Math.min(days,heatWeekStart+7);d++) {
    const date=dayCode(d);
    elements.push(`<span class="heatday">${date.slice(8)}.${date.slice(5,7)}</span>`);
    for (let h=0;h<24;h++) {
      const val=hourly[d*24+h];
      const hourCode = `${date}T${String(h).padStart(2,"0")}:00`;
      const selected = (state.from!==source.period[0]||state.to!==source.period[1]) && toMs(hourCode)>=startMs && toMs(hourCode)<endMs;
      const opacity = val ? (.15+.82*Math.log1p(val)/Math.log1p(max)) : .04;
      elements.push(`<button type="button" class="heat-cell${selected ? " selected" : ""}" data-hour="${hourCode}" style="background:rgba(199,100,76,${opacity.toFixed(3)})" aria-label="${date} ${String(h).padStart(2,"0")}:00: ${val} по «К-во»" title="${date}, ${String(h).padStart(2,"0")}:00 — ${fmt(val)} по «К-во»"></button>`);
    }
  }
  $("#heatmap").innerHTML = elements.join("");
  $("#heatmap").querySelectorAll("[data-hour]").forEach(button => button.addEventListener("click", () => {
    const from = button.dataset.hour;
    const upper = new Date(toMs(from)+3600000+2*3600000).toISOString().slice(0,16);
    setPeriod(from, upper);
  }));
}

function renderJournal() {
  const rows = filtered.slice(0, displayLimit);
  $("#journal-body").innerHTML = rows.length ? rows.map(e => `<tr data-id="${e.id}" class="${e.id===selectedId ? "active" : ""}">
    <td class="date-cell">${esc(dayName(e.start))}${e.end !== e.start ? `<span class="method-tag">до ${esc(dayName(e.end))}</span>` : ""}</td>
    <td><span class="kind-dot ${kindClass(e.incident)}"></span>${esc(e.incident)}<span class="method-tag">${esc(e.methodOriginal)}</span><span class="territory-tag"><span class="territory-dot" style="background:${territoryInfo(e.incidentTerritory).color}"></span>${esc(territoryInfo(e.incidentTerritory).label)}</span></td>
    <td><strong>${e.quantity === null ? "—" : fmt(e.quantity)}</strong></td>
    <td title="${esc(e.position)}"><span class="location-label">${esc(e.siteName || e.position)}</span><span class="method-tag">${esc(e.where)}</span></td>
    <td><button type="button" class="pin-button" aria-label="Показать запись на карте" title="Показать запись на карте" data-id="${e.id}">⌖</button></td>
  </tr>`).join("") : `<tr><td colspan="5">Для выбранных фильтров записей нет.</td></tr>`;
  $("#journal-shown").textContent = `Показано ${fmt(rows.length)} из ${fmt(filtered.length)} строк`;
  $("#load-more").hidden = rows.length >= filtered.length;
}

function currentSites() {
  const groups = new Map();
  const controlBySite=new Map();
  const corrections=new Map();
  for(const e of events){
    if(e.site&&e.observerControlCorrection)corrections.set(e.site,e.observerControlCorrection);
    if(!e.site || !["П","Н"].includes(e.observerControl))continue;
    if(!controlBySite.has(e.site))controlBySite.set(e.site,new Set());
    controlBySite.get(e.site).add(e.observerControl);
  }
  for (const e of filtered) {
    if (!e.coordinates || !e.site) continue;
    if (e.siteType === "camera" && !state.cameras) continue;
    if (e.siteType === "patrol" && !state.patrols) continue;
    let group = groups.get(e.site);
    if (!group) {
      group = {id:e.site,name:e.siteName,point:e.coordinates,type:e.siteType,accuracyKm:e.accuracyKm,locationBasis:e.locationBasis,rows:0,quantity:0,events:[]};
      groups.set(e.site,group);
    }
    group.rows++;
    group.quantity += e.quantity || 0;
    group.events.push(e);
  }
  // A camera exists independently of the currently selected events. Keep the
  // 18 historical reference markers on the map; fade those with no matching
  // records rather than implying the cameras were absent during that hour.
  if (state.cameras) for (const c of geography.cameraSites) {
    if (!groups.has(c.site_id)) groups.set(c.site_id, {
      id:c.site_id, name:c.title, point:c.coordinates, type:"camera",
      accuracyKm:c.accuracyKm, locationBasis:c.locationBasis, controlOverride:c.controlOverride,
      rows:0, quantity:0, events:[],
    });
  }
  for(const site of groups.values()){
    const labels=controlBySite.get(site.id);
    site.control=labels?.size===1?[...labels][0]:"?";
    site.controlConflict=!!labels&&labels.size>1;
    site.controlCorrection=corrections.get(site.id)||site.controlOverride;
  }
  return [...groups.values()];
}

function currentIncidentGroups() {
  if(!state.incidents)return [];
  const groups=new Map();
  // Screen-space aggregation: fewer diamonds at regional zoom, more detail as
  // the reader zooms in. These cells are display bins, not inferred events.
  const zoom=mapEngine==="osm"&&leaflet?leaflet.getZoom():8;
  const [lonStep,latStep]=zoom<=8?[0.19,0.14]:zoom===9?[0.105,0.075]:zoom===10?[0.052,0.037]:[0.027,0.019];
  for(const e of filtered){
    if(!e.incidentPoint)continue;
    // Aggregate crowded diamonds without merging different territory classes.
    // The anchor is only a visual summary, not a new estimated event location.
    const [lon,lat]=e.incidentPoint;
    const key=`${Math.round(lon/lonStep)}:${Math.round(lat/latStep)}:${e.incidentTerritory}`;
    let group=groups.get(key);
    if(!group){group={key,point:[0,0],events:[],quantity:0,territory:e.incidentTerritory};groups.set(key,group);}
    group.events.push(e);group.point[0]+=lon;group.point[1]+=lat;
    group.quantity+=e.quantity||0;
  }
  return [...groups.values()].map(group=>({...group,point:group.point.map(v=>v/group.events.length)}));
}

function markerHtml(group) {
  const color=territoryInfo(group.territory).color;
  const selected=group.events.some(e=>e.id===selectedId);
  const count=group.events.length,quantity=group.quantity;
  return `<button class="incident-marker${selected?" active":""}" type="button" title="${esc(territoryInfo(group.territory).label)} · ${fmt(quantity)} по «К-во» · ${count} строк" aria-label="${esc(territoryInfo(group.territory).label)}: ${fmt(quantity)} по «К-во», ${count} строк">
    <span class="incident-diamond" style="--mark:${color}"></span><span class="incident-count" style="--mark:${color}">${quantity?fmt(quantity):"—"}</span>
  </button>`;
}

// The shared modelled uncertainty envelope is computed once in Python and
// also used to determine territory confidence. Never display a smaller
// area than the classifier evaluates. This is NOT a probability contour.
function uncertaintyRadiusKm(event){
  if(Number.isFinite(event.uncertaintyRadiusKm))return event.uncertaintyRadiusKm;
  const d=event.direction;
  if(!d)return 0;
  const {near_km:near,far_km:far,half_angle_deg:half}=d;
  const center=far>near?(2/3)*(far**3-near**3)/(far**2-near**2):near;
  const offset=(distance)=>Math.sqrt(Math.max(0,center**2+distance**2-2*center*distance*Math.cos(half*Math.PI/180)));
  return Math.max(.5,offset(.85*near),offset(1.15*far))+(event.accuracyKm||0);
}

// A shared geographical description is rendered once even if it occurs in
// many OSCE rows; this is display deduplication, not event deduplication.
function currentUncertaintyAreas(){
  if(!state.incidents||!state.uncertainty)return [];
  const zones=new Map();
  for(const event of filtered){
    if(!event.incidentPoint||!event.direction)continue;
    const key=`${event.site}|${event.where}|${event.incidentTerritory}`;
    const existing=zones.get(key);
    if(existing){existing.quantity+=event.quantity||0;existing.rows++;}
    else zones.set(key,{
      point:event.incidentPoint,radiusKm:uncertaintyRadiusKm(event),
      territory:event.incidentTerritory,quantity:event.quantity||0,rows:1,
    });
  }
  return [...zones.values()];
}

function renderMap() {
  if (!geography || !source) return;
  if (mapEngine === "yandex" && yandex) renderYandex();
  else if (leaflet && typeof L !== "undefined") renderLeaflet();
}

function initLeaflet() {
  if (typeof L === "undefined") {
    $("#map-unavailable").hidden = false;
    return;
  }
  try {
    $("#map").innerHTML = "";
    leaflet = L.map("map", {zoomControl:false, preferCanvas:true, scrollWheelZoom:true}).setView([48.05,38.45],8);
    L.control.zoom({position:"bottomright"}).addTo(leaflet);
    L.control.scale({position:"bottomleft",metric:true,imperial:false,maxWidth:145}).addTo(leaflet);
    L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
      maxZoom:19, attribution:'© <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">OpenStreetMap</a> contributors',
    }).addTo(leaflet);
    leafletLayer = L.layerGroup().addTo(leaflet);
    leaflet.on("zoomend",()=>{if(mapEngine==="osm")renderLeaflet();});
    mapEngine = "osm";
    $("#map-unavailable").hidden = true;
    $("#basemap-label").textContent = "OSM";
    $("#map-control-note").textContent = "Современная подложка OSM. Исторические данные показаны отдельными слоями.";
    renderLeaflet();
    setTimeout(() => leaflet?.invalidateSize(), 160);
  } catch (error) {
    console.error("Map initialization failed",error);
    $("#map-unavailable").hidden = false;
  }
}

function renderLeaflet() {
  if (!leafletLayer) return;
  leafletLayer.clearLayers();
  if (state.area) L.geoJSON({type:"Feature",geometry:geography.territory.geometry}, {
    style:{color:"#d69a3c",weight:1.5,fillColor:"#e4ac59",fillOpacity:.13,opacity:.62},
    interactive:false,
  }).addTo(leafletLayer);
  if (state.line) L.geoJSON({type:"Feature",geometry:geography.contactLine.geometry}, {
    style:{color:"#ae473d",weight:3,opacity:.9},interactive:false,
  }).addTo(leafletLayer);
  if (state.schematic) L.geoJSON({type:"Feature",geometry:geography.osceSchematic.geometry},{
    style:{color:"#465c76",weight:2.5,dashArray:"3 8",opacity:.85},interactive:false,
  }).addTo(leafletLayer);
  for(const zone of currentUncertaintyAreas()){
    L.circle([zone.point[1],zone.point[0]],{
      radius:zone.radiusKm*1000,color:territoryInfo(zone.territory).color,
      weight:1.35,dashArray:"4 5",opacity:.55,fillColor:territoryInfo(zone.territory).color,
      fillOpacity:.08,interactive:false,
    }).addTo(leafletLayer);
  }
  const e = events.find(x=>x.id===selectedId);
  if (state.incidents&&e?.coordinates&&e.direction) {
    const polygon = sectorCoordinates(e);
    if(polygon.length)L.polygon(polygon.map(c=>[c[1],c[0]]),{color:"#183e55",weight:2,fillColor:territoryInfo(e.incidentTerritory).color,fillOpacity:.19,dashArray:"5 4",interactive:false}).addTo(leafletLayer);
    if(e.incidentPoint)L.polyline([[e.coordinates[1],e.coordinates[0]],[e.incidentPoint[1],e.incidentPoint[0]]],{color:"#516a7a",weight:1.5,dashArray:"3 6",opacity:.8,interactive:false}).addTo(leafletLayer);
  }
  for (const group of currentIncidentGroups()) {
    const count=group.events.length;
    const icon=L.divIcon({className:"incident-icon",html:markerHtml(group),iconSize:[68,36],iconAnchor:[16,16]});
    const marker=L.marker([group.point[1],group.point[0]],{icon,zIndexOffset:group.events.some(x=>x.id===selectedId)?800:100}).addTo(leafletLayer);
    marker.bindTooltip(`${esc(territoryInfo(group.territory).label)} · ${fmt(group.quantity)} по «К-во» (${fmt(count)} строк)`,{sticky:true});
    marker.on("click",()=>count===1?selectEvent(group.events[0].id):selectIncidentGroup(group));
  }
  const sites = currentSites();
  for (const site of sites) {
    const selected = site.events.some(e=>e.id===selectedId) || site.id===activeSiteId;
    const radius = 4+Math.min(8, Math.sqrt(site.rows)*1.2) + (selected ? 2 : 0);
    const camera = site.type === "camera";
    const control=observerInfo(site.control);
    if(state.uncertainty && !camera && site.accuracyKm>0 && site.accuracyKm<=0.5 && site.locationBasis?.startsWith("По тексту ОБСЕ")){
      L.circle([site.point[1],site.point[0]],{
        radius:site.accuracyKm*1000,color:control.color,weight:1.3,
        dashArray:"3 4",opacity:.7,fillColor:control.color,fillOpacity:.11,interactive:false,
      }).addTo(leafletLayer);
    }
    if(camera)L.circleMarker([site.point[1],site.point[0]],{
      radius:radius+3,color:"#284c62",weight:1.5,fill:false,interactive:false,opacity:.8,
    }).addTo(leafletLayer);
    const marker = L.circleMarker([site.point[1],site.point[0]],{
      radius, color:selected?"#14334c":"#fff", weight:selected?3:2,
      fillColor:control.color, fillOpacity:site.rows?0.94:0.4,
    }).addTo(leafletLayer);
    marker.bindTooltip(`○ ${esc(site.name)} · ${esc(control.label)} · ${fmt(site.quantity)} по «К-во» · ${fmt(site.rows)} строк`,{sticky:true});
    marker.on("click", () => selectSite(site.id));
  }
}

function destination(lnglat, bearing, distanceKm) {
  const [lon,lat]=lnglat, a=lat*Math.PI/180, l=lon*Math.PI/180,
    br=bearing*Math.PI/180, d=distanceKm/6371;
  const phi=Math.asin(Math.sin(a)*Math.cos(d)+Math.cos(a)*Math.sin(d)*Math.cos(br));
  const lam=l+Math.atan2(Math.sin(br)*Math.sin(d)*Math.cos(a),Math.cos(d)-Math.sin(a)*Math.sin(phi));
  return [lam*180/Math.PI,phi*180/Math.PI];
}

function circlePolygon(center,radiusKm){
  const ring=[];
  for(let i=0;i<=48;i++)ring.push(destination(center,i*360/48,radiusKm));
  return ring;
}

function sectorCoordinates(e) {
  if (!e.coordinates || !e.direction) return [];
  const {near_km,far_km,bearing_deg,half_angle_deg}=e.direction;
  const pts=[];
  for(let i=0;i<=18;i++) pts.push(destination(e.coordinates,bearing_deg-half_angle_deg+i*2*half_angle_deg/18,far_km));
  for(let i=18;i>=0;i--) pts.push(destination(e.coordinates,bearing_deg-half_angle_deg+i*2*half_angle_deg/18,near_km));
  pts.push(pts[0]);
  return pts;
}

function selectIncidentGroup(group) {
  selectedId=null;activeSiteId=null;
  const card=$("#selection-card");card.hidden=false;
  const entries=[...group.events].sort(chronological);
  const territory=territoryInfo(group.territory);
  card.innerHTML=`<button type="button" class="selection-close" aria-label="Закрыть">×</button>
    <span class="item-label">◇ Группа ориентиров инцидентов</span>
    <h3>${fmt(group.quantity)} по «К-во» · ${fmt(group.events.length)} строк</h3>
    <p class="territory-readout"><span class="territory-dot" style="background:${territory.color}"></span>${esc(territory.label)}</p>
    <p>Число у ромба — сумма «К-во» этих строк; оно входит в общую сводку по цвету территории. Ромб показывает усреднённый ориентир нескольких секторов, а не установленное место происшествия. Полупрозрачные круги — модель неопределённости. Записи ниже расположены <strong>от ранних к поздним</strong>. Нажмите на запись для просмотра её собственного сектора.</p>
    ${entries.slice(0,14).map(e=>`<p><a href="#" data-event="${e.id}">${esc(dayName(e.start))} · ${esc(e.incident)} · ${e.quantity===null?"—":fmt(e.quantity)} — ${esc(e.siteName||"без точной позиции")}</a></p>`).join("")}
    ${entries.length>14?`<p>Ещё ${fmt(entries.length-14)} записей можно найти в журнале по фильтрам.</p>`:""}`;
  renderJournal();renderMap();focusCoordinates(group.point,10);
}

function selectEvent(id) {
  const e=events.find(x=>x.id===id);
  if(!e)return;
  selectedId=id;activeSiteId=null;
  const territory=territoryInfo(e.incidentTerritory);
  const control=observerInfo(e.observerControl);
  const evidence=e.territoryEvidence;
  const card=$("#selection-card");
  card.hidden=false;
  card.innerHTML=`<button type="button" class="selection-close" aria-label="Закрыть">×</button>
    <span class="item-label">${e.reportNumber?`Отчёт ОБСЕ № ${e.reportNumber}/2022 · приложение, стр. ${e.annexPage}`:`Запись № ${e.sourceRow}`} · ${esc(e.methodOriginal)}</span>
    <h3>${esc(e.incident)} · ${e.quantity===null?esc(e.quantityOriginal||"количество не указано"):fmt(e.quantity)}</h3>
    <p class="item-meta">${esc(dayName(e.start))}${e.end!==e.start?" — "+esc(dayName(e.end)):""} EET</p>
    <p class="territory-readout"><span class="territory-dot" style="background:${territory.color}"></span><strong>Район инцидента:</strong> ${esc(territory.label)}</p>
    <p><strong>Наблюдатель:</strong> ${esc(e.position)}</p>
    <p><strong>Сторона наблюдателя:</strong> ${esc(control.label)}. <span class="method-tag">${esc(e.observerControlSource||"Официальное подтверждение не установлено")}</span></p>
    ${e.observerControlCorrection?`<p class="warning-text">В таблице указано «${esc(e.control)}». <a target="_blank" rel="noopener" href="${esc(e.observerControlCorrection.url)}">Источник уточнения ОБСЕ ↗</a>: ${esc(e.observerControlCorrection.reason)}.</p>`:""}
    <p><strong>Расположение события:</strong> ${esc(e.where||"не указано")}</p>
    <p><strong>Описание:</strong> ${esc(e.description||"—")}</p>
    <p><strong>Геопривязка:</strong> ${esc(e.locationBasis)}${e.coordinates?`. Модельный радиус позиции: около ${fmt(e.accuracyKm)} км.`:`. ${esc(e.locationDiagnosis||"Точка на карте не строится")}. Точка на карте не строится.`}${e.locationReferenceUrl?` <a href="${esc(e.locationReferenceUrl)}" target="_blank" rel="noopener">Источник опорных координат ↗</a>`:""}</p>
    ${e.referenceAnchorUncertaintyKm!=null?`<p class="method-tag">Отдельная неопределённость картографического ориентира — около ${fmt(e.referenceAnchorUncertaintyKm)} км; в малый допуск патруля не включена.</p>`:""}
    ${evidence?`<p>Геометрическая доля оценочного сектора внутри контура Н: <strong>${Math.round(evidence.ngcaAreaShare*100)}%</strong>; ближайшая точка сектора к линии: ≈ ${esc(evidence.minDistanceFromContactLineKm)} км. Это <strong>не вероятность</strong> и не независимое доказательство.</p>`:""}
    ${evidence?`<p>До ближайшей границы контура от центра круга: ≈ <strong>${esc(evidence.distanceToPolygonBoundaryKm)} км</strong>. ${evidence.entireUncertaintyCircleOneSide?"Круг целиком по одну сторону исторической границы — категория «уверенно» в рамках модели.":"Круг пересекает либо касается границы — категория «уверенно» не присваивается."}</p>`:""}
    ${e.direction&&e.coordinates?`<p>Показанный сектор приблизителен и основан на диапазоне расстояний и компасном направлении из текста. Полупрозрачный круг диаметром около <strong>${(2*uncertaintyRadiusKm(e)).toLocaleString("ru-RU",{maximumFractionDigits:1})} км</strong> включает модельный запас ±15% по дальности, допуск положения наблюдателя <strong>и ошибку картографического ориентира</strong>. Ромб — геометрический ориентир, не место удара.</p>`:""}
    <a class="source-link" target="_blank" rel="noopener" href="${esc(e.annexUrl?`${e.annexUrl}#page=${e.annexPage}`:metadataUrl(e.sourceRow))}">${e.annexUrl?"Открыть официальное приложение ОБСЕ ↗":"Открыть исходную строку ↗"}</a>
    ${e.reportUrl?`<p><a target="_blank" rel="noopener" href="${esc(e.reportUrl)}">Страница ежедневного отчёта № ${e.reportNumber}/2022 ↗</a></p>`:""}`;
  renderJournal();renderMap();
  if (e.incidentPoint) focusCoordinates(e.incidentPoint,11);
  else if (e.coordinates) focusCoordinates(e.coordinates,9);
}

function selectSite(id) {
  const site=currentSites().find(s=>s.id===id);
  if(!site)return;
  selectedId=null;activeSiteId=id;
  const card=$("#selection-card");card.hidden=false;
  const slice=[...site.events].sort(chronological).slice(0,8);
  const observer=observerInfo(site.control);
  const hasReferenceControl=site.events.some(e=>e.observerControlScope==="inferred_from_reference_settlement");
  card.innerHTML=`<button type="button" class="selection-close" aria-label="Закрыть">×</button>
    <span class="item-label">${site.type==="camera"?"Камера СММ":"Позиция наблюдений · приблизительно"}</span>
    <h3>${esc(site.name)}</h3>
    <p class="item-meta">${fmt(site.quantity)} по «К-во» · ${fmt(site.rows)} строк в текущей выборке</p>
    <p class="territory-readout"><span class="territory-dot" style="background:${observer.color}"></span><strong>Сторона наблюдателя:</strong> ${esc(observer.label)}</p>
    ${hasReferenceControl?"<p>Для патруля принята сторона населённого пункта-ориентира, указанная ОБСЕ. Это рабочее допущение, а не GPS-проверка линии соприкосновения.</p>":""}
    ${site.controlConflict?`<p class="warning-text">В официальных описаниях этой позиции встречаются противоречивые статусы. Она окрашена серым, пока противоречие не разрешено.</p>`:""}
    ${site.controlCorrection?`<p class="warning-text">Статус уточнён по <a href="${esc(site.controlCorrection.url)}" target="_blank" rel="noopener">отчёту ОБСЕ ↗</a>: ${esc(site.controlCorrection.reason)}.</p>`:""}
    <p>${esc(site.locationBasis)}. Неопределённость: около ${fmt(site.accuracyKm)} км.</p>
    ${slice.length?"<p><strong>Записи по времени (сначала ранние):</strong></p>":"<p>В выбранной выборке с этой позиции нет зарегистрированных событий.</p>"}
    ${slice.map(e=>`<p><a href="#" data-event="${e.id}">${esc(dayName(e.start))} — ${esc(e.incident)} (${e.quantity===null?"—":fmt(e.quantity)})</a></p>`).join("")}
    ${site.events.length>slice.length?`<p>И ещё ${fmt(site.events.length-slice.length)} строк в журнале.</p>`:""}`;
  card.querySelectorAll("[data-event]").forEach(a=>a.addEventListener("click",ev=>ev.preventDefault()));
  renderMap();focusCoordinates(site.point,9);
}

function clearSelection(){selectedId=null;activeSiteId=null;$("#selection-card").hidden=true;renderJournal();renderMap();}

function focusCoordinates(lonlat,zoom) {
  if(mapEngine==="osm"&&leaflet){leaflet.setView([lonlat[1],lonlat[0]],Math.max(leaflet.getZoom(),zoom),{animate:true});}
  else if(yandex){try{yandex.update({location:{center:lonlat,zoom}});}catch(error){console.warn(error);}}
}

function downloadCsv() {
  const fields=["sourceKind","sourceRow","reportNumber","annexUrl","annexPage","annexRow","start","end","incident","methodOriginal","quantity","quantityOriginal","position","where","description","control","observerControl","observerControlScope","observerControlSource","observerControlCorrection","siteType","coordinates","locationReferenceUrl","accuracyKm","referenceAnchorUncertaintyKm","locationDiagnosis","uncertaintyRadiusKm","incidentPoint","incidentTerritory","territoryEvidence"];
  const csv=[fields.join(";"),...filtered.map(e=>fields.map(k=>{
    let x=e[k]??"";if(Array.isArray(x))x=x.join(",");
    else if(typeof x==="object")x=JSON.stringify(x);
    return '"'+String(x).replaceAll('"','""')+'"';
  }).join(";"))].join("\r\n");
  const blob=new Blob(["\ufeff",csv],{type:"text/csv;charset=utf-8"});
  const url=URL.createObjectURL(blob),a=document.createElement("a");
  a.href=url;a.download="OSCE_filtered_2022-02.csv";document.body.appendChild(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);
}

async function requestYandex() {
  const key=$("#yandex-key").value.trim();
  const status=$("#map-api-status");
  if(!key){status.textContent="Для JS API 3.0 нужен ключ Яндекс Карт.";return;}
  status.textContent="Загружаю API Яндекс Карт…";
  try {
    if(!window.ymaps3){
      const script=document.createElement("script");
      script.src=`https://api-maps.yandex.ru/v3/?apikey=${encodeURIComponent(key)}&lang=ru_RU`;
      script.async=true;
      await new Promise((resolve,reject)=>{script.onload=resolve;script.onerror=()=>reject(new Error("API не загрузился. Проверьте ключ и HTTP Referer."));document.head.appendChild(script);});
    }
    await window.ymaps3.ready;
    sessionStorage.setItem("osce_yandex_js_key",key);
    if(leaflet){leaflet.remove();leaflet=null;leafletLayer=null;}
    $("#map").innerHTML="";
    const Y=window.ymaps3;
    yandex=new Y.YMap($("#map"),{location:{center:[38.45,48.05],zoom:8},mode:"vector",showScaleInCopyrights:true});
    yandex.addChild(new Y.YMapDefaultSchemeLayer());
    yandex.addChild(new Y.YMapDefaultFeaturesLayer());
    mapEngine="yandex";
    $("#basemap-label").textContent="Яндекс";
    $("#map-control-note").textContent="Современная подложка Яндекс Карт. Исторические слои и наблюдения — отдельные данные.";
    $("#map-unavailable").hidden=true;
    renderYandex();
    status.textContent="Карта Яндекса подключена.";
    $("#settings").close();
  } catch(error) {
    console.error(error);status.textContent=error.message;
    switchToLeaflet();
  }
}

function switchToLeaflet() {
  if(yandex){try{yandex.destroy?.();}catch(e){console.warn(e);}yandex=null;yandexChildren=[];}
  if(leaflet){mapEngine="osm";$("#basemap-label").textContent="OSM";renderLeaflet();return;}
  initLeaflet();
}

function renderYandex() {
  if(!yandex)return;
  const Y=window.ymaps3;
  for(const entity of yandexChildren){try{yandex.removeChild(entity);}catch(e){console.warn(e);}}
  yandexChildren=[];
  const add=(entity)=>{yandex.addChild(entity);yandexChildren.push(entity);};
  try {
    if(state.area) add(new Y.YMapFeature({geometry:geography.territory.geometry,style:{stroke:[{color:"#c89743",width:1}],fill:"#e6a74e29"}}));
    if(state.line) add(new Y.YMapFeature({geometry:geography.contactLine.geometry,style:{stroke:[{color:"#ae473d",width:3}]}}));
    if(state.schematic) add(new Y.YMapFeature({geometry:geography.osceSchematic.geometry,style:{stroke:[{color:"#465c76",width:2.5}]}}));
    for(const zone of currentUncertaintyAreas()){
      const color=territoryInfo(zone.territory).color;
      add(new Y.YMapFeature({
        geometry:{type:"Polygon",coordinates:[circlePolygon(zone.point,zone.radiusKm)]},
        style:{stroke:[{color:color+"99",width:1.35}],fill:color+"14"},
      }));
    }
    const event=events.find(e=>e.id===selectedId);
    if(state.incidents&&event?.direction&&event.coordinates){
      add(new Y.YMapFeature({geometry:{type:"Polygon",coordinates:[sectorCoordinates(event)]},style:{stroke:[{color:"#183e55",width:2}],fill:"#edb46455"}}));
      if(event.incidentPoint)add(new Y.YMapFeature({geometry:{type:"LineString",coordinates:[event.coordinates,event.incidentPoint]},style:{stroke:[{color:"#516a7a",width:1.5}]}}));
    }
    for(const group of currentIncidentGroups()){
      const el=document.createElement("div");el.className="yandex-incident-wrapper";
      el.innerHTML=markerHtml(group);
      el.querySelector("button").addEventListener("click",()=>group.events.length===1?selectEvent(group.events[0].id):selectIncidentGroup(group));
      add(new Y.YMapMarker({coordinates:group.point},el));
    }
    for(const site of currentSites()) {
      const el=document.createElement("button");el.type="button";
      const camera=site.type==="camera";
      const size=Math.round(13+Math.min(18,Math.sqrt(site.rows)*2));
      const observer=observerInfo(site.control);
      if(state.uncertainty && !camera && site.accuracyKm>0 && site.accuracyKm<=0.5 && site.locationBasis?.startsWith("По тексту ОБСЕ")){
        add(new Y.YMapFeature({geometry:{type:"Polygon",coordinates:[circlePolygon(site.point,site.accuracyKm)]},
          style:{stroke:[{color:observer.color+"aa",width:1.2}],fill:observer.color+"1a"}}));
      }
      el.style.cssText=`width:${size}px;height:${size}px;border-radius:50%;background:${observer.color};opacity:${site.rows?1:0.45};border:${camera?"3px double":"2px solid"} #fff;${camera?"outline:1px solid #284c62;":""}box-shadow:0 2px 8px #12273860;cursor:pointer`;
      el.title=`○ ${site.name} — ${observer.label} — ${site.rows} строк`;
      el.addEventListener("click",()=>selectSite(site.id));
      add(new Y.YMapMarker({coordinates:site.point},el));
    }
  } catch(error) {console.error("Yandex drawing error",error);$("#map-api-status").textContent="Не удалось отобразить один из объектов: "+error.message;}
}

load();
