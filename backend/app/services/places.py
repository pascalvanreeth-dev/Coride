from __future__ import annotations

import math
import re
from typing import Any

import httpx

from app.config import settings
from app.http import client
from app.services.wikipedia import first_sentences, search_wikipedia_title
from app.services import nominatim
from app.services.geocoding import in_belgium
from app.services.geo import haversine_m

WIKI_HEADERS = {"User-Agent": settings.wikipedia_user_agent, "Api-User-Agent": settings.wikipedia_user_agent}


async def place_context(lat: float, lng: float) -> dict[str, Any]:
    if not in_belgium(lat, lng):
        return {"place_name": "", "municipality": "", "population": None, "local_fact": "", "known_for": []}
    try:
        row = await nominatim.reverse(lat, lng, zoom=14, extratags=True, namedetails=True)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code in {429, 403}:
            return {"place_name": "", "municipality": "", "population": None, "local_fact": "", "known_for": []}
        raise
    address = row.get("address") or {}
    place = (
        address.get("village")
        or address.get("town")
        or address.get("city")
        or address.get("municipality")
        or address.get("hamlet")
        or address.get("suburb")
        or ""
    )
    municipality = address.get("municipality") or address.get("city") or address.get("county") or place
    population = _population(row.get("extratags") or {})
    wiki = await _wiki_place(place or municipality)
    return {
        "place_name": place,
        "municipality": municipality,
        "population": population or wiki.get("population"),
        "local_fact": wiki.get("summary") or "",
        "known_for": wiki.get("known_for") or [],
        "wikipedia_url": wiki.get("url") or "",
    }


async def enrich_stops_with_places(stops: list[dict[str, Any]], interests: list[str]) -> list[dict[str, Any]]:
    cache: dict[str, dict[str, Any]] = {}
    for stop in stops:
        key = f"{round(stop['lat'], 3)}|{round(stop['lng'], 3)}"
        if key not in cache:
            try:
                cache[key] = await place_context(stop["lat"], stop["lng"])
            except Exception:
                cache[key] = {"place_name": "", "municipality": "", "population": None, "local_fact": "", "known_for": []}
        ctx = cache[key]
        stop["place_name"] = ctx.get("place_name") or ""
        stop["population"] = ctx.get("population")
        stop["local_fact"] = _fact_for_interests(ctx, interests)
    return stops


def assign_sides(stops: list[dict[str, Any]], geometry: list[list[float]]) -> list[dict[str, Any]]:
    if not geometry or len(geometry) < 2:
        for stop in stops:
            stop["side"] = ""
        return stops
    for stop in stops:
        idx = _nearest_index(geometry, stop["lat"], stop["lng"])
        a = geometry[max(0, idx - 1)]
        b = geometry[min(len(geometry) - 1, idx + 1)]
        stop["side"] = _side_of(a[0], a[1], b[0], b[1], stop["lat"], stop["lng"])
    return stops


def _population(extratags: dict[str, Any]) -> int | None:
    raw = extratags.get("population") or extratags.get("population:date")
    if not raw:
        return None
    digits = re.sub(r"[^\d]", "", str(raw).split(";")[0])
    if not digits:
        return None
    try:
        return int(digits)
    except ValueError:
        return None


async def _wiki_place(name: str) -> dict[str, Any]:
    title = (name or "").strip()
    if len(title) < 2:
        return {}

    async def fetch_page(page_title: str) -> dict[str, Any]:
        async with client() as http:
            response = await http.get(
                f"https://nl.wikipedia.org/api/rest_v1/page/summary/{page_title.replace(' ', '_')}",
                headers={**WIKI_HEADERS, "Accept": "application/json"},
            )
            if response.status_code != 200:
                return {}
            payload = response.json() or {}
            extract = (payload.get("extract") or "").strip()
            if not extract:
                return {}
            return {
                "extract": extract,
                "type": (payload.get("type") or "").lower(),
                "url": payload.get("content_urls", {}).get("desktop", {}).get("page") or "",
            }

    data = await fetch_page(title)
    extract = (data.get("extract") or "").strip()
    if _is_disambiguation(extract, data.get("type", "")):
        resolved = None
        for candidate in (f"{title} (België)", f"{title} (stad)"):
            alt = await fetch_page(candidate)
            alt_extract = (alt.get("extract") or "").strip()
            if alt_extract and not _is_disambiguation(alt_extract, alt.get("type", "")):
                resolved = alt
                break
        if not resolved:
            search_title = await search_wikipedia_title(title, "nl")
            if search_title and search_title.lower() != title.lower():
                alt = await fetch_page(search_title)
                alt_extract = (alt.get("extract") or "").strip()
                if alt_extract and not _is_disambiguation(alt_extract, alt.get("type", "")):
                    resolved = alt
        data = resolved or {}

    extract = (data.get("extract") or "").strip()
    if not extract or _is_disambiguation(extract, data.get("type", "")):
        return {}

    summary = _place_blurb(extract)
    pop = _population_from_text(extract)
    return {
        "summary": summary,
        "url": data.get("url") or "",
        "population": pop,
        "known_for": [],
    }


def _is_disambiguation(text: str, page_type: str = "") -> bool:
    if page_type == "disambiguation":
        return True
    lowered = (text or "").lower()
    return "kan verwijzen naar" in lowered or "may refer to" in lowered


def _place_blurb(text: str, *, sentences: int = 2, max_chars: int = 280) -> str:
    blurb = first_sentences((text or "").strip(), sentences)
    if len(blurb) <= max_chars:
        return blurb
    cut = blurb[: max_chars - 1].rsplit(" ", 1)[0].strip()
    return f"{cut}…" if cut else blurb[: max_chars - 1].strip() + "…"


def _population_from_text(text: str) -> int | None:
    match = re.search(r"([\d.\s]+)\s*inwoners", text or "", re.I)
    if not match:
        return None
    digits = re.sub(r"[^\d]", "", match.group(1))
    return int(digits) if digits else None
def _fact_for_interests(ctx: dict[str, Any], interests: list[str]) -> str:
    fact = (ctx.get("local_fact") or "").strip()
    place = ctx.get("place_name") or ctx.get("municipality") or "deze streek"
    pop = ctx.get("population")
    interest_set = set(interests or [])

    if "natuur" in interest_set:
        nature_hint = _nature_hint(fact, place)
        if nature_hint:
            return nature_hint

    if not fact:
        if pop:
            return f"{place} telt ongeveer {pop:,} inwoners.".replace(",", ".")
        return f"Je rijdt door {place}."

    if "oorlog" in interest_set and not any(word in fact.lower() for word in ("oorlog", "slag", "front", "memoriaal")):
        return fact
    if "landbouw" in interest_set and not any(word in fact.lower() for word in ("landbouw", "fruit", "hop", "wijn", "boomgaard")):
        return fact
    if "geschiedenis" in interest_set or "architectuur" in interest_set:
        return fact
    return fact


def _nature_hint(fact: str, place: str) -> str:
    blob = f"{fact} {place}".lower()
    flora = ("bos", "heide", "natuur", "park", "reservaat", "boomgaard", "vallei", "rivier", "nete", "leie", "duin", "polder", "weide")
    if any(word in blob for word in flora):
        if fact:
            return fact
        return f"Rond {place} wisselen bossen, weilanden en sloten — typisch Pajottenland en Vlaams groen."
    if fact:
        return fact
    return f"Langs {place} zie je veel groen: weiden, struiken en bomen langs de fietspaden."


def _nearest_index(geometry: list[list[float]], lat: float, lng: float) -> int:
    best_i = 0
    best_d = 10**12
    for index, point in enumerate(geometry[::3]):
        dist = haversine_m(lat, lng, point[0], point[1])
        if dist < best_d:
            best_d = dist
            best_i = index * 3
    return min(best_i, len(geometry) - 1)


def _side_of(a_lat: float, a_lng: float, b_lat: float, b_lng: float, lat: float, lng: float) -> str:
    scale = 111_000 * max(0.2, abs(math.cos(math.radians(a_lat))))
    ax = (b_lng - a_lng) * scale
    ay = (b_lat - a_lat) * 111_000
    bx = (lng - a_lng) * scale
    by = (lat - a_lat) * 111_000
    cross = ax * by - ay * bx
    if abs(cross) < 80:
        return "langs de route"
    return "rechts" if cross < 0 else "links"
