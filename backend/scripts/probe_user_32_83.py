"""Simulate manual route 88,93,94,32,83 like user scenario."""
import asyncio

from app.services.knooppunten import (
    _fetch_bbox,
    enrich_chain_geoids,
    expand_chain,
    infer_chain_network,
    filter_network_data,
    chain_for_display,
    chain_on_route_geometry,
    fetch_network_for_chain,
)
from app.services.planner import _build_knoop_route


async def main() -> None:
    bbox = (4.40, 50.85, 4.90, 51.20)
    nodes, trajects = await _fetch_bbox(bbox)
    ant = "Fietsroutenetwerk Antwerpen"
    nodes, trajects = filter_network_data(nodes, trajects, ant)
    by_num: dict[str, list] = {}
    for n in nodes:
        by_num.setdefault(n["number"], []).append(n)

  # Typical Antwerpen picks + VB Emblem 83 (wrong network)
    spine = []
    for nr in ["88", "93", "94", "32"]:
        spine.append(by_num[nr][0])
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
    network = infer_chain_network(spine, nodes)
    print("infer network:", network)
    enriched = enrich_chain_geoids(spine, nodes, trajects=trajects, network=network)
    print("enriched numbers:", [n["number"] for n in enriched])
    print("enriched 83 geoid:", next(n["geoid"] for n in enriched if n["number"] == "83"))
    expanded = expand_chain(enriched, nodes, trajects)
    nums = [n["number"] for n in expanded]
    print("expanded len", len(nums), "has 76", "76" in nums)
    if "32" in nums and "76" in nums and "83" in nums:
        i32 = nums.index("32")
        i76 = nums.index("76")
        i83 = nums.index("83")
        print(f"order 32@{i32} 76@{i76} 83@{i83}")

    display = await _build_knoop_route(51.05, 4.55, spine, close_loop=True)
    display_chain, route = display
    dnums = [n["number"] for n in display_chain]
    print("display_chain:", dnums[-15:])
    if "32" in dnums:
        idx = dnums.index("32")
        print("after first 32:", dnums[idx:idx + 5])

    geom = route.get("geometry") or []
    on_geom = chain_on_route_geometry(expanded, geom, max_m=90)
    on_nums = [n["number"] for n in on_geom]
    print("on_geom 90m after 32:", on_nums[on_nums.index("32"):on_nums.index("32") + 5] if "32" in on_nums else "no 32")
    disp2 = chain_for_display(expanded, enriched)
    print("chain_for_display after 32:", [n["number"] for n in disp2][-8:])


if __name__ == "__main__":
    asyncio.run(main())
