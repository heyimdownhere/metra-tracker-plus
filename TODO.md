# Metra Tracker+: what's left

Updated 2026-11-09. The app runs against Metra's **real** schedule and **live** feeds (tested with the real API key). 40 tests pass. CI is green and publishes a ready-to-run Docker image on every push to `main`. What's left is deployment, real-world checks, and new features.

## 1. Deploy on the server (SERVER-IP): do first

- [ ] Add the template: in the Unraid terminal run `wget -O /boot/config/plugins/dockerMan/templates-user/my-metra-tracker-plus.xml https://raw.githubusercontent.com/heyimdownhere/metra-tracker-plus/main/unraid/metra-tracker-plus.xml` (see README → Install on Unraid).
- [ ] In Unraid, go to **Docker → Add Container** and choose **metra-tracker-plus** from the template list. The template already points at the published image (`ghcr.io/heyimdownhere/metra-tracker-plus:latest`, verified publicly pullable), so nothing needs building.
- [ ] Fill in the template fields:
  - **Metra API key** (masked).
  - Optional: **Home station**, **Work station**, **Commute switch time**, **Workdays**. These can be left empty and set in the app instead.
  - Leave **Web UI port** at 8080 unless that port is taken.
- [ ] Click **Apply**, then check the container log. It starts with a settings summary (API key masked) and warns about any value it couldn't use.
- [ ] Open `http://SERVER-IP:8080`. Check that the status pill shows **Live**, the WebUI button works, and Docker health shows **healthy**.
- [ ] If home and work weren't set in Unraid, use the **Set up your commute** card or the briefcase button.
- [ ] Check that `/mnt/user/appdata/metra-tracker/` contains `gtfs/`, `history.db` and `settings.json`, and that a restart is fast.

## 2. Check in real use

Map tiles were blocked in the dev sandbox, so these have never been seen drawing:

- [ ] **Self-hosted map:** after deploying, watch the container log for "Map: extracting Chicago area…" then "Map: saved N MB". The layers panel shows progress. This first real download is untested: the dev sandbox couldn't reach Protomaps, though the whole pipeline was tested against a local test archive. If the latest-build lookup fails, set `MAP_SOURCE` to a specific `https://build.protomaps.com/YYYYMMDD.pmtiles`.
- [ ] Check the map looks right (labels, icons, light/dark), how big the file is, and 3D buildings from the local map (their heights come from the map's `height` field; unverified).
- [ ] Online options and fallbacks: Streets (OpenFreeMap), Satellite (Esri), tracks overlay (OpenRailwayMap). The fallback to Simple was tested; the online maps themselves couldn't be reached from the sandbox.
- [ ] 3D buildings (OpenFreeMap) in the Loop, in dark and light mode, and phone performance with them on.
- [ ] Estimated train positions (schedule-only mode) sit on the lines, especially the ME and RI branches.
- [ ] Refresh the README screenshots once the map tiles show (current ones have a blank map background).

Over the first days and weeks:

- [ ] Morning and evening: the app opens on the right trip (home → work before the switch time, work → home after), and flips over by itself if it's open at the switch time.
- [ ] Compare a delayed train's predictions with Metra's own app.
- [ ] Watch for the **Tracking** badge (GPS-based delay estimate). It hasn't been seen on a real in-service train yet, because every in-service train had a Metra prediction during testing.
- [ ] After about a week, on-time history lines ("90% on time · avg 3 min late") should start appearing. Check that they look sensible.
- [ ] railsmaps.com: check its license. If reuse is allowed, add it as a basemap in `web/map.js`; otherwise drop it.

- [ ] Display options (eye button): hide the From/Going-to boxes; alerts as a 🔔 bell (default; number = new alerts, red if on your lines), a banner, or off. Check on your phone.
- [ ] After updating, reopen the app twice: the second open should be noticeably quicker (scripts now come from the phone's cache).

- [ ] Weekly schedule re-download: about a week after updating, the log should show "Weekly schedule check: downloading the full schedule" then "Schedule unchanged" (or "Now using schedule …").

## 3. Home Assistant

- [ ] Find your phone's notify service (`notify.mobile_app_…`).
- [ ] Use `http://SERVER-IP:8080/api/now` as the sensor resource. It returns the right trip for the time of day, so no stations are hard-coded. (`docs/home-assistant.yaml` has a working example using `/api/commute`; swap the URL.)
- [ ] Add it to the HA repo (`(private Home Assistant repo)`) as a package; check how `configuration.yaml` is organized first.
- [ ] Set the notification time and weekdays. Run it manually once to test.

## 4. Next features


- [ ] **"Leave now" alerts.** Set how long it takes to get to your station; get told when to leave, adjusted for live delays ("Leave by 7:04 for the 7:12, running 3 min late"). It can use the home/work profile and `/api/now`. Deliver via Home Assistant (`notify.mobile_app_…`) or ntfy.
- [ ] **Alerts for your own stations.** Feature station-level alerts (elevator outages, entrance closures) for your home and work stations, and notify when one affects your train.
- [x] **Remote access with Tailscale.** Working (the server at SERVER-IP). Don't port-forward the app: it has no login.

## 5. Ideas

- [ ] Offline app shell (service worker): opens instantly even with no signal and shows the last board, marked as old.

- [ ] **Wall-display mode** (`/?kiosk`): only the current commute board in large type, for a tablet by the door.
- [ ] A small history page with charts (per-train on-time trend, worst days).
- [ ] Store train positions in history too (currently predictions, cancellations and alerts only).
- [ ] Update GitHub Actions versions to clear the "Node.js 20 is deprecated" warning.
- [ ] Run the container as a non-root user (Unraid's 99:100).
- [ ] Shrink old history: full detail for 90 days, then daily summaries (about 1 GB/year → tens of MB).

## Done

**Setup and configuration**
- Every setting can be set by environment variable (Unraid template, `.env` / `.env.example` for compose, `docker run -e`), with defaults and validation. Invalid values are logged and fall back instead of crashing. A startup log summary shows the settings with the API key masked. `METRA_API_TOKEN_FILE` supports Docker secrets.
- **Home + work commute**: work station, switch time (presets Noon–7 PM) and workdays. On workdays the app opens on home → work before the switch time and work → home after; other days on the home board. Settable in the app (briefcase button with searchable station pickers, "Set as home" / "Set as work") or by env (`HOME_STATION`, `WORK_STATION`, `COMMUTE_SWITCH_TIME`, `WORK_DAYS`). App-saved values win; unset ones fall back to env. A fresh install shows a "Set up your commute" card. Adds `/api/now` and `/api/work`.

**Board and map**
- **Self-hosted map**: the server downloads a Chicago-area OpenStreetMap extract (Protomaps) by range requests, serves tiles, fonts and icons itself, and refreshes it monthly. Tested end-to-end against a local test archive (the extract is tile-for-tile exact; with the map present the browser makes zero outside requests). Fallback chain: the server map → OpenFreeMap → Simple.
- Map background switched from CARTO (now needs an API key) to **Simple**, the app's own background with no outside service, key or extra data, as the default. Optional free, no-key backgrounds: OpenFreeMap Streets/Light/Dark and Esri Satellite, with automatic fallback to Simple if one can't load.
- Home, work and switch time are saved on the server (`/config/settings.json`) and survive restarts and updates (verified).
- Home and work shown on the map as labelled pins (blue house, amber briefcase) that sit above trains, a pulsing ring on the station you're viewing, the map opening framed on your commute, and a "show my commute" map button.
- On-time history per departure ("90% on time · avg 3 min late · 21 days"): last 30 days of this train at this station on the same kind of day. Shown after 5+ days of data.
- Home station shared by all devices; estimated arrival at each train's final stop, shown as ~~scheduled~~ estimate; AM/PM kept on one line.
- Line-name search ("upn"), optional **Going to** with arrival times, swap button.
- Tracking badge (GPS delay estimate); parked or out-of-service trains hidden (41 → 28 real trains on the live map); next stop always shown; departures never shown before schedule; amber 1–5 min late, red 6+; countdowns on server time; alerts banner limited to your lines; station names cleaned ("Lake Forest (UP-N)"); map popup title no longer runs into the close button.

**Data, performance and reliability**
- Live Metra feeds verified: the Bearer token works, trip IDs match the schedule, and `vehicle.label` is the train number. Metra doesn't send track numbers, and its feed often returns 500/504 errors; the app keeps the last good data and retries.
- Metra polled at most once per 30 s, enforced in code and verified live (~8,000 API requests → 3 polls per feed in 70 s). A code-review fix for a poll-timing bug restored the true 30 s refresh (it was 60 s).
- Gzip (map library 1.06 MB → 276 KB, trains 16 KB → 2.8 KB); no polling while the app is hidden; stale positions dropped; "Live" checks the age of Metra's data.
- `/api/next?station=…&direction=inbound|outbound`, `/api/commute`, `/api/home`, history API and CSV export.
- CI: tests on every push and a Docker image published to GHCR from `main` (fixed: tests couldn't import the app under plain `pytest`; GitHub-side errors on the first runs weren't our code).
- App icons, Unraid template icon, README sections on data use, backups and pulling the published image.
