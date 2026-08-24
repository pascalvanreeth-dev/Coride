from __future__ import annotations

import asyncio
from typing import Any

from app.config import settings
from app.models import Knooppunt, Place, PlanRequest, RerouteRequest, RerouteResponse, RoutePlan, Stop
from app.services import events as events_service
from app.services import geocoding
from app.services import knooppunten as knoop_service
from app.services import pois as pois_service
from app.services import routing
from app.services import wikipedia
from app.services.ai import enrich_with_ai, fallback_scripts, polish_scripts
from app.services.geo import haversine_m, point_to_segment_m, unique_key


async def plan_route(request: PlanRequest) -> RoutePlan:
    start = await geocoding.geocode_one(request.start)
    if request.mode == "punt":
        if not request.end:
            raise ValueError("Vul een eindlocatie in, of kies een lus.")
        end = await geocoding.geocode_one(request.end)
    else:
        end = start

    radius = _search_radius(request, start, end)
    extra = (end.lat, end.lng) if request.mode == "punt" else None
    want_horeca = pois_service.notes_want_horeca(request.notes)
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
            pois_service.fetch_horeca(start.lat, start.lng, radius, extra) if want_horeca else asyncio.sleep(0, result=[]),
            [],
            10,
        ),
    )
    if "geschiedenis" not in request.interests:
        wiki_places = []
    candidates = _merge(osm_pois, wiki_places, live_events, horeca)
    knoop_service.attach_nearby(nodes, candidates)
    knoop_service.score_nodes_for_notes(nodes, request.notes)

    geometric = knoop_service.plan_node_chain(nodes, start.lat, start.lng, request.distance_km, extra)
    noted = (
        knoop_service.chain_from_notes(nodes, start.lat, start.lng, request.distance_km, extra)
        if request.notes.strip()
        else []
    )
    chain = noted or geometric
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
        request.notes,
        wiki_targets,
        request.mode,
        request.distance_km,
        knoop_service.chain_label(chain) if chain else "",
        ai_nodes if request.notes.strip() else [],
    )
    route_reason = ""
    if request.notes.strip() and ai_choice and start_node and nodes:
        ai_chain = knoop_service.resolve_chain(
            ai_choice.get("knoop_ids") or [],
            nodes,
            start_node,
            request.mode == "lus",
        )
        if ai_chain:
            chain = ai_chain
            route_reason = ai_choice.get("reason") or f"Route aangepast aan: {request.notes}"
    if request.notes.strip() and (noted or (ai_choice or {}).get("knoop_ids")):
        route_reason = route_reason or f"Knooppunten gekozen bij: {request.notes}"

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

    if chain:
        route_points = [(start.lat, start.lng), *[(n["lat"], n["lng"]) for n in chain]]
        if request.mode == "punt":
            route_points.append((end.lat, end.lng))
        else:
            route_points.append((start.lat, start.lng))
    elif request.mode == "lus":
        order = await routing.round_trip_order((start.lat, start.lng), [(s["lat"], s["lng"]) for s in selected])
        selected = [selected[i] for i in order]
        route_points = [(start.lat, start.lng), *[(s["lat"], s["lng"]) for s in selected], (start.lat, start.lng)]
    else:
        selected = _along_corridor(selected, start, end)
        route_points = [(start.lat, start.lng), *[(s["lat"], s["lng"]) for s in selected], (end.lat, end.lng)]

    route = await routing.bike_route(route_points)
    if chain:
        selected = _along_geometry(selected, route["geometry"])

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
    title = (ai_choice or {}).get("title") or _title(request, start, end, knoop_label)
    intro = (ai_choice or {}).get("intro") or _intro(request, start, selected, route, knoop_label)

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
        )
        for n in chain
    ]
    chain_ids = {n.get("id") for n in chain if n.get("id")}
    all_knoop = _unique_knooppunten(nodes, chain_ids)
    sources = sorted(
        {stop.source for stop in model_stops}
        | {n.get("source") or "Toerisme Vlaanderen" for n in chain}
        | {"OpenStreetMap", "OSRM fietsrouting", "Wikipedia", "Fietsknooppuntennetwerk Vlaanderen"}
    )
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
        sources=sources,
        ai_used=bool(ai_choice),
    )


async def reroute(request: RerouteRequest) -> RerouteResponse:
    if len(request.nodes) < 1:
        raise ValueError("Selecteer minstens één knooppunt.")
    points: list[tuple[float, float]] = [(request.start_lat, request.start_lng)]
    for node in request.nodes:
        candidate = (node.lat, node.lng)
        if haversine_m(candidate[0], candidate[1], points[-1][0], points[-1][1]) > 20:
            points.append(candidate)
    if request.close_loop:
        start = (request.start_lat, request.start_lng)
        if haversine_m(points[-1][0], points[-1][1], start[0], start[1]) > 20:
            points.append(start)
    if len(points) < 2:
        raise ValueError("Te weinig punten voor een fietsroute.")
    route = await routing.bike_route(points)
    knoop_models = [
        Knooppunt(
            id=n.id,
            number=n.number,
            lat=n.lat,
            lng=n.lng,
            network=n.network,
            on_route=True,
        )
        for n in request.nodes
    ]
    if request.close_loop and knoop_models and knoop_models[0].id != knoop_models[-1].id:
        knoop_models.append(knoop_models[0])
    return RerouteResponse(
        geometry=route["geometry"],
        distance_km=round(route["distance_m"] / 1000, 1),
        duration_min=max(1, round(route["duration_s"] / 60)),
        knooppunten=knoop_models,
        knoop_chain=" → ".join(n.number for n in knoop_models),
        steps=route.get("steps") or [],
    )


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
            )
        )
    return result


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
) -> str:
    km = round(route["distance_m"] / 1000, 1)
    names = ", ".join(poi["name"] for poi in selected[:3])
    extra = f" {request.notes}." if request.notes else ""
    chain = f" Volg knooppunten {knoop_chain}." if knoop_chain else ""
    sights = f" Onderweg: {names}." if names else ""
    return (
        f"Vanaf {start.label.split(',')[0]} fiets je ongeveer {km} km.{chain}{sights}{extra}"
    )


async def _optional(task, fallback, timeout: float = 8):
    try:
        return await asyncio.wait_for(task, timeout=timeout)
    except Exception:
        return fallback
