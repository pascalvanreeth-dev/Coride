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
    """Afstand van een punt tot een segment (meter, projectie op het segment)."""
    _, _, dist = snap_point_to_segment(lat, lng, a_lat, a_lng, b_lat, b_lng)
    return dist


def snap_point_to_segment(
    lat: float,
    lng: float,
    a_lat: float,
    a_lng: float,
    b_lat: float,
    b_lng: float,
) -> tuple[float, float, float]:
    lat_scale = 111_000.0
    lng_scale = 111_000.0 * max(0.2, math.cos(math.radians((a_lng + b_lng) / 2)))
    x = (lat - a_lat) * lat_scale
    y = (lng - a_lng) * lng_scale
    dx = (b_lat - a_lat) * lat_scale
    dy = (b_lng - a_lng) * lng_scale
    len2 = dx * dx + dy * dy
    if len2 == 0:
        return a_lat, a_lng, math.hypot(x, y)
    t = max(0.0, min(1.0, (x * dx + y * dy) / len2))
    snap_lat = a_lat + t * (b_lat - a_lat)
    snap_lng = a_lng + t * (b_lng - a_lng)
    dist = math.hypot((lat - snap_lat) * lat_scale, (lng - snap_lng) * lng_scale)
    return snap_lat, snap_lng, dist


def distance_point_to_geometry(lat: float, lng: float, geometry: list[list[float]]) -> float:
    if not geometry or len(geometry) < 2:
        return float("inf")
    step = 1 if len(geometry) <= 500 else max(1, len(geometry) // 160)
    best = float("inf")
    for index in range(0, len(geometry) - 1, step):
        _, _, dist = snap_point_to_segment(
            lat,
            lng,
            geometry[index][0],
            geometry[index][1],
            geometry[index + 1][0],
            geometry[index + 1][1],
        )
        best = min(best, dist)
    return best


def snap_point_on_geometry(
    lat: float,
    lng: float,
    geometry: list[list[float]],
) -> tuple[float, float, float]:
    snap_lat, snap_lng, dist, _ = snap_point_on_geometry_with_progress(lat, lng, geometry)
    return snap_lat, snap_lng, dist


def geometry_length_m(geometry: list[list[float]]) -> float:
    if not geometry or len(geometry) < 2:
        return 0.0
    total = 0.0
    for index in range(len(geometry) - 1):
        total += haversine_m(
            geometry[index][0],
            geometry[index][1],
            geometry[index + 1][0],
            geometry[index + 1][1],
        )
    return total


def point_on_geometry_at_progress(geometry: list[list[float]], progress_m: float) -> tuple[float, float]:
    if not geometry or len(geometry) < 2:
        return 0.0, 0.0
    total = geometry_length_m(geometry)
    if total <= 0:
        return float(geometry[0][0]), float(geometry[0][1])
    progress_m = progress_m % total
    remaining = progress_m
    for index in range(len(geometry) - 1):
        a_lat, a_lng = geometry[index][0], geometry[index][1]
        b_lat, b_lng = geometry[index + 1][0], geometry[index + 1][1]
        seg_len = haversine_m(a_lat, a_lng, b_lat, b_lng)
        if remaining <= seg_len or index == len(geometry) - 2:
            t = remaining / seg_len if seg_len > 0 else 0.0
            t = max(0.0, min(1.0, t))
            return a_lat + t * (b_lat - a_lat), a_lng + t * (b_lng - a_lng)
        remaining -= seg_len
    last = geometry[-1]
    return float(last[0]), float(last[1])


def midpoint_progress_on_loop(
    progress_a: float,
    progress_b: float,
    total_m: float,
) -> float:
    """Kortste boog langs een lus tussen twee posities (meter)."""
    if total_m <= 0:
        return 0.0
    forward = (progress_b - progress_a) % total_m
    backward = (progress_a - progress_b) % total_m
    if forward <= backward:
        return (progress_a + forward / 2) % total_m
    return (progress_a - backward / 2) % total_m


def snap_point_on_geometry_with_progress(
    lat: float,
    lng: float,
    geometry: list[list[float]],
) -> tuple[float, float, float, float]:
    """Snap op de route en geef afstand (m) en positie langs het traject (m)."""
    if not geometry or len(geometry) < 2:
        return lat, lng, float("inf"), 0.0
    best_lat, best_lng = lat, lng
    best_dist = float("inf")
    best_progress = 0.0
    cumulative = 0.0
    for index in range(len(geometry) - 1):
        a_lat, a_lng = geometry[index][0], geometry[index][1]
        b_lat, b_lng = geometry[index + 1][0], geometry[index + 1][1]
        seg_len = haversine_m(a_lat, a_lng, b_lat, b_lng)
        snap_lat, snap_lng, dist = snap_point_to_segment(lat, lng, a_lat, a_lng, b_lat, b_lng)
        if dist < best_dist:
            lat_scale = 111_000.0
            lng_scale = 111_000.0 * max(0.2, math.cos(math.radians((a_lng + b_lng) / 2)))
            dx = (b_lat - a_lat) * lat_scale
            dy = (b_lng - a_lng) * lng_scale
            len2 = dx * dx + dy * dy
            if len2 > 0:
                x = (lat - a_lat) * lat_scale
                y = (lng - a_lng) * lng_scale
                t = max(0.0, min(1.0, (x * dx + y * dy) / len2))
            else:
                t = 0.0
            best_lat, best_lng = snap_lat, snap_lng
            best_dist = dist
            best_progress = cumulative + t * seg_len
        cumulative += seg_len
    return best_lat, best_lng, best_dist, best_progress


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
