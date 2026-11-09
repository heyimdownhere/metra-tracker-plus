# Metra Tracker+: what's left

Status as of 2026-11-09. The app runs and has been tested against Metra's real **static** schedule feed. Everything below is either blocked on something outside the code, or hasn't been checked in a real environment yet.

## 1. Do first (blockers)

- [x] Create the GitHub repo and push.
- [x] Get the Metra API token. It works and was tested against the live feeds on 2026-11-09.
- [ ] Put the token in `METRA_API_TOKEN` on the server (Unraid: edit the container; the field is masked).

## 2. Deploy on the server (SERVER-IP)

- [ ] Clone to `/mnt/user/appdata/metra-tracker-plus-src`.
- [ ] `docker build -t metra-tracker-plus:latest .` The image has never been built: the dev sandbox had no Docker. The app has only been tested from a clean pip install.
- [ ] Copy `unraid/metra-tracker-plus.xml` to `/boot/config/plugins/dockerMan/templates-user/`, then add the container from the template.
- [ ] Open `http://SERVER-IP:8080`. Check that the WebUI button works and the Docker health status turns **healthy**.
- [ ] Check that `/mnt/user/appdata/metra-tracker/gtfs/` gets created and that a restart is fast (it loads `static.pkl`).

## 3. Realtime: findings from the first live test (2026-11-09)

Done:
- [x] The `Authorization: Bearer` token works on all three feeds.
- [x] **Trip IDs match the schedule exactly** (32/32 trip updates, 45/45 positioned trains), so no ID-format fallback is needed yet.
- [x] **Train numbers:** `vehicle.label` is the train number (e.g. UP-N #326), now shown on the board and map.
- [x] Of 191 vehicle positions, ~146 have no trip (cars and locomotives in yards or layovers). These are now hidden from the map.
- [x] **Alerts:** almost all line-wide alerts are standing notices (elevators, construction, weekend closures) with `UNKNOWN_CAUSE`. The line-level fallback for delay reasons now only uses delay-related alerts. Trip-level alerts do exist (e.g. "BNSF Inbound Trains On the Move Delayed" naming one train) and are used as-is.

Still open:
- [ ] **Track numbers:** Metra doesn't send them (no `stop_time_properties`), so tracks never show. Consider removing the track UI, or find another source.
- [ ] **Metra's feed is flaky:** frequent HTTP 500/504 and one corrupt response during testing. The app keeps the last good data and retries every 30 s. Consider showing "realtime data N min old" when it's stale.
- [ ] Trip updates only cover about 32 of 45 running trains and contain only arrival times. Trains without a trip update show "Scheduled" on the board even though they're on the map. Consider showing them as "Tracking" with no delay.
- [ ] Some live trains show no next stop, when the schedule says the trip should already have ended (very late train, no trip update). Could use the nearest stop ahead from the GPS position instead.
- [ ] Watch a delayed train and compare its predictions against Metra's own app.
- [ ] Check `/api/history/station?station=…` after a day or two of data.

## 4. Map: check in a real browser

Map tiles were blocked in the dev sandbox, so none of these have been seen actually drawing.

- [ ] Basemaps load: Auto/Dark/Light/Streets (CARTO) and Satellite (Esri).
- [ ] **3D buildings** (OpenFreeMap) appear when zoomed into the Loop and tilted. Check the colors in dark and light mode.
- [ ] Railway tracks overlay (OpenRailwayMap) and its opacity slider.
- [ ] Phone performance with 3D on. If it's sluggish, default `buildings` to `false` in `web/map.js`.
- [ ] Estimated train positions (schedule-only mode) sit on the lines, especially the ME and RI branches.
- [ ] **railsmaps.com:** check its license / terms. If reuse is allowed, add it as a basemap option in `BASEMAPS` in `web/map.js`. If not, drop the idea.

## 5. Home Assistant morning alert

- [ ] Pick the station IDs (`/api/stations`), e.g. `from=NAPERVILLE&to=CUS`.
- [ ] Find the phone's notify service (`notify.mobile_app_…`).
- [ ] Add `docs/home-assistant.yaml` to the HA config repo (`(private Home Assistant repo)`), probably as a package. Check how `configuration.yaml` there is organized first.
- [ ] Set the notification time and weekdays. Maybe add an evening reverse-commute version.
- [ ] Test: run the automation manually and confirm the phone gets the message.

## 6. Nice to have / known gaps

- [x] Search by line name ("upn" lists all UP-N stations) and an optional **Going to** destination, with arrival times and trip length (done 2026-11-09).
- [x] Fixed: the swap (⇅) button overlapped the From field's clear (×) button.
- [x] **Home station** (saved on the server, shared by all devices) and **estimated arrival at each train's final station** (done 2026-11-09).
- [ ] Idea: a full-screen "wall display" mode (`/?kiosk`) showing only the home board in large type, for a tablet by the door.

- [ ] **History UI:** history is API-only right now. Ideas: an "on time 85% (30 days)" note on each departure, or a small history page with charts.
- [ ] Decide whether to also store train *positions* in history. Today only predictions, cancellations and alerts are stored.
- [ ] Back up `history.db` (about 1 GB/year at the default 365 days), e.g. with the Unraid appdata backup plugin.
- [ ] GitHub Actions: run `pytest` on every push. Optionally build and publish the image to GHCR so Unraid can pull it instead of building locally (then set `<Repository>` in the template).
- [ ] Unraid template: add an icon URL.
- [ ] UI polish: the board's time column wraps "8:17 AM" onto two lines; consider a wider column or a smaller font.
- [ ] Refresh the README screenshots once real map tiles show.
- [ ] Trains added in realtime only (not in the schedule) use their last reported stop as the destination; check this looks right if Metra runs extras.
- [ ] No login on the web UI or API. Fine on the LAN. Remote access (e.g. a VPN to the server) isn't set up.
