# Metra Tracker+: what's left

Status as of 2026-11-09. The app runs and has been tested against Metra's real **static** schedule feed. Everything below is either blocked on something outside the code, or hasn't been checked in a real environment yet.

## 1. Do first (blockers)

- [ ] **Create the GitHub repo** `heyimdownhere/metra-tracker-plus` (empty, no README) and push this code. Until then the code only exists in a temporary cloud session.
- [ ] **Get the Metra API token** and put it in `METRA_API_TOKEN` (Unraid: edit the container; the field is masked).

## 2. Deploy on the server (SERVER-IP)

- [ ] Clone to `/mnt/user/appdata/metra-tracker-plus-src`.
- [ ] `docker build -t metra-tracker-plus:latest .` The image has never been built: the dev sandbox had no Docker. The app has only been tested from a clean pip install.
- [ ] Copy `unraid/metra-tracker-plus.xml` to `/boot/config/plugins/dockerMan/templates-user/`, then add the container from the template.
- [ ] Open `http://SERVER-IP:8080`. Check that the WebUI button works and the Docker health status turns **healthy**.
- [ ] Check that `/mnt/user/appdata/metra-tracker/gtfs/` gets created and that a restart is fast (it loads `static.pkl`).

## 3. Verify with real realtime data (needs the token)

Realtime has only been tested with fake feeds built from real trip IDs.

- [ ] Status pill shows **Live**. `/api/status` shows all three feeds with `entities > 0` and no `error`.
- [ ] Confirm the token goes in an `Authorization: Bearer` header. If Metra rejects it, check their docs; the header format is in `app/tracker.py` → `_fetch_feed`.
- [ ] **Trip matching:** do realtime `trip_id`s match the schedule's? If departures stay "Scheduled" while trains move on the map, the ID format has changed. The fallback matches on route + start time; check that it works.
- [ ] **Train numbers:** is `vehicle.label` the train number (e.g. 1234)? It's shown as "#1234" on the board and map.
- [ ] **Track numbers:** does Metra send them? The app reads `stop_time_properties.assigned_stop_id` → `platform_code`. If Metra puts track elsewhere, adapt `_track()` in `app/tracker.py`.
- [ ] **Delay reasons:** look at real alerts. Do they name trips or only routes, and are the causes set or mostly `UNKNOWN_CAUSE`? Decide whether skipping `OTHER_CAUSE` (showing the alert text instead) is right.
- [ ] Watch a delayed train: is the delay carried correctly to later stops? Compare against Metra's own app.
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

- [ ] **History UI:** history is API-only right now. Ideas: an "on time 85% (30 days)" note on each departure, or a small history page with charts.
- [ ] Decide whether to also store train *positions* in history. Today only predictions, cancellations and alerts are stored.
- [ ] Back up `history.db` (about 1 GB/year at the default 365 days), e.g. with the Unraid appdata backup plugin.
- [ ] GitHub Actions: run `pytest` on every push. Optionally build and publish the image to GHCR so Unraid can pull it instead of building locally (then set `<Repository>` in the template).
- [ ] Unraid template: add an icon URL.
- [ ] UI polish: the board's time column wraps "8:17 AM" onto two lines; consider a wider column or a smaller font.
- [ ] Refresh the README screenshots once real map tiles show.
- [ ] Trains added in realtime only (not in the schedule) use their last reported stop as the destination; check this looks right if Metra runs extras.
- [ ] No login on the web UI or API. Fine on the LAN. Remote access (e.g. a VPN to the server) isn't set up.
