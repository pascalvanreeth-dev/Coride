import asyncio
import json

import httpx


async def main() -> None:
    url = "https://geodata.toerismevlaanderen.be/geoserver/wfs"
    params = {
        "service": "WFS",
        "version": "1.1.0",
        "request": "GetFeature",
        "typeName": "routes:knoop_fiets",
        "outputFormat": "application/json",
        "srsName": "EPSG:4326",
        "maxFeatures": 3,
        "cql_filter": "BBOX(geom,4.5,51.1,4.6,51.2,'EPSG:4326') AND knooptype=1",
    }
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.get(url, params=params)
        print("status", response.status_code)
        data = response.json()
        for feature in data.get("features") or []:
            print(json.dumps(feature.get("properties"), indent=2))
            print("id", feature.get("id"))


if __name__ == "__main__":
    asyncio.run(main())
