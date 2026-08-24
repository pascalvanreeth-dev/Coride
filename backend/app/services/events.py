from __future__ import annotations

from datetime import date
from typing import Any

from app.http import client
from app.services.geo import haversine_m

BRUSSELS_EVENTS = (
    "https://opendata.brussels.be/api/explore/v2.1/catalog/datasets/"
    "bruxelles-evenements/records"
)


async def fetch_events(lat: float, lng: float, radius_m: int) -> list[dict[str, Any]]:
    today = date.today().isoformat()
    events: list[dict[str, Any]] = []
    try:
        async with client() as http:
            response = await http.get(
                BRUSSELS_EVENTS,
                params={"limit": 80, "order_by": "date_begin desc"},
            )
            if response.status_code != 200:
                return []
            for row in response.json().get("results", []):
                point = _point(row)
                if not point:
                    continue
                elat, elng = point
                if haversine_m(lat, lng, elat, elng) > radius_m + 8000:
                    continue
                name = _name(row)
                if not name:
                    continue
                when = str(row.get("date_begin") or row.get("date") or "")
                if when and when[:10] < today:
                    # keep only current or upcoming if a date exists
                    end = str(row.get("date_end") or when)
                    if end[:10] < today:
                        continue
                description = (
                    row.get("description_fr")
                    or row.get("description_nl")
                    or row.get("description")
                    or "Evenement in Brussel, via open data van de stad."
                )
                if isinstance(description, dict):
                    description = description.get("nl") or description.get("fr") or ""
                events.append(
                    {
                        "id": f"event-{row.get('id', name)}",
                        "name": name,
                        "lat": elat,
                        "lng": elng,
                        "kind": "evenement",
                        "kind_label": "evenement",
                        "interest": "evenementen",
                        "source": "Open Data Brussels",
                        "wikipedia": None,
                        "wikidata": None,
                        "description": str(description)[:600],
                        "heritage": "",
                    }
                )
    except Exception:
        return []
    return events[:25]


def _name(row: dict[str, Any]) -> str:
    for key in ("title_nl", "title_fr", "title", "nom", "name"):
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, dict):
            text = value.get("nl") or value.get("fr") or ""
            if text:
                return str(text)
    return ""


def _point(row: dict[str, Any]) -> tuple[float, float] | None:
    for key in ("geo_point_2d", "location", "geopoint", "geo_point"):
        value = row.get(key)
        if isinstance(value, dict) and "lat" in value and "lon" in value:
            return float(value["lat"]), float(value["lon"])
        if isinstance(value, dict) and "lat" in value and "lng" in value:
            return float(value["lat"]), float(value["lng"])
        if isinstance(value, list) and len(value) == 2:
            # OpenDataSoft sometimes returns [lat, lon]
            return float(value[0]), float(value[1])
    return None
