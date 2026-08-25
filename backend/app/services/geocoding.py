from __future__ import annotations

import re

import httpx

from app.models import GeocodeHit, Place
from app.services import nominatim

BELGIUM = {"min_lng": 2.3, "min_lat": 49.45, "max_lng": 6.45, "max_lat": 51.55}
COORD_RE = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*$")


def in_belgium(lat: float, lng: float) -> bool:
    return (
        BELGIUM["min_lat"] <= lat <= BELGIUM["max_lat"]
        and BELGIUM["min_lng"] <= lng <= BELGIUM["max_lng"]
    )


def coord_label(lat: float, lng: float) -> str:
    return f"{lat:.5f}, {lng:.5f}"


async def geocode(query: str, limit: int = 5) -> list[GeocodeHit]:
    try:
        rows = await nominatim.search(query, limit=limit)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 429:
            return []
        raise
    hits: list[GeocodeHit] = []
    for row in rows:
        lat = float(row["lat"])
        lng = float(row["lon"])
        if not in_belgium(lat, lng):
            continue
        hits.append(GeocodeHit(label=row.get("display_name", query), lat=lat, lng=lng))
    return hits


async def reverse(lat: float, lng: float) -> GeocodeHit | None:
    if not in_belgium(lat, lng):
        raise ValueError("Dit GPS-punt ligt buiten België.")
    try:
        row = await nominatim.reverse(lat, lng, zoom=16)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code in {429, 403}:
            return GeocodeHit(label=coord_label(lat, lng), lat=lat, lng=lng)
        raise
    if not row or row.get("error"):
        return GeocodeHit(label=coord_label(lat, lng), lat=lat, lng=lng)
    return GeocodeHit(label=row.get("display_name") or coord_label(lat, lng), lat=lat, lng=lng)


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
