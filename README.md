# Se1smo — earthquake checks and alerts for Telegram

Se1smo helps someone who wakes up after a tremor answer a practical question: “Was that an earthquake near me?” It compares public USGS and EMSC reports, gives a quick estimate for a saved place, and can alert the user about nearby events. The first audience is people in Uzbekistan and nearby parts of Central Asia, with Russian, Uzbek and English interfaces.

This is an information tool, not an official warning service, seismic instrument, or emergency service. Feed delays and the felt-shaking estimate mean that a quiet reply cannot prove there was no shaking.

## Run with Docker

You need Docker Compose and a PostgreSQL server reachable from the Docker network `docker_pg_network`. In the existing setup, PostgreSQL is the `postgres_db` container; the app uses database `quakebot` and role `quakewatch_app`. pgAdmin is optional and is not needed to run the bot. The database and role must already exist; the app creates its tables when it starts. In the current setup, PostgreSQL is published on host port 5433 to avoid a collision; containers on `docker_pg_network` still connect to `postgres_db:5432`.

1. Create a local environment file and edit it:

   ```sh
   cp .env.example .env
   ```

   Set `BOT_TOKEN` to the token from [@BotFather](https://t.me/BotFather). Set `DATABASE_URL` to the password and host for your PostgreSQL role. For the existing Docker network, use `postgres_db` as the hostname, not `localhost`. URL-encode reserved characters in the password. Keep `.env` private; it is ignored by Git.

2. Make sure PostgreSQL is running and attached to `docker_pg_network`, then build and start the bot:

   ```sh
   docker compose up -d --build
   docker compose logs -f quakewatch
   ```

3. Check the backend health endpoint:

   ```sh
   curl http://localhost:8001/health
   ```

   It should return `{"status":"ok"}`. This confirms the FastAPI process is responding; Telegram interaction below checks the bot itself.

For an existing stopped database container, start it with `docker start postgres_db`. `POLLING=true` (the default) lets the bot receive Telegram updates by long polling. `POLLING=false` disables polling; a webhook adapter is not included. `CHECK_INTERVAL_SECONDS`, `CACHE_TTL_SECONDS` and `MAX_STALE_CACHE_SECONDS` tune alert polling and feed caching.

## Try it in Telegram

1. Open [@Se1smo_bot](https://t.me/Se1smo_bot) and send `/start`. On the first visit, the bot explains what it does and asks for a place. On later visits it greets the returning user.
2. Tap **📍 Отправить местоположение** (or the matching localized location button) to share location, or type a city name. The bot saves the place and inferred timezone. To change it later, tap **📍 Место**.
3. Tap **🌐 Трясло?**. The bot checks the last 30 minutes and shows nearby reports with magnitude, depth, distance, local time, source and feed status. Tap the **USGS** or **EMSC** link under a result to open that source. If nothing is currently available, the bot says so without treating it as proof that there was no shaking; it schedules one follow-up check and updates the reply if a late report appears.
4. Tap **🔔 Подписка** to enable alerts. Choose the minimum magnitude, maximum screening radius and whether quiet hours are enabled. Quiet hours are 22:00–07:00 in the saved local timezone. This lets you check both the button flow and the alert settings; real alerts depend on actual matching earthquakes, so there is no synthetic “test quake” button.
5. Tap **🌍 Язык** to switch between Русский, O‘zbekcha and English, or **❓ Помощь** for the in-bot guide.

## Who it is for, and why a Telegram bot

The user may be anxious, half-awake and checking from a phone. A persistent keyboard keeps the core actions visible: check, alerts, place, language and help. Asking for a place once avoids repeated setup; keeping a local timezone makes event times and quiet hours easier to understand. The bot gives a quick answer from two public feeds without requiring an account on an earthquake website.

The first release focuses on people who want a fast, cautious check near a saved place, especially Russian-, Uzbek- and English-speaking users in Central Asia. It is also useful to anyone who prefers Telegram notifications over repeatedly refreshing a seismic map.

## Decisions made for this version

- **“May be felt” is a screening estimate.** At magnitude 3.5 or above, an event qualifies within `min(600 km, 30 km × 10^(0.5 × (M − 3.5))`. For depth of 200 km or more, the threshold is tightened to magnitude 5.5 and the distance is halved. This is an intentionally simple filter, not a ShakeMap or a claim about what a specific person felt. Local geology, buildings, magnitude-scale differences and uncertainty are not modeled.
- **The two feeds are reconciled conservatively.** USGS and EMSC reports are paired when origin times are within 90 seconds and epicentres within 50 km. Both magnitudes and their types are retained. A reviewed USGS report is preferred for the displayed working value; otherwise USGS, then EMSC. This is a deterministic display choice, not a claim that USGS is always more accurate.
- **A missing feed is shown as missing.** The bot distinguishes fresh, cached, stale and unavailable data. Cached data may be used for up to 10 minutes by default; the event is still filtered against the requested time window. An empty or unavailable feed is never presented as proof that there was no earthquake.
- **Alerts favor understandable controls.** Users choose magnitude thresholds of 3.5, 4.0 or 4.5, a screening radius of 100, 250 or 500 km, and optional quiet hours. A normalized time/epicentre fingerprint prevents the same event from being sent twice from both sources. Failed sends remain pending for retry.
- **The interface ships in three languages.** Russian, Uzbek and English are included from the first version because they match the initial audience.

## Deliberately out of scope

This version does not predict damage, issue official warnings, provide evacuation or medical advice, or guarantee that a user felt an event. It does not include a live map, crowd-sourced intensity reports, long-term catalog search, magnitude conversion, extra seismic networks, or a synthetic alert generator. Those would add complexity or imply certainty that the current sources and simple screening model do not provide. The bot should not replace local emergency guidance.

## Feed reconciliation and delayed updates

USGS and EMSC are queried concurrently for the last 30 minutes, within 600 km and from magnitude 2.5. A quiet on-demand check schedules one follow-up after ten minutes so a late-arriving report can be appended to the conversation. Subscription alerts poll at `CHECK_INTERVAL_SECONDS`. The short-lived cache is stored in `feed_cache`; `CACHE_TTL_SECONDS` controls its lifetime. Existing databases gain new preference and cache timestamp columns at startup using `ADD COLUMN IF NOT EXISTS`.

## Data endpoints

- [USGS FDSN Event API](https://earthquake.usgs.gov/fdsnws/event/1/)
- [EMSC SeismicPortal FDSN API](https://www.seismicportal.eu/fdsnws/event/1/query?format=json)
- [USGS Latest Earthquakes map](https://earthquake.usgs.gov/earthquakes/map/)
- [EMSC earthquake activity](https://www.emsc-csem.org/)

Nominatim/OpenStreetMap is used without an API key for city lookup and reverse geocoding. Set an identifying `USER_AGENT` and follow the [Nominatim usage policy](https://operations.osmfoundation.org/policies/nominatim/).
