from __future__ import annotations

import asyncio
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


async def plan_route(request: PlanRequest) -> RoutePlan:
    catalog_route = suggestion_service.get_route_by_id(request.suggestion_id) if request.suggestion_id else None
    if catalog_route:
        request.interests = suggestion_service.merge_interests(catalog_route, request.interests)
        request.notes = suggestion_service.merge_notes(catalog_route, request.notes)
    wish_interests = pois_service.interests_from_notes(request.notes)
    if wish_interests:
        request.interests = list(dict.fromkeys([*request.interests, *wish_interests]))

    start = await geocoding.geocode_one(request.start)
    if request.mode == "punt":
        if request.knooppunten and len(request.knooppunten) >= 2:
            first = request.knooppunten[0]
            last = request.knooppunten[-1]
            start = await geocoding.geocode_one(f"{first.lat},{first.lng}")
            end = await geocoding.geocode_one(f"{last.lat},{last.lng}")
        elif not request.end:
            raise ValueError("Kies minstens twee knooppunten voor Van A naar B, of kies een lus.")
        else:
            end = await geocoding.geocode_one(request.end)
    else:
        end = start

    profile = request.profile
    weather = WeatherInfo(**await weather_service.fetch_weather(start.lat, start.lng))
    request.distance_km = _effective_distance(request, profile, weather)

    radius = _search_radius(request, start, end)
    extra = (end.lat, end.lng) if request.mode == "punt" else None
    horeca_prefs = list(profile.horeca) if profile else []
    want_horeca = pois_service.notes_want_horeca(request.notes, request.interests, horeca_prefs)
    want_wiki = any(
        item in request.interests
        for item in ("geschiedenis", "oorlog", "architectuur", "natuur", "landbouw", "activiteiten", "evenementen")
    )
    rider_notes = _profile_notes(profile, request.notes, request.adapt_reason, weather)
    nodes, osm_pois, wiki_places, live_events, horeca = await asyncio.gather(
        _optional(knoop_service.fetch_nodes(start.lat, start.lng, radius, extra), [], 14),
        _optional(pois_service.fetch_pois(start.lat, start.lng, radius, request.interests, extra), [], 8),
        _optional(wikipedia.places_for_route(start.lat, start.lng, min(radius, 10000), extra), [], 18),
        _optional(
            events_service.fetch_events(start.lat, start.lng, radius)
            if "evenementen" in request.interests
            else asyncio.sleep(0, result=[]),
            [],
            6,
        ),
        _optional(
            pois_service.fetch_horeca(start.lat, start.lng, radius, extra, horeca_prefs) if want_horeca else asyncio.sleep(0, result=[]),
            [],
            10,
        ),
    )
    if not want_wiki:
        wiki_places = []
    candidates = _merge(osm_pois, wiki_places, live_events, horeca)
    knoop_service.attach_nearby(nodes, candidates)
    knoop_service.score_nodes_for_notes(nodes, rider_notes)

    user_chain = knoop_service.chain_from_user(
        [n.model_dump() for n in request.knooppunten],
        request.mode == "lus",
    )
    if user_chain:
        seen = {n.get("id") for n in nodes}
        for node in user_chain:
            if node["id"] not in seen:
                nodes.append(node)
                seen.add(node["id"])

    geometric = knoop_service.plan_node_chain(nodes, start.lat, start.lng, request.distance_km, extra)
    noted = (
        knoop_service.chain_from_notes(nodes, start.lat, start.lng, request.distance_km, extra)
        if rider_notes.strip()
        else []
    )
    chain = user_chain or noted or geometric
    start_node = knoop_service.nearest_node(nodes, start.lat, start.lng) if nodes else None
    ai_nodes = _nodes_for_ai(nodes, start_node) if nodes else []

    ranked_all = _rank(candidates, start, end, request)
    wiki_targets = pois_service.build_stop_pool(ranked_all, request.interests, limit=20)
    if request.notes.strip():
        for poi in ranked_all:
            if pois_service.matches_notes(poi, request.notes) and poi not in wiki_targets:
                wiki_targets.append(poi)
        wiki_targets = wiki_targets[:24]
    if wiki_targets:
        wiki_results = await asyncio.gather(*(wikipedia.summary_for_poi(poi) for poi in wiki_targets))
        for poi, wiki in zip(wiki_targets, wiki_results, strict=True):
            poi["wiki"] = wiki
            poi["summary"] = wiki.get("summary") or poi.get("description") or ""

    ai_choice = await enrich_with_ai(
        start.label,
        request.interests,
        rider_notes,
        wiki_targets,
        request.mode,
        request.distance_km,
        knoop_service.chain_label(chain) if chain else "",
        ai_nodes if rider_notes.strip() else [],
        profile,
    )
    route_reason = "Eigen knooppuntenroute" if user_chain else ""
    if rider_notes.strip() and ai_choice and start_node and nodes and not user_chain:
        ai_chain = knoop_service.resolve_chain(
            ai_choice.get("knoop_ids") or [],
            nodes,
            start_node,
            request.mode == "lus",
        )
        if ai_chain:
            chain = ai_chain
            route_reason = ai_choice.get("reason") or f"Route aangepast aan: {rider_notes}"
    if rider_notes.strip() and (noted or (ai_choice or {}).get("knoop_ids")):
        route_reason = route_reason or f"Knooppunten gekozen bij: {rider_notes}"

    knoop_label = knoop_service.chain_label(chain) if chain else ""
    if chain:
        candidates = _near_chain(candidates, chain, request.notes)
    ranked = _rank(candidates, start, end, request)
    if not ranked and not chain:
        raise ValueError(
            "Geen plekken of knooppunten gevonden. Probeer een andere startlocatie in Vlaanderen."
        )
    stop_pool = pois_service.build_stop_pool(ranked, request.interests)
    wished = [poi for poi in ranked if pois_service.matches_notes(poi, request.notes)]
    if wished:
        seen = {str(poi.get("id")) for poi in stop_pool}
        stop_pool = [poi for poi in wished if str(poi.get("id")) not in seen] + stop_pool
    selected = _pick(stop_pool, ai_choice, request) if stop_pool else []
    selected = _merge_user_pois(selected, request.poi_picks)

    if chain:
        spine = _chain_spine(chain)
        display_chain, route = await _build_knoop_route(
            start.lat,
            start.lng,
            spine,
            close_loop=request.mode != "punt",
            end_lat=end.lat if request.mode == "punt" else None,
            end_lng=end.lng if request.mode == "punt" else None,
        )
        chain = display_chain
        if request.notes.strip() and route.get("geometry"):
            chain = knoop_service.supplement_chain_on_geometry(chain, nodes, route["geometry"], max_m=120)
        knoop_label = knoop_service.chain_label(
            knoop_service.close_chain_for_loop(chain) if request.mode == "lus" else chain
        )
        selected = _along_geometry(selected, route["geometry"])
    elif request.mode == "lus":
        order = await routing.round_trip_order((start.lat, start.lng), [(s["lat"], s["lng"]) for s in selected])
        selected = [selected[i] for i in order]
        route_points = [(start.lat, start.lng), *[(s["lat"], s["lng"]) for s in selected], (start.lat, start.lng)]
        route = await routing.bike_route(route_points)
    else:
        selected = _along_corridor(selected, start, end)
        route_points = [(start.lat, start.lng), *[(s["lat"], s["lng"]) for s in selected], (end.lat, end.lng)]
        route = await routing.bike_route(route_points)

    stops = []
    for index, poi in enumerate(selected, start=1):
        wiki = poi.get("wiki") or {}
        scripts = fallback_scripts(poi, wiki, request.explanation_level)
        ai_scripts = ((ai_choice or {}).get("scripts") or {}).get(poi["id"]) or {}
        stops.append(
            {
                "id": poi["id"],
                "name": poi["name"],
                "lat": poi["lat"],
                "lng": poi["lng"],
                "kind": poi.get("kind_label") or poi.get("kind") or "plek",
                "interest": poi["interest"],
                "source": poi.get("source") or "OpenStreetMap",
                "summary": poi.get("summary") or scripts["summary"],
                "approaching": ai_scripts.get("approaching") or scripts["approaching"],
                "arrived": ai_scripts.get("arrived") or scripts["arrived"],
                "why": ai_scripts.get("why") or scripts["why"],
                "wikipedia_url": wiki.get("url") or None,
                "image_url": wiki.get("image") or None,
                "wikipedia": poi.get("wikipedia"),
                "wikidata": poi.get("wikidata"),
                "description": poi.get("description") or "",
                "kind_label": poi.get("kind_label"),
                "index": index,
                "matches_wish": pois_service.matches_notes(poi, request.notes),
                "on_route": False,
            }
        )

    if request.notes.strip() and route.get("geometry"):
        wish_pois, _ = await _wish_pois_for_geometry(
            request.notes,
            candidates,
            ranked,
            chain or [],
            route["geometry"],
            list(request.interests),
        )
        _merge_wish_into_stops(stops, wish_pois, request, route["geometry"])

    stops = await polish_scripts(stops, request.explanation_level)
    stops = await places_service.enrich_stops_with_places(stops, request.interests)
    stops = places_service.assign_sides(stops, route["geometry"])
    for stop in stops:
        place = stop.get("place_name") or ""
        pop = stop.get("population")
        fact = stop.get("local_fact") or ""
        side = stop.get("side") or ""
        extras = []
        if place:
            extras.append(f"in {place}" + (f" ({pop} inwoners)" if pop else ""))
        if side and side not in {"langs de route", ""}:
            extras.append(f"aan je {side}")
        if fact and fact not in (stop.get("arrived") or ""):
            extras.append(fact)
        if extras and request.explanation_level != "kort":
            stop["arrived"] = f"{stop['arrived']} {' · '.join(extras)}"
        elif place and place not in (stop.get("approaching") or ""):
            stop["approaching"] = f"{stop['approaching']} Je bent in {place}."

    title = (ai_choice or {}).get("title") or (catalog_route["title"] if catalog_route else _title(request, start, end, knoop_label))
    intro = (ai_choice or {}).get("intro") or (
        suggestion_service.catalog_intro(
            catalog_route,
            request.interests,
            round(route["distance_m"] / 1000, 1),
            start.label,
        )
        if catalog_route
        else _intro(request, start, selected, route, knoop_label, weather)
    )

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
        )
        for s in stops
    ]
    knoop_chain_display = (
        knoop_service.close_chain_for_loop(chain)
        if request.mode == "lus" and chain
        else (chain or [])
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
    chain_ids = {n.get("id") for n in chain if n.get("id")}
    all_knoop = _unique_knooppunten(nodes, chain_ids)
    localities = _localities_from_stops(model_stops)
    if catalog_route:
        localities = suggestion_service.merge_localities(catalog_route, localities)
        if not route_reason:
            route_reason = f"Route Top 10 · {catalog_route['title']}"
    sources = sorted(
        {stop.source for stop in model_stops}
        | {n.get("source") or "Toerisme Vlaanderen" for n in chain}
        | {"OpenStreetMap", "OSRM fietsrouting", "Wikipedia", "Fietsknooppuntennetwerk Vlaanderen", "Open-Meteo"}
    )
    if weather.alert and not route_reason:
        route_reason = weather.alert
    return RoutePlan(
        title=title,
        intro=intro,
        mode=request.mode,
        interests=request.interests,
        notes=(request.notes or "").strip(),
        start=start,
        end=end,
        distance_km=round(route["distance_m"] / 1000, 1),
        duration_min=max(1, round(route["duration_s"] / 60)),
        geometry=route["geometry"],
        stops=model_stops,
        knooppunten=knoop_models,
        all_knooppunten=all_knoop,
        knoop_chain=knoop_label,
        route_reason=route_reason,
        steps=route.get("steps") or [],
        explanation_level=request.explanation_level,
        interaction=(profile.interaction if profile else "live"),
        weather=weather,
        budget_mode=request.budget_mode,
        duration_budget_min=request.duration_min,
        localities=localities,
        sources=sources,
        ai_used=bool(ai_choice),
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
    interpret_task = (
        asyncio.create_task(interpret_wish_notes(notes, profile_interests))
        if notes.strip() and has_ai()
        else None
    )
    nodes_task = knoop_service.fetch_nodes(lat, lng, radius, extra)
    pois_task = (
        pois_service.fetch_pois(lat, lng, radius, wish_interests, extra)
        if notes.strip() and wish_interests
        else asyncio.sleep(0, result=[])
    )
    horeca_task = (
        pois_service.fetch_horeca(lat, lng, radius, extra)
        if notes.strip() and ("horeca" in wish_interests or pois_service.notes_want_horeca(notes))
        else asyncio.sleep(0, result=[])
    )
    wiki_task = (
        wikipedia.places_for_route(lat, lng, min(radius, 10000), extra)
        if notes.strip()
        else asyncio.sleep(0, result=[])
    )
    nodes, osm_pois, horeca, wiki_places, interpreted = await asyncio.gather(
        _optional(nodes_task, [], 16),
        _optional(pois_task, [], 24),
        _optional(horeca_task, [], 24),
        _optional(wiki_task, [], 18),
        interpret_task if interpret_task else asyncio.sleep(0, result=None),
    )
    wish_summary = ""
    if interpreted:
        ai_interests = [item for item in interpreted.get("interests", []) if item]
        merged_interests = list(dict.fromkeys([*wish_interests, *ai_interests]))
        extra_interests = [item for item in ai_interests if item not in wish_interests]
        if extra_interests:
            more_pois = await _optional(
                pois_service.fetch_pois(lat, lng, radius, extra_interests, extra),
                [],
                18,
            )
            osm_pois = _merge(osm_pois, more_pois)
        wish_interests = merged_interests[:4] or wish_interests
        wish_summary = str(interpreted.get("summary") or "").strip()
    candidates = _merge(osm_pois, horeca, wiki_places)
    if candidates:
        knoop_service.attach_nearby(nodes, candidates)
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

    display_chain, route = await _build_knoop_route(
        lat,
        lng,
        _chain_spine(chain),
        close_loop=mode == "lus",
        end_lat=end_lat if mode == "punt" else None,
        end_lng=end_lng if mode == "punt" else None,
        poi_picks=poi_picks,
    )
    geometry = route["geometry"]
    chain_candidates = list(candidates)
    for node in display_chain:
        nearby = [
            poi
            for poi in (node.get("nearby") or [])
            if poi.get("lat") is not None and poi.get("lng") is not None and poi.get("name")
        ]
        chain_candidates = _merge(chain_candidates, nearby)
    wish_pois, ranked_summary = await _wish_pois_for_geometry(
        notes, chain_candidates, [], display_chain, geometry, profile_interests
    )
    if ranked_summary:
        wish_summary = ranked_summary
    if poi_picks:
        picked_ids: set[str] = set()
        for pick in poi_picks:
            data = pick.model_dump() if hasattr(pick, "model_dump") else dict(pick)
            picked_ids.add(str(data["id"]))
        for poi in wish_pois:
            if str(poi.get("id")) in picked_ids:
                poi["on_route"] = True
    suggestions = []
    for poi in wish_pois:
        try:
            suggestions.append(
                {
                    "id": str(poi["id"]),
                    "name": poi["name"],
                    "lat": float(poi["lat"]),
                    "lng": float(poi["lng"]),
                    "kind": str(poi.get("kind") or "plek"),
                    "kind_label": poi.get("kind_label"),
                    "interest": poi.get("interest") or (
                        pois_service.interests_from_notes(notes)[0]
                        if pois_service.interests_from_notes(notes)
                        else "geschiedenis"
                    ),
                    "on_route": bool(poi.get("on_route")),
                    "hint": poi.get("hint"),
                }
            )
        except Exception:
            continue
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
        "suggestions": suggestions,
    }


def _format_wish_suggestions(wish_pois: list[dict[str, Any]], notes: str) -> list[dict[str, Any]]:
    suggestions: list[dict[str, Any]] = []
    note_interests = pois_service.interests_from_notes(notes)
    fallback_interest = note_interests[0] if note_interests else "geschiedenis"
    for poi in wish_pois:
        try:
            suggestions.append(
                {
                    "id": str(poi["id"]),
                    "name": poi["name"],
                    "lat": float(poi["lat"]),
                    "lng": float(poi["lng"]),
                    "kind": str(poi.get("kind") or "plek"),
                    "kind_label": poi.get("kind_label"),
                    "interest": poi.get("interest") or fallback_interest,
                    "on_route": bool(poi.get("on_route")),
                    "hint": poi.get("hint"),
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
    if not (notes or "").strip() or not geometry or len(geometry) < 2:
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
    wish_pois, wish_summary = await _wish_pois_for_geometry(
        notes,
        [],
        [],
        chain,
        geometry,
        profile_interests,
    )
    return {
        "suggestions": _format_wish_suggestions(wish_pois, notes),
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
    by_geoid: dict[int, dict[str, Any]] = {
        int(node["geoid"]): node for node in network_nodes if node.get("geoid") is not None
    }
    for node in spine_geo:
        if node.get("geoid") is not None:
            by_geoid[int(node["geoid"])] = node
    adj = knoop_service.build_adjacency(trajects)
    route_chain = knoop_service._display_chain_between_picks(spine_geo, by_geoid, adj)
    if len(route_chain) < len(spine_geo):
        route_chain = list(spine_geo)
    must_visit = route_chain

    # Eerst de zuivere knooppuntenroute; POI's daarna als lokale spur (geen A→POI→B).
    route = await _route_along_must_visit(
        start_lat,
        start_lng,
        route_chain,
        network_nodes,
        trajects,
        close_loop=close_loop,
        end_lat=end_lat,
        end_lng=end_lng,
    )
    if poi_picks:
        route = await _apply_poi_spurs(route, poi_picks)

    # Absolute garantie: elke gekozen knoop ligt op de rode lijn.
    route["geometry"] = _pin_nodes_on_geometry(route.get("geometry") or [], spine_geo)
    if not _route_covers_nodes(route.get("geometry") or [], spine_geo, max_m=50):
        route["geometry"] = _pin_nodes_on_geometry(route.get("geometry") or [], must_visit + spine_geo)

    geometry = route.get("geometry") or []
    geo_nodes_all, _ = await knoop_service.fetch_network_for_geometry(geometry, network=None)
    network_nodes = knoop_service._merge_network_nodes(network_nodes, geo_nodes_all)
    # Lijst = netwerkvolgorde tussen je picks (niet geometry-filter: mist tussenliggende knopen).
    display_chain = knoop_service.chain_for_display(
        route_chain,
        spine_geo,
        network_nodes=network_nodes,
        trajects=trajects,
    )
    display_chain = knoop_service.refresh_chain_coords(display_chain, network_nodes)
    display_chain = knoop_service.ensure_corridor_knoop_76(display_chain, geometry, network_nodes)
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

    async def add_osrm_segment(a: tuple[float, float], b: tuple[float, float]) -> None:
        nonlocal distance_m, duration_s
        if haversine_m(a[0], a[1], b[0], b[1]) < 20:
            return
        try:
            osrm = await routing.bike_route([a, b], retries=1)
            piece = list(osrm["geometry"])
            if piece:
                piece[0] = [a[0], a[1]]
                piece[-1] = [b[0], b[1]]
                geometries.append(piece)
                distance_m += float(osrm["distance_m"])
                duration_s += float(osrm["duration_s"])
                steps.extend(osrm.get("steps") or [])
        except Exception:
            geometries.append([[a[0], a[1]], [b[0], b[1]]])
            distance_m += haversine_m(a[0], a[1], b[0], b[1])
            duration_s += 60.0

    if must_visit:
        first = must_visit[0]
        await add_osrm_segment((start_lat, start_lng), (float(first["lat"]), float(first["lng"])))
        mark_visited(first)

    for index in range(len(must_visit) - 1):
        left = must_visit[index]
        right = must_visit[index + 1]
        if knoop_service._same_knoop(left, right):
            mark_visited(right)
            continue
        segment, known_length = knoop_service.geometry_between_nodes(
            left, right, by_edge, edge_length, adj, by_geoid
        )
        if segment and len(segment) >= 2:
            geometries.append(segment)
            if known_length > 0:
                distance_m += known_length
            else:
                for i in range(1, len(segment)):
                    distance_m += haversine_m(segment[i - 1][0], segment[i - 1][1], segment[i][0], segment[i][1])
            duration_s += max(30.0, (known_length or 0) / 3.9)
            mark_visited(right)
            continue
        segment, known_length = knoop_service.geometry_through_network(
            left,
            right,
            by_edge,
            edge_length,
            adj,
            by_geoid,
            avoid_geoids=visited_geoids,
        )
        if segment and len(segment) >= 2:
            geometries.append(segment)
            if known_length > 0:
                distance_m += known_length
            else:
                for i in range(1, len(segment)):
                    distance_m += haversine_m(segment[i - 1][0], segment[i - 1][1], segment[i][0], segment[i][1])
            duration_s += max(30.0, (known_length or 0) / 3.9)
            mark_visited(right)
            continue
        await add_osrm_segment(
            (float(left["lat"]), float(left["lng"])),
            (float(right["lat"]), float(right["lng"])),
        )
        mark_visited(right)

    if must_visit:
        last = must_visit[-1]
        first = must_visit[0]
        if close_loop:
            if not knoop_service._same_knoop(first, last):
                segment, known_length = knoop_service.geometry_between_nodes(
                    last, first, by_edge, edge_length, adj, by_geoid
                )
                if not segment or len(segment) < 2:
                    segment, known_length = knoop_service.geometry_through_network(
                        last,
                        first,
                        by_edge,
                        edge_length,
                        adj,
                        by_geoid,
                        avoid_geoids=visited_geoids,
                    )
                if segment and len(segment) >= 2:
                    geometries.append(segment)
                    if known_length > 0:
                        distance_m += known_length
                    else:
                        for i in range(1, len(segment)):
                            distance_m += haversine_m(
                                segment[i - 1][0], segment[i - 1][1], segment[i][0], segment[i][1]
                            )
                    duration_s += max(30.0, (known_length or 0) / 3.9)
                else:
                    await add_osrm_segment(
                        (float(last["lat"]), float(last["lng"])),
                        (float(first["lat"]), float(first["lng"])),
                    )
            if haversine_m(start_lat, start_lng, float(first["lat"]), float(first["lng"])) > 20:
                await add_osrm_segment(
                    (float(first["lat"]), float(first["lng"])),
                    (start_lat, start_lng),
                )
        elif end_lat is not None and end_lng is not None:
            await add_osrm_segment((float(last["lat"]), float(last["lng"])), (end_lat, end_lng))

    geometry = routing._merge_geometries(geometries)
    geometry = _pin_nodes_on_geometry(geometry, must_visit)
    geometry = _pin_nodes_on_geometry(geometry, [{"lat": start_lat, "lng": start_lng, "number": ""}])
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
            osrm = await routing.bike_route([a, b], retries=1)
            piece = list(osrm["geometry"])
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
            geometries.append([[a[0], a[1]], [b[0], b[1]]])
            distance_m += haversine_m(a[0], a[1], b[0], b[1])
            duration_s += 60.0

    geometry = routing._merge_geometries(geometries)
    geometry = _pin_nodes_on_geometry(geometry, [n for n in node_at if n])
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


def _pin_nodes_on_geometry(geometry: list[list[float]], nodes: list[dict[str, Any]]) -> list[list[float]]:
    """Zorg dat elk knooppunt letterlijk op de lijn ligt (geen voorbijrijden)."""
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
            score = point_to_segment_m(lat, lng, result[index][0], result[index][1], result[index + 1][0], result[index + 1][1])
            if score < best_score:
                best_score = score
                insert_at = index + 1
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


async def _wish_pois_for_geometry(
    notes: str,
    candidates: list[dict[str, Any]],
    ranked: list[dict[str, Any]],
    chain: list[dict[str, Any]],
    geometry: list[list[float]],
    profile_interests: list[str] | None = None,
) -> tuple[list[dict[str, Any]], str]:
    if not notes.strip() or not geometry:
        return [], ""
    wish_interests = pois_service.wish_interests_for_notes(notes, profile_interests)
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

    # Horeca (café/…) — geen korte outer-timeout die alles weggooit bij trage mirrors.
    if want_horeca:
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
        route_candidates = _merge(route_candidates, horeca)

    other_interests = [item for item in wish_interests if not (want_horeca and item == "horeca")]
    if other_interests:
        along = await _optional(
            pois_service.fetch_pois_along_points(
                sample_points,
                corridor_m,
                other_interests,
                max_points=min(18, len(sample_points)),
            ),
            [],
            50,
        )
        route_candidates = _merge(route_candidates, along)
    elif wish_interests and not want_horeca:
        along = await _optional(
            pois_service.fetch_pois_along_points(
                sample_points,
                corridor_m,
                wish_interests,
                max_points=min(18, len(sample_points)),
            ),
            [],
            50,
        )
        route_candidates = _merge(route_candidates, along)

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
        if not wish and not pois_service.matches_notes(poi, notes):
            if not (bool(wish_set) and poi.get("interest") in wish_set):
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
            0 if fits_wish(poi) else 1,
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
                profile_interests,
                wish_interests,
                target_count=target,
                route_km=route_km,
            ),
            None,
            14,
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
    return diverse, wish_summary


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
            continue
        wiki = poi.get("wiki") or {}
        scripts = fallback_scripts(poi, wiki, request.explanation_level)
        stops.append(
            {
                "id": pid,
                "name": poi["name"],
                "lat": poi["lat"],
                "lng": poi["lng"],
                "kind": poi.get("kind_label") or poi.get("kind") or "plek",
                "interest": poi["interest"],
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
                "matches_wish": True,
                "on_route": on_route,
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
