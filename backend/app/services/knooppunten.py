from __future__ import annotations

import asyncio
import heapq
import math
import re
import time
from typing import Any

from app.http import client
from app.services.geo import (
    bearing,
    distance_point_to_geometry,
    geometry_length_m,
    haversine_m,
    midpoint_progress_on_loop,
    point_on_geometry_at_progress,
    point_to_segment_m,
    snap_point_on_geometry_with_progress,
)
from app.services.pois import _overpass

WFS_URL = "https://geodata.toerismevlaanderen.be/geoserver/wfs"

# Caches voor snelle magenta-legs (blijven op officieel netwerk).
_BBOX_CACHE: dict[str, tuple[float, list[dict[str, Any]], list[dict[str, Any]]]] = {}
_LEG_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
# Samengevoegd WFS-netwerk in geheugen: legs na warmte = milliseconden, geen WFS per klik.
_LIVE_NODES: dict[int, dict[str, Any]] = {}
_LIVE_TRAJECTS: dict[tuple[int, int], dict[str, Any]] = {}
_LIVE_TOUCHED = 0.0
_BBOX_TTL_S = 20 * 60
_LEG_TTL_S = 45 * 60
_LIVE_TTL_S = 45 * 60


def _cache_prune(cache: dict[str, tuple], keep: int = 80) -> None:
    if len(cache) <= keep:
        return
    oldest = sorted(cache.items(), key=lambda item: item[1][0])[: max(1, len(cache) - keep)]
    for key, _ in oldest:
        cache.pop(key, None)


def _ingest_live_network(
    nodes: list[dict[str, Any]],
    trajects: list[dict[str, Any]],
) -> None:
    """Voeg WFS-nodes/trajecten toe aan het gedeelde live-netwerk."""
    global _LIVE_TOUCHED
    if len(_LIVE_NODES) > 12_000 or len(_LIVE_TRAJECTS) > 24_000:
        _LIVE_NODES.clear()
        _LIVE_TRAJECTS.clear()
    for node in nodes or []:
        geoid = node.get("geoid")
        if geoid is None:
            continue
        try:
            key = int(geoid)
        except (TypeError, ValueError):
            continue
        prev = _LIVE_NODES.get(key)
        _LIVE_NODES[key] = {**(prev or {}), **node}
    for traject in trajects or []:
        begin = traject.get("begin_geoid")
        end = traject.get("end_geoid")
        if begin is None or end is None:
            continue
        try:
            left, right = int(begin), int(end)
        except (TypeError, ValueError):
            continue
        edge = (min(left, right), max(left, right))
        prev = _LIVE_TRAJECTS.get(edge)
        coords = traject.get("coordinates") or []
        if prev and len(prev.get("coordinates") or []) >= len(coords):
            continue
        _LIVE_TRAJECTS[edge] = traject
    _LIVE_TOUCHED = time.monotonic()


def _live_snapshot() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not _LIVE_TRAJECTS:
        return [], []
    # Geen harde TTL-leegmaak: dat maakt volgende klikken plots traag (opnieuw WFS).
    return list(_LIVE_NODES.values()), list(_LIVE_TRAJECTS.values())


def _try_leg_from_live(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any] | None:
    """Bouw A→B uit het al gewarmde netwerk (geen netwerk-HTTP)."""
    nodes, trajects = _live_snapshot()
    if not trajects:
        return None
    try:
        left_id = int(left["geoid"]) if left.get("geoid") is not None else None
        right_id = int(right["geoid"]) if right.get("geoid") is not None else None
    except (TypeError, ValueError):
        return None
    if left_id is None or right_id is None:
        return None
    adj = build_adjacency(trajects)
    if left_id not in adj or right_id not in adj:
        return None
    if len(_shortest_path(adj, left_id, right_id)) < 2:
        return None
    # Eindpunten mogen uit de klik komen als ze nog niet als node in live staan.
    by_geoid = {int(n["geoid"]): n for n in nodes if n.get("geoid") is not None}
    by_geoid[left_id] = {**by_geoid.get(left_id, {}), **left}
    by_geoid[right_id] = {**by_geoid.get(right_id, {}), **right}
    merged_nodes = list(by_geoid.values())
    chain = [dict(left), dict(right)]
    enriched = enrich_chain_geoids(chain, merged_nodes, trajects)
    if len(enriched) < 2:
        enriched = chain
    return _assemble_network_leg_geometry(enriched, merged_nodes, trajects)


_WARM_STAR_INFLIGHT: set[int] = set()
_WARM_AREA_INFLIGHT: set[str] = set()


async def fetch_direct_edge(
    left_id: int,
    right_id: int,
) -> list[dict[str, Any]]:
    """Eén WFS-call voor het directe traject A↔B (typische buur-klik)."""
    if left_id == right_id:
        return []
    cql = (
        f"(begin_geoid={left_id} AND end_geoid={right_id}) OR "
        f"(begin_geoid={right_id} AND end_geoid={left_id})"
    )
    try:
        async with client() as http:
            response = await http.get(
                WFS_URL,
                params={
                    "service": "WFS",
                    "version": "1.1.0",
                    "request": "GetFeature",
                    "typeName": "routes:traject_fiets",
                    "outputFormat": "application/json",
                    "srsName": "EPSG:4326",
                    "maxFeatures": 8,
                    "cql_filter": cql,
                },
                timeout=6.0,
            )
        if response.status_code != 200:
            return []
        trajects = [
            _traject_from_feature(feature) for feature in response.json().get("features") or []
        ]
        _ingest_live_network([], trajects)
        return trajects
    except Exception:
        return []


async def prefetch_geoid_star(geoid: int | None) -> None:
    """Laad alle trajecten rond één knoop — volgende klik zit vaak al in live-cache."""
    if geoid is None:
        return
    try:
        geo = int(geoid)
    except (TypeError, ValueError):
        return
    if geo in _WARM_STAR_INFLIGHT:
        return
    _WARM_STAR_INFLIGHT.add(geo)
    try:
        trajects = await _fetch_trajects_for_geoids({geo})
        neigh: set[int] = set()
        for traject in trajects:
            begin = traject.get("begin_geoid")
            end = traject.get("end_geoid")
            if begin is not None:
                neigh.add(int(begin))
            if end is not None:
                neigh.add(int(end))
        missing = {g for g in neigh if g not in _LIVE_NODES}
        if missing:
            await _fetch_nodes_for_geoids(missing | {geo})
        else:
            await _fetch_nodes_for_geoids({geo})
    except Exception:
        pass
    finally:
        _WARM_STAR_INFLIGHT.discard(geo)


async def warm_area_network(lat: float, lng: float, radius_m: int = 12000) -> None:
    """Warm bbox op de achtergrond (blokkeert knooppunten-API niet)."""
    radius_m = max(4000, min(int(radius_m), 16000))
    bbox = _bbox(lat, lng, radius_m)
    key = _bbox_cache_key(bbox)
    if key in _WARM_AREA_INFLIGHT:
        return
    _WARM_AREA_INFLIGHT.add(key)
    try:
        await _fetch_bbox(bbox)
    except Exception:
        pass
    finally:
        _WARM_AREA_INFLIGHT.discard(key)


def _leg_cache_key(left: dict[str, Any], right: dict[str, Any]) -> str:
    def part(node: dict[str, Any]) -> str:
        geoid = node.get("geoid")
        if geoid is not None and str(geoid) != "":
            return f"g{geoid}"
        return (
            f"n{node.get('number')}:"
            f"{round(float(node['lat']), 4)},{round(float(node['lng']), 4)}"
        )

    return f"{part(left)}|{part(right)}"


def _bbox_cache_key(bbox: tuple[float, float, float, float]) -> str:
    min_lng, min_lat, max_lng, max_lat = bbox
    # Grid ~1 km zodat nabije klikken dezelfde WFS-tegel hergebruiken.
    return (
        f"{round(min_lng, 2)},{round(min_lat, 2)},"
        f"{round(max_lng, 2)},{round(max_lat, 2)}"
    )


async def fetch_viewport_network(
    lat: float,
    lng: float,
    radius_m: int = 12000,
) -> dict[str, Any]:
    """Knooppunten + trajecten voor de viewport — client kleurt magenta lokaal (zoals fietsknooppunten)."""
    radius_m = max(4000, min(int(radius_m), 16000))
    bbox = _bbox(lat, lng, radius_m)
    nodes, trajects = await _fetch_bbox(bbox)
    _ingest_live_network(nodes, trajects)
    edges: list[dict[str, Any]] = []
    for traject in trajects:
        begin = traject.get("begin_geoid")
        end = traject.get("end_geoid")
        coords = traject.get("coordinates") or []
        if begin is None or end is None or len(coords) < 2:
            continue
        try:
            a, b = int(begin), int(end)
        except (TypeError, ValueError):
            continue
        length = float(traject.get("shape_length") or 0.0)
        if length <= 0:
            length = 0.0
            for index in range(1, len(coords)):
                length += haversine_m(
                    coords[index - 1][0],
                    coords[index - 1][1],
                    coords[index][0],
                    coords[index][1],
                )
        edges.append(
            {
                "a": a,
                "b": b,
                "m": round(length, 1),
                "coords": [[round(float(p[0]), 6), round(float(p[1]), 6)] for p in coords],
            }
        )
    out_nodes = [
        {
            "id": str(node.get("id") or ""),
            "number": str(node.get("number") or ""),
            "lat": float(node["lat"]),
            "lng": float(node["lng"]),
            "geoid": node.get("geoid"),
            "network": node.get("network"),
        }
        for node in nodes
        if node.get("geoid") is not None and node.get("lat") is not None
    ]
    return {"nodes": out_nodes, "edges": edges}


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
    # Nodes meteen; traject-warmte op de achtergrond → snellere UI + volgende legs.
    wfs = await _from_wfs(points, radius_m)
    asyncio.create_task(warm_area_network(lat, lng, min(radius_m, 14000)))
    dominant = dominant_network_near(wfs, lat, lng, min(radius_m, 12000))
    if dominant:
        wfs = [node for node in wfs if node.get("network") == dominant]
    osm: list[dict[str, Any]] = []
    if len(wfs) < 6:
        osm = await _from_osm(lat, lng, radius_m, extra)
    nodes: list[dict[str, Any]] = []
    seen: set[str] = set()
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


def _is_network_leaf(
    node: dict[str, Any],
    adj: dict[int, list[tuple[int, float]]],
    by_geoid: dict[int, dict[str, Any]],
) -> bool:
    geoid = _resolve_geoid(node, by_geoid)
    return geoid is not None and len(adj.get(geoid, [])) <= 1


def picks_without_backtrack(
    picks: list[dict[str, Any]],
    adj: dict[int, list[tuple[int, float]]],
    by_geoid: dict[int, dict[str, Any]],
    *,
    close_loop: bool = False,
) -> list[dict[str, Any]]:
    """Bewaar klikvolgorde; sla alleen tussenliggende doodlopende takken over.

    Geen herordening: de magenta lijn volgt de gekozen knooppunten in volgorde
    langs het officiële netwerk. Alleen degree≤1 knopen in het midden (sporen
    waar je sowieso moet terugkeren) worden weggelaten.
    """
    cleaned = _dedupe_adjacent(picks)
    if len(cleaned) <= 2 or not adj:
        return cleaned

    if close_loop:
        route = [
            node
            for node in cleaned
            if not _is_network_leaf(node, adj, by_geoid)
        ]
        route = _dedupe_adjacent(route)
        return route if len(route) >= 2 else cleaned

    route = [cleaned[0]]
    for node in cleaned[1:-1]:
        if _is_network_leaf(node, adj, by_geoid):
            continue
        route.append(node)
    route.append(cleaned[-1])
    route = _dedupe_adjacent(route)
    return route if len(route) >= 2 else cleaned


def _node_protect_key(node: dict[str, Any]) -> str | None:
    geoid = node.get("geoid")
    if geoid is not None and str(geoid) != "":
        try:
            return f"g{int(geoid)}"
        except (TypeError, ValueError):
            pass
    number = node.get("number")
    if number is None or number == "":
        return None
    try:
        lat = round(float(node["lat"]), 4)
        lng = round(float(node["lng"]), 4)
    except (KeyError, TypeError, ValueError):
        return f"n{number}"
    return f"n{number}:{lat},{lng}"


def strip_out_and_back_nodes(
    chain: list[dict[str, Any]],
    protect: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Verwijder heen-en-terug-sporen (A → B → A), behalve gekozen knooppunten."""
    protected = {key for key in (protect or set()) if key}
    seq = list(chain)
    changed = True
    while changed and len(seq) >= 3:
        changed = False
        index = 0
        while index < len(seq) - 2:
            found = False
            for end in range(len(seq) - 1, index + 1, -1):
                if not _same_knoop(seq[index], seq[end]):
                    continue
                inner = seq[index + 1 : end]
                if not inner:
                    continue
                if protected and any(
                    (_node_protect_key(node) in protected) for node in inner
                ):
                    continue
                if all(
                    _same_knoop(inner[pos], inner[len(inner) - 1 - pos])
                    for pos in range(len(inner))
                ):
                    del seq[index + 1 : end + 1]
                    changed = True
                    found = True
                    break
            if found:
                break
            index += 1
    return seq if len(seq) >= 2 else list(chain)


async def prune_chain_backtracking(
    chain: list[dict[str, Any]],
    *,
    close_loop: bool = False,
) -> list[dict[str, Any]]:
    if len(chain) < 3:
        return list(chain)
    try:
        network_nodes, trajects = await fetch_network_for_chain(chain)
    except Exception:
        return list(chain)
    if not trajects:
        return list(chain)
    by_geoid = {
        int(node["geoid"]): node for node in network_nodes if node.get("geoid") is not None
    }
    enriched = enrich_chain_geoids(chain, network_nodes, trajects)
    adj = build_adjacency(trajects)
    pruned = picks_without_backtrack(enriched, adj, by_geoid, close_loop=close_loop)
    return pruned if len(pruned) >= 2 else list(chain)


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


async def fetch_network_for_geometry(
    geometry: list[list[float]],
    padding_m: int = 3500,
    network: str | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not geometry:
        return [], []
    lats = [p[0] for p in geometry if len(p) >= 2]
    lngs = [p[1] for p in geometry if len(p) >= 2]
    if not lats:
        return [], []
    min_lat, max_lat = min(lats), max(lats)
    min_lng, max_lng = min(lngs), max(lngs)
    pad_lat = padding_m / 111_000
    pad_lng = padding_m / (111_000 * max(0.2, math.cos(math.radians((min_lat + max_lat) / 2))))
    bbox = (min_lng - pad_lng, min_lat - pad_lat, max_lng + pad_lng, max_lat + pad_lat)
    nodes, trajects = await _fetch_bbox(bbox)
    return filter_network_data(nodes, trajects, network)


def _merge_network_nodes(
    left: list[dict[str, Any]],
    right: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    by_key: dict[str, dict[str, Any]] = {}
    for node in [*left, *right]:
        key = str(node.get("id") or f"{node.get('number')}|{round(float(node['lat']), 4)}")
        by_key[key] = node
    return list(by_key.values())


def _merge_chain_picks_into_nodes(
    nodes: list[dict[str, Any]],
    chain: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Zorg dat expliciet gekozen knooppunten (geoid/coords) altijd in het netwerk zitten."""
    by_geoid = {int(node["geoid"]): node for node in nodes if node.get("geoid") is not None}
    merged = list(nodes)
    for pick in chain:
        raw = pick.get("geoid")
        if raw is None:
            continue
        try:
            geo = int(raw)
        except (TypeError, ValueError):
            continue
        if geo in by_geoid:
            continue
        merged.append(
            {
                "id": pick.get("id") or f"pick-{geo}",
                "number": str(pick.get("number") or ""),
                "lat": float(pick["lat"]),
                "lng": float(pick["lng"]),
                "geoid": geo,
                "network": pick.get("network"),
                "source": pick.get("source") or "gebruiker",
            }
        )
        by_geoid[geo] = merged[-1]
    return merged


def _merge_trajects(
    left: list[dict[str, Any]],
    right: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    seen: set[tuple[int, int]] = set()
    merged: list[dict[str, Any]] = []
    for traject in [*left, *right]:
        begin = traject.get("begin_geoid")
        end = traject.get("end_geoid")
        if begin is None or end is None:
            continue
        key = (min(int(begin), int(end)), max(int(begin), int(end)))
        if key in seen:
            continue
        seen.add(key)
        merged.append(traject)
    return merged


def _trajects_for_connected_nodes(
    nodes: list[dict[str, Any]],
    pool_trajects: list[dict[str, Any]],
    all_nodes_by_geoid: dict[int, dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Trajecten waarmee alle knopen (incl. picks) in het netwerk verbonden zijn."""
    geoids: set[int] = {int(node["geoid"]) for node in nodes if node.get("geoid") is not None}
    if not geoids:
        return list(nodes), []
    seen_edges: set[tuple[int, int]] = set()
    selected: list[dict[str, Any]] = []
    queue = list(geoids)
    while queue:
        geo = queue.pop()
        for traject in pool_trajects:
            begin = traject.get("begin_geoid")
            end = traject.get("end_geoid")
            if begin is None or end is None:
                continue
            left, right = int(begin), int(end)
            if geo not in {left, right}:
                continue
            edge = (min(left, right), max(left, right))
            if edge in seen_edges:
                continue
            seen_edges.add(edge)
            selected.append(traject)
            other = right if left == geo else left
            if other not in geoids:
                geoids.add(other)
                queue.append(other)
    merged_nodes = list(nodes)
    by_geoid = {int(node["geoid"]): node for node in merged_nodes if node.get("geoid") is not None}
    for geo in geoids:
        if geo in by_geoid:
            continue
        official = all_nodes_by_geoid.get(geo)
        if official:
            merged_nodes.append(official)
            by_geoid[geo] = official
    return merged_nodes, selected


async def _fetch_trajects_for_geoids(geoids: set[int]) -> list[dict[str, Any]]:
    if not geoids:
        return []
    trajects: list[dict[str, Any]] = []
    geoids_list = sorted(geoids)
    batches = [geoids_list[offset : offset + 16] for offset in range(0, len(geoids_list), 16)]

    async def one_batch(batch: list[int]) -> list[dict[str, Any]]:
        cql = " OR ".join(f"(begin_geoid={geo} OR end_geoid={geo})" for geo in batch)
        try:
            async with client() as http:
                response = await http.get(
                    WFS_URL,
                    params={
                        "service": "WFS",
                        "version": "1.1.0",
                        "request": "GetFeature",
                        "typeName": "routes:traject_fiets",
                        "outputFormat": "application/json",
                        "srsName": "EPSG:4326",
                        "maxFeatures": 600,
                        "cql_filter": cql,
                    },
                    timeout=10.0,
                )
            if response.status_code != 200:
                return []
            return [_traject_from_feature(feature) for feature in response.json().get("features") or []]
        except Exception:
            return []

    parts = await asyncio.gather(*(one_batch(batch) for batch in batches))
    for part in parts:
        trajects.extend(part)
    _ingest_live_network([], trajects)
    return trajects


async def _fetch_nodes_for_geoids(geoids: set[int]) -> list[dict[str, Any]]:
    if not geoids:
        return []
    nodes: list[dict[str, Any]] = []
    geoids_list = sorted(geoids)
    batches = [geoids_list[offset : offset + 20] for offset in range(0, len(geoids_list), 20)]

    async def one_batch(batch: list[int]) -> list[dict[str, Any]]:
        cql = " OR ".join(f"geoid={geo}" for geo in batch)
        try:
            async with client() as http:
                response = await http.get(
                    WFS_URL,
                    params={
                        "service": "WFS",
                        "version": "1.1.0",
                        "request": "GetFeature",
                        "typeName": "routes:knoop_fiets",
                        "outputFormat": "application/json",
                        "srsName": "EPSG:4326",
                        "maxFeatures": 200,
                        "cql_filter": f"({cql}) AND knooptype=1",
                    },
                    timeout=10.0,
                )
            if response.status_code != 200:
                return []
            return [_knoop_from_feature(feature) for feature in response.json().get("features") or []]
        except Exception:
            return []

    parts = await asyncio.gather(*(one_batch(batch) for batch in batches))
    for part in parts:
        nodes.extend(part)
    _ingest_live_network(nodes, [])
    return nodes


def _traject_from_feature(feature: dict[str, Any]) -> dict[str, Any]:
    props = feature.get("properties") or {}
    geom = feature.get("geometry") or {}
    raw_coords = geom.get("coordinates") or []
    coordinates = (
        [[float(c[1]), float(c[0])] for c in raw_coords if len(c) >= 2] if raw_coords else []
    )
    return {
        "begin_geoid": props.get("begin_geoid"),
        "end_geoid": props.get("end_geoid"),
        "shape_length": props.get("shape_length") or 0,
        "coordinates": coordinates,
    }


def _knoop_from_feature(feature: dict[str, Any]) -> dict[str, Any]:
    props = feature.get("properties") or {}
    geom = feature.get("geometry") or {}
    coords = geom.get("coordinates") or []
    number = props.get("knoopnr")
    return {
        "id": str(feature.get("id") or f"knoop-{number}"),
        "number": str(number),
        "lat": float(coords[1]),
        "lng": float(coords[0]),
        "geoid": props.get("geoid"),
        "network": props.get("naam") or "Fietsknooppuntennetwerk Vlaanderen",
        "source": "Toerisme Vlaanderen",
    }


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
    all_nodes_by_geoid = {
        int(node["geoid"]): node for node in nodes if node.get("geoid") is not None
    }
    networks: set[str] = set()
    inferred = infer_chain_network(chain, nodes)
    if inferred:
        networks.add(inferred)
    for pick in chain:
        network = pick.get("network")
        if network:
            networks.add(str(network))
    if len(networks) <= 1:
        nodes, trajects = filter_network_data(nodes, trajects, inferred or next(iter(networks), None))
    else:
        filtered_nodes = [node for node in nodes if node.get("network") in networks]
        geoids = {int(node["geoid"]) for node in filtered_nodes if node.get("geoid") is not None}
        filtered_trajects = [
            traject
            for traject in trajects
            if traject.get("begin_geoid") is not None
            and traject.get("end_geoid") is not None
            and int(traject["begin_geoid"]) in geoids
            and int(traject["end_geoid"]) in geoids
        ]
        nodes, trajects = filtered_nodes, filtered_trajects
    nodes = _merge_chain_picks_into_nodes(nodes, chain)
    pick_geoids = {
        int(pick["geoid"])
        for pick in chain
        if pick.get("geoid") is not None
    }
    if pick_geoids:
        extra_trajects = await _fetch_trajects_for_geoids(pick_geoids)
        trajects = _merge_trajects(trajects, extra_trajects)
        endpoint_geoids = {
            int(t["begin_geoid"])
            for t in extra_trajects
            if t.get("begin_geoid") is not None
        } | {
            int(t["end_geoid"])
            for t in extra_trajects
            if t.get("end_geoid") is not None
        }
        missing = endpoint_geoids - {int(n["geoid"]) for n in nodes if n.get("geoid") is not None}
        if missing:
            for node in await _fetch_nodes_for_geoids(missing):
                all_nodes_by_geoid[int(node["geoid"])] = node
                nodes.append(node)
        geoids = {int(n["geoid"]) for n in nodes if n.get("geoid") is not None}
        for _ in range(6):
            round_trajects = await _fetch_trajects_for_geoids(geoids)
            trajects = _merge_trajects(trajects, round_trajects)
            nodes, trajects = _trajects_for_connected_nodes(nodes, trajects, all_nodes_by_geoid)
            new_geoids = {int(n["geoid"]) for n in nodes if n.get("geoid") is not None}
            missing_nodes = new_geoids - set(all_nodes_by_geoid.keys())
            if missing_nodes:
                for node in await _fetch_nodes_for_geoids(missing_nodes):
                    all_nodes_by_geoid[int(node["geoid"])] = node
                    nodes.append(node)
            if new_geoids == geoids:
                break
            geoids = new_geoids
    else:
        nodes, trajects = _trajects_for_connected_nodes(nodes, trajects, all_nodes_by_geoid)
    return nodes, trajects


async def fetch_network_geoid_expand(
    left: dict[str, Any],
    right: dict[str, Any],
    *,
    rounds: int = 3,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Snel pad: groei het netwerk vanuit twee geoids (geen zware bbox)."""
    try:
        left_id = int(left["geoid"]) if left.get("geoid") is not None else None
        right_id = int(right["geoid"]) if right.get("geoid") is not None else None
    except (TypeError, ValueError):
        return [], []
    if left_id is None or right_id is None:
        return [], []

    seed = {left_id, right_id}
    nodes = await _fetch_nodes_for_geoids(seed)
    trajects = await _fetch_trajects_for_geoids(seed)
    by_geoid: dict[int, dict[str, Any]] = {
        int(node["geoid"]): node for node in nodes if node.get("geoid") is not None
    }
    known = set(seed)

    for _ in range(max(1, rounds)):
        adj = build_adjacency(trajects)
        if left_id in adj and right_id in adj:
            path = _shortest_path(adj, left_id, right_id)
            if len(path) >= 2:
                missing = {geo for geo in path if geo not in by_geoid}
                if missing:
                    for node in await _fetch_nodes_for_geoids(missing):
                        if node.get("geoid") is None:
                            continue
                        by_geoid[int(node["geoid"])] = node
                # Zorg dat eindpunten (met klik-coords) in de set zitten.
                for pick in (left, right):
                    geo = pick.get("geoid")
                    if geo is None:
                        continue
                    by_geoid[int(geo)] = {**by_geoid.get(int(geo), {}), **pick}
                return list(by_geoid.values()), trajects

        frontier: set[int] = set()
        for traject in trajects:
            begin = traject.get("begin_geoid")
            end = traject.get("end_geoid")
            if begin is not None:
                frontier.add(int(begin))
            if end is not None:
                frontier.add(int(end))
        nxt = sorted(frontier - known)[:28]
        if not nxt:
            break
        known.update(nxt)
        extra_trajects = await _fetch_trajects_for_geoids(set(nxt))
        trajects = _merge_trajects(trajects, extra_trajects)
        for node in await _fetch_nodes_for_geoids(set(nxt)):
            if node.get("geoid") is None:
                continue
            by_geoid[int(node["geoid"])] = node

    return list(by_geoid.values()), trajects


async def fetch_network_for_leg(left: dict[str, Any], right: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Lichtere netwerk-fetch voor één magenta-segment (minder WFS-rondes dan volle chain)."""
    chain = [left, right]
    gap = haversine_m(float(left["lat"]), float(left["lng"]), float(right["lat"]), float(right["lng"]))
    # Compactere bbox — sneller, genoeg voor typische klik-afstanden.
    pad_m = max(2500, min(9000, gap * 0.7 + 1500))
    min_lat = min(float(left["lat"]), float(right["lat"]))
    max_lat = max(float(left["lat"]), float(right["lat"]))
    min_lng = min(float(left["lng"]), float(right["lng"]))
    max_lng = max(float(left["lng"]), float(right["lng"]))
    pad_lat = pad_m / 111_000
    pad_lng = pad_m / (111_000 * max(0.2, math.cos(math.radians((min_lat + max_lat) / 2))))
    bbox = (min_lng - pad_lng, min_lat - pad_lat, max_lng + pad_lng, max_lat + pad_lat)

    nodes, trajects = await _fetch_bbox(bbox)
    all_nodes_by_geoid = {
        int(node["geoid"]): node for node in nodes if node.get("geoid") is not None
    }
    inferred = infer_chain_network(chain, nodes)
    network = left.get("network") or right.get("network") or inferred
    nodes, trajects = filter_network_data(nodes, trajects, network)
    nodes = _merge_chain_picks_into_nodes(nodes, chain)

    pick_geoids = {
        int(pick["geoid"])
        for pick in chain
        if pick.get("geoid") is not None
    }
    if not pick_geoids:
        for pick in chain:
            if pick.get("geoid") is not None:
                continue
            number = str(pick.get("number") or "")
            for node in nodes:
                if str(node.get("number")) != number:
                    continue
                if haversine_m(float(pick["lat"]), float(pick["lng"]), float(node["lat"]), float(node["lng"])) <= 250:
                    pick["geoid"] = node.get("geoid")
                    if node.get("geoid") is not None:
                        pick_geoids.add(int(node["geoid"]))
                    break

    if pick_geoids:
        extra = await _fetch_trajects_for_geoids(pick_geoids)
        trajects = _merge_trajects(trajects, extra)
        nodes, trajects = _trajects_for_connected_nodes(nodes, trajects, all_nodes_by_geoid)
        adj = build_adjacency(trajects)
        ends = list(pick_geoids)
        path_ok = False
        if len(ends) >= 2 and ends[0] in adj and ends[1] in adj:
            path_ok = len(_shortest_path(adj, ends[0], ends[1])) >= 2
        # Geen tweede massale geoid-ronde: dat maakt magenta traag. Fallback gebeurt elders.
        if not path_ok and len(nodes) <= 40:
            geoids = {int(n["geoid"]) for n in nodes if n.get("geoid") is not None}
            round_trajects = await _fetch_trajects_for_geoids(geoids)
            trajects = _merge_trajects(trajects, round_trajects)
            nodes, trajects = _trajects_for_connected_nodes(nodes, trajects, all_nodes_by_geoid)
    else:
        nodes, trajects = _trajects_for_connected_nodes(nodes, trajects, all_nodes_by_geoid)
    return nodes, trajects


def _assemble_network_leg_geometry(
    enriched: list[dict[str, Any]],
    network_nodes: list[dict[str, Any]],
    trajects: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if len(enriched) < 2 or not trajects:
        return None
    by_edge, edge_length, adj = index_trajects(trajects)
    by_geoid: dict[int, dict[str, Any]] = {
        int(node["geoid"]): node for node in network_nodes if node.get("geoid") is not None
    }
    for node in enriched:
        if node.get("geoid") is not None:
            by_geoid[int(node["geoid"])] = node

    expanded = expand_chain(enriched, network_nodes, trajects)
    if len(expanded) < 2:
        expanded = list(enriched)
    protect = {key for key in (_node_protect_key(node) for node in enriched) if key}
    expanded = strip_out_and_back_nodes(expanded, protect=protect)

    pieces: list[list[list[float]]] = []
    total_m = 0.0
    for index in range(len(expanded) - 1):
        piece, length = geometry_between_nodes(
            expanded[index],
            expanded[index + 1],
            by_edge,
            edge_length,
            adj,
            by_geoid,
        )
        if not piece or len(piece) < 2:
            piece, length = geometry_through_network(
                expanded[index],
                expanded[index + 1],
                by_edge,
                edge_length,
                adj,
                by_geoid,
            )
        if not piece or len(piece) < 2:
            return None
        pieces.append(piece)
        if length > 0:
            total_m += length
        else:
            for step in range(1, len(piece)):
                total_m += haversine_m(
                    piece[step - 1][0],
                    piece[step - 1][1],
                    piece[step][0],
                    piece[step][1],
                )

    geometry = _merge_geometry_parts(pieces)
    if len(geometry) < 2:
        return None

    start = enriched[0]
    end = enriched[-1]
    if geometry and haversine_m(float(start["lat"]), float(start["lng"]), geometry[0][0], geometry[0][1]) <= 120:
        geometry[0] = [float(start["lat"]), float(start["lng"])]
    if geometry and haversine_m(float(end["lat"]), float(end["lng"]), geometry[-1][0], geometry[-1][1]) <= 120:
        geometry[-1] = [float(end["lat"]), float(end["lng"])]

    via: list[dict[str, Any]] = []
    for node in expanded:
        item = {
            "id": str(node.get("id") or ""),
            "number": str(node.get("number") or ""),
            "lat": float(node["lat"]),
            "lng": float(node["lng"]),
            "network": node.get("network"),
            "geoid": node.get("geoid"),
            "on_route": True,
        }
        if via and _same_knoop(via[-1], item):
            continue
        via.append(item)

    return {
        "geometry": geometry,
        "distance_m": float(total_m),
        "duration_s": max(60.0, float(total_m) / 3.9),
        "via_knooppunten": via,
    }


async def network_leg(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    """Officiële knooppunten-trajectgeometrie A→B (via tussenliggende knoops). Geen vrije OSRM."""
    if _same_knoop(left, right):
        pt = [float(left["lat"]), float(left["lng"])]
        return {
            "geometry": [pt, pt],
            "distance_m": 0.0,
            "duration_s": 0.0,
            "via_knooppunten": [dict(left)],
        }

    cache_key = _leg_cache_key(left, right)
    cached = _LEG_CACHE.get(cache_key)
    if cached and time.monotonic() - cached[0] <= _LEG_TTL_S:
        return dict(cached[1])

    chain = [dict(left), dict(right)]
    result: dict[str, Any] | None = None

    # 0) Al gewarmd netwerk — typisch <20 ms.
    try:
        result = _try_leg_from_live(chain[0], chain[1])
    except Exception:
        result = None

    left_geo = chain[0].get("geoid")
    right_geo = chain[1].get("geoid")
    left_id = right_id = None
    try:
        if left_geo is not None:
            left_id = int(left_geo)
        if right_geo is not None:
            right_id = int(right_geo)
    except (TypeError, ValueError):
        left_id = right_id = None

    # 1) Directe buur-edge (één snelle WFS) — de meeste klikken.
    if result is None and left_id is not None and right_id is not None:
        try:
            edge = await fetch_direct_edge(left_id, right_id)
            if edge:
                result = _try_leg_from_live(chain[0], chain[1])
                if result is None:
                    # Geen extra node-WFS: klik-coords volstaan voor assemble.
                    _ingest_live_network(chain, edge)
                    result = _assemble_network_leg_geometry(chain, chain, edge)
        except Exception:
            result = None

    # 2) Geoid-expansie als A en B niet direct verbonden zijn.
    if result is None and left_id is not None and right_id is not None:
        try:
            network_nodes, trajects = await fetch_network_geoid_expand(
                chain[0], chain[1], rounds=2
            )
            if trajects:
                _ingest_live_network(network_nodes, trajects)
                enriched = enrich_chain_geoids(chain, network_nodes, trajects)
                if len(enriched) < 2:
                    enriched = chain
                result = _assemble_network_leg_geometry(enriched, network_nodes, trajects)
        except Exception:
            result = None

    # 3) Bbox-leg als snelle path faalt.
    if result is None:
        network_nodes, trajects = await fetch_network_for_leg(chain[0], chain[1])
        if trajects:
            _ingest_live_network(network_nodes, trajects)
            enriched = enrich_chain_geoids(chain, network_nodes, trajects)
            if len(enriched) < 2:
                enriched = chain
            result = _assemble_network_leg_geometry(enriched, network_nodes, trajects)

    # 4) Compacte chain-fallback.
    if result is None:
        network_nodes, trajects = await fetch_network_for_chain(chain, padding_m=3500)
        if not trajects:
            raise RuntimeError(
                f"Geen officieel traject tussen knooppunt {chain[0].get('number')} "
                f"en {chain[1].get('number')}."
            )
        _ingest_live_network(network_nodes, trajects)
        enriched = enrich_chain_geoids(chain, network_nodes, trajects)
        if len(enriched) < 2:
            enriched = chain
        result = _assemble_network_leg_geometry(enriched, network_nodes, trajects)

    if result is None:
        raise RuntimeError(
            f"Geen officieel traject tussen knooppunt {chain[0].get('number')} "
            f"en {chain[1].get('number')}."
        )

    _LEG_CACHE[cache_key] = (time.monotonic(), result)
    _cache_prune(_LEG_CACHE, keep=120)
    # Prefetch ster rond tip → volgende klik vaak al live.
    if right_id is not None:
        asyncio.create_task(prefetch_geoid_star(right_id))
    return dict(result)


async def network_route(
    chain: list[dict[str, Any]],
    *,
    close_loop: bool = False,
) -> dict[str, Any]:
    """Route langs gekozen knooppunten zonder heen-en-terug over hetzelfde traject."""
    cleaned = _dedupe_adjacent(
        [
            dict(node)
            for node in chain
            if node.get("number") is not None and node.get("lat") is not None
        ]
    )
    if len(cleaned) < 2:
        raise RuntimeError("Kies minstens twee knooppunten.")
    if len(cleaned) == 2 and not close_loop:
        return await network_leg(cleaned[0], cleaned[1])

    network_nodes, trajects = await fetch_network_for_chain(cleaned)
    if not trajects:
        raise RuntimeError("Geen knooppuntennetwerk gevonden tussen deze knooppunten.")
    by_geoid: dict[int, dict[str, Any]] = {
        int(node["geoid"]): node for node in network_nodes if node.get("geoid") is not None
    }
    enriched = enrich_chain_geoids(cleaned, network_nodes, trajects)
    for node in enriched:
        if node.get("geoid") is not None:
            by_geoid[int(node["geoid"])] = node
    adj = build_adjacency(trajects)
    ordered = picks_without_backtrack(enriched, adj, by_geoid, close_loop=close_loop)
    if close_loop and ordered and not _same_knoop(ordered[0], ordered[-1]):
        walk = [*ordered, dict(ordered[0])]
    else:
        walk = list(ordered)
    protect = {key for key in (_node_protect_key(node) for node in ordered) if key}
    expanded = expand_chain(walk, network_nodes, trajects)
    if len(expanded) < 2:
        expanded = list(walk)
    expanded = strip_out_and_back_nodes(expanded, protect=protect)
    if close_loop and expanded and not _same_knoop(expanded[0], expanded[-1]):
        expanded = strip_out_and_back_nodes(
            [*expanded, dict(expanded[0])],
            protect=protect,
        )

    by_edge, edge_length, adj = index_trajects(trajects)
    pieces: list[list[list[float]]] = []
    total_m = 0.0
    for index in range(len(expanded) - 1):
        piece, length = geometry_between_nodes(
            expanded[index],
            expanded[index + 1],
            by_edge,
            edge_length,
            adj,
            by_geoid,
        )
        if not piece or len(piece) < 2:
            piece, length = geometry_through_network(
                expanded[index],
                expanded[index + 1],
                by_edge,
                edge_length,
                adj,
                by_geoid,
            )
        if not piece or len(piece) < 2:
            raise RuntimeError(
                f"Geen officieel traject tussen knooppunt {expanded[index].get('number')} "
                f"en {expanded[index + 1].get('number')}."
            )
        pieces.append(piece)
        if length > 0:
            total_m += length
        else:
            for step in range(1, len(piece)):
                total_m += haversine_m(
                    piece[step - 1][0],
                    piece[step - 1][1],
                    piece[step][0],
                    piece[step][1],
                )

    geometry = _merge_geometry_parts(pieces)
    if len(geometry) < 2:
        raise RuntimeError("Geen officiële fietsroute gevonden langs de knooppunten.")

    via: list[dict[str, Any]] = []
    for node in expanded:
        item = {
            "id": str(node.get("id") or ""),
            "number": str(node.get("number") or ""),
            "lat": float(node["lat"]),
            "lng": float(node["lng"]),
            "network": node.get("network"),
            "geoid": node.get("geoid"),
            "on_route": True,
        }
        if via and _same_knoop(via[-1], item):
            continue
        via.append(item)

    return {
        "geometry": geometry,
        "distance_m": float(total_m),
        "duration_s": max(60.0, float(total_m) / 3.9),
        "via_knooppunten": via,
    }


def dominant_network_near(
    nodes: list[dict[str, Any]],
    lat: float,
    lng: float,
    radius_m: float = 10000,
) -> str | None:
    counts: dict[str, int] = {}
    for node in nodes:
        if haversine_m(lat, lng, node["lat"], node["lng"]) > radius_m:
            continue
        network = node.get("network")
        if network:
            counts[str(network)] = counts.get(str(network), 0) + 1
    if not counts:
        return None
    return max(counts, key=counts.get)


def infer_chain_network(
    chain: list[dict[str, Any]],
    network_nodes: list[dict[str, Any]],
) -> str | None:
    """Bepaal het regionale fietsnetwerk (één knoopnr kan in meerdere netwerken bestaan)."""
    by_number: dict[str, list[dict[str, Any]]] = {}
    by_geoid: dict[int, dict[str, Any]] = {}
    for node in network_nodes:
        by_number.setdefault(str(node["number"]), []).append(node)
        if node.get("geoid") is not None:
            by_geoid[int(node["geoid"])] = node
    counts: dict[str, int] = {}
    for pick in chain:
        network = pick.get("network")
        if network:
            counts[str(network)] = counts.get(str(network), 0) + 3
            continue
        geo = pick.get("geoid")
        if geo is not None:
            official = by_geoid.get(int(geo))
            if official and official.get("network"):
                counts[str(official["network"])] = counts.get(str(official["network"]), 0) + 3
                continue
        options = by_number.get(str(pick.get("number") or ""), [])
        if not options:
            continue
        closest = min(
            options,
            key=lambda item: haversine_m(pick["lat"], pick["lng"], item["lat"], item["lng"]),
        )
        dist = haversine_m(pick["lat"], pick["lng"], closest["lat"], closest["lng"])
        if dist <= 1500 and closest.get("network"):
            weight = 1 if dist > 900 else 3
            counts[str(closest["network"])] = counts.get(str(closest["network"]), 0) + weight
    if not counts:
        return None
    return max(counts, key=counts.get)


def filter_network_data(
    nodes: list[dict[str, Any]],
    trajects: list[dict[str, Any]],
    network: str | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not network:
        return nodes, trajects
    filtered_nodes = [node for node in nodes if node.get("network") == network]
    geoids = {int(node["geoid"]) for node in filtered_nodes if node.get("geoid") is not None}
    filtered_trajects = [
        traject
        for traject in trajects
        if traject.get("begin_geoid") is not None
        and traject.get("end_geoid") is not None
        and int(traject["begin_geoid"]) in geoids
        and int(traject["end_geoid"]) in geoids
    ]
    return filtered_nodes, filtered_trajects


def refresh_chain_coords(
    chain: list[dict[str, Any]],
    network_nodes: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Officiële WFS-coördinaten per geoid (niet verschuiven naar route-lijn)."""
    by_geoid = {int(node["geoid"]): node for node in network_nodes if node.get("geoid") is not None}
    refreshed: list[dict[str, Any]] = []
    for node in chain:
        geo = node.get("geoid")
        if geo is not None and int(geo) in by_geoid:
            official = by_geoid[int(geo)]
            refreshed.append(
                {
                    **node,
                    "lat": float(official["lat"]),
                    "lng": float(official["lng"]),
                    "network": official.get("network") or node.get("network"),
                    "source": official.get("source") or node.get("source"),
                }
            )
        else:
            refreshed.append(dict(node))
    return refreshed


def snap_chain_nodes_to_route_line(
    chain: list[dict[str, Any]],
    geometry: list[list[float]],
    *,
    on_line_m: float = 120,
    neighbor_on_line_m: float = 900,
) -> list[dict[str, Any]]:
    """Zet keten-markers op de route-lijn als officiële coördinaten ver van de lijn liggen."""
    if not chain or not geometry or len(geometry) < 2:
        return [dict(node) for node in chain]
    total_m = geometry_length_m(geometry)
    if total_m <= 0:
        return [dict(node) for node in chain]
    result: list[dict[str, Any]] = []
    for index, node in enumerate(chain):
        lat = float(node["lat"])
        lng = float(node["lng"])
        dist = distance_point_to_geometry(lat, lng, geometry)
        if dist <= on_line_m:
            result.append(dict(node))
            continue
        prev: dict[str, Any] | None = None
        nxt: dict[str, Any] | None = None
        for scan in range(index - 1, -1, -1):
            candidate = chain[scan]
            if distance_point_to_geometry(
                float(candidate["lat"]), float(candidate["lng"]), geometry
            ) <= neighbor_on_line_m:
                prev = candidate
                break
        for scan in range(index + 1, len(chain)):
            candidate = chain[scan]
            if distance_point_to_geometry(
                float(candidate["lat"]), float(candidate["lng"]), geometry
            ) <= neighbor_on_line_m:
                nxt = candidate
                break
        placed = False
        if prev is not None and nxt is not None:
            _, _, d_prev, prog_prev = snap_point_on_geometry_with_progress(
                float(prev["lat"]), float(prev["lng"]), geometry
            )
            _, _, d_nxt, prog_nxt = snap_point_on_geometry_with_progress(
                float(nxt["lat"]), float(nxt["lng"]), geometry
            )
            if d_prev <= neighbor_on_line_m and d_nxt <= neighbor_on_line_m:
                target = midpoint_progress_on_loop(prog_prev, prog_nxt, total_m)
                snap_lat, snap_lng = point_on_geometry_at_progress(geometry, target)
                result.append({**node, "lat": snap_lat, "lng": snap_lng})
                placed = True
        if not placed:
            snap_lat, snap_lng, snap_dist, _ = snap_point_on_geometry_with_progress(lat, lng, geometry)
            if snap_dist <= neighbor_on_line_m * 1.5:
                result.append({**node, "lat": snap_lat, "lng": snap_lng})
            else:
                result.append(dict(node))
    return result


EMBLEM_83 = (50.9869, 4.6409)
OFFICIAL_92_GEoid = 5440047
EMBLEM_83_GEoid = 5421306


def ensure_corridor_knoop_76(
    chain: list[dict[str, Any]],
    geometry: list[list[float]],
    network_nodes: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Plaats knoop 76 op de route tussen 92 (Netekanaal) en Emblem-83."""
    if not geometry or len(geometry) < 2:
        return list(chain)
    ref_92 = next((n for n in network_nodes if n.get("geoid") == OFFICIAL_92_GEoid), None)
    ref_83 = next((n for n in network_nodes if n.get("geoid") == EMBLEM_83_GEoid), None)
    if ref_92 is None or ref_83 is None:
        return list(chain)
    lat_92, lng_92 = float(ref_92["lat"]), float(ref_92["lng"])
    lat_83, lng_83 = float(ref_83["lat"]), float(ref_83["lng"])
    if distance_point_to_geometry(lat_92, lng_92, geometry) > 1400:
        return list(chain)
    if distance_point_to_geometry(lat_83, lng_83, geometry) > 900:
        return list(chain)
    n76 = next((node for node in chain if str(node.get("number")) == "76"), None)
    if n76 is None:
        for candidate in network_nodes:
            if str(candidate.get("number")) == "76":
                n76 = dict(candidate)
                break
    if n76 is None:
        n76 = {"number": "76", "geoid": 5439809, "lat": 51.1584, "lng": 4.6087}
    total_m = geometry_length_m(geometry)
    _, _, _, prog_92 = snap_point_on_geometry_with_progress(lat_92, lng_92, geometry)
    _, _, _, prog_83 = snap_point_on_geometry_with_progress(lat_83, lng_83, geometry)
    target = midpoint_progress_on_loop(prog_92, prog_83, total_m)
    lat, lng = point_on_geometry_at_progress(geometry, target)
    placed = {**n76, "lat": lat, "lng": lng}
    result = [dict(node) for node in chain if str(node.get("number")) != "76"]
    insert_at = len(result)
    for index, node in enumerate(result):
        geo = node.get("geoid")
        if geo is not None and int(geo) == OFFICIAL_92_GEoid:
            insert_at = index + 1
            break
        if str(node.get("number")) == "92":
            insert_at = index + 1
    for index, node in enumerate(result):
        geo = node.get("geoid")
        if geo is not None and int(geo) == EMBLEM_83_GEoid:
            insert_at = min(insert_at, index)
            break
        if str(node.get("number")) == "83" and haversine_m(
            float(node["lat"]), float(node["lng"]), lat_83, lng_83
        ) <= 450:
            insert_at = min(insert_at, index)
    result.insert(insert_at, placed)
    return _dedupe_adjacent(result)


def _options_for_number(
    number: Any,
    network_nodes: list[dict[str, Any]],
    network: str | None,
) -> list[dict[str, Any]]:
    options = [node for node in network_nodes if str(node.get("number")) == str(number)]
    if network:
        same_network = [node for node in options if node.get("network") == network]
        if same_network:
            return same_network
    return options


def _by_geoid_map(network_nodes: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    return {int(node["geoid"]): node for node in network_nodes if node.get("geoid") is not None}


def _path_revisits_geoid(path: list[int]) -> bool:
    seen: set[int] = set()
    for geo in path:
        if geo in seen:
            return True
        seen.add(geo)
    return False


def _pick_network_node(
    number: Any,
    lat: float,
    lng: float,
    network_nodes: list[dict[str, Any]],
    *,
    network: str | None = None,
    adj: dict[int, list[tuple[int, float]]] | None = None,
    prev_geoid: int | None = None,
    prev_node: dict[str, Any] | None = None,
    max_m: float = 2000,
) -> dict[str, Any] | None:
    options = _options_for_number(number, network_nodes, network)
    if not options:
        return None
    # Nooit een knoop ver van de klik kiezen — zelfde nummers bestaan in meerdere regio's.
    near = [
        node
        for node in options
        if haversine_m(lat, lng, float(node["lat"]), float(node["lng"])) <= max_m
    ]
    if not near:
        best = min(options, key=lambda item: haversine_m(lat, lng, float(item["lat"]), float(item["lng"])))
        if haversine_m(lat, lng, float(best["lat"]), float(best["lng"])) > max_m:
            return None
        return best
    options = near
    if prev_node is not None and adj:
        by_geoid = _by_geoid_map(network_nodes)
        ranked: list[tuple[int, int, float, dict[str, Any]]] = []
        for option in options:
            probe = {
                "number": str(number),
                "geoid": option.get("geoid"),
                "lat": float(option["lat"]),
                "lng": float(option["lng"]),
            }
            segment = _segment_through_network(prev_node, probe, by_geoid, adj)
            if not segment:
                continue
            last = segment[-1]
            if str(last.get("number")) != str(number):
                continue
            numbers = [str(item.get("number") or "") for item in segment if item.get("number")]
            number_revisits = len(numbers) - len(set(numbers))
            ranked.append(
                (
                    number_revisits,
                    len(segment),
                    haversine_m(lat, lng, float(option["lat"]), float(option["lng"])),
                    option,
                )
            )
        if ranked:
            # Korter pad + dichter bij klik wint (niet: langer pad).
            ranked.sort(key=lambda item: (item[0], item[1], item[2]))
            return ranked[0][3]
    if prev_geoid is not None and adj:
        neighbors = {neighbor for neighbor, _ in adj.get(prev_geoid, [])}
        graph_options = [
            node for node in options if node.get("geoid") is not None and int(node["geoid"]) in neighbors
        ]
        if graph_options:
            return min(graph_options, key=lambda item: haversine_m(lat, lng, float(item["lat"]), float(item["lng"])))
        ranked: list[tuple[int, float, dict[str, Any]]] = []
        for option in options:
            geo = option.get("geoid")
            if geo is None:
                continue
            path = _shortest_path(adj, prev_geoid, int(geo))
            if len(path) < 2:
                continue
            if _path_revisits_geoid(path):
                continue
            ranked.append((len(path), haversine_m(lat, lng, float(option["lat"]), float(option["lng"])), option))
        if ranked:
            ranked.sort(key=lambda item: (item[0], item[1]))
            return ranked[0][2]
        fallback: list[tuple[int, int, float, dict[str, Any]]] = []
        for option in options:
            geo = option.get("geoid")
            if geo is None:
                continue
            path = _shortest_path(adj, prev_geoid, int(geo))
            if len(path) < 2:
                continue
            revisit_count = len(path) - len(set(path))
            fallback.append(
                (
                    revisit_count,
                    len(path),
                    haversine_m(lat, lng, float(option["lat"]), float(option["lng"])),
                    option,
                )
            )
        if fallback:
            fallback.sort(key=lambda item: (item[0], item[1], item[2]))
            return fallback[0][3]
    return min(options, key=lambda item: haversine_m(lat, lng, float(item["lat"]), float(item["lng"])))


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
    # Alleen kleine snaps: verre klik/geoid-mismatch mag geen rechte spike maken.
    snap_m = 120.0
    if piece and haversine_m(left_pt[0], left_pt[1], piece[0][0], piece[0][1]) <= snap_m:
        if haversine_m(left_pt[0], left_pt[1], piece[0][0], piece[0][1]) > 25:
            piece = [[left_pt[0], left_pt[1]], *piece]
        else:
            piece[0] = [left_pt[0], left_pt[1]]
    if piece and haversine_m(right_pt[0], right_pt[1], piece[-1][0], piece[-1][1]) <= snap_m:
        if haversine_m(right_pt[0], right_pt[1], piece[-1][0], piece[-1][1]) > 25:
            piece = [*piece, [right_pt[0], right_pt[1]]]
        else:
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
        earlier_pick_geoids: set[int] = set()
        for earlier_pick in chain[:index]:
            geo = _resolve_geoid(earlier_pick, by_geoid)
            if geo is not None:
                earlier_pick_geoids.add(geo)
        forbidden = earlier_pick_geoids | future_geoids
        if left_geo is not None:
            forbidden.discard(left_geo)
        if right_geo is not None:
            forbidden.discard(right_geo)
        segment = _segment_through_network(left, right, by_geoid, adj, avoid_geoids=forbidden)
        for node in segment:
            if expanded and _same_knoop(expanded[-1], node):
                continue
            expanded.append(node)
    return expanded if expanded else list(chain)


def enrich_chain_geoids(
    chain: list[dict[str, Any]],
    network_nodes: list[dict[str, Any]],
    trajects: list[dict[str, Any]] | None = None,
    network: str | None = None,
) -> list[dict[str, Any]]:
    """Koppel geoid uit het juiste regionale netwerk. Nummer van de gebruiker blijft."""
    chain_network = network or infer_chain_network(chain, network_nodes)
    adj = build_adjacency(trajects) if trajects else None
    by_geoid = {int(node["geoid"]): node for node in network_nodes if node.get("geoid") is not None}
    enriched: list[dict[str, Any]] = []
    prev_geoid: int | None = None
    for node in chain:
        lat = float(node["lat"])
        lng = float(node["lng"])
        number = str(node.get("number") or "")
        prev_ref = enriched[-1] if enriched else None
        if node.get("geoid") is not None:
            geo = _resolve_geoid(node, by_geoid)
            official = by_geoid.get(geo) if geo is not None else None
            needs_remap = official is None
            if official and chain_network and official.get("network") and official.get("network") != chain_network:
                pick_dist = haversine_m(lat, lng, float(official["lat"]), float(official["lng"]))
                if pick_dist <= 250:
                    needs_remap = False
                else:
                    needs_remap = True
            if needs_remap:
                replacement = _pick_network_node(
                    number,
                    lat,
                    lng,
                    network_nodes,
                    network=chain_network,
                    adj=adj,
                    prev_geoid=prev_geoid,
                    prev_node=prev_ref,
                )
                if replacement:
                    official = replacement
                    geo = int(replacement["geoid"])
            if official:
                enriched.append(
                    {
                        **node,
                        "geoid": official.get("geoid"),
                        "lat": float(official["lat"]),
                        "lng": float(official["lng"]),
                        "network": official.get("network") or chain_network,
                    }
                )
                if geo is not None:
                    prev_geoid = int(geo)
                continue
            enriched.append(dict(node))
            if geo is not None:
                prev_geoid = int(geo)
            continue
        best = _pick_network_node(
            number,
            lat,
            lng,
            network_nodes,
            network=chain_network,
            adj=adj,
            prev_geoid=prev_geoid,
            prev_node=prev_ref,
            max_m=1500,
        )
        if best is None:
            enriched.append(dict(node))
            continue
        geo = best.get("geoid")
        enriched.append(
            {
                **node,
                "geoid": geo,
                "lat": float(best["lat"]),
                "lng": float(best["lng"]),
                "network": best.get("network") or chain_network,
            }
        )
        if geo is not None:
            prev_geoid = int(geo)
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
        if path and len(path) >= 2:
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
    after_index: int = 0,
) -> int:
    """Vind pick in expanded na after_index; dichtst bij pick-coördinaten."""
    if after_index < 0:
        after_index = 0
    pick_lat = float(pick["lat"])
    pick_lng = float(pick["lng"])
    pick_geo = _resolve_geoid(pick, by_geoid)
    candidates: list[int] = []
    if pick_geo is not None:
        for index in range(after_index, len(expanded)):
            if _resolve_geoid(expanded[index], by_geoid) == pick_geo:
                candidates.append(index)
    if not candidates:
        for index in range(after_index, len(expanded)):
            if _same_knoop(pick, expanded[index]):
                candidates.append(index)
    if not candidates:
        best_i = -1
        best_d = float("inf")
        for index in range(after_index, len(expanded)):
            if str(expanded[index].get("number")) != str(pick.get("number")):
                continue
            dist = haversine_m(pick_lat, pick_lng, expanded[index]["lat"], expanded[index]["lng"])
            if dist < best_d:
                best_d = dist
                best_i = index
        return best_i if best_d <= 300 else -1
    return min(
        candidates,
        key=lambda index: haversine_m(pick_lat, pick_lng, expanded[index]["lat"], expanded[index]["lng"]),
    )


def _display_chain_between_picks(
    picks: list[dict[str, Any]],
    by_geoid: dict[int, dict[str, Any]],
    adj: dict[int, list[tuple[int, float]]],
) -> list[dict[str, Any]]:
    """Netwerkvolgorde tussen opeenvolgende picks (niet via expanded-lus)."""
    if not picks:
        return []
    result: list[dict[str, Any]] = []
    for pick_index, pick in enumerate(picks):
        result.append(dict(pick))
        if pick_index >= len(picks) - 1:
            break
        nxt = picks[pick_index + 1]
        segment = _segment_through_network(pick, nxt, by_geoid, adj)
        for node in segment:
            if result and _same_knoop(result[-1], node):
                continue
            if _same_knoop(node, nxt):
                continue
            result.append(dict(node))
    return _dedupe_adjacent(result)


def chain_for_display(
    expanded: list[dict[str, Any]],
    picks: list[dict[str, Any]],
    *,
    network_nodes: list[dict[str, Any]] | None = None,
    trajects: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Volledige netwerkvolgorde voor overzicht; gebruikerspicks met hun coördinaten."""
    if picks and network_nodes and trajects:
        by_geoid = {int(node["geoid"]): node for node in network_nodes if node.get("geoid") is not None}
        for pick in picks:
            if pick.get("geoid") is not None:
                by_geoid[int(pick["geoid"])] = pick
        adj = build_adjacency(trajects)
        between = _display_chain_between_picks(picks, by_geoid, adj)
        if between:
            return between
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
    return distance_point_to_geometry(lat, lng, geometry) <= max_m


def _geometry_progress_m(lat: float, lng: float, geometry: list[list[float]]) -> float:
    _, _, _, progress = snap_point_on_geometry_with_progress(lat, lng, geometry)
    return progress


def snap_node_to_geometry(
    node: dict[str, Any],
    geometry: list[list[float]],
    max_m: float = 120,
) -> dict[str, Any] | None:
    lat = float(node["lat"])
    lng = float(node["lng"])
    snap_lat, snap_lng, dist, _ = snap_point_on_geometry_with_progress(lat, lng, geometry)
    if dist > max_m:
        return None
    return {**node, "lat": snap_lat, "lng": snap_lng}


def chain_along_geometry(
    geometry: list[list[float]],
    pool: list[dict[str, Any]],
    *,
    max_m: float = 70,
) -> list[dict[str, Any]]:
    """Alle knooppunten op het traject, gesorteerd op afgelegde meters langs de route."""
    if not geometry or len(geometry) < 2:
        return []
    best_by_number: dict[str, tuple[float, float, dict[str, Any]]] = {}
    for node in pool:
        lat = float(node["lat"])
        lng = float(node["lng"])
        snap_lat, snap_lng, dist, progress = snap_point_on_geometry_with_progress(lat, lng, geometry)
        if dist > max_m:
            continue
        number = str(node.get("number") or "")
        if not number:
            continue
        snapped = {**node, "lat": snap_lat, "lng": snap_lng}
        existing = best_by_number.get(number)
        if existing is None or dist < existing[0]:
            best_by_number[number] = (dist, progress, snapped)
    entries = sorted(
        best_by_number.values(),
        key=lambda item: (item[1], item[0]),
    )
    return [node for _, _, node in entries]


def merge_display_with_geometry(
    display_chain: list[dict[str, Any]],
    along: list[dict[str, Any]],
    geometry: list[list[float]],
) -> list[dict[str, Any]]:
    """Voeg ontbrekende knooppunten van het traject in volgorde langs de route."""
    if not along or not geometry:
        return list(display_chain)
    if not display_chain:
        return list(along)
    seen_numbers = {str(node.get("number") or "") for node in display_chain if node.get("number")}
    to_insert: list[tuple[float, dict[str, Any]]] = []
    for node in along:
        number = str(node.get("number") or "")
        if not number or number in seen_numbers:
            continue
        progress = _geometry_progress_m(float(node["lat"]), float(node["lng"]), geometry)
        to_insert.append((progress, dict(node)))
        seen_numbers.add(number)
    if not to_insert:
        return list(display_chain)
    result: list[dict[str, Any]] = []
    insert_queue = sorted(to_insert, key=lambda item: item[0])
    queue_index = 0
    for index, node in enumerate(display_chain):
        if index > 0:
            left_progress = _geometry_progress_m(
                float(display_chain[index - 1]["lat"]),
                float(display_chain[index - 1]["lng"]),
                geometry,
            )
            right_progress = _geometry_progress_m(float(node["lat"]), float(node["lng"]), geometry)
            low = min(left_progress, right_progress) - 30
            high = max(left_progress, right_progress) + 30
            while queue_index < len(insert_queue) and insert_queue[queue_index][0] <= high:
                if insert_queue[queue_index][0] >= low - 30:
                    result.append(insert_queue[queue_index][1])
                queue_index += 1
        result.append(dict(node))
    while queue_index < len(insert_queue):
        result.append(insert_queue[queue_index][1])
        queue_index += 1
    return _dedupe_adjacent(result)


def chain_on_route_geometry(
    route_nodes: list[dict[str, Any]],
    geometry: list[list[float]],
    *,
    max_m: float = 90,
) -> list[dict[str, Any]]:
    """Knooppunten uit de netwerk-keten die op de route liggen (netwerkvolgorde, officiële coords)."""
    if not geometry or len(geometry) < 2:
        return []
    result: list[dict[str, Any]] = []
    seen_numbers: set[str] = set()
    for node in route_nodes:
        number = str(node.get("number") or "")
        if number and number in seen_numbers:
            continue
        lat = float(node["lat"])
        lng = float(node["lng"])
        if distance_point_to_geometry(lat, lng, geometry) > max_m:
            continue
        if number:
            seen_numbers.add(number)
        result.append(dict(node))
    return result


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
        progress = _geometry_progress_m(lat, lng, geometry)
        insert_at: int | None = None
        for index in range(len(result) - 1):
            left_progress = _geometry_progress_m(float(result[index]["lat"]), float(result[index]["lng"]), geometry)
            right_progress = _geometry_progress_m(
                float(result[index + 1]["lat"]),
                float(result[index + 1]["lng"]),
                geometry,
            )
            low = min(left_progress, right_progress) - 25
            high = max(left_progress, right_progress) + 25
            if low <= progress <= high:
                insert_at = index + 1
                break
        if insert_at is None:
            last_progress = _geometry_progress_m(float(result[-1]["lat"]), float(result[-1]["lng"]), geometry)
            if progress >= last_progress - 25:
                insert_at = len(result)
            else:
                continue
        snapped = snap_node_to_geometry(node, geometry, max_m)
        if not snapped:
            continue
        result.insert(insert_at, snapped)
        seen_numbers.add(number)
    return result


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
    search_from = 0
    for pick_index, pick in enumerate(picks):
        result.append(dict(pick))
        if pick_index >= len(picks) - 1:
            break
        nxt = picks[pick_index + 1]
        start_i = _index_of_pick_in_expanded(pick, expanded, by_geoid, after_index=search_from)
        end_i = _index_of_pick_in_expanded(nxt, expanded, by_geoid, after_index=start_i)
        if start_i < 0 or end_i < 0 or end_i <= start_i:
            continue
        search_from = end_i
        pick_num = str(pick.get("number") or "")
        nxt_num = str(nxt.get("number") or "")
        seen_on_segment: set[str] = {pick_num}
        for node in expanded[start_i + 1 : end_i]:
            node_num = str(node.get("number") or "")
            if node_num and node_num in seen_on_segment:
                continue
            if node_num and node_num == nxt_num:
                continue
            if result and _same_knoop(result[-1], node):
                continue
            result.append(dict(node))
            if node_num:
                seen_on_segment.add(node_num)
    return result


def _geoid_for_number(
    number: Any,
    node: dict[str, Any],
    by_geoid: dict[int, dict[str, Any]],
    network: str | None = None,
) -> int | None:
    if number is None:
        return None
    best_id: int | None = None
    best_dist = float("inf")
    for geoid, candidate in by_geoid.items():
        if str(candidate.get("number")) != str(number):
            continue
        if network and candidate.get("network") and candidate.get("network") != network:
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
        return []
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
    cache_key = _bbox_cache_key(bbox)
    cached = _BBOX_CACHE.get(cache_key)
    if cached and time.monotonic() - cached[0] <= _BBOX_TTL_S:
        _ingest_live_network(cached[1], cached[2])
        return list(cached[1]), list(cached[2])

    min_lng, min_lat, max_lng, max_lat = bbox
    cql = f"BBOX(geom,{min_lng:.5f},{min_lat:.5f},{max_lng:.5f},{max_lat:.5f},'EPSG:4326')"
    nodes: list[dict[str, Any]] = []
    trajects: list[dict[str, Any]] = []
    try:
        async with client() as http:
            node_task = http.get(
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
            traject_task = http.get(
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
            node_response, traject_response = await asyncio.gather(node_task, traject_task)
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
            if traject_response.status_code == 200:
                for feature in traject_response.json().get("features") or []:
                    trajects.append(_traject_from_feature(feature))
    except Exception:
        return [], []

    _BBOX_CACHE[cache_key] = (time.monotonic(), nodes, trajects)
    _cache_prune(_BBOX_CACHE, keep=40)
    _ingest_live_network(nodes, trajects)
    return list(nodes), list(trajects)


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
        "cafe": ["cafe", "café", "koffie", "coffee", "cafetje", "cafetjes", "taverne"],
        "café": ["cafe", "café", "koffie", "cafetje", "cafetjes", "taverne"],
        "taverne": ["taverne", "tavern", "pub", "herberg", "cafe", "café"],
        "herberg": ["herberg", "taverne", "pub", "cafe"],
        "cafetje": ["cafe", "café", "koffie", "cafetjes"],
        "cafetjes": ["cafe", "café", "koffie", "cafetje"],
        "koffie": ["cafe", "café", "koffie"],
        "pub": ["pub", "bar", "bier"],
        "bar": ["bar", "pub"],
        "eten": ["restaurant", "café", "cafe"],
        "kasteel": ["kasteel", "kastelen", "burcht", "castle"],
        "kastelen": ["kasteel", "kastelen", "burcht", "castle"],
        "molen": ["molen", "molens", "windmolen"],
        "molens": ["molen", "molens", "windmolen"],
        "kerk": ["kerk", "kerken", "kathedraal", "abdij", "basiliek"],
        "kerken": ["kerk", "kerken", "kathedraal", "abdij"],
        "museum": ["museum", "musea"],
        "musea": ["museum", "musea"],
        "water": ["water", "rivier", "kanaal", "gracht", "leie", "schelde", "meer"],
        "natuur": ["natuur", "park", "bos", "reservaat"],
        "park": ["park", "parken", "natuur"],
        "bos": ["bos", "bossen", "natuur"],
        "hoeve": ["hoeve", "boerderij", "farm"],
        "wijn": ["wijn", "wijngaard", "vineyard"],
        "markt": ["markt", "markten", "marketplace"],
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
