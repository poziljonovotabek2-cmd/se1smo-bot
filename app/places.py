import httpx
from timezonefinder import TimezoneFinder

tf = TimezoneFinder()


async def city_to_place(query: str, user_agent: str) -> tuple[float, float, str, str] | None:
    async with httpx.AsyncClient(timeout=8, headers={"User-Agent": user_agent}) as client:
        response = await client.get("https://nominatim.openstreetmap.org/search", params={"q": query, "format": "jsonv2", "limit": 1})
        response.raise_for_status()
        rows = response.json()
    if not rows:
        return None
    row = rows[0]
    lat, lon = float(row["lat"]), float(row["lon"])
    return lat, lon, row.get("name") or row["display_name"].split(",")[0], tf.timezone_at(lat=lat, lng=lon) or "UTC"


async def coords_to_place(lat: float, lon: float, user_agent: str) -> tuple[str, str]:
    tz = tf.timezone_at(lat=lat, lng=lon) or "UTC"
    name = f"{lat:.3f}, {lon:.3f}"
    async with httpx.AsyncClient(timeout=5, headers={"User-Agent": user_agent}) as client:
        try:
            response = await client.get("https://nominatim.openstreetmap.org/reverse", params={"lat": lat, "lon": lon, "format": "jsonv2"})
            if response.is_success:
                address = response.json().get("address", {})
                name = address.get("city") or address.get("town") or address.get("village") or address.get("county") or name
        except httpx.HTTPError:
            pass
    return name, tz
