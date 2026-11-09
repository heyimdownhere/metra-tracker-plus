# Metra Tracker+: what's left

Updated 2026-11-09. The app runs against Metra's **real** schedule and **live** feeds (tested with the real API key). All 24 tests pass. What's left is deployment, a few real-world checks, and the improvements below.

## 1. Deploy on the server (SERVER-IP): do first

- [ ] Clone to `/mnt/user/appdata/metra-tracker-plus-src`.
- [ ] `docker build -t metra-tracker-plus:latest .` The image has never been built: the dev sandbox had no Docker. The app has only run from a clean pip install.
- [ ] Copy `unraid/metra-tracker-plus.xml` to `/boot/config/plugins/dockerMan/templates-user/`, then add the container from the template.
- [ ] Put the API key in `METRA_API_TOKEN` (masked field). The status pill should show **Live**.
- [ ] Open `http://SERVER-IP:8080`. Check the WebUI button and that Docker health shows **healthy**.
- [ ] Check that `/mnt/user/appdata/metra-tracker/` contains `gtfs/`, `history.db` and `settings.json`, and that a restart is fast.
- [ ] Set your home station in the app (⌂ Set as home).

## 2. Check in a real browser / over time

Map tiles were blocked in the dev sandbox, so these have never been seen drawing:

- [ ] Basemaps: Auto/Dark/Light/Streets (CARTO), Satellite (Esri), railway-tracks overlay (OpenRailwayMap).
- [ ] 3D buildings (OpenFreeMap) in the Loop, in dark and light mode, and phone performance with them on.
- [ ] Estimated train positions (schedule-only mode) sit on the lines, especially the ME and RI branches.
- [ ] Refresh the README screenshots once the map tiles show (current ones have a blank map background).
- [ ] Check the first GitHub Actions run (Actions tab) and, if you want to pull instead of build, the published `ghcr.io` image.

Over the first days of real use:

- [ ] Compare a delayed train's predictions with Metra's own app.
- [ ] After a day or two, look at `/api/history/station?station=EVANSTON` to see whether the on-time stats look sensible.
- [ ] railsmaps.com: check its license. If reuse is allowed, add it as a basemap in `web/map.js`; otherwise drop it.

## 3. Home Assistant

- [ ] Pick a notify service (`notify.mobile_app_…`) and stations. `/api/home` uses the saved home station; `/api/next?station=…&direction=inbound` gives the next trains to downtown.
- [ ] Add `docs/home-assistant.yaml` to the HA repo (`(private Home Assistant repo)`) as a package; check how `configuration.yaml` is organized first.
- [ ] Set the time and weekdays; maybe add an evening reverse commute. Run it manually once to test.

## 4. Review suggestions: done 2026-11-09

All implemented, tested (34 tests) and checked on live Metra data:

- [x] Gzip compression: map library 1.06 MB → 276 KB, train list 16 KB → 2.8 KB per refresh.
- [x] No polling while the app is hidden or the phone is locked (0 requests in 65 s hidden; refreshes on return).
- [x] Stale positions (no update for 3+ min) dropped from the map.
- [x] "Live" checks the age of Metra's data, not just that the request worked; the pill shows "Live · N min old".
- [x] Alerts banner only features alerts for your lines; the rest sit behind "N on other lines".
- [x] **Tracking** badge with a GPS-based delay estimate for trains Metra tracks but sends no prediction for. Live data showed most such "trains" were actually **parked or running empty after their trip ended**. Those are now hidden from the map: 41 → 28 trains, all real.
- [x] Next stop always shown (GPS-based when needed; the terminal when arriving).
- [x] Countdowns corrected to the server's clock.
- [x] Amber for 1–5 min late, red for 6+ (Metra's on-time rule).
- [x] Departures never shown before their scheduled time (arrival estimates still can be early).
- [x] Track code and UI removed (Metra doesn't send tracks).
- [x] Station names cleaned ("Lake Forest." → "Lake Forest") and duplicates labelled: "Lake Forest (MD-N)" / "Lake Forest (UP-N)", "Prairie Crossing (MD-N)" / "(NCS)".
- [x] Per-trip prediction lookups cached; lookback for very late trains extended from 2 h to 4 h.
- [x] `/api/status` reports `realtime_age`; `/healthz` still ignores realtime on purpose, so Metra outages don't restart the container.
- [x] GitHub Actions: tests on every push; Docker image published to GHCR on `main` (optional for Unraid; see README).
- [x] App icon (PWA, iPhone home screen, Unraid template). History backup documented in the README.

Still to check: the **Tracking** estimate itself hasn't been seen on a real in-service train yet, because every in-service train had a Metra prediction during testing. Watch for the "Tracking" badge and compare it with reality.

## 5. Ideas

- [ ] **Wall-display mode** (`/?kiosk`): only the home board in large type, for a tablet by the door.
- [ ] **History UI:** "on time 85% (30 days)" next to each departure, or a small charts page.
- [ ] Store train positions in history too (currently predictions, cancellations and alerts only).
- [ ] Remote access away from home (e.g. a VPN to the server). Not set up; there's no login, so don't expose port 8080 to the internet.

## Done

- Live Metra feeds verified: Bearer token works; trip IDs match the schedule; `vehicle.label` is the train number; ~146 untripped yard vehicles hidden; line-wide alerts filtered so only delay-related ones explain delays. Metra doesn't send track numbers, and its feed often returns 500/504 errors; the app keeps the last good data and retries.
- Line-name search ("upn"), optional **Going to** with arrival times, swap button.
- **Home station** (server-side, shared by all devices) and estimated arrival at each train's final stop, shown as ~~scheduled~~ estimate.
- AM/PM kept on one line.
- `/api/next?station=…&direction=inbound|outbound`.
- Metra polled at most once per 30 s, enforced in code and verified live (~8,000 API requests → 3 polls per feed in 70 s).
- Code-review fixes: a poll-timing bug that halved refresh rate (60 s instead of 30 s), ambiguous station names, realtime-only trains listed at their final stop, 500 errors during startup, export memory use, duplicated code.
