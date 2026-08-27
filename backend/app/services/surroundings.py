from __future__ import annotations

import asyncio
from typing import Any

from app.services import places as places_service
from app.services import pois as pois_service
from app.services.ai import describe_surroundings
from app.services.geo import haversine_m
from app.services.pois import _overpass

SURROUNDINGS_RADIUS_M = 350

LANDSCAPE_LABELS: dict[str, str] = {
    "farmland": "akkerland",
    "meadow": "weiland",
    "grass": "grasland",
    "wood": "bos",
    "forest": "bos",
    "wetland": "moeras",
    "water": "water",
    "scrub": "struikgewas",
    "heath": "heide",
    "sand": "zand",
    "beach": "strand",
    "orchard": "boomgaard",
    "vineyard": "wijngaard",
    "reservoir": "water",
    "river": "rivier",
    "stream": "beek",
    "canal": "kanaal",
    "ditch": "gracht",
}


async def live_surroundings(
    lat: float,
    lng: float,
    interests: list[str],
    *,
    explanation_level: str = "normaal",
    heading: float | None = None,
) -> dict[str, Any]:
    themes = pois_service._unique_interests(interests)
    place_task = places_service.place_context(lat, lng)
    poi_task = pois_service.fetch_pois(lat, lng, SURROUNDINGS_RADIUS_M, themes)
    landscape_task = fetch_landscape_hints(lat, lng, SURROUNDINGS_RADIUS_M)
    place_ctx, pois, landscape = await asyncio.gather(place_task, poi_task, landscape_task)

    ranked = _rank_nearby(pois, lat, lng, themes)
    highlights = [
        {
            "name": poi["name"],
            "kind": poi.get("kind_label") or poi.get("kind") or "plek",
            "interest": poi.get("interest") or themes[0],
            "distance_m": round(haversine_m(lat, lng, poi["lat"], poi["lng"])),
        }
        for poi in ranked[:5]
    ]

    ai_text = await describe_surroundings(
        place_ctx,
        ranked[:6],
        landscape,
        themes,
        explanation_level,
        heading,
    )
    summary = ai_text or _fallback_summary(place_ctx, ranked, landscape, themes)
    return {
        "summary": summary,
        "place_name": place_ctx.get("place_name") or place_ctx.get("municipality") or "",
        "highlights": highlights,
        "ai_used": bool(ai_text),
    }


def _rank_nearby(
    pois: list[dict[str, Any]],
    lat: float,
    lng: float,
    interests: list[str],
) -> list[dict[str, Any]]:
    interest_set = set(interests)
    scored: list[tuple[float, dict[str, Any]]] = []
    for poi in pois:
        score = 0.0
        if poi.get("interest") in interest_set:
            score += 4.0
        if poi.get("wikipedia") or poi.get("wikidata"):
            score += 2.0
        dist = haversine_m(lat, lng, poi["lat"], poi["lng"])
        score -= dist / 500.0
        scored.append((score, poi))
    scored.sort(key=lambda item: item[0], reverse=True)
    return [poi for _, poi in scored]


def _fallback_summary(
    place_ctx: dict[str, Any],
    pois: list[dict[str, Any]],
    landscape: list[str],
    interests: list[str],
) -> str:
    place = place_ctx.get("place_name") or place_ctx.get("municipality") or ""
    parts: list[str] = []
    if place:
        parts.append(f"Je fietst door {place}.")
    if landscape:
        parts.append(f"Het landschap hier is vooral {', '.join(landscape[:3])}.")
    if pois:
        names = ", ".join(p["name"] for p in pois[:2])
        parts.append(f"In een straal van 350 meter: {names}.")
    elif interests:
        parts.append("Er zijn weinig benoemde plekken in de buurt op de kaart.")
    fact = (place_ctx.get("local_fact") or "").strip()
    if fact and len(parts) < 3:
        parts.append(fact)
    return " ".join(parts).strip() or "Geen extra omgevingsinfo beschikbaar op deze plek."


async def fetch_landscape_hints(lat: float, lng: float, radius_m: int) -> list[str]:
    query = (
        f"[out:json][timeout:10];"
        f'('
        f'nwr(around:{radius_m},{lat:.5f},{lng:.5f})["natural"];'
        f'nwr(around:{radius_m},{lat:.5f},{lng:.5f})["landuse"];'
        f'nwr(around:{radius_m},{lat:.5f},{lng:.5f})["waterway"];'
        f");"
        f"out tags 25;"
    )
    try:
        data = await _overpass(query)
    except Exception:
        return []
    labels: list[str] = []
    seen: set[str] = set()
    for element in data.get("elements") or []:
        tags = element.get("tags") or {}
        for key in ("natural", "landuse", "waterway"):
            raw = tags.get(key)
            if not raw:
                continue
            label = LANDSCAPE_LABELS.get(raw, raw.replace("_", " "))
            if label in seen:
                continue
            seen.add(label)
            labels.append(label)
            if len(labels) >= 5:
                return labels
    return labels
