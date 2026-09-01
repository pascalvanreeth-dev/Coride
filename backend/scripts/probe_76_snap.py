import asyncio

from app.services.geo import distance_point_to_geometry
from app.services.knooppunten import (
    enrich_chain_geoids,
    expand_chain,
    infer_chain_network,
    fetch_network_for_chain,
)
from app.services.planner import _build_knoop_route


async def main() -> None:
    spine = [
        {"number": "88", "lat": 51.08, "lng": 4.55, "geoid": None, "network": None},
    ]
    # minimal - use real fetch
    nodes, trajects = await _fetch_bbox_fallback()
    spine = build_spine(nodes)
    network_nodes, trajects = await fetch_network_for_chain(spine)
    print("networks in fetch:", {n.get("network") for n in network_nodes})
    net = infer_chain_network(spine, network_nodes)
    print("infer:", net)
    e1 = enrich_chain_geoids(spine, network_nodes, trajects=trajects, network=net)
    print("after enrich1 83:", next(n for n in e1 if n["number"] == "83"))
    e2 = enrich_chain_geoids(e1, network_nodes, trajects=trajects)
    print("after enrich2 83:", next(n for n in e2 if n["number"] == "83"))
    expanded = expand_chain(e1, network_nodes, trajects)
    nums = [n["number"] for n in expanded]
    if "76" in nums:
        i76 = nums.index("76")
        print("76 in expanded at", i76, expanded[i76])
    display, route = await _build_knoop_route(51.05, 4.55, spine, close_loop=True)
    geom = route["geometry"]
    chain_slice = []
    for n in display:
        if n["number"] in ("32", "76", "83", "92", "84", "90"):
            d = distance_point_to_geometry(n["lat"], n["lng"], geom)
            chain_slice.append((n["number"], round(d), n.get("geoid")))
    print("display key nodes dist_to_line:", chain_slice)
    # segment 32-83 in display order
    dnums = [n["number"] for n in display]
    if "32" in dnums and "83" in dnums:
        i32 = dnums.index("32")
        i83 = dnums.index("83", i32)
        print("between 32 and 83:", dnums[i32:i83 + 1])


async def _fetch_bbox_fallback():
    from app.services.knooppunten import _fetch_bbox, filter_network_data

    nodes, trajects = await _fetch_bbox((4.40, 50.85, 4.90, 51.20))
    return nodes, trajects


def build_spine(nodes):
    ant = [n for n in nodes if n.get("network") == "Fietsroutenetwerk Antwerpen"]
    by_num = {}
    for n in ant:
        by_num.setdefault(n["number"], []).append(n)
    spine = [by_num[nr][0] for nr in ["88", "93", "94", "32"]]
    spine.append(
        {
            "number": "83",
            "lat": 50.9869,
            "lng": 4.6409,
            "geoid": 5421306,
            "network": "Fietsnetwerk Vlaams-Brabant",
            "id": "vb-83",
        }
    )
    return spine


if __name__ == "__main__":
    asyncio.run(main())
