# Metra Tracker+: what's left

Updated 2026-11-11. The app runs on the home server against Metra's **real** schedule and **live** feeds. 66 tests pass. CI is green and publishes a ready-to-run Docker image on every push to `main`. What's left is real-world checks, Home Assistant, and new features.

## 1. Deploy on the home server: done

Running on the home server (reached privately over Tailscale), with live data and home/work saved on the server.


## 2. Check in real use

Map tiles were blocked in the dev sandbox, so these have never been seen drawing:

- [x] **Self-hosted map:** downloaded and working on the server (street labels, places and 3D buildings seen on Nov 11).
- [ ] Check the map looks right (labels, icons, light/dark), how big the file is, and 3D buildings from the local map (their heights come from the map's `height` field; unverified).
- [ ] Online options and fallbacks: Streets (OpenFreeMap), Satellite (Esri), tracks overlay (OpenRailwayMap). The fallback to Simple was tested; the online maps themselves couldn't be reached from the sandbox.
- [ ] 3D buildings after the Nov 11 fix: no hard edge when the map is tilted; check the 40% default looks right (raise it in the layers panel if walls look see-through), and phone performance with them on.
- [ ] Estimated train positions (schedule-only mode) sit on the lines, especially the ME and RI branches.
- [ ] Retake the README screenshots with the self-hosted map showing (current ones use the Simple background).

Over the first days and weeks:

- [ ] Morning and evening: the app opens on the right trip (home → work before the switch time, work → home after), and flips over by itself if it's open at the switch time.
- [ ] Compare a delayed train's predictions with Metra's own app.
- [x] **Tracking** badge (GPS-based delay) seen on real trains (e.g. ME #227, ~42 min late, Nov 10).
- [ ] After about a week, on-time history lines ("90% on time · avg 3 min late") should start appearing. Check that they look sensible.
- [ ] railsmaps.com: check its license. If reuse is allowed, add it as a basemap in `web/map.js`; otherwise drop it.

- [ ] Settings page (gear button): hide the From/Going-to boxes; alerts as a 🔔 bell (default; number = new alerts, red if on your lines), a banner, or off. Check on your phone.
- [ ] After updating, reopen the app twice: the second open should be noticeably quicker (scripts now come from the phone's cache).

- [ ] Weekly schedule re-download: about a week after updating, the log should show "Weekly schedule check: downloading the full schedule" then "Schedule unchanged" (or "Now using schedule …").

- [ ] Schedule changes: alerts saying trains won't match the timetable (extra/special service, line closures, expressing, cancellations) are tagged **Schedule change**, listed first in the bell, and shown as a strip above the board for your line. Matching is by wording, because Metra sets almost every alert's type to "unknown"; it was tuned on Metra's 13 live alerts on Nov 10. Tell me about any it gets wrong.
- [ ] Trains sit on the line now (GPS snapped to the track within 150 m). Check a few on the map.
- [ ] New since Nov 11: **Clear** button, **Map** tab (map only), station names in the board title move the map, Going-to list only shows stations with a direct train.
- [ ] If the Unraid template still has `STATIC_CHECK_INTERVAL=300`, change it to `3600` (hourly) or clear it.

## 3. Home Assistant

- [ ] Find your phone's notify service (`notify.mobile_app_…`).
- [ ] Use `http://<server-ip>:<port>/api/now` as the sensor resource. It returns the right trip for the time of day, so no stations are hard-coded. (`docs/home-assistant.yaml` has a working example using `/api/commute`; swap the URL.)
- [ ] Add it to the Home Assistant config as a package; check how `configuration.yaml` is organized first.
- [ ] Set the notification time and weekdays. Run it manually once to test.

## 4. Next features


- [ ] **"Leave now" alerts.** Set how long it takes to get to your station; get told when to leave, adjusted for live delays ("Leave by 7:04 for the 7:12, running 3 min late"). It can use the home/work profile and `/api/now`. Deliver via Home Assistant (`notify.mobile_app_…`) or ntfy.
- [ ] **Phone notification for schedule changes.** Home Assistant notification when a schedule-change alert or a cancellation affects your lines or your next train (alerts already carry `schedule_change` in `/api/alerts`).
- [ ] **Alerts for your own stations.** Feature station-level alerts (elevator outages, entrance closures) for your home and work stations, and notify when one affects your train.
- [x] **Remote access with Tailscale.** Working. Don't port-forward the app: it has no login.

## 5. Ideas

- [ ] **Show trains that skip your stop** on the board, crossed out with "Skips <station>", instead of leaving them out.
- [ ] **Install as an Android app:** give the server an https address with Tailscale (`tailscale serve --bg <port>`, MagicDNS + HTTPS certificates on), then Chrome → Install app. Check the icon and launch colours in light mode.

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
- **Self-hosted map**: the server downloads a Chicago-area OpenStreetMap extract (Protomaps) by range requests, serves tiles, fonts and icons itself, and refreshes it monthly. Tested end-to-end against a local test archive (the extract is tile-for-tile exact; with the map present the browser makes zero outside requests). Fallback chain: server map → OpenFreeMap → Simple.
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
