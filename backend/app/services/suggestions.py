from __future__ import annotations

from typing import Any

from app.models import Locality
from app.services import icoonroutes as icoon_service

INTEREST_LABELS: dict[str, str] = {
    "geschiedenis": "Geschiedenis",
    "natuur": "Natuur & vegetatie",
    "landbouw": "Landbouw",
    "horeca": "Horeca",
    "oorlog": "Oorlog",
    "architectuur": "Architectuur",
    "activiteiten": "Activiteiten",
    "evenementen": "Evenementen",
}


async def get_route_by_id(route_id: str) -> dict[str, Any] | None:
    return await icoon_service.get_route_by_id(route_id)


def merge_interests(route: dict[str, Any], user_interests: list[str] | None) -> list[str]:
    merged: list[str] = []
    for item in list(route.get("interests") or []) + list(user_interests or []):
        if item and item not in merged:
            merged.append(item)
    return merged or ["geschiedenis"]


def merge_notes(route: dict[str, Any], user_notes: str) -> str:
    base = (route.get("notes") or "").strip()
    extra = (user_notes or "").strip()
    if base and extra and extra not in base:
        return f"{base} {extra}"
    return extra or base


def merge_localities(route: dict[str, Any], existing: list[Locality]) -> list[Locality]:
    seen = {item.name.lower() for item in existing}
    merged = list(existing)
    for item in route.get("localities") or []:
        name = (item.get("name") or "").strip()
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        merged.append(
            Locality(
                name=name,
                municipality=name,
                population=None,
                fact=(item.get("fact") or "").strip(),
                lat=float(item["lat"]),
                lng=float(item["lng"]),
            )
        )
    return merged


def catalog_intro(route: dict[str, Any], interests: list[str], km: float, start_label: str) -> str:
    themes = ", ".join(INTEREST_LABELS.get(item, item) for item in interests[:5])
    bits = [
        f"Icoonroute Toerisme Vlaanderen: {route['title']}.",
        f"Vanaf {start_label.split(',')[0]} fiets je ongeveer {km} km op het officiële traject.",
    ]
    if themes:
        bits.append(f"Thema's: {themes}.")
    if route.get("notes"):
        bits.append(route["notes"])
    return " ".join(bits)


def _interest_score(route: dict[str, Any], interests: list[str] | None) -> int:
    if not interests:
        return 0
    route_set = set(route.get("interests") or [])
    return len(route_set.intersection(interests))


async def suggest_routes(
    lat: float,
    lng: float,
    interests: list[str] | None = None,
    used_ids: list[str] | None = None,
    limit: int = 10,
) -> list[dict[str, Any]]:
    used = set(used_ids or [])
    user_interests = interests or []
    routes = await icoon_service.list_route_summaries(lat, lng)

    for route in routes:
        route["used_before"] = route["id"] in used
        route["swapped_from"] = None
        route["match_score"] = _interest_score(route, user_interests)

    routes.sort(
        key=lambda item: (
            item["used_before"],
            -item["match_score"],
            item.get("distance_from_you_km") or 999,
            item.get("rank") or 99,
        )
    )
    for index, route in enumerate(routes[:limit], start=1):
        route["rank"] = index
    return routes[:limit]
