import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services import knooppunten as knoop_service
from app.services.geo import haversine_m
from app.services.planner import _build_knoop_route, _route_covers_nodes


async def main() -> None:
    nodes, _ = await knoop_service.fetch_network_for_chain(
        [{"lat": 51.16, "lng": 4.36, "number": "x"}]
    )
    chain = nodes[:4]
    print("spine", [n["number"] for n in chain])
    expanded, route = await _build_knoop_route(
        chain[0]["lat"],
        chain[0]["lng"],
        chain,
        close_loop=False,
    )
    geom = route["geometry"]
    print("expanded", [n["number"] for n in expanded])
    print("km", round(route["distance_m"] / 1000, 2), "pts", len(geom))
    print("covers", _route_covers_nodes(geom, chain, max_m=90))
    for n in chain:
        best = min(haversine_m(n["lat"], n["lng"], p[0], p[1]) for p in geom)
        print(f"pick {n['number']} nearest_m={best:.1f}")


if __name__ == "__main__":
    asyncio.run(main())
