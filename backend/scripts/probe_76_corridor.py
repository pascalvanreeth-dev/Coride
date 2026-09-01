import asyncio
from collections import deque

from app.services.geo import haversine_m
from app.services.knooppunten import (
    _overpass,
    _fetch_bbox,
    build_adjacency,
    index_trajects,
    geometry_between_geoids,
)


async def main() -> None:
    mid_lat, mid_lng = 51.019, 4.675
    query = f"[out:json][timeout:25];node[\"rcn_ref\"](around:2500,{mid_lat},{mid_lng});out;"
    data = await _overpass(query)
    refs = sorted(
        {str(el.get("tags", {}).get("rcn_ref")) for el in data.get("elements", []) if el.get("tags", {}).get("rcn_ref")},
        key=lambda x: int(x),
    )
    print("OSM rcn near midpoint:", refs)

    nodes, trajects = await _fetch_bbox((4.50, 50.90, 4.80, 51.10))
    ant = [n for n in nodes if n.get("network") == "Fietsroutenetwerk Antwerpen"]
    by_geo = {int(n["geoid"]): n for n in ant if n.get("geoid")}
    ant_geos = set(by_geo)
    ant_trajects = [
        t
        for t in trajects
        if int(t["begin_geoid"]) in ant_geos and int(t["end_geoid"]) in ant_geos
    ]
    adj = build_adjacency(ant_trajects)
    by_edge, edge_length, _ = index_trajects(ant_trajects)

    start = 5440047  # 92
    emblem = (50.9869, 4.6409)
    queue: deque[tuple[int, int]] = deque([(start, 0)])
    seen = {start}
    while queue:
        geo, depth = queue.popleft()
        if depth > 14:
            continue
        node = by_geo.get(geo)
        if node:
            dist_emblem = haversine_m(emblem[0], emblem[1], node["lat"], node["lng"])
            if node["number"] in ("76", "75", "83", "92", "36") or dist_emblem < 3500:
                print(
                    f"  hop={depth} nr={node['number']} geoid={geo} "
                    f"dist_emblem={dist_emblem:.0f}m lat={node['lat']:.4f}"
                )
        for neighbor, _ in adj.get(geo, []):
            if neighbor not in seen:
                seen.add(neighbor)
                queue.append((neighbor, depth + 1))

    # Direct traject geometry 92 to closest ant node near emblem
    best_goal = None
    best_d = float("inf")
    for geo, node in by_geo.items():
        d = haversine_m(emblem[0], emblem[1], node["lat"], node["lng"])
        if d < best_d:
            best_d = d
            best_goal = geo
    if best_goal:
        path = []
        # simple BFS path
        from app.services.knooppunten import _shortest_path

        path = _shortest_path(adj, start, best_goal)
        nums = [by_geo[g]["number"] for g in path if g in by_geo]
        print(f"shortest 92 to near-emblem ({best_goal}):", nums)


if __name__ == "__main__":
    asyncio.run(main())
