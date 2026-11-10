/* Map for Metra Tracker+: MapLibre GL with switchable basemaps, opacity
 * controls, 3D buildings and layer toggles. Exposes window.MetraMap. */
(function () {
  "use strict";

  const SETTINGS_KEY = "metra.map";
  const DEFAULTS = {
    basemap: "simple",     // simple | streets | light | dark | satellite
    basemapOpacity: 100,
    rail: false,           // OpenRailwayMap track overlay
    railOpacity: 70,
    buildings: true,
    buildingOpacity: 70,
    lineOpacity: 90,
    lineWidth: 4,
    stations: true,
    trains: true,
    lines: true,
  };

  // Backgrounds. "simple" is the app's own background: no outside map service,
  // no key and no extra data. The others are free and need no API key:
  // OpenFreeMap vector styles and Esri satellite imagery. Each style URL list
  // is tried in order; if all fail, the map falls back to "simple".
  const OFM = (name) => `https://tiles.openfreemap.org/styles/${name}`;
  const BASEMAPS = {
    simple: { label: "Simple (no map data)" },
    streets: { label: "Streets", styles: [OFM("liberty"), OFM("bright")], light: true },
    light: { label: "Light", styles: [OFM("positron")], light: true },
    dark: { label: "Dark", styles: [OFM("dark"), OFM("fiord")] },
    satellite: {
      label: "Satellite",
      raster: {
        tiles: ["https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}"],
        attribution: "Imagery &copy; Esri, Maxar, Earthstar Geographics",
      },
    },
  };
  // Railway infrastructure overlay (tracks, yards, stations) from OpenStreetMap.
  const RAIL_OVERLAY = {
    tiles: ["a", "b", "c"].map((s) => `https://${s}.tiles.openrailwaymap.org/standard/{z}/{x}/{y}.png`),
    attribution: 'Rail overlay: <a href="https://www.openrailwaymap.org/" target="_blank" rel="noopener">OpenRailwayMap</a> (CC-BY-SA)',
  };
  // OpenMapTiles-schema vector tiles (free, no API key) used only for 3D buildings.
  const LOOP = [-87.6345, 41.8812];
  const BUILDINGS_TILEJSON = "https://tiles.openfreemap.org/planet";
  const BUILDINGS_ATTR = '<a href="https://openfreemap.org" target="_blank" rel="noopener">OpenFreeMap</a> &copy; <a href="https://www.openmaptiles.org/" target="_blank" rel="noopener">OpenMapTiles</a>';

  function loadSettings() {
    let saved = {};
    try { saved = JSON.parse(localStorage.getItem(SETTINGS_KEY) || "{}"); } catch (e) { /* ignore */ }
    const s = { ...DEFAULTS, ...saved };
    // Older versions had "auto"/"none" (and CARTO maps that now need a key).
    if (!BASEMAPS[s.basemap] || s.basemap === "auto" || s.basemap === "none") s.basemap = "simple";
    return s;
  }

  function isLight() { return document.documentElement.dataset.theme === "light"; }

  function createMap({ container, onStationClick, popupHtml }) {
    const settings = loadSettings();
    const save = () => localStorage.setItem(SETTINGS_KEY, JSON.stringify(settings));

    const themeBg = () => (isLight() ? "#eef1f5" : "#0f1216");

    // A style made only of our background (plus satellite imagery if chosen).
    function ownStyle(name) {
      const style = { version: 8, sources: {}, layers: [{ id: "bg", type: "background", paint: { "background-color": themeBg() } }] };
      const r = BASEMAPS[name] && BASEMAPS[name].raster;
      if (r) {
        style.sources.sat = { type: "raster", tiles: r.tiles, tileSize: 256, attribution: r.attribution, maxzoom: 19 };
        style.layers.push({ id: "sat", type: "raster", source: "sat" });
      }
      return style;
    }

    const map = new maplibregl.Map({
      container,
      style: ownStyle("simple"),
      center: [-87.85, 41.88],
      zoom: 8.6,
      maxPitch: 70,
      attributionControl: false,
      dragRotate: true,
      pitchWithRotate: true,
    });
    map.addControl(new maplibregl.NavigationControl({ visualizePitch: true }), "top-left");
    map.addControl(new maplibregl.AttributionControl({ compact: true, customAttribution: "Data: Metra" }), "bottom-right");
    map.addControl(new maplibregl.GeolocateControl({ positionOptions: { enableHighAccuracy: false }, trackUserLocation: false }), "top-left");

    let markReady;
    const ready = new Promise((resolve) => { markReady = resolve; });
    const trainMarkers = new Map();
    let lastTrains = null;
    let buildingsAdded = false;
    // Overlay data is kept here so it can be re-added after a background change
    // (switching styles replaces every source and layer).
    let linesData = { type: "FeatureCollection", features: [] };
    let stationsData = { type: "FeatureCollection", features: [] };
    let currentBase = "simple";
    let attempt = null;  // {name, index, url, timer} while a background style loads

    // ---- apply settings ----------------------------------------------------
    function activeBasemap() { return currentBase; }

    function applyBasemap() {
      if (map.getLayer("bg")) map.setPaintProperty("bg", "background-color", themeBg());
      if (map.getLayer("base-dim")) {
        // Basemap opacity = fade the background toward the app's own colour.
        map.setPaintProperty("base-dim", "background-color", themeBg());
        map.setPaintProperty("base-dim", "background-opacity", 1 - settings.basemapOpacity / 100);
      }
      if (map.getLayer("rail")) {
        map.setLayoutProperty("rail", "visibility", settings.rail ? "visible" : "none");
        map.setPaintProperty("rail", "raster-opacity", settings.railOpacity / 100);
      }
      if (map.getLayer("buildings-3d")) {
        map.setPaintProperty("buildings-3d", "fill-extrusion-color", buildingColor());
      }
    }

    // Switch the background. Overlays are re-added on "style.load".
    function setBasemap(name, index = 0) {
      if (attempt) clearTimeout(attempt.timer);
      attempt = null;
      const b = BASEMAPS[name] || BASEMAPS.simple;
      if (!b.styles) {
        currentBase = BASEMAPS[name] ? name : "simple";
        map.setStyle(ownStyle(currentBase), { diff: false });
        return;
      }
      if (index >= b.styles.length) {
        console.warn(`map: "${name}" background unavailable; using Simple`);
        showNote(`${b.label} map couldn't load; showing Simple instead`);
        setBasemap("simple");
        return;
      }
      currentBase = name;
      const url = b.styles[index];
      attempt = { name, index, url, timer: setTimeout(() => fallback(url), 12000) };
      map.setStyle(url, { diff: false });
    }

    function fallback(url) {
      if (!attempt || attempt.url !== url) return;
      const { name, index } = attempt;
      setBasemap(name, index + 1);
    }

    function showNote(text) {
      const note = document.getElementById("map-note");
      if (!note) return;
      note.textContent = text;
      note.hidden = false;
    }

    function buildingColor() {
      const active = activeBasemap();
      const light = (BASEMAPS[active] && BASEMAPS[active].light) || (active === "simple" && isLight());
      return [
        "interpolate", ["linear"], ["coalesce", ["get", "render_height"], 0],
        0, light ? "#d6dbe2" : "#2a3340",
        150, light ? "#b7c0cc" : "#3d4a5c",
      ];
    }

    function ensureBuildings() {
      if (buildingsAdded) return;
      buildingsAdded = true;
      map.addSource("omt", { type: "vector", url: BUILDINGS_TILEJSON, attribution: BUILDINGS_ATTR });
      map.addLayer({
        id: "buildings-3d",
        type: "fill-extrusion",
        source: "omt",
        "source-layer": "building",
        minzoom: 13,
        paint: {
          "fill-extrusion-color": buildingColor(),
          "fill-extrusion-height": ["interpolate", ["linear"], ["zoom"], 13, 0, 14.5, ["coalesce", ["get", "render_height"], 6]],
          "fill-extrusion-base": ["coalesce", ["get", "render_min_height"], 0],
          "fill-extrusion-opacity": settings.buildingOpacity / 100,
        },
      }, map.getLayer("lines") ? "lines" : undefined);
    }

    function applyBuildings(animate) {
      if (settings.buildings) ensureBuildings();
      if (map.getLayer("buildings-3d")) {
        map.setLayoutProperty("buildings-3d", "visibility", settings.buildings ? "visible" : "none");
        map.setPaintProperty("buildings-3d", "fill-extrusion-opacity", settings.buildingOpacity / 100);
      }
      if (animate) {
        if (settings.buildings && map.getPitch() < 20) {
          // Zoomed out there is nothing to extrude; show off downtown instead.
          if (map.getZoom() < 12) map.flyTo({ center: LOOP, zoom: 15, pitch: 55, bearing: -20, duration: 1500 });
          else map.easeTo({ pitch: 50, zoom: Math.max(map.getZoom(), 14), duration: 900 });
        }
        if (!settings.buildings && map.getPitch() > 0) map.easeTo({ pitch: 0, bearing: 0, duration: 700 });
      }
    }

    function applyOverlays() {
      if (map.getLayer("lines")) {
        map.setLayoutProperty("lines", "visibility", settings.lines ? "visible" : "none");
        map.setPaintProperty("lines", "line-opacity", settings.lineOpacity / 100);
        map.setPaintProperty("lines", "line-width", ["interpolate", ["linear"], ["zoom"], 8, settings.lineWidth * 0.6, 14, settings.lineWidth * 1.6]);
      }
      if (map.getLayer("stations")) map.setLayoutProperty("stations", "visibility", settings.stations ? "visible" : "none");
      container.classList.toggle("hide-trains", !settings.trains);
    }

    function applyAll(animate) {
      applyBasemap();
      applyBuildings(animate);
      applyOverlays();
    }

    function addOverlays() {
      if (attempt) { clearTimeout(attempt.timer); attempt = null; }
      buildingsAdded = false;
      if (currentBase !== "simple") {
        // Sits on top of the background; its opacity implements "Basemap opacity".
        map.addLayer({ id: "base-dim", type: "background", paint: { "background-color": themeBg(), "background-opacity": 0 } });
      }
      map.addSource("rail", { type: "raster", tiles: RAIL_OVERLAY.tiles, tileSize: 256, attribution: RAIL_OVERLAY.attribution, maxzoom: 19 });
      map.addLayer({ id: "rail", type: "raster", source: "rail", layout: { visibility: "none" } });
      map.addSource("lines", { type: "geojson", data: linesData });
      map.addLayer({
        id: "lines", type: "line", source: "lines",
        layout: { "line-cap": "round", "line-join": "round" },
        paint: { "line-color": ["get", "color"], "line-width": 4, "line-opacity": 0.9 },
      });
      map.addSource("stations", { type: "geojson", data: stationsData });
      map.addLayer({
        id: "stations", type: "circle", source: "stations",
        paint: {
          "circle-radius": ["interpolate", ["linear"], ["zoom"], 8, 2.5, 13, 6],
          "circle-color": "#30363d",
          "circle-stroke-color": "#ffffff",
          "circle-stroke-width": ["interpolate", ["linear"], ["zoom"], 8, 1.2, 13, 2.5],
          "circle-pitch-alignment": "map",
        },
      });
      applyAll(false);
      markReady();
    }
    map.on("style.load", addOverlays);

    // Station hover/click: bound once, by layer id, so they survive style changes.
    const hover = new maplibregl.Popup({ closeButton: false, closeOnClick: false, offset: 10, className: "hover-popup" });
    map.on("mouseenter", "stations", (e) => {
      map.getCanvas().style.cursor = "pointer";
      const f = e.features[0];
      hover.setLngLat(f.geometry.coordinates).setText(f.properties.name).addTo(map);
    });
    map.on("mouseleave", "stations", () => { map.getCanvas().style.cursor = ""; hover.remove(); });
    map.on("click", "stations", (e) => onStationClick(e.features[0].properties.id));
    map.on("error", (e) => {
      const err = e && e.error;
      console.warn("map:", err ? err.message : e);
      // A background style that fails to load: try the next one / fall back.
      if (attempt && err && (err.url === attempt.url || String(err.message || "").includes(attempt.url))) fallback(attempt.url);
    });
    // Load the saved background once the map exists.
    ready.then(() => { if (settings.basemap !== "simple") setBasemap(settings.basemap); });

    // ---- settings panel ----------------------------------------------------
    function buildPanel() {
      const btn = document.getElementById("map-settings-btn");
      const panel = document.getElementById("map-settings");
      const basemapOpts = Object.entries(BASEMAPS).map(([k, b]) => [k, b.label]);
      panel.innerHTML = `
        <h3>Map</h3>
        <label class="row"><span>Basemap</span>
          <select data-k="basemap">${basemapOpts.map(([v, l]) => `<option value="${v}">${l}</option>`).join("")}</select></label>
        <label class="row slider"><span>Basemap opacity</span><input type="range" min="0" max="100" step="5" data-k="basemapOpacity"><output></output></label>
        <label class="row check"><input type="checkbox" data-k="rail"><span>Railway tracks overlay</span></label>
        <label class="row slider"><span>Tracks opacity</span><input type="range" min="10" max="100" step="5" data-k="railOpacity"><output></output></label>
        <label class="row check"><input type="checkbox" data-k="buildings"><span>3D buildings</span></label>
        <label class="row slider"><span>Building opacity</span><input type="range" min="10" max="100" step="5" data-k="buildingOpacity"><output></output></label>
        <h3>Metra</h3>
        <label class="row check"><input type="checkbox" data-k="lines"><span>Lines</span></label>
        <label class="row slider"><span>Line opacity</span><input type="range" min="10" max="100" step="5" data-k="lineOpacity"><output></output></label>
        <label class="row slider"><span>Line width</span><input type="range" min="1" max="10" step="1" data-k="lineWidth"><output></output></label>
        <label class="row check"><input type="checkbox" data-k="stations"><span>Stations</span></label>
        <label class="row check"><input type="checkbox" data-k="trains"><span>Trains</span></label>
        <div class="btns">
          <button type="button" data-act="reset-view">Reset view</button>
          <button type="button" data-act="defaults">Defaults</button>
        </div>
        <p class="hint">Right-drag (or two-finger drag) to tilt and rotate.</p>`;

      const sync = () => {
        panel.querySelectorAll("[data-k]").forEach((el) => {
          const v = settings[el.dataset.k];
          if (el.type === "checkbox") el.checked = !!v; else el.value = v;
          const out = el.parentElement.querySelector("output");
          if (out) out.textContent = el.dataset.k === "lineWidth" ? `${v}px` : `${v}%`;
        });
        // Nothing to fade on the Simple background.
        const op = panel.querySelector('[data-k="basemapOpacity"]');
        op.disabled = settings.basemap === "simple";
        op.closest(".row").classList.toggle("disabled", op.disabled);
      };
      sync();
      panel.addEventListener("input", (e) => {
        const el = e.target.closest("[data-k]");
        if (!el) return;
        const k = el.dataset.k;
        settings[k] = el.type === "checkbox" ? el.checked : el.type === "range" ? Number(el.value) : el.value;
        save();
        sync();
        if (k === "basemap") ready.then(() => setBasemap(settings.basemap));
        else ready.then(() => applyAll(k === "buildings"));
      });
      panel.addEventListener("click", (e) => {
        const act = e.target.closest("[data-act]");
        if (!act) return;
        if (act.dataset.act === "reset-view") { map.easeTo({ pitch: 0, bearing: 0, duration: 600 }); fitLines(); }
        if (act.dataset.act === "defaults") {
          const baseChanged = settings.basemap !== DEFAULTS.basemap;
          Object.assign(settings, DEFAULTS); save(); sync();
          ready.then(() => (baseChanged ? setBasemap(settings.basemap) : applyAll(false)));
        }
      });
      const toggle = (open) => { panel.hidden = !open; btn.setAttribute("aria-expanded", String(open)); };
      btn.addEventListener("click", () => toggle(panel.hidden));
      document.addEventListener("keydown", (e) => { if (e.key === "Escape") toggle(false); });
      map.on("mousedown", () => toggle(false));
      map.on("touchstart", () => toggle(false));
    }
    buildPanel();

    // ---- data --------------------------------------------------------------
    let linesBounds = null;
    function fitLines() {
      if (linesBounds) map.fitBounds(linesBounds, { padding: 30, duration: 600 });
    }

    async function setLines(geo) {
      linesData = geo;
      await ready;
      if (map.getSource("lines")) map.getSource("lines").setData(geo);
      const b = new maplibregl.LngLatBounds();
      for (const f of geo.features) for (const c of f.geometry.coordinates) b.extend(c);
      if (!b.isEmpty()) { linesBounds = b; map.fitBounds(b, { padding: 20, duration: 0 }); }
    }

    async function setStations(list) {
      stationsData = {
        type: "FeatureCollection",
        features: list.map((s) => ({ type: "Feature", properties: { id: s.id, name: s.name }, geometry: { type: "Point", coordinates: [s.lon, s.lat] } })),
      };
      await ready;
      if (map.getSource("stations")) map.getSource("stations").setData(stationsData);
    }

    function trainEl(t) {
      const el = document.createElement("div");
      el.className = "train-marker";
      el.setAttribute("role", "button");
      el.setAttribute("aria-label", `${t.route_short || "Train"} to ${t.destination || "unknown"}`);
      styleTrainEl(el, t);
      return el;
    }
    function styleTrainEl(el, t) {
      const rot = t.bearing == null ? null : `${t.bearing}deg`;
      el.innerHTML = `<div class="train-dot${t.live ? "" : " est"}${t.delay >= 360 ? " late" : ""}" style="--c:${t.color}">${rot ? `<span class="arrow" style="--rot:${rot}"></span>` : ""}</div>`;
    }

    function updateTrains(data) {
      lastTrains = data;
      const seen = new Set();
      for (const t of data.trains) {
        seen.add(t.id);
        let m = trainMarkers.get(t.id);
        if (!m) {
          const popup = new maplibregl.Popup({ offset: 14, maxWidth: "280px" }).setHTML(popupHtml(t));
          m = new maplibregl.Marker({ element: trainEl(t), rotationAlignment: "viewport", pitchAlignment: "viewport" })
            .setLngLat([t.lon, t.lat]).setPopup(popup).addTo(map);
          trainMarkers.set(t.id, m);
        } else {
          m.setLngLat([t.lon, t.lat]);
          styleTrainEl(m.getElement(), t);
          m.getPopup().setHTML(popupHtml(t));
        }
      }
      for (const [id, m] of trainMarkers) {
        if (!seen.has(id)) { m.remove(); trainMarkers.delete(id); }
      }
    }

    // ---- home / work / current station markers ----------------------------
    const HOUSE = '<path d="M12 3 2 11.5h3V21h5.5v-6h3v6H19v-9.5h3L12 3Z"/>';
    const CASE = '<path d="M9 3h6a2 2 0 0 1 2 2v2h3a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V9a2 2 0 0 1 2-2h3V5a2 2 0 0 1 2-2Zm0 4h6V5H9v2Z"/>';
    const placeMarkers = {};
    let places = {};

    function placeEl(kind, label, name) {
      const el = document.createElement("div");
      el.className = `place-marker place-${kind}`;
      el.setAttribute("role", "button");
      el.setAttribute("aria-label", `${label}: ${name}`);
      if (kind === "current") {
        el.innerHTML = '<span class="place-ring"></span>';
      } else {
        el.innerHTML = `<span class="place-pin"><svg viewBox="0 0 24 24" aria-hidden="true">${kind === "home" ? HOUSE : CASE}</svg></span>`
          + `<span class="place-label"><b>${label}</b> ${name.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]))}</span>`;
      }
      return el;
    }

    /** places: {home: {id,name,lat,lon}|null, work: ...|null, current: ...|null} */
    async function setPlaces(next) {
      await ready;
      for (const kind of ["current", "home", "work"]) {
        const p = next[kind];
        // The ring only marks a station that isn't already shown as home/work.
        const hidden = !p || (kind === "current" && [next.home, next.work].some((q) => q && q.id === p.id));
        const prev = places[kind];
        if (hidden) {
          if (placeMarkers[kind]) { placeMarkers[kind].remove(); delete placeMarkers[kind]; }
          continue;
        }
        if (placeMarkers[kind] && prev && prev.id === p.id) continue;
        if (placeMarkers[kind]) placeMarkers[kind].remove();
        const label = kind === "home" ? "Home" : kind === "work" ? "Work" : "";
        const el = placeEl(kind, label, p.name);
        el.addEventListener("click", (e) => { e.stopPropagation(); onStationClick(p.id); });
        placeMarkers[kind] = new maplibregl.Marker({
          element: el, anchor: kind === "current" ? "center" : "bottom", pitchAlignment: "viewport", rotationAlignment: "viewport",
        }).setLngLat([p.lon, p.lat]).addTo(map);
      }
      places = next;
      commuteCtl.update();
    }

    /** Frame home and work (or whichever is set). Returns false if neither is set. */
    function fitPlaces(animate = true) {
      const pts = [places.home, places.work].filter(Boolean);
      if (!pts.length) return false;
      if (pts.length === 1) {
        map.flyTo({ center: [pts[0].lon, pts[0].lat], zoom: 12.5, duration: animate ? 800 : 0 });
      } else {
        const b = new maplibregl.LngLatBounds();
        pts.forEach((p) => b.extend([p.lon, p.lat]));
        // Room for the pins + labels above each point, and the info bar at the bottom.
        const h = map.getContainer().clientHeight;
        const pad = { top: Math.min(110, h * 0.25), bottom: Math.min(80, h * 0.2), left: 70, right: 70 };
        map.fitBounds(b, { padding: pad, maxZoom: 13, duration: animate ? 800 : 0 });
      }
      return true;
    }

    // Map button: jump back to your commute.
    const commuteCtl = {
      el: null,
      onAdd() {
        const wrap = document.createElement("div");
        wrap.className = "maplibregl-ctrl maplibregl-ctrl-group";
        wrap.innerHTML = `<button type="button" class="commute-ctl" title="Show my commute" aria-label="Show my commute"><svg viewBox="0 0 24 24" aria-hidden="true">${HOUSE}</svg></button>`;
        wrap.querySelector("button").addEventListener("click", () => fitPlaces(true));
        this.el = wrap;
        this.update();
        return wrap;
      },
      onRemove() { this.el.remove(); },
      update() { if (this.el) this.el.hidden = !(places.home || places.work); },
    };
    map.addControl(commuteCtl, "top-left");

    return {
      map,
      setLines,
      setStations,
      updateTrains,
      setPlaces,
      fitPlaces,
      flyTo(lat, lon) { map.flyTo({ center: [lon, lat], zoom: Math.max(map.getZoom(), 12.5), duration: 800 }); },
      onThemeChange() { ready.then(() => applyBasemap()); },
      get lastTrains() { return lastTrains; },
    };
  }

  window.MetraMap = { create: createMap };
})();
