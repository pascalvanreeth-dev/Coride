from __future__ import annotations

import asyncio
import heapq
import math
import re
from typing import Any

from app.http import client
from app.services.geo import bearing, haversine_m, point_to_segment_m
from app.services.pois import _overpass

WFS_URL = "https://geodata.toerismevlaanderen.be/geoserver/wfs"


async def fetch_nodes(
    lat: float,
    lng: float,
    radius_m: int,
    extra: tuple[float, float] | None = None,
) -> list[dict[str, Any]]:
    radius_m = max(4000, min(int(radius_m), 18000))
    points = [(lat, lng)]
    if extra:
        points.append(extra)
    nodes: list[dict[str, Any]] = []
    seen: set[str] = set()
    wfs = await _from_wfs(points, radius_m)
    osm: list[dict[str, Any]] = []
    if len(wfs) < 6:
        osm = await _from_osm(lat, lng, radius_m, extra)
    for node in wfs + osm:
        key = f"{node['number']}|{round(node['lat'], 4)}|{round(node['lng'], 4)}"
        if key in seen:
            continue
        seen.add(key)
        nodes.append(node)
    return nodes


async def _from_wfs(points: list[tuple[float, float]], radius_m: int) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    try:
        async with client() as http:
            for plat, plng in points:
                minx, miny, maxx, maxy = _bbox(plat, plng, radius_m)
                cql = (
                    f"BBOX(geom,{minx:.5f},{miny:.5f},{maxx:.5f},{maxy:.5f},'EPSG:4326') "
                    "AND knooptype=1"
                )
                response = await http.get(
                    WFS_URL,
                    params={
                        "service": "WFS",
                        "version": "1.1.0",
                        "request": "GetFeature",
                        "typeName": "routes:knoop_fiets",
                        "outputFormat": "application/json",
                        "srsName": "EPSG:4326",
                        "maxFeatures": 180,
                        "cql_filter": cql,
                    },
                    timeout=12.0,
                )
                if response.status_code != 200:
                    continue
                for feature in response.json().get("features") or []:
                    props = feature.get("properties") or {}
                    geom = feature.get("geometry") or {}
                    coords = geom.get("coordinates") or []
                    number = props.get("knoopnr")
                    if number is None or len(coords) < 2:
                        continue
                    nodes.append(
                        {
                            "id": str(feature.get("id") or f"knoop-{number}"),
                            "number": str(number),
                            "lat": float(coords[1]),
                            "lng": float(coords[0]),
                            "geoid": props.get("geoid"),
                            "network": props.get("naam") or "Fietsknooppuntennetwerk Vlaanderen",
                            "source": "Toerisme Vlaanderen",
                        }
                    )
    except Exception:
        return []
    return nodes


async def _from_osm(
    lat: float,
    lng: float,
    radius_m: int,
    extra: tuple[float, float] | None,
) -> list[dict[str, Any]]:
    arounds = [f"around:{radius_m},{lat:.5f},{lng:.5f}"]
    if extra:
        arounds.append(f"around:{radius_m},{extra[0]:.5f},{extra[1]:.5f}")
    clauses = "".join(f'node["rcn_ref"]({around});' for around in arounds)
    query = f"[out:json][timeout:15];({clauses});out;"
    try:
        data = await _overpass(query)
    except Exception:
        return []
    nodes = []
    for element in data.get("elements") or []:
        ref = (element.get("tags") or {}).get("rcn_ref")
        if not ref or "lat" not in element:
            continue
        nodes.append(
            {
                "id": f"osm-{element['id']}",
                "number": str(ref),
                "lat": float(element["lat"]),
                "lng": float(element["lon"]),
                "network": "OpenStreetMap fietsknooppunten",
                "source": "OpenStreetMap",
            }
        )
    return nodes


def plan_node_chain(
    nodes: list[dict[str, Any]],
    start_lat: float,
    start_lng: float,
    target_km: int,
    end: tuple[float, float] | None = None,
) -> list[dict[str, Any]]:
    if len(nodes) < 3:
        return []
    start_node = nearest_node(nodes, start_lat, start_lng)
    if end:
        end_node = nearest_node(nodes, end[0], end[1])
        corridor = [
            n
            for n in nodes
            if n["id"] not in {start_node["id"], end_node["id"]}
            and _along(start_node, end_node, n)
        ]
        corridor.sort(
            key=lambda n: haversine_m(start_node["lat"], start_node["lng"], n["lat"], n["lng"])
        )
        spaced: list[dict[str, Any]] = []
        for node in corridor:
            if all(haversine_m(node["lat"], node["lng"], s["lat"], s["lng"]) > 1200 for s in spaced):
                spaced.append(node)
            if len(spaced) >= 5:
                break
        chain = [start_node, *spaced, end_node]
        return _dedupe_adjacent(chain)

    radius = min(max(target_km * 1000 / 6.3, 1600), 7500)
    ring = [
        n
        for n in nodes
        if n["id"] != start_node["id"]
        and 0.45 * radius <= haversine_m(start_node["lat"], start_node["lng"], n["lat"], n["lng"]) <= 1.15 * radius
    ]
    picked = [start_node]
    sectors = 5 if target_km >= 30 else 4
    for index in range(sectors):
        target = index * (360 / sectors)
        best = None
        best_diff = 45.0
        for node in ring:
            if any(node["id"] == p["id"] for p in picked):
                continue
            angle = bearing(start_node["lat"], start_node["lng"], node["lat"], node["lng"])
            diff = min(abs(angle - target), 360 - abs(angle - target))
            if diff < best_diff:
                best = node
                best_diff = diff
        if best:
            picked.append(best)
    others = [n for n in picked if n["id"] != start_node["id"]]
    if len(others) < 3:
        extras = sorted(
            (n for n in nodes if n["id"] != start_node["id"]),
            key=lambda n: abs(
                haversine_m(start_node["lat"], start_node["lng"], n["lat"], n["lng"]) - radius * 0.7
            ),
        )
        for node in extras:
            if any(node["id"] == o["id"] for o in others):
                continue
            if all(haversine_m(node["lat"], node["lng"], p["lat"], p["lng"]) > 900 for p in others + [start_node]):
                others.append(node)
            if len(others) >= 4:
                break
    others = _order_loop_nodes(start_node, others)
    chain = [start_node, *others, start_node]
    return _dedupe_adjacent(chain)


def chain_from_user(selected: list[dict[str, Any]], loop: bool) -> list[dict[str, Any]]:
    chain = _dedupe_adjacent(
        [
            {
                "id": str(node.get("id") or f"{node['number']}|{round(float(node['lat']), 4)}"),
                "number": str(node["number"]),
                "lat": float(node["lat"]),
                "lng": float(node["lng"]),
                "network": node.get("network") or "Fietsknooppuntennetwerk Vlaanderen",
            }
            for node in selected
            if node.get("number") is not None and node.get("lat") is not None
        ]
    )
    if loop and chain and chain[0]["id"] != chain[-1]["id"]:
        chain.append(chain[0])
    return chain


def chain_label(nodes: list[dict[str, Any]]) -> str:
    return " → ".join(n["number"] for n in nodes)


def dedupe_revisited_numbers(chain: list[dict[str, Any]], allow_close: bool = False) -> list[dict[str, Any]]:
    """Eén bezoek per knoopnummer; voor lus mag het startknooppunt nog eens als sluiting."""
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for index, node in enumerate(chain):
        number = str(node.get("number") or "")
        if allow_close and index == len(chain) - 1 and result and number == str(result[0].get("number")):
            result.append(dict(node))
            continue
        if not number or number in seen:
            continue
        seen.add(number)
        result.append(dict(node))
    return result


def close_chain_for_loop(chain: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Lus tonen als A → B → C → A in overzichten."""
    if not chain:
        return []
    if _same_knoop(chain[0], chain[-1]):
        return list(chain)
    return [*chain, dict(chain[0])]


async def fetch_network_for_chain(chain: list[dict[str, Any]], padding_m: int = 3500) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not chain:
        return [], []
    min_lat = min(node["lat"] for node in chain)
    max_lat = max(node["lat"] for node in chain)
    min_lng = min(node["lng"] for node in chain)
    max_lng = max(node["lng"] for node in chain)
    max_gap = 0.0
    for index in range(len(chain) - 1):
        max_gap = max(
            max_gap,
            haversine_m(chain[index]["lat"], chain[index]["lng"], chain[index + 1]["lat"], chain[index + 1]["lng"]),
        )
    # Ruimere bbox zodat overgeslagen knooppunten tussen twee picks ook meegenomen worden.
    pad_m = max(padding_m, min(18000, max_gap * 0.9 + 2500))
    pad_lat = pad_m / 111_000
    pad_lng = pad_m / (111_000 * max(0.2, math.cos(math.radians((min_lat + max_lat) / 2))))
    bbox = (min_lng - pad_lng, min_lat - pad_lat, max_lng + pad_lng, max_lat + pad_lat)
    nodes, trajects = await _fetch_bbox(bbox)
    return nodes, trajects


def build_adjacency(trajects: list[dict[str, Any]]) -> dict[int, list[tuple[int, float]]]:
    adj: dict[int, list[tuple[int, float]]] = {}
    for traject in trajects:
        start = traject.get("begin_geoid")
        end = traject.get("end_geoid")
        if not start or not end:
            continue
        weight = float(traject.get("shape_length") or traject.get("length_m") or 1.0)
        adj.setdefault(int(start), []).append((int(end), weight))
        adj.setdefault(int(end), []).append((int(start), weight))
    return adj


def index_trajects(
    trajects: list[dict[str, Any]],
) -> tuple[dict[tuple[int, int], list[list[float]]], dict[tuple[int, int], float], dict[int, list[tuple[int, float]]]]:
    by_edge: dict[tuple[int, int], list[list[float]]] = {}
    edge_length: dict[tuple[int, int], float] = {}
    for traject in trajects:
        begin = traject.get("begin_geoid")
        end = traject.get("end_geoid")
        coords = traject.get("coordinates") or []
        if begin is None or end is None or len(coords) < 2:
            continue
        a, b = int(begin), int(end)
        by_edge[(a, b)] = coords
        by_edge[(b, a)] = list(reversed(coords))
        length = float(traject.get("shape_length") or 0)
        if length > 0:
            edge_length[(a, b)] = length
            edge_length[(b, a)] = length
    return by_edge, edge_length, build_adjacency(trajects)


def geometry_between_geoids(
    left_id: int,
    right_id: int,
    by_edge: dict[tuple[int, int], list[list[float]]],
    edge_length: dict[tuple[int, int], float],
    adj: dict[int, list[tuple[int, float]]],
) -> tuple[list[list[float]] | None, float]:
    """Official knooppunten shapes along the network graph (multi-hop when needed)."""
    if left_id == right_id:
        return [], 0.0
    direct = by_edge.get((left_id, right_id))
    if direct and len(direct) >= 2:
        return list(direct), float(edge_length.get((left_id, right_id)) or 0.0)
    path = _shortest_path(adj, left_id, right_id)
    if len(path) < 2:
        return None, 0.0
    combined: list[list[float]] = []
    total = 0.0
    for index in range(len(path) - 1):
        key = (path[index], path[index + 1])
        segment = by_edge.get(key)
        if not segment or len(segment) < 2:
            return None, 0.0
        piece = list(segment)
        if combined and haversine_m(combined[-1][0], combined[-1][1], piece[0][0], piece[0][1]) < 8:
            piece = piece[1:]
        combined.extend(piece)
        known = float(edge_length.get(key) or 0.0)
        if known > 0:
            total += known
        else:
            for i in range(1, len(piece)):
                total += haversine_m(piece[i - 1][0], piece[i - 1][1], piece[i][0], piece[i][1])
    return combined, total


def geometry_between_nodes(
    left: dict[str, Any],
    right: dict[str, Any],
    by_edge: dict[tuple[int, int], list[list[float]]],
    edge_length: dict[tuple[int, int], float],
    adj: dict[int, list[tuple[int, float]]],
    by_geoid: dict[int, dict[str, Any]],
) -> tuple[list[list[float]] | None, float]:
    left_id = left.get("geoid")
    right_id = right.get("geoid")
    if left_id is None:
        left_id = _geoid_for_number(left.get("number"), left, by_geoid)
    if right_id is None:
        right_id = _geoid_for_number(right.get("number"), right, by_geoid)
    if left_id is None or right_id is None:
        return None, 0.0
    geometry, length = geometry_between_geoids(int(left_id), int(right_id), by_edge, edge_length, adj)
    if not geometry:
        return None, 0.0
    piece = list(geometry)
    left_pt = (float(left["lat"]), float(left["lng"]))
    right_pt = (float(right["lat"]), float(right["lng"]))
    if piece and haversine_m(left_pt[0], left_pt[1], piece[0][0], piece[0][1]) > 25:
        piece = [[left_pt[0], left_pt[1]], *piece]
    elif piece:
        piece[0] = [left_pt[0], left_pt[1]]
    if piece and haversine_m(right_pt[0], right_pt[1], piece[-1][0], piece[-1][1]) > 25:
        piece = [*piece, [right_pt[0], right_pt[1]]]
    elif piece:
        piece[-1] = [right_pt[0], right_pt[1]]
    return piece, length


def _merge_geometry_parts(parts: list[list[list[float]]]) -> list[list[float]]:
    geometry: list[list[float]] = []
    for part in parts:
        if not part:
            continue
        if geometry and haversine_m(geometry[-1][0], geometry[-1][1], part[0][0], part[0][1]) < 12:
            part = part[1:]
        geometry.extend(part)
    return geometry


def geometry_through_network(
    left: dict[str, Any],
    right: dict[str, Any],
    by_edge: dict[tuple[int, int], list[list[float]]],
    edge_length: dict[tuple[int, int], float],
    adj: dict[int, list[tuple[int, float]]],
    by_geoid: dict[int, dict[str, Any]],
    avoid_geoids: set[int] | None = None,
) -> tuple[list[list[float]] | None, float]:
    """Traject-geometrie langs het knooppuntennetwerk, met vermijding van herbezoek."""
    segment = _segment_through_network(left, right, by_geoid, adj, avoid_geoids=avoid_geoids or set())
    if len(segment) < 2:
        return None, 0.0
    pieces: list[list[list[float]]] = []
    total = 0.0
    for index in range(len(segment) - 1):
        piece, length = geometry_between_nodes(
            segment[index],
            segment[index + 1],
            by_edge,
            edge_length,
            adj,
            by_geoid,
        )
        if not piece:
            continue
        pieces.append(piece)
        if length > 0:
            total += length
        else:
            for step in range(1, len(piece)):
                total += haversine_m(piece[step - 1][0], piece[step - 1][1], piece[step][0], piece[step][1])
    if not pieces:
        return None, 0.0
    return _merge_geometry_parts(pieces), total


def expand_chain(
    chain: list[dict[str, Any]],
    network_nodes: list[dict[str, Any]],
    trajects: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    chain = enrich_chain_geoids(chain, network_nodes)
    if len(chain) <= 1:
        return list(chain)
    by_geoid = {
        int(node["geoid"]): node
        for node in network_nodes
        if node.get("geoid") is not None
    }
    for node in chain:
        if node.get("geoid") is not None:
            by_geoid[int(node["geoid"])] = node
    adj = build_adjacency(trajects)
    expanded: list[dict[str, Any]] = []
    visited_geoids: set[int] = set()
    visited_numbers: set[str] = set()
    for index in range(len(chain) - 1):
        left = chain[index]
        right = chain[index + 1]
        left_geo = _resolve_geoid(left, by_geoid)
        right_geo = _resolve_geoid(right, by_geoid)
        future_geoids: set[int] = set()
        for future_pick in chain[index + 2 :]:
            geo = _resolve_geoid(future_pick, by_geoid)
            if geo is not None:
                future_geoids.add(geo)
        forbidden = set(visited_geoids) | future_geoids
        if left_geo is not None:
            forbidden.discard(left_geo)
        if right_geo is not None:
            forbidden.discard(right_geo)
        segment = _segment_through_network(left, right, by_geoid, adj, avoid_geoids=forbidden)
        for node in segment:
            if expanded and _same_knoop(expanded[-1], node):
                continue
            number = str(node.get("number") or "")
            geo = _resolve_geoid(node, by_geoid)
            if number and number in visited_numbers and not _same_knoop(node, right):
                continue
            if geo is not None and geo in visited_geoids and not _same_knoop(node, right):
                continue
            expanded.append(node)
            if number:
                visited_numbers.add(number)
            if geo is not None:
                visited_geoids.add(geo)
    return expanded if expanded else list(chain)


def enrich_chain_geoids(chain: list[dict[str, Any]], network_nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Attach geoid when a matching network node is nearby. Never move/rename user picks."""
    by_number: dict[str, list[dict[str, Any]]] = {}
    for node in network_nodes:
        by_number.setdefault(str(node["number"]), []).append(node)
    enriched: list[dict[str, Any]] = []
    for node in chain:
        if node.get("geoid") is not None:
            enriched.append(dict(node))
            continue
        options = by_number.get(str(node["number"]), [])
        best = None
        if options:
            candidate = min(
                options,
                key=lambda item: haversine_m(node["lat"], node["lng"], item["lat"], item["lng"]),
            )
            if haversine_m(node["lat"], node["lng"], candidate["lat"], candidate["lng"]) <= 150:
                best = candidate
        if best is None and network_nodes:
            candidate = min(
                network_nodes,
                key=lambda item: haversine_m(node["lat"], node["lng"], item["lat"], item["lng"]),
            )
            # Alleen geoid overnemen als het echt dezelfde plek is; nummer van de gebruiker behouden.
            if haversine_m(node["lat"], node["lng"], candidate["lat"], candidate["lng"]) <= 40:
                best = candidate
        if best is None:
            enriched.append(dict(node))
            continue
        enriched.append({**node, "geoid": best.get("geoid")})
    return enriched


def waypoints_for_chain(
    start_lat: float,
    start_lng: float,
    chain: list[dict[str, Any]],
    *,
    close_loop: bool = True,
    end_lat: float | None = None,
    end_lng: float | None = None,
) -> list[tuple[float, float]]:
    points: list[tuple[float, float]] = [(start_lat, start_lng)]
    for node in chain:
        candidate = (float(node["lat"]), float(node["lng"]))
        if haversine_m(points[-1][0], points[-1][1], candidate[0], candidate[1]) > 15:
            points.append(candidate)
    if close_loop:
        start = (start_lat, start_lng)
        if haversine_m(points[-1][0], points[-1][1], start[0], start[1]) > 15:
            points.append(start)
    elif end_lat is not None and end_lng is not None:
        end = (end_lat, end_lng)
        if haversine_m(points[-1][0], points[-1][1], end[0], end[1]) > 15:
            points.append(end)
    return points


def route_from_trajects(
    waypoints: list[tuple[float, float]],
    expanded: list[dict[str, Any]],
    trajects: list[dict[str, Any]],
) -> dict[str, Any]:
    """Official knooppunten shapes from WFS when OSRM is unavailable."""
    by_edge, edge_length, adj = index_trajects(trajects)
    by_geoid = {
        int(node["geoid"]): node
        for node in expanded
        if node.get("geoid") is not None
    }

    geometry: list[list[float]] = []
    distance_m = 0.0

    def append_segment(segment: list[list[float]], *, known_length: float | None = None) -> None:
        nonlocal distance_m
        if not segment:
            return
        trimmed = segment
        if geometry and trimmed:
            if haversine_m(geometry[-1][0], geometry[-1][1], trimmed[0][0], trimmed[0][1]) < 8:
                trimmed = trimmed[1:]
        if not trimmed:
            return
        geometry.extend(trimmed)
        if known_length and known_length > 0:
            distance_m += known_length
            return
        for index in range(1, len(trimmed)):
            distance_m += haversine_m(
                trimmed[index - 1][0],
                trimmed[index - 1][1],
                trimmed[index][0],
                trimmed[index][1],
            )

    def straight(left: dict[str, Any] | tuple[float, float], right: dict[str, Any] | tuple[float, float]) -> list[list[float]]:
        if isinstance(left, dict):
            left = (float(left["lat"]), float(left["lng"]))
        if isinstance(right, dict):
            right = (float(right["lat"]), float(right["lng"]))
        return [[left[0], left[1]], [right[0], right[1]]]

    if waypoints and expanded:
        append_segment(straight(waypoints[0], expanded[0]))

    for index in range(len(expanded) - 1):
        left = expanded[index]
        right = expanded[index + 1]
        segment, known_length = geometry_between_nodes(left, right, by_edge, edge_length, adj, by_geoid)
        append_segment(segment or straight(left, right), known_length=known_length if segment else None)

    if waypoints and len(waypoints) >= 2 and expanded:
        append_segment(straight(expanded[-1], waypoints[-1]))

    if not geometry:
        raise RuntimeError("Geen routegeometrie gevonden langs de knooppunten.")

    return {
        "geometry": geometry,
        "distance_m": distance_m,
        "duration_s": max(60.0, distance_m / 3.9),
        "steps": [],
    }


def _segment_through_network(
    left: dict[str, Any],
    right: dict[str, Any],
    by_geoid: dict[int, dict[str, Any]],
    adj: dict[int, list[tuple[int, float]]],
    *,
    avoid_geoids: set[int] | None = None,
) -> list[dict[str, Any]]:
    left_id = _resolve_geoid(left, by_geoid)
    right_id = _resolve_geoid(right, by_geoid)
    if left_id is not None and right_id is not None and left_id in adj and right_id in adj:
        path: list[int] | None = None
        if avoid_geoids:
            path = _shortest_path_avoid(adj, left_id, right_id, avoid_geoids)
        if not path or len(path) < 2:
            path = _shortest_path_penalty(adj, left_id, right_id, avoid_geoids or set(), penalty_m=4000.0)
        if len(path) >= 2:
            middle = [by_geoid[geoid] for geoid in path if geoid in by_geoid]
            return _segment_with_ends(left, right, middle)
    return [left, right]


def _same_knoop(left: dict[str, Any], right: dict[str, Any]) -> bool:
    if not left or not right:
        return False
    left_geo, right_geo = left.get("geoid"), right.get("geoid")
    if left_geo is not None and right_geo is not None:
        try:
            if int(left_geo) == int(right_geo):
                return True
        except (TypeError, ValueError):
            pass
    if left.get("id") and right.get("id") and left["id"] == right["id"]:
        return True
    if str(left.get("number")) != str(right.get("number")):
        return False
    return haversine_m(left["lat"], left["lng"], right["lat"], right["lng"]) <= 80


def _segment_with_ends(
    left: dict[str, Any],
    right: dict[str, Any],
    middle: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    nodes = list(middle)
    if not nodes:
        return [left, right]
    if not _same_knoop(nodes[0], left):
        nodes = [left, *nodes]
    if not _same_knoop(nodes[-1], right):
        nodes = [*[node for node in nodes if not _same_knoop(node, right)], right]
    return nodes


def _index_of_pick_in_expanded(
    pick: dict[str, Any],
    expanded: list[dict[str, Any]],
    by_geoid: dict[int, dict[str, Any]],
) -> int:
    index = next((i for i, node in enumerate(expanded) if _same_knoop(pick, node)), -1)
    if index >= 0:
        return index
    pick_geo = _resolve_geoid(pick, by_geoid)
    if pick_geo is not None:
        index = next((i for i, node in enumerate(expanded) if _resolve_geoid(node, by_geoid) == pick_geo), -1)
        if index >= 0:
            return index
    best_i = -1
    best_d = float("inf")
    for i, node in enumerate(expanded):
        if str(node.get("number")) != str(pick.get("number")):
            continue
        dist = haversine_m(pick["lat"], pick["lng"], node["lat"], node["lng"])
        if dist < best_d:
            best_d = dist
            best_i = i
    return best_i if best_d <= 300 else -1


def chain_for_display(expanded: list[dict[str, Any]], picks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Volledige netwerkvolgorde voor overzicht; gebruikerspicks met hun coördinaten."""
    if not expanded:
        return [dict(p) for p in picks]
    ensured = _ensure_picks_in_chain(expanded, picks)
    if len(ensured) >= max(len(expanded) - 2, len(picks)):
        return ensured
    by_number = {str(p["number"]): p for p in picks}
    result: list[dict[str, Any]] = []
    for node in expanded:
        pick = by_number.get(str(node.get("number")))
        if pick is not None and haversine_m(pick["lat"], pick["lng"], node["lat"], node["lng"]) <= 200:
            merged = dict(pick)
            if node.get("geoid") is not None:
                merged["geoid"] = node["geoid"]
            node_out = merged
        else:
            node_out = dict(node)
        if result and _same_knoop(result[-1], node_out):
            continue
        result.append(node_out)
    return _dedupe_adjacent(result)


def _near_geometry(lat: float, lng: float, geometry: list[list[float]], max_m: float = 650) -> bool:
    if len(geometry) < 2:
        return False
    step = max(1, len(geometry) // 48)
    for index in range(0, len(geometry) - 1, step):
        a = geometry[index]
        b = geometry[index + 1]
        if point_to_segment_m(lat, lng, a[0], a[1], b[0], b[1]) <= max_m:
            return True
    return False


def _geometry_progress_index(lat: float, lng: float, geometry: list[list[float]]) -> float:
    best_i = 0
    best_d = float("inf")
    step = max(1, len(geometry) // 120)
    for index in range(0, len(geometry) - 1, step):
        a = geometry[index]
        b = geometry[index + 1]
        dist = point_to_segment_m(lat, lng, a[0], a[1], b[0], b[1])
        if dist < best_d:
            best_d = dist
            best_i = index
    return float(best_i)


def supplement_chain_on_geometry(
    chain: list[dict[str, Any]],
    pool: list[dict[str, Any]],
    geometry: list[list[float]],
    max_m: float = 650,
) -> list[dict[str, Any]]:
    """Voeg ontbrekende knooppunten in volgorde langs het traject, zonder herhaling."""
    if not chain or not geometry:
        return list(chain)
    result = [dict(node) for node in chain]
    seen_numbers = {str(node.get("number") or "") for node in result if node.get("number")}
    for node in pool:
        number = str(node.get("number") or "")
        if not number or number in seen_numbers:
            continue
        lat, lng = float(node["lat"]), float(node["lng"])
        if not _near_geometry(lat, lng, geometry, max_m):
            continue
        if any(_same_knoop(node, existing) for existing in result):
            continue
        progress = _geometry_progress_index(lat, lng, geometry)
        insert_at = len(result)
        for index, existing in enumerate(result):
            existing_progress = _geometry_progress_index(float(existing["lat"]), float(existing["lng"]), geometry)
            if existing_progress > progress:
                insert_at = index
                break
        result.insert(insert_at, dict(node))
        seen_numbers.add(number)
    return dedupe_revisited_numbers(result)


def _ensure_picks_in_chain(expanded: list[dict[str, Any]], picks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep user picks (original number/coords) in order; fill network nodes between them."""
    if not picks:
        return list(expanded)
    if not expanded:
        return [dict(p) for p in picks]

    by_geoid = {
        int(node["geoid"]): node
        for node in [*expanded, *picks]
        if node.get("geoid") is not None
    }
    result: list[dict[str, Any]] = []
    seen_geoids: set[int] = set()
    for pick_index, pick in enumerate(picks):
        pick_geo = _resolve_geoid(pick, by_geoid)
        if pick_geo is not None:
            seen_geoids.add(pick_geo)
        result.append(dict(pick))
        if pick_index >= len(picks) - 1:
            break
        nxt = picks[pick_index + 1]
        start_i = _index_of_pick_in_expanded(pick, expanded, by_geoid)
        end_i = _index_of_pick_in_expanded(nxt, expanded, by_geoid)
        if start_i < 0 or end_i < 0 or end_i <= start_i:
            continue
        for node in expanded[start_i + 1 : end_i]:
            if _same_knoop(node, pick) or _same_knoop(node, nxt):
                continue
            if result and _same_knoop(result[-1], node):
                continue
            geo = _resolve_geoid(node, by_geoid)
            if geo is not None and geo in seen_geoids:
                continue
            result.append(dict(node))
            if geo is not None:
                seen_geoids.add(geo)
    return result


def _geoid_for_number(
    number: Any,
    node: dict[str, Any],
    by_geoid: dict[int, dict[str, Any]],
) -> int | None:
    if number is None:
        return None
    best_id: int | None = None
    best_dist = float("inf")
    for geoid, candidate in by_geoid.items():
        if str(candidate.get("number")) != str(number):
            continue
        dist = haversine_m(node["lat"], node["lng"], candidate["lat"], candidate["lng"])
        if dist < best_dist:
            best_dist = dist
            best_id = int(geoid)
    return best_id if best_dist <= 250 else None


def _resolve_geoid(node: dict[str, Any], by_geoid: dict[int, dict[str, Any]]) -> int | None:
    raw = node.get("geoid")
    if raw is not None:
        try:
            return int(raw)
        except (TypeError, ValueError):
            pass
    return _geoid_for_number(node.get("number"), node, by_geoid)


def _shortest_path_avoid(
    adj: dict[int, list[tuple[int, float]]],
    start: int,
    goal: int,
    forbidden: set[int],
) -> list[int] | None:
    """Shortest path that skips already-visited knooppunten (goal always allowed)."""
    if start == goal:
        return [start]
    blocked = {geoid for geoid in forbidden if geoid not in {start, goal}}
    dist: dict[int, float] = {start: 0.0}
    prev: dict[int, int] = {}
    heap: list[tuple[float, int]] = [(0.0, start)]
    while heap:
        cost, node = heapq.heappop(heap)
        if cost > dist.get(node, float("inf")):
            continue
        if node == goal:
            break
        for neighbor, weight in adj.get(node, []):
            if neighbor in blocked:
                continue
            next_cost = cost + weight
            if next_cost < dist.get(neighbor, float("inf")):
                dist[neighbor] = next_cost
                prev[neighbor] = node
                heapq.heappush(heap, (next_cost, neighbor))
    if goal not in prev and start != goal:
        return None
    path: list[int] = []
    current = goal
    while True:
        path.append(current)
        if current == start:
            break
        current = prev.get(current, start)
        if current == goal:
            break
    path.reverse()
    return path


def _shortest_path_penalty(
    adj: dict[int, list[tuple[int, float]]],
    start: int,
    goal: int,
    discouraged: set[int],
    *,
    penalty_m: float = 2500.0,
) -> list[int] | None:
    """Prefer paths that avoid already visited knooppunten when a strict detour exists."""
    if start == goal:
        return [start]
    dist: dict[int, float] = {start: 0.0}
    prev: dict[int, int] = {}
    heap: list[tuple[float, int]] = [(0.0, start)]
    while heap:
        cost, node = heapq.heappop(heap)
        if cost > dist.get(node, float("inf")):
            continue
        if node == goal:
            break
        for neighbor, weight in adj.get(node, []):
            extra = penalty_m if neighbor in discouraged and neighbor not in {start, goal} else 0.0
            next_cost = cost + weight + extra
            if next_cost < dist.get(neighbor, float("inf")):
                dist[neighbor] = next_cost
                prev[neighbor] = node
                heapq.heappush(heap, (next_cost, neighbor))
    if goal not in prev and start != goal:
        return None
    path: list[int] = []
    current = goal
    while True:
        path.append(current)
        if current == start:
            break
        current = prev.get(current, start)
        if current == goal:
            break
    path.reverse()
    return path


def _shortest_path(adj: dict[int, list[tuple[int, float]]], start: int, goal: int) -> list[int]:
    if start == goal:
        return [start]
    dist: dict[int, float] = {start: 0.0}
    prev: dict[int, int] = {}
    heap: list[tuple[float, int]] = [(0.0, start)]
    while heap:
        cost, node = heapq.heappop(heap)
        if cost > dist.get(node, float("inf")):
            continue
        if node == goal:
            break
        for neighbor, weight in adj.get(node, []):
            next_cost = cost + weight
            if next_cost < dist.get(neighbor, float("inf")):
                dist[neighbor] = next_cost
                prev[neighbor] = node
                heapq.heappush(heap, (next_cost, neighbor))
    if goal not in prev and start != goal:
        return [start, goal]
    path: list[int] = []
    current = goal
    while True:
        path.append(current)
        if current == start:
            break
        current = prev.get(current, start)
        if current == goal:
            break
    path.reverse()
    return path


async def _fetch_bbox(bbox: tuple[float, float, float, float]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    min_lng, min_lat, max_lng, max_lat = bbox
    cql = f"BBOX(geom,{min_lng:.5f},{min_lat:.5f},{max_lng:.5f},{max_lat:.5f},'EPSG:4326')"
    nodes: list[dict[str, Any]] = []
    trajects: list[dict[str, Any]] = []
    try:
        async with client() as http:
            node_response = await http.get(
                WFS_URL,
                params={
                    "service": "WFS",
                    "version": "1.1.0",
                    "request": "GetFeature",
                    "typeName": "routes:knoop_fiets",
                    "outputFormat": "application/json",
                    "srsName": "EPSG:4326",
                    "maxFeatures": 800,
                    "cql_filter": f"{cql} AND knooptype=1",
                },
                timeout=14.0,
            )
            if node_response.status_code == 200:
                for feature in node_response.json().get("features") or []:
                    props = feature.get("properties") or {}
                    geom = feature.get("geometry") or {}
                    coords = geom.get("coordinates") or []
                    number = props.get("knoopnr")
                    if number is None or len(coords) < 2:
                        continue
                    nodes.append(
                        {
                            "id": str(feature.get("id") or f"knoop-{number}"),
                            "number": str(number),
                            "lat": float(coords[1]),
                            "lng": float(coords[0]),
                            "geoid": props.get("geoid"),
                            "network": props.get("naam") or "Fietsknooppuntennetwerk Vlaanderen",
                            "source": "Toerisme Vlaanderen",
                        }
                    )
            traject_response = await http.get(
                WFS_URL,
                params={
                    "service": "WFS",
                    "version": "1.1.0",
                    "request": "GetFeature",
                    "typeName": "routes:traject_fiets",
                    "outputFormat": "application/json",
                    "srsName": "EPSG:4326",
                    "maxFeatures": 1200,
                    "cql_filter": cql,
                },
                timeout=16.0,
            )
            if traject_response.status_code == 200:
                for feature in traject_response.json().get("features") or []:
                    props = feature.get("properties") or {}
                    geom = feature.get("geometry") or {}
                    raw_coords = geom.get("coordinates") or []
                    coordinates = (
                        [[float(c[1]), float(c[0])] for c in raw_coords if len(c) >= 2]
                        if raw_coords
                        else []
                    )
                    trajects.append(
                        {
                            "begin_geoid": props.get("begin_geoid"),
                            "end_geoid": props.get("end_geoid"),
                            "shape_length": props.get("shape_length") or 0,
                            "coordinates": coordinates,
                        }
                    )
    except Exception:
        return [], []
    return nodes, trajects


def attach_nearby(nodes: list[dict[str, Any]], pois: list[dict[str, Any]], radius_m: int = 550) -> list[dict[str, Any]]:
    for node in nodes:
        nearby = []
        for poi in pois:
            dist = haversine_m(node["lat"], node["lng"], poi["lat"], poi["lng"])
            if dist <= radius_m:
                nearby.append(
                    {
                        "name": poi["name"],
                        "kind": poi.get("kind_label") or poi.get("kind") or "plek",
                        "id": poi["id"],
                        "dist": round(dist),
                    }
                )
        nearby.sort(key=lambda item: item["dist"])
        node["nearby"] = nearby[:8]
    return nodes


def score_nodes_for_notes(nodes: list[dict[str, Any]], notes: str) -> list[dict[str, Any]]:
    needles = _note_needles(notes)
    for node in nodes:
        score = 0
        for place in node.get("nearby") or []:
            blob = f"{place['name']} {place['kind']}".lower()
            if any(needle in blob for needle in needles):
                score += 4
        node["match_score"] = score
    return nodes


def chain_from_notes(
    nodes: list[dict[str, Any]],
    start_lat: float,
    start_lng: float,
    target_km: int,
    end: tuple[float, float] | None = None,
) -> list[dict[str, Any]]:
    if len(nodes) < 3:
        return []
    start_node = nearest_node(nodes, start_lat, start_lng)
    max_out = min(max(target_km * 1000 / 2.3, 2500), 12000)
    ranked = sorted(
        (n for n in nodes if n["id"] != start_node["id"] and n.get("match_score", 0) > 0),
        key=lambda n: n.get("match_score", 0),
        reverse=True,
    )
    picked = [start_node]
    wanted = 4 if target_km < 30 else 5
    for node in ranked:
        dist = haversine_m(start_node["lat"], start_node["lng"], node["lat"], node["lng"])
        if dist < 600 or dist > max_out:
            continue
        if any(haversine_m(node["lat"], node["lng"], other["lat"], other["lng"]) < 850 for other in picked):
            continue
        picked.append(node)
        if len(picked) >= wanted:
            break
    if len(picked) < 3:
        return []
    if end:
        end_node = nearest_node(nodes, end[0], end[1])
        mid = [n for n in picked if n["id"] not in {start_node["id"], end_node["id"]}]
        mid.sort(key=lambda n: haversine_m(start_node["lat"], start_node["lng"], n["lat"], n["lng"]))
        return _dedupe_adjacent([start_node, *mid, end_node])
    others = [n for n in picked if n["id"] != start_node["id"]]
    others = _order_loop_nodes(start_node, others)
    return _dedupe_adjacent([start_node, *others, start_node])


def resolve_chain(raw_ids: list[Any], nodes: list[dict[str, Any]], start_node: dict[str, Any], loop: bool) -> list[dict[str, Any]]:
    by_id = {n["id"]: n for n in nodes}
    by_number: dict[str, list[dict[str, Any]]] = {}
    for node in nodes:
        by_number.setdefault(str(node["number"]), []).append(node)
    chain: list[dict[str, Any]] = []
    for raw in raw_ids:
        token = str(raw).strip()
        node = by_id.get(token)
        if not node:
            options = by_number.get(token) or []
            if options:
                node = min(
                    options,
                    key=lambda item: haversine_m(start_node["lat"], start_node["lng"], item["lat"], item["lng"]),
                )
        if node:
            chain.append(node)
    chain = _dedupe_adjacent(chain)
    if len(chain) < 3:
        return []
    if chain[0]["id"] != start_node["id"]:
        chain = [start_node, *chain]
    if loop and chain[-1]["id"] != start_node["id"]:
        chain.append(start_node)
    return _dedupe_adjacent(chain)


def _note_needles(notes: str) -> list[str]:
    text = (notes or "").lower()
    needles = [part.strip() for part in re.split(r"[,\s/]+", text) if len(part.strip()) >= 3]
    synonyms = {
        "cafe": ["cafe", "café", "koffie", "coffee"],
        "café": ["cafe", "café", "koffie"],
        "koffie": ["cafe", "café", "koffie"],
        "pub": ["pub", "bar", "bier"],
        "bar": ["bar", "pub"],
        "eten": ["restaurant", "café", "cafe"],
        "kasteel": ["kasteel", "burcht", "castle"],
        "kerk": ["kerk", "kathedraal", "abdij"],
    }
    expanded: set[str] = set(needles)
    for word in list(expanded):
        expanded.update(synonyms.get(word, []))
    return [item for item in expanded if item]


def nearest_node(nodes: list[dict[str, Any]], lat: float, lng: float) -> dict[str, Any]:
    return min(nodes, key=lambda n: haversine_m(lat, lng, n["lat"], n["lng"]))


def _along(start: dict[str, Any], end: dict[str, Any], node: dict[str, Any]) -> bool:
    via = haversine_m(start["lat"], start["lng"], node["lat"], node["lng"]) + haversine_m(
        node["lat"], node["lng"], end["lat"], end["lng"]
    )
    direct = haversine_m(start["lat"], start["lng"], end["lat"], end["lng"]) or 1
    return via < direct * 1.28 and via - direct < 8000


def _dedupe_adjacent(chain: list[dict[str, Any]]) -> list[dict[str, Any]]:
    cleaned: list[dict[str, Any]] = []
    for node in chain:
        if cleaned and cleaned[-1]["id"] == node["id"]:
            continue
        cleaned.append(node)
    return cleaned


def _loop_tour_km(start: dict[str, Any], nodes: list[dict[str, Any]]) -> float:
    if not nodes:
        return 0.0
    total = haversine_m(start["lat"], start["lng"], nodes[0]["lat"], nodes[0]["lng"])
    for index in range(len(nodes) - 1):
        left = nodes[index]
        right = nodes[index + 1]
        total += haversine_m(left["lat"], left["lng"], right["lat"], right["lng"])
    last = nodes[-1]
    total += haversine_m(last["lat"], last["lng"], start["lat"], start["lng"])
    return total


def _order_loop_nodes(start: dict[str, Any], nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Order loop picks to reduce back-and-forth over the same corridors."""
    if len(nodes) <= 1:
        return list(nodes)

    remaining = {node["id"]: node for node in nodes}
    ordered: list[dict[str, Any]] = []
    current = start
    while remaining:
        best = min(
            remaining.values(),
            key=lambda node: haversine_m(current["lat"], current["lng"], node["lat"], node["lng"]),
        )
        ordered.append(best)
        del remaining[best["id"]]
        current = best

    return _improve_loop_order(start, ordered)


def _improve_loop_order(start: dict[str, Any], nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """2-opt pass to uncross a loop and avoid obvious backtracking."""
    if len(nodes) < 3:
        return list(nodes)
    best = list(nodes)
    best_len = _loop_tour_km(start, best)
    improved = True
    while improved:
        improved = False
        for i in range(len(best) - 1):
            for j in range(i + 2, len(best)):
                candidate = best[: i + 1] + list(reversed(best[i + 1 : j + 1])) + best[j + 1 :]
                candidate_len = _loop_tour_km(start, candidate)
                if candidate_len + 80 < best_len:
                    best = candidate
                    best_len = candidate_len
                    improved = True
    return best


def _bbox(lat: float, lng: float, radius_m: int) -> tuple[float, float, float, float]:
    dlat = radius_m / 111_000
    dlng = radius_m / (111_000 * max(0.2, math.cos(math.radians(lat))))
    return lng - dlng, lat - dlat, lng + dlng, lat + dlat
