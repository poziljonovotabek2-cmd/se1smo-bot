# QuakeWatch Telegram bot

Telegram bot backend built with Python, FastAPI, aiogram and PostgreSQL. It checks both public feeds, stores user place/language/subscription and caches feed responses. The reply keyboard contains check, alerts, place and language controls. Every earthquake answer also has URL buttons to the USGS and EMSC activity sites.

## Run

The included Docker Compose setup joins the existing `docker_pg_network` network used by pgAdmin. Set `BOT_TOKEN` in `.env` and set `DATABASE_URL` to the PostgreSQL server address reachable from that Docker network (`postgres_db` is the hostname when using the existing server). Start the bot with:

```sh
docker compose up -d --build
```

The bot listens on port 8000, and `GET /health` is a liveness endpoint. With the existing Docker database, pgAdmin can connect to host `postgres_db`, port `5432`; the app uses a dedicated `quakebot` database and `quakewatch_app` role. The database tables are created on startup. Keep `.env` private. `POLLING=false` disables Telegram polling (a webhook adapter can then be deployed).

## User flow and languages

On the first `/start`, the bot introduces its purpose and asks for a Telegram location or city. Later `/start` messages greet the returning user. The bot saves latitude/longitude, a display place and the timezone inferred from coordinates. The place button lets a user replace it. Interface strings and instructions are available in Russian, Uzbek and English; use the language button to switch.

The alerts button opens subscription settings: toggle alerts, choose minimum magnitude (3.5/4.0/4.5), choose a maximum screening radius (100/250/500 km), and enable quiet hours (22:00–07:00 in the saved local timezone). Events during quiet hours are checked again for up to 12 hours after quiet hours end. Each result also reports whether the USGS and EMSC data are fresh, cached, stale, or unavailable, with the approximate cache age. Existing databases gain the new preference and cache timestamp columns at startup using `ADD COLUMN IF NOT EXISTS`.

`/help` or the Help keyboard button explains the checking estimate, feed statuses and alert settings in Russian, Uzbek or English.

## What “may be felt” means

This is a screening heuristic, not a ShakeMap or a guarantee that a person did or did not feel shaking. A report qualifies for an on-demand result and an alert at magnitude 3.5 or above if the epicentral distance is within `min(600 km, 30 km × 10^(0.5 × (M − 3.5))`. For depth at or above 200 km, the threshold is tightened to magnitude 5.5 and half that distance. Local geology, building type, magnitude scale, depth uncertainty and human perception are not modeled. The bot says “may be felt” and preserves uncertainty.

## Feed reconciliation

USGS and EMSC are queried concurrently for the last 30 minutes, within 600 km and from magnitude 2.5. Reports are paired when origins are within 90 seconds and epicentres within 50 km. Both reported values and magnitude types are shown. The displayed working value prefers a reviewed USGS report; if none exists it prefers USGS, then EMSC. This is a deterministic display preference, not a claim that USGS is always more accurate. The answer lists source names and links to both services. If one or both feeds fail, the message says so; an empty response never means there was definitely no earthquake.

The short-lived cache is stored in `feed_cache`; `CACHE_TTL_SECONDS` controls its lifetime. If a feed fails, the bot can use its last successful cache for up to `MAX_STALE_CACHE_SECONDS` (default 600 seconds), and still filters every event against the requested time window. A quiet check schedules one follow-up for ten minutes, so a late-arriving feed event can be appended to the conversation. Subscription alerts poll at `CHECK_INTERVAL_SECONDS`. Failed Telegram sends remain pending for retry; successful sends are marked delivered. A normalized time/epicentre fingerprint suppresses duplicate alerts across the two feeds.

## Data endpoints

- [USGS FDSN Event API](https://earthquake.usgs.gov/fdsnws/event/1/)
- [EMSC SeismicPortal FDSN API](https://www.seismicportal.eu/fdsnws/event/1/query?format=json)
- [USGS Latest Earthquakes map](https://earthquake.usgs.gov/earthquakes/map/)
- [EMSC earthquake activity](https://www.emsc-csem.org/)

Nominatim/OpenStreetMap is used without an API key for city lookup and reverse-geocoding; observe its usage policy and set an identifying `USER_AGENT`.
