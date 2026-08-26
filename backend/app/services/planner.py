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
from app.services.ai import enrich_with_ai, fallback_scripts, polish_scripts
from app.services.geo import haversine_m, point_to_segment_m, unique_key


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
    want_wiki = any(item in request.interests for item in ("geschiedenis", "oorlog", "architectuur"))
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
    wiki_targets = ranked_all[:16]
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
        candidates = _near_chain(candidates, chain)
    ranked = _rank(candidates, start, end, request)
    if not ranked and not chain:
        raise ValueError(
            "Geen plekken of knooppunten gevonden. Probeer een andere startlocatie in Vlaanderen."
        )
    wiki_targets = ranked[:16]
    selected = _pick(wiki_targets, ai_choice, request) if wiki_targets else []
    selected = _merge_user_pois(selected, request.poi_picks)

    if chain:
        spine = _chain_spine(chain)
        expanded, route = await _build_knoop_route(
            start.lat,
            start.lng,
            spine,
            close_loop=request.mode != "punt",
            end_lat=end.lat if request.mode == "punt" else None,
            end_lng=end.lng if request.mode == "punt" else None,
        )
        chain = expanded
        knoop_label = knoop_service.chain_label(chain)
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
                "kind_label": poi.get("kind_label"),
                "index": index,
            }
        )

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
            place_name=s.get("place_name"),
            population=s.get("population"),
            local_fact=s.get("local_fact"),
            side=s.get("side"),
        )
        for s in stops
    ]
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
        for n in chain
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
    if request.reason in {"regen", "wind", "korter"} or (request.reason is None and weather.suggest_shorter):
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
    expanded, route = await _build_knoop_route(
        request.start_lat,
        request.start_lng,
        chain,
        close_loop=request.close_loop,
        end_lat=request.end_lat,
        end_lng=request.end_lng,
        poi_picks=request.poi_picks,
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
        for n in expanded
    ]
    return RerouteResponse(
        geometry=route["geometry"],
        distance_km=round(route["distance_m"] / 1000, 1),
        duration_min=max(1, round(route["duration_s"] / 60)),
        knooppunten=knoop_models,
        knoop_chain=" → ".join(n.number for n in knoop_models),
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
) -> dict[str, Any]:
    extra = (end_lat, end_lng) if mode == "punt" and end_lat is not None and end_lng is not None else None
    if mode == "punt" and extra:
        direct = haversine_m(lat, lng, end_lat, end_lng)
        radius = int(min(16000, max(5000, direct / 2 + 4000)))
    else:
        radius = int(min(16000, max(5000, distance_km * 1000 / 2.2)))

    nodes = await knoop_service.fetch_nodes(lat, lng, radius, extra)
    geometric = knoop_service.plan_node_chain(nodes, lat, lng, distance_km, extra)
    noted = (
        knoop_service.chain_from_notes(nodes, lat, lng, distance_km, extra)
        if notes.strip()
        else []
    )
    chain = noted or geometric
    if not chain:
        raise ValueError("Geen knooppunten gevonden voor een routevoorbeeld.")

    expanded, route = await _build_knoop_route(
        lat,
        lng,
        _chain_spine(chain),
        close_loop=mode == "lus",
        end_lat=end_lat if mode == "punt" else None,
        end_lng=end_lng if mode == "punt" else None,
    )
    return {
        "geometry": route["geometry"],
        "distance_km": round(route["distance_m"] / 1000, 1),
        "duration_min": max(1, round(route["duration_s"] / 60)),
        "knooppunten": [
            Knooppunt(
                id=n.get("id") or "",
                number=n["number"],
                lat=n["lat"],
                lng=n["lng"],
                network=n.get("network"),
                on_route=True,
            )
            for n in expanded
        ],
        "knoop_chain": knoop_service.chain_label(expanded),
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
    if not picks:
        return waypoints
    points = list(waypoints)
    ordered: list[tuple[float, float, float]] = []
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
    spine = _chain_spine(chain)
    network_nodes, trajects = await knoop_service.fetch_network_for_chain(spine)
    spine = knoop_service.enrich_chain_geoids(spine, network_nodes)
    expanded = knoop_service.expand_chain(spine, network_nodes, trajects)
    waypoints = knoop_service.waypoints_for_chain(
        start_lat,
        start_lng,
        expanded,
        close_loop=close_loop,
        end_lat=end_lat,
        end_lng=end_lng,
    )
    if poi_picks:
        waypoints = _insert_poi_waypoints(waypoints, poi_picks)
    try:
        route = await routing.bike_route_via_waypoints(waypoints)
    except Exception:
        route = knoop_service.route_from_trajects(waypoints, expanded, trajects)
    return expanded, route


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


def _near_chain(pois: list[dict[str, Any]], chain: list[dict[str, Any]]) -> list[dict[str, Any]]:
    kept = []
    for poi in pois:
        if any(haversine_m(poi["lat"], poi["lng"], n["lat"], n["lng"]) < 1200 for n in chain):
            kept.append(poi)
    return kept or pois


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
        name = (poi.get("name") or "").lower()
        if request.notes:
            blob = f"{name} {poi.get('kind', '')} {poi.get('kind_label', '')}".lower()
            if any(needle in blob for needle in knoop_service._note_needles(request.notes)):
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
    wanted = 5 if request.distance_km < 30 else 6
    by_id = {poi["id"]: poi for poi in ranked}
    chosen: list[dict[str, Any]] = []
    if ai_choice and ai_choice.get("stop_ids"):
        for stop_id in ai_choice["stop_ids"]:
            if stop_id in by_id:
                chosen.append(by_id[stop_id])
    if not chosen:
        chosen = _spread(ranked, wanted)
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
