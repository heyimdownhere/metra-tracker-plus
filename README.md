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
- The board shows the next 5 departures with time, minutes until, destination, line, track (when Metra provides it) and a **Live** or **Scheduled** badge. Realtime predictions override the schedule and fall back to it when missing. Cancelled trains are struck through.
- **Delay reasons**: for a late or cancelled train, the app looks for an active alert naming that trip, or else one for the train's line. It shows the alert's cause (e.g. "Weather"), or the alert's text if the cause is unknown.
- **Service alerts** banner. Alerts for your station's lines are listed first.
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

The image is built locally on the server; there is no registry image.

1. **Copy the source to the server.** From the Unraid terminal (or SSH into `root@SERVER-IP`):
   ```sh
   cd /mnt/user/appdata
   git clone https://github.com/heyimdownhere/metra-tracker-plus.git metra-tracker-plus-src
   ```
   (Or copy the folder over the `appdata` SMB share.)
2. **Build the image:**
   ```sh
   docker build -t metra-tracker-plus:latest /mnt/user/appdata/metra-tracker-plus-src
   ```
3. **Install the template:**
   ```sh
   cp /mnt/user/appdata/metra-tracker-plus-src/unraid/metra-tracker-plus.xml \
      /boot/config/plugins/dockerMan/templates-user/my-metra-tracker-plus.xml
   ```
4. In the Unraid web UI, go to **Docker → Add Container** and choose **metra-tracker-plus** from the *Template* dropdown. Check these settings:
   - Port `8080`
   - Appdata `/mnt/user/appdata/metra-tracker` → `/config`
   - `METRA_API_TOKEN`: leave blank for now, or paste your key (the field is masked)
   - `TZ=America/Chicago`

   Click **Apply**.
5. Open **http://SERVER-IP:8080** (or click **WebUI** on the container icon).

**When your API key arrives:** edit the container, paste the key into `METRA_API_TOKEN`, and click Apply. The status pill changes from *Schedule only* to *Live*.

**Updating:** `cd /mnt/user/appdata/metra-tracker-plus-src && git pull && docker build -t metra-tracker-plus:latest .`, then restart the container. The cached schedule and history in `/config` are kept.

### docker compose (alternative)

```sh
METRA_API_TOKEN=yourkey docker compose up -d --build
```

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `METRA_API_TOKEN` | *(empty)* | Metra GTFS-RT token. Empty means schedule-only. |
| `TZ` | `America/Chicago` | Container timezone. |
| `HISTORY_DAYS` | `365` | Days of realtime history to keep. `0` disables history. |
| `REALTIME_INTERVAL` | `30` | Realtime poll interval in seconds. Minimum 30. |
| `STATIC_CHECK_INTERVAL` | `300` | How often to check `published.txt`, in seconds. |
| `PORT` | `8080` | Listen port inside the container. |
| `LOG_LEVEL` | `INFO` | Python log level. |

A weekday has about 11,500 train-stop events, so history grows by roughly 3 MB a day while realtime is on (about 1 GB for a full year). Rows older than `HISTORY_DAYS` are pruned every 6 hours.

## Home Assistant: morning train notification

`GET /api/commute?from=<station>&to=<station>&limit=3` returns the next trains that stop at `from` and later at `to`. Each train includes departure and arrival (predicted and scheduled), minutes until, trip duration, status (`on time`, `5 min late`, `cancelled`, `scheduled`), track, delay reason, and a ready-to-send `message`, for example:

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
| `GET /api/settings` / `PUT /api/settings` | Shared settings: `{"home_station": "EVANSTON", "home_dest": null}`. |
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
