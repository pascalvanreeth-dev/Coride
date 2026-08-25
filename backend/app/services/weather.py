from __future__ import annotations

from typing import Any

from app.http import client
from app.services.geo import bearing, haversine_m

OPEN_METEO = "https://api.open-meteo.com/v1/forecast"


async def fetch_weather(lat: float, lng: float) -> dict[str, Any]:
    try:
        async with client() as http:
            response = await http.get(
                OPEN_METEO,
                params={
                    "latitude": round(lat, 4),
                    "longitude": round(lng, 4),
                    "current": "temperature_2m,precipitation,rain,weather_code,wind_speed_10m,wind_direction_10m",
                    "timezone": "Europe/Brussels",
                },
                timeout=8.0,
            )
            response.raise_for_status()
            current = (response.json() or {}).get("current") or {}
    except Exception:
        return {
            "available": False,
            "summary": "Weer niet beschikbaar",
            "alert": None,
            "suggest_shorter": False,
            "temperature_c": None,
            "precipitation_mm": 0,
            "wind_kmh": 0,
            "wind_direction": None,
            "code": None,
        }

    temp = current.get("temperature_2m")
    rain = float(current.get("rain") or current.get("precipitation") or 0)
    wind = float(current.get("wind_speed_10m") or 0)
    wind_dir = current.get("wind_direction_10m")
    code = current.get("weather_code")
    summary = _summary(code, temp, rain, wind)
    alert = None
    suggest = False
    if rain >= 1.5 or (isinstance(code, int) and code >= 61):
        alert = "Regen onderweg — kortere of meer beschutte knooppuntenlus aangeraden."
        suggest = True
    elif wind >= 45:
        alert = "Harde wind — kortere lus of met de wind mee plannen."
        suggest = True
    elif wind >= 30:
        alert = "Stevige wind — houd rekening met tegenwind."
    return {
        "available": True,
        "summary": summary,
        "alert": alert,
        "suggest_shorter": suggest,
        "temperature_c": temp,
        "precipitation_mm": round(rain, 1),
        "wind_kmh": round(wind, 1),
        "wind_direction": wind_dir,
        "code": code,
    }


def _summary(code: Any, temp: Any, rain: float, wind: float) -> str:
    label = {
        0: "helder",
        1: "grotendeels helder",
        2: "licht bewolkt",
        3: "bewolkt",
        45: "mistig",
        48: "mistig",
        51: "lichte motregen",
        53: "motregen",
        55: "dichte motregen",
        61: "lichte regen",
        63: "regen",
        65: "hevige regen",
        80: "buien",
        81: "buien",
        82: "zware buien",
        95: "onweer",
    }.get(int(code) if code is not None else -1, "wisselvallig")
    bits = [label]
    if temp is not None:
        bits.append(f"{round(float(temp))}°C")
    if rain >= 0.2:
        bits.append(f"{rain:.1f} mm regen")
    if wind >= 20:
        bits.append(f"wind {round(wind)} km/u")
    return " · ".join(bits)


def wind_against_segment(wind_dir: float | None, from_lat: float, from_lng: float, to_lat: float, to_lng: float) -> bool:
    if wind_dir is None:
        return False
    ride = bearing(from_lat, from_lng, to_lat, to_lng)
    # Wind comes FROM wind_dir; headwind when riding toward that direction.
    diff = abs((ride - float(wind_dir) + 180) % 360 - 180)
    return diff < 55 and haversine_m(from_lat, from_lng, to_lat, to_lng) > 400
