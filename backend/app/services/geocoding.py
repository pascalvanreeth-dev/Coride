from __future__ import annotations

import re

from app.config import settings
from app.http import client
from app.models import GeocodeHit, Place

BELGIUM = {"min_lng": 2.3, "min_lat": 49.45, "max_lng": 6.45, "max_lat": 51.55}
COORD_RE = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*$")


def in_belgium(lat: float, lng: float) -> bool:
    return (
        BELGIUM["min_lat"] <= lat <= BELGIUM["max_lat"]
        and BELGIUM["min_lng"] <= lng <= BELGIUM["max_lng"]
    )


async def geocode(query: str, limit: int = 5) -> list[GeocodeHit]:
    async with client() as http:
        response = await http.get(
            f"{settings.nominatim_url}/search",
            params={
                "q": query,
                "format": "jsonv2",
                "addressdetails": 1,
                "limit": limit,
                "countrycodes": "be",
                "accept-language": "nl,fr,de,en",
            },
        )
        response.raise_for_status()
        hits: list[GeocodeHit] = []
        for row in response.json():
            lat = float(row["lat"])
            lng = float(row["lon"])
            if not in_belgium(lat, lng):
                continue
            hits.append(GeocodeHit(label=row.get("display_name", query), lat=lat, lng=lng))
        return hits


async def geocode_one(query: str) -> Place:
    match = COORD_RE.match(query or "")
    if match:
        lat = float(match.group(1))
        lng = float(match.group(2))
        if not in_belgium(lat, lng):
            raise ValueError("Dit GPS-punt ligt buiten België.")
        hit = await reverse(lat, lng)
        return Place(lat=lat, lng=lng, label=hit.label if hit else query, country="BE")
    hits = await geocode(query, limit=1)
    if not hits:
        raise ValueError(f"Geen plaats in België gevonden voor '{query}'.")
    hit = hits[0]
    return Place(lat=hit.lat, lng=hit.lng, label=hit.label, country="BE")


async def reverse(lat: float, lng: float) -> GeocodeHit | None:
    if not in_belgium(lat, lng):
        raise ValueError("Dit GPS-punt ligt buiten België.")
    async with client() as http:
        response = await http.get(
            f"{settings.nominatim_url}/reverse",
            params={
                "lat": lat,
                "lon": lng,
                "format": "jsonv2",
                "addressdetails": 1,
                "zoom": 18,
                "accept-language": "nl,fr,de,en",
            },
        )
        response.raise_for_status()
        row = response.json()
    if not row or row.get("error"):
        return GeocodeHit(label=f"{lat:.5f}, {lng:.5f}", lat=lat, lng=lng)
    return GeocodeHit(label=row.get("display_name") or f"{lat:.5f}, {lng:.5f}", lat=lat, lng=lng)
