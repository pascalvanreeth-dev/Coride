import asyncio

from app.services.geo import haversine_m
from app.services.knooppunten import _fetch_bbox, filter_network_data


async def main() -> None:
    nodes, trajects = await _fetch_bbox((4.40, 50.85, 4.90, 51.20))
    nodes, _ = filter_network_data(nodes, trajects, "Fietsroutenetwerk Antwerpen")
    ref92 = (51.0512, 4.6906)
    ref83 = (50.9869, 4.6409)
    for nr in ["76", "92", "83", "32"]:
        opts = [n for n in nodes if n["number"] == nr]
        print(nr, "count", len(opts))
        for opt in sorted(opts, key=lambda n: haversine_m(ref92[0], ref92[1], n["lat"], n["lng"])):
            d92 = haversine_m(ref92[0], ref92[1], opt["lat"], opt["lng"])
            d83 = haversine_m(ref83[0], ref83[1], opt["lat"], opt["lng"])
            print(
                f"  geoid={opt['geoid']} lat={opt['lat']:.5f} lng={opt['lng']:.5f} "
                f"d92={d92:.0f}m d83={d83:.0f}m"
            )


if __name__ == "__main__":
    asyncio.run(main())
