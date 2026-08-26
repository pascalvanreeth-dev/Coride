from __future__ import annotations

import asyncio
import re

import httpx

from app.models import GeocodeHit, Place
from app.services import nominatim, photon

BELGIUM = {"min_lng": 2.3, "min_lat": 49.45, "max_lng": 6.45, "max_lat": 51.55}
COORD_RE = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*$")


def in_belgium(lat: float, lng: float) -> bool:
    return (
        BELGIUM["min_lat"] <= lat <= BELGIUM["max_lat"]
        and BELGIUM["min_lng"] <= lng <= BELGIUM["max_lng"]
    )


def coord_label(lat: float, lng: float) -> str:
    return f"{lat:.5f}, {lng:.5f}"


def _query_variants(query: str) -> list[str]:
    cleaned = query.strip()
    if not cleaned:
        return []
    variants = [cleaned]
    lower = cleaned.lower()
    if "belgi" not in lower and "belg" not in lower:
        variants.append(f"{cleaned}, België")
    return variants


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


async def _search_rows(query: str, limit: int = 5) -> list[dict]:
    try:
        return await nominatim.search(query, limit=limit)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 429:
            await asyncio.sleep(2.0)
            try:
                return await nominatim.search(query, limit=limit)
            except httpx.HTTPStatusError:
                return await photon.search(query, limit=limit)
        if exc.response.status_code == 403:
            return await photon.search(query, limit=limit)
        raise
    except httpx.HTTPError:
        return await photon.search(query, limit=limit)


async def geocode(query: str, limit: int = 5) -> list[GeocodeHit]:
    hits: list[GeocodeHit] = []
    seen: set[str] = set()
    for variant in _query_variants(query):
        rows = await _search_rows(variant, limit=limit)
        for hit in _rows_to_hits(rows, variant):
            key = f"{hit.lat:.4f},{hit.lng:.4f}"
            if key in seen:
                continue
            seen.add(key)
            hits.append(hit)
        if hits:
            break
    return hits[:limit]


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
