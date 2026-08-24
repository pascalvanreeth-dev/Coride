from __future__ import annotations

from typing import Any

import httpx

from app.config import settings
from app.http import client
from app.services.geo import osm_center, unique_key


OVERPASS_FILTERS: dict[str, list[str]] = {
    "geschiedenis": [
        '["historic"]',
        '["tourism"="museum"]',
        '["tourism"="artwork"]',
        '["amenity"="place_of_worship"]',
    ],
    "activiteiten": [
        '["leisure"~"park|nature_reserve|garden|sports_centre"]',
        '["tourism"~"attraction|viewpoint"]',
    ],
    "evenementen": [
        '["amenity"~"theatre|arts_centre|marketplace|community_centre|events_venue|cinema"]',
        '["leisure"="stadium"]',
    ],
}


KIND_LABELS = {
    "castle": "kasteel",
    "monument": "monument",
    "memorial": "gedenkteken",
    "ruins": "ruïne",
    "archaeological_site": "archeologische site",
    "museum": "museum",
    "artwork": "kunstwerk",
    "place_of_worship": "kerk of gebedshuis",
    "cathedral": "kathedraal",
    "church": "kerk",
    "park": "park",
    "nature_reserve": "natuurgebied",
    "garden": "tuin",
    "attraction": "attractie",
    "viewpoint": "uitkijkpunt",
    "theatre": "theater",
    "arts_centre": "kunstencentrum",
    "marketplace": "markt",
    "events_venue": "evenementenlocatie",
    "concert_hall": "concertzaal",
    "stadium": "stadion",
    "cafe": "café",
    "pub": "café/pub",
    "bar": "bar",
    "restaurant": "restaurant",
    "ice_cream": "ijssalon",
    "biergarten": "biertuin",
}


def classify(tags: dict[str, str], interests: list[str]) -> tuple[str, str]:
    historic = tags.get("historic", "")
    tourism = tags.get("tourism", "")
    amenity = tags.get("amenity", "")
    leisure = tags.get("leisure", "")
    building = tags.get("building", "")

    if amenity in {"cafe", "pub", "bar", "restaurant", "ice_cream", "biergarten"}:
        return "activiteiten", amenity
    if "geschiedenis" in interests and (
        historic or tags.get("heritage") or tourism in {"museum", "artwork"} or amenity == "place_of_worship"
    ):
        kind = historic or tourism or amenity or building or "erfgoed"
        return "geschiedenis", kind
    if "evenementen" in interests and (
        amenity in {"theatre", "arts_centre", "marketplace", "community_centre", "events_venue", "cinema", "concert_hall"}
        or leisure in {"stadium", "bandstand"}
    ):
        kind = amenity or leisure or "evenementenlocatie"
        return "evenementen", kind
    if "activiteiten" in interests:
        kind = leisure or tourism or "activiteit"
        return "activiteiten", kind
    kind = historic or tourism or amenity or leisure or "plek"
    interest = interests[0] if interests else "geschiedenis"
    return interest, kind


def kind_label(kind: str) -> str:
    return KIND_LABELS.get(kind, kind.replace("_", " "))


async def fetch_pois(
    lat: float,
    lng: float,
    radius_m: int,
    interests: list[str],
    extra_point: tuple[float, float] | None = None,
) -> list[dict[str, Any]]:
    radius_m = max(2500, min(radius_m, 22000))
    points = [(lat, lng)]
    if extra_point:
        points.append(extra_point)
    wanted = interests or ["geschiedenis"]
    clauses: list[str] = []
    for plat, plng in points:
        around = f"around:{radius_m},{plat:.5f},{plng:.5f}"
        for interest in wanted:
            for filt in OVERPASS_FILTERS.get(interest, []):
                clauses.append(f'nwr{filt}["name"]({around});')

    query = f"[out:json][timeout:25];({''.join(clauses)});out center 100;"
    data = await _overpass(query)
    seen: set[str] = set()
    pois: list[dict[str, Any]] = []
    for element in data.get("elements", []):
        tags = element.get("tags") or {}
        name = (tags.get("name:nl") or tags.get("name") or "").strip()
        if not name:
            continue
        center = osm_center(element)
        if not center:
            continue
        plat, plng = center
        key = unique_key(name, plat, plng)
        if key in seen:
            continue
        seen.add(key)
        interest, kind = classify(tags, wanted)
        pois.append(
            {
                "id": str(element.get("id")),
                "name": name,
                "lat": plat,
                "lng": plng,
                "kind": kind,
                "kind_label": kind_label(kind),
                "interest": interest,
                "source": "OpenStreetMap",
                "wikipedia": tags.get("wikipedia"),
                "wikidata": tags.get("wikidata"),
                "description": tags.get("description:nl") or tags.get("description") or "",
                "heritage": tags.get("heritage") or tags.get("heritage:operator") or "",
            }
        )
    return pois


HORECA_FILTER = '["amenity"~"cafe|pub|bar|restaurant|ice_cream|biergarten"]'


def notes_want_horeca(notes: str) -> bool:
    text = (notes or "").lower()
    keys = (
        "cafe", "café", "koffie", "koffi", "coffee", "pub", "bar", "bier",
        "terras", "restaurant", "eten", "lunch", "eetcafe", "eetcafé", "ijs",
    )
    return any(key in text for key in keys)


async def fetch_horeca(
    lat: float,
    lng: float,
    radius_m: int,
    extra_point: tuple[float, float] | None = None,
) -> list[dict[str, Any]]:
    radius_m = max(2500, min(radius_m, 16000))
    points = [(lat, lng)]
    if extra_point:
        points.append(extra_point)
    clauses = [
        f'nwr{HORECA_FILTER}["name"](around:{radius_m},{plat:.5f},{plng:.5f});'
        for plat, plng in points
    ]
    query = f"[out:json][timeout:15];({''.join(clauses)});out center 80;"
    try:
        data = await _overpass(query)
    except Exception:
        return []
    seen: set[str] = set()
    pois: list[dict[str, Any]] = []
    for element in data.get("elements", []):
        tags = element.get("tags") or {}
        name = (tags.get("name:nl") or tags.get("name") or "").strip()
        if not name:
            continue
        center = osm_center(element)
        if not center:
            continue
        key = unique_key(name, center[0], center[1])
        if key in seen:
            continue
        seen.add(key)
        kind = tags.get("amenity") or "cafe"
        pois.append(
            {
                "id": f"horeca-{element.get('id')}",
                "name": name,
                "lat": center[0],
                "lng": center[1],
                "kind": kind,
                "kind_label": kind_label(kind),
                "interest": "activiteiten",
                "source": "OpenStreetMap",
                "wikipedia": tags.get("wikipedia"),
                "wikidata": tags.get("wikidata"),
                "description": tags.get("description:nl") or tags.get("description") or "",
                "heritage": "",
            }
        )
    return pois


async def _overpass(query: str) -> dict[str, Any]:
    errors: list[str] = []
    async with client() as http:
        for url in settings.overpass_urls.split(","):
            target = url.strip()
            try:
                response = await http.post(
                    target,
                    content=query.encode("utf-8"),
                    headers={"Content-Type": "text/plain; charset=utf-8"},
                    timeout=httpx.Timeout(12.0, connect=8.0),
                )
                if response.status_code >= 400:
                    errors.append(f"{target} -> HTTP {response.status_code}")
                    continue
                payload = response.json()
                remark = str(payload.get("remark") or "")
                elements = payload.get("elements") or []
                if "error" in remark.lower() or "runtime error" in remark.lower():
                    errors.append(f"{target} -> {remark[:120]}")
                    continue
                if elements:
                    return payload
                errors.append(f"{target} -> 0 resultaten")
            except Exception as exc:  # noqa: BLE001 - try next mirror
                errors.append(f"{target} -> {type(exc).__name__}: {exc or 'geen bericht'}")
    raise RuntimeError("Overpass API reageerde niet: " + "; ".join(errors))
