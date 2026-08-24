from __future__ import annotations

import math
import re
from typing import Any

from app.http import client
from app.services.geo import bearing, haversine_m
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
    others.sort(key=lambda n: bearing(start_node["lat"], start_node["lng"], n["lat"], n["lng"]))
    if len(others) < 3:
        extras = sorted(
            (n for n in nodes if n["id"] != start_node["id"]),
            key=lambda n: abs(
                haversine_m(start_node["lat"], start_node["lng"], n["lat"], n["lng"]) - radius * 0.7
            ),
        )
        for node in extras:
            if all(haversine_m(node["lat"], node["lng"], p["lat"], p["lng"]) > 900 for p in others + [start_node]):
                others.append(node)
            if len(others) >= 4:
                break
        others.sort(key=lambda n: bearing(start_node["lat"], start_node["lng"], n["lat"], n["lng"]))
    chain = [start_node, *others, start_node]
    return _dedupe_adjacent(chain)


def chain_label(nodes: list[dict[str, Any]]) -> str:
    return " → ".join(n["number"] for n in nodes)


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
    others.sort(key=lambda n: bearing(start_node["lat"], start_node["lng"], n["lat"], n["lng"]))
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


def _bbox(lat: float, lng: float, radius_m: int) -> tuple[float, float, float, float]:
    dlat = radius_m / 111_000
    dlng = radius_m / (111_000 * max(0.2, math.cos(math.radians(lat))))
    return lng - dlng, lat - dlat, lng + dlng, lat + dlat
