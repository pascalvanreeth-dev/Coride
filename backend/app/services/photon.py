from __future__ import annotations

from typing import Any

from app.http import client

PHOTON_URL = "https://photon.komoot.io/api"
BELGIUM_BBOX = "2.3,49.45,6.45,51.55"


def _display_name(properties: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in ("name", "city", "county", "state", "country"):
        value = properties.get(key)
        if value and (not parts or parts[-1] != value):
            parts.append(str(value))
    return ", ".join(parts) if parts else "Onbekende plaats"


def _importance(properties: dict[str, Any]) -> float:
    by_type = {
        "city": 0.9,
        "town": 0.82,
        "municipality": 0.8,
        "village": 0.65,
        "hamlet": 0.45,
        "suburb": 0.55,
    }
    return by_type.get(str(properties.get("type") or ""), 0.5)


def _normalize(features: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for feature in features:
        geometry = feature.get("geometry") or {}
        coords = geometry.get("coordinates") or []
        properties = feature.get("properties") or {}
        if len(coords) < 2:
            continue
        country = str(properties.get("countrycode") or "").upper()
        if country and country != "BE":
            continue
        lng, lat = float(coords[0]), float(coords[1])
        rows.append(
            {
                "lat": lat,
                "lon": lng,
                "display_name": _display_name(properties),
                "importance": _importance(properties),
                "place_rank": 16 if properties.get("type") in {"city", "town", "village", "municipality"} else 20,
            }
        )
    return rows


async def search(query: str, limit: int = 5) -> list[dict[str, Any]]:
    async with client() as http:
        response = await http.get(
            PHOTON_URL,
            params={
                "q": query,
                "limit": max(limit, 5),
                "lang": "nl",
                "bbox": BELGIUM_BBOX,
            },
        )
        response.raise_for_status()
        payload = response.json()
    features = payload.get("features") if isinstance(payload, dict) else []
    return _normalize(features if isinstance(features, list) else [])[:limit]
