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

Over the first days of real use:

- [ ] Compare a delayed train's predictions with Metra's own app.
- [ ] After a day or two, look at `/api/history/station?station=EVANSTON` to see whether the on-time stats look sensible.
- [ ] railsmaps.com: check its license. If reuse is allowed, add it as a basemap in `web/map.js`; otherwise drop it.

## 3. Home Assistant

- [ ] Pick a notify service (`notify.mobile_app_…`) and stations. `/api/home` uses the saved home station; `/api/next?station=…&direction=inbound` gives the next trains to downtown.
- [ ] Add `docs/home-assistant.yaml` to the HA repo (`(private Home Assistant repo)`) as a package; check how `configuration.yaml` is organized first.
- [ ] Set the time and weekdays; maybe add an evening reverse commute. Run it manually once to test.

## 4. Review suggestions (2026-11-09)

From a full read-through of the logic, ranked by value. **Not implemented yet.**

### High value

- [ ] **Compress responses (gzip).** Nothing is compressed today. Measured sizes: the map library is 1.06 MB, `/api/lines` 168 KB, `/api/trains` 17 KB every 30 s. With FastAPI's `GZipMiddleware` (one line) these shrink roughly 4–5×. This fits the project's goal of low phone data usage. *Effort: tiny.*
- [ ] **Stop polling when the app is in the background.** The page keeps fetching trains, the board and alerts every 30 s even when the tab is hidden or the phone is locked (only resuming on focus is handled). With the screen open that's about 3 MB/hour uncompressed; it should pause when hidden. *Effort: small.*
- [ ] **Hide stale train positions.** A vehicle that stops reporting stays on the map at its last spot for up to 5 minutes, and individual `vehicle.timestamp`s aren't checked; positions up to ~6 min old were seen. Drop positions older than ~3 min, or fade them. *Effort: small.*
- [ ] **Detect stale-but-"successful" feeds.** "Live" only means the last request returned HTTP 200. If Metra serves an old snapshot (old `header.timestamp`) the app still says Live. Treat a feed as stale when its header timestamp is more than ~3 min old, and show "Live data 4 min old" in the pill. *Effort: small.*
- [ ] **Alerts banner shows alerts for every line.** Right now it shows things like "ME Harvey elevator out" to a UP-N rider, as "+11 more". Show only alerts for your station's or home station's lines in the banner, with the rest behind "N other alerts". *Effort: small.*

### Medium value

- [ ] **Trains being tracked but with no prediction.** Metra sends GPS for ~45 trains but trip updates for only ~32. Those ~13 show "Scheduled" on the board even though they're visibly moving. Show them as "Tracking" (no delay). Even better, estimate the delay from the GPS position against the schedule. *Effort: medium.*
- [ ] **No next stop for very late trains.** If the timetable says a trip has already ended and Metra sent no trip update, the popup shows no next stop. Work it out from the GPS position: the nearest stop ahead along the line. *Effort: medium.*
- [ ] **Phone clock vs server clock.** "Minutes until" uses the phone's clock. A phone a couple of minutes off shows wrong countdowns. Use the server's `generated_at` to correct for the difference. *Effort: tiny.*
- [ ] **Delay colors.** Even 1 min late shows in red. Metra counts trains as on time if they're under 6 minutes late; use amber for 1–5 min and red for 6+. *Effort: tiny.*
- [ ] **Early departures.** With only arrival predictions (all Metra sends), a train running early is shown leaving early. Metra trains generally don't leave a station before their scheduled time, so consider showing departure as max(prediction, schedule). Check against real behavior first. *Effort: tiny.*
- [ ] **Track numbers never appear.** Metra doesn't send them. Remove the track code and UI, or keep it dormant and document why. *Effort: tiny.*
- [ ] **Station name quirks.** Metra's data has "Lake Forest." (trailing period) and two separate "Lake Forest" stations. Clean up names for display, and add the line to ambiguous names in the picker, e.g. "Lake Forest (UP-N)". *Effort: tiny.*

### Lower value / housekeeping

- [ ] **Prediction cost.** `_predict` rebuilds per-trip lookup tables on every call, and history recording calls it for every stop of every live trip each poll. Fine at Metra's size (~1 ms) but easy to cache per trip per poll.
- [ ] **Delays over 2 hours.** Trains more than 2 hours late drop off the board (lookback window). Rare; could extend the window.
- [ ] **Health check ignores realtime.** `/healthz` is healthy as soon as the schedule loads, even if realtime has failed for hours. Keep it that way so Docker doesn't restart for Metra outages, but add realtime age to `/api/status` for monitoring.
- [ ] **GitHub Actions** to run `pytest` on every push; optionally publish the image to GHCR so Unraid can pull instead of build.
- [ ] Unraid template icon; refresh README screenshots once map tiles show.
- [ ] Back up `history.db` (about 1 GB/year) with the Unraid appdata backup plugin.

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
