from __future__ import annotations

import asyncio
import re
from typing import Any

import httpx

from app.config import settings
from app.http import client
from app.services.geo import haversine_m, osm_center, unique_key


OVERPASS_FILTERS: dict[str, list[str]] = {
    "geschiedenis": [
        '["historic"]',
        '["tourism"="museum"]',
        '["tourism"="artwork"]',
        '["amenity"="place_of_worship"]',
    ],
    "natuur": [
        '["leisure"~"park|nature_reserve|garden"]',
        '["tourism"="viewpoint"]',
        '["boundary"="national_park"]',
        '["natural"="wood"]["name"]',
    ],
    "landbouw": [
        '["tourism"="farm"]',
        '["shop"="farm"]',
        '["craft"~"winery|cider"]',
        '["landuse"="vineyard"]["name"]',
        '["amenity"="marketplace"]',
    ],
    "horeca": [
        '["amenity"~"cafe|pub|bar|restaurant|ice_cream|biergarten|fast_food"]',
        '["craft"="brewery"]',
        '["shop"="bakery"]',
    ],
    "oorlog": [
        '["historic"~"memorial|fort|bunker|battlefield|ruins"]',
        '["memorial"]',
        '["landuse"="military"]["name"]',
    ],
    "architectuur": [
        '["man_made"="windmill"]',
        '["historic"~"manor|castle|church|tower"]',
        '["building"~"cathedral|church|chapel"]["name"]',
    ],
    "activiteiten": [
        '["leisure"~"park|nature_reserve|garden|sports_centre|playground|miniature_golf|water_park"]',
        '["tourism"~"attraction|viewpoint|theme_park|zoo|aquarium"]',
        '["amenity"~"swimming_pool|bowling_alley|ice_rink"]',
    ],
    "evenementen": [
        '["amenity"~"theatre|arts_centre|marketplace|community_centre|events_venue|cinema"]',
        '["leisure"="stadium"]',
    ],
}


KIND_LABELS = {
    "castle": "kasteel",
    "monument": "monument",
    "memorial": "gedenkteken",
    "ruins": "ruïne",
    "archaeological_site": "archeologische site",
    "museum": "museum",
    "artwork": "kunstwerk",
    "place_of_worship": "kerk of gebedshuis",
    "cathedral": "kathedraal",
    "church": "kerk",
    "park": "park",
    "nature_reserve": "natuurgebied",
    "garden": "tuin",
    "attraction": "attractie",
    "viewpoint": "uitkijkpunt",
    "theatre": "theater",
    "arts_centre": "kunstencentrum",
    "marketplace": "markt",
    "events_venue": "evenementenlocatie",
    "concert_hall": "concertzaal",
    "stadium": "stadion",
    "cafe": "café",
    "pub": "café/pub",
    "bar": "bar",
    "restaurant": "restaurant",
    "ice_cream": "ijssalon",
    "biergarten": "biertuin",
    "fast_food": "snack",
    "bakery": "bakker",
    "brewery": "brouwerij",
    "farm": "hoeve",
    "vineyard": "wijngaard",
    "winery": "wijnhuis",
    "windmill": "molen",
    "manor": "herenhuis",
    "fort": "fort",
    "bunker": "bunker",
    "battlefield": "slagveld",
    "national_park": "natuurpark",
    "wood": "bos",
}


def classify(tags: dict[str, str], interests: list[str]) -> tuple[str, str]:
    historic = tags.get("historic", "")
    tourism = tags.get("tourism", "")
    amenity = tags.get("amenity", "")
    leisure = tags.get("leisure", "")
    building = tags.get("building", "")
    craft = tags.get("craft", "")
    shop = tags.get("shop", "")
    man_made = tags.get("man_made", "")

    if amenity in {"cafe", "pub", "bar", "restaurant", "ice_cream", "biergarten", "fast_food"} or craft == "brewery" or shop == "bakery":
        kind = amenity or craft or shop
        return ("horeca" if "horeca" in interests else "activiteiten"), kind
    if "oorlog" in interests and (
        historic in {"memorial", "fort", "bunker", "battlefield"}
        or tags.get("memorial")
        or tags.get("military")
        or tags.get("landuse") == "military"
    ):
        return "oorlog", historic or "gedenkteken"
    if "architectuur" in interests and (
        man_made == "windmill" or historic in {"manor", "castle", "church", "tower"} or building in {"cathedral", "church", "chapel"}
    ):
        return "architectuur", man_made or historic or building
    if "geschiedenis" in interests and (
        historic or tags.get("heritage") or tourism in {"museum", "artwork"} or amenity == "place_of_worship"
    ):
        kind = historic or tourism or amenity or building or "erfgoed"
        return "geschiedenis", kind
    if "natuur" in interests and (
        leisure in {"park", "nature_reserve", "garden"} or tourism == "viewpoint" or tags.get("natural") == "wood"
    ):
        return "natuur", leisure or tourism or tags.get("natural") or "natuur"
    if "landbouw" in interests and (tourism == "farm" or shop == "farm" or craft == "winery" or tags.get("landuse") == "vineyard"):
        return "landbouw", tourism or shop or craft or "hoeve"
    if "evenementen" in interests and (
        amenity in {"theatre", "arts_centre", "marketplace", "community_centre", "events_venue", "cinema", "concert_hall"}
        or leisure in {"stadium", "bandstand"}
    ):
        kind = amenity or leisure or "evenementenlocatie"
        return "evenementen", kind
    if "activiteiten" in interests:
        kind = leisure or tourism or "activiteit"
        return "activiteiten", kind
    kind = historic or tourism or amenity or leisure or "plek"
    interest = interests[0] if interests else "geschiedenis"
    return interest, kind


def kind_label(kind: str) -> str:
    return KIND_LABELS.get(kind, kind.replace("_", " "))


async def fetch_pois(
    lat: float,
    lng: float,
    radius_m: int,
    interests: list[str],
    extra_point: tuple[float, float] | None = None,
) -> list[dict[str, Any]]:
    """Fetch named POIs for every selected interest (parallel per theme)."""
    radius_m = max(2500, min(radius_m, 22000))
    wanted = _unique_interests(interests)
    batches = await asyncio.gather(
        *[_fetch_pois_for_interest(lat, lng, radius_m, interest, extra_point) for interest in wanted],
        return_exceptions=True,
    )
    merged: dict[str, dict[str, Any]] = {}
    for batch in batches:
        if isinstance(batch, Exception):
            continue
        for poi in batch:
            merged[str(poi["id"])] = poi
    return list(merged.values())


def _unique_interests(interests: list[str] | None) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in interests or ["geschiedenis"]:
        if item and item not in seen:
            seen.add(item)
            result.append(item)
    return result


NOTE_INTEREST_KEYS: list[tuple[str, tuple[str, ...]]] = [
    (
        "horeca",
        (
            "cafe", "café", "cafetje", "cafetjes", "koffie", "koffi", "coffee", "pub", "bar", "bier",
            "terras", "restaurant", "eten", "lunch", "eetcafe", "eetcafé", "ijs", "drank",
            "bakker", "brouwerij", "taart", "brasserie", "frituur", "snack",
        ),
    ),
    (
        "architectuur",
        (
            "kasteel", "kastelen", "burcht", "castle", "molen", "molens", "kerk", "kerken",
            "kathedraal", "abdij", "abdijen", "toren", "torens", "basiliek", "kapel",
        ),
    ),
    (
        "natuur",
        (
            "park", "parken", "bos", "bossen", "natuur", "water", "rivier", "leie", "schelde",
            "kanaal", "gracht", "duin", "polder", "meer", "vijver", "reservaat", "wandeling",
        ),
    ),
    (
        "geschiedenis",
        ("museum", "musea", "geschiedenis", "erfgoed", "historisch", "middeleeuw", "monument", "monumenten"),
    ),
    ("oorlog", ("oorlog", "memorial", "gedenkteken", "fort", "slagveld", "bunker", "bevrijding")),
    ("landbouw", ("hoeve", "hoeven", "boerderij", "wijn", "wijngaard", "fruit", "hop", "streekproduct", "boer")),
    ("activiteiten", ("uitzicht", "uitkijk", "attractie", "zwem", "speeltuin", "wandel", "recreatie")),
    ("evenementen", ("markt", "festival", "evenement", "theater", "concert", "optreden")),
]


def wish_interests_for_notes(notes: str, fallback: list[str] | None = None) -> list[str]:
    """Map vrije extra-wens-tekst naar zoekbare interesses."""
    found = interests_from_notes(notes)
    if found:
        return _unique_interests(found)[:4]
    if not (notes or "").strip():
        return []
    if fallback:
        return _unique_interests(fallback)[:4]
    return _unique_interests(["geschiedenis", "natuur", "architectuur", "horeca"])[:3]


def interests_from_notes(notes: str) -> list[str]:
    text = (notes or "").lower()
    if not text.strip():
        return []
    found: list[str] = []
    for interest, keys in NOTE_INTEREST_KEYS:
        if any(key in text for key in keys):
            found.append(interest)
    return found


def matches_notes(poi: dict[str, Any], notes: str) -> bool:
    text = (notes or "").strip().lower()
    if not text:
        return False
    blob = f"{poi.get('name', '')} {poi.get('kind', '')} {poi.get('kind_label', '')} {poi.get('interest', '')} {poi.get('description', '')}".lower()
    from app.services import knooppunten as knoop_service

    if any(needle in blob for needle in knoop_service._note_needles(notes)):
        return True
    wish_interests = set(interests_from_notes(notes))
    if wish_interests and poi.get("interest") in wish_interests:
        return True
    for word in re.findall(r"[a-zà-ÿ]{4,}", text):
        if word in blob:
            return True
    return False


async def _fetch_pois_for_interest(
    lat: float,
    lng: float,
    radius_m: int,
    interest: str,
    extra_point: tuple[float, float] | None = None,
) -> list[dict[str, Any]]:
    points = [(lat, lng)]
    if extra_point:
        points.append(extra_point)
    filters = OVERPASS_FILTERS.get(interest, [])
    if not filters:
        return []
    clauses: list[str] = []
    for plat, plng in points:
        around = f"around:{radius_m},{plat:.5f},{plng:.5f}"
        for filt in filters[:3]:
            clauses.append(f'nwr{filt}["name"]({around});')
    if not clauses:
        return []
    query = f"[out:json][timeout:25];({''.join(clauses)});out center 80;"
    try:
        data = await _overpass(query)
    except Exception:
        return []
    seen: set[str] = set()
    pois: list[dict[str, Any]] = []
    for element in data.get("elements", []):
        tags = element.get("tags") or {}
        name = (tags.get("name:nl") or tags.get("name") or "").strip()
        if not name:
            continue
        center = osm_center(element)
        if not center:
            continue
        plat, plng = center
        key = unique_key(name, plat, plng)
        if key in seen:
            continue
        seen.add(key)
        _, kind = classify(tags, [interest])
        pois.append(
            {
                "id": f"{interest}-{element.get('id')}",
                "name": name,
                "lat": plat,
                "lng": plng,
                "kind": kind,
                "kind_label": kind_label(kind),
                "interest": interest,
                "source": "OpenStreetMap",
                "wikipedia": tags.get("wikipedia"),
                "wikidata": tags.get("wikidata"),
                "description": tags.get("description:nl") or tags.get("description") or "",
                "heritage": tags.get("heritage") or tags.get("heritage:operator") or "",
            }
        )
    return pois


def build_stop_pool(
    ranked: list[dict[str, Any]],
    interests: list[str],
    *,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Ranked POIs with at least one candidate per selected interest."""
    themes = _unique_interests(interests)
    cap = limit or min(24, max(16, len(themes) * 2))
    pool: list[dict[str, Any]] = []
    used_ids: set[str] = set()

    def add(poi: dict[str, Any]) -> None:
        pid = str(poi.get("id") or "")
        if not pid or pid in used_ids:
            return
        pool.append(poi)
        used_ids.add(pid)

    for interest in themes:
        for poi in ranked:
            if poi.get("interest") == interest:
                add(poi)
                break

    for poi in ranked:
        if len(pool) >= cap:
            break
        add(poi)

    return pool


def pick_diverse_pois(
    ranked: list[dict[str, Any]],
    interests: list[str],
    *,
    wanted: int = 12,
    min_distance_m: float = 800,
) -> list[dict[str, Any]]:
    """Spread picks along the route and cover each selected interest when possible."""
    themes = _unique_interests(interests)
    selected: list[dict[str, Any]] = []
    used_ids: set[str] = set()

    def far_enough(poi: dict[str, Any]) -> bool:
        return not any(
            haversine_m(poi["lat"], poi["lng"], other["lat"], other["lng"]) < min_distance_m for other in selected
        )

    def try_add(poi: dict[str, Any]) -> bool:
        pid = str(poi.get("id") or "")
        if pid in used_ids:
            return False
        if not far_enough(poi):
            return False
        selected.append(poi)
        used_ids.add(pid)
        return True

    for interest in themes:
        for poi in ranked:
            if poi.get("interest") != interest:
                continue
            pid = str(poi.get("id") or "")
            if pid in used_ids:
                continue
            selected.append(poi)
            used_ids.add(pid)
            break

    for poi in ranked:
        if len(selected) >= wanted:
            break
        try_add(poi)

    return selected or ranked[:wanted]


def _sample_route_points(points: list[tuple[float, float]], max_points: int = 5) -> list[tuple[float, float]]:
    cleaned = [(float(lat), float(lng)) for lat, lng in points if lat is not None and lng is not None]
    if len(cleaned) <= max_points:
        return cleaned
    step = max(1, (len(cleaned) - 1) // (max_points - 1))
    sampled = [cleaned[0]]
    for index in range(step, len(cleaned) - 1, step):
        sampled.append(cleaned[index])
        if len(sampled) >= max_points - 1:
            break
    if cleaned[-1] != sampled[-1]:
        sampled.append(cleaned[-1])
    return sampled[:max_points]


async def fetch_pois_along_points(
    points: list[tuple[float, float]],
    radius_m: int,
    interests: list[str],
    *,
    max_points: int = 4,
) -> list[dict[str, Any]]:
    sampled = _sample_route_points(points, max_points)
    if not sampled:
        return []
    try:
        return await asyncio.wait_for(
            _fetch_pois_along_points_impl(sampled, radius_m, interests),
            timeout=40.0,
        )
    except TimeoutError as exc:
        raise RuntimeError("Overpass API reageerde niet: timeout") from exc


async def _fetch_pois_along_points_impl(
    sampled: list[tuple[float, float]],
    radius_m: int,
    interests: list[str],
) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    results = await asyncio.gather(
        *[fetch_pois(plat, plng, radius_m, interests) for plat, plng in sampled],
        return_exceptions=True,
    )
    last_error: Exception | None = None
    for result in results:
        if isinstance(result, Exception):
            last_error = result
            continue
        for poi in result:
            merged[str(poi["id"])] = poi
    if merged:
        return list(merged.values())
    try:
        return await fetch_pois(
            sampled[0][0],
            sampled[0][1],
            min(int(radius_m * 2), 16000),
            interests,
        )
    except Exception:
        if last_error:
            raise last_error
        return []


HORECA_FILTER = '["amenity"~"cafe|pub|bar|restaurant|ice_cream|biergarten"]'
HORECA_PREF_FILTERS: dict[str, list[str]] = {
    "snack": ['["amenity"~"fast_food|ice_cream|kiosk|cafe"]'],
    "tafelen": ['["amenity"="restaurant"]'],
    "koffie": ['["amenity"="cafe"]', '["shop"="bakery"]'],
    "brouwerijen": ['["amenity"~"pub|bar|biergarten"]', '["craft"="brewery"]'],
}


def notes_want_horeca(notes: str, interests: list[str] | None = None, prefs: list[str] | None = None) -> bool:
    if prefs:
        return True
    if interests and "horeca" in interests:
        return True
    text = (notes or "").lower()
    keys = (
        "cafe", "café", "cafetje", "cafetjes", "koffie", "koffi", "coffee", "pub", "bar", "bier",
        "terras", "restaurant", "eten", "lunch", "eetcafe", "eetcafé", "ijs",
    )
    return any(key in text for key in keys)


def _horeca_query(
    points: list[tuple[float, float]],
    radius_m: int,
    filters: list[str],
    *,
    require_name: bool = True,
) -> str:
    name_filter = '["name"]' if require_name else ""
    clauses: list[str] = []
    for plat, plng in points:
        around = f"around:{radius_m},{plat:.5f},{plng:.5f}"
        for filt in filters:
            clauses.append(f"nwr{filt}{name_filter}({around});")
    limit = 100 if require_name else 150
    return f"[out:json][timeout:35];({''.join(clauses)});out center {limit};"


def _parse_horeca_elements(elements: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    pois: list[dict[str, Any]] = []
    for element in elements or []:
        tags = element.get("tags") or {}
        name = (tags.get("name:nl") or tags.get("name") or "").strip()
        if not name:
            continue
        center = osm_center(element)
        if not center:
            continue
        key = unique_key(name, center[0], center[1])
        if key in seen:
            continue
        seen.add(key)
        kind = tags.get("amenity") or tags.get("craft") or tags.get("shop") or "cafe"
        pois.append(
            {
                "id": f"horeca-{element.get('id')}",
                "name": name,
                "lat": center[0],
                "lng": center[1],
                "kind": kind,
                "kind_label": kind_label(kind),
                "interest": "horeca",
                "source": "OpenStreetMap",
                "wikipedia": tags.get("wikipedia"),
                "wikidata": tags.get("wikidata"),
                "description": tags.get("description:nl") or tags.get("description") or "",
                "heritage": "",
            }
        )
    return pois


async def fetch_horeca(
    lat: float,
    lng: float,
    radius_m: int,
    extra_point: tuple[float, float] | None = None,
    prefs: list[str] | None = None,
) -> list[dict[str, Any]]:
    radius_m = max(2500, min(radius_m, 16000))
    points = [(lat, lng)]
    if extra_point:
        points.append(extra_point)
    filters = []
    for pref in prefs or []:
        filters.extend(HORECA_PREF_FILTERS.get(pref, []))
    if not filters:
        filters = [HORECA_FILTER]
    try:
        data = await _overpass(_horeca_query(points, radius_m, filters, require_name=True))
        if not data.get("elements"):
            data = await _overpass(_horeca_query(points, min(radius_m + 1500, 16000), filters, require_name=False))
    except Exception:
        return []
    return _parse_horeca_elements(data.get("elements") or [])


async def _overpass_fast(query: str, *, timeout_s: float = 18.0) -> dict[str, Any]:
    """Snelle Overpass: alleen eerste werkende mirror, raw POST."""
    errors: list[str] = []
    mirrors = [url.strip() for url in settings.overpass_urls.split(",") if url.strip()]
    for url in mirrors[:2]:
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(timeout_s, connect=4.0),
                headers={"User-Agent": settings.user_agent},
                follow_redirects=True,
            ) as http:
                response = await http.post(
                    url,
                    content=query.encode("utf-8"),
                    headers={
                        "User-Agent": settings.user_agent,
                        "Content-Type": "text/plain; charset=utf-8",
                    },
                )
                if response.status_code >= 400:
                    errors.append(f"{url} -> HTTP {response.status_code}")
                    continue
                payload = response.json()
                remark = str(payload.get("remark") or "")
                if "error" in remark.lower() or "runtime error" in remark.lower():
                    errors.append(f"{url} -> {remark[:120]}")
                    continue
                return payload
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{url} -> {type(exc).__name__}")
            continue
    raise RuntimeError("Overpass (fast) faalde: " + "; ".join(errors))


async def fetch_horeca_along_points(
    points: list[tuple[float, float]],
    radius_m: int,
    prefs: list[str] | None = None,
    *,
    chunk_size: int = 4,
    max_points: int = 8,
) -> list[dict[str, Any]]:
    """Zoek cafés/horeca langs een route — Overpass + Nominatim parallel (niet minuten wachten)."""
    radius_m = max(3500, min(int(radius_m), 14000))
    cleaned: list[tuple[float, float]] = []
    seen_pt: set[tuple[float, float]] = set()
    for lat, lng in points or []:
        if lat is None or lng is None:
            continue
        key = (round(float(lat), 3), round(float(lng), 3))
        if key in seen_pt:
            continue
        seen_pt.add(key)
        cleaned.append((float(lat), float(lng)))
    if not cleaned:
        return []
    if len(cleaned) > max_points:
        step = max(1, (len(cleaned) - 1) // (max_points - 1))
        spaced = [cleaned[0]]
        for index in range(step, len(cleaned) - 1, step):
            spaced.append(cleaned[index])
            if len(spaced) >= max_points - 1:
                break
        if cleaned[-1] != spaced[-1]:
            spaced.append(cleaned[-1])
        cleaned = spaced[:max_points]

    filters: list[str] = []
    for pref in prefs or []:
        filters.extend(HORECA_PREF_FILTERS.get(pref, []))
    if not filters:
        filters = [HORECA_FILTER]

    async def _overpass_path() -> list[dict[str, Any]]:
        found: dict[str, dict[str, Any]] = {}
        # Max 2 chunks — daarna stoppen i.p.v. alle mirrors uitputten.
        size = max(2, min(chunk_size, 4))
        for index in range(0, min(len(cleaned), size * 2), size):
            chunk = cleaned[index : index + size]
            try:
                data = await _overpass_fast(
                    _horeca_query(chunk, radius_m, filters, require_name=True),
                    timeout_s=16.0,
                )
            except Exception:
                continue
            for poi in _parse_horeca_elements(data.get("elements") or []):
                found[str(poi["id"])] = poi
            if len(found) >= 12:
                break
        return list(found.values())

    async def _fallback_path() -> list[dict[str, Any]]:
        nomi = await fetch_horeca_nominatim_along_points(cleaned, max_points=min(6, len(cleaned)), per_point=8)
        if len(nomi) >= 6:
            return nomi
        photon = await fetch_horeca_photon_along_points(cleaned, max_points=min(6, len(cleaned)))
        merged: dict[str, dict[str, Any]] = {str(p["id"]): p for p in nomi}
        for poi in photon:
            merged[str(poi["id"])] = poi
        return list(merged.values())

    overpass_task = asyncio.create_task(_overpass_path())
    fallback_task = asyncio.create_task(_fallback_path())
    merged: dict[str, dict[str, Any]] = {}

    done, pending = await asyncio.wait(
        {overpass_task, fallback_task},
        timeout=35.0,
        return_when=asyncio.FIRST_COMPLETED,
    )
    for task in done:
        try:
            for poi in task.result() or []:
                merged[str(poi["id"])] = poi
        except Exception:
            continue

    # Als we al genoeg hebben: rest annuleren.
    if len(merged) >= 8:
        for task in pending:
            task.cancel()
        return list(merged.values())

    if pending:
        done2, still = await asyncio.wait(pending, timeout=25.0)
        for task in still:
            task.cancel()
        for task in done2:
            try:
                for poi in task.result() or []:
                    merged[str(poi["id"])] = poi
            except Exception:
                continue

    return list(merged.values())


async def fetch_horeca_photon_along_points(
    points: list[tuple[float, float]],
    *,
    max_points: int = 10,
    per_point: int = 12,
) -> list[dict[str, Any]]:
    """Horeca via Photon (Komoot) als Overpass niet meewerkt."""
    cleaned: list[tuple[float, float]] = []
    seen_pt: set[tuple[float, float]] = set()
    for lat, lng in points or []:
        if lat is None or lng is None:
            continue
        key = (round(float(lat), 3), round(float(lng), 3))
        if key in seen_pt:
            continue
        seen_pt.add(key)
        cleaned.append((float(lat), float(lng)))
    if not cleaned:
        return []
    if len(cleaned) > max_points:
        step = max(1, (len(cleaned) - 1) // (max_points - 1))
        spaced = [cleaned[i] for i in range(0, len(cleaned), step)][: max_points - 1]
        if cleaned[-1] not in spaced:
            spaced.append(cleaned[-1])
        cleaned = spaced[:max_points]

    tags = ("amenity:cafe", "amenity:pub", "amenity:bar", "amenity:restaurant", "amenity:biergarten")
    queries = ("café", "cafe", "restaurant", "pub")
    merged: dict[str, dict[str, Any]] = {}

    async def _one(lat: float, lng: float, query: str, osm_tag: str) -> list[dict[str, Any]]:
        try:
            async with client() as http:
                response = await http.get(
                    "https://photon.komoot.io/api/",
                    params={
                        "q": query,
                        "lat": lat,
                        "lon": lng,
                        "limit": per_point,
                        "lang": "nl",
                        "osm_tag": osm_tag,
                        "bbox": "2.3,49.45,6.45,51.55",
                    },
                    timeout=httpx.Timeout(12.0, connect=5.0),
                )
                if response.status_code >= 400:
                    return []
                payload = response.json()
        except Exception:
            return []
        rows: list[dict[str, Any]] = []
        for feature in payload.get("features") or []:
            geometry = feature.get("geometry") or {}
            coords = geometry.get("coordinates") or []
            props = feature.get("properties") or {}
            if len(coords) < 2:
                continue
            name = str(props.get("name") or "").strip()
            if not name:
                continue
            plat, plng = float(coords[1]), float(coords[0])
            osm_value = str(props.get("osm_value") or props.get("type") or "cafe")
            osm_id = props.get("osm_id") or unique_key(name, plat, plng)
            rows.append(
                {
                    "id": f"horeca-photon-{osm_id}",
                    "name": name,
                    "lat": plat,
                    "lng": plng,
                    "kind": osm_value,
                    "kind_label": kind_label(osm_value),
                    "interest": "horeca",
                    "source": "Photon",
                    "wikipedia": None,
                    "wikidata": props.get("wikidata"),
                    "description": "",
                    "heritage": "",
                }
            )
        return rows

    tasks = []
    for lat, lng in cleaned:
        for query, tag in zip(queries, tags, strict=False):
            tasks.append(_one(lat, lng, query, tag))
    results = await asyncio.gather(*tasks, return_exceptions=True)
    for result in results:
        if isinstance(result, Exception):
            continue
        for poi in result:
            # Houd Photon-hits redelijk dicht bij een samplepunt.
            if any(haversine_m(poi["lat"], poi["lng"], lat, lng) <= 9000 for lat, lng in cleaned):
                merged[str(poi["id"])] = poi
    return list(merged.values())


async def fetch_poi_at(lat: float, lng: float, *, name: str | None = None, radius_m: int = 70) -> dict[str, Any] | None:
    """Nearest named OSM feature at a stop coordinate."""
    query = (
        f"[out:json][timeout:12];"
        f'(nwr(around:{max(25, radius_m)},{lat:.6f},{lng:.6f})["name"];);'
        f"out tags center 40;"
    )
    try:
        data = await _overpass(query)
    except Exception:
        return None
    best: dict[str, Any] | None = None
    best_score = float("inf")
    target = (name or "").strip().lower()
    for element in data.get("elements") or []:
        tags = element.get("tags") or {}
        label = (tags.get("name:nl") or tags.get("name") or "").strip()
        if not label:
            continue
        center = osm_center(element)
        if not center:
            continue
        plat, plng = center
        dist = haversine_m(lat, lng, plat, plng)
        score = dist
        if target:
            lower = label.lower()
            if lower == target:
                score -= 120
            elif target in lower or lower in target:
                score -= 60
        if score < best_score:
            best_score = score
            interest, kind = classify(tags, ["geschiedenis", "natuur", "architectuur"])
            best = {
                "name": label,
                "lat": plat,
                "lng": plng,
                "kind": kind,
                "kind_label": kind_label(kind),
                "interest": interest,
                "source": "OpenStreetMap",
                "wikipedia": tags.get("wikipedia"),
                "wikidata": tags.get("wikidata"),
                "description": tags.get("description:nl") or tags.get("description") or "",
                "heritage": tags.get("heritage") or tags.get("heritage:operator") or "",
            }
    return best


async def _overpass(query: str) -> dict[str, Any]:
    errors: list[str] = []
    mirrors = [url.strip() for url in settings.overpass_urls.split(",") if url.strip()]

    async def _try(url: str, *, as_form: bool) -> dict[str, Any] | None:
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(28.0, connect=5.0),
                headers={"User-Agent": settings.user_agent},
                follow_redirects=True,
            ) as http:
                if as_form:
                    response = await http.post(url, data={"data": query})
                else:
                    response = await http.post(
                        url,
                        content=query.encode("utf-8"),
                        headers={
                            "User-Agent": settings.user_agent,
                            "Content-Type": "text/plain; charset=utf-8",
                        },
                    )
                if response.status_code >= 400:
                    errors.append(f"{url} form={as_form} -> HTTP {response.status_code}")
                    return None
                payload = response.json()
                remark = str(payload.get("remark") or "")
                if "error" in remark.lower() or "runtime error" in remark.lower():
                    errors.append(f"{url} form={as_form} -> {remark[:120]}")
                    return None
                return payload
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{url} form={as_form} -> {type(exc).__name__}: {exc or 'geen bericht'}")
            return None

    # Raw body eerst (werkt betrouwbaarder op kumi), daarna form-encoded.
    for url in mirrors:
        for as_form in (False, True):
            payload = await _try(url, as_form=as_form)
            if payload is not None:
                return payload
    raise RuntimeError("Overpass API reageerde niet: " + "; ".join(errors))


async def fetch_horeca_nominatim_along_points(
    points: list[tuple[float, float]],
    *,
    max_points: int = 8,
    per_point: int = 10,
) -> list[dict[str, Any]]:
    """Horeca via Nominatim als Overpass/Photon niet meewerken."""
    cleaned: list[tuple[float, float]] = []
    seen_pt: set[tuple[float, float]] = set()
    for lat, lng in points or []:
        if lat is None or lng is None:
            continue
        key = (round(float(lat), 3), round(float(lng), 3))
        if key in seen_pt:
            continue
        seen_pt.add(key)
        cleaned.append((float(lat), float(lng)))
    if not cleaned:
        return []
    if len(cleaned) > max_points:
        step = max(1, (len(cleaned) - 1) // (max_points - 1))
        spaced = [cleaned[i] for i in range(0, len(cleaned), step)][: max_points - 1]
        if cleaned[-1] not in spaced:
            spaced.append(cleaned[-1])
        cleaned = spaced[:max_points]

    merged: dict[str, dict[str, Any]] = {}
    queries = ("café", "restaurant")

    async def _one(lat: float, lng: float, query: str) -> None:
        delta = 0.06  # ~6–7 km
        viewbox = f"{lng - delta},{lat + delta},{lng + delta},{lat - delta}"
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(15.0, connect=5.0),
                headers={
                    "User-Agent": settings.nominatim_user_agent,
                    "Accept": "application/json",
                    "Accept-Language": "nl,en",
                },
                follow_redirects=True,
            ) as http:
                response = await http.get(
                    f"{settings.nominatim_url}/search",
                    params={
                        "q": query,
                        "format": "jsonv2",
                        "limit": per_point,
                        "viewbox": viewbox,
                        "bounded": 1,
                        "countrycodes": "be",
                        "addressdetails": 0,
                    },
                )
                if response.status_code >= 400:
                    return
                rows = response.json()
        except Exception:
            return
        if not isinstance(rows, list):
            return
        for row in rows:
            try:
                plat = float(row["lat"])
                plng = float(row["lon"])
                name = str(row.get("name") or row.get("display_name") or "").split(",")[0].strip()
            except (KeyError, TypeError, ValueError):
                continue
            if not name:
                continue
            kind = "cafe"
            cls = str(row.get("class") or "")
            typ = str(row.get("type") or "")
            if cls == "amenity" and typ in {
                "cafe",
                "pub",
                "bar",
                "restaurant",
                "biergarten",
                "fast_food",
                "ice_cream",
            }:
                kind = typ
            elif cls in {"amenity", "tourism"} and typ:
                kind = typ
            else:
                # Geen duidelijke horeca-hit (bv. straat/adres) → overslaan.
                blob = f"{name} {row.get('display_name') or ''}".lower()
                if not any(
                    key in blob
                    for key in ("café", "cafe", "restaurant", "brasserie", "pub", "bar ", "bistro")
                ):
                    continue
                if query == "restaurant" or "restaurant" in blob:
                    kind = "restaurant"
            if name.isdigit() or len(name) < 3:
                continue
            pid = f"horeca-nom-{row.get('osm_type', 'n')}{row.get('osm_id') or unique_key(name, plat, plng)}"
            merged[pid] = {
                "id": pid,
                "name": name,
                "lat": plat,
                "lng": plng,
                "kind": kind,
                "kind_label": kind_label(kind),
                "interest": "horeca",
                "source": "Nominatim",
                "wikipedia": None,
                "wikidata": None,
                "description": "",
                "heritage": "",
            }

    tasks = [_one(lat, lng, query) for lat, lng in cleaned for query in queries]
    # Nominatim rate-limit: kleine batches, korte pauze.
    for index in range(0, len(tasks), 4):
        await asyncio.gather(*tasks[index : index + 4], return_exceptions=True)
        if index + 4 < len(tasks):
            await asyncio.sleep(0.85)
    return list(merged.values())
