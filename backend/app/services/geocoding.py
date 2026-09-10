from __future__ import annotations

import asyncio
import re

import httpx

from app.models import GeocodeHit, Place
from app.services import nominatim, photon

BELGIUM = {"min_lng": 2.3, "min_lat": 49.45, "max_lng": 6.45, "max_lat": 51.55}
COORD_RE = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*$")
OPEN_METEO_GEOCODE = "https://geocoding-api.open-meteo.com/v1/search"

# Offline vangnet als Open-Meteo/Photon niet antwoorden.
LOCAL_PLACES: list[tuple[str, float, float]] = [
    ("Aalst", 50.9372, 4.0403),
    ("Antwerpen", 51.2194, 4.4025),
    ("Brugge", 51.2093, 3.2247),
    ("Brussel", 50.8503, 4.3517),
    ("Gent", 51.0543, 3.7174),
    ("Hasselt", 50.9307, 5.3325),
    ("Ieper", 50.8510, 2.8856),
    ("Knokke-Heist", 51.3460, 3.2877),
    ("Kortrijk", 50.8279, 3.2649),
    ("Leuven", 50.8798, 4.7005),
    ("Lier", 51.1311, 4.5697),
    ("Lokeren", 51.1036, 3.9934),
    ("Mechelen", 51.0259, 4.4776),
    ("Oostende", 51.2300, 2.9167),
    ("Roeselare", 50.9445, 3.1229),
    ("Sint-Niklaas", 51.1651, 4.1437),
    ("Tongeren", 50.7805, 5.4648),
    ("Turnhout", 51.3227, 4.9495),
]


def _local_search(query: str, limit: int = 5) -> list[dict]:
    needle = query.strip().lower()
    if len(needle) < 2:
        return []
    rows: list[dict] = []
    for name, lat, lng in LOCAL_PLACES:
        if needle not in name.lower() and not name.lower().startswith(needle):
            continue
        rows.append(
            {
                "lat": lat,
                "lon": lng,
                "display_name": f"{name}, België",
                "importance": 0.85 if name.lower().startswith(needle) else 0.7,
                "place_rank": 16,
            }
        )
    rows.sort(key=lambda row: -float(row["importance"]))
    return rows[:limit]


def in_belgium(lat: float, lng: float) -> bool:
    return (
        BELGIUM["min_lat"] <= lat <= BELGIUM["max_lat"]
        and BELGIUM["min_lng"] <= lng <= BELGIUM["max_lng"]
    )


def coord_label(lat: float, lng: float) -> str:
    return f"{lat:.5f}, {lng:.5f}"


def _rows_to_hits(rows: list[dict], fallback_label: str) -> list[GeocodeHit]:
    ranked: list[tuple[float, int, GeocodeHit]] = []
    for row in rows:
        if row.get("lat") is None or row.get("lon") is None:
            continue
        lat = float(row["lat"])
        lng = float(row["lon"])
        if not in_belgium(lat, lng):
            continue
        importance = float(row.get("importance") or 0)
        place_rank = int(row.get("place_rank") or 30)
        ranked.append(
            (
                importance,
                place_rank,
                GeocodeHit(label=row.get("display_name", fallback_label), lat=lat, lng=lng),
            )
        )
    ranked.sort(key=lambda item: (-item[0], item[1]))
    return [hit for _, _, hit in ranked]


async def _open_meteo_search(query: str, limit: int = 5) -> list[dict]:
    """Snelle stadzoekdienst (Open-Meteo) — werkt als Nominatim/Photon time-outen."""
    cleaned = query.strip()
    if not cleaned:
        return []
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(8.0, connect=4.0)) as http:
            response = await http.get(
                OPEN_METEO_GEOCODE,
                params={
                    "name": cleaned,
                    "count": max(limit, 5),
                    "language": "nl",
                    "countryCode": "BE",
                    "format": "json",
                },
            )
            response.raise_for_status()
            payload = response.json() or {}
    except Exception:
        return []
    rows: list[dict] = []
    for item in payload.get("results") or []:
        try:
            lat = float(item["latitude"])
            lng = float(item["longitude"])
        except (KeyError, TypeError, ValueError):
            continue
        if not in_belgium(lat, lng):
            continue
        parts = [
            str(item.get("name") or "").strip(),
            str(item.get("admin2") or "").strip(),
            str(item.get("admin1") or "").strip(),
            str(item.get("country") or "België").strip(),
        ]
        label = ", ".join(part for part in parts if part)
        population = float(item.get("population") or 0)
        rows.append(
            {
                "lat": lat,
                "lon": lng,
                "display_name": label or cleaned,
                "importance": min(0.95, 0.4 + population / 500_000),
                "place_rank": 16 if population >= 20000 else 20,
            }
        )
    return rows


async def _search_rows(query: str, limit: int = 5) -> list[dict]:
    rows = await _open_meteo_search(query, limit=limit)
    if rows:
        return rows
    try:
        rows = await asyncio.wait_for(photon.search(query, limit=limit), timeout=3.0)
        if rows:
            return rows
    except Exception:
        pass
    return _local_search(query, limit=limit)


async def geocode(query: str, limit: int = 5) -> list[GeocodeHit]:
    hits: list[GeocodeHit] = []
    seen: set[str] = set()
    base = query.strip()
    if not base:
        return []
    rows = await _search_rows(base, limit=limit)
    for hit in _rows_to_hits(rows, base):
        key = f"{hit.lat:.4f},{hit.lng:.4f}"
        if key in seen:
            continue
        seen.add(key)
        hits.append(hit)
    return hits[:limit]


async def reverse(lat: float, lng: float) -> GeocodeHit | None:
    if not in_belgium(lat, lng):
        raise ValueError("Dit GPS-punt ligt buiten België.")
    fallback = GeocodeHit(label=coord_label(lat, lng), lat=lat, lng=lng)
    # Nominatim time-out vaak; nooit de UI blokkeren — korte poging, dan coördinaten.
    try:
        row = await asyncio.wait_for(nominatim.reverse(lat, lng, zoom=16), timeout=2.5)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code in {429, 403}:
            return fallback
        return fallback
    except Exception:
        return fallback
    if not row or row.get("error"):
        return fallback
    return GeocodeHit(label=row.get("display_name") or fallback.label, lat=lat, lng=lng)


def place_parts(label: str) -> tuple[str | None, str | None]:
    parts = [part.strip() for part in (label or "").split(",") if part.strip()]
    if not parts:
        return None, None
    return parts[0], parts[1] if len(parts) > 1 else parts[0]


async def geocode_one(query: str) -> Place:
    match = COORD_RE.match(query or "")
    if match:
        lat = float(match.group(1))
        lng = float(match.group(2))
        if not in_belgium(lat, lng):
            raise ValueError("Dit GPS-punt ligt buiten België.")
        hit = await reverse(lat, lng)
        place, municipality = place_parts(hit.label if hit else query)
        return Place(
            lat=lat,
            lng=lng,
            label=hit.label if hit else query,
            country="BE",
            place_name=place,
            municipality=municipality,
        )
    hits = await geocode(query, limit=1)
    if not hits:
        raise ValueError(f"Geen plaats in België gevonden voor '{query}'.")
    hit = hits[0]
    place, municipality = place_parts(hit.label)
    return Place(
        lat=hit.lat,
        lng=hit.lng,
        label=hit.label,
        country="BE",
        place_name=place,
        municipality=municipality,
    )
