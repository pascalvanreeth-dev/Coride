from __future__ import annotations

import asyncio
from typing import Any

from app.config import settings
from app.http import client, routing_client
from app.models import Step
from app.services.geo import haversine_m

_MOD = {
    "uturn": "keer om",
    "sharp right": "scherp rechts",
    "right": "rechts",
    "slight right": "licht naar rechts",
    "straight": "rechtdoor",
    "slight left": "licht naar links",
    "left": "links",
    "sharp left": "scherp links",
}


def _clean_waypoints(waypoints: list[tuple[float, float]]) -> list[tuple[float, float]]:
    cleaned: list[tuple[float, float]] = []
    for point in waypoints:
        if cleaned and haversine_m(cleaned[-1][0], cleaned[-1][1], point[0], point[1]) < 12:
            continue
        cleaned.append(point)
    return cleaned


def _merge_geometries(parts: list[list[list[float]]]) -> list[list[float]]:
    geometry: list[list[float]] = []
    for part in parts:
        if not part:
            continue
        if geometry and haversine_m(geometry[-1][0], geometry[-1][1], part[0][0], part[0][1]) < 12:
            part = part[1:]
        geometry.extend(part)
    return geometry


async def bike_route(points: list[tuple[float, float]], *, retries: int = 1) -> dict[str, Any]:
    if len(points) < 2:
        raise ValueError("Een fietsroute heeft minstens twee punten nodig.")
    coords = ";".join(f"{lng:.6f},{lat:.6f}" for lat, lng in points)
    url = f"{settings.osrm_bike_url}/route/v1/driving/{coords}"
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            async with routing_client() as http:
                response = await http.get(
                    url,
                    params={
                        "overview": "full",
                        "geometries": "geojson",
                        "steps": "true",
                        "annotations": "false",
                        "alternatives": "false",
                        "continue_straight": "false",
                    },
                )
                response.raise_for_status()
                payload = response.json()
            if payload.get("code") != "Ok" or not payload.get("routes"):
                raise RuntimeError("Geen fietsroute gevonden tussen deze punten.")
            route = payload["routes"][0]
            geometry = [[lat, lng] for lng, lat in route["geometry"]["coordinates"]]
            return {
                "geometry": geometry,
                "distance_m": float(route["distance"]),
                "duration_s": float(route["duration"]),
                "steps": _parse_steps(route.get("legs") or []),
            }
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if attempt < retries:
                await asyncio.sleep(0.8)
    assert last_error is not None
    raise last_error


async def bike_route_via_waypoints(waypoints: list[tuple[float, float]]) -> dict[str, Any]:
    """Route that visits every waypoint in order (no shortcuts past knooppunten)."""
    cleaned = _clean_waypoints(waypoints)
    if len(cleaned) < 2:
        raise ValueError("Een fietsroute heeft minstens twee punten nodig.")
    if len(cleaned) <= 8:
        try:
            return await bike_route(cleaned, retries=1)
        except Exception:
            if len(cleaned) == 2:
                raise

    # Chunked stitching: every knooppunt remains a via-point; OSRM cannot skip chunks' endpoints.
    geometries: list[list[list[float]]] = []
    distance_m = 0.0
    duration_s = 0.0
    steps: list[Step] = []
    chunk_size = 6
    index = 0
    while index < len(cleaned) - 1:
        chunk = cleaned[index : index + chunk_size]
        if len(chunk) < 2:
            break
        try:
            segment = await bike_route(chunk, retries=1)
        except Exception:
            segment = None
            for pair_i in range(len(chunk) - 1):
                pair = [chunk[pair_i], chunk[pair_i + 1]]
                try:
                    piece = await bike_route(pair, retries=0)
                except Exception:
                    piece = {
                        "geometry": [[pair[0][0], pair[0][1]], [pair[1][0], pair[1][1]]],
                        "distance_m": haversine_m(pair[0][0], pair[0][1], pair[1][0], pair[1][1]),
                        "duration_s": 60.0,
                        "steps": [],
                    }
                geometries.append(piece["geometry"])
                distance_m += float(piece["distance_m"])
                duration_s += float(piece["duration_s"])
                steps.extend(piece.get("steps") or [])
            index += max(1, len(chunk) - 1)
            continue
        geometries.append(segment["geometry"])
        distance_m += float(segment["distance_m"])
        duration_s += float(segment["duration_s"])
        steps.extend(segment.get("steps") or [])
        index += max(1, len(chunk) - 1)

    geometry = _merge_geometries(geometries)
    if len(geometry) < 2:
        raise RuntimeError("Geen fietsroute gevonden tussen deze knooppunten.")
    return {
        "geometry": geometry,
        "distance_m": distance_m,
        "duration_s": max(60.0, duration_s),
        "steps": steps,
    }


def _parse_steps(legs: list[dict[str, Any]]) -> list[Step]:
    steps: list[Step] = []
    for leg in legs:
        for raw in leg.get("steps") or []:
            maneuver = raw.get("maneuver") or {}
            location = maneuver.get("location") or [0, 0]
            if len(location) < 2:
                continue
            name = (raw.get("name") or "").strip()
            mtype = maneuver.get("type") or "continue"
            if mtype == "notification":
                continue
            steps.append(
                Step(
                    instruction=_nl_instruction(maneuver, name),
                    type=mtype,
                    modifier=maneuver.get("modifier") or "",
                    distance_m=float(raw.get("distance") or 0),
                    lat=float(location[1]),
                    lng=float(location[0]),
                    name=name,
                )
            )
    return steps


def _nl_instruction(maneuver: dict[str, Any], name: str) -> str:
    mtype = maneuver.get("type") or "continue"
    modifier = _MOD.get(maneuver.get("modifier") or "", maneuver.get("modifier") or "")
    onto = f" op {name}" if name else ""
    toward = f" richting {name}" if name else ""
    if mtype == "depart":
        return f"Vertrek{onto}" if name else "Vertrek"
    if mtype == "arrive":
        return "Je bent er"
    if mtype == "turn":
        if modifier in ("rechtdoor", "straight"):
            return f"Ga rechtdoor{onto}"
        return f"Sla {modifier or 'af'} af{onto}"
    if mtype == "new name":
        return f"Ga verder{onto}" if name else "Ga verder"
    if mtype == "continue":
        return f"Ga rechtdoor{onto}"
    if mtype in ("roundabout", "rotary"):
        exit_num = maneuver.get("exit")
        extra = f", neem de {exit_num}e afslag" if exit_num else ""
        return f"Rijd de rotonde op{extra}{onto}"
    if mtype == "exit roundabout":
        return f"Verlaat de rotonde{onto}"
    if mtype == "roundabout turn":
        return f"Sla {modifier or 'af'} af op de rotonde{onto}"
    if mtype == "fork":
        return f"Houd {modifier or 'links'} aan{toward}"
    if mtype == "end of road":
        return f"Sla {modifier or 'af'} af aan het einde van de weg{onto}"
    if mtype == "merge":
        return f"Voeg {modifier} in{onto}".strip()
    if mtype == "on ramp":
        return f"Neem de oprit {modifier}{onto}".strip()
    if mtype == "off ramp":
        return f"Neem de afrit {modifier}{onto}".strip()
    return f"Ga verder{onto}" if name else "Ga verder"


async def round_trip_order(start: tuple[float, float], stops: list[tuple[float, float]]) -> list[int]:
    """Return stop indices in a sensible cycling order using OSRM trip, else clockwise."""
    if not stops:
        return []
    points = [start, *stops]
    coords = ";".join(f"{lng:.6f},{lat:.6f}" for lat, lng in points)
    url = f"{settings.osrm_bike_url}/trip/v1/driving/{coords}"
    try:
        async with client() as http:
            response = await http.get(
                url,
                params={
                    "roundtrip": "true",
                    "source": "first",
                    "destination": "any",
                    "geometries": "geojson",
                    "overview": "false",
                },
            )
            response.raise_for_status()
            payload = response.json()
        waypoints = payload.get("waypoints") or []
        indexed = []
        for i, waypoint in enumerate(waypoints):
            indexed.append((waypoint.get("waypoint_index", i), i))
        indexed.sort()
        order = [original - 1 for _, original in indexed if original != 0]
        if len(order) == len(stops):
            return order
    except Exception:
        pass
    from app.services.geo import bearing

    ranked = sorted(range(len(stops)), key=lambda i: bearing(start[0], start[1], stops[i][0], stops[i][1]))
    return ranked
