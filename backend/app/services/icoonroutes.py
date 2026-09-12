"""Officiële fiets-icoonroutes van Toerisme Vlaanderen (WFS)."""

from __future__ import annotations

import asyncio
import math
import re
import time
from typing import Any

from app.http import client
from app.services.geo import haversine_m

WFS_URL = "https://geodata.toerismevlaanderen.be/geoserver/wfs"
_CACHE_TTL_S = 24 * 60 * 60
_cache: dict[str, Any] | None = None
_cache_at = 0.0
_lock = asyncio.Lock()

# Thema-interesses per icoonroute (vaste mapping).
ROUTE_META: dict[str, dict[str, Any]] = {
    "Vlaanderenroute": {
        "highlight": "Dwars door Vlaanderen",
        "interests": ["geschiedenis", "activiteiten"],
        "notes": "Officiële icoonroute van Toerisme Vlaanderen, dwars door het land.",
    },
    "Heuvelroute": {
        "highlight": "Heuvels & panorama's",
        "interests": ["natuur", "activiteiten"],
        "notes": "Officiële icoonroute door heuvelachtig Vlaanderen.",
    },
    "Kunststedenroute": {
        "highlight": "Kunststeden",
        "interests": ["geschiedenis", "architectuur"],
        "notes": "Officiële icoonroute langs Vlaamse kunststeden.",
    },
    "Schelderoute": {
        "highlight": "Schelde",
        "interests": ["natuur", "geschiedenis"],
        "notes": "Officiële icoonroute langs de Schelde.",
    },
    "Kempenroute": {
        "highlight": "Kempen",
        "interests": ["natuur", "landbouw"],
        "notes": "Officiële icoonroute door de Kempen.",
    },
    "Frontroute 14-18": {
        "highlight": "WO I-erfgoed",
        "interests": ["oorlog", "geschiedenis"],
        "notes": "Officiële icoonroute langs het front 1914–1918.",
    },
    "Kustroute": {
        "highlight": "Vlaamse kust",
        "interests": ["natuur", "activiteiten"],
        "notes": "Officiële icoonroute langs de Vlaamse kust.",
    },
    "Groene Gordelroute": {
        "highlight": "Groene gordel",
        "interests": ["natuur", "landbouw"],
        "notes": "Officiële icoonroute rond de groene gordel van Brussel.",
    },
    "Maasroute": {
        "highlight": "Maasvallei",
        "interests": ["natuur", "landbouw"],
        "notes": "Officiële icoonroute langs de Maas.",
    },
}


def route_id_from_name(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return f"icoon-{slug}" if slug else "icoon-onbekend"


def name_from_route_id(route_id: str) -> str | None:
    if not route_id:
        return None
    raw = route_id[6:] if route_id.startswith("icoon-") else route_id
    for name in ROUTE_META:
        if route_id_from_name(name) == route_id or route_id_from_name(name).endswith(raw):
            return name
    # Fuzzy: compare slug of known names
    for name in ROUTE_META:
        if re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") == raw:
            return name
    return None


async def _wfs_features(type_name: str, cql: str, max_features: int = 5000) -> list[dict[str, Any]]:
    async with client() as http:
        response = await http.get(
            WFS_URL,
            params={
                "service": "WFS",
                "version": "1.1.0",
                "request": "GetFeature",
                "typeName": type_name,
                "outputFormat": "application/json",
                "srsName": "EPSG:4326",
                "maxFeatures": max_features,
                "cql_filter": cql,
            },
            timeout=45.0,
        )
    if response.status_code != 200:
        raise RuntimeError(f"WFS {type_name} faalde ({response.status_code})")
    return list(response.json().get("features") or [])


def _node_from_feature(feature: dict[str, Any], icoonroute: str) -> dict[str, Any] | None:
    props = feature.get("properties") or {}
    geom = feature.get("geometry") or {}
    coords = geom.get("coordinates") or []
    if len(coords) < 2:
        return None
    number = props.get("knoopnr")
    geoid = props.get("geoid")
    if number is None or geoid is None:
        return None
    try:
        geoid_i = int(geoid)
    except (TypeError, ValueError):
        return None
    return {
        "id": f"icoon-{geoid_i}",
        "number": str(number),
        "lat": float(coords[1]),
        "lng": float(coords[0]),
        "geoid": geoid_i,
        "network": props.get("naam") or "Fietsknooppuntennetwerk Vlaanderen",
        "source": "Toerisme Vlaanderen",
        "icoonroute": icoonroute,
    }


def _edge_from_feature(feature: dict[str, Any], icoonroute: str) -> dict[str, Any] | None:
    props = feature.get("properties") or {}
    geom = feature.get("geometry") or {}
    raw = geom.get("coordinates") or []
    begin = props.get("begin_geoid")
    end = props.get("end_geoid")
    if begin is None or end is None or len(raw) < 2:
        return None
    try:
        a, b = int(begin), int(end)
    except (TypeError, ValueError):
        return None
    coordinates = [[float(c[1]), float(c[0])] for c in raw if len(c) >= 2]
    meters = 0.0
    for i in range(len(coordinates) - 1):
        meters += haversine_m(
            coordinates[i][0],
            coordinates[i][1],
            coordinates[i + 1][0],
            coordinates[i + 1][1],
        )
    if meters <= 0:
        meters = float(props.get("shape_length") or 0) or 1.0
    return {
        "a": a,
        "b": b,
        "m": meters,
        "coordinates": coordinates,
        "begin_knoopnr": props.get("begin_knoopnr"),
        "end_knoopnr": props.get("end_knoopnr"),
        "icoonroute": icoonroute,
    }


async def _load_raw() -> dict[str, Any]:
    """Alle fiets-icoonroute knopen + trajecten, gegroepeerd per naam."""
    cql = "mobility='Fiets'"
    node_feats, edge_feats = await asyncio.gather(
        _wfs_features("routes:icoonroute_knooppunten", cql),
        _wfs_features("routes:icoonroute_trajecten", cql),
    )
    by_name: dict[str, dict[str, Any]] = {}
    for feature in node_feats:
        props = feature.get("properties") or {}
        name = (props.get("icoonroute") or "").strip()
        if not name:
            continue
        node = _node_from_feature(feature, name)
        if not node:
            continue
        bucket = by_name.setdefault(name, {"nodes": {}, "edges": []})
        bucket["nodes"][int(node["geoid"])] = node
    for feature in edge_feats:
        props = feature.get("properties") or {}
        name = (props.get("icoonroute") or "").strip()
        if not name:
            continue
        edge = _edge_from_feature(feature, name)
        if not edge:
            continue
        bucket = by_name.setdefault(name, {"nodes": {}, "edges": []})
        bucket["edges"].append(edge)
        # Zorg dat eindpunten bestaan (minimaal coords uit traject).
        for geoid, latlng in (
            (edge["a"], edge["coordinates"][0] if edge["coordinates"] else None),
            (edge["b"], edge["coordinates"][-1] if edge["coordinates"] else None),
        ):
            if geoid in bucket["nodes"] or not latlng:
                continue
            bucket["nodes"][geoid] = {
                "id": f"icoon-{geoid}",
                "number": str(
                    edge["begin_knoopnr"] if geoid == edge["a"] else edge["end_knoopnr"] or "?"
                ),
                "lat": float(latlng[0]),
                "lng": float(latlng[1]),
                "geoid": geoid,
                "network": "Fietsknooppuntennetwerk Vlaanderen",
                "source": "Toerisme Vlaanderen",
                "icoonroute": name,
            }
    return by_name


async def get_catalog() -> dict[str, dict[str, Any]]:
    global _cache, _cache_at
    now = time.monotonic()
    if _cache is not None and now - _cache_at <= _CACHE_TTL_S:
        return _cache
    async with _lock:
        now = time.monotonic()
        if _cache is not None and now - _cache_at <= _CACHE_TTL_S:
            return _cache
        raw = await _load_raw()
        _cache = raw
        _cache_at = time.monotonic()
        return raw


def _build_adj(edges: list[dict[str, Any]]) -> dict[int, list[tuple[int, float]]]:
    adj: dict[int, list[tuple[int, float]]] = {}
    for edge in edges:
        a, b, m = int(edge["a"]), int(edge["b"]), float(edge["m"])
        adj.setdefault(a, []).append((b, m))
        adj.setdefault(b, []).append((a, m))
    return adj


def _nearest_geoid(nodes: dict[int, dict[str, Any]], lat: float, lng: float) -> int | None:
    best = None
    best_d = math.inf
    for geoid, node in nodes.items():
        d = haversine_m(lat, lng, float(node["lat"]), float(node["lng"]))
        if d < best_d:
            best_d = d
            best = geoid
    return best


def _grow_path(
    adj: dict[int, list[tuple[int, float]]],
    start: int,
    target_m: float,
    first_hop: tuple[int, float] | None = None,
) -> tuple[list[int], float]:
    """Groei een pad vanaf start langs ongebruikte randen tot ~target_m."""
    path = [start]
    used_edges: set[tuple[int, int]] = set()
    dist = 0.0

    def edge_key(u: int, v: int) -> tuple[int, int]:
        return (u, v) if u < v else (v, u)

    if first_hop:
        nbr, m = first_hop
        used_edges.add(edge_key(start, nbr))
        path.append(nbr)
        dist += m

    while dist < target_m * 0.98 and len(path) < 120:
        cur = path[-1]
        cands = [
            (nbr, m)
            for nbr, m in adj.get(cur, [])
            if edge_key(cur, nbr) not in used_edges
        ]
        if not cands:
            break
        fresh = [c for c in cands if c[0] not in path]
        pool = fresh or cands

        def score(item: tuple[int, float]) -> float:
            _nbr, m = item
            progress = dist + m
            if progress < target_m * 0.55:
                return -m  # eerst uitwaaien
            return abs(progress - target_m)

        nbr, m = min(pool, key=score)
        used_edges.add(edge_key(cur, nbr))
        path.append(nbr)
        dist += m

    return path, dist


def nearest_stretch(
    route_data: dict[str, Any],
    lat: float,
    lng: float,
    target_km: float = 50.0,
) -> dict[str, Any]:
    nodes: dict[int, dict[str, Any]] = route_data.get("nodes") or {}
    edges: list[dict[str, Any]] = route_data.get("edges") or []
    if not nodes or not edges:
        raise ValueError("Deze icoonroute heeft geen knooppunten/trajecten.")

    adj = _build_adj(edges)
    start = _nearest_geoid(nodes, lat, lng)
    if start is None or start not in adj:
        raise ValueError("Geen knooppunt dicht bij jouw positie op deze icoonroute.")

    target_m = max(8.0, min(90.0, float(target_km))) * 1000
    hops = adj.get(start) or []
    candidates: list[tuple[list[int], float]] = []
    if hops:
        for hop in hops:
            candidates.append(_grow_path(adj, start, target_m, first_hop=hop))
    else:
        candidates.append(_grow_path(adj, start, target_m, first_hop=None))

    path, meters = min(candidates, key=lambda item: abs(item[1] - target_m))
    if len(path) < 2:
        raise ValueError("Kon geen traject op deze icoonroute bouwen.")

    chain = [dict(nodes[g]) for g in path if g in nodes]
    if len(chain) < 2:
        raise ValueError("Knooppuntenketen is te kort.")

    edge_lookup: dict[tuple[int, int], dict[str, Any]] = {}
    for edge in edges:
        key = (min(int(edge["a"]), int(edge["b"])), max(int(edge["a"]), int(edge["b"])))
        edge_lookup[key] = edge

    legs: list[dict[str, Any]] = []
    geometry: list[list[float]] = []
    for i in range(len(path) - 1):
        a, b = path[i], path[i + 1]
        edge = edge_lookup.get((min(a, b), max(a, b)))
        coords = list((edge or {}).get("coordinates") or [])
        if len(coords) < 2:
            na, nb = nodes.get(a), nodes.get(b)
            if na and nb:
                coords = [[float(na["lat"]), float(na["lng"])], [float(nb["lat"]), float(nb["lng"])]]
        if not coords:
            continue
        # Richting van pad a→b
        if (
            len(coords) >= 2
            and nodes.get(a)
            and haversine_m(coords[0][0], coords[0][1], float(nodes[a]["lat"]), float(nodes[a]["lng"]))
            > haversine_m(coords[-1][0], coords[-1][1], float(nodes[a]["lat"]), float(nodes[a]["lng"]))
        ):
            coords = list(reversed(coords))
        from_node = nodes.get(a) or {"geoid": a, "number": "?", "lat": coords[0][0], "lng": coords[0][1]}
        to_node = nodes.get(b) or {"geoid": b, "number": "?", "lat": coords[-1][0], "lng": coords[-1][1]}
        leg_m = float((edge or {}).get("m") or 0)
        if leg_m <= 0:
            for j in range(len(coords) - 1):
                leg_m += haversine_m(coords[j][0], coords[j][1], coords[j + 1][0], coords[j + 1][1])
        legs.append(
            {
                "geometry": coords,
                "distance_m": leg_m,
                "duration_s": max(60, leg_m / 4.2),
                "official": True,
                "via_knooppunten": [from_node, to_node],
                "from_geoid": a,
                "to_geoid": b,
            }
        )
        if not geometry:
            geometry.extend(coords)
        else:
            start_i = 0
            if coords and geometry:
                last = geometry[-1]
                first = coords[0]
                if abs(last[0] - first[0]) < 1e-5 and abs(last[1] - first[1]) < 1e-5:
                    start_i = 1
            geometry.extend(coords[start_i:])

    start_node = chain[0]
    return {
        "knooppunten": chain,
        "legs": legs,
        "geometry": geometry,
        "distance_km": round(max(meters / 1000, 1.0), 1),
        "lat": float(start_node["lat"]),
        "lng": float(start_node["lng"]),
        "start_label": f"Knooppunt {start_node.get('number')}",
    }


async def list_route_summaries(lat: float, lng: float) -> list[dict[str, Any]]:
    catalog = await get_catalog()
    out: list[dict[str, Any]] = []
    for rank, name in enumerate(sorted(catalog.keys()), start=1):
        data = catalog[name]
        nodes = data.get("nodes") or {}
        if not nodes:
            continue
        nearest = _nearest_geoid(nodes, lat, lng)
        if nearest is None:
            continue
        node = nodes[nearest]
        meta = ROUTE_META.get(name) or {
            "highlight": name,
            "interests": ["natuur"],
            "notes": f"Officiële icoonroute: {name}.",
        }
        dist_km = haversine_m(lat, lng, float(node["lat"]), float(node["lng"])) / 1000
        out.append(
            {
                "rank": rank,
                "id": route_id_from_name(name),
                "city": "vlaanderen",
                "title": name,
                "highlight": meta["highlight"],
                "start": f"{name} · knooppunt {node.get('number')}",
                "lat": float(node["lat"]),
                "lng": float(node["lng"]),
                "end": None,
                "mode": "punt",
                "distance_km": 50,
                "interests": list(meta["interests"]),
                "municipalities": [],
                "localities": [],
                "notes": meta["notes"],
                "distance_from_you_km": round(dist_km, 1),
                "icoonroute": name,
                "source": "Toerisme Vlaanderen icoonroutes",
            }
        )
    # Rank by distance, then name
    out.sort(key=lambda item: (item["distance_from_you_km"], item["title"]))
    for index, item in enumerate(out, start=1):
        item["rank"] = index
    return out


async def get_route_by_id(route_id: str) -> dict[str, Any] | None:
    name = name_from_route_id(route_id)
    if not name:
        return None
    catalog = await get_catalog()
    if name not in catalog:
        return None
    meta = ROUTE_META.get(name) or {
        "highlight": name,
        "interests": ["natuur"],
        "notes": f"Officiële icoonroute: {name}.",
    }
    nodes = catalog[name].get("nodes") or {}
    sample = next(iter(nodes.values()), None)
    return {
        "id": route_id_from_name(name),
        "city": "vlaanderen",
        "title": name,
        "highlight": meta["highlight"],
        "start": name,
        "lat": float(sample["lat"]) if sample else 51.0,
        "lng": float(sample["lng"]) if sample else 4.0,
        "mode": "punt",
        "distance_km": 50,
        "interests": list(meta["interests"]),
        "municipalities": [],
        "localities": [],
        "notes": meta["notes"],
        "icoonroute": name,
        "source": "Toerisme Vlaanderen icoonroutes",
    }


async def stretch_for_route(
    route_id: str,
    lat: float,
    lng: float,
    target_km: float = 50.0,
) -> dict[str, Any]:
    name = name_from_route_id(route_id)
    if not name:
        raise ValueError("Onbekende icoonroute.")
    catalog = await get_catalog()
    data = catalog.get(name)
    if not data:
        raise ValueError("Icoonroute niet gevonden in Toerisme Vlaanderen-data.")
    meta = await get_route_by_id(route_id)
    stretch = nearest_stretch(data, lat, lng, target_km)
    return {
        **(meta or {}),
        **stretch,
        "id": route_id_from_name(name),
        "title": name,
        "mode": "punt",
        "notes": (meta or {}).get("notes") or "",
        "interests": (meta or {}).get("interests") or ["natuur"],
        "source": "Toerisme Vlaanderen icoonroutes",
    }
