/* Next Stop frontend. Talks only to this server's /api endpoints. */
(function () {
  "use strict";

  const STATION_KEY = "metra.station";
  const THEME_KEY = "metra.theme";
  const DEST_KEY = "metra.dest";
  const TAB_KEY = "metra.tab";
  const VIEW_KEY = "metra.view";
  const DISMISSED_KEY = "metra.alerts.dismissed";

  const state = {
    tz: "America/Chicago",
    interval: 30,
    stations: [],
    stationById: new Map(),
    station: localStorage.getItem(STATION_KEY),
    dest: localStorage.getItem(DEST_KEY),
    home: { home_station: null, home_dest: null },
    departures: null,
    status: null,
  };

  const $ = (id) => document.getElementById(id);

  // ---------- motion: one set of helpers so everything moves the same way ----------
  const reduceMotion = () => matchMedia("(prefers-reduced-motion: reduce)").matches;
  // Show an element with its enter animation (see .anim-enter in style.css).
  function enter(el) {
    if (!el) return;
    const wasHidden = el.hidden;
    const wasLeaving = el.classList.contains("anim-leave");
    el._animToken = (el._animToken || 0) + 1;  // cancels a leave() still in progress
    el.hidden = false;
    el.classList.remove("anim-leave");
    if (!wasHidden && !wasLeaving) return;
    if (reduceMotion()) return;
    el.classList.remove("anim-enter");
    void el.offsetWidth;  // restart the animation
    el.classList.add("anim-enter");
    el.addEventListener("animationend", () => el.classList.remove("anim-enter"), { once: true });
  }
  // Hide an element after its leave animation; `done` runs once it's hidden.
  function leave(el, done) {
    if (!el) return;
    if (el.hidden || reduceMotion()) { el.hidden = true; if (done) done(); return; }
    let finished = false;
    const token = el._animToken = (el._animToken || 0) + 1;
    const fin = () => {
      if (finished || el._animToken !== token) return;  // re-opened meanwhile
      finished = true;
      el.classList.remove("anim-leave");
      el.hidden = true;
      if (done) done();
    };
    el.classList.remove("anim-enter");
    el.classList.add("anim-leave");
    el.addEventListener("animationend", fin, { once: true });
    setTimeout(fin, 320);  // in case the animation never runs
  }
  // Fade/slide a list's items in one after another (only when the view changes).
  function staggerIn(container) {
    if (!container || reduceMotion()) return;
    [...container.children].forEach((c, i) => c.style.setProperty("--i", String(Math.min(i, 10))));
    container.classList.remove("list-enter");
    void container.offsetWidth;
    container.classList.add("list-enter");
    clearTimeout(container._animT);
    container._animT = setTimeout(() => container.classList.remove("list-enter"), 700);
  }
  // A short message at the bottom of the screen.
  function toast(text) {
    let el = document.getElementById("toast");
    if (!el) {
      el = document.createElement("div");
      el.id = "toast"; el.className = "toast"; el.setAttribute("role", "status"); el.hidden = true;
      document.body.appendChild(el);
    }
    el.textContent = text;
    enter(el);
    clearTimeout(el._t);
    el._t = setTimeout(() => leave(el), 4000);
  }
  function fadeIn(el) {
    if (!el || reduceMotion()) return;
    el.classList.remove("fade-in");
    void el.offsetWidth;
    el.classList.add("fade-in");
    el.addEventListener("animationend", () => el.classList.remove("fade-in"), { once: true });
  }
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  async function api(path) {
    const r = await fetch(path, { cache: "no-store" });
    if (!r.ok) throw new Error(`${path}: HTTP ${r.status}`);
    const data = await r.json();
    // Countdowns use the server's clock, not the phone's (which may be off).
    if (data && typeof data.generated_at === "number") {
      state.clockOffset = data.generated_at - Date.now() / 1000;
    }
    return data;
  }
  const serverNow = () => Date.now() / 1000 + (state.clockOffset || 0);

  // ---------- Metra data statement (license: shown with the data) ----------
  // "When Metra last updated this data" + "not sponsored, affiliated, or operated by Metra".
  let dateTimeFmt = null;
  function fmtDateTime(epoch) {
    if (!dateTimeFmt) dateTimeFmt = new Intl.DateTimeFormat([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit", timeZone: state.tz });
    return dateTimeFmt.format(new Date(epoch * 1000));
  }
  const DISCLAIMER = "Not sponsored, affiliated, or operated by Metra.";
  function dataStatement(asOf, what) {
    const a = asOf || {};
    const when = a.realtime_updated ? `Metra ${what} data last updated ${fmtDateTime(a.realtime_updated)}.`
      : a.schedule_published ? `Metra schedule data published ${a.schedule_published}.`
      : "Data from Metra.";
    return `${when} Not sponsored, affiliated, or operated by Metra.`;
  }

  // ---------- formatting ----------
  let timeFmt = null;
  function fmtTime(epoch) {
    if (!timeFmt) timeFmt = new Intl.DateTimeFormat([], { hour: "numeric", minute: "2-digit", timeZone: state.tz });
    return timeFmt.format(new Date(epoch * 1000));
  }
  // "10:26 AM" with a smaller AM/PM, kept on one line.
  function bigTime(epoch) {
    const t = fmtTime(epoch);
    const m = t.match(/^(.*?)\s*([AaPp]\.?\s?[Mm]\.?)$/);
    return m ? `${esc(m[1])}<span class="ampm">${esc(m[2])}</span>` : esc(t);
  }
  // Metra counts a train as on time if it's less than 6 minutes late:
  // amber for 1-5 min, red from 6.
  function delayHtml(delay, approx) {
    if (delay == null) return "";
    const m = Math.round(delay / 60);
    const pre = approx ? "~" : "";
    if (m >= 6) return `<span class="delay late">${pre}${m} min late</span>`;
    if (m >= 1) return `<span class="delay minor">${pre}${m} min late</span>`;
    if (m <= -1) return `<span class="delay early">${-m} min early</span>`;
    return `<span class="delay ontime">On time</span>`;
  }
  function chip(short, color, text) {
    return `<span class="chip" style="background:${esc(color)};color:${esc(text)}">${esc(short)}</span>`;
  }
  function ago(epoch) {
    const s = Math.max(0, Math.round(serverNow() - epoch));
    return s < 60 ? `${s}s ago` : `${Math.round(s / 60)} min ago`;
  }

  // ---------- theme ----------
  $("theme-toggle").addEventListener("click", () => {
    const next = document.documentElement.dataset.theme === "light" ? "dark" : "light";
    const root = document.documentElement;
    if (!reduceMotion()) {
      root.classList.add("theme-anim");
      clearTimeout(root._themeT);
      root._themeT = setTimeout(() => root.classList.remove("theme-anim"), 450);
    }
    root.dataset.theme = next;
    localStorage.setItem(THEME_KEY, next);
    document.querySelector('meta[name="theme-color"]').content = next === "light" ? "#ffffff" : "#0f1216";
    mapView.onThemeChange();
  });

  // ---------- map ----------
  function trainPopup(t) {
    const title = `${chip(t.route_short || "?", t.color, t.text_color)} ${esc(t.route_name || "Metra")}`;
    const rows = [];
    if (t.train) rows.push(["Train", esc(t.train)]);
    rows.push(["To", esc(t.destination || "—")]);
    rows.push([t.stopped ? "At" : "Next", esc(t.next_stop || "—")]);
    const status = !t.live ? '<span class="muted">Estimated from schedule</span>'
      : t.tracking ? `${delayHtml(t.delay, true) || "On time"} <span class="muted">(from GPS)</span>`
      : (delayHtml(t.delay) || "Live");
    rows.push(["Status", status]);
    if (t.delay_reason) rows.push(["Reason", esc(t.delay_reason)]);
    if (t.timestamp) rows.push(["Updated", ago(t.timestamp)]);
    if (t.snapped_m >= 5) rows.push(["Map spot", `<span class="muted">Drawn on the track line. Metra's GPS put it ${t.snapped_m} m away.</span>`]);
    // Is this train on the board you're looking at?
    const d = state.departures;
    const row = d && t.trip_id && (d.departures || []).find((r) => r.trip_id === t.trip_id);
    const from = d && d.station ? d.station.name : "";
    const foot = row
      ? `<div class="popup-board"><span>Leaves ${esc(from)} <b>${esc(fmtTime(row.time))}</b></span></div>`
      : (from ? `<div class="popup-board muted">Not in the ${esc(from)} list</div>` : "");
    return `<div class="popup"><h3>${title}</h3><dl>${rows.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join("")}</dl>${foot}</div>`;
  }

  const mapView = MetraMap.create({
    container: $("map"),
    onStationClick: (id) => selectStation(id, { pan: false }),
    popupHtml: trainPopup,
    onLineFocus: (rid) => { renderMapHead(rid); refreshTrains(); },
    // A train tapped on the map: highlight it on the board too.
    onTrainSelect: (t) => {
      state.selectedTrip = t ? t.trip_id : null;
      // Highlight its row; on a computer also scroll the board to it (on a phone that would leave the map).
      markBoardSelection(!narrow());
    },
  });

  // ---------- Map tab: header with line chips, and a card for the focused line ----------
  function mapRoutes() {
    const routes = ((state.status && state.status.routes) || []).slice();
    return routes.sort((a, b) => String(a.short_name).localeCompare(String(b.short_name)));
  }
  // Let the map's buttons and notes sit clear of the header and the line card.
  function measureMapOverlays() {
    const wrap = document.querySelector(".map-wrap");
    const head = $("map-head"), card = $("line-card");
    if (!head.hidden) wrap.style.setProperty("--map-head-h", `${head.offsetHeight + 4}px`);
    if (!card.hidden) wrap.style.setProperty("--line-card-h", `${card.offsetHeight + 12}px`);
  }
  window.addEventListener("resize", () => requestAnimationFrame(measureMapOverlays));
  function renderMapHead(focus = mapView.lineFocus) {
    const routes = mapRoutes();
    const chip = (id, label, color) => `<button type="button" class="line-chip${(focus || null) === id ? " on" : ""}" data-route="${esc(id || "")}" role="tab" aria-selected="${(focus || null) === id}">${color ? `<i style="background:${esc(color)}"></i>` : ""}${esc(label)}</button>`;
    $("line-chips").innerHTML = chip(null, "All", null) + routes.map((r) => chip(r.id, r.short_name, r.color)).join("");
    const on = $("line-chips").querySelector(".line-chip.on");
    if (on && on.scrollIntoView) on.scrollIntoView({ block: "nearest", inline: "nearest" });
    renderLineCard(focus);
  }
  function renderLineCard(focus) {
    const card = $("line-card");
    const r = focus && mapRoutes().find((x) => x.id === focus);
    document.querySelector(".map-wrap").classList.toggle("has-line-card", !!r);
    if (!r) { if (!card.hidden) leave(card); return; }
    const nStations = state.stations.filter((s) => s.routes.includes(r.id)).length;
    const trains = ((mapView.lastTrains && mapView.lastTrains.trains) || []).filter((t) => t.route_id === r.id);
    const late = trains.filter((t) => t.delay >= 360).length;
    $("line-card-top").innerHTML = `${chip(r.short_name, r.color, r.text_color)}<b>${esc(r.long_name || r.short_name)}</b>`;
    $("line-card-info").textContent = `${nStations} stations · ${trains.length} train${trains.length === 1 ? "" : "s"} on the map`
      + (late ? ` · ${late} running 6+ min late` : "") + ". Tap a station to see its trains.";
    if (card.hidden) enter(card);
    requestAnimationFrame(measureMapOverlays);
  }
  $("line-chips").addEventListener("click", (e) => {
    const b = e.target.closest(".line-chip");
    if (b) mapView.setLineFocus(b.dataset.route || null);
  });
  $("line-card-x").addEventListener("click", () => mapView.setLineFocus(null));
  // Tap the line card: open the Timetable tab for this line, direction by time of day.
  state.ttLine = null;  // {route, direction} while the Timetable shows a whole line
  function defaultDirection() {
    // Toward downtown before the commute switch time (or noon), away from downtown after.
    const sw = (state.home && state.home.switch_time) || "12:00";
    const hhmm = new Intl.DateTimeFormat("en-GB", { hour: "2-digit", minute: "2-digit", hour12: false, timeZone: state.tz })
      .format(new Date(serverNow() * 1000));
    return hhmm < sw ? "inbound" : "outbound";
  }
  $("line-card-open").addEventListener("click", () => {
    const rid = mapView.lineFocus;
    if (!rid) return;
    state.ttLine = { route: rid, direction: defaultDirection() };
    if (state.tab !== "timetable") $("tab-timetable").click();
    else { state.departures = null; refreshDepartures(); }
    updateClear();
  });

  // ---------- train selection (board <-> map) ----------
  function markBoardSelection(scroll) {
    let hit = null;
    document.querySelectorAll("#board-list .dep[data-trip]").forEach((li) => {
      const on = !!state.selectedTrip && li.dataset.trip === state.selectedTrip;
      li.classList.toggle("selected", on);
      li.setAttribute("aria-pressed", String(on));
      if (on) hit = li;
    });
    if (hit && scroll) hit.scrollIntoView({ behavior: "smooth", block: "center" });
    updateClear();
    return hit;
  }
  const narrow = () => matchMedia("(max-width: 820px)").matches;
  function pickBoardTrain(li) {
    if (mapHidden()) return;
    const trip = li.dataset.trip;
    if (state.selectedTrip === trip) {  // tap again to unselect
      state.selectedTrip = null;
      mapView.clearSelection(false);
      markBoardSelection(false);
      return;
    }
    state.selectedTrip = trip;
    markBoardSelection(false);
    const onMap = mapView.selectTrip(trip, li.dataset.route);
    if (!onMap) {
      const note = $("map-note");
      note.textContent = "That train isn't running yet; its line is highlighted";
      note.hidden = false;
    }
    // On phones the map is below the board: bring it into view.
    if (narrow()) $("map").scrollIntoView({ behavior: "smooth", block: "center" });
  }
  $("board-list").addEventListener("click", (e) => {
    if (e.target.closest("a, button")) return;
    const li = e.target.closest(".dep[data-trip]");
    if (li) pickBoardTrain(li);
  });
  $("board-list").addEventListener("keydown", (e) => {
    if (e.key !== "Enter" && e.key !== " ") return;
    const li = e.target.closest(".dep[data-trip]");
    if (li) { e.preventDefault(); pickBoardTrain(li); }
  });

  async function refreshTrains() {
    if (mapHidden()) return;  // timetable only: no train positions needed
    let data;
    // Only trains still heading to your "Going to" stop, unless you chose all trains.
    const dest = view.mapTrains !== "all" && state.dest && state.stationById.get(state.dest);
    const from = dest && state.station ? `&from=${encodeURIComponent(state.station)}` : "";
    try { data = await api(dest ? `/api/trains?to=${encodeURIComponent(state.dest)}${from}` : "/api/trains"); } catch (e) { console.warn(e); return; }
    mapView.updateTrains(data);
    $("map-data").textContent = DISCLAIMER;
    if (mapView.lineFocus) renderLineCard(mapView.lineFocus);
    const asOf = data.data_as_of || {};
    const when = asOf.realtime_updated ? ` · ${fmtDateTime(asOf.realtime_updated)}` : "";
    $("map-data").hidden = false;
    const focus = mapView.lineFocus;
    const n = focus ? data.trains.filter((t) => t.route_id === focus).length : data.trains.length;
    const focusName = focus ? `${(mapRoutes().find((r) => r.id === focus) || {}).short_name || focus} ` : "";
    const note = $("map-note");
    note.textContent = `${n} ${focusName}train${n === 1 ? "" : "s"}${dest ? ` to ${dest.name}` : ""} · ${data.source === "schedule" ? "positions estimated from schedule" : "live positions"}${when}`;
    note.hidden = false;
  }

  // ---------- station pickers ----------
  function routeChips(ids) {
    const routes = (state.status && state.status.routes) || [];
    return ids.map((id) => {
      const r = routes.find((x) => x.id === id);
      return r ? chip(r.short_name, r.color, r.text_color) : chip(id, "#888", "#fff");
    }).join("");
  }

  // "upn", "UP-N" and "up n" all match the UP-N line.
  const norm = (x) => String(x).toLowerCase().replace(/[^a-z0-9]/g, "");

  function stationMatches(q) {
    const raw = q.trim().toLowerCase();
    const nq = norm(q);
    if (!nq) return state.stations.slice();
    const routes = (state.status && state.status.routes) || [];
    const lineIds = new Set(routes
      .filter((r) => norm(r.id) === nq || norm(r.short_name) === nq || norm(r.long_name).startsWith(nq))
      .map((r) => r.id));
    const scored = [];
    for (const s of state.stations) {
      const name = s.name.toLowerCase();
      let score = -1;
      if (name.startsWith(raw)) score = 0;
      else if (name.includes(raw) || norm(s.name).includes(nq) || norm(s.id).includes(nq)) score = 1;
      else if (s.routes.some((r) => lineIds.has(r))) score = 2;
      if (score >= 0) scored.push([score, s]);
    }
    scored.sort((a, b) => a[0] - b[0] || a[1].name.localeCompare(b[1].name));
    return scored.map((x) => x[1]);
  }

  // Going-to suggestions: only stations with a direct train from "From",
  // grouped by direction and nearest first. Loaded once per From station.
  state.dests = {};
  const destsLoading = {};
  function loadDests(id) {
    if (!id || state.dests[id]) return Promise.resolve();
    if (!destsLoading[id]) {
      destsLoading[id] = api(`/api/destinations?station=${encodeURIComponent(id)}`)
        .then((list) => { state.dests[id] = list; })
        .catch((e) => { console.warn(e); state.dests[id] = "error"; })
        .finally(() => { delete destsLoading[id]; });
    }
    return destsLoading[id];
  }
  function destSuggestions(q) {
    const list = state.station && state.dests[state.station];
    if (list === "error") return stationMatches(q);  // couldn't load: don't block picking
    if (!list) {
      if (!state.station) return stationMatches(q);
      // Not loaded yet: wait for it rather than offering every station.
      loadDests(state.station).then(() => toPicker.refresh());
      return [{ header: "Loading stations…" }];
    }
    const reach = new Map(list.map((d) => [d.id, d]));
    const withHint = (st) => ({ ...st, hint: `${reach.get(st.id).minutes} min` });
    const from = state.stationById.get(state.station);
    if (!q.trim()) {
      const out = [];
      for (const [dir, label] of [["inbound", "Toward downtown"], ["outbound", "Away from downtown"], [null, "Other direct trains"]]) {
        const group = list.filter((d) => (d.direction || null) === dir && state.stationById.has(d.id));
        if (!group.length) continue;
        out.push({ header: label });
        group.forEach((d) => out.push(withHint(state.stationById.get(d.id))));
      }
      return out.length ? out : [{ header: `No direct trains from ${from ? from.name : "here"}` }];
    }
    // Typing: only stations with a direct train can be picked. Others that match are
    // shown greyed out with their line, so it's clear why they aren't offered.
    const all = stationMatches(q).filter((st) => st.id !== state.station);
    const direct = all.filter((st) => reach.has(st.id)).map(withHint);
    const other = all.filter((st) => !reach.has(st.id)).slice(0, 8)
      .map((st) => ({ ...st, disabled: true, hint: "no direct train" }));
    return direct.concat(other.length ? [{ header: `No direct train from ${from ? from.name : "here"}` }, ...other] : []);
  }

  function makePicker({ inputId, listId, getValue, onSelect, allowClear, clearLabel = "Any destination", suggest = stationMatches }) {
    const input = $(inputId);
    const list = $(listId);
    let matches = [];
    let active = -1;
    const current = () => { const st = state.stationById.get(getValue()); return st ? st.name : ""; };

    function open(isOpen) {
      if (isOpen) enter(list); else list.hidden = true;
      input.setAttribute("aria-expanded", String(isOpen));
    }
    function render() {
      const items = suggest(input.value).slice(0, 120);
      matches = items.filter((x) => !x.header && !x.disabled);
      active = matches.length ? 0 : -1;
      const clear = allowClear && getValue() ? `<li role="option" data-id="" class="clear-opt">${esc(clearLabel)}</li>` : "";
      let i = -1;
      list.innerHTML = clear + (items.length
        ? items.map((st) => {
          if (st.header) return `<li class="group" role="presentation">${esc(st.header)}</li>`;
          if (st.disabled) return `<li class="disabled" aria-disabled="true"><span>${esc(st.name)} <small class="muted">${esc(st.hint || "")}</small></span><span class="chips">${routeChips(st.routes)}</span></li>`;
          i += 1;
          return `<li role="option" id="${listId}-${i}" data-id="${esc(st.id)}" aria-selected="${i === active}"><span>${esc(st.name)}${st.hint ? ` <small class="muted">${esc(st.hint)}</small>` : ""}</span><span class="chips">${routeChips(st.routes)}</span></li>`;
        }).join("")
        : '<li class="empty">No matching stations</li>');
      open(true);
    }
    function setActive(i) {
      if (!matches.length) return;
      active = (i + matches.length) % matches.length;
      list.querySelectorAll("li[id]").forEach((li) => li.setAttribute("aria-selected", String(li.id === `${listId}-${active}`)));
      const el = $(`${listId}-${active}`);
      if (el) el.scrollIntoView({ block: "nearest" });
      input.setAttribute("aria-activedescendant", `${listId}-${active}`);
    }
    function choose(id) {
      open(false);
      input.blur();
      onSelect(id);
      input.value = current();
    }

    input.addEventListener("focus", () => { input.select(); render(); });
    input.addEventListener("input", render);
    input.addEventListener("search", () => { if (allowClear && !input.value) choose(""); });
    input.addEventListener("keydown", (e) => {
      if (e.key === "ArrowDown") { e.preventDefault(); if (list.hidden) render(); else setActive(active + 1); }
      else if (e.key === "ArrowUp") { e.preventDefault(); setActive(active - 1); }
      else if (e.key === "Enter") {
        e.preventDefault();
        if (allowClear && !input.value.trim()) choose("");
        else if (matches[active]) choose(matches[active].id);
      }
      else if (e.key === "Escape") { open(false); input.value = current(); input.blur(); }
    });
    list.addEventListener("mousedown", (e) => {
      const li = e.target.closest("li[data-id]");
      if (li) { e.preventDefault(); choose(li.dataset.id); }
    });
    input.addEventListener("blur", () => setTimeout(() => { open(false); input.value = current(); }, 120));
    return {
      sync: () => { input.value = current(); },
      open: () => input.focus(),
      refresh: () => { if (!list.hidden && document.activeElement === input) render(); },
    };
  }

  const fromPicker = makePicker({
    inputId: "station-input", listId: "station-list",
    getValue: () => state.station,
    onSelect: (id) => selectStation(id),
  });
  const toPicker = makePicker({
    inputId: "dest-input", listId: "dest-list", allowClear: true, suggest: destSuggestions,
    getValue: () => state.dest,
    onSelect: (id) => selectDest(id),
  });

  $("board-swap").addEventListener("click", () => $("swap-btn").click());

  // "Clear": drop the Going-to station and any selected train, and show every train.
  // Clear everything: From, Going to, any line view and selected train. The map
  // shows every train; "To home"/"To work" brings the commute back.
  function clearView() {
    state.selectedTrip = null;
    state.line = null;
    state.ttLine = null;
    mapView.clearSelection(false);
    mapView.closePopups();
    state.station = null;
    localStorage.removeItem(STATION_KEY);
    fromPicker.sync();
    selectDest("");  // also refreshes the board and the map's trains
    if (view.pickers === "hide") { pickersOpen = true; applyView(); }  // so a station can be picked
    mapView.fitLines();
  }
  function updateClear() {
    $("clear-btn").hidden = !(state.station || state.dest || state.selectedTrip || state.line || state.ttLine);
    const chip = $("map-clear");
    const dest = state.dest && state.stationById.get(state.dest);
    if (state.tab === "map" && dest) { if (chip.hidden) enter(chip); } else chip.hidden = true;
    if (dest) chip.innerHTML = `Trains to <b>${esc(dest.name)}</b> <span>· Show all ✕</span>`;
  }
  $("clear-btn").addEventListener("click", clearView);
  $("map-clear").addEventListener("click", clearView);
  // Tap a station name in the board title: centre the map on it.
  $("board-title").addEventListener("click", (e) => {
    const b = e.target.closest(".title-stn");
    const st = b && state.stationById.get(b.dataset.stn);
    if (!st) return;
    mapView.flyTo(st.lat, st.lon);
    if (narrow()) $("map").scrollIntoView({ behavior: "smooth", block: "center" });
  });
  $("swap-btn").addEventListener("click", () => {
    if (!state.station || !state.dest) return;
    const from = state.station;
    state.station = state.dest;
    state.dest = from;
    loadDests(state.station);
    localStorage.setItem(STATION_KEY, state.station);
    localStorage.setItem(DEST_KEY, state.dest);
    fromPicker.sync();
    toPicker.sync();
    updateHomeUi();
    refreshDepartures();
    refreshTrains();
  });

  function selectStation(id, opts = {}) {
    const st = state.stationById.get(id);
    if (!st) return;
    state.station = id;
    state.line = null;
    state.ttLine = null;
    localStorage.setItem(STATION_KEY, id);
    loadDests(id).then(() => {
      if (state.station !== id) return;
      // A "Going to" with no direct train from the new From station can't show anything: drop it.
      const reach = state.dests[id];
      if (state.dest && Array.isArray(reach) && !reach.some((x) => x.id === state.dest)) {
        const gone = state.stationById.get(state.dest);
        selectDest("");
        toast(`No direct train from ${st.name} to ${gone ? gone.name : "that station"}, so "Going to" was cleared.`);
      } else renderBoard();
    });
    if (state.dest === id) selectDest("", { refresh: false });
    fromPicker.sync();
    $("board-title").textContent = st.name;
    updateHomeUi();
    renderAlerts();
    if (opts.pan !== false) mapView.flyTo(st.lat, st.lon);
    refreshDepartures();
    refreshTrains();
  }

  function selectDest(id, opts = {}) {
    state.dest = id && state.stationById.has(id) ? id : null;
    if (state.dest) state.line = null;
    if (state.dest) localStorage.setItem(DEST_KEY, state.dest); else localStorage.removeItem(DEST_KEY);
    toPicker.sync();
    updateHomeUi();
    renderAlerts();
    if (opts.refresh !== false) { refreshDepartures(); refreshTrains(); }
  }

  // ---------- home / work / commute ----------
  // state.home is the server's commute profile: home_station, home_dest,
  // work_station, switch_time, work_days, source/work_source ("app"/"env"),
  // and leg = {leg: "to_work"|"to_home"|"home", from, to} for right now.
  const viewIs = (from, to) => !!from && state.station === from && (state.dest || null) === (to || null);
  const isHome = () => viewIs(state.home.home_station, state.home.home_dest);
  const isWork = () => !!state.home.work_station && state.station === state.home.work_station;
  const onLeg = () => !!state.home.leg && viewIs(state.home.leg.from, state.home.leg.to);

  // Tag shown next to the board title for the commute views.
  function viewTag() {
    const p = state.home;
    if (viewIs(p.home_station, p.work_station) && p.work_station) return "TO WORK";
    if (viewIs(p.work_station, p.home_station) && p.work_station) return "TO HOME";
    if (isHome()) return "HOME";
    if (isWork()) return "WORK";
    return "";
  }

  // Home / work / current-station markers on the map.
  function syncMapPlaces() {
    const pick = (id) => {
      const st = id && state.stationById.get(id);
      return st ? { id: st.id, name: st.name, lat: st.lat, lon: st.lon } : null;
    };
    return mapView.setPlaces({
      home: pick(state.home.home_station),
      work: pick(state.home.work_station),
      current: pick(state.station),
    });
  }

  function updateHomeUi() {
    if (state.stations.length) syncMapPlaces();
    const p = state.home;
    const homeBtn = $("home-btn"), workBtn = $("work-btn"), go = $("go-home-btn");
    const atHome = !!p.home_station && state.station === p.home_station;
    // One station can't be both: at your home only "Home" shows, at work only "Work".
    homeBtn.hidden = !state.station || isWork();
    workBtn.hidden = !state.station || atHome;
    $("board-swap").hidden = !(state.station && state.dest) || (state.tab === "timetable" && !!state.ttLine);
    updateClear();
    homeBtn.classList.toggle("is-home", atHome);
    $("home-btn-text").textContent = atHome ? "Home" : "Set as home";
    homeBtn.title = atHome ? "Your home station" : "Make this your home station (opens here on every device)";
    workBtn.classList.toggle("is-home", isWork());
    $("work-btn-text").textContent = isWork() ? "Work" : "Set as work";
    workBtn.title = isWork() ? "Your work station" : "Make this your work station";
    // The quick button jumps to the right trip for the time of day.
    const leg = p.leg;
    go.hidden = !leg || onLeg();
    $("go-home-text").textContent = !leg ? "Home" : leg.leg === "to_work" ? "To work" : leg.leg === "to_home" ? "To home" : "Home";
  }

  async function saveProfile(values, what) {
    try {
      const r = await fetch("/api/settings", {
        method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(values),
      });
      if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || `HTTP ${r.status}`);
      state.home = await r.json();
      if (!$("settings-page").hidden) fillSettings();
    } catch (e) {
      console.warn(e);
      alert(`Couldn't save the ${what}: ${e.message}`);
    }
    updateHomeUi();
    renderBoard();
    renderAlerts();
  }

  const envNote = (name) => `This comes from the ${name} setting on the server (Docker/Unraid). Change it there, or set a different one here to override it.`;

  $("home-btn").addEventListener("click", () => {
    const p = state.home;
    if (p.home_station && state.station === p.home_station) {
      if (p.source === "env") alert(envNote("HOME_STATION"));
      else if (confirm("Remove your home station?")) saveProfile({ home_station: null, home_dest: null }, "home station");
    } else {
      // Keep a "Going to" as the home destination only when no work station is set.
      saveProfile({ home_station: state.station, home_dest: p.work_station ? null : (state.dest || null) }, "home station");
    }
  });

  $("work-btn").addEventListener("click", () => {
    const p = state.home;
    if (isWork()) {
      if (p.work_source === "env") alert(envNote("WORK_STATION"));
      else if (confirm("Remove your work station?")) saveProfile({ work_station: null }, "work station");
    } else {
      saveProfile({ work_station: state.station }, "work station");
    }
  });

  function goToLeg() {
    const leg = state.home.leg;
    if (!leg) return;
    if (pickersOpen && view.pickers === "hide") { pickersOpen = false; applyView(); }  // opened by Clear
    selectDest(leg.to || "", { refresh: false });
    selectStation(leg.from);
  }
  $("go-home-btn").addEventListener("click", goToLeg);

  // ---------- commute settings panel ----------
  // Works whether or not anything is configured: unset stations show
  // "Not set", time and days show their defaults. Values that come from the
  // Docker/Unraid settings are labelled, and only sent back if changed.
  const DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
  // Typical times to start looking at trains home; any other time can be typed in.
  const TIME_PRESETS = [["12:00", "Noon"], ["14:00", "2 PM"], ["15:00", "3 PM"], ["16:00", "4 PM"],
    ["17:00", "5 PM"], ["18:00", "6 PM"], ["19:00", "7 PM"]];
  const draft = { home: null, work: null };
  const homePick = makePicker({
    inputId: "cm-home", listId: "cm-home-list", allowClear: true, clearLabel: "Not set",
    getValue: () => draft.home, onSelect: (id) => { draft.home = id || null; syncDraft(); },
  });
  const workPick = makePicker({
    inputId: "cm-work", listId: "cm-work-list", allowClear: true, clearLabel: "No work station",
    getValue: () => draft.work, onSelect: (id) => { draft.work = id || null; syncDraft(); },
  });
  $("cm-days").innerHTML = DAYS.map((d, i) =>
    `<label class="day"><input type="checkbox" name="day" value="${i}"><span>${d}</span></label>`).join("");
  $("cm-time-presets").innerHTML = TIME_PRESETS.map(([t, label]) =>
    `<button type="button" class="chip-btn" data-time="${t}">${label}</button>`).join("");
  $("cm-time-presets").addEventListener("click", (e) => {
    const b = e.target.closest("[data-time]");
    if (b) { $("cm-time").value = b.dataset.time; syncDraft(); }
  });
  $("cm-time").addEventListener("input", syncDraft);

  function syncDraft() {
    homePick.sync();
    workPick.sync();
    const hasWork = !!draft.work;
    $("cm-work-fields").hidden = !hasWork;
    $("cm-work-note").hidden = hasWork;
    const t = $("cm-time").value;
    document.querySelectorAll("#cm-time-presets [data-time]").forEach((b) => b.classList.toggle("on", b.dataset.time === t));
  }

  function openCommute() {
    const p = state.home;
    draft.home = p.home_station || null;
    draft.work = p.work_station || null;
    const src = (s, name) => s === "env" ? `from ${name}` : "";
    $("cm-home-src").textContent = src(p.source, "HOME_STATION");
    $("cm-work-src").textContent = src(p.work_source, "WORK_STATION");
    $("cm-time").value = p.switch_time || "12:00";
    document.querySelectorAll("#cm-days input").forEach((cb) => { cb.checked = (p.work_days || [0, 1, 2, 3, 4]).includes(Number(cb.value)); });
    syncDraft();
    $("commute-dialog").showModal();
    if (!draft.home) setTimeout(() => $("cm-home").focus(), 50);
  }

  $("commute-cancel").addEventListener("click", () => $("commute-dialog").close());
  $("commute-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const p = state.home;
    if (draft.home && draft.work && draft.home === draft.work) { alert("Home and work need to be different stations."); return; }
    if (draft.work && !draft.home) { alert("Pick a home station too, so the app knows where to take you after work."); return; }
    const days = [...document.querySelectorAll("#cm-days input:checked")].map((cb) => Number(cb.value));
    if (draft.work && !days.length) { alert("Pick at least one workday."); return; }
    const values = { switch_time: $("cm-time").value || null, work_days: days };
    // Only send stations that changed, so values from the environment aren't copied into the app.
    if (draft.home !== (p.home_station || null)) { values.home_station = draft.home; values.home_dest = null; }
    if (draft.work !== (p.work_station || null)) values.work_station = draft.work;
    $("commute-dialog").close();
    await saveProfile(values, "commute");
    goToLeg();
  });

  // First run: nothing configured anywhere -> invite the user to set it up.
  function setupCardHtml() {
    if (state.home.home_station) return "";
    return `<li class="setup-card"><div><b>Set up your commute</b><br><span class="muted small">Pick your home (and work) station so the app opens on the right trains.</span></div><button type="button" class="primary" id="setup-commute">Set up</button></li>`;
  }
  document.addEventListener("click", (e) => { if (e.target.id === "setup-commute") openCommute(); });

  // ---------- departure board ----------
  // Which list the board shows: to a destination, one line in one direction, or line cards.
  state.line = null;  // {route, direction} after tapping a train on a line card
  function lineQuery() {
    if (state.dest) return `&limit=${boardCount()}`;
    if (state.line) return `&route=${encodeURIComponent(state.line.route)}${state.line.direction ? `&direction=${state.line.direction}` : ""}&limit=${Math.max(boardCount(), 15)}`;
    return "&limit=40";
  }

  async function refreshDepartures() {
    if (state.tab === "timetable" && state.ttLine) {
      const { route, direction } = state.ttLine;
      let d;
      try { d = await api(`/api/line?route=${encodeURIComponent(route)}&direction=${direction}`); } catch (e) { console.warn(e); return; }
      if (state.tab !== "timetable" || !state.ttLine || state.ttLine.route !== route || state.ttLine.direction !== direction) return;
      d.tab = "timetable";
      d.kind = "line";
      state.departures = d;
      renderBoard();
      return;
    }
    if (!state.station) {
      $("data-line").hidden = true;
      state.departures = null;  // so the 15 s re-render doesn't bring the old board back
      lastBoardKey = null;
      $("board-title").textContent = "Choose a station";
      $("board-list").innerHTML = "";
      $("board-notice").hidden = true;
      showEmpty("Search for a station above, tap one on the map, or use To home / To work.");
      updateHomeUi();
      return;
    }
    try {
      const to = state.dest ? `&to=${encodeURIComponent(state.dest)}` : "";
      const tab = state.tab;
      const d = tab === "timetable"
        ? await api(`/api/timetable?station=${encodeURIComponent(state.station)}${to}`)
        : await api(`/api/departures?station=${encodeURIComponent(state.station)}${to}${lineQuery()}`);
      if (tab !== state.tab) return;  // switched tabs while loading
      d.tab = tab;
      state.departures = d;
    } catch (e) {
      console.warn(e);
      showEmpty("Couldn't load departures. Retrying…");
      return;
    }
    renderBoard();
  }

  function showEmpty(msg) {
    const p = $("board-empty");
    p.textContent = msg;
    p.hidden = !msg;
  }

  // Estimated arrival at the end of the line (skipped when it's the chosen destination).
  function finalHtml(r, d) {
    if (!r.final_stop || !r.final_arrival || r.cancelled) return "";
    if (d.to && r.arrival && r.final_stop === d.to.name) return "";
    return `<div class="final">Arrives ${esc(r.final_stop)} ${timeChange(r.final_arrival_scheduled, r.final_arrival)}</div>`;
  }

  // Like the departure time: if the estimate differs from the timetable,
  // cross out the scheduled time and show the estimate next to it.
  function timeChange(scheduled, estimate) {
    const est = `<b>${fmtTime(estimate)}</b>`;
    if (!scheduled || fmtTime(scheduled) === fmtTime(estimate)) return est;
    return `<s class="was">${fmtTime(scheduled)}</s> ${est}`;
  }

  // "On time 85% · avg 3 min late · 30 days": this train's record at this
  // station on the same kind of day (weekday/Sat/Sun). Server only sends it
  // once the train has been seen on enough days.
  function historyHtml(h) {
    if (!h) return "";
    const days = `${h.observations} days`;
    if (h.on_time_pct == null) return `<div class="hist"><span class="bad">Cancelled</span> on all ${days} seen</div>`;
    const pct = Math.round(h.on_time_pct);
    const cls = pct >= 90 ? "good" : pct >= 75 ? "ok" : "bad";
    const avg = Math.round(h.avg_delay_min || 0);
    const parts = [`<span class="${cls}">${pct}%</span> on time`];
    if (avg >= 1) parts.push(`avg ${avg} min late`);
    if (h.cancelled) parts.push(`${h.cancelled} cancelled`);
    parts.push(days);
    return `<div class="hist" title="Based on the last ${h.days} days of Metra's predictions for this train at this station (weekdays, Saturdays and Sundays counted separately). Metra counts under 6 minutes late as on time.">${parts.map((x) => `<span class="part">${x}</span>`).join(" · ")}</div>`;
  }

  const untilText = (m) => (m < 60 ? `${m} min` : `${Math.floor(m / 60)} hr${m % 60 ? ` ${m % 60} min` : ""}`);

  // Timetable for a whole line in one direction (opened from the Map tab's line card).
  // Running trains are cards with a progress bar coloured by how late they are;
  // the rest are a compact list.
  function delayClass(delay) {
    if (delay == null) return "sched";
    const m = Math.round(delay / 60);
    return m >= 6 ? "late" : m >= 1 ? "minor" : m <= -1 ? "early" : "ontime";
  }
  function renderLineTimetable(d, now) {
    const dirLabel = d.direction === "inbound" ? "toward downtown" : "away from downtown";
    $("board-title").textContent = "Line timetable";
    $("board-swap").hidden = true;  // the direction toggle below swaps here
    const asOf = d.data_as_of || {};
    $("board-updated").textContent = asOf.realtime_updated ? `Metra data ${fmtDateTime(asOf.realtime_updated)}` : "Metra data";
    $("board-updated").title = dataStatement(asOf, "train");
    $("data-line").textContent = DISCLAIMER;
    $("data-line").hidden = false;
    $("board-list").innerHTML = "";
    showEmpty("");
    const running = d.trains.filter((t) => t.running);
    const later = d.trains.filter((t) => !t.running);
    const counts = [running.length ? `${running.length} on the way` : "", later.length ? `${later.length} more today` : ""]
      .filter(Boolean).join(" · ") || "No more trains today";
    const html = [`<div class="lt-hero" style="--line:${esc(d.color || "")}">
        <div class="lt-name">${chip(d.route_short, d.color, d.text_color)}<b>${esc(d.route_name)}</b></div>
        <div class="lt-sub">${esc(counts)}</div>
      </div>
      <div class="lt-dir" role="group" aria-label="Direction">
        <button type="button" data-dir="inbound" aria-pressed="${d.direction === "inbound"}">Toward downtown</button>
        <button type="button" data-dir="outbound" aria-pressed="${d.direction === "outbound"}">Away from downtown</button>
      </div>`];
    if (!d.trains.length) {
      html.push(`<p class="muted tt-none">No more ${esc(d.route_short)} trains ${dirLabel} today.</p>`);
      $("tt").innerHTML = html.join("");
      return;
    }
    const status = (t) => t.cancelled ? '<span class="badge cancel">Cancelled</span>'
      : (t.live || t.tracking) && t.delay != null ? delayHtml(t.delay, t.tracking)
      : '<span class="tt-sched">Scheduled</span>';
    if (running.length) {
      html.push('<h3 class="lt-head"><span class="lt-live"></span>On the way now</h3>');
      for (const t of running) {
        // How far along: time since it (likely) left vs. its scheduled run time.
        const late = Math.max(0, t.delay || 0);
        const span = Math.max(60, (t.dest_time_scheduled || t.dest_time) - t.origin_time);
        const pct = Math.max(3, Math.min(97, ((now - t.origin_time - late) / span) * 100));
        const cls = (t.live || t.tracking) ? delayClass(t.delay) : "sched";
        html.push(`<div class="lt-card" data-trip="${esc(t.trip_id)}">
          <div class="lt-card-top"><b>${t.train ? `#${esc(t.train)} ` : ""}to ${esc(t.destination || "")}</b>${status(t)}</div>
          <div class="lt-bar ${cls}"><span style="width:${pct.toFixed(1)}%"></span></div>
          <div class="lt-card-foot">
            <span class="muted">${esc(t.origin)} ${fmtTime(t.origin_time)}</span>
            ${t.next_stop ? `<span class="lt-next"><span class="muted">Next</span> <b>${esc(t.next_stop)}</b></span>` : ""}
            <b class="lt-arr" title="Arrives ${esc(t.destination || "")}${t.dest_time !== t.dest_time_scheduled ? ` (scheduled ${fmtTime(t.dest_time_scheduled)})` : ""}">${fmtTime(t.dest_time)}</b>
          </div>
        </div>`);
      }
    }
    if (later.length) {
      html.push(`<h3 class="lt-head">${running.length ? "Later today" : "Still to come today"}</h3><ol class="lt-list">`);
      for (const t of later) {
        const m = Math.round((t.origin_time - now) / 60);
        html.push(`<li class="lt-row${t.cancelled ? " cancelled" : ""}">
          <span class="lt-time">${bigTime(t.origin_time)}</span>
          <span class="lt-main"><span>${esc(t.destination || "")}${t.train ? ` <span class="muted small">#${esc(t.train)}</span>` : ""}</span>
            <span class="muted small">arrives ${timeChange(t.dest_time_scheduled, t.dest_time)}${t.cancelled || t.live ? ` · ${status(t)}` : ""}</span></span>
          <span class="lt-in">${m > 0 ? untilText(m) : ""}</span>
        </li>`);
      }
      html.push("</ol>");
    }
    $("tt").innerHTML = html.join("");
  }
  $("tt").addEventListener("click", (e) => {
    const b = e.target.closest(".lt-dir button");
    if (!b || !state.ttLine || b.dataset.dir === state.ttLine.direction) return;
    state.ttLine = { ...state.ttLine, direction: b.dataset.dir };
    state.departures = null;
    refreshDepartures();
  });

  // Today's timetable: every train still to come, compact, grouped by hour.
  function renderTimetable(d, now, from) {
    const el = $("tt");
    const rows = d.departures;
    if (!rows.length) {
      el.innerHTML = "";
      showEmpty(d.to ? `No more trains today from ${from} to ${d.to.name}.` : `No more trains today from ${from}.`);
      return;
    }
    showEmpty("");
    const hourFmt = new Intl.DateTimeFormat([], { hour: "numeric", timeZone: state.tz });
    const next = rows.find((r) => !r.cancelled);
    let lastHour = null;
    const html = [];
    html.push(`<p class="tt-summary">${rows.length} train${rows.length === 1 ? "" : "s"} left today${d.to ? ` to ${esc(d.to.name)}` : ""} · last at ${esc(fmtTime(rows[rows.length - 1].time))}</p>`);
    html.push('<ol class="tt-list">');
    for (const r of rows) {
      const hour = hourFmt.format(new Date(r.time * 1000));
      if (hour !== lastHour) { html.push(`<li class="tt-hour">${esc(hour)}</li>`); lastHour = hour; }
      const mins = Math.round((r.time - now) / 60);
      const status = r.cancelled ? '<span class="badge cancel">Cancelled</span>'
        : r.added ? '<span class="badge extra">Extra</span>'
        : (r.live || r.tracking) ? (delayHtml(r.delay, r.tracking) || '<span class="delay ontime">On time</span>')
        : '<span class="tt-sched">Scheduled</span>';
      const changed = r.scheduled && Math.abs(r.time - r.scheduled) >= 60;
      const where = d.to && r.arrival
        ? `Arrives ${esc(d.to.name)} ${timeChange(r.arrival_scheduled, r.arrival)}`
        : `To ${esc(r.destination || r.route_name)}`;
      html.push(`<li class="tt-row${r.cancelled ? " cancelled" : ""}${r === next ? " next" : ""}" style="--line:${esc(r.color)}">
        <span class="tt-time">${bigTime(r.time)}${changed ? `<s class="was">${fmtTime(r.scheduled)}</s>` : ""}</span>
        <span class="tt-main">
          <span class="tt-top">${chip(r.route_short, r.color, r.text_color)}${r.train ? `<span class="muted">#${esc(r.train)}</span>` : ""}${status}${r.schedule_change ? '<span class="badge schedchg">Changed</span>' : ""}</span>
          <span class="tt-where">${where}</span>
        </span>
        <span class="tt-mins">${r.cancelled ? "" : r === next || mins < 60 ? (mins <= 0 ? "Now" : untilText(mins)) : ""}</span>
      </li>`);
    }
    html.push("</ol>");
    el.innerHTML = html.join("");
  }

  // The board re-renders every 15-30 s; only animate when the view itself changes
  // (another station, direction, line or tab), so a refresh never flickers.
  let lastBoardKey = null;
  function renderBoard() {
    renderBoardInner();
    const d = state.departures;
    if (!d) return;
    const key = [d.tab, d.station && d.station.id, d.to && d.to.id, d.route, d.direction].join("|");
    if (key !== lastBoardKey) {
      if (lastBoardKey !== null) {
        staggerIn(d.tab === "timetable" ? $("tt").querySelector(".tt-list") : $("board-list"));
        fadeIn($("board-title"));
      }
      lastBoardKey = key;
    }
  }

  function renderBoardInner() {
    const d = state.departures;
    renderBoardNotice();
    if (!d) return;
    const now = serverNow();
    if (d.kind === "line") { renderLineTimetable(d, now); return; }
    const from = d.station ? d.station.name : "";
    const tag = viewTag();
    // Station names are buttons that show that station on the map (not on the timetable tab, which has no map).
    const stn = (id, name) => (mapHidden() || !id ? esc(name)
      : `<button type="button" class="title-stn" data-stn="${esc(id)}" title="Show ${esc(name)} on the map">${esc(name)}</button>`);
    const lineTitle = () => {
      const r = d.departures[0];
      const dir = { inbound: "toward downtown", outbound: "away from downtown" }[d.direction] || "";
      return ` <span class="title-line">${r ? chip(r.route_short, r.color, r.text_color) : esc(d.route)} ${esc(dir)}</span>`;
    };
    $("board-title").innerHTML = (d.to ? `${stn(d.station && d.station.id, from)} → ${stn(d.to.id, d.to.name)}`
      : stn(d.station && d.station.id, from) + (d.route ? lineTitle() : ""))
      + (tag && !d.route ? `<span class="home-tag">${tag}</span>` : "");
    const asOf = d.data_as_of || {};
    // License: when Metra last updated the data (here) + the disclaimer (small line below).
    $("board-updated").textContent = asOf.realtime_updated ? `Metra data ${fmtDateTime(asOf.realtime_updated)}`
      : asOf.schedule_published ? `Metra schedule ${asOf.schedule_published.split(" ")[0]}` : "Metra data";
    $("board-updated").title = dataStatement(asOf, "train");
    $("data-line").textContent = DISCLAIMER;
    $("data-line").hidden = false;
    if (d.tab === "timetable") { renderTimetable(d, now, from); return; }
    if (!d.departures.length) {
      $("board-list").innerHTML = "";
      const reach = state.dests[d.station && d.station.id];
      const toSt = d.to && state.stationById.get(d.to.id);
      const noDirect = d.to && Array.isArray(reach) && !reach.some((x) => x.id === d.to.id);
      const toLines = toSt ? toSt.routes.join(", ") : "";
      showEmpty(noDirect
        ? `There's no direct train from ${from} to ${d.to.name}${toLines ? ` (it's on the ${toLines} line${toSt.routes.length > 1 ? "s" : ""})` : ""}. Pick another "Going to" station, or tap Clear.`
        : d.to
        ? `No upcoming trains from ${from} to ${d.to.name}. They may be on different lines; try "Any destination".`
        : "No upcoming departures found in the schedule.");
      return;
    }
    showEmpty("");
    if (!d.to && !d.route) { renderLineCards(d, now); return; }
    $("board-list").innerHTML = setupCardHtml() + d.departures.map((r) => {
      const mins = Math.max(0, Math.round((r.time - now) / 60));
      // Under an hour: "45 min". From an hour: "2 hr" with the minutes underneath.
      const minsHtml = r.cancelled ? "<b>—</b>" : mins === 0 ? "<b>Now</b>"
        : mins < 60 ? `<b>${mins}</b><span>min</span>`
        : `<b>${Math.floor(mins / 60)}<small>hr</small></b><span>${mins % 60 ? `${mins % 60} min` : "&nbsp;"}</span>`;
      const changed = r.scheduled && Math.abs(r.time - r.scheduled) >= 60;
      const badge = r.cancelled ? '<span class="badge cancel">Cancelled</span>'
        : r.live ? '<span class="badge live">Live</span>'
        : r.tracking ? '<span class="badge tracking" title="Metra sent no prediction for this train; delay estimated from its GPS position">Tracking</span>'
        : '<span class="badge sched">Scheduled</span>';
      const meta = [
        chip(r.route_short, r.color, r.text_color),
        badge,
        r.added ? '<span class="badge extra" title="Not in the published timetable">Extra train</span>' : "",
        r.schedule_change && !r.cancelled ? `<span class="badge schedchg" title="${esc(r.schedule_change)}">Changed</span>` : "",
        r.train ? `<span>#${esc(r.train)}</span>` : "",
        (r.live || r.tracking) && !r.cancelled ? delayHtml(r.delay, r.tracking) : "",
      ].filter(Boolean).join("");
      const sel = r.trip_id && r.trip_id === state.selectedTrip;
      return `<li class="dep${r.cancelled ? " cancelled" : ""}${sel ? " selected" : ""}" style="--line:${esc(r.color)}"${r.trip_id ? ` data-trip="${esc(r.trip_id)}" data-route="${esc(r.route_id || "")}" tabindex="0" role="button" aria-pressed="${sel}" title="Show this train on the map"` : ""}>
        <div class="timecol"><div class="time">${bigTime(r.time)}${changed ? `<span class="sched">${fmtTime(r.scheduled)}</span>` : ""}</div></div>
        <div class="main"><div class="dest" title="${esc(r.destination)}">${esc(r.destination || r.route_name)}</div>
        <div class="meta">${meta}</div>
        ${r.arrival ? `<div class="arrive">Arrives ${esc(d.to.name)} ${timeChange(r.arrival_scheduled, r.arrival)} · ${Math.round((r.arrival - r.time) / 60)} min trip</div>` : ""}
        ${finalHtml(r, d)}
        ${historyHtml(r.history)}
        ${r.delay_reason ? `<div class="reason" title="${esc(r.delay_reason)}">${esc(r.delay_reason)}</div>`
          : r.schedule_change ? `<div class="reason schedchg" title="${esc(r.schedule_change)}">${esc(r.schedule_change)}</div>` : ""}</div>
        <div class="mins">${minsHtml}</div>
      </li>`;
    }).join("");
    mapView.refreshPopups();  // "Leaves … at" in an open train popup follows the board
  }

  // No destination picked: one card per line and direction, with the next train
  // big and the following ones as small times. Tapping a train opens the board
  // to that train's final station (with arrival times), the train highlighted.
  function untilHtml(r, now) {
    const mins = Math.max(0, Math.round((r.time - now) / 60));
    if (r.cancelled) return "<b>—</b>";
    if (mins === 0) return "<b>Now</b>";
    return mins < 60 ? `<b>${mins}</b><small>min</small>` : `<b>${Math.floor(mins / 60)}</b><small>hr${mins % 60 ? ` ${mins % 60}m` : ""}</small>`;
  }
  function renderLineCards(d, now) {
    const groups = new Map();
    for (const r of d.departures) {
      const key = `${r.route_id}|${r.direction || ""}`;
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(r);
    }
    const dirLabel = { inbound: "Toward downtown", outbound: "Away from downtown" };
    const pickAttrs = (r) => (r.route_id
      ? ` data-pick="${esc(r.trip_id || "")}" data-route="${esc(r.route_id || "")}" data-dir="${esc(r.direction || "")}" title="All ${esc(r.route_short)} trains this way"` : "");
    const html = [...groups.values()].map((rs) => {
      const r = rs[0];
      const changed = r.scheduled && Math.abs(r.time - r.scheduled) >= 60;
      const badge = r.cancelled ? '<span class="badge cancel">Cancelled</span>'
        : r.live ? '<span class="badge live">Live</span>'
        : r.tracking ? '<span class="badge tracking">Tracking</span>'
        : '<span class="badge sched">Scheduled</span>';
      const status = [badge, r.train ? `<span class="muted">#${esc(r.train)}</span>` : "",
        (r.live || r.tracking) && !r.cancelled ? delayHtml(r.delay, r.tracking) : ""].filter(Boolean).join(" ");
      const later = rs.slice(1, 4).map((x) => {
        const late = (x.live || x.tracking) && x.delay >= 60 ? (x.delay >= 360 ? " late" : " minor") : "";
        return `<button type="button" class="lc-pill${x.cancelled ? " cancelled" : ""}${late}"${pickAttrs(x)}><b>${esc(fmtTime(x.time))}</b> · ${esc(x.destination || "")}</button>`;
      }).join("");
      return `<li class="lc${r.cancelled ? " cancelled" : ""}" style="--line:${esc(r.color)}">
        <div class="lc-top">${chip(r.route_short, r.color, r.text_color)}<span class="lc-name">${esc(r.route_name)}</span><span class="lc-dir">${dirLabel[r.direction] || ""}</span></div>
        <button type="button" class="lc-next"${pickAttrs(r)}>
          <span class="lc-time">${bigTime(r.time)}${changed ? `<s class="was">${fmtTime(r.scheduled)}</s>` : ""}</span>
          <span class="lc-main"><span class="lc-dest">${esc(r.destination || r.route_name)}</span><span class="lc-status">${status}</span>
            ${r.delay_reason ? `<span class="reason">${esc(r.delay_reason)}</span>` : r.schedule_change ? `<span class="reason schedchg">${esc(r.schedule_change)}</span>` : ""}</span>
          <span class="lc-in">${untilHtml(r, now)}</span>
        </button>
        ${later ? `<div class="lc-later"><span class="muted">Then</span>${later}</div>` : ""}
      </li>`;
    });
    $("board-list").innerHTML = setupCardHtml() + html.join("");
    mapView.refreshPopups();
  }
  $("board-list").addEventListener("click", (e) => {
    const b = e.target.closest("[data-pick]");
    if (!b || !b.dataset.route) return;
    e.stopPropagation();
    // Show every train on this line in this direction, with the tapped train highlighted (and on the map).
    state.selectedTrip = b.dataset.pick || null;
    state.line = { route: b.dataset.route, direction: b.dataset.dir || null };
    state.departures = null;
    updateClear();
    refreshDepartures();
    if (state.selectedTrip && !mapHidden()) mapView.selectTrip(state.selectedTrip, b.dataset.route);
    window.scrollTo({ top: $("board-title").getBoundingClientRect().top + window.scrollY - 12, behavior: "smooth" });
  }, true);

  // ---------- display options (this device only) ----------
  const view = { pickers: "show", alerts: "bell", alertLines: "all", mapTrains: "dest", count: "5" };
  try { Object.assign(view, JSON.parse(localStorage.getItem(VIEW_KEY) || "{}")); } catch (e) { /* ignore */ }
  // The old "Timetable only" display option is now the Timetable tab.
  if (view.map === "hide" && !localStorage.getItem(TAB_KEY)) localStorage.setItem(TAB_KEY, "timetable");
  delete view.map;
  // Earlier versions stored the line choice in "alerts".
  if (view.alerts === "all" || view.alerts === "mine") { view.alertLines = view.alerts; view.alerts = "bell"; }
  let pickersOpen = false;  // "Change" shows hidden pickers for a moment

  // ---------- tabs: next trains + map / today's timetable ----------
  state.tab = ["timetable", "map"].includes(localStorage.getItem(TAB_KEY)) ? localStorage.getItem(TAB_KEY) : "live";
  const mapHidden = () => state.tab === "timetable";
  const COUNT_PRESETS = ["3", "5", "8", "10"];
  const clampCount = (v) => Math.min(50, Math.max(1, parseInt(v, 10) || 5));
  const boardCount = () => clampCount(view.count);
  function syncCountUi() {
    const custom = !COUNT_PRESETS.includes(String(view.count));
    document.querySelectorAll("#view-form input[name=count]").forEach((r) => { r.checked = custom ? r.value === "custom" : r.value === String(view.count); });
    $("count-custom").hidden = !custom;
    $("count-input").value = boardCount();
  }
  // Custom: type any number of trains; saved as you type.
  $("count-input").addEventListener("input", () => {
    const raw = $("count-input").value;
    if (!raw) return;
    view.count = String(clampCount(raw));
    localStorage.setItem(VIEW_KEY, JSON.stringify(view));
    clearTimeout(syncCountUi.t);
    syncCountUi.t = setTimeout(refreshDepartures, 400);
  });
  $("count-input").addEventListener("change", () => { $("count-input").value = boardCount(); });
  document.querySelectorAll(".tabs [data-tab]").forEach((b) => b.addEventListener("click", () => {
    if (state.tab === b.dataset.tab) return;
    const boardKind = (t) => (t === "timetable" ? "timetable" : "board");
    const reload = boardKind(state.tab) !== boardKind(b.dataset.tab);
    state.tab = b.dataset.tab;
    localStorage.setItem(TAB_KEY, state.tab);
    if (reload) state.departures = null;
    applyView();
    if (reload) refreshDepartures();
  }));

  function applyView() {
    const tabs = [...document.querySelectorAll(".tabs [data-tab]")];
    tabs.forEach((b) => b.setAttribute("aria-selected", String(b.dataset.tab === state.tab)));
    const tabBar = document.querySelector(".tabs");
    tabBar.style.setProperty("--tab-i", String(Math.max(0, tabs.findIndex((b) => b.dataset.tab === state.tab))));
    tabBar.style.setProperty("--tab-n", String(tabs.length));
    const wasMapOnly = document.body.classList.contains("map-only");
    $("board-list").hidden = mapHidden();
    $("tt").hidden = !mapHidden();
    const wasHidden = document.body.classList.contains("no-map");
    document.body.classList.toggle("no-map", mapHidden());
    document.body.classList.toggle("map-only", state.tab === "map");
    if (state.tab === "map") { if ($("map-head").hidden) { renderMapHead(); enter($("map-head")); requestAnimationFrame(measureMapOverlays); } }
    else {
      $("map-head").hidden = true;
      if (mapView.lineFocus) mapView.setLineFocus(null, { fit: false });
    }
    if (wasMapOnly !== (state.tab === "map") || (wasHidden && !mapHidden())) fadeIn(document.querySelector(".map-wrap"));
    // The map's box changes size between tabs: fit it again (and catch up on trains if it was hidden).
    requestAnimationFrame(() => {
      if (mapHidden()) return;
      mapView.resize();
      if (wasHidden && state.stations.length) refreshTrains();
    });
    updateClear();
    const show = view.pickers !== "hide" || pickersOpen;
    if (show && $("picker").hidden) enter($("picker")); else if (!show) $("picker").hidden = true;
    const btn = $("change-btn");
    btn.hidden = view.pickers !== "hide";
    btn.textContent = pickersOpen ? "Done" : "Change";
    btn.setAttribute("aria-expanded", String(show));
    renderAlerts();
  }

  $("change-btn").addEventListener("click", () => {
    pickersOpen = !pickersOpen;
    applyView();
    if (pickersOpen) $("station-input").focus();
  });

  // ---------- settings page (#settings, so the phone's back button closes it) ----------
  function openSettings() {
    if (location.hash !== "#settings") location.hash = "settings";
    else showSettings(true);
  }
  function closeSettings() {
    if (location.hash === "#settings") history.back();
    else showSettings(false);
  }
  function showSettings(on) {
    const page = $("settings-page");
    if (on === !page.hidden && !page.classList.contains("anim-leave")) return;
    if (on) {
      document.body.classList.add("settings-open");
      fillSettings();
      window.scrollTo(0, 0);
      enter(page);
    } else {
      leave(page, () => {
        document.body.classList.remove("settings-open");
        fadeIn(document.querySelector(".layout"));
        // The map may have been resized while hidden.
        requestAnimationFrame(() => { if (!mapHidden()) mapView.resize(); });
      });
    }
  }
  window.addEventListener("hashchange", () => showSettings(location.hash === "#settings"));
  $("settings-back").addEventListener("click", closeSettings);
  $("settings-done").addEventListener("click", closeSettings);
  $("settings-commute-edit").addEventListener("click", () => openCommute());

  function fillSettings() {
    const p = state.home || {};
    const name = (id) => (id && state.stationById.get(id) ? state.stationById.get(id).name : null);
    const DAYN = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
    const days = (p.work_days || []).map((d) => DAYN[d]).join(", ");
    const clock = (hhmm) => {
      const [h, m] = hhmm.split(":").map(Number);
      return `${h % 12 || 12}:${String(m).padStart(2, "0")} ${h < 12 ? "AM" : "PM"}`;
    };
    const kv = (rows) => rows.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${v == null || v === "" ? '<span class="muted">Not set</span>' : esc(v)}</dd>`).join("");
    $("settings-commute").innerHTML = kv([
      ["Home", name(p.home_station)],
      ["Work", name(p.work_station)],
      ["Trains home from", p.work_station && p.switch_time ? clock(p.switch_time) : null],
      ["Workdays", p.work_station ? days : null],
    ]);
    const s = state.status || {};
    const mapSt = s.map || {};
    $("settings-about").innerHTML = kv([
      ["Realtime", s.mode === "schedule-only" ? "Off (no API key)" : s.realtime_live ? `Live (${Math.round(s.realtime_age || 0)}s old)` : "Unavailable right now"],
      ["Schedule", s.static_version || null],
      ["Map", {
        ready: `Stored on the server (${Math.round((mapSt.size || 0) / 1e6)} MB)`,
        downloading: "The server is downloading it",
        missing: "Not downloaded yet",
        error: `Download failed: ${mapSt.error || "unknown error"}`,
        disabled: "The server's map is off (MAP_DOWNLOAD=false); using online maps",
      }[mapSt.state] || null],
      ["Trains update", `every ${state.interval}s`],
    ]);
    const theme = document.documentElement.dataset.theme;
    document.querySelectorAll("input[name=theme]").forEach((r) => { r.checked = r.value === theme; });
    syncCountUi();
    for (const k of ["mapTrains", "pickers", "alerts", "alertLines"]) {
      document.querySelectorAll(`#view-form input[name=${k}]`).forEach((r) => { r.checked = r.value === view[k]; });
    }
  }
  document.querySelectorAll("input[name=theme]").forEach((r) => r.addEventListener("change", () => {
    if (r.checked && document.documentElement.dataset.theme !== r.value) $("theme-toggle").click();
  }));

  $("view-btn").addEventListener("click", () => openSettings());
  $("view-form").addEventListener("change", (e) => {
    if (!e.target.name) return;
    if (e.target.name === "count") {
      if (e.target.value === "custom") {
        // Start from the current number (or 12 if it was a preset) and let them type.
        if (COUNT_PRESETS.includes(String(view.count))) view.count = "12";
        localStorage.setItem(VIEW_KEY, JSON.stringify(view));
        syncCountUi();
        refreshDepartures();
        $("count-input").focus();
        $("count-input").select();
        return;
      }
      view.count = e.target.value;
      localStorage.setItem(VIEW_KEY, JSON.stringify(view));
      syncCountUi();
      refreshDepartures();
      return;
    }
    view[e.target.name] = e.target.value;
    localStorage.setItem(VIEW_KEY, JSON.stringify(view));
    if (e.target.name === "mapTrains") refreshTrains();
    pickersOpen = false;
    applyView();
  });

  // ---------- alerts ----------
  // Alert ids hidden with the banner's ✕; the banner returns when a new alert appears.
  let dismissed = new Set();
  try { dismissed = new Set(JSON.parse(localStorage.getItem(DISMISSED_KEY) || "[]")); } catch (e) { /* ignore */ }
  let bannerIds = [];
  $("alerts-dismiss").addEventListener("click", () => {
    // Remember only alerts that still exist, so the list never grows.
    const live = new Set((state.alerts || []).map((a) => a.id));
    dismissed = new Set([...dismissed, ...bannerIds].filter((id) => live.has(id)));
    localStorage.setItem(DISMISSED_KEY, JSON.stringify([...dismissed]));
    renderAlerts();
  });

  // Bell: alerts stay out of the way until you open them. The badge counts
  // alerts you haven't looked at yet; opening the bell marks them seen.
  const bellOpen = () => !$("bell-panel").hidden && !$("bell-panel").classList.contains("anim-leave");
  function setBell(open) {
    const wasOpen = bellOpen();
    if (open) enter($("bell-panel")); else leave($("bell-panel"));
    $("bell-btn").setAttribute("aria-expanded", String(open));
    // New ones keep their dot while the list is open; they count as seen once you close it.
    if (wasOpen && !open) {
      const live = new Set((state.alerts || []).map((a) => a.id));
      dismissed = new Set([...dismissed, ...bannerIds].filter((id) => live.has(id)));
      localStorage.setItem(DISMISSED_KEY, JSON.stringify([...dismissed]));
      renderAlerts();
    }
  }
  $("bell-btn").addEventListener("click", (e) => { e.stopPropagation(); setBell(!bellOpen()); });
  document.addEventListener("click", (e) => {
    if (bellOpen() && !$("bell-panel").contains(e.target)) setBell(false);
  });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && bellOpen()) setBell(false); });

  $("alerts-toggle").addEventListener("click", () => {
    const listEl = $("alerts-list");
    const opening = listEl.hidden;
    if (opening) enter(listEl); else leave(listEl);
    $("alerts-toggle").setAttribute("aria-expanded", String(opening));
  });

  async function refreshAlerts() {
    let alerts;
    try { alerts = await api("/api/alerts"); } catch (e) { console.warn(e); return; }
    state.alerts = alerts;
    renderAlerts();
    renderBoardNotice();
  }

  // The banner only features alerts for lines you use (the selected station,
  // its destination and your home station); everything else sits behind a
  // quiet "N alerts on other lines".
  function renderAlerts() {
    const alerts = state.alerts || [];
    const box = $("alerts");
    const bell = view.alerts === "bell";
    $("bell-btn").hidden = !bell;
    if (!bell) $("bell-panel").hidden = true;
    if (view.alerts === "off") { box.hidden = true; return; }
    const myLines = new Set();
    for (const id of [state.station, state.dest, state.home.home_station, state.home.home_dest, state.home.work_station]) {
      const st = state.stationById.get(id);
      if (st) st.routes.forEach((r) => myLines.add(r));
    }
    const isMine = (a) => a.routes.some((r) => myLines.has(r.id));
    // Alerts you haven't hidden come first, so a new one leads the banner.
    // Then timetable changes before other notices (elevators, construction...).
    const fresh = (a) => (dismissed.has(a.id) ? 2 : 0) + (a.schedule_change ? 0 : 1);
    const mine = alerts.filter(isMine).sort((x, y) => fresh(x) - fresh(y));
    const other = view.alertLines === "mine" ? [] : alerts.filter((a) => !isMine(a)).sort((x, y) => fresh(x) - fresh(y));
    bannerIds = [...mine, ...other].map((a) => a.id);
    const item = (a) => `<li class="${dismissed.has(a.id) ? "" : "unseen"}">
      <div>${a.routes.map((r) => chip(r.short_name, r.color, r.text_color)).join(" ")}${a.schedule_change ? ' <span class="badge schedchg">Schedule change</span>' : ""}</div>
      <h3>${esc(a.header)}</h3>
      ${a.description && a.description !== a.header ? `<p>${esc(a.description)}</p>` : ""}
      ${a.url ? `<p><a href="${esc(a.url)}" target="_blank" rel="noopener noreferrer">More info</a></p>` : ""}
    </li>`;
    const listHtml = mine.map(item).join("")
      + (other.length && mine.length ? '<li class="alerts-divider">Other lines</li>' : "")
      + other.map(item).join("");
    if (bell) {
      box.hidden = true;
      const unseen = bannerIds.filter((id) => !dismissed.has(id)).length;
      const badge = $("bell-badge");
      badge.hidden = !unseen;
      badge.textContent = unseen > 9 ? "9+" : String(unseen);
      badge.classList.toggle("mine", mine.some((a) => !dismissed.has(a.id)));
      $("bell-btn").title = bannerIds.length ? `${bannerIds.length} alert${bannerIds.length === 1 ? "" : "s"}${unseen ? `, ${unseen} new` : ""}` : "No alerts";
      $("bell-sub").textContent = !bannerIds.length ? "" : mine.length ? `${mine.length} on your lines` : "None on your lines";
      $("bell-list").innerHTML = listHtml || `<li class="empty">No alerts${view.alertLines === "mine" ? " on your lines" : ""} right now.</li>`;
      return;
    }
    if (!bannerIds.length || bannerIds.every((id) => dismissed.has(id))) { box.hidden = true; return; }
    box.hidden = false;
    box.classList.toggle("quiet", !mine.length);
    if (mine.length) {
      $("alerts-headline").textContent = mine[0].header;
      const extra = mine.length - 1;
      $("alerts-count").textContent = extra > 0 ? `+${extra} more` + (other.length ? ` · ${other.length} other lines` : "")
        : other.length ? `${other.length} on other lines` : "Details";
    } else {
      $("alerts-headline").textContent = `${other.length} alert${other.length === 1 ? "" : "s"} on other lines`;
      $("alerts-count").textContent = "Show";
    }
    $("alerts-list").innerHTML = listHtml;
  }

  // A line above the board when Metra says trains on the lines shown won't
  // match the published timetable (special or modified service, extra trains...).
  function renderBoardNotice() {
    const el = $("board-notice");
    const d = state.departures;
    const routes = new Set(((d && d.departures) || []).map((r) => r.route_id));
    const hits = view.alerts === "off" ? []
      : (state.alerts || []).filter((a) => a.schedule_change && a.routes.some((r) => routes.has(r.id)))
        .sort((x, y) => noticeHidden.has(x.id) - noticeHidden.has(y.id));  // ones you haven't hidden first
    noticeIds = hits.map((a) => a.id);
    // Hidden once swiped away / ✕'d, until a schedule-change alert we haven't hidden shows up.
    const show = hits.length && !hits.every((a) => noticeHidden.has(a.id));
    el.style.transform = ""; el.style.opacity = "";
    if (!show) { el.hidden = true; return; }
    if (el.hidden) enter(el);
    const lines = [...new Set(hits.flatMap((a) => a.routes.filter((r) => routes.has(r.id)).map((r) => r.short_name)))];
    $("board-notice-main").innerHTML = `<span class="badge schedchg">Schedule change</span>
      <span class="txt"><b>${esc(lines.join(", "))}:</b> ${esc(hits[0].header)}</span>
      ${hits.length > 1 ? `<span class="more">+${hits.length - 1}</span>` : ""}`;
    el.title = hits.map((a) => a.header).join("\n");
  }
  // Hidden schedule-change strips (alert ids), kept on this device.
  const NOTICE_KEY = "metra.notice.hidden";
  let noticeIds = [];
  let noticeHidden = new Set();
  try { noticeHidden = new Set(JSON.parse(localStorage.getItem(NOTICE_KEY) || "[]")); } catch (e) { /* ignore */ }
  function hideNotice() {
    const live = new Set((state.alerts || []).map((a) => a.id));
    noticeHidden = new Set([...noticeHidden, ...noticeIds].filter((id) => live.has(id)));
    localStorage.setItem(NOTICE_KEY, JSON.stringify([...noticeHidden]));
    const el = $("board-notice");
    if (el.style.opacity === "0") el.hidden = true;  // already swiped off screen
    else leave(el);
  }
  $("board-notice-x").addEventListener("click", (e) => { e.stopPropagation(); hideNotice(); });
  // Swipe left or right to hide it.
  (() => {
    const el = $("board-notice");
    let x0 = null, dx = 0, dragged = false;
    el.addEventListener("pointerdown", (e) => { x0 = e.clientX; dx = 0; dragged = false; });
    el.addEventListener("pointermove", (e) => {
      if (x0 === null) return;
      dx = e.clientX - x0;
      if (Math.abs(dx) > 8) {
        dragged = true;
        el.style.transition = "none";
        el.style.transform = `translateX(${dx}px)`;
        el.style.opacity = String(Math.max(0.2, 1 - Math.abs(dx) / 260));
      }
    });
    const end = () => {
      if (x0 === null) return;
      x0 = null;
      el.style.transition = "transform .2s ease, opacity .2s ease";
      if (Math.abs(dx) > 90) {
        el.style.transform = `translateX(${dx > 0 ? 120 : -120}%)`;
        el.style.opacity = "0";
        setTimeout(hideNotice, 200);
      } else {
        el.style.transform = ""; el.style.opacity = "";
      }
    };
    el.addEventListener("pointerup", end);
    el.addEventListener("pointercancel", end);
    // A swipe isn't a tap: don't open the alerts after one.
    el.addEventListener("click", (e) => { if (dragged) { e.stopPropagation(); e.preventDefault(); dragged = false; } }, true);
  })();
  $("board-notice-main").addEventListener("click", (e) => {
    if (view.alerts === "bell") { e.stopPropagation(); setBell(true); }
    else { $("alerts-list").hidden = false; $("alerts-toggle").setAttribute("aria-expanded", "true"); $("alerts").scrollIntoView({ behavior: "smooth" }); }
  });

  // ---------- status ----------
  async function refreshStatus() {
    try { state.status = await api("/api/status"); } catch (e) { return false; }
    const s = state.status;
    if (s.timezone !== state.tz) { state.tz = s.timezone; timeFmt = null; dateTimeFmt = null; }
    state.interval = Math.max(30, s.realtime_interval || 30);
    const pill = $("mode-pill");
    pill.classList.remove("live", "warn");
    if (s.mode === "schedule-only") {
      pill.textContent = "Schedule only";
      pill.title = "No METRA_API_TOKEN configured; showing scheduled times.";
    } else if (s.realtime_live) {
      const age = s.realtime_age;
      pill.classList.add("live");
      if (age != null && age >= 90) {
        pill.textContent = `Live · ${Math.round(age / 60)} min old`;
        pill.classList.add("warn");
      } else {
        pill.textContent = "Live";
      }
      pill.title = age != null ? `Realtime data from Metra, ${Math.round(age)}s old` : "Realtime data from Metra";
    } else {
      pill.textContent = s.realtime_age != null ? `Realtime stale (${Math.round(s.realtime_age / 60)} min)` : "Realtime unavailable";
      pill.classList.add("warn");
      const errs = Object.entries(s.feeds || {}).filter(([, f]) => f.error).map(([n, f]) => `${n}: ${f.error}`);
      pill.title = errs.join("\n") || "Waiting for realtime data; showing schedule.";
    }
    $("data-version").textContent = s.static_version ? `Schedule version: ${s.static_version}.` : "";
    if (s.static_loaded && state.stations.length) refreshProfile();
    mapView.setMapStatus(s.map);
    if (!$("map-head").hidden && state.stations.length) renderMapHead();
    return s.static_loaded;
  }

  async function refreshProfile() {
    let p;
    try { p = await api("/api/settings"); } catch (e) { return; }
    const wasOnLeg = onLeg();
    const changed = JSON.stringify(p.leg) !== JSON.stringify(state.home.leg);
    state.home = p;
    if (changed && wasOnLeg && p.leg) goToLeg();  // follow the commute only if you were on it
    else updateHomeUi();
  }

  // ---------- boot ----------
  async function boot() {
    // Ask for everything at once (one round trip instead of four in a row);
    // only if the server is still downloading the schedule do we wait and ask again.
    const fetchAll = () => [
      api("/api/stations").catch(() => null),
      api("/api/settings").catch(() => null),
      api("/api/lines").catch(() => null),
    ];
    let pending = fetchAll();
    if (!(await refreshStatus())) {
      while (!(await refreshStatus())) {
        showEmpty("Downloading Metra schedule… this takes a few seconds on first start.");
        await new Promise((r) => setTimeout(r, 3000));
      }
      pending = fetchAll();
    }
    showEmpty("");
    let [stations, settings, lines] = await Promise.all(pending);
    if (!Array.isArray(stations) || !stations.length) stations = await api("/api/stations");
    state.stations = stations;
    state.stationById = new Map(stations.map((s) => [s.id, s]));
    if (settings) state.home = settings;
    else { try { state.home = await api("/api/settings"); } catch (e) { console.warn(e); } }
    const leg = state.home.leg;
    if (leg && state.stationById.has(leg.from)) {
      // Every device opens on the commute leg for right now (or home).
      state.station = leg.from;
      state.dest = leg.to || null;
    }
    if (state.dest && !state.stationById.has(state.dest)) state.dest = null;
    toPicker.sync();
    if (state.station && state.stationById.has(state.station)) selectStation(state.station, { pan: false });
    else { state.station = null; refreshDepartures(); }
    // The board comes first; the map gets its data right after.
    mapView.setStations(stations);
    if (!$("map-head").hidden) renderMapHead();
    if (lines) mapView.setLines(lines);
    else api("/api/lines").then((g) => mapView.setLines(g)).catch(console.warn);
    // Open the map framed on your commute rather than the whole system.
    syncMapPlaces().then(() => mapView.fitPlaces(false));
    refreshTrains();
    refreshAlerts();

    // Poll only while the app is on screen: a hidden tab or locked phone
    // uses no data. Coming back refreshes immediately.
    const tick = () => { refreshTrains(); refreshDepartures(); refreshAlerts(); };
    let timers = [];
    const start = () => {
      timers.forEach(clearInterval);
      timers = [
        setInterval(tick, state.interval * 1000),
        setInterval(renderBoard, 15000),  // keep "minutes until" current between polls
        setInterval(refreshStatus, 60000),
      ];
    };
    const stop = () => { timers.forEach(clearInterval); timers = []; };
    start();
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "visible") {
        refreshStatus();
        tick();
        start();
      } else {
        stop();
      }
    });
  }

  applyView();
  boot().catch((e) => { console.error(e); showEmpty("Something went wrong loading the app. Reload to retry."); });
})();
