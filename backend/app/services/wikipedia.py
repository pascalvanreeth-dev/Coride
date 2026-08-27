from __future__ import annotations

import asyncio
import re
from typing import Any
from urllib.parse import quote, unquote

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


def parse_wikipedia_url(url: str | None) -> tuple[str, str] | None:
    if not url:
        return None
    match = re.search(r"https?://([a-z]{2,3})\.wikipedia\.org/wiki/([^#?]+)", url, re.I)
    if not match:
        return None
    return match.group(1).lower(), unquote(match.group(2).replace("_", " "))


async def lookup_stop_summary(
    name: str,
    lat: float,
    lng: float,
    *,
    wikipedia_url: str | None = None,
    wikipedia: str | None = None,
    wikidata: str | None = None,
    description: str | None = None,
    kind: str | None = None,
) -> dict[str, str]:
    """Look up 1–2 informative sentences about a route stop."""
    from app.services import pois as pois_service

    clean_name = name.strip()
    kind_label = (kind or "plek").strip()

    async def from_wiki_poi(poi: dict[str, Any]) -> dict[str, str] | None:
        data = await summary_for_poi(poi)
        blurb = stop_blurb(data.get("summary") or "", name=clean_name, kind=kind_label)
        if blurb:
            return {**data, "summary": blurb}
        return None

    if description and not is_generic_blurb(description, clean_name, kind_label):
        return {"summary": stop_blurb(description, name=clean_name, kind=kind_label), "url": wikipedia_url or "", "image": ""}

    if wikipedia or wikidata:
        hit = await from_wiki_poi(
            {"name": clean_name, "wikipedia": wikipedia, "wikidata": wikidata, "description": description or ""}
        )
        if hit:
            return hit

    parsed_url = parse_wikipedia_url(wikipedia_url)
    if parsed_url:
        data = await page_summary(*parsed_url)
        blurb = stop_blurb(data.get("summary") or "", name=clean_name, kind=kind_label) if data else ""
        if blurb:
            return {**(data or {}), "summary": blurb}

    osm = await pois_service.fetch_poi_at(lat, lng, name=clean_name)
    if osm:
        if osm.get("description") and not is_generic_blurb(osm["description"], clean_name, kind_label):
            wiki = await from_wiki_poi(osm)
            url = (wiki or {}).get("url") or wikipedia_url or ""
            desc = stop_blurb(osm["description"], name=clean_name, kind=kind_label)
            if wiki and wiki.get("summary"):
                return {"summary": wiki["summary"], "url": wiki.get("url") or url, "image": wiki.get("image") or ""}
            return {"summary": desc, "url": url, "image": ""}
        hit = await from_wiki_poi(osm)
        if hit:
            return hit

    lower_name = clean_name.lower()
    for radius in (900, 2200, 4500):
        nearby = await nearby_places(lat, lng, radius)
        exact = next((poi for poi in nearby if poi["name"].strip().lower() == lower_name), None)
        if exact:
            hit = await from_wiki_poi(exact)
            if hit:
                return hit
        fuzzy = next(
            (
                poi
                for poi in nearby
                if lower_name in poi["name"].strip().lower() or poi["name"].strip().lower() in lower_name
            ),
            None,
        )
        if fuzzy:
            hit = await from_wiki_poi(fuzzy)
            if hit:
                return hit

    for lang in LANGS:
        title = await search_wikipedia_title(clean_name, lang)
        if title:
            data = await page_summary(lang, title.replace(" ", "_"))
            blurb = stop_blurb(data.get("summary") or "", name=clean_name, kind=kind_label) if data else ""
            if blurb:
                return {**(data or {}), "summary": blurb}

    for lang in LANGS:
        data = await page_summary(lang, clean_name.replace(" ", "_"))
        blurb = stop_blurb(data.get("summary") or "", name=clean_name, kind=kind_label) if data else ""
        if blurb:
            return {**(data or {}), "summary": blurb}

    return {"summary": "", "url": wikipedia_url or "", "image": ""}


async def search_wikipedia_title(name: str, lang: str) -> str | None:
    async with client() as http:
        response = await http.get(
            f"https://{lang}.wikipedia.org/w/api.php",
            params={"action": "opensearch", "search": name, "limit": 1, "namespace": 0, "format": "json"},
            headers=WIKI_HEADERS,
        )
        if response.status_code != 200:
            return None
        payload = response.json()
        titles = payload[1] if isinstance(payload, list) and len(payload) > 1 else []
        return titles[0] if titles else None


def is_generic_blurb(text: str, name: str, kind: str = "plek") -> bool:
    cleaned = (text or "").strip()
    if not cleaned:
        return True
    lowered = cleaned.lower()
    if "bekend via openstreetmap" in lowered:
        return True
    if "past bij je interesse" in lowered:
        return True
    if "langs je fietsroute" in lowered or "langs je route" in lowered:
        return True
    if name and kind:
        pattern = re.compile(
            rf"^{re.escape(name.strip())} is een {re.escape(kind.strip())} langs je (fiets)?route\.?$",
            re.I,
        )
        if pattern.match(cleaned):
            return True
    if len(cleaned) < 28 and name.lower() in lowered and kind.lower() in lowered:
        return True
    return False


def stop_blurb(text: str, *, name: str = "", kind: str = "plek", sentences: int = 2) -> str:
    cleaned = (text or "").strip()
    if not cleaned or is_generic_blurb(cleaned, name, kind):
        return ""
    return first_sentences(cleaned, sentences)


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
