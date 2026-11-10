/* Metra Tracker+ frontend. Talks only to this server's /api endpoints. */
(function () {
  "use strict";

  const STATION_KEY = "metra.station";
  const THEME_KEY = "metra.theme";
  const DEST_KEY = "metra.dest";
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

  function makePicker({ inputId, listId, getValue, onSelect, allowClear, clearLabel = "Any destination" }) {
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
      const clear = allowClear && getValue() ? `<li role="option" data-id="" class="clear-opt">${esc(clearLabel)}</li>` : "";
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
    homeBtn.hidden = workBtn.hidden = !state.station;
    const atHome = !!p.home_station && state.station === p.home_station;
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

  $("commute-btn").addEventListener("click", openCommute);
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
    const tag = viewTag();
    $("board-title").innerHTML = esc(d.to ? `${from} → ${d.to.name}` : from) + (tag ? `<span class="home-tag">${tag}</span>` : "");
    $("board-updated").textContent = `Updated ${fmtTime(d.generated_at)}`;
    if (!d.departures.length) {
      $("board-list").innerHTML = "";
      showEmpty(d.to
        ? `No upcoming trains from ${from} to ${d.to.name}. They may be on different lines; try "Any destination".`
        : "No upcoming departures found in the schedule.");
      return;
    }
    showEmpty("");
    $("board-list").innerHTML = setupCardHtml() + d.departures.map((r) => {
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

  // ---------- display options (this device only) ----------
  const view = { pickers: "show", alerts: "bell", alertLines: "all" };
  try { Object.assign(view, JSON.parse(localStorage.getItem(VIEW_KEY) || "{}")); } catch (e) { /* ignore */ }
  // Earlier versions stored the line choice in "alerts".
  if (view.alerts === "all" || view.alerts === "mine") { view.alertLines = view.alerts; view.alerts = "bell"; }
  let pickersOpen = false;  // "Change" shows hidden pickers for a moment

  function applyView() {
    const show = view.pickers !== "hide" || pickersOpen;
    $("picker").hidden = !show;
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

  $("view-btn").addEventListener("click", () => {
    for (const k of ["pickers", "alerts", "alertLines"]) {
      document.querySelectorAll(`#view-form input[name=${k}]`).forEach((r) => { r.checked = r.value === view[k]; });
    }
    $("view-dialog").showModal();
  });
  $("view-form").addEventListener("change", (e) => {
    if (!e.target.name) return;
    view[e.target.name] = e.target.value;
    localStorage.setItem(VIEW_KEY, JSON.stringify(view));
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
  const bellOpen = () => !$("bell-panel").hidden;
  function setBell(open) {
    const wasOpen = bellOpen();
    $("bell-panel").hidden = !open;
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
    const fresh = (a) => (dismissed.has(a.id) ? 1 : 0);
    const mine = alerts.filter(isMine).sort((x, y) => fresh(x) - fresh(y));
    const other = view.alertLines === "mine" ? [] : alerts.filter((a) => !isMine(a));
    bannerIds = [...mine, ...other].map((a) => a.id);
    const item = (a) => `<li class="${dismissed.has(a.id) ? "" : "unseen"}">
      <div>${a.routes.map((r) => chip(r.short_name, r.color, r.text_color)).join(" ")}</div>
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
    if (s.static_loaded && state.stations.length) refreshProfile();
    mapView.setMapStatus(s.map);
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
