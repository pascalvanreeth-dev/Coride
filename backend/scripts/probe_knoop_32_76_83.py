import asyncio

from app.http import client
from app.services.geo import haversine_m
from app.services.knooppunten import (
    WFS_URL,
    _fetch_bbox,
    enrich_chain_geoids,
    expand_chain,
    index_trajects,
    geometry_between_nodes,
    geometry_through_network,
)


async def fetch_by_number(number: int) -> list:
    async with client() as http:
        response = await http.get(
            WFS_URL,
            params={
                "service": "WFS",
                "version": "1.1.0",
                "request": "GetFeature",
                "typeName": "routes:knoop_fiets",
                "outputFormat": "application/json",
                "srsName": "EPSG:4326",
                "maxFeatures": 20,
                "cql_filter": f"knoopnr={number} AND knooptype=1",
            },
            timeout=15.0,
        )
        return response.json().get("features") or []


async def main() -> None:
    for nr in [32, 76, 83, 36, 92]:
        feats = await fetch_by_number(nr)
        print(f"Knoop {nr}: {len(feats)} WFS features")
        for feature in feats:
            props = feature.get("properties") or {}
            coords = (feature.get("geometry") or {}).get("coordinates") or []
            if len(coords) < 2:
                continue
            print(
                f"  geoid={props.get('geoid')} lat={coords[1]:.5f} lng={coords[0]:.5f} "
                f"naam={props.get('naam')}"
            )

    # bbox around 83
    bbox = (4.40, 50.85, 4.90, 51.20)
    nodes, trajects = await _fetch_bbox(bbox)
    by_num: dict[str, list] = {}
    for node in nodes:
        by_num.setdefault(str(node["number"]), []).append(node)

    wfs_nodes = []
    for nr in [32, 76, 83]:
        feats = await fetch_by_number(nr)
        for feature in feats:
            props = feature.get("properties") or {}
            coords = (feature.get("geometry") or {}).get("coordinates") or []
            if len(coords) < 2:
                continue
            wfs_nodes.append(
                {
                    "number": str(props.get("knoopnr")),
                    "lat": float(coords[1]),
                    "lng": float(coords[0]),
                    "geoid": props.get("geoid"),
                }
            )
    # merge into nodes list for path finding
    all_nodes = list(nodes)
    for wn in wfs_nodes:
        if not any(int(n.get("geoid") or -1) == int(wn.get("geoid") or -2) for n in all_nodes if n.get("geoid")):
            all_nodes.append(wn)

    by_geoid = {int(n["geoid"]): n for n in all_nodes if n.get("geoid")}
    n32 = next((n for n in wfs_nodes if n["number"] == "32"), None)
    n76 = next((n for n in wfs_nodes if n["number"] == "76"), None)
    n83 = next((n for n in wfs_nodes if n["number"] == "83"), None)
    if not n32 or not n83:
        print("missing 32 or 83")
        return

    by_edge, edge_length, adj = index_trajects(trajects)
    seg, ln = geometry_between_nodes(n32, n83, by_edge, edge_length, adj, by_geoid)
    print(f"Direct 32->83 traject: len={ln:.0f}m points={len(seg or [])}")

    seg76, ln76 = geometry_between_nodes(n32, n76, by_edge, edge_length, adj, by_geoid)
    print(f"Direct 32->76 traject: len={ln76:.0f}m points={len(seg76 or [])}")

    seg7683, ln7683 = geometry_between_nodes(n76, n83, by_edge, edge_length, adj, by_geoid)
    print(f"Direct 76->83 traject: len={ln7683:.0f}m points={len(seg7683 or [])}")

    through, tln = geometry_through_network(n32, n83, by_edge, edge_length, adj, by_geoid)
    print(f"Through network 32->83: len={tln:.0f}m points={len(through or [])}")

    elat, elng = 50.9869, 4.6409
    for nr in ["76", "32", "83", "36", "92"]:
        opts = [n for n in nodes if n["number"] == nr]
        opts.sort(key=lambda n: haversine_m(elat, elng, n["lat"], n["lng"]))
        print(f"{nr} closest to Emblem:")
        for o in opts[:3]:
            dist = haversine_m(elat, elng, o["lat"], o["lng"])
            print(f"  geoid={o['geoid']} dist={dist:.0f}m net={o.get('network')}")

    vb = [n for n in nodes if n.get("network") == "Fietsnetwerk Vlaams-Brabant"]
    vb_geoids = {int(n["geoid"]) for n in vb if n.get("geoid")}
    vb_trajects = [
        t
        for t in trajects
        if int(t["begin_geoid"]) in vb_geoids and int(t["end_geoid"]) in vb_geoids
    ]
    by_geoid = {int(n["geoid"]): n for n in vb}
    n32 = next(n for n in vb if n["number"] == "32")
    n83 = next(n for n in vb if n["number"] == "83")
    n76 = next((n for n in vb if n["number"] == "76"), None)
    print("VB 76:", n76)
    by_edge, edge_length, adj = index_trajects(vb_trajects)
    seg, ln = geometry_between_nodes(n32, n83, by_edge, edge_length, adj, by_geoid)
    print(f"VB 32->83 direct: {len(seg or [])} pts len={ln:.0f}")
    if n76:
        seg2, ln2 = geometry_between_nodes(n32, n76, by_edge, edge_length, adj, by_geoid)
        print(f"VB 32->76: {len(seg2 or [])} pts len={ln2:.0f}")
        seg3, ln3 = geometry_between_nodes(n76, n83, by_edge, edge_length, adj, by_geoid)
        print(f"VB 76->83: {len(seg3 or [])} pts len={ln3:.0f}")
    expanded = expand_chain(enrich_chain_geoids([n32, n83], vb), vb, vb_trajects)
    print("VB expand 32->83:", [x["number"] for x in expanded])
    ant = [n for n in nodes if "Antwerpen" in str(n.get("network") or "")]
    for nr in ["32", "76", "83", "75", "31"]:
        for o in [n for n in ant if n["number"] == nr]:
            print(f"Ant {nr}: geoid={o['geoid']} lat={o['lat']:.5f} lng={o['lng']:.5f}")

    ant_geoids = {int(n["geoid"]) for n in ant if n.get("geoid")}
    ant_trajects = [
        t
        for t in trajects
        if int(t["begin_geoid"]) in ant_geoids and int(t["end_geoid"]) in ant_geoids
    ]
    by_geoid = {int(n["geoid"]): n for n in ant}
    n32 = next((n for n in ant if n["number"] == "32"), None)
    n76 = next((n for n in ant if n["number"] == "76"), None)
    n83 = next((n for n in ant if n["number"] == "83"), None)
    by_edge, edge_length, adj = index_trajects(ant_trajects)
    if n32 and n76:
        seg, ln = geometry_between_nodes(n32, n76, by_edge, edge_length, adj, by_geoid)
        print(f"Antwerpen 32->76: points={len(seg or [])} len={ln:.0f}")
    if n76 and n83:
        seg, ln = geometry_between_nodes(n76, n83, by_edge, edge_length, adj, by_geoid)
        print(f"Antwerpen 76->83: points={len(seg or [])} len={ln:.0f}")
    if n32 and n83:
        enriched = enrich_chain_geoids([n32, n83], ant)
        expanded = expand_chain(enriched, ant, ant_trajects)
        print("Antwerpen expand 32->83:", [x["number"] for x in expanded])


if __name__ == "__main__":
    asyncio.run(main())
