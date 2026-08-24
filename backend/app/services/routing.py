from __future__ import annotations

from typing import Any

from app.config import settings
from app.http import client
from app.models import Step

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


async def bike_route(points: list[tuple[float, float]]) -> dict[str, Any]:
    if len(points) < 2:
        raise ValueError("Een fietsroute heeft minstens twee punten nodig.")
    coords = ";".join(f"{lng:.6f},{lat:.6f}" for lat, lng in points)
    url = f"{settings.osrm_bike_url}/route/v1/driving/{coords}"
    async with client() as http:
        response = await http.get(
            url,
            params={
                "overview": "full",
                "geometries": "geojson",
                "steps": "true",
                "annotations": "false",
                "alternatives": "false",
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
