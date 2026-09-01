import asyncio

from app.services.geo import haversine_m
from app.services.knooppunten import (
    _fetch_bbox,
    _shortest_path,
    build_adjacency,
    filter_network_data,
    index_trajects,
)


async def main() -> None:
    nodes, trajects = await _fetch_bbox((4.40, 50.85, 4.90, 51.20))
    vb = "Fietsnetwerk Vlaams-Brabant"
    vb_nodes, _ = filter_network_data(nodes, trajects, vb)
    ref92 = (51.0512, 4.6906)
    for nr in ["76", "83", "92", "32", "75"]:
        opts = [n for n in vb_nodes if n["number"] == nr]
        print(nr, len(opts))
        for o in opts:
            print(
                " ",
                o["geoid"],
                round(o["lat"], 5),
                round(o["lng"], 5),
                haversine_m(ref92[0], ref92[1], o["lat"], o["lng"]),
            )

    geos = {int(n["geoid"]) for n in nodes if n.get("geoid")}
    cross_trajects = [
        t
        for t in trajects
        if int(t["begin_geoid"]) in geos and int(t["end_geoid"]) in geos
    ]
    adj = build_adjacency(cross_trajects)
    by_geoid = {int(n["geoid"]): n for n in nodes if n.get("geoid")}
    path = _shortest_path(adj, 5440047, 5421306)
    print("path len", len(path) if path else 0)
    if path:
        nums = [by_geoid[g]["number"] for g in path if g in by_geoid]
        print("first 40:", nums[:40])
        print("last 15:", nums[-15:])
        if "76" in nums:
            i = nums.index("76")
            n = by_geoid[path[i]]
            print("76 on path:", n)
        else:
            print("NO 76 on shortest path 92->VB83")


if __name__ == "__main__":
    asyncio.run(main())
