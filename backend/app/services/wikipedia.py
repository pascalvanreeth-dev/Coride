from __future__ import annotations

import asyncio
import re
from typing import Any
from urllib.parse import quote

from app.config import settings
from app.http import client

LANGS = [part.strip() for part in settings.wikipedia_langs.split(",") if part.strip()]
WIKI_HEADERS = {"User-Agent": settings.wikipedia_user_agent, "Api-User-Agent": settings.wikipedia_user_agent}


def parse_wikipedia_tag(tag: str | None) -> tuple[str, str] | None:
    if not tag:
        return None
    if ":" in tag:
        lang, title = tag.split(":", 1)
        return lang.strip(), title.strip().replace(" ", "_")
    return "nl", tag.replace(" ", "_")


async def summary_for_poi(poi: dict[str, Any]) -> dict[str, str]:
    parsed = parse_wikipedia_tag(poi.get("wikipedia"))
    if parsed:
        data = await page_summary(*parsed)
        if data:
            return data

    if poi.get("wikidata"):
        data = await summary_from_wikidata(poi["wikidata"])
        if data:
            return data

    fallback = poi.get("description") or (
        f"{poi['name']} is een {poi.get('kind_label', 'plek')} in België, "
        "bekend via OpenStreetMap."
    )
    return {"summary": fallback, "url": "", "image": ""}


async def page_summary(lang: str, title: str) -> dict[str, str] | None:
    async with client() as http:
        url = f"https://{lang}.wikipedia.org/api/rest_v1/page/summary/{quote(title)}"
        response = await http.get(url, headers={**WIKI_HEADERS, "Accept": "application/json"})
        if response.status_code != 200:
            return None
        payload = response.json()
        extract = (payload.get("extract") or "").strip()
        if not extract:
            return None
        thumbnail = (payload.get("thumbnail") or {}).get("source") or ""
        return {
            "summary": extract,
            "url": payload.get("content_urls", {}).get("desktop", {}).get("page") or "",
            "image": thumbnail,
        }


async def summary_from_wikidata(qid: str) -> dict[str, str] | None:
    qid = qid.strip()
    if not re.match(r"^Q\d+$", qid):
        return None
    async with client() as http:
        response = await http.get(
            "https://www.wikidata.org/wiki/Special:EntityData/" + qid + ".json",
            headers=WIKI_HEADERS,
        )
        if response.status_code != 200:
            return None
        entity = response.json().get("entities", {}).get(qid, {})
        sitelinks = entity.get("sitelinks") or {}
        for lang in LANGS:
            wiki = sitelinks.get(f"{lang}wiki")
            if wiki and wiki.get("title"):
                data = await page_summary(lang, wiki["title"])
                if data:
                    return data
        descriptions = entity.get("descriptions") or {}
        for lang in LANGS:
            if lang in descriptions:
                return {"summary": descriptions[lang]["value"], "url": "", "image": ""}
    return None


async def nearby_places(
    lat: float, lng: float, radius_m: int = 8000, langs: list[str] | None = None
) -> list[dict[str, Any]]:
    radius_m = max(300, min(int(radius_m), 10000))
    places: list[dict[str, Any]] = []
    seen: set[str] = set()
    use_langs = langs or LANGS[:1]
    async with client() as http:
        for lang in use_langs:
            response = await http.get(
                f"https://{lang}.wikipedia.org/w/api.php",
                params={
                    "action": "query",
                    "list": "geosearch",
                    "gscoord": f"{lat}|{lng}",
                    "gsradius": radius_m,
                    "gslimit": 20,
                    "format": "json",
                },
                headers=WIKI_HEADERS,
            )
            if response.status_code != 200:
                continue
            for hit in response.json().get("query", {}).get("geosearch") or []:
                title = (hit.get("title") or "").strip()
                if not title or title in seen:
                    continue
                seen.add(title)
                places.append(
                    {
                        "id": f"wiki-{lang}-{hit.get('pageid', title)}",
                        "name": title,
                        "lat": float(hit["lat"]),
                        "lng": float(hit["lon"]),
                        "kind": "erfgoed",
                        "kind_label": "plek met een verhaal",
                        "interest": "geschiedenis",
                        "source": "Wikipedia",
                        "wikipedia": f"{lang}:{title}",
                        "wikidata": None,
                        "description": "",
                        "heritage": "",
                    }
                )
    return places


def _usable(title: str) -> bool:
    lowered = title.lower()
    if lowered.startswith("lijst ") or lowered.startswith("categorie:"):
        return False
    if re.match(r"^[abn]\d+", lowered):
        return False
    blocked = (
        "haven van",
        "snelweg",
        "gewestweg",
        "afrit",
        "knooppunt",
        "arena",
        "stadion",
        "parking",
        "industriepark",
    )
    return not any(word in lowered for word in blocked)


async def places_for_route(
    lat: float,
    lng: float,
    radius_m: int,
    end: tuple[float, float] | None = None,
) -> list[dict[str, Any]]:
    from app.services.geo import offset_point, unique_key

    points = [(lat, lng)]
    if end:
        points.append(end)
        points.append(((lat + end[0]) / 2, (lng + end[1]) / 2))
    else:
        ring = min(max(radius_m * 0.38, 1600), 3000)
        for deg in (20, 90, 160, 230, 300):
            points.append(offset_point(lat, lng, ring, deg))

    batches = await asyncio.gather(*(_nearby_at(plat, plng, min(radius_m, 4000)) for plat, plng in points))
    seen: set[str] = set()
    merged: list[dict[str, Any]] = []
    for batch in batches:
        for poi in batch:
            if not _usable(poi["name"]):
                continue
            key = unique_key(poi["name"], poi["lat"], poi["lng"])
            if key in seen:
                continue
            seen.add(key)
            merged.append(poi)
    return merged


async def _nearby_at(lat: float, lng: float, radius_m: int) -> list[dict[str, Any]]:
    try:
        return await nearby_places(lat, lng, radius_m)
    except Exception:
        return []


def first_sentences(text: str, count: int = 2) -> str:
    parts = re.split(r"(?<=[.!?])\s+", text.strip())
    return " ".join(parts[:count]).strip()
