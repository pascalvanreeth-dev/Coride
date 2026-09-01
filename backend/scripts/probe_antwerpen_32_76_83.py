"""Verify Antwerpen network routing 32 -> 76 -> 83 matches fietsknooppunt."""
import asyncio

from app.services.geo import haversine_m
from app.services.knooppunten import (
    _fetch_bbox,
    enrich_chain_geoids,
    expand_chain,
    infer_chain_network,
    filter_network_data,
    index_trajects,
    geometry_between_nodes,
)


async def main() -> None:
    bbox = (4.40, 50.85, 4.90, 51.20)
    nodes, trajects = await _fetch_bbox(bbox)
    ant = "Fietsroutenetwerk Antwerpen"
    nodes, trajects = filter_network_data(nodes, trajects, ant)
    by_geoid = {int(n["geoid"]): n for n in nodes}
    n32 = next(n for n in nodes if n["number"] == "32" and n["geoid"] == 5440402)
    n83_emblem = {
        "number": "83",
        "lat": 50.9869,
        "lng": 4.6409,
        "geoid": 5421306,
        "network": "Fietsnetwerk Vlaams-Brabant",
    }
    enriched = enrich_chain_geoids([n32, n83_emblem], nodes, trajects=trajects, network=ant)
    expanded = expand_chain(enriched, nodes, trajects)
    numbers = [n["number"] for n in expanded]
    print("32 + VB Emblem 83 ->", numbers)
    assert "76" in numbers
    idx32 = numbers.index("32")
    idx76 = numbers.index("76")
    idx83 = numbers.index("83")
    assert idx32 < idx76 < idx83
    n76 = next(n for n in expanded if n["number"] == "76")
    official76 = by_geoid[int(n76["geoid"])]
    assert abs(n76["lat"] - official76["lat"]) < 0.00001


if __name__ == "__main__":
    asyncio.run(main())
