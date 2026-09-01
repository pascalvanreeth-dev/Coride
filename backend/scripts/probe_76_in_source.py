"""Check whether knoop 76 exists in WFS/OSM near 92-83 corridor (Emblem/Netekanaal)."""
import asyncio

from app.services.geo import haversine_m
from app.services.knooppunten import _fetch_bbox, _from_wfs, _overpass, filter_network_data

REF_92 = (51.0512, 4.6906)  # Antwerpen 92 near Netekanaal
REF_83 = (50.9869, 4.6409)  # VB Emblem 83
CORRIDOR = (51.019, 4.675)  # midpoint


async def wfs_76_global_near() -> None:
    print("=== WFS knoopnr=76 (cql) near corridor ===")
    # bbox around corridor ~8km
    points = [CORRIDOR, REF_92, REF_83]
    radius = 12000
    wfs = await _from_wfs(points, radius)
    all76 = [n for n in wfs if n["number"] == "76"]
    print(f"Total WFS nodes in fetch: {len(wfs)}, number=76: {len(all76)}")
    for n in sorted(all76, key=lambda x: haversine_m(REF_92[0], REF_92[1], x["lat"], x["lng"])):
        d92 = haversine_m(REF_92[0], REF_92[1], n["lat"], n["lng"])
        d83 = haversine_m(REF_83[0], REF_83[1], n["lat"], n["lng"])
        print(
            f"  geoid={n.get('geoid')} network={n.get('network')} "
            f"lat={n['lat']:.5f} lng={n['lng']:.5f} d92={d92:.0f}m d83={d83:.0f}m"
        )
    if not all76:
        print("  -> GEEN knoop 76 in WFS binnen ~12km van corridor")


async def wfs_by_network() -> None:
    print("\n=== WFS 76 per netwerk (grote bbox) ===")
    nodes, trajects = await _fetch_bbox((4.40, 50.85, 4.90, 51.20))
    for net in ["Fietsroutenetwerk Antwerpen", "Fietsnetwerk Vlaams-Brabant"]:
        filtered, _ = filter_network_data(nodes, trajects, net)
        opts = [n for n in filtered if n["number"] == "76"]
        print(f"{net}: {len(opts)} x 76")
        for o in opts:
            d92 = haversine_m(REF_92[0], REF_92[1], o["lat"], o["lng"])
            print(f"  geoid={o['geoid']} lat={o['lat']:.5f} lng={o['lng']:.5f} d92={d92:.0f}m")


async def osm_76_corridor() -> None:
    print("\n=== OSM rcn_ref=76 in corridor bbox ===")
  # south,west,north,east
    query = "[out:json][timeout:25];node[\"rcn_ref\"=\"76\"](50.98,4.60,51.08,4.72);out;"
    try:
        data = await _overpass(query)
        els = data.get("elements") or []
        print(f"OSM nodes with rcn_ref=76: {len(els)}")
        for el in els:
            lat, lon = el.get("lat"), el.get("lon")
            if lat and lon:
                d92 = haversine_m(REF_92[0], REF_92[1], lat, lon)
                print(f"  osm_id={el['id']} lat={lat:.5f} lon={lon:.5f} d92={d92:.0f}m")
        if not els:
            print("  -> GEEN OSM knoop 76 in bbox 50.98-51.08, 4.60-4.72")
    except Exception as exc:
        print(f"  Overpass error: {exc}")


async def path_numbers() -> None:
    from app.services.knooppunten import _shortest_path, build_adjacency

    print("\n=== Officiële traject-keten 92 -> VB Emblem 83 ===")
    nodes, trajects = await _fetch_bbox((4.40, 50.85, 4.90, 51.20))
    geos = {int(n["geoid"]) for n in nodes if n.get("geoid")}
    adj = build_adjacency(
        [t for t in trajects if int(t["begin_geoid"]) in geos and int(t["end_geoid"]) in geos]
    )
    by = {int(n["geoid"]): n for n in nodes if n.get("geoid")}
    path = _shortest_path(adj, 5440047, 5421306)
    if path:
        nums = [by[g]["number"] for g in path if g in by]
        print("  nummers:", nums)
        print("  bevat 76:", "76" in nums)
    else:
        print("  geen pad gevonden")


async def main() -> None:
    await wfs_76_global_near()
    await wfs_by_network()
    await osm_76_corridor()
    await path_numbers()


if __name__ == "__main__":
    asyncio.run(main())
