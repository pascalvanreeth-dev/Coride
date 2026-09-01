import asyncio

from app.services.knooppunten import _overpass
from app.services.geo import haversine_m

MID = (51.025, 4.665)


async def main() -> None:
    query = f"[out:json][timeout:25];node[\"rcn_ref\"](around:2000,{MID[0]},{MID[1]});out;"
    data = await _overpass(query)
    for el in data.get("elements") or []:
        ref = (el.get("tags") or {}).get("rcn_ref")
        if not ref:
            continue
        lat, lon = el.get("lat"), el.get("lon")
        print(f"rcn={ref} lat={lat:.5f} lon={lon:.5f}")


if __name__ == "__main__":
    asyncio.run(main())
