from __future__ import annotations

import asyncio
import time
from typing import Any

from app.config import settings
from app.models import Knooppunt, Locality, Place, PlanRequest, RerouteRequest, RerouteResponse, RoutePlan, Stop, WeatherInfo
from app.services import events as events_service
from app.services import geocoding
from app.services import knooppunten as knoop_service
from app.services import places as places_service
from app.services import pois as pois_service
from app.services import routing
from app.services import suggestions as suggestion_service
from app.services import weather as weather_service
from app.services import wikipedia
from app.services.ai import enrich_with_ai, fallback_scripts, has_ai, interpret_wish_notes, polish_scripts, rank_wish_poi_suggestions
from app.services.geo import (
    haversine_m,
    point_on_geometry_at_progress,
    point_to_segment_m,
    snap_point_on_geometry_with_progress,
    unique_key,
)


SPEED_KMH = {
    ("recreant", "stadsfiets"): 14,
    ("recreant", "ebike"): 18,
    ("recreant", "racefiets"): 18,
    ("recreant", "gravel"): 15,
    ("sportief", "stadsfiets"): 17,
    ("sportief", "ebike"): 22,
    ("sportief", "racefiets"): 24,
    ("sportief", "gravel"): 19,
    ("wielrenner", "stadsfiets"): 20,
    ("wielrenner", "ebike"): 24,
    ("wielrenner", "racefiets"): 28,
    ("wielrenner", "gravel"): 22,
}

# Suggesties: hergebruik recente zoekresultaten (zelfde gebied/wens).
_WISH_POI_CACHE: dict[str, tuple[float, tuple[list[dict[str, Any]], str]]] = {}
_WISH_POI_TTL_S = 12 * 60


def _wish_poi_cache_key(
    notes: str,
    profile_interests: list[str] | None,
    geometry: list[list[float]],
    chain: list[dict[str, Any]],
) -> str:
    mid = geometry[len(geometry) // 2] if geometry else [0.0, 0.0]
    interests = ",".join(sorted(pois_service._unique_interests(profile_interests or [])))
    note = (notes or "").strip().lower()[:48]
    knoop = "|".join(
        str(n.get("geoid") or n.get("number") or "") for n in (chain or [])[:4]
    )
    return f"{round(float(mid[0]), 2)}:{round(float(mid[1]), 2)}|{interests}|{note}|{knoop}"


def _place_from_knoop(node: dict[str, Any]) -> Place:
    number = str(node.get("number") or "")
    return Place(
        lat=float(node["lat"]),
        lng=float(node["lng"]),
        label=f"Knooppunt {number}" if number else "Startknooppunt",
        country="BE",
        place_name=f"Knooppunt {number}" if number else None,
        municipality=None,
    )


async def _plan_user_knoop_route(
    request: PlanRequest,
    user_chain: list[dict[str, Any]],
    catalog_route: dict[str, Any] | None = None,
    *,
    start_place: Place | None = None,
    end_place: Place | None = None,
) -> RoutePlan:
    """Snelle planning voor zelfgekozen knooppunten — geen zware Overpass/AI-wachttijd."""
    profile = request.profile
    user_chain = await knoop_service.prune_chain_backtracking(
        user_chain, close_loop=request.mode == "lus"
    )
    first = user_chain[0]
    last = user_chain[-1]
    start = start_place or _place_from_knoop(first)
    end = end_place or (_place_from_knoop(last) if request.mode == "punt" else start)

    weather_raw = await _optional(weather_service.fetch_weather(start.lat, start.lng), None, 3)
    weather = WeatherInfo(**(weather_raw or {})) if isinstance(weather_raw, dict) else WeatherInfo()

    spine = _chain_spine(user_chain)
    display_chain = list(spine)
    if request.mode == "lus":
        display_chain = knoop_service.close_chain_for_loop(display_chain)

    # 1) Hergebruik magenta draft van de frontend (al berekend tijdens knoopkeuze).
    draft_geom = [
        [float(pt[0]), float(pt[1])]
        for pt in (request.route_geometry or [])
        if isinstance(pt, (list, tuple)) and len(pt) >= 2
    ]
    if len(draft_geom) >= 2:
        dist_km = _geometry_length_km(draft_geom)
        if dist_km < 0.4:
            dist_km = float(max(8, request.distance_km or 8))
        duration_min = int(request.route_duration_min or max(1, round((dist_km / 16) * 60)))
        route = {
            "geometry": draft_geom,
            "distance_m": dist_km * 1000.0,
            "duration_s": max(60.0, duration_min * 60.0),
            "steps": [],
        }
    else:
        # 2) Snelle OSRM — geen trage WFS-fallback (die veroorzaakte timeouts).
        try:
            route = await _osrm_route_along_chain(
                start.lat,
                start.lng,
                user_chain,
                close_loop=request.mode != "punt",
                end_lat=end.lat if request.mode == "punt" else None,
                end_lng=end.lng if request.mode == "punt" else None,
                timeout=20.0,
            )
        except Exception as exc:
            raise ValueError(
                "De knooppuntenroute kon niet snel genoeg berekend worden. "
                "Wacht tot de magenta lijn zichtbaar is en probeer opnieuw."
            ) from exc
    chain = display_chain or list(user_chain)
    knoop_label = knoop_service.chain_label(
        knoop_service.close_chain_for_loop(chain) if request.mode == "lus" else chain
    )

    selected = _merge_user_pois([], request.poi_picks)
    stops: list[dict[str, Any]] = []
    for index, poi in enumerate(selected, start=1):
        wiki = poi.get("wiki") or {}
        scripts = fallback_scripts(poi, wiki, request.explanation_level)
        stops.append(
            {
                "id": poi["id"],
                "name": poi["name"],
                "lat": poi["lat"],
                "lng": poi["lng"],
                "kind": poi.get("kind_label") or poi.get("kind") or "plek",
                "interest": poi.get("interest") or "geschiedenis",
                "source": poi.get("source") or "OpenStreetMap",
                "summary": poi.get("summary") or scripts["summary"],
                "approaching": scripts["approaching"],
                "arrived": scripts["arrived"],
                "why": scripts["why"],
                "wikipedia_url": wiki.get("url") or None,
                "image_url": wiki.get("image") or None,
                "wikipedia": poi.get("wikipedia"),
                "wikidata": poi.get("wikidata"),
                "description": poi.get("description") or "",
                "kind_label": poi.get("kind_label"),
                "index": index,
                "matches_wish": True,
                "on_route": True,
            }
        )

    # Snelle wens/profiel-suggesties (Nominatim/Wikipedia — geen trage Overpass).
    profile_interests = list(request.interests or [])
    if profile and getattr(profile, "interests", None):
        profile_interests = list(dict.fromkeys([*profile_interests, *list(profile.interests)]))
    notes = (request.notes or "").strip()
    # Wens- én/of profielsuggesties (Nominatim/Wikipedia — geen Overpass-hang).
    if route.get("geometry") and (notes or profile_interests):
        try:
            wish_pois, _ = await asyncio.wait_for(
                _fast_wish_pois_for_manual(
                    notes,
                    chain,
                    route["geometry"],
                    profile_interests,
                ),
                timeout=18.0,
            )
            _merge_wish_into_stops(stops, wish_pois, request, route["geometry"])
        except TimeoutError:
            pass
        except Exception:
            pass

    stops = places_service.assign_sides(stops, route["geometry"])
    model_stops = [
        Stop(
            id=s["id"],
            name=s["name"],
            lat=s["lat"],
            lng=s["lng"],
            kind=s["kind"],
            interest=s["interest"],
            source=s["source"],
            summary=s["summary"],
            approaching=s["approaching"],
            arrived=s["arrived"],
            why=s["why"],
            wikipedia_url=s.get("wikipedia_url"),
            image_url=s.get("image_url"),
            wikipedia=s.get("wikipedia"),
            wikidata=s.get("wikidata"),
            description=s.get("description"),
            place_name=s.get("place_name"),
            population=s.get("population"),
            local_fact=s.get("local_fact"),
            side=s.get("side"),
            matches_wish=bool(s.get("matches_wish")),
            on_route=bool(s.get("on_route")),
            hint=s.get("hint"),
            wish_source=s.get("wish_source") if s.get("wish_source") in {"wish", "profile"} else None,
        )
        for s in stops
    ]
    knoop_chain_display = (
        knoop_service.close_chain_for_loop(chain) if request.mode == "lus" else chain
    )
    knoop_models = [
        Knooppunt(
            id=n.get("id") or "",
            number=str(n["number"]),
            lat=float(n["lat"]),
            lng=float(n["lng"]),
            network=n.get("network"),
            on_route=True,
            geoid=n.get("geoid"),
        )
        for n in knoop_chain_display
    ]
    chain_ids = {n.get("id") for n in chain if n.get("id")}
    all_knoop = _unique_knooppunten(chain, chain_ids)
    title = f"Knooppuntenroute {knoop_label}" if knoop_label else "Jouw knooppuntenroute"
    intro = (
        f"Je volgt de knooppunten {knoop_label}."
        if knoop_label
        else "Je volgt de knooppunten die je zelf koos."
    )
    if catalog_route:
        title = catalog_route.get("title") or title
    sources = sorted(
        {stop.source for stop in model_stops}
        | {"OpenStreetMap", "OSRM fietsrouting", "Fietsknooppuntennetwerk Vlaanderen", "Open-Meteo"}
    )
    return RoutePlan(
        title=title,
        intro=intro,
        mode=request.mode,
        interests=request.interests,
        notes=(request.notes or "").strip(),
        start=start,
        end=end,
        distance_km=round(float(route["distance_m"]) / 1000, 1),
        duration_min=max(1, round(float(route["duration_s"]) / 60)),
        geometry=route["geometry"],
        stops=model_stops,
        knooppunten=knoop_models,
        all_knooppunten=all_knoop,
        knoop_chain=knoop_label,
        route_reason="Eigen knooppuntenroute",
        steps=route.get("steps") or [],
        explanation_level=request.explanation_level,
        interaction=(profile.interaction if profile else "live"),
        weather=weather,
        budget_mode=request.budget_mode,
        duration_budget_min=request.duration_min,
        localities=_localities_from_stops(model_stops),
        sources=sources,
        ai_used=False,
    )


def _osrm_waypoints_for_chain(
    start_lat: float,
    start_lng: float,
    chain: list[dict[str, Any]],
    *,
    close_loop: bool,
    end_lat: float | None = None,
    end_lng: float | None = None,
) -> list[tuple[float, float]]:
    spine = _chain_spine(chain)
    points: list[tuple[float, float]] = [(start_lat, start_lng)]
    for node in spine:
        pt = (float(node["lat"]), float(node["lng"]))
        if haversine_m(points[-1][0], points[-1][1], pt[0], pt[1]) > 40:
            points.append(pt)
    if close_loop:
        if haversine_m(points[-1][0], points[-1][1], start_lat, start_lng) > 40:
            points.append((start_lat, start_lng))
    elif end_lat is not None and end_lng is not None:
        if haversine_m(points[-1][0], points[-1][1], end_lat, end_lng) > 40:
            points.append((end_lat, end_lng))
    return points


async def _osrm_route_along_chain(
    start_lat: float,
    start_lng: float,
    chain: list[dict[str, Any]],
    *,
    close_loop: bool,
    end_lat: float | None = None,
    end_lng: float | None = None,
    timeout: float = 22.0,
) -> dict[str, Any]:
    points = _osrm_waypoints_for_chain(
        start_lat,
        start_lng,
        chain,
        close_loop=close_loop,
        end_lat=end_lat,
        end_lng=end_lng,
    )
    if len(points) < 2:
        raise ValueError("Te weinig punten voor een fietsroute.")
    try:
        return await asyncio.wait_for(routing.bike_route_via_waypoints(points), timeout=timeout)
    except TimeoutError as exc:
        raise ValueError(
            "De route duurde te lang. Probeer minder kilometers of een andere start."
        ) from exc


async def plan_route(request: PlanRequest) -> RoutePlan:
    catalog_route = suggestion_service.get_route_by_id(request.suggestion_id) if request.suggestion_id else None
    if catalog_route:
        request.interests = suggestion_service.merge_interests(catalog_route, request.interests)
        request.notes = suggestion_service.merge_notes(catalog_route, request.notes)
    wish_interests = pois_service.interests_from_notes(request.notes)
    if wish_interests:
        request.interests = list(dict.fromkeys([*request.interests, *wish_interests]))

    # Zelf gekozen knooppunten: snelle route zonder zware Overpass/AI.
    user_chain = knoop_service.chain_from_user(
        [n.model_dump() for n in request.knooppunten],
        request.mode == "lus",
    )
    if user_chain:
        return await _plan_user_knoop_route(request, user_chain, catalog_route)

    return await _plan_auto_knoop_route(request, catalog_route)


async def _plan_auto_knoop_route(
    request: PlanRequest,
    catalog_route: dict[str, Any] | None = None,
) -> RoutePlan:
    """Snelle auto/suggest-planning op knooppunten + gekozen afstand (geen Overpass-hang)."""
    start = await geocoding.geocode_one(request.start)
    if request.mode == "punt":
        if not request.end:
            raise ValueError("Kies een eindpunt voor Van A naar B, of kies een lus.")
        end = await geocoding.geocode_one(request.end)
    else:
        end = start

    profile = request.profile
    weather_raw = await _optional(weather_service.fetch_weather(start.lat, start.lng), None, 3)
    weather = WeatherInfo(**(weather_raw or {})) if isinstance(weather_raw, dict) else WeatherInfo()
    request.distance_km = _effective_distance(request, profile, weather)

    if not (50.55 <= start.lat <= 51.55 and 2.35 <= start.lng <= 5.95):
        raise ValueError(
            "Kies een startpunt in Vlaanderen. Buiten het knooppuntennetwerk kan er geen tocht worden gepland."
        )

    radius = _search_radius(request, start, end)
    extra = (end.lat, end.lng) if request.mode == "punt" else None
    rider_notes = _profile_notes(profile, request.notes, request.adapt_reason, weather)

    nodes = await _optional(knoop_service.fetch_nodes(start.lat, start.lng, radius, extra), [], 12)
    if not nodes:
        raise ValueError(
            "Geen knooppunten gevonden. Probeer een andere startlocatie in Vlaanderen."
        )

    if rider_notes.strip():
        wiki_places = await _optional(
            wikipedia.places_for_route(start.lat, start.lng, min(radius, 10000), extra),
            [],
            6,
        )
        if wiki_places:
            knoop_service.attach_nearby(nodes, wiki_places)
            knoop_service.score_nodes_for_notes(nodes, rider_notes)

    geometric = knoop_service.plan_node_chain(nodes, start.lat, start.lng, request.distance_km, extra)
    noted = (
        knoop_service.chain_from_notes(nodes, start.lat, start.lng, request.distance_km, extra)
        if rider_notes.strip()
        else []
    )
    chain = noted or geometric

    if rider_notes.strip() and chain:
        start_node = knoop_service.nearest_node(nodes, start.lat, start.lng) if nodes else None
        ai_nodes = _nodes_for_ai(nodes, start_node) if nodes else []
        ai_choice = await _optional(
            enrich_with_ai(
                start.label,
                request.interests,
                rider_notes,
                [],
                request.mode,
                request.distance_km,
                knoop_service.chain_label(chain) if chain else "",
                ai_nodes,
                profile,
            ),
            None,
            8,
        )
        if ai_choice and start_node:
            ai_chain = knoop_service.resolve_chain(
                ai_choice.get("knoop_ids") or [],
                nodes,
                start_node,
                request.mode == "lus",
            )
            if ai_chain:
                chain = ai_chain

    if not chain:
        raise ValueError(
            "Geen knooppuntenroute gevonden. Probeer een andere afstand of startlocatie."
        )

    chain = await knoop_service.prune_chain_backtracking(
        chain, close_loop=request.mode == "lus"
    )

    # Snelle geometrie: OSRM langs de gekozen knooppunten (geen zware WFS-netwerkrouting).
    try:
        route = await _osrm_route_along_chain(
            start.lat,
            start.lng,
            chain,
            close_loop=request.mode == "lus",
            end_lat=end.lat if request.mode == "punt" else None,
            end_lng=end.lng if request.mode == "punt" else None,
            timeout=22.0,
        )
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("Kon geen fietsroute berekenen. Probeer opnieuw.") from exc

    display_chain = list(_chain_spine(chain))
    if request.mode == "lus":
        display_chain = knoop_service.close_chain_for_loop(display_chain)
    knoop_label = knoop_service.chain_label(display_chain)

    title = catalog_route["title"] if catalog_route else _title(request, start, end, knoop_label)
    intro = (
        suggestion_service.catalog_intro(
            catalog_route,
            request.interests,
            round(float(route["distance_m"]) / 1000, 1),
            start.label,
        )
        if catalog_route
        else (
            f"Je volgt de knooppunten {knoop_label}."
            if knoop_label
            else "Een knooppuntenlus op basis van je gekozen afstand."
        )
    )

    knoop_models = [
        Knooppunt(
            id=n.get("id") or "",
            number=str(n["number"]),
            lat=float(n["lat"]),
            lng=float(n["lng"]),
            network=n.get("network"),
            on_route=True,
            geoid=n.get("geoid"),
        )
        for n in display_chain
    ]

    # Wens- én profielsuggesties meegeven aan de rit (snelle Nominatim/Wikipedia).
    profile_interests = list(request.interests or [])
    if profile and getattr(profile, "interests", None):
        profile_interests = list(dict.fromkeys([*profile_interests, *list(profile.interests)]))
    notes = (request.notes or "").strip()
    stops: list[dict[str, Any]] = []
    selected = _merge_user_pois([], request.poi_picks)
    for index, poi in enumerate(selected, start=1):
        wiki = poi.get("wiki") or {}
        scripts = fallback_scripts(poi, wiki, request.explanation_level)
        stops.append(
            {
                "id": poi["id"],
                "name": poi["name"],
                "lat": poi["lat"],
                "lng": poi["lng"],
                "kind": poi.get("kind_label") or poi.get("kind") or "plek",
                "interest": poi.get("interest") or "geschiedenis",
                "source": poi.get("source") or "OpenStreetMap",
                "summary": poi.get("summary") or scripts["summary"],
                "approaching": scripts["approaching"],
                "arrived": scripts["arrived"],
                "why": scripts["why"],
                "wikipedia_url": wiki.get("url") or None,
                "image_url": wiki.get("image") or None,
                "wikipedia": poi.get("wikipedia"),
                "wikidata": poi.get("wikidata"),
                "description": poi.get("description") or "",
                "kind_label": poi.get("kind_label"),
                "index": index,
                "matches_wish": True,
                "on_route": True,
            }
        )
    if route.get("geometry") and (notes or profile_interests):
        try:
            wish_pois, _ = await asyncio.wait_for(
                _fast_wish_pois_for_manual(
                    notes,
                    display_chain,
                    route["geometry"],
                    profile_interests,
                ),
                timeout=18.0,
            )
            _merge_wish_into_stops(stops, wish_pois, request, route["geometry"])
        except TimeoutError:
            pass
        except Exception:
            pass
    stops = places_service.assign_sides(stops, route["geometry"]) if route.get("geometry") else stops
    model_stops = [
        Stop(
            id=s["id"],
            name=s["name"],
            lat=s["lat"],
            lng=s["lng"],
            kind=s["kind"],
            interest=s["interest"],
            source=s["source"],
            summary=s["summary"],
            approaching=s["approaching"],
            arrived=s["arrived"],
            why=s["why"],
            wikipedia_url=s.get("wikipedia_url"),
            image_url=s.get("image_url"),
            wikipedia=s.get("wikipedia"),
            wikidata=s.get("wikidata"),
            description=s.get("description"),
            place_name=s.get("place_name"),
            population=s.get("population"),
            local_fact=s.get("local_fact"),
            side=s.get("side"),
            matches_wish=bool(s.get("matches_wish")),
            on_route=bool(s.get("on_route")),
            hint=s.get("hint"),
            wish_source=s.get("wish_source") if s.get("wish_source") in {"wish", "profile"} else None,
        )
        for s in stops
    ]
    sources = sorted(
        {stop.source for stop in model_stops}
        | {"OpenStreetMap", "OSRM fietsrouting", "Fietsknooppuntennetwerk Vlaanderen", "Open-Meteo"}
    )
    return RoutePlan(
        title=title,
        intro=intro,
        mode=request.mode,
        interests=request.interests,
        notes=(request.notes or "").strip(),
        start=start,
        end=end,
        distance_km=round(float(route["distance_m"]) / 1000, 1),
        duration_min=max(1, round(float(route["duration_s"]) / 60)),
        geometry=route["geometry"],
        stops=model_stops,
        knooppunten=knoop_models,
        all_knooppunten=_unique_knooppunten(display_chain, {n.get("id") for n in display_chain if n.get("id")}),
        knoop_chain=knoop_label,
        route_reason="Route op basis van je afstand en knooppunten",
        steps=route.get("steps") or [],
        explanation_level=request.explanation_level,
        interaction=(profile.interaction if profile else "live"),
        weather=weather,
        budget_mode=request.budget_mode,
        duration_budget_min=request.duration_min,
        localities=_localities_from_stops(model_stops),
        sources=sources,
        ai_used=False,
    )


async def reroute(request: RerouteRequest) -> RerouteResponse:
    weather = WeatherInfo(**await weather_service.fetch_weather(request.start_lat, request.start_lng))
    nodes = list(request.nodes)
    reason = ""
    # Alleen verkorten bij expliciete aanpassing (regen/wind/…), nooit automatisch
    # bij eigen gekozen knooppunten — anders verdwijnen picks uit de route.
    if request.reason in {"regen", "wind", "korter"}:
        target = request.target_km or max(8.0, (weather.suggest_shorter and 12.0) or 12.0)
        if request.reason == "wind":
            reason = weather.alert or "Kortere lus door wind."
        elif request.reason == "regen":
            reason = weather.alert or "Kortere lus door regen."
        else:
            reason = weather.alert or "Kortere knooppuntenroute voorgesteld."
        nodes = _shorten_nodes(nodes, request.start_lat, request.start_lng, target, request.close_loop)
    elif request.reason == "veer":
        reason = "Omleiding: veerpont vermeden of route verkort."
        if len(nodes) > 3:
            mid = len(nodes) // 2
            nodes = nodes[:mid] + nodes[mid + 1 :]
    if request.remaining_nodes:
        nodes = [
            Knooppunt(
                id=n.id,
                number=n.number,
                lat=n.lat,
                lng=n.lng,
                network=n.network,
                on_route=True,
            )
            for n in request.remaining_nodes
        ] or nodes

    if len(nodes) < 1:
        raise ValueError("Selecteer minstens één knooppunt.")
    chain = [
        {
            "id": n.id or f"{n.number}|{round(n.lat, 4)}",
            "number": n.number,
            "lat": n.lat,
            "lng": n.lng,
            "network": n.network,
            "geoid": n.geoid,
        }
        for n in nodes
    ]
    display_chain, route = await _build_knoop_route(
        request.start_lat,
        request.start_lng,
        chain,
        close_loop=request.close_loop,
        end_lat=request.end_lat,
        end_lng=request.end_lng,
        poi_picks=request.poi_picks,
    )
    knoop_chain_display = (
        knoop_service.close_chain_for_loop(display_chain) if request.close_loop else display_chain
    )
    knoop_models = [
        Knooppunt(
            id=n.get("id") or "",
            number=n["number"],
            lat=n["lat"],
            lng=n["lng"],
            network=n.get("network"),
            on_route=True,
            geoid=n.get("geoid"),
        )
        for n in knoop_chain_display
    ]
    return RerouteResponse(
        geometry=route["geometry"],
        distance_km=round(route["distance_m"] / 1000, 1),
        duration_min=max(1, round(route["duration_s"] / 60)),
        knooppunten=knoop_models,
        knoop_chain=knoop_service.chain_label(knoop_chain_display),
        steps=route.get("steps") or [],
        reason=reason,
        weather=weather,
    )


async def preview_route(
    lat: float,
    lng: float,
    distance_km: int,
    mode: str = "lus",
    end_lat: float | None = None,
    end_lng: float | None = None,
    notes: str = "",
    poi_picks: list[Any] | None = None,
    profile_interests: list[str] | None = None,
) -> dict[str, Any]:
    extra = (end_lat, end_lng) if mode == "punt" and end_lat is not None and end_lng is not None else None
    if mode == "punt" and extra:
        direct = haversine_m(lat, lng, end_lat, end_lng)
        radius = int(min(16000, max(5000, direct / 2 + 4000)))
    else:
        radius = int(min(16000, max(5000, distance_km * 1000 / 2.2)))

    wish_interests = pois_service.wish_interests_for_notes(notes, profile_interests)
    # Preview blijft licht: alleen knooppunten + OSRM. Suggesties komen apart via /api/wish-suggestions.
    nodes = await _optional(knoop_service.fetch_nodes(lat, lng, radius, extra), [], 16)
    if nodes and notes.strip():
        knoop_service.score_nodes_for_notes(nodes, notes)
    geometric = knoop_service.plan_node_chain(nodes, lat, lng, distance_km, extra)
    noted = (
        knoop_service.chain_from_notes(nodes, lat, lng, distance_km, extra)
        if notes.strip()
        else []
    )
    chain = noted or geometric
    if not chain:
        raise ValueError("Geen knooppunten gevonden voor een routevoorbeeld.")

    chain = await knoop_service.prune_chain_backtracking(chain, close_loop=mode == "lus")

    stub = [[float(n["lat"]), float(n["lng"])] for n in chain]
    route, wish_pack = await asyncio.gather(
        _osrm_route_along_chain(
            lat,
            lng,
            chain,
            close_loop=mode == "lus",
            end_lat=end_lat if mode == "punt" else None,
            end_lng=end_lng if mode == "punt" else None,
            timeout=20.0,
        ),
        _fast_wish_pois_for_manual(notes or "", chain, stub, profile_interests),
        return_exceptions=True,
    )
    if isinstance(route, BaseException):
        raise route
    wish_pois, wish_summary = ([], "")
    if isinstance(wish_pack, tuple) and len(wish_pack) == 2:
        wish_pois, wish_summary = wish_pack
    display_chain = list(_chain_spine(chain))
    knoop_chain_display = (
        knoop_service.close_chain_for_loop(display_chain) if mode == "lus" else display_chain
    )
    return {
        "geometry": route["geometry"],
        "distance_km": round(route["distance_m"] / 1000, 1),
        "duration_min": max(1, round(route["duration_s"] / 60)),
        "wish_summary": wish_summary or None,
        "knooppunten": [
            Knooppunt(
                id=n.get("id") or "",
                number=n["number"],
                lat=n["lat"],
                lng=n["lng"],
                network=n.get("network"),
                on_route=True,
            )
            for n in knoop_chain_display
        ],
        "knoop_chain": knoop_service.chain_label(knoop_chain_display),
        "suggestions": _format_wish_suggestions(wish_pois or [], notes or "", profile_interests),
    }


def _format_wish_suggestions(
    wish_pois: list[dict[str, Any]],
    notes: str,
    profile_interests: list[str] | None = None,
) -> list[dict[str, Any]]:
    suggestions: list[dict[str, Any]] = []
    note_interests = pois_service.interests_from_notes(notes)
    note_set = set(note_interests)
    profile_set = set(pois_service._unique_interests(profile_interests or []))
    valid = {
        "geschiedenis",
        "natuur",
        "landbouw",
        "horeca",
        "oorlog",
        "architectuur",
        "activiteiten",
        "evenementen",
    }
    fallback_interest = (
        note_interests[0]
        if note_interests
        else ((profile_interests or [None])[0] or "geschiedenis")
    )
    if fallback_interest not in valid:
        fallback_interest = "geschiedenis"
    for poi in wish_pois:
        try:
            interest = poi.get("interest") or fallback_interest
            if interest not in valid:
                interest = fallback_interest
            hint = poi.get("hint")
            source = poi.get("source")
            if source not in {"wish", "profile"}:
                if hint == "uit je profiel":
                    source = "profile"
                elif (notes or "").strip() and (
                    pois_service.matches_notes(poi, notes)
                    or interest in note_set
                    or hint == "past bij je wens"
                ):
                    source = "wish"
                elif interest in profile_set and interest not in note_set:
                    source = "profile"
                    hint = hint or "uit je profiel"
                else:
                    source = "wish" if (notes or "").strip() else "profile"
            if source == "profile" and not hint:
                hint = "uit je profiel"
            if source == "wish" and (notes or "").strip() and not hint:
                hint = "past bij je wens"
            suggestions.append(
                {
                    "id": str(poi["id"]),
                    "name": poi["name"],
                    "lat": float(poi["lat"]),
                    "lng": float(poi["lng"]),
                    "kind": str(poi.get("kind") or "plek"),
                    "kind_label": poi.get("kind_label"),
                    "interest": interest,
                    "on_route": bool(poi.get("on_route")),
                    "hint": hint,
                    "source": source,
                }
            )
        except Exception:
            continue
    return suggestions


async def wish_suggestions_along_route(
    notes: str,
    geometry: list[list[float]],
    nodes: list[Any] | None = None,
    profile_interests: list[str] | None = None,
) -> dict[str, Any]:
    """Suggesties voor een bestaande (manuele) knooppuntenroute op basis van extra wens."""
    if not geometry or len(geometry) < 2:
        return {"suggestions": [], "wish_summary": None}
    if not (notes or "").strip() and not (profile_interests or []):
        return {"suggestions": [], "wish_summary": None}
    chain: list[dict[str, Any]] = []
    for node in nodes or []:
        data = node.model_dump() if hasattr(node, "model_dump") else dict(node)
        if data.get("lat") is None or data.get("lng") is None:
            continue
        chain.append(
            {
                "id": data.get("id") or "",
                "number": str(data.get("number") or ""),
                "lat": float(data["lat"]),
                "lng": float(data["lng"]),
                "network": data.get("network"),
                "geoid": data.get("geoid"),
            }
        )
    try:
        wish_pois, wish_summary = await _fast_wish_pois_for_manual(
            notes or "",
            chain,
            geometry,
            profile_interests,
        )
    except Exception:
        wish_pois, wish_summary = [], None
    if not wish_pois and chain:
        # Noodpad: Photon rond de knooppunten — nooit stil leeg als er knopen zijn.
        try:
            pts = [(float(n["lat"]), float(n["lng"])) for n in chain[:4]]
            cafe = await pois_service.fetch_horeca_photon_along_points(
                pts,
                max_points=min(4, len(pts)),
                per_point=8,
                queries=("café", "taverne"),
                osm_tags=("amenity:cafe", "amenity:pub"),
            )
            wiki = await _optional(
                wikipedia.places_for_route(pts[0][0], pts[0][1], 8000, pts[-1] if len(pts) > 1 else None),
                [],
                5,
            )
            for poi in wiki or []:
                if profile_interests:
                    poi["interest"] = profile_interests[0]
                    poi["hint"] = poi.get("hint") or "uit je profiel"
            wish_pois = _merge(cafe or [], wiki or [])
            if wish_pois:
                wish_summary = "Plekken langs je knooppunten (noodzoektocht)."
        except Exception:
            wish_pois, wish_summary = [], None
    return {
        "suggestions": _format_wish_suggestions(wish_pois, notes or "", profile_interests),
        "wish_summary": wish_summary or None,
    }


def _unique_knooppunten(nodes: list[dict[str, Any]], chain_ids: set[str]) -> list[Knooppunt]:
    seen: set[str] = set()
    result: list[Knooppunt] = []
    for node in nodes:
        nid = node.get("id") or f"{node['number']}|{round(node['lat'], 4)}"
        if nid in seen:
            continue
        seen.add(nid)
        result.append(
            Knooppunt(
                id=nid,
                number=node["number"],
                lat=node["lat"],
                lng=node["lng"],
                network=node.get("network"),
                on_route=nid in chain_ids,
                geoid=node.get("geoid"),
            )
        )
    return result


def _insert_poi_waypoints(
    waypoints: list[tuple[float, float]],
    picks: list[Any],
) -> list[tuple[float, float]]:
    """Legacy helper — voorkeur gaat naar _apply_poi_spurs op de basisroute."""
    if not picks:
        return waypoints
    points = list(waypoints)
    ordered: list[tuple[float, int, float, float]] = []
    for pick in picks:
        data = pick.model_dump() if hasattr(pick, "model_dump") else dict(pick)
        plat = float(data["lat"])
        plng = float(data["lng"])
        best_i = 0
        best_score = float("inf")
        for index in range(len(points) - 1):
            a_lat, a_lng = points[index]
            b_lat, b_lng = points[index + 1]
            score = point_to_segment_m(plat, plng, a_lat, a_lng, b_lat, b_lng)
            if score < best_score:
                best_score = score
                best_i = index
        ordered.append((best_score, best_i, plat, plng))
    for _, index, plat, plng in sorted(ordered, key=lambda item: item[1], reverse=True):
        insert_at = index + 1
        if insert_at < len(points) and haversine_m(plat, plng, points[insert_at][0], points[insert_at][1]) < 15:
            continue
        points.insert(insert_at, (plat, plng))
    return routing._clean_waypoints(points)


def _vertex_index_at_progress(geometry: list[list[float]], progress_m: float) -> int:
    """Index van het vertex net vóór of op progress_m langs de polyline."""
    if not geometry:
        return 0
    if len(geometry) < 2:
        return 0
    remaining = max(0.0, float(progress_m))
    for index in range(len(geometry) - 1):
        seg = haversine_m(
            geometry[index][0],
            geometry[index][1],
            geometry[index + 1][0],
            geometry[index + 1][1],
        )
        if remaining <= seg:
            return index
        remaining -= seg
    return max(0, len(geometry) - 2)


async def _osrm_leg(a: tuple[float, float], b: tuple[float, float]) -> list[list[float]]:
    if haversine_m(a[0], a[1], b[0], b[1]) < 12:
        return [[a[0], a[1]], [b[0], b[1]]]
    try:
        osrm = await routing.bike_route([a, b], retries=1)
        piece = list(osrm.get("geometry") or [])
        if not piece:
            return [[a[0], a[1]], [b[0], b[1]]]
        piece[0] = [a[0], a[1]]
        piece[-1] = [b[0], b[1]]
        return piece
    except Exception:
        return [[a[0], a[1]], [b[0], b[1]]]


async def _apply_poi_spurs(
    route: dict[str, Any],
    picks: list[Any],
) -> dict[str, Any]:
    """Voeg POI's toe als korte lokale aftakkingen op de bestaande knooppuntenroute.

    Zo blijft A→B op het netwerk intact. We doen niet A→POI→B (dat veroorzaakt
    heen-en-weer-lussen). In plaats daarvan: route tot snap → POI → verderop op de route.
    """
    geometry = [list(point) for point in (route.get("geometry") or []) if point and len(point) >= 2]
    if len(geometry) < 2 or not picks:
        return route

    placements: list[dict[str, Any]] = []
    seen: set[str] = set()
    for pick in picks:
        data = pick.model_dump() if hasattr(pick, "model_dump") else dict(pick)
        try:
            plat = float(data["lat"])
            plng = float(data["lng"])
        except (KeyError, TypeError, ValueError):
            continue
        key = unique_key(str(data.get("id") or data.get("name") or ""), plat, plng)
        if key in seen:
            continue
        seen.add(key)
        snap_lat, snap_lng, dist, progress = snap_point_on_geometry_with_progress(plat, plng, geometry)
        # Al vrijwel op de route: geen aftakking nodig.
        if dist < 35:
            continue
        placements.append(
            {
                "lat": plat,
                "lng": plng,
                "snap_lat": snap_lat,
                "snap_lng": snap_lng,
                "dist": dist,
                "progress": progress,
            }
        )

    if not placements:
        return route

    # Van achter naar voren invoegen zodat progress-indices stabiel blijven.
    placements.sort(key=lambda item: item["progress"], reverse=True)
    distance_m = float(route.get("distance_m") or 0)
    duration_s = float(route.get("duration_s") or 0)
    steps = list(route.get("steps") or [])

    for item in placements:
        snap = (float(item["snap_lat"]), float(item["snap_lng"]))
        poi = (float(item["lat"]), float(item["lng"]))
        progress = float(item["progress"])
        idx = _vertex_index_at_progress(geometry, progress)

        # Verderop op de route hervatten (niet terugkeren naar snap) → minder overlap.
        ahead_m = 120.0 if item["dist"] < 400 else 180.0
        ahead = point_on_geometry_at_progress(geometry, progress + ahead_m)
        if haversine_m(snap[0], snap[1], ahead[0], ahead[1]) < 45:
            ahead = point_on_geometry_at_progress(geometry, progress + 220.0)
        ahead_idx = _vertex_index_at_progress(geometry, progress + ahead_m)
        if ahead_idx <= idx:
            ahead_idx = min(len(geometry) - 1, idx + 1)
            ahead = (float(geometry[ahead_idx][0]), float(geometry[ahead_idx][1]))

        # Als we dicht bij het einde zitten: korte heen-en-terug naar snap.
        near_end = ahead_idx >= len(geometry) - 1 and haversine_m(
            ahead[0], ahead[1], geometry[-1][0], geometry[-1][1]
        ) < 40
        to_poi = await _osrm_leg(snap, poi)
        if near_end:
            from_poi = await _osrm_leg(poi, snap)
            resume = snap
            resume_idx = idx
        else:
            from_poi = await _osrm_leg(poi, ahead)
            resume = ahead
            resume_idx = ahead_idx

        spur = list(to_poi)
        if from_poi:
            spur.extend(from_poi[1:] if spur else from_poi)

        left = [list(point) for point in geometry[: idx + 1]]
        if not left:
            left = [[snap[0], snap[1]]]
        elif haversine_m(left[-1][0], left[-1][1], snap[0], snap[1]) > 10:
            left.append([snap[0], snap[1]])
        else:
            left[-1] = [snap[0], snap[1]]

        right = [list(point) for point in geometry[resume_idx:]]
        if right:
            if haversine_m(right[0][0], right[0][1], resume[0], resume[1]) > 10:
                right = [[resume[0], resume[1]], *right]
            else:
                right[0] = [resume[0], resume[1]]
        else:
            right = [[resume[0], resume[1]]]

        # spur begint op snap; left eindigt op snap → skip eerste spur-punt.
        mid = spur[1:] if spur and haversine_m(spur[0][0], spur[0][1], snap[0], snap[1]) < 15 else spur
        # right begint op resume; mid eindigt op resume → skip eerste right-punt.
        if mid and right and haversine_m(mid[-1][0], mid[-1][1], right[0][0], right[0][1]) < 15:
            geometry = left + mid + right[1:]
        else:
            geometry = left + mid + right

        # Ruwe afstand/tijd bijwerken.
        spur_m = 0.0
        for i in range(1, len(spur)):
            spur_m += haversine_m(spur[i - 1][0], spur[i - 1][1], spur[i][0], spur[i][1])
        # Trek het overgeslagen stuk snap→ahead van de basisroute af (dat vervangen we).
        skipped = haversine_m(snap[0], snap[1], resume[0], resume[1])
        distance_m = max(0.0, distance_m - skipped + spur_m)
        duration_s = max(60.0, duration_s - skipped / 4.0 + spur_m / 3.9)

    # Lichte dedupe (niet 12 m — dat kan de POI-aftakking platslaan).
    cleaned: list[list[float]] = []
    for point in geometry:
        if cleaned and haversine_m(cleaned[-1][0], cleaned[-1][1], point[0], point[1]) < 2.5:
            cleaned[-1] = [point[0], point[1]]
            continue
        cleaned.append([point[0], point[1]])
    geometry = cleaned
    for item in placements:
        geometry = _pin_nodes_on_geometry(
            geometry,
            [{"lat": item["lat"], "lng": item["lng"], "number": ""}],
        )
    return {
        **route,
        "geometry": geometry,
        "distance_m": distance_m,
        "duration_s": duration_s,
        "steps": steps,
    }


async def _build_knoop_route(
    start_lat: float,
    start_lng: float,
    chain: list[dict[str, Any]],
    *,
    close_loop: bool = True,
    end_lat: float | None = None,
    end_lng: float | None = None,
    poi_picks: list[Any] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    # Spine = exacte gebruikerskeuze (nummer + coördinaten blijven behouden).
    spine = _chain_spine([{**n, "lat": float(n["lat"]), "lng": float(n["lng"]), "number": str(n["number"])} for n in chain])
    network_nodes, trajects = await knoop_service.fetch_network_for_chain(spine)
    chain_network = knoop_service.infer_chain_network(spine, network_nodes)
    spine_geo = knoop_service.enrich_chain_geoids(
        spine, network_nodes, trajects=trajects, network=chain_network
    )
    # Officiële netwerkcoördinaten behouden voor routing (klik mag geoid niet “wegtrekken”).
    for index, node in enumerate(spine_geo):
        if index < len(spine):
            if spine[index].get("id"):
                node["id"] = spine[index]["id"]
            if spine[index].get("number") is not None:
                node["number"] = str(spine[index]["number"])
    by_geoid: dict[int, dict[str, Any]] = {
        int(node["geoid"]): node for node in network_nodes if node.get("geoid") is not None
    }
    for node in spine_geo:
        if node.get("geoid") is not None:
            official = by_geoid.get(int(node["geoid"]))
            if official:
                # Gebruik altijd officiële lat/lng voor dit geoid in de graaf.
                node["lat"] = float(official["lat"])
                node["lng"] = float(official["lng"])
                by_geoid[int(node["geoid"])] = {**official, **node, "lat": float(official["lat"]), "lng": float(official["lng"])}
            else:
                by_geoid[int(node["geoid"])] = node
    adj = knoop_service.build_adjacency(trajects)
    route_chain = knoop_service._display_chain_between_picks(spine_geo, by_geoid, adj)
    if len(route_chain) < len(spine_geo):
        route_chain = list(spine_geo)
    # Altijd langs de picks (spine) routen — niet elke via-knoop apart (dat hangt bij 30–50 km).
    must_visit = spine_geo

    # Eerst de zuivere knooppuntenroute; POI's daarna als lokale spur (geen A→POI→B).
    route = await _route_along_must_visit(
        start_lat,
        start_lng,
        must_visit,
        network_nodes,
        trajects,
        close_loop=close_loop,
        end_lat=end_lat,
        end_lng=end_lng,
    )
    if poi_picks:
        route = await _apply_poi_spurs(route, poi_picks)

    # Absolute garantie: gekozen knopen dicht bij de route vastzetten (geen verre spikes).
    route["geometry"] = _pin_nodes_on_geometry(route.get("geometry") or [], spine_geo, max_m=180)
    if not _route_covers_nodes(route.get("geometry") or [], spine_geo, max_m=50):
        route["geometry"] = _pin_nodes_on_geometry(
            route.get("geometry") or [], must_visit + spine_geo, max_m=180
        )

    geometry = route.get("geometry") or []
    # Zelf kiezen (open route): toon enkel de gekozen knooppunten, geen lange via-lijst.
    if not close_loop:
        display_chain = [dict(node) for node in spine_geo]
        display_chain = knoop_service.snap_chain_nodes_to_route_line(display_chain, geometry)
        return display_chain, route

    # Snelle weergave: netwerkvolgorde uit de graaf, zonder tweede WFS-ronde.
    display_chain = knoop_service.chain_for_display(
        route_chain,
        spine_geo,
        network_nodes=network_nodes,
        trajects=trajects,
    )
    display_chain = knoop_service.refresh_chain_coords(display_chain, network_nodes)
    display_chain = knoop_service.snap_chain_nodes_to_route_line(display_chain, geometry)
    return display_chain, route


async def _route_along_must_visit(
    start_lat: float,
    start_lng: float,
    must_visit: list[dict[str, Any]],
    network_nodes: list[dict[str, Any]],
    trajects: list[dict[str, Any]],
    *,
    close_loop: bool = True,
    end_lat: float | None = None,
    end_lng: float | None = None,
    poi_picks: list[Any] | None = None,
    waypoints_with_poi: list[tuple[float, float]] | None = None,
) -> dict[str, Any]:
    """Build route geometry along official knooppunten trajects; OSRM only to/from GPS."""
    if poi_picks and waypoints_with_poi:
        return await _route_through_waypoints(waypoints_with_poi, must_visit, trajects, network_nodes)

    by_edge, edge_length, adj = knoop_service.index_trajects(trajects)
    by_geoid: dict[int, dict[str, Any]] = {
        int(node["geoid"]): node for node in network_nodes if node.get("geoid") is not None
    }
    for node in must_visit:
        if node.get("geoid") is not None:
            by_geoid[int(node["geoid"])] = node

    geometries: list[list[list[float]]] = []
    distance_m = 0.0
    duration_s = 0.0
    steps: list[Any] = []
    visited_geoids: set[int] = set()

    def mark_visited(node: dict[str, Any]) -> None:
        geo = knoop_service._resolve_geoid(node, by_geoid)
        if geo is not None:
            visited_geoids.add(int(geo))

    async def add_bike_segment(a: tuple[float, float], b: tuple[float, float]) -> bool:
        """Fietsroute via OSRM — nooit vogelvlucht (geen rechte lijn door huizen)."""
        nonlocal distance_m, duration_s
        if haversine_m(a[0], a[1], b[0], b[1]) < 20:
            return True
        try:
            osrm = await routing.bike_route([a, b], retries=2)
            piece = list(osrm["geometry"])
            if not piece or len(piece) < 2:
                return False
            piece[0] = [a[0], a[1]]
            piece[-1] = [b[0], b[1]]
            geometries.append(piece)
            distance_m += float(osrm["distance_m"])
            duration_s += float(osrm["duration_s"])
            steps.extend(osrm.get("steps") or [])
            return True
        except Exception:
            return False

    def _append_network_segment(segment: list[list[float]], known_length: float) -> None:
        nonlocal distance_m, duration_s
        geometries.append(segment)
        if known_length > 0:
            distance_m += known_length
        else:
            for i in range(1, len(segment)):
                distance_m += haversine_m(
                    segment[i - 1][0], segment[i - 1][1], segment[i][0], segment[i][1]
                )
        duration_s += max(30.0, (known_length or 0) / 3.9)

    def _network_length(segment: list[list[float]], known_length: float) -> float:
        if known_length > 0:
            return known_length
        total = 0.0
        for i in range(1, len(segment)):
            total += haversine_m(segment[i - 1][0], segment[i - 1][1], segment[i][0], segment[i][1])
        return total

    def _network_is_reasonable(segment: list[list[float]], known_length: float, left: dict, right: dict) -> bool:
        """Vermijd bizarre omwegen via het knooppuntennet (typisch bij foute geoids/avoid)."""
        direct = haversine_m(float(left["lat"]), float(left["lng"]), float(right["lat"]), float(right["lng"]))
        length = _network_length(segment, known_length)
        if direct < 30:
            return length < 500
        # Meer dan 2.5× hemelsbreed of >8 km extra = onlogisch voor A→B.
        return length <= max(direct * 2.5, direct + 8000.0)

    async def add_pair_segment(left: dict[str, Any], right: dict[str, Any]) -> bool:
        """Segment tussen twee knoops: alleen officieel knooppuntennetwerk (geen vrije OSRM)."""
        if knoop_service._same_knoop(left, right):
            return True

        def network_candidate() -> tuple[list[list[float]] | None, float]:
            avoid = None if not close_loop else visited_geoids
            segment, known_length = knoop_service.geometry_between_nodes(
                left, right, by_edge, edge_length, adj, by_geoid
            )
            if segment and len(segment) >= 2 and _network_is_reasonable(segment, known_length, left, right):
                return segment, known_length
            segment, known_length = knoop_service.geometry_through_network(
                left, right, by_edge, edge_length, adj, by_geoid, avoid_geoids=avoid
            )
            if (not segment or len(segment) < 2) and avoid:
                segment, known_length = knoop_service.geometry_through_network(
                    left, right, by_edge, edge_length, adj, by_geoid, avoid_geoids=None
                )
            if segment and len(segment) >= 2 and _network_is_reasonable(segment, known_length, left, right):
                return segment, known_length
            # Laatste kans: netwerk zonder detour-filter (nog steeds officiële trajecten).
            if segment and len(segment) >= 2:
                return segment, known_length
            segment, known_length = knoop_service.geometry_between_nodes(
                left, right, by_edge, edge_length, adj, by_geoid
            )
            if segment and len(segment) >= 2:
                return segment, known_length
            return None, 0.0

        segment, known_length = network_candidate()
        if segment and len(segment) >= 2:
            _append_network_segment(segment, known_length)
            mark_visited(right)
            return True
        return False

    if must_visit:
        first = must_visit[0]
        if haversine_m(start_lat, start_lng, float(first["lat"]), float(first["lng"])) > 20:
            await add_bike_segment((start_lat, start_lng), (float(first["lat"]), float(first["lng"])))
        mark_visited(first)

    for index in range(len(must_visit) - 1):
        await add_pair_segment(must_visit[index], must_visit[index + 1])

    if must_visit:
        last = must_visit[-1]
        first = must_visit[0]
        if close_loop:
            if not knoop_service._same_knoop(first, last):
                await add_pair_segment(last, first)
            if haversine_m(start_lat, start_lng, float(first["lat"]), float(first["lng"])) > 20:
                await add_bike_segment(
                    (float(first["lat"]), float(first["lng"])),
                    (start_lat, start_lng),
                )
        elif end_lat is not None and end_lng is not None:
            if haversine_m(float(last["lat"]), float(last["lng"]), end_lat, end_lng) > 20:
                await add_bike_segment((float(last["lat"]), float(last["lng"])), (end_lat, end_lng))

    geometry = routing._merge_geometries(geometries)
    # Alleen knopen die dicht bij de route liggen vastpinnen — anders ontstaan V-spikes.
    geometry = _pin_nodes_on_geometry(geometry, must_visit, max_m=180)
    if must_visit and haversine_m(start_lat, start_lng, float(must_visit[0]["lat"]), float(must_visit[0]["lng"])) > 20:
        geometry = _pin_nodes_on_geometry(
            geometry, [{"lat": start_lat, "lng": start_lng, "number": ""}], max_m=180
        )
    if len(geometry) < 2 and len(must_visit) >= 2:
        # Nood: OSRM langs alle picks als waypoints.
        try:
            waypoints = [(float(n["lat"]), float(n["lng"])) for n in must_visit]
            stitched = await routing.bike_route_via_waypoints(waypoints)
            geometry = list(stitched.get("geometry") or [])
            if geometry:
                distance_m = float(stitched.get("distance_m") or distance_m)
                duration_s = float(stitched.get("duration_s") or duration_s)
                steps = list(stitched.get("steps") or steps)
        except Exception:
            pass
    if len(geometry) < 2:
        raise RuntimeError("Geen fietsroute gevonden langs de knooppunten.")
    return {
        "geometry": geometry,
        "distance_m": distance_m,
        "duration_s": max(60.0, duration_s),
        "steps": steps,
    }


async def _route_through_waypoints(
    waypoints: list[tuple[float, float]],
    nodes: list[dict[str, Any]],
    trajects: list[dict[str, Any]],
    network_nodes: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Bouw de rode lijn via opeenvolgende waypoints (POI-detours); knoopsegmenten via trajecten."""
    cleaned = routing._clean_waypoints(waypoints)
    if len(cleaned) < 2:
        raise ValueError("Een fietsroute heeft minstens twee punten nodig.")

    by_edge, edge_length, adj = knoop_service.index_trajects(trajects)
    by_geoid: dict[int, dict[str, Any]] = {
        int(node["geoid"]): node for node in (network_nodes or nodes) if node.get("geoid") is not None
    }
    for node in nodes:
        if node.get("geoid") is not None:
            by_geoid[int(node["geoid"])] = node

    node_at = [_nearest_node(lat, lng, nodes, max_m=120.0) for lat, lng in cleaned]

    geometries: list[list[list[float]]] = []
    distance_m = 0.0
    duration_s = 0.0
    steps: list[Any] = []

    for index in range(len(cleaned) - 1):
        a = cleaned[index]
        b = cleaned[index + 1]
        left = node_at[index]
        right = node_at[index + 1]
        segment: list[list[float]] | None = None
        known_length: float | None = None
        if (
            left
            and right
            and left.get("geoid") is not None
            and right.get("geoid") is not None
            and (knoop_service._same_knoop(left, {"number": left["number"], "lat": a[0], "lng": a[1]}) or haversine_m(left["lat"], left["lng"], a[0], a[1]) <= 120)
            and (knoop_service._same_knoop(right, {"number": right["number"], "lat": b[0], "lng": b[1]}) or haversine_m(right["lat"], right["lng"], b[0], b[1]) <= 120)
        ):
            segment, known_length = knoop_service.geometry_between_nodes(
                left, right, by_edge, edge_length, adj, by_geoid
            )

        if segment and len(segment) >= 2:
            piece = list(segment)
            if haversine_m(a[0], a[1], piece[0][0], piece[0][1]) > 25:
                piece = [[a[0], a[1]], *piece]
            else:
                piece[0] = [a[0], a[1]]
            if haversine_m(b[0], b[1], piece[-1][0], piece[-1][1]) > 25:
                piece = [*piece, [b[0], b[1]]]
            else:
                piece[-1] = [b[0], b[1]]
            geometries.append(piece)
            if known_length and known_length > 0:
                distance_m += known_length
            else:
                for i in range(1, len(piece)):
                    distance_m += haversine_m(piece[i - 1][0], piece[i - 1][1], piece[i][0], piece[i][1])
            duration_s += max(30.0, (known_length or 0) / 3.9)
            continue

        # Geen netwerktraject: OSRM tussen twee punten (start/eind of POI-detour).
        try:
            osrm = await routing.bike_route([a, b], retries=2)
            piece = list(osrm["geometry"])
            if not piece or len(piece) < 2:
                continue
            if not piece or haversine_m(a[0], a[1], piece[0][0], piece[0][1]) > 15:
                piece = [[a[0], a[1]], *piece]
            if haversine_m(b[0], b[1], piece[-1][0], piece[-1][1]) > 15:
                piece = [*piece, [b[0], b[1]]]
            # Forceer eindpunten exact op de gekozen knooppunten.
            piece[0] = [a[0], a[1]]
            piece[-1] = [b[0], b[1]]
            geometries.append(piece)
            distance_m += float(osrm["distance_m"])
            duration_s += float(osrm["duration_s"])
            steps.extend(osrm.get("steps") or [])
        except Exception:
            # Geen vogelvlucht — liever een gat dan een lijn door huizen.
            continue

    geometry = routing._merge_geometries(geometries)
    geometry = _pin_nodes_on_geometry(geometry, [n for n in node_at if n], max_m=180)
    # Pin ook de ruwe waypoints zelf (GPS-start / eind).
    for lat, lng in cleaned:
        geometry = _pin_nodes_on_geometry(geometry, [{"lat": lat, "lng": lng, "number": ""}])
    if len(geometry) < 2:
        raise RuntimeError("Geen fietsroute gevonden langs de knooppunten.")
    return {
        "geometry": geometry,
        "distance_m": distance_m,
        "duration_s": max(60.0, duration_s),
        "steps": steps,
    }


def _nearest_node(lat: float, lng: float, nodes: list[dict[str, Any]], max_m: float = 80.0) -> dict[str, Any] | None:
    best = None
    best_d = float("inf")
    for node in nodes:
        dist = haversine_m(lat, lng, float(node["lat"]), float(node["lng"]))
        if dist < best_d:
            best_d = dist
            best = node
    return best if best is not None and best_d <= max_m else None


def _pin_nodes_on_geometry(
    geometry: list[list[float]],
    nodes: list[dict[str, Any]],
    max_m: float = 180.0,
) -> list[list[float]]:
    """Zorg dat knooppunten op de lijn liggen — zonder verre V-spikes."""
    if not geometry or not nodes:
        return geometry
    result = list(geometry)
    for node in nodes:
        lat = float(node["lat"])
        lng = float(node["lng"])
        best_i = 0
        best_d = float("inf")
        for index, point in enumerate(result):
            dist = haversine_m(lat, lng, point[0], point[1])
            if dist < best_d:
                best_d = dist
                best_i = index
        if best_d <= 25:
            result[best_i] = [lat, lng]
            continue
        # Zoek beste segment om het knooppunt in te voegen.
        insert_at = best_i
        best_score = float("inf")
        for index in range(len(result) - 1):
            score = point_to_segment_m(
                lat, lng, result[index][0], result[index][1], result[index + 1][0], result[index + 1][1]
            )
            if score < best_score:
                best_score = score
                insert_at = index + 1
        # Ver weg van de route? Niet forceren — dat maakt onlogische pieken.
        if best_score > max_m:
            continue
        result.insert(insert_at, [lat, lng])
    return result


def _route_covers_nodes(geometry: list[list[float]], nodes: list[dict[str, Any]], max_m: float = 90.0) -> bool:
    if not nodes:
        return True
    if len(geometry) < 2:
        return False
    for node in nodes:
        lat = float(node["lat"])
        lng = float(node["lng"])
        best = min(haversine_m(lat, lng, point[0], point[1]) for point in geometry)
        if best > max_m:
            # Ook corridor-check tussen opeenvolgende punten.
            along = False
            for index in range(0, len(geometry) - 1, max(1, len(geometry) // 300)):
                nxt = min(len(geometry) - 1, index + max(1, len(geometry) // 300))
                if point_to_segment_m(lat, lng, geometry[index][0], geometry[index][1], geometry[nxt][0], geometry[nxt][1]) <= max_m:
                    along = True
                    break
            if not along:
                return False
    return True


def _chain_spine(chain: list[dict[str, Any]]) -> list[dict[str, Any]]:
    cleaned = knoop_service._dedupe_adjacent(chain)
    if len(cleaned) >= 2 and cleaned[0].get("id") == cleaned[-1].get("id"):
        return cleaned[:-1]
    return cleaned


def _search_radius(request: PlanRequest, start: Place, end: Place) -> int:
    if request.mode == "lus":
        return int(min(16000, max(5000, request.distance_km * 1000 / 2.2)))
    direct = haversine_m(start.lat, start.lng, end.lat, end.lng)
    return int(min(16000, max(5000, direct / 2 + 4000)))


def _merge(*groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    merged: list[dict[str, Any]] = []
    for group in groups:
        for poi in group:
            key = unique_key(poi["name"], poi["lat"], poi["lng"])
            if key in seen:
                continue
            seen.add(key)
            merged.append(poi)
    return merged


def _near_chain(pois: list[dict[str, Any]], chain: list[dict[str, Any]], notes: str = "") -> list[dict[str, Any]]:
    kept = []
    for poi in pois:
        wish = pois_service.matches_notes(poi, notes)
        limit = 2800 if wish else 1200
        if any(haversine_m(poi["lat"], poi["lng"], n["lat"], n["lng"]) < limit for n in chain):
            kept.append(poi)
    if notes.strip():
        wished = [poi for poi in pois if pois_service.matches_notes(poi, notes)]
        seen = {unique_key(p["name"], p["lat"], p["lng"]) for p in kept}
        for poi in wished:
            key = unique_key(poi["name"], poi["lat"], poi["lng"])
            if key not in seen:
                kept.append(poi)
                seen.add(key)
    return kept or pois


def _on_route_geometry(poi: dict[str, Any], geometry: list[list[float]], max_m: float = 650) -> bool:
    if not geometry or len(geometry) < 2:
        return False
    # Langere routes: dichter bemonsteren zodat stadsdoorsteken niet gemist worden.
    samples = min(160, max(48, len(geometry) // 8))
    step = max(1, len(geometry) // samples)
    for index in range(0, len(geometry) - 1, step):
        a = geometry[index]
        b = geometry[index + 1]
        if point_to_segment_m(poi["lat"], poi["lng"], a[0], a[1], b[0], b[1]) <= max_m:
            return True
    # Altijd ook het laatste segment checken.
    a = geometry[-2]
    b = geometry[-1]
    return point_to_segment_m(poi["lat"], poi["lng"], a[0], a[1], b[0], b[1]) <= max_m


def _geometry_length_km(geometry: list[list[float]]) -> float:
    if not geometry or len(geometry) < 2:
        return 0.0
    total = 0.0
    step = max(1, len(geometry) // 200)
    prev = geometry[0]
    for index in range(step, len(geometry), step):
        point = geometry[index]
        total += haversine_m(prev[0], prev[1], point[0], point[1])
        prev = point
    last = geometry[-1]
    if prev is not last:
        total += haversine_m(prev[0], prev[1], last[0], last[1])
    return total / 1000.0


def _sample_route_points(geometry: list[list[float]], count: int = 6) -> list[tuple[float, float]]:
    """Sample roughly evenly along the polyline (by distance), not only by vertex index."""
    if not geometry:
        return []
    points = [(float(point[0]), float(point[1])) for point in geometry if len(point) >= 2]
    if len(points) <= count:
        return points
    if count <= 1:
        return [points[0]]

    # Cumulative distances.
    dists = [0.0]
    for index in range(1, len(points)):
        dists.append(
            dists[-1] + haversine_m(points[index - 1][0], points[index - 1][1], points[index][0], points[index][1])
        )
    total = dists[-1] or 1.0
    targets = [total * i / (count - 1) for i in range(count)]
    sampled: list[tuple[float, float]] = []
    cursor = 0
    for target in targets:
        while cursor < len(dists) - 1 and dists[cursor] < target:
            cursor += 1
        sampled.append(points[cursor])
    # Unieke opeenvolgende punten behouden.
    unique: list[tuple[float, float]] = []
    for point in sampled:
        if not unique or haversine_m(unique[-1][0], unique[-1][1], point[0], point[1]) > 80:
            unique.append(point)
    if points[-1] not in unique:
        unique.append(points[-1])
    return unique[:count]


def _route_midpoint(geometry: list[list[float]]) -> tuple[float, float] | None:
    if not geometry:
        return None
    mid = geometry[len(geometry) // 2]
    return float(mid[0]), float(mid[1])


def _notes_want_halfway(notes: str) -> bool:
    text = (notes or "").lower()
    return any(key in text for key in ("halverwege", "halfway", " halve ", "midden", "tussendoor", " onderweg"))


def _wish_suggestion_target(route_km: float, wish_interests: list[str]) -> int:
    """Meer suggesties op langere routes doorheen meerdere steden."""
    by_distance = int(round(route_km / 4.0))  # ~1 per 4 km
    by_theme = max(8, len(wish_interests or []) * 6)
    return max(12, min(36, max(by_distance, by_theme)))


async def _fast_wish_pois_for_manual(
    notes: str,
    chain: list[dict[str, Any]],
    geometry: list[list[float]],
    profile_interests: list[str] | None = None,
) -> tuple[list[dict[str, Any]], str]:
    """Suggesties voor knooproutes: snelle bronnen eerst, daarna profiel; met cache."""
    if not geometry or len(geometry) < 2:
        return [], ""

    cache_key = _wish_poi_cache_key(notes, profile_interests, geometry, chain)
    cached = _WISH_POI_CACHE.get(cache_key)
    if cached and time.monotonic() - cached[0] <= _WISH_POI_TTL_S:
        pois, summary = cached[1]
        return [dict(p) for p in pois], summary

    profile_only = pois_service._unique_interests(profile_interests or [])
    wish_interests = pois_service.wish_interests_for_notes(notes, profile_only) or profile_only or [
        "geschiedenis"
    ]
    wish_set = set(wish_interests)
    sample_points = _sample_route_points(geometry, 3)
    for node in (chain or [])[:2]:
        try:
            sample_points.append((float(node["lat"]), float(node["lng"])))
        except (KeyError, TypeError, ValueError):
            continue
    want_horeca = pois_service.notes_want_horeca(notes, wish_interests)
    start_pt = sample_points[0]
    extra_pt = sample_points[-1] if len(sample_points) > 1 else None
    cafe_focus = want_horeca and bool((notes or "").strip())

    fast_tasks: list[asyncio.Task] = []
    slow_tasks: list[asyncio.Task] = []
    if want_horeca:
        fast_tasks.append(
            asyncio.create_task(
                _optional(pois_service.fetch_cafes_fast(sample_points, 7500), [], 3.5)
            )
        )
        fast_tasks.append(
            asyncio.create_task(
                _optional(
                    pois_service.fetch_horeca_photon_along_points(
                        sample_points[:2],
                        max_points=2,
                        per_point=8,
                        queries=("café", "taverne"),
                        osm_tags=("amenity:cafe", "amenity:pub"),
                    ),
                    [],
                    3.0,
                )
            )
        )
    profile_themes = [item for item in profile_only if item != "horeca"] or (
        [] if cafe_focus else [item for item in wish_interests if item != "horeca"]
    )
    if not profile_themes and not cafe_focus:
        profile_themes = list(wish_interests)
    if profile_themes:
        slow_tasks.append(
            asyncio.create_task(
                _optional(
                    pois_service.fetch_pois(
                        start_pt[0],
                        start_pt[1],
                        6500,
                        profile_themes,
                        extra_pt,
                    ),
                    [],
                    3.5,
                )
            )
        )
        slow_tasks.append(
            asyncio.create_task(
                _optional(
                    wikipedia.places_for_route(start_pt[0], start_pt[1], 6000, extra_pt),
                    [],
                    2.5,
                )
            )
        )

    groups: list[list[dict[str, Any]]] = []

    async def _drain(tasks: list[asyncio.Task], timeout: float) -> None:
        if not tasks:
            return
        done, pending = await asyncio.wait(set(tasks), timeout=timeout)
        for task in pending:
            task.cancel()
        for task in done:
            try:
                groups.append(task.result() or [])
            except Exception:
                groups.append([])

    # Horeca/wens én profiel parallel — kort genoeg om onder de API-timeout te blijven.
    await asyncio.gather(
        _drain(fast_tasks, 4.0),
        _drain(slow_tasks, 4.0),
    )

    merged: list[dict[str, Any]] = []
    for group in groups:
        merged = _merge(merged, group or [])

    # Café-wens zonder horeca-hits: één snelle Photon-poging (geen tweede lange golf).
    if cafe_focus:
        has_cafe = any(
            "cafe" in f"{p.get('kind', '')} {p.get('kind_label', '')} {p.get('name', '')}".lower()
            or "café" in f"{p.get('name', '')}".lower()
            or "taverne" in f"{p.get('name', '')}".lower()
            or p.get("interest") == "horeca"
            for p in merged
            if "kerk" not in f"{p.get('name', '')} {p.get('kind', '')}".lower()
        )
        if not has_cafe:
            cafe_retry = await _optional(
                pois_service.fetch_horeca_photon_along_points(
                    sample_points[:3],
                    max_points=3,
                    per_point=10,
                    queries=("café", "cafe", "taverne", "pub"),
                    osm_tags=("amenity:cafe", "amenity:pub", "amenity:bar"),
                ),
                [],
                3.0,
            )
            for poi in cafe_retry or []:
                poi["interest"] = "horeca"
                poi["hint"] = "past bij je wens"
            merged = _merge(cafe_retry or [], merged)

    if not merged:
        wiki_extra = await _optional(
            wikipedia.places_for_route(start_pt[0], start_pt[1], 8000, extra_pt),
            [],
            2.0,
        )
        merged = _merge(merged, wiki_extra or [])
    elif cafe_focus and profile_themes and len(merged) < 3:
        has_profile_hit = any(
            (p.get("hint") == "uit je profiel") or (p.get("interest") in set(profile_themes))
            for p in merged
        )
        if not has_profile_hit:
            wiki_extra = await _optional(
                wikipedia.places_for_route(start_pt[0], start_pt[1], 8000, extra_pt),
                [],
                1.5,
            )
            merged = _merge(merged, wiki_extra or [])

    for poi in merged:
        inferred = pois_service.infer_interest(poi, wish_interests)
        if inferred:
            poi["interest"] = inferred
        elif not poi.get("interest"):
            poi["interest"] = profile_only[0] if profile_only else wish_interests[0]
        if cafe_focus and pois_service.matches_notes(poi, notes):
            poi["interest"] = poi.get("interest") or "horeca"
            poi.pop("hint", None)
        elif profile_only and (
            poi.get("interest") in set(profile_only)
            or (poi.get("source") or "").lower().startswith("wikipedia")
            or not pois_service.matches_notes(poi, notes)
        ):
            if not pois_service.matches_notes(poi, notes):
                if poi.get("interest") not in set(profile_only) and profile_only:
                    poi["interest"] = profile_only[0]
                poi["hint"] = poi.get("hint") or "uit je profiel"

    kept: list[dict[str, Any]] = []
    seen: set[str] = set()
    for poi in merged:
        interest = poi.get("interest")
        if interest not in wish_set and not pois_service.matches_notes(poi, notes):
            if profile_only:
                poi["interest"] = profile_only[0]
                poi["hint"] = poi.get("hint") or "uit je profiel"
                interest = poi["interest"]
            else:
                continue
        key = str(poi.get("id") or "") or unique_key(poi.get("name") or "", poi["lat"], poi["lng"])
        if key in seen:
            continue
        if not (
            _on_route_geometry(poi, geometry, max_m=12000)
            or any(haversine_m(poi["lat"], poi["lng"], n["lat"], n["lng"]) < 8000 for n in chain)
        ):
            continue
        seen.add(key)
        tagged = dict(poi)
        tagged["on_route"] = _on_route_geometry(poi, geometry, max_m=3500)
        kept.append(tagged)

    if not kept and merged:
        for poi in merged:
            key = str(poi.get("id") or "") or unique_key(poi.get("name") or "", poi["lat"], poi["lng"])
            if key in seen:
                continue
            seen.add(key)
            tagged = dict(poi)
            tagged["on_route"] = _on_route_geometry(poi, geometry, max_m=3500)
            tagged["hint"] = poi.get("hint") or "in de buurt"
            kept.append(tagged)

    def _looks_like_horeca(poi: dict[str, Any]) -> bool:
        blob = f"{poi.get('kind', '')} {poi.get('kind_label', '')} {poi.get('name', '')}".lower()
        if any(bad in blob for bad in ("kerk", "church", "begraaf", "kapel", "museum", "station")):
            return False
        if any(
            token in blob
            for token in ("cafe", "café", "taverne", "pub", "bar", "herberg", "bistro", "restaurant")
        ):
            return True
        return poi.get("interest") == "horeca"

    wish_hits: list[dict[str, Any]] = []
    profile_hits: list[dict[str, Any]] = []
    other_hits: list[dict[str, Any]] = []
    for poi in kept:
        item = dict(poi)
        if cafe_focus and _looks_like_horeca(item):
            item["interest"] = "horeca"
            item["hint"] = "past bij je wens"
            item["source"] = "wish"
            wish_hits.append(item)
        elif (
            (notes or "").strip()
            and pois_service.matches_notes(item, notes)
            and not any(
                bad in f"{item.get('kind', '')} {item.get('kind_label', '')} {item.get('name', '')}".lower()
                for bad in ("kerk", "church", "begraaf", "kapel", "museum", "station")
            )
        ):
            item["hint"] = item.get("hint") or "past bij je wens"
            item["source"] = "wish"
            wish_hits.append(item)
        elif item.get("hint") == "uit je profiel" or (
            profile_only and item.get("interest") in set(profile_only) and not _looks_like_horeca(item)
        ):
            if profile_only and item.get("interest") not in set(profile_only):
                item["interest"] = profile_only[0]
            item["hint"] = item.get("hint") or "uit je profiel"
            item["source"] = "profile"
            profile_hits.append(item)
        else:
            if (notes or "").strip():
                item["source"] = item.get("source") or "wish"
                item["hint"] = item.get("hint") or "past bij je wens"
            else:
                item["source"] = "profile"
                item["hint"] = item.get("hint") or "uit je profiel"
            other_hits.append(item)
    ordered = [*wish_hits, *profile_hits, *other_hits]
    diverse = pois_service.pick_diverse_pois(
        ordered,
        wish_interests,
        wanted=min(16, max(10, len(wish_interests) * 3)),
        min_distance_m=180 if cafe_focus else 350,
    )
    if not diverse and ordered:
        diverse = ordered[:16]
    if not diverse and kept:
        diverse = kept[:16]
    labels = [
        suggestion_service.INTEREST_LABELS.get(item, item)
        for item in (profile_only or wish_interests)[:4]
    ]
    summary = ""
    if cafe_focus and (wish_hits and profile_hits):
        summary = "Plekken langs je route op basis van je extra wens én je profiel."
    elif cafe_focus and labels:
        summary = "Plekken langs je route op basis van je extra wens én je profiel."
    elif cafe_focus:
        summary = "Cafés, tavernes en profielsuggesties langs je knooppunten."
    elif labels and not (notes or "").strip():
        summary = f"Plekken langs je route op basis van je profiel: {', '.join(labels)}."
    elif labels:
        summary = "Plekken langs je route op basis van je extra wens én je profiel."
    if cafe_focus and profile_hits and wish_hits:
        summary = "Plekken langs je route op basis van je extra wens én je profiel."

    _WISH_POI_CACHE[cache_key] = (time.monotonic(), (list(diverse), summary))
    if len(_WISH_POI_CACHE) > 80:
        oldest = sorted(_WISH_POI_CACHE.items(), key=lambda item: item[1][0])[:20]
        for key, _ in oldest:
            _WISH_POI_CACHE.pop(key, None)
    return diverse, summary


async def _wish_pois_for_geometry(
    notes: str,
    candidates: list[dict[str, Any]],
    ranked: list[dict[str, Any]],
    chain: list[dict[str, Any]],
    geometry: list[list[float]],
    profile_interests: list[str] | None = None,
) -> tuple[list[dict[str, Any]], str]:
    if not geometry:
        return [], ""
    if not notes.strip() and not (profile_interests or []):
        return [], ""
    note_interests = pois_service.interests_from_notes(notes)
    profile_only = pois_service._unique_interests(profile_interests or [])
    wish_interests = pois_service.wish_interests_for_notes(notes, profile_only)
    if not wish_interests:
        return [], ""
    wish_set = set(wish_interests)
    route_km = _geometry_length_km(geometry)
    target = _wish_suggestion_target(route_km, wish_interests)
    route_candidates = _merge(candidates, ranked)
    want_horeca = pois_service.notes_want_horeca(notes, wish_interests)

    # Langere routes: spaarzaam bemonsteren + ruime straal (weinig Overpass-calls).
    sample_count = max(8, min(14, int(round(route_km / 10.0)) or 8))
    sample_points = _sample_route_points(geometry, sample_count)
    # Een handvol knooppunten meenemen (dorpen), niet allemaal — dat maakt Overpass te traag.
    for node in (chain or [])[:: max(1, len(chain or []) // 6 or 1)][:8]:
        try:
            sample_points.append((float(node["lat"]), float(node["lng"])))
        except (KeyError, TypeError, ValueError):
            continue
    if _notes_want_halfway(notes):
        midpoint = _route_midpoint(geometry)
        if midpoint:
            sample_points.append(midpoint)
    corridor_m = 8000 if route_km >= 80 else (6500 if route_km >= 40 else 4500)
    corridor_keep_m = 7000 if route_km >= 80 else (5000 if route_km >= 40 else 2800)

    # Extra wens (bv. café) en profiel-thema's apart ophalen zodat profiel niet verdwijnt.
    async def _fetch_themes(themes: list[str]) -> list[dict[str, Any]]:
        cleaned = [item for item in themes if item]
        if not cleaned:
            return []
        return await _optional(
            pois_service.fetch_pois_along_points(
                sample_points,
                corridor_m,
                cleaned,
                max_points=min(12, len(sample_points)),
            ),
            [],
            18,
        )

    horeca_task = None
    if want_horeca:
        async def _horeca_path() -> list[dict[str, Any]]:
            try:
                horeca = await pois_service.fetch_horeca_along_points(
                    sample_points,
                    corridor_m,
                    max_points=8,
                    chunk_size=4,
                )
            except Exception:
                horeca = []
            if len(horeca) < 5:
                try:
                    nomi = await pois_service.fetch_horeca_nominatim_along_points(
                        sample_points, max_points=6, per_point=8
                    )
                    horeca = _merge(horeca, nomi)
                except Exception:
                    pass
            return horeca

        horeca_task = asyncio.create_task(_horeca_path())

    note_themes = [item for item in note_interests if not (want_horeca and item == "horeca")]
    profile_themes = [item for item in profile_only if item]
    note_task = asyncio.create_task(_fetch_themes(note_themes))
    profile_task = asyncio.create_task(_fetch_themes(profile_themes))

    horeca = await horeca_task if horeca_task else []
    note_pois = await note_task
    profile_pois = await profile_task
    # Overpass valt vaak stil: Wikipedia + Nominatim als fallback voor profiel-thema's.
    profile_hit = {poi.get("interest") for poi in profile_pois}
    if profile_themes and len(profile_hit.intersection(profile_themes)) < min(2, len(profile_themes)):
        start_pt = sample_points[0]
        end_pt = sample_points[-1] if len(sample_points) > 1 else None
        wiki = await _optional(
            wikipedia.places_for_route(
                start_pt[0],
                start_pt[1],
                min(int(corridor_m), 9000),
                end_pt,
            ),
            [],
            12,
        )
        for poi in wiki:
            name = (poi.get("name") or "").lower()
            if any(key in name for key in ("park", "bos", "duin", "natuur", "heide", "meer ")):
                interest = "natuur" if "natuur" in profile_themes else profile_themes[0]
            elif any(key in name for key in ("kerk", "molen", "kapel", "basiliek")):
                interest = (
                    "architectuur"
                    if "architectuur" in profile_themes
                    else ("geschiedenis" if "geschiedenis" in profile_themes else profile_themes[0])
                )
            elif "geschiedenis" in profile_themes:
                interest = "geschiedenis"
            else:
                interest = profile_themes[0]
            poi["interest"] = interest
            if not poi.get("hint"):
                poi["hint"] = "uit je profiel"
        nomi = await _optional(
            pois_service.fetch_theme_nominatim_along_points(
                sample_points,
                profile_themes,
                max_points=4,
                per_point=4,
            ),
            [],
            14,
        )
        for poi in nomi:
            if not poi.get("hint"):
                poi["hint"] = "uit je profiel"
        profile_pois = _merge(profile_pois, wiki, nomi)
    if horeca:
        route_candidates = _merge(route_candidates, horeca)
    route_candidates = _merge(route_candidates, note_pois, profile_pois)

    # Houd plekken bij de route-corridor (niet enkel dicht bij een knooppunt).
    near = []
    seen_near: set[str] = set()
    for poi in route_candidates:
        pid = str(poi.get("id") or "")
        key = pid or unique_key(poi.get("name") or "", poi["lat"], poi["lng"])
        if key in seen_near:
            continue
        wish = pois_service.matches_notes(poi, notes) or (
            bool(wish_set) and poi.get("interest") in wish_set
        )
        if not wish:
            continue
        near_knoop = any(
            haversine_m(poi["lat"], poi["lng"], n["lat"], n["lng"]) < (3500 if wish else 1400)
            for n in chain
        ) if chain else False
        near_line = _on_route_geometry(poi, geometry, max_m=corridor_keep_m)
        if near_line or near_knoop or pois_service.matches_notes(poi, notes):
            near.append(poi)
            seen_near.add(key)
    if not near:
        near = _near_chain(route_candidates, chain, notes) if route_candidates else []
    # Laatste redmiddel: horeca-kandidaten dichter bij samplepunten houden, niet wegfilteren.
    if want_horeca and route_candidates:
        have = {str(p.get("id")) for p in near if p.get("id")}
        for poi in route_candidates:
            if poi.get("interest") != "horeca":
                continue
            pid = str(poi.get("id") or "")
            if pid in have:
                continue
            if _on_route_geometry(poi, geometry, max_m=max(corridor_keep_m, 9000)) or any(
                haversine_m(poi["lat"], poi["lng"], plat, plng) <= 9000 for plat, plng in sample_points
            ):
                near.append(poi)
                have.add(pid)

    result: list[dict[str, Any]] = []
    seen: set[str] = set()

    def fits_wish(poi: dict[str, Any]) -> bool:
        return pois_service.matches_notes(poi, notes) or (
            bool(wish_set) and poi.get("interest") in wish_set
        )

    ordered = sorted(
        near,
        key=lambda poi: (
            0 if pois_service.matches_notes(poi, notes) else 1,
            0 if (poi.get("interest") in set(profile_only)) else 1,
            0 if _on_route_geometry(poi, geometry) else 1,
            poi.get("name") or "",
        ),
    )
    for poi in ordered:
        if not fits_wish(poi):
            continue
        pid = str(poi.get("id") or "")
        if not pid or pid in seen:
            continue
        seen.add(pid)
        tagged = dict(poi)
        tagged["on_route"] = _on_route_geometry(poi, geometry, max_m=corridor_keep_m)
        if (
            not pois_service.matches_notes(poi, notes)
            and poi.get("interest") in set(profile_only)
            and not tagged.get("hint")
        ):
            tagged["hint"] = "uit je profiel"
        result.append(tagged)

    # Op lange routes iets dichter bij elkaar toegestaan, anders blijft de lijst te kort.
    min_gap = 400 if route_km >= 50 else 550
    diverse = pois_service.pick_diverse_pois(
        result,
        wish_interests or list(wish_set) or ["geschiedenis"],
        wanted=target,
        min_distance_m=min_gap,
    )
    for poi in diverse:
        poi["on_route"] = _on_route_geometry(poi, geometry, max_m=corridor_keep_m)
    diverse.sort(key=lambda item: (0 if item.get("on_route") else 1, (item.get("name") or "").lower()))

    wish_summary = ""
    if has_ai():
        pool = _merge(
            diverse,
            [
                dict(poi, on_route=_on_route_geometry(poi, geometry, max_m=corridor_keep_m))
                for poi in ordered
                if fits_wish(poi)
            ],
        )
        if len(pool) < max(14, target):
            pool = _merge(
                pool,
                [
                    dict(poi, on_route=_on_route_geometry(poi, geometry, max_m=corridor_keep_m))
                    for poi in near[:160]
                    if poi.get("interest") in wish_set or fits_wish(poi)
                ],
            )
        for poi in pool:
            poi["route_progress"] = _route_progress(poi, geometry)
            poi["on_route"] = bool(poi.get("on_route")) or _on_route_geometry(
                poi, geometry, max_m=corridor_keep_m
            )
        for poi in result:
            poi["route_progress"] = _route_progress(poi, geometry)
            poi["on_route"] = bool(poi.get("on_route")) or _on_route_geometry(
                poi, geometry, max_m=corridor_keep_m
            )
        ai_rank = await _optional(
            rank_wish_poi_suggestions(
                notes,
                pool,
                profile_only,
                wish_interests,
                target_count=target,
                route_km=route_km,
            ),
            None,
            8,
        )
        if ai_rank:
            wish_summary = str(ai_rank.get("summary") or "").strip()
            pick_ids = ai_rank.get("pick_ids") or []
            hints = ai_rank.get("hints") or {}
            # Alleen AI-picks als start; daarna aanvullen tot target, gespreid over de route.
            seeded = _ai_wish_seed(pool, pick_ids, hints)
            diverse = _spread_fill_wish_pois(seeded, result or pool, target, geometry)
            for poi in diverse:
                poi["on_route"] = _on_route_geometry(poi, geometry, max_m=corridor_keep_m)
            diverse.sort(
                key=lambda item: (
                    item.get("route_progress") if item.get("route_progress") is not None else 1.5,
                    (item.get("name") or "").lower(),
                )
            )
        elif result:
            # AI gaf niets bruikbaars terug: toon OSM-resultaten toch.
            diverse = _spread_fill_wish_pois(diverse, result, target, geometry)
    elif len(diverse) < target and result:
        diverse = _spread_fill_wish_pois(diverse, result, target, geometry)
    # Altijd minstens één plek per profiel-/wens-thema behouden als die bestaat.
    diverse = _ensure_interest_coverage(
        diverse,
        result or near or route_candidates,
        wish_interests,
        note_interests=note_interests,
        profile_interests=profile_only,
        target=target,
    )
    # Laatste redmiddel: als filtering alles weggooide, toon toch gevonden horeca/wens-kandidaten.
    if not diverse and near:
        diverse = pois_service.pick_diverse_pois(
            [poi for poi in near if fits_wish(poi) or (want_horeca and poi.get("interest") == "horeca")],
            wish_interests or ["horeca"],
            wanted=max(6, target),
            min_distance_m=300,
        )
        for poi in diverse:
            poi["on_route"] = _on_route_geometry(poi, geometry, max_m=corridor_keep_m)
    if not diverse and route_candidates and want_horeca:
        diverse = pois_service.pick_diverse_pois(
            [poi for poi in route_candidates if poi.get("interest") == "horeca"],
            ["horeca"],
            wanted=max(6, target),
            min_distance_m=250,
        )
        for poi in diverse:
            poi["on_route"] = _on_route_geometry(poi, geometry, max_m=max(corridor_keep_m, 9000))
    return diverse, wish_summary


def _ensure_interest_coverage(
    selected: list[dict[str, Any]],
    pool: list[dict[str, Any]],
    themes: list[str],
    *,
    note_interests: list[str] | None = None,
    profile_interests: list[str] | None = None,
    target: int = 12,
) -> list[dict[str, Any]]:
    """Zorg dat elk thema (wens + profiel) minstens één plek krijgt als beschikbaar."""
    out = [dict(poi) for poi in selected]
    have = {str(poi.get("id")) for poi in out if poi.get("id")}
    present = {poi.get("interest") for poi in out}
    note_set = set(note_interests or [])
    profile_set = set(profile_interests or [])
    for interest in pois_service._unique_interests(themes):
        if interest in present:
            continue
        for poi in pool:
            if poi.get("interest") != interest:
                continue
            pid = str(poi.get("id") or "")
            if not pid or pid in have:
                continue
            tagged = dict(poi)
            if interest in profile_set and interest not in note_set and not tagged.get("hint"):
                tagged["hint"] = "uit je profiel"
            elif interest in note_set and not tagged.get("hint"):
                tagged["hint"] = "past bij je wens"
            out.append(tagged)
            have.add(pid)
            present.add(interest)
            break
    if len(out) <= max(target, len(themes) + 2):
        return out
    # Behoud thema-dekking, trim overschot via diverse pick.
    return pois_service.pick_diverse_pois(
        out,
        themes or ["geschiedenis"],
        wanted=max(target, len(themes) + 2),
        min_distance_m=300,
    )


def _route_progress(poi: dict[str, Any], geometry: list[list[float]]) -> float | None:
    """Ruwe positie langs de route (0–1) via dichtstbijzijnde vertex."""
    if not geometry or poi.get("lat") is None or poi.get("lng") is None:
        return None
    best_i = 0
    best_d = float("inf")
    step = max(1, len(geometry) // 120)
    for index in range(0, len(geometry), step):
        point = geometry[index]
        dist = haversine_m(poi["lat"], poi["lng"], point[0], point[1])
        if dist < best_d:
            best_d = dist
            best_i = index
    return round(best_i / max(1, len(geometry) - 1), 3)


def _ai_wish_seed(
    pois: list[dict[str, Any]],
    pick_ids: list[str],
    hints: dict[str, str],
) -> list[dict[str, Any]]:
    """Alleen de door AI gekozen ids, in AI-volgorde."""
    by_id = {str(poi.get("id")): poi for poi in pois if poi.get("id")}
    ordered: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw_id in pick_ids:
        pid = str(raw_id)
        poi = by_id.get(pid)
        if not poi or pid in seen:
            continue
        tagged = dict(poi)
        hint = hints.get(pid)
        if hint:
            tagged["hint"] = hint
        ordered.append(tagged)
        seen.add(pid)
    return ordered


def _spread_fill_wish_pois(
    selected: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
    target: int,
    geometry: list[list[float]],
) -> list[dict[str, Any]]:
    """Vul aan tot target, zo gespreid mogelijk langs de route."""
    out = [dict(poi) for poi in selected]
    have = {str(poi.get("id")) for poi in out if poi.get("id")}
    remaining = [
        dict(poi)
        for poi in candidates
        if poi.get("id") and str(poi.get("id")) not in have
    ]
    while len(out) < target and remaining:
        sel_prog = [
            p.get("route_progress")
            if p.get("route_progress") is not None
            else _route_progress(p, geometry)
            for p in out
        ]
        sel_prog = [p for p in sel_prog if p is not None]
        best = None
        best_score = -1.0
        for poi in remaining:
            prog = poi.get("route_progress")
            if prog is None:
                prog = _route_progress(poi, geometry)
                poi["route_progress"] = prog
            if prog is None:
                score = 0.05
            elif not sel_prog:
                score = 1.0
            else:
                score = min(abs(float(prog) - float(s)) for s in sel_prog)
            if poi.get("on_route") or _on_route_geometry(poi, geometry):
                score += 0.02
            if score > best_score:
                best_score = score
                best = poi
        if not best:
            break
        out.append(best)
        have.add(str(best.get("id")))
        remaining = [poi for poi in remaining if str(poi.get("id")) not in have]
    return out[:target]


def _apply_ai_wish_pick_order(
    pois: list[dict[str, Any]],
    pick_ids: list[str],
    hints: dict[str, str],
) -> list[dict[str, Any]]:
    """Backwards-compatible: AI-volgorde, daarna rest van de pool."""
    ordered = _ai_wish_seed(pois, pick_ids, hints)
    seen = {str(poi.get("id")) for poi in ordered if poi.get("id")}
    for poi in pois:
        pid = str(poi.get("id") or "")
        if not pid or pid in seen:
            continue
        ordered.append(dict(poi))
        seen.add(pid)
    return ordered


def _merge_wish_into_stops(
    stops: list[dict[str, Any]],
    wish_pois: list[dict[str, Any]],
    request: PlanRequest,
    geometry: list[list[float]],
) -> None:
    by_id = {str(s["id"]): s for s in stops}
    for poi in wish_pois:
        pid = str(poi.get("id") or "")
        if not pid:
            continue
        on_route = bool(poi.get("on_route")) or _on_route_geometry(poi, geometry)
        if pid in by_id:
            by_id[pid]["matches_wish"] = True
            by_id[pid]["on_route"] = on_route
            if poi.get("hint"):
                by_id[pid]["hint"] = poi.get("hint")
            wish_src = poi.get("source") if poi.get("source") in {"wish", "profile"} else None
            if wish_src:
                by_id[pid]["wish_source"] = wish_src
            continue
        wiki = poi.get("wiki") or {}
        scripts = fallback_scripts(poi, wiki, request.explanation_level)
        wish_src = poi.get("source") if poi.get("source") in {"wish", "profile"} else None
        if not wish_src:
            if poi.get("hint") == "uit je profiel":
                wish_src = "profile"
            elif poi.get("hint") == "past bij je wens" or (
                (request.notes or "").strip() and pois_service.matches_notes(poi, request.notes)
            ):
                wish_src = "wish"
            else:
                wish_src = "profile"
        stops.append(
            {
                "id": pid,
                "name": poi["name"],
                "lat": poi["lat"],
                "lng": poi["lng"],
                "kind": poi.get("kind_label") or poi.get("kind") or "plek",
                "interest": poi["interest"],
                "source": "OpenStreetMap" if wish_src else (poi.get("source") or "OpenStreetMap"),
                "summary": poi.get("summary") or scripts["summary"],
                "approaching": scripts["approaching"],
                "arrived": scripts["arrived"],
                "why": scripts["why"],
                "wikipedia_url": wiki.get("url") or None,
                "image_url": wiki.get("image") or None,
                "wikipedia": poi.get("wikipedia"),
                "wikidata": poi.get("wikidata"),
                "description": poi.get("description") or "",
                "kind_label": poi.get("kind_label"),
                "matches_wish": True,
                "on_route": on_route,
                "hint": poi.get("hint")
                or ("past bij je wens" if wish_src == "wish" else "uit je profiel"),
                "wish_source": wish_src,
            }
        )
    for stop in stops:
        if pois_service.matches_notes(stop, request.notes):
            stop["matches_wish"] = True
            stop["on_route"] = _on_route_geometry(stop, geometry)


def _along_geometry(stops: list[dict[str, Any]], geometry: list[list[float]]) -> list[dict[str, Any]]:
    if not geometry:
        return stops

    def progress(poi: dict[str, Any]) -> float:
        best = 0
        best_d = 10**12
        for index, point in enumerate(geometry[::8]):
            dist = haversine_m(poi["lat"], poi["lng"], point[0], point[1])
            if dist < best_d:
                best_d = dist
                best = index
        return best

    return sorted(stops, key=progress)


def _rank(candidates: list[dict[str, Any]], start: Place, end: Place, request: PlanRequest) -> list[dict[str, Any]]:
    scored = []
    for poi in candidates:
        score = 0.0
        if poi.get("interest") in request.interests:
            score += 4
        if poi.get("wikipedia") or poi.get("wikidata"):
            score += 3
        if request.notes and pois_service.matches_notes(poi, request.notes):
            score += 10
        dist = haversine_m(start.lat, start.lng, poi["lat"], poi["lng"])
        if request.mode == "lus":
            target = request.distance_km * 1000 / 3
            score -= abs(dist - target) / 4000
            if dist < 800:
                score -= 6
        else:
            detour = point_to_segment_m(poi["lat"], poi["lng"], start.lat, start.lng, end.lat, end.lng)
            score -= detour / 2500
        scored.append((score, poi))
    scored.sort(key=lambda item: item[0], reverse=True)
    return [poi for _, poi in scored]


def _pick(ranked: list[dict[str, Any]], ai_choice: dict[str, Any] | None, request: PlanRequest) -> list[dict[str, Any]]:
    interests = list(dict.fromkeys(request.interests or ["geschiedenis"]))
    wanted = min(16, max(len(interests) + 2, len(interests) + (4 if request.notes.strip() else 2)))
    by_id = {poi["id"]: poi for poi in ranked}
    chosen: list[dict[str, Any]] = []
    chosen_ids: set[str] = set()
    if request.notes.strip():
        for poi in ranked:
            if not pois_service.matches_notes(poi, request.notes):
                continue
            if poi["id"] in chosen_ids:
                continue
            chosen.append(poi)
            chosen_ids.add(poi["id"])
            if len(chosen) >= min(8, wanted):
                break
    if ai_choice and ai_choice.get("stop_ids"):
        for stop_id in ai_choice["stop_ids"]:
            if stop_id in by_id and stop_id not in chosen_ids:
                chosen.append(by_id[stop_id])
                chosen_ids.add(stop_id)
    if not chosen:
        chosen = pois_service.pick_diverse_pois(ranked, interests, wanted=wanted)
    else:
        present = {poi.get("interest") for poi in chosen}
        for interest in interests:
            if interest in present:
                continue
            for poi in ranked:
                if poi.get("interest") != interest or poi["id"] in chosen_ids:
                    continue
                chosen.append(poi)
                chosen_ids.add(poi["id"])
                present.add(interest)
                break
        if len(chosen) < wanted:
            for poi in pois_service.pick_diverse_pois(ranked, interests, wanted=wanted):
                if poi["id"] in chosen_ids:
                    continue
                chosen.append(poi)
                chosen_ids.add(poi["id"])
                if len(chosen) >= wanted:
                    break
    return chosen[:wanted]


def _spread(ranked: list[dict[str, Any]], wanted: int) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    for poi in ranked:
        if any(haversine_m(poi["lat"], poi["lng"], other["lat"], other["lng"]) < 800 for other in selected):
            continue
        selected.append(poi)
        if len(selected) >= wanted:
            break
    return selected or ranked[:wanted]


def _merge_user_pois(selected: list[dict[str, Any]], picks: list[Any]) -> list[dict[str, Any]]:
    if not picks:
        return selected
    by_id = {poi["id"]: poi for poi in selected}
    for pick in picks:
        data = pick.model_dump() if hasattr(pick, "model_dump") else dict(pick)
        poi = {
            "id": str(data["id"]),
            "name": data["name"],
            "lat": float(data["lat"]),
            "lng": float(data["lng"]),
            "kind": data.get("kind") or "plek",
            "kind_label": data.get("kind_label") or data.get("kind") or "plek",
            "interest": data.get("interest") or "geschiedenis",
            "source": "OpenStreetMap",
            "description": "",
        }
        by_id[poi["id"]] = poi
    merged = list(by_id.values())
    merged.sort(key=lambda item: item["name"])
    return merged


def _nodes_for_ai(nodes: list[dict[str, Any]], start_node: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not nodes:
        return []
    rest = [n for n in nodes if not start_node or n["id"] != start_node["id"]]
    rest.sort(key=lambda n: n.get("match_score", 0), reverse=True)
    ordered = ([start_node] if start_node else []) + rest
    return ordered[:36]


def _along_corridor(stops: list[dict[str, Any]], start: Place, end: Place) -> list[dict[str, Any]]:
    def progress(poi: dict[str, Any]) -> float:
        return haversine_m(start.lat, start.lng, poi["lat"], poi["lng"]) - 0.15 * haversine_m(
            poi["lat"], poi["lng"], end.lat, end.lng
        )

    return sorted(stops, key=progress)


def _title(request: PlanRequest, start: Place, end: Place, knoop_chain: str) -> str:
    city = start.label.split(",")[0]
    if knoop_chain:
        return f"Knooppuntenlus rond {city}"
    if request.mode == "lus":
        return f"Fietsverhaal rond {city}"
    return f"Fietsverhaal van {city} naar {end.label.split(',')[0]}"


def _intro(
    request: PlanRequest,
    start: Place,
    selected: list[dict[str, Any]],
    route: dict[str, Any],
    knoop_chain: str,
    weather: WeatherInfo | None = None,
) -> str:
    km = round(route["distance_m"] / 1000, 1)
    names = ", ".join(poi["name"] for poi in selected[:3])
    extra = f" {request.notes}." if request.notes else ""
    chain = f" Volg knooppunten {knoop_chain}." if knoop_chain else ""
    sights = f" Onderweg: {names}." if names else ""
    place = start.place_name or start.label.split(",")[0]
    weather_bit = f" Weer: {weather.summary}." if weather and weather.summary else ""
    return (
        f"Vanaf {place} fiets je ongeveer {km} km.{chain}{sights}{extra}{weather_bit}"
    )


def _effective_distance(request: PlanRequest, profile, weather: WeatherInfo) -> int:
    distance = int(request.distance_km)
    if request.budget_mode == "time" and request.duration_min:
        speed = SPEED_KMH.get(
            (getattr(profile, "fitness", "recreant"), getattr(profile, "bike", "stadsfiets")),
            16,
        )
        distance = max(8, min(90, round(request.duration_min / 60 * speed)))
    if request.adapt_reason in {"regen", "wind", "korter"} or weather.suggest_shorter:
        distance = max(8, min(distance, round(distance * 0.65)))
    return distance


def _shorten_nodes(nodes: list[Knooppunt], start_lat: float, start_lng: float, target_km: float, loop: bool) -> list[Knooppunt]:
    if len(nodes) <= 2:
        return nodes
    unique = []
    seen = set()
    for node in nodes:
        key = node.id or f"{node.number}|{round(node.lat, 4)}"
        if key in seen:
            continue
        seen.add(key)
        unique.append(node)
    unique.sort(key=lambda n: haversine_m(start_lat, start_lng, n.lat, n.lng))
    keep = max(2, min(len(unique), 3 if target_km < 15 else 4))
    picked = unique[:keep]
    if loop and picked:
        return picked
    return picked


def _localities_from_stops(stops: list[Stop]) -> list[Locality]:
    seen: set[str] = set()
    result: list[Locality] = []
    for stop in stops:
        name = (stop.place_name or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        result.append(
            Locality(
                name=name,
                municipality=None,
                population=stop.population,
                fact=stop.local_fact or "",
                lat=stop.lat,
                lng=stop.lng,
            )
        )
    return result


def _profile_notes(profile, notes: str, adapt_reason: str | None = None, weather: WeatherInfo | None = None) -> str:
    parts = [notes.strip()] if notes and notes.strip() else []
    if profile and profile.horeca:
        labels = {
            "snack": "snack of terras",
            "tafelen": "restaurant",
            "koffie": "koffie en taart",
            "brouwerijen": "brouwerij of café",
        }
        parts.append("horeca: " + ", ".join(labels.get(item, item) for item in profile.horeca))
    if adapt_reason == "veer":
        parts.append("vermijd veerponten")
    if adapt_reason in {"regen", "wind", "korter"} or (weather and weather.suggest_shorter):
        parts.append("kortere beschutte lus")
    return ". ".join(part for part in parts if part)


async def _optional(task, fallback, timeout: float = 8):
    try:
        return await asyncio.wait_for(task, timeout=timeout)
    except Exception:
        return fallback
