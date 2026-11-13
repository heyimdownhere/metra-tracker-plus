# CTA tracker in Next Stop — to-do

Goal: same site, same theme and details; a **Metra | CTA** switch in the top bar.
Start with the **L (trains)**. Buses can come later as a separate step.

## Decision (Nov 13): same app, separate data
CTA lives in the same app behind the **Metra | CTA** switch, but **nothing is shared with Metra's data**:
- its own stations, home and work, and commute switch time (separate keys in `/config/settings.json`, e.g. `cta.home`);
- its own alerts (bell and banner only show the system on screen);
- its own history database/tables and on-time stats;
- its own browser storage keys (`cta.*`, not `metra.*`).
Shared: the look, the tabs, the map component and the server process/container.

## Where things stand
- **API key:** `CTA_TRAIN_TRACKER_KEY` is saved on the cloud environment and visible to sessions (checked Nov 13: `ttarrivals` and `ttpositions` both return live data).
  - Check with `echo ${CTA_TRAIN_TRACKER_KEY:+set}`. Never print the key itself.
  - Note: `ttpositions` returns `train` as an object, not a list, when a line has only one train. Handle both.
  - Why it's not stored like the Metra key: CTA only accepts the key in the address (`?key=...`), and the secure key store can only add headers.
- **Network:** the environment allows any site; `lapi.transitchicago.com` and `www.transitchicago.com` both respond.
- **Schedule file:** CTA's public schedule is at `https://www.transitchicago.com/downloads/sch_data/google_transit.zip`.
  - It's 51 MB, mostly buses. The L lines are `route_type=1`.
  - Lines and colours: Red C60C30, Blue 00A1DE, Brn 62361B, G 009B3A, Org F9461C, P 522398, Pink E27EA6, Y F9E300.
  - 143 stations, ids `4xxxx`; platforms `3xxxx` link to them through `parent_station`.
  - Format notes: the train's destination is `stop_headsign` in `stop_times.txt`, not in `trips.txt`; `trips.txt` has a `direction` column (North/South/...).
- **Code:** nothing has been changed for CTA yet. `main` is at the Home/Work buttons release (39c6b93).

## Train Tracker API (documentation at transitchicago.com/developers/ttdocs/)
Base address: `https://lapi.transitchicago.com/api/1.0/`, with `outputType=JSON` and `key=` on every request.
- **Arrivals:** `ttarrivals.aspx?mapid=<4xxxx station>` (or `stpid=<3xxxx platform>`, `rt=`, `max=`). Next arrivals with predictions and flags for scheduled, delayed and approaching trains.
- **Follow a train:** `ttfollow.aspx?runnumber=<rn>`. The stops still ahead for one train, used by the double-tap sheet. Stops it already passed come from the schedule.
- **Positions:** `ttpositions.aspx?rt=red,blue,brn,g,org,p,pink,y`. Every train's location and heading, for the map.
- **Limits:** 100,000 requests a day per key, and too many requests from one address can trigger a temporary block. Plan: positions every 30 s with all lines in one call (about 2,900 a day); arrivals only while someone is looking at that station; follow only while the sheet is open.

## CTA license (Developer License Agreement)
- The app must not say or imply that CTA is affiliated with it or endorses it. Add "Not affiliated with or endorsed by CTA" next to the Metra line.
- CTA trademarks can't be in the app's name. "Next Stop" is fine, and a plain "CTA" label on the switch should be fine.
- A credit line is optional. Plan to use "Data provided by CTA".
- Keeping data around is allowed if it's kept reasonably up to date.
- Purpose must be helping transit riders, which this app is.

## Steps
1. **Mockups first, for approval (no code changes):** done Nov 13 (switch, Next trains at Clark/Lake, frequency Timetable, Map; dark and light, live data). Waiting for approval.
   - the Metra | CTA switch in the header;
   - CTA Next trains for a station, for example Clark/Lake 40380: each line and direction with its next few arrivals in minutes, e.g. "Brown to Kimball 3, 11, 19 min";
   - the CTA map with the L lines in their colours and live train dots;
   - a CTA Timetable showing how often trains come ("every 6–8 min") plus first and last trains, instead of a full-day list.
   - Use the real schedule; use live data once the key is visible.
   - Any animation (for example the switch) gets a video for approval before it's built. The app uses the calm motion timing.
2. **Server:** done Nov 13: `app/cta.py` + `app/cta_api.py`, endpoints under `/api/cta/...` (see README "CTA"), 20 tests in `tests/test_cta.py`.
   - Built: schedule (L only) cached in `/config/cta`, checked daily with ETag (only re-downloads when CTA changes it); live arrivals/positions/follow fetched only on request, cached 30 s, 80,000/day budget then schedule fallback; alerts (no key); separate CTA home/work in `/config/cta-settings.json`; `/api/cta/now`; key blanked out of logs.
   - Green southbound (Ashland/63rd + Cottage Grove) is one group, as approved. Red/Blue/Green group by direction, loop lines by destination.
   - Done Nov 13 (second pass): CTA history in its own database (`/config/cta-history.db`): departures with actual vs scheduled gap ("on time" = no long wait; long wait = more than 2× the scheduled gap and 5+ min longer), delay log from Train Tracker's delayed flag, stats on each board group after 5 days, `/api/cta/history/...` endpoints and CSV; follow sheet's `passed` stops (tracked times, or estimated from the schedule) and the train's delays today.
   - Known limits: passed stops on Brown/Orange/Pink/Purple/Yellow only go back to where the sign last changed (e.g. a Kimball-bound train shows its stops since the Loop). Stats need 5 days of data before they appear.
   - Original plan notes:
   - Load the schedule for the L only: stations, lines, track shapes, and timetables for first/last trains and frequency. Cache it like Metra's and check for a new version weekly.
   - Poll positions every 30 s. Fetch arrivals and follow-train data when needed, with a short cache.
   - Endpoints: either a `system=cta` parameter on the existing ones (stations, departures, trains, line, trip, lines/map) or a `/api/cta/...` set. Decide when building.
   - Settings: CTA home and work stations kept separately from Metra's (see Decision). Add `CTA_TRAIN_TRACKER_KEY` to `.env.example`, the Unraid template, `docker-compose.yml` and the README.
   - Tests in the same style as `tests/test_tracker.py`.
3. **Front end (next):**
   - Metra | CTA switch, remembered per device (for example `metra.system` in localStorage).
   - All tabs reused: Next trains, Timetable (frequency version), Map (L shapes and trains), double-tap train sheet (built from `ttfollow` plus the schedule), Home and Work for CTA, alerts for CTA if wanted.
   - Line chips and colours from CTA. The disclaimer and the time the data was last updated follow whichever system is showing.
4. **Unraid:** the user adds `CTA_TRAIN_TRACKER_KEY` to the container template.
5. **Release:** develop on `feature/cta-l-trains`, test (pytest plus phone screenshots and video), and merge to `main` only when the user says "release".

## Later (optional)
- **Buses:** a Bus Tracker key from ctabustracker.com → My Account → Developer API, then add buses to the CTA side.
