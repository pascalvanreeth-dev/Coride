import asyncio

from app.services.geo import distance_point_to_geometry
from app.services.planner import _build_knoop_route
from scripts.probe_76_snap import build_spine, _fetch_bbox_fallback


async def main() -> None:
    nodes, _ = await _fetch_bbox_fallback()
    spine = build_spine(nodes)
    display, route = await _build_knoop_route(51.05, 4.55, spine, close_loop=True)
    geom = route["geometry"]
    dnums = [n["number"] for n in display]
    print("76 in display", "76" in dnums)
    print("numbers", dnums)
    for n in display:
        if n["number"] in ("32", "76", "83", "92", "84", "90"):
            d = int(distance_point_to_geometry(n["lat"], n["lng"], geom))
            print(n["number"], d, n.get("geoid"))


if __name__ == "__main__":
    asyncio.run(main())
