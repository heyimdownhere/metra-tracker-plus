/* Metra Tracker+ frontend. Talks only to this server's /api endpoints. */
(function () {
  "use strict";

  const STATION_KEY = "metra.station";
  const THEME_KEY = "metra.theme";
  const DEST_KEY = "metra.dest";

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
    document.documentElement.dataset.theme = next;
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
    return `<div class="popup"><h3>${title}</h3><dl>${rows.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join("")}</dl></div>`;
  }

  const mapView = MetraMap.create({
    container: $("map"),
    onStationClick: (id) => selectStation(id, { pan: false }),
    popupHtml: trainPopup,
  });

  async function refreshTrains() {
    let data;
    try { data = await api("/api/trains"); } catch (e) { console.warn(e); return; }
    mapView.updateTrains(data);
    const n = data.trains.length;
    const note = $("map-note");
    note.textContent = `${n} train${n === 1 ? "" : "s"} · ${data.source === "schedule" ? "positions estimated from schedule" : "live positions"}`;
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

  function makePicker({ inputId, listId, getValue, onSelect, allowClear }) {
    const input = $(inputId);
    const list = $(listId);
    let matches = [];
    let active = -1;
    const current = () => { const st = state.stationById.get(getValue()); return st ? st.name : ""; };

    function open(isOpen) {
      list.hidden = !isOpen;
      input.setAttribute("aria-expanded", String(isOpen));
    }
    function render() {
      matches = stationMatches(input.value).slice(0, 80);
      active = matches.length ? 0 : -1;
      const clear = allowClear && getValue() ? '<li role="option" data-id="" class="clear-opt">Any destination</li>' : "";
      list.innerHTML = clear + (matches.length
        ? matches.map((s, i) => `<li role="option" id="${listId}-${i}" data-id="${esc(s.id)}" aria-selected="${i === active}"><span>${esc(s.name)}</span><span class="chips">${routeChips(s.routes)}</span></li>`).join("")
        : '<li class="empty">No matching stations</li>');
      open(true);
    }
    function setActive(i) {
      if (!matches.length) return;
      active = (i + matches.length) % matches.length;
      list.querySelectorAll("li[id]").forEach((li, j) => li.setAttribute("aria-selected", String(j === active)));
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
    return { sync: () => { input.value = current(); }, open: () => input.focus() };
  }

  const fromPicker = makePicker({
    inputId: "station-input", listId: "station-list",
    getValue: () => state.station,
    onSelect: (id) => selectStation(id),
  });
  const toPicker = makePicker({
    inputId: "dest-input", listId: "dest-list", allowClear: true,
    getValue: () => state.dest,
    onSelect: (id) => selectDest(id),
  });

  $("swap-btn").addEventListener("click", () => {
    if (!state.station || !state.dest) return;
    const from = state.station;
    state.station = state.dest;
    state.dest = from;
    localStorage.setItem(STATION_KEY, state.station);
    localStorage.setItem(DEST_KEY, state.dest);
    fromPicker.sync();
    toPicker.sync();
    updateHomeUi();
    refreshDepartures();
  });

  function selectStation(id, opts = {}) {
    const st = state.stationById.get(id);
    if (!st) return;
    state.station = id;
    localStorage.setItem(STATION_KEY, id);
    if (state.dest === id) selectDest("", { refresh: false });
    fromPicker.sync();
    $("board-title").textContent = st.name;
    updateHomeUi();
    renderAlerts();
    if (opts.pan !== false) mapView.flyTo(st.lat, st.lon);
    refreshDepartures();
  }

  function selectDest(id, opts = {}) {
    state.dest = id && state.stationById.has(id) ? id : null;
    if (state.dest) localStorage.setItem(DEST_KEY, state.dest); else localStorage.removeItem(DEST_KEY);
    toPicker.sync();
    updateHomeUi();
    renderAlerts();
    if (opts.refresh !== false) refreshDepartures();
  }

  // ---------- home station ----------
  const isHome = () => !!state.home.home_station
    && state.station === state.home.home_station
    && (state.dest || null) === (state.home.home_dest || null);

  function updateHomeUi() {
    const btn = $("home-btn");
    const go = $("go-home-btn");
    btn.hidden = !state.station;
    btn.classList.toggle("is-home", isHome());
    $("home-btn-text").textContent = isHome() ? "Home" : "Set as home";
    btn.title = isHome() ? "This is your home station. Click to remove it." : "Open the app on this station (and destination) on every device";
    go.hidden = !state.home.home_station || isHome();
  }

  async function saveHome(values) {
    try {
      const r = await fetch("/api/settings", {
        method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(values),
      });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      state.home = await r.json();
    } catch (e) {
      console.warn(e);
      alert("Couldn't save the home station. Is the server's /config folder writable?");
    }
    updateHomeUi();
    renderBoard();
  }

  $("home-btn").addEventListener("click", () => {
    if (isHome()) {
      if (confirm("Remove your home station?")) saveHome({ home_station: null, home_dest: null });
    } else {
      saveHome({ home_station: state.station, home_dest: state.dest || null });
    }
  });

  $("go-home-btn").addEventListener("click", () => {
    selectDest(state.home.home_dest || "", { refresh: false });
    selectStation(state.home.home_station);
  });

  // ---------- departure board ----------
  async function refreshDepartures() {
    if (!state.station) {
      $("board-title").textContent = "Choose a station";
      $("board-list").innerHTML = "";
      showEmpty("Search for a station above, or tap one on the map.");
      return;
    }
    try {
      const to = state.dest ? `&to=${encodeURIComponent(state.dest)}` : "";
      state.departures = await api(`/api/departures?station=${encodeURIComponent(state.station)}${to}&limit=5`);
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

  function renderBoard() {
    const d = state.departures;
    if (!d) return;
    const now = serverNow();
    const from = d.station ? d.station.name : "";
    $("board-title").innerHTML = esc(d.to ? `${from} → ${d.to.name}` : from) + (isHome() ? '<span class="home-tag">HOME</span>' : "");
    $("board-updated").textContent = `Updated ${fmtTime(d.generated_at)}`;
    if (!d.departures.length) {
      $("board-list").innerHTML = "";
      showEmpty(d.to
        ? `No upcoming trains from ${from} to ${d.to.name}. They may be on different lines; try "Any destination".`
        : "No upcoming departures found in the schedule.");
      return;
    }
    showEmpty("");
    $("board-list").innerHTML = d.departures.map((r) => {
      const mins = Math.max(0, Math.round((r.time - now) / 60));
      const minsHtml = r.cancelled ? "<b>—</b>" : mins === 0 ? "<b>Now</b>" : `<b>${mins}</b><span>min</span>`;
      const changed = r.scheduled && Math.abs(r.time - r.scheduled) >= 60;
      const badge = r.cancelled ? '<span class="badge cancel">Cancelled</span>'
        : r.live ? '<span class="badge live">Live</span>'
        : r.tracking ? '<span class="badge tracking" title="Metra sent no prediction for this train; delay estimated from its GPS position">Tracking</span>'
        : '<span class="badge sched">Scheduled</span>';
      const meta = [
        chip(r.route_short, r.color, r.text_color),
        badge,
        r.train ? `<span>#${esc(r.train)}</span>` : "",
        (r.live || r.tracking) && !r.cancelled ? delayHtml(r.delay, r.tracking) : "",
      ].filter(Boolean).join("");
      return `<li class="dep${r.cancelled ? " cancelled" : ""}" style="--line:${esc(r.color)}">
        <div class="timecol"><div class="time">${bigTime(r.time)}${changed ? `<span class="sched">${fmtTime(r.scheduled)}</span>` : ""}</div></div>
        <div class="main"><div class="dest" title="${esc(r.destination)}">${esc(r.destination || r.route_name)}</div>
        <div class="meta">${meta}</div>
        ${r.arrival ? `<div class="arrive">Arrives ${esc(d.to.name)} ${timeChange(r.arrival_scheduled, r.arrival)} · ${Math.round((r.arrival - r.time) / 60)} min trip</div>` : ""}
        ${finalHtml(r, d)}
        ${historyHtml(r.history)}
        ${r.delay_reason ? `<div class="reason" title="${esc(r.delay_reason)}">${esc(r.delay_reason)}</div>` : ""}</div>
        <div class="mins">${minsHtml}</div>
      </li>`;
    }).join("");
  }

  // ---------- alerts ----------
  $("alerts-toggle").addEventListener("click", () => {
    const listEl = $("alerts-list");
    listEl.hidden = !listEl.hidden;
    $("alerts-toggle").setAttribute("aria-expanded", String(!listEl.hidden));
  });

  async function refreshAlerts() {
    let alerts;
    try { alerts = await api("/api/alerts"); } catch (e) { console.warn(e); return; }
    state.alerts = alerts;
    renderAlerts();
  }

  // The banner only features alerts for lines you use (the selected station,
  // its destination and your home station); everything else sits behind a
  // quiet "N alerts on other lines".
  function renderAlerts() {
    const alerts = state.alerts || [];
    const box = $("alerts");
    if (!alerts.length) { box.hidden = true; return; }
    const myLines = new Set();
    for (const id of [state.station, state.dest, state.home.home_station, state.home.home_dest]) {
      const st = state.stationById.get(id);
      if (st) st.routes.forEach((r) => myLines.add(r));
    }
    const isMine = (a) => a.routes.some((r) => myLines.has(r.id));
    const mine = alerts.filter(isMine);
    const other = alerts.filter((a) => !isMine(a));
    const item = (a) => `<li>
      <div>${a.routes.map((r) => chip(r.short_name, r.color, r.text_color)).join(" ")}</div>
      <h3>${esc(a.header)}</h3>
      ${a.description && a.description !== a.header ? `<p>${esc(a.description)}</p>` : ""}
      ${a.url ? `<p><a href="${esc(a.url)}" target="_blank" rel="noopener noreferrer">More info</a></p>` : ""}
    </li>`;
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
    $("alerts-list").innerHTML = mine.map(item).join("")
      + (other.length && mine.length ? '<li class="alerts-divider">Other lines</li>' : "")
      + other.map(item).join("");
  }

  // ---------- status ----------
  async function refreshStatus() {
    try { state.status = await api("/api/status"); } catch (e) { return false; }
    const s = state.status;
    if (s.timezone !== state.tz) { state.tz = s.timezone; timeFmt = null; }
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
    return s.static_loaded;
  }

  // ---------- boot ----------
  async function boot() {
    // Wait for the server to finish its first schedule download.
    while (!(await refreshStatus())) {
      showEmpty("Downloading Metra schedule… this takes a few seconds on first start.");
      await new Promise((r) => setTimeout(r, 3000));
    }
    showEmpty("");
    const [stations] = await Promise.all([
      api("/api/stations"),
      api("/api/lines").then((g) => mapView.setLines(g)).catch(console.warn),
    ]);
    state.stations = stations;
    state.stationById = new Map(stations.map((s) => [s.id, s]));
    mapView.setStations(stations);
    try {
      state.home = await api("/api/settings");
    } catch (e) { console.warn(e); }
    if (state.home.home_station && state.stationById.has(state.home.home_station)) {
      // Every device opens on the home station.
      state.station = state.home.home_station;
      state.dest = state.home.home_dest || null;
    }
    if (state.dest && !state.stationById.has(state.dest)) state.dest = null;
    toPicker.sync();
    if (state.station && state.stationById.has(state.station)) selectStation(state.station, { pan: false });
    else { state.station = null; refreshDepartures(); }
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

  boot().catch((e) => { console.error(e); showEmpty("Something went wrong loading the app. Reload to retry."); });
})();
