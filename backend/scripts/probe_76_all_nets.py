import asyncio

from app.services.geo import haversine_m
from app.services.knooppunten import _fetch_bbox


async def main() -> None:
    nodes, _ = await _fetch_bbox((4.55, 50.92, 4.75, 51.08))
    ref92 = (51.0512, 4.6906)
    ref83 = (50.9869, 4.6409)
    for nr in ["76", "83", "92"]:
        opts = [n for n in nodes if n["number"] == nr]
        print(nr, len(opts))
        for opt in sorted(opts, key=lambda n: haversine_m(ref92[0], ref92[1], n["lat"], n["lng"]))[:5]:
            d92 = haversine_m(ref92[0], ref92[1], opt["lat"], opt["lng"])
            d83 = haversine_m(ref83[0], ref83[1], opt["lat"], opt["lng"])
            print(f"  {opt.get('network')} geoid={opt['geoid']} d92={d92:.0f} d83={d83:.0f}")


if __name__ == "__main__":
    asyncio.run(main())
