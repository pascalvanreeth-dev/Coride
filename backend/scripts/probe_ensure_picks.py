import asyncio

from app.services.knooppunten import (
    _fetch_bbox,
    enrich_chain_geoids,
    expand_chain,
    infer_chain_network,
    filter_network_data,
    _ensure_picks_in_chain,
    _index_of_pick_in_expanded,
)


async def main() -> None:
    nodes, trajects = await _fetch_bbox((4.40, 50.85, 4.90, 51.20))
    ant = "Fietsroutenetwerk Antwerpen"
    nodes, trajects = filter_network_data(nodes, trajects, ant)
    by_num: dict[str, list] = {}
    for node in nodes:
        by_num.setdefault(node["number"], []).append(node)
    spine = [by_num[nr][0] for nr in ["88", "93", "94", "32"]]
    spine.append(
        {
            "number": "83",
            "lat": 50.9869,
            "lng": 4.6409,
            "geoid": 5421306,
            "network": "Fietsnetwerk Vlaams-Brabant",
            "id": "vb",
        }
    )
    net = infer_chain_network(spine, nodes)
    enriched = enrich_chain_geoids(spine, nodes, trajects=trajects, network=net)
    expanded = expand_chain(enriched, nodes, trajects)
    by_geoid = {int(n["geoid"]): n for n in [*expanded, *enriched] if n.get("geoid")}
    search_from = 0
    for index, pick in enumerate(enriched):
        start_i = _index_of_pick_in_expanded(pick, expanded, by_geoid, search_from)
        print(f"pick {index} {pick['number']} geoid {pick.get('geoid')} at {start_i}")
        if index < len(enriched) - 1:
            nxt = enriched[index + 1]
            end_i = _index_of_pick_in_expanded(nxt, expanded, by_geoid, start_i)
            seg = [n["number"] for n in expanded[start_i:end_i + 1]]
            print(f"  -> {nxt['number']} end {end_i} segment {seg}")
            search_from = end_i
    ensured = _ensure_picks_in_chain(expanded, enriched)
    nums = [n["number"] for n in ensured]
    i32 = nums.index("32", nums.index("94") + 1)
    i83 = nums.index("83", i32 + 1)
    print("ensured 32->83", nums[i32:i83 + 1])


if __name__ == "__main__":
    asyncio.run(main())
