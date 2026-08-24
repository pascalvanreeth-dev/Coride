from __future__ import annotations

import math
from typing import Any


def haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    radius = 6_371_000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlmb / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(a))


def bearing(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dlmb = math.radians(lng2 - lng1)
    x = math.sin(dlmb) * math.cos(phi2)
    y = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(dlmb)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def point_to_segment_m(
    lat: float,
    lng: float,
    a_lat: float,
    a_lng: float,
    b_lat: float,
    b_lng: float,
) -> float:
    """Approximate distance from a point to a start-end corridor."""
    to_start = haversine_m(lat, lng, a_lat, a_lng)
    to_end = haversine_m(lat, lng, b_lat, b_lng)
    corridor = haversine_m(a_lat, a_lng, b_lat, b_lng) or 1
    # If the point is "between" the ends, the extra length vs straight line is a detour proxy.
    extra = to_start + to_end - corridor
    return max(0.0, extra / 2)


def osm_center(element: dict[str, Any]) -> tuple[float, float] | None:
    if element.get("type") == "node":
        return float(element["lat"]), float(element["lon"])
    center = element.get("center")
    if center:
        return float(center["lat"]), float(center["lon"])
    return None


def offset_point(lat: float, lng: float, distance_m: float, bearing_deg: float) -> tuple[float, float]:
    radius = 6_371_000
    angular = distance_m / radius
    bearing = math.radians(bearing_deg)
    lat1 = math.radians(lat)
    lng1 = math.radians(lng)
    lat2 = math.asin(
        math.sin(lat1) * math.cos(angular) + math.cos(lat1) * math.sin(angular) * math.cos(bearing)
    )
    lng2 = lng1 + math.atan2(
        math.sin(bearing) * math.sin(angular) * math.cos(lat1),
        math.cos(angular) - math.sin(lat1) * math.sin(lat2),
    )
    return math.degrees(lat2), math.degrees(lng2)


def unique_key(name: str, lat: float, lng: float) -> str:
    return f"{name.strip().lower()}|{round(lat, 4)}|{round(lng, 4)}"
