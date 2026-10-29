import json
import math
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import select

from app.config import settings
from app.db import FeedCache, Session

USGS = "https://earthquake.usgs.gov/fdsnws/event/1/query"
EMSC = "https://www.seismicportal.eu/fdsnws/event/1/query"


def haversine_km(a_lat: float, a_lon: float, b_lat: float, b_lon: float) -> float:
    radius = 6371.0
    p1, p2 = math.radians(a_lat), math.radians(b_lat)
    dp, dl = math.radians(b_lat - a_lat), math.radians(b_lon - a_lon)
    x = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return radius * 2 * math.atan2(math.sqrt(x), math.sqrt(1 - x))


def normalize(source: str, item: dict) -> dict | None:
    try:
        if source == "USGS":
            props, coords = item["properties"], item["geometry"]["coordinates"]
            return {"source": source, "id": item.get("id"), "time": datetime.fromtimestamp(props["time"] / 1000, timezone.utc),
                    "lat": coords[1], "lon": coords[0], "depth": coords[2], "mag": float(props["mag"]) if props.get("mag") is not None else None,
                    "mag_type": props.get("magType"), "place": props.get("place") or "Unknown location",
                    "reviewed": str(props.get("status", "")).lower() == "reviewed", "url": props.get("url")}
        props, coords = item.get("properties", {}), item.get("geometry", {}).get("coordinates", [])
        # SeismicPortal JSON has appeared in both GeoJSON and FDSN-style property layouts.
        lat = coords[1] if len(coords) >= 2 else props.get("lat")
        lon = coords[0] if len(coords) >= 2 else props.get("lon")
        if lat is None or lon is None:
            return None
        time_value = props.get("time")
        if isinstance(time_value, (int, float)):
            when = datetime.fromtimestamp(time_value / (1000 if time_value > 10_000_000_000 else 1), timezone.utc)
        else:
            when = datetime.fromisoformat(str(time_value).replace("Z", "+00:00"))
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return {"source": source, "id": item.get("id"), "time": when.astimezone(timezone.utc),
                "lat": float(lat), "lon": float(lon), "depth": coords[2] if len(coords) > 2 else props.get("depth"),
                "mag": float(props["mag"]) if props.get("mag") is not None else None, "mag_type": props.get("magtype") or props.get("magType"),
                "place": props.get("flynn_region") or props.get("region") or "Unknown location",
                "reviewed": False, "url": props.get("unid") and f"https://www.seismicportal.eu/eventdetails.html?unid={props['unid']}"}
    except (KeyError, TypeError, ValueError, IndexError):
        return None


async def _cached_json(key: str, url: str, params: dict) -> tuple[dict, dict]:
    now = datetime.now(timezone.utc)
    async with Session() as session:
        row = await session.get(FeedCache, key)
        if row and row.expires_at > now:
            age = max(0, int((now - row.fetched_at).total_seconds()))
            return json.loads(row.payload), {"state": "cached", "age": age}
    async with httpx.AsyncClient(timeout=9, headers={"User-Agent": settings.user_agent}) as client:
        response = await client.get(url, params=params)
        if response.status_code == 204:
            payload = {"features": []}
        else:
            response.raise_for_status()
            payload = response.json()
    async with Session() as session:
        row = await session.get(FeedCache, key)
        if row:
            row.payload = json.dumps(payload)
            row.expires_at = now + timedelta(seconds=settings.cache_ttl_seconds)
            row.fetched_at = now
        else:
            session.add(FeedCache(cache_key=key, payload=json.dumps(payload), expires_at=now + timedelta(seconds=settings.cache_ttl_seconds), fetched_at=now))
        await session.commit()
    return payload, {"state": "fresh", "age": 0}


async def fetch_near(lat: float, lon: float, minutes: int = 30) -> tuple[list[dict], list[str], dict]:
    now = datetime.now(timezone.utc)
    since = now - timedelta(minutes=minutes)
    # Cache is intentionally keyed by rounded coordinates to reduce upstream traffic.
    suffix = f"{round(lat, 1)}:{round(lon, 1)}:{minutes}:{int(now.timestamp() // settings.cache_ttl_seconds)}"
    common = {"starttime": since.isoformat(), "endtime": now.isoformat(), "latitude": lat, "longitude": lon,
              "minmagnitude": 2.5, "limit": 500}
    requests = [
        _cached_json("usgs:" + suffix, USGS, {**common, "maxradiuskm": 600, "orderby": "time", "format": "geojson"}),
        _cached_json("emsc:" + suffix, EMSC, {**common, "maxradius": 600 / 111.195, "format": "json"}),
    ]
    results = await __import__("asyncio").gather(*requests, return_exceptions=True)
    events, errors, statuses = [], [], {}
    for source, cache_key, result in zip(("USGS", "EMSC"), ("usgs:" + suffix, "emsc:" + suffix), results):
        if isinstance(result, Exception):
            errors.append(source)
            stale_prefix = f"{source.lower()}:{round(lat, 1)}:{round(lon, 1)}:{minutes}:"
            async with Session() as session:
                row = await session.scalar(select(FeedCache).where(FeedCache.cache_key.like(stale_prefix + "%"))
                                           .order_by(FeedCache.fetched_at.desc()).limit(1))
            if row:
                age = max(0, int((now - row.fetched_at).total_seconds()))
                statuses[source] = {"state": "stale", "age": age}
                if age > settings.max_stale_cache_seconds:
                    continue
                result = json.loads(row.payload)
            else:
                statuses[source] = {"state": "unavailable", "age": None}
                continue
        else:
            payload, statuses[source] = result
            result = payload
        features = result.get("features", [])
        for raw in features:
            event = normalize(source, raw)
            if event and since <= event["time"] <= now:
                events.append(event)
    # Merge reports for the same quake when origin times and epicentres are close.
    events.sort(key=lambda e: e["time"])
    merged: list[dict] = []
    for event in events:
        match = next((e for e in merged if abs((e["time"] - event["time"]).total_seconds()) <= 90 and
                      haversine_km(e["lat"], e["lon"], event["lat"], event["lon"]) <= 50), None)
        if match:
            match["reports"].append(event)
        else:
            event["reports"] = [event]
            merged.append(event)
    return merged, errors, statuses


def felt_radius_km(magnitude: float) -> float:
    """Conservative notification screen; not a prediction of actual shaking."""
    if magnitude < 3.5:
        return 0
    return min(600, 30 * (10 ** (0.5 * (magnitude - 3.5))))


def preferred_report(event: dict) -> dict:
    reports = event.get("reports", [event])
    usgs = [report for report in reports if report["source"] == "USGS"]
    return next((report for report in usgs if report.get("reviewed")), usgs[0] if usgs else reports[0])


def assess(event: dict, user_lat: float, user_lon: float, min_magnitude: float = 3.5,
           max_distance_km: int = 600) -> tuple[bool, float, str]:
    dist = haversine_km(user_lat, user_lon, event["lat"], event["lon"])
    mag = float(preferred_report(event).get("mag") or 0)
    radius = min(felt_radius_km(mag), max_distance_km)
    depth = float(event["depth"] or 0)
    qualifies = mag >= min_magnitude and radius > 0 and dist <= radius and (depth < 200 or (mag >= 5.5 and dist < radius / 2))
    confidence = "screening estimate; actual shaking varies" if qualifies else "outside screening range"
    return qualifies, dist, confidence


def describe(event: dict, timezone_name: str) -> tuple[str, str]:
    reports = event.get("reports", [event])
    # The same preferred report drives both the displayed magnitude and screening threshold.
    preferred = preferred_report(event)
    values = []
    for r in reports:
        if r.get("mag") is not None:
            values.append(f"{r['source']} {r['mag']:.1f} {str(r.get('mag_type') or '').upper()}".strip())
    magnitudes = " / ".join(values) if values else "unavailable"
    event["time"] = event["time"].astimezone(ZoneInfo(timezone_name))
    event["display_mag"] = f"{magnitudes} (using {preferred['source']}: {preferred.get('mag')})"
    event["source_names"] = "+".join(sorted({r["source"] for r in reports}))
    return event["display_mag"], event["source_names"]


def local_time(event: dict, timezone_name: str) -> str:
    return event["time"].astimezone(ZoneInfo(timezone_name)).strftime("%Y-%m-%d %H:%M:%S %Z")


def feed_status_text(statuses: dict, lang: str) -> str:
    labels = {
        "ru": {"fresh": "свежие", "cached": "кэш {age}с", "stale": "устарели ~{age}с", "unavailable": "недоступен", "unknown": "нет данных"},
        "uz": {"fresh": "yangi", "cached": "kesh {age}s", "stale": "eskirgan ~{age}s", "unavailable": "mavjud emas", "unknown": "ma'lumot yo'q"},
        "en": {"fresh": "fresh", "cached": "cached {age}s ago", "stale": "stale ~{age}s", "unavailable": "unavailable", "unknown": "no status"},
    }.get(lang, {})
    parts = []
    for source in ("USGS", "EMSC"):
        info = statuses.get(source, {"state": "unknown", "age": None})
        state = info["state"]
        label = labels.get(state, labels["unknown"])
        if "{age}" in label:
            label = label.format(age=info.get("age", "?"))
        parts.append(f"{source}: {label}")
    lead = {"ru": "Ленты", "uz": "Lentalar", "en": "Feeds"}.get(lang, "Feeds")
    return lead + ": " + " · ".join(parts)
