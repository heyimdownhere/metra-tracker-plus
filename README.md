# Metra Tracker+

Better Metra tracker for phone and data usage.

A self-hosted web app that shows live Metra trains, built to run on an Unraid server. Open `http://SERVER-IP:8080` from any device on your network.

## What it does

Metra Tracker+ runs on your home server and answers the questions you have on the way to the train:

- **When is my next train, and is it on time?** Pick your station once. The board then shows the next five departures, how many minutes away each is, and whether it's on time, late, or cancelled. When a train is late it shows why (for example "Weather" or "Freight interference"), if Metra has posted an alert about it.
- **Where are the trains right now?** A map of the whole Metra system shows every line in its official color and every train moving along it. Tap a train to see where it's going, its next stop, and how late it is. The map can switch between dark, light, street and satellite views, show 3D buildings downtown, and overlay all the railway tracks.
- **Is anything wrong on my line?** Service alerts appear in a banner at the top. Alerts for your line are listed first.
- **Can my phone just tell me?** Home Assistant can ask the app for your commute (say, Naperville → Union Station) and send a weekday-morning notification like *"7:12 AM (on time), 7:30 AM (8 min late: Weather)"*.
- **How reliable is my train, really?** The app keeps a year of delay and cancellation history, so you can see how often your usual train is on time and export the data.

It's light on your phone's data: your home server talks to Metra, and your phone only downloads a small summary from it. It works before you have a Metra API key, using the published schedule and estimated train positions. Add the key and everything switches to live data.

| Desktop | Phone |
|---|---|
| ![Desktop, dark mode](docs/screenshot-desktop-dark-settings.png) | ![Phone, dark mode](docs/screenshot-mobile-dark.png) |

*Screenshots use simulated live data. The real app shows a street map under the lines.*

**Status:** the app runs against Metra's real schedule. Live data, the Docker build on the server, and the Home Assistant setup still need to be verified; see [TODO.md](TODO.md).

## Features

- **Map** (MapLibre GL): every Metra line drawn in its official color from `shapes.txt`, plus station dots and train markers that refresh every 30 s. Click a train to see its line, train number, destination, next stop, delay, and delay reason.
  - **Map settings panel** (layers button, top right): basemap (Auto/Dark/Light/Streets/Satellite/None), basemap opacity, an OpenRailwayMap railway-tracks overlay with its own opacity, **3D buildings** with opacity, line opacity and width, and on/off switches for lines, stations and trains. Your choices are saved in the browser.
  - Right-drag (or two-finger drag on a phone) to tilt and rotate.
- **Home station**: tap **⌂ Set as home** next to "From" (optionally with a "Going to" station). Home is saved on the server (`/config/settings.json`), so every phone, tablet and computer opens straight to it. A **Home** button brings you back after looking at other stations.
- **Departure board**: searchable station picker (search by station or line, e.g. "upn") that remembers your last choice, plus an optional **Going to** station that shows only trains to that stop, with arrival time and trip length. Each train also shows its **estimated arrival at its final station** (e.g. "Est. at Kenosha 11:46 AM"), using live delays when Metra has them.
- The board shows the next 5 departures with time, minutes until, destination, line, train number and a badge:
  - **Live**: Metra's own prediction.
  - **Tracking**: Metra reports the train's GPS position but sent no prediction, so the delay is estimated from where the train is compared with the timetable (shown as "~3 min late").
  - **Scheduled**: no live data; timetable time.

  Delays of 1–5 min are amber and 6+ min red (Metra counts under 6 minutes as on time). Departures are never shown earlier than scheduled, because Metra trains don't leave a station early. Cancelled trains are struck through. Metra doesn't publish track numbers, so none are shown.
- **Stale and out-of-service trains are hidden**: positions that stop updating for 3+ minutes, and trains Metra still tags with a trip that has ended (parked at the terminal or running empty to a yard).
- **"Live" means fresh**: the status pill checks the age of Metra's data itself, not just that the request worked, and shows "Live · 4 min old" when Metra falls behind.
- **On-time history**: once a train has been seen on 5+ days, its departure shows its record at your station over the last 30 days, e.g. "90% on time · avg 3 min late · 21 days". Weekday, Saturday and Sunday trains are counted separately; on time means under 6 minutes late (Metra's definition).
- **Delay reasons**: for a late or cancelled train, the app looks for an active alert naming that trip, or else one for the train's line. It shows the alert's cause (e.g. "Weather"), or the alert's text if the cause is unknown.
- **Service alerts** banner, featuring only alerts for the lines you use (current, destination and home stations). Alerts for other lines sit behind a quiet "N on other lines".
- **Long-term history** in SQLite (`/config/history.db`): every realtime prediction, cancellation and alert, kept for `HISTORY_DAYS` (default 365). You can query on-time stats per departure or export the data as CSV.
- **Home Assistant** commute API for morning notifications (see below).
- Works on phones, has dark and light modes, and can be installed as a PWA.
- **Schedule-only mode**: if `METRA_API_TOKEN` is unset, everything runs from the static schedule, and train positions are estimated along the track shapes.

### Data and licensing

- Static GTFS comes from `https://schedules.metrarail.com/gtfs/schedule.zip`. The app checks `published.txt` every 5 minutes and reloads when it changes. The parsed result is cached in `/config/gtfs/`, so a restart takes well under a second.
- GTFS-Realtime (`positions`, `tripupdates`, `alerts`) comes from `https://gtfspublic.metrarr.com/gtfs/public/…` with `Authorization: Bearer $METRA_API_TOKEN`. It is polled every 30 s and never faster.
- **The browser never contacts Metra.** All Metra requests are made by the server, as Metra's license requires. The browser only loads map tiles (CARTO, Esri, OpenFreeMap, OpenRailwayMap).
- Trip, service, shape and stop IDs are treated as opaque strings. If realtime trip IDs stop matching the schedule (Metra is changing its ID format), trips are matched by route + start time + active service day instead.

## Install on Unraid (the server)

GitHub builds and publishes a ready-to-run image (`ghcr.io/heyimdownhere/metra-tracker-plus:latest`) on every push to `main`, and the Unraid template uses it. Nothing needs building.

1. **Add the template to the server.** Open a terminal on Unraid (**>_** icon at top right of the web UI, or SSH to `root@SERVER-IP`) and run:
   ```sh
   wget -O /boot/config/plugins/dockerMan/templates-user/my-metra-tracker-plus.xml \
     https://raw.githubusercontent.com/heyimdownhere/metra-tracker-plus/main/unraid/metra-tracker-plus.xml
   ```
2. **Create the container.** In the Unraid web UI, go to **Docker → Add Container**. In the **Template** dropdown (under *User templates*), choose **metra-tracker-plus**.
3. **Fill in the fields:**
   - **Metra API key**: paste your key (the field is masked). Leave it empty to run schedule-only.
   - **Home station / Work station / Commute switch time / Workdays**: optional. You can leave these empty and set them in the app instead (briefcase button). Stations can be an id (`EVANSTON`, `OTC`) or a name.
   - **Web UI port**: 8080, unless that's taken on the server.
   - **Appdata**: `/mnt/user/appdata/metra-tracker` (default).
   - More options (history days, poll interval, log level) are under **Show more settings**.
4. Click **Apply**. Unraid pulls the image and starts the container.
5. Open **http://SERVER-IP:8080**, or click the container icon → **WebUI**. On first start it downloads Metra's schedule, which takes a few seconds.
6. **Check:** the status pill shows **Live**, and the container's **Logs** start with a settings summary. Fix anything the log warns about.

**Changing settings later:** Docker tab → click the container icon → **Edit**, change a field, **Apply**. The schedule cache, history and app settings in appdata are kept.

**Updating:** Docker tab → **Check for Updates**, then **Apply update** when it shows "update ready". New versions appear automatically whenever the project is pushed.

### Alternative: build the image on the server yourself

```sh
cd /mnt/user/appdata
git clone https://github.com/heyimdownhere/metra-tracker-plus.git metra-tracker-plus-src
docker build -t metra-tracker-plus:latest metra-tracker-plus-src
```

Then set the template's **Repository** to `metra-tracker-plus:latest`.

### Backing up history

`/config/history.db` holds your delay history (about 1 GB a year at the default 365 days). The **Appdata Backup** plugin (Community Applications) backs up `/mnt/user/appdata/metra-tracker` along with your other containers. It stops the container while copying, so the SQLite file is consistent. `/config/gtfs/` is only a cache and doesn't need backing up.

### docker compose (alternative)

```sh
cp .env.example .env    # then edit .env: API key, stations, port...
docker compose up -d
```

## Phone data use

The server does all the talking to Metra; your phone only downloads small summaries from the server:
- Responses are gzip-compressed. The map library is about 276 KB on first load (cached afterwards), and the train list about 3 KB every 30 s.
- The app stops refreshing when it's in the background or the phone is locked, and refreshes straight away when you come back.
- Map tiles are the largest download. The 3D buildings layer uses the most; turn it off in map settings if data matters.

## Configuration

Everything is optional; with nothing set the app runs schedule-only and asks for your stations on first use. Set values as environment variables: Unraid template fields, `.env` for docker compose (copy `.env.example`), or `-e` with `docker run`. The container log starts with a summary of the settings it picked up (API key masked), and warns about any value it couldn't use.

| Variable | Default | Purpose |
|---|---|---|
| `METRA_API_TOKEN` | *(empty)* | Metra GTFS-RT API key. Empty = schedule-only. |
| `METRA_API_TOKEN_FILE` | *(empty)* | Read the key from a file instead (Docker/compose secrets). The variable wins if both are set. |
| `HOME_STATION` | *(empty)* | Default home station: an id (`EVANSTON`) or a name (`Evanston (Davis St.)`). Ids are at `/api/stations`. |
| `HOME_DESTINATION` | *(empty)* | Default "Going to" for the home board (when no work station is set). |
| `WORK_STATION` | *(empty)* | Work station. On workdays the app opens on home → work before the switch time, work → home after. |
| `COMMUTE_SWITCH_TIME` | `12:00` | When to switch from trains to work to trains home (24-hour `HH:MM`). |
| `WORK_DAYS` | `mon-fri` | Commute days, e.g. `mon-fri`, `mon,tue,thu`. Other days open on the home board. |
| `TZ` | `America/Chicago` | Container time zone (logs). The app always uses Metra's Chicago time. |
| `HISTORY_DAYS` | `365` | Days of delay history to keep (0–3650). `0` disables history. |
| `REALTIME_INTERVAL` | `30` | Seconds between realtime polls (30–3600). Metra's minimum is 30. |
| `STATIC_CHECK_INTERVAL` | `300` | Seconds between checks for a new schedule (60–86400). |
| `PORT` | `8080` | Port the app listens on **inside** the container. To change the port you open in the browser, change the host side of the port mapping instead (Unraid "Web UI port", compose `WEB_PORT`). |
| `HOST` | `0.0.0.0` | Address the app listens on inside the container. |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING` or `ERROR`. |
| `CONFIG_DIR` | `/config` | Where the schedule cache, history and settings live (mount your appdata here). |

**Home, work, switch time and workdays can also be set in the app**: tap the briefcase (top right), or "Set as home" / "Set as work" next to the station field. Values saved in the app win over the environment; anything left unset in the app falls back to the environment. On a fresh install with nothing set, the board shows a "Set up your commute" card.

A weekday has about 11,500 train-stop events, so history grows by roughly 3 MB a day while realtime is on (about 1 GB for a full year). Rows older than `HISTORY_DAYS` are pruned every 6 hours.

## Home Assistant: morning train notification

`GET /api/commute?from=<station>&to=<station>&limit=3` returns the next trains that stop at `from` and later at `to`. Each train includes departure and arrival (predicted and scheduled), minutes until, trip duration, status (`on time`, `5 min late`, `about 3 min late` for GPS estimates, `cancelled`, `scheduled`), delay reason, and a ready-to-send `message`, for example:

> Naperville → Chicago Union Station: 7:12 AM (on time), 7:30 AM (8 min late: Weather), 7:45 AM (scheduled)

Station IDs are listed at `/api/stations`.

[`docs/home-assistant.yaml`](docs/home-assistant.yaml) has a working setup to paste into Home Assistant:

- a REST sensor (timestamp of the next train, with all trains as attributes)
- a weekday **6:45 AM push notification** with the next three trains
- an optional alert if your train becomes late or cancelled between 6 and 9 AM

Change the station IDs, the time, and `notify.mobile_app_YOUR_PHONE`.

## API

All endpoints read the server's in-memory copy of Metra's data. **No request to this app ever triggers a request to Metra.** A single background job polls Metra's realtime feeds, and the poll function itself refuses to run more than once every 30 seconds, whatever calls it. A test checks this, and a live run of ~8,000 API requests in 70 s produced exactly 3 polls of each feed. The schedule is checked every 5 minutes.

Which way a train is going (`inbound`/`outbound`) is decided by geography: a train is inbound if its last stop is closer to the Loop than your station. It doesn't depend on Metra's IDs or direction codes.

Example: `curl "http://SERVER-IP:8080/api/next?station=EVANSTON&direction=inbound"`

```json
{
  "station": {"id": "EVANSTON", "name": "Evanston (Davis St.)"},
  "direction": "inbound",
  "next_minutes": 50,
  "message": "Evanston (Davis St.) toward downtown: 11:20 AM to Chicago OTC (on time), ...",
  "trains": [
    {"departure": "2026-11-09T11:20:00-06:00", "minutes_until": 50, "status": "on time",
     "line": "UP-N", "train": "330", "destination": "Chicago OTC",
     "final_arrival": "2026-11-09T11:50:00-06:00", "live": true, "...": "..."}
  ]
}
```

| Endpoint | Description |
|---|---|
| `GET /healthz` | Health check. Returns 200 once the schedule is loaded, otherwise 503. |
| `GET /api/status` | Mode, feed health, schedule version, history stats. |
| `GET /api/stations` | Stations with the lines that serve them. |
| `GET /api/next?station=EVANSTON&direction=inbound` | **Next 5 trains** at a station. `direction`: `inbound`/`to` = toward downtown, `outbound`/`from` = away from downtown, omit for both. `station` takes an id or a name (`Evanston (Davis St.)`). Optional `limit` (1–20). Returns ISO times, minutes until, status, line, train number, destination, estimated arrival at the final stop, and a ready-made `message`. |
| `GET /api/departures?station=ID&limit=5[&to=ID][&direction=inbound]` | Departure board; with `to`, only trains that later stop there. Each row includes `final_stop` and `final_arrival` (estimated arrival at the end of the line). |
| `GET /api/settings` / `PUT /api/settings` | Commute profile shared by all devices: `home_station`, `home_dest`, `work_station`, `switch_time`, `work_days` (Monday = 0), plus `leg` (which trip applies right now) and where each value came from (`app`/`env`). |
| `GET /api/now?limit=5` | The board for right now: home → work before the switch time on workdays, work → home after, otherwise the home board. `leg` says which. Ideal for Home Assistant. |
| `GET /api/work?limit=5` | Work → home board. |
| `GET /api/home?limit=5` | Departure board for the saved home station (also handy for Home Assistant or a wall display). |
| `GET /api/trains` | Train positions (live, or estimated from the schedule). |
| `GET /api/alerts` | Active service alerts. |
| `GET /api/lines` | GeoJSON of every line. |
| `GET /api/commute?from=ID&to=ID&limit=3` | Commute summary for Home Assistant. |
| `GET /api/history/station?station=ID&days=30` | On-time %, average and worst delay, and cancellations per scheduled departure. |
| `GET /api/history/alerts?days=30` | Every alert seen. |
| `GET /api/history/export.csv?days=30` | Raw history as CSV. |
| `GET /api/docs` | Interactive API docs. |

## Development

```sh
pip install -r requirements-dev.txt
CONFIG_DIR=./config uvicorn app.main:app --reload --port 8080
pytest                          # unit tests (synthetic GTFS + GTFS-RT)
METRA_LIVE_TESTS=1 pytest       # also parse Metra's real static feed
```

## Credits

Schedule and realtime data are provided by Metra. This project is not affiliated with Metra. The map uses [MapLibre GL JS](https://maplibre.org/) (BSD-3, vendored), basemaps from © CARTO / © OpenStreetMap contributors and Esri World Imagery, 3D buildings from OpenFreeMap / OpenMapTiles, and the rail overlay from OpenRailwayMap (CC-BY-SA).
