import asyncio

from app.services.geo import haversine_m
from app.services.knooppunten import _from_wfs

EMBLEM = (50.9869, 4.6409)
REF_92 = (51.0512, 4.6906)


async def main() -> None:
    wfs = await _from_wfs([EMBLEM, REF_92], 4000)
    near_emblem = sorted(
        [n for n in wfs if haversine_m(EMBLEM[0], EMBLEM[1], n["lat"], n["lng"]) < 3000],
        key=lambda n: haversine_m(EMBLEM[0], EMBLEM[1], n["lat"], n["lng"]),
    )
    print("WFS knopen binnen 3km van Emblem 83:")
    for n in near_emblem:
        d = haversine_m(EMBLEM[0], EMBLEM[1], n["lat"], n["lng"])
        print(f"  nr={n['number']:>3} geoid={n.get('geoid')} d={d:.0f}m")

    near_mid = sorted(
        wfs,
        key=lambda n: haversine_m(51.019, 4.675, n["lat"], n["lng"]),
    )
    print("\nDichtst bij middelpunt 92-83 (top 15):")
    for n in near_mid[:15]:
        d = haversine_m(51.019, 4.675, n["lat"], n["lng"])
        print(f"  nr={n['number']:>3} geoid={n.get('geoid')} d={d:.0f}m lat={n['lat']:.4f}")


if __name__ == "__main__":
    asyncio.run(main())
