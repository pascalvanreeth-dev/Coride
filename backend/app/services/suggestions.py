from __future__ import annotations

from typing import Any

from app.models import Locality
from app.services.geo import haversine_m

TOP_10: list[dict[str, Any]] = [
    {
        "rank": 1,
        "id": "brugge-kastelenroute",
        "city": "brugge",
        "title": "Kastelenroute Brugge",
        "highlight": "Kasteel van Wijnendale",
        "start": "Brugge, Markt",
        "lat": 51.209,
        "lng": 3.225,
        "mode": "lus",
        "distance_km": 50,
        "interests": ["geschiedenis", "architectuur"],
        "municipalities": ["Brugge", "Damme", "Beernem", "Oostkamp"],
        "localities": [
            {"name": "Brugge", "lat": 51.209, "lng": 3.225, "fact": "Middeleeuwse binnenstad en uitgangspunt van de kastelenlus."},
            {"name": "Damme", "lat": 51.251, "lng": 3.283, "fact": "Pittoresk vestingstadje aan het Damse Vaart."},
            {"name": "Beernem", "lat": 51.139, "lng": 3.338, "fact": "Polders en kasteeldomeinen rond Wijnendale."},
            {"name": "Oostkamp", "lat": 51.154, "lng": 3.235, "fact": "Landelijke polders tussen Brugge en de kustvlakte."},
        ],
        "notes": "Kastelenroute rond Brugge: Wijnendale, Beisbroek, polders en terug via Damme.",
        "popularity": 100,
    },
    {
        "rank": 2,
        "id": "zwinroute",
        "city": "knokke",
        "title": "Zwinroute",
        "highlight": "Zwin natuurpark",
        "start": "Knokke-Heist, Zwin",
        "lat": 51.366,
        "lng": 3.373,
        "mode": "lus",
        "distance_km": 48,
        "interests": ["natuur", "activiteiten"],
        "municipalities": ["Knokke-Heist", "Brugge", "Zeebrugge"],
        "localities": [
            {"name": "Knokke-Heist", "lat": 51.366, "lng": 3.373, "fact": "Duinen, dijken en toegang tot het Zwin natuurpark."},
            {"name": "Zeebrugge", "lat": 51.329, "lng": 3.197, "fact": "Haven en kust met zeevaart en vogeltrek."},
            {"name": "Heist", "lat": 51.340, "lng": 3.238, "fact": "Woonkern tussen duinen en polderwegen."},
        ],
        "notes": "Kust, duinen en het Zwin — vogels, dijken en polderwegen.",
        "popularity": 98,
    },
    {
        "rank": 3,
        "id": "diamantpad",
        "city": "lier",
        "title": "Het Diamantpad",
        "highlight": "Diamantroute Kempen",
        "start": "Lier, Grote Markt",
        "lat": 51.131,
        "lng": 4.570,
        "mode": "lus",
        "distance_km": 53,
        "interests": ["geschiedenis", "landbouw"],
        "municipalities": ["Lier", "Nijlen", "Berlaar", "Grobbendonk"],
        "localities": [
            {"name": "Lier", "lat": 51.131, "lng": 4.570, "fact": "Stad aan de Nete, bekend om de Zimmertoren en het stadhuis."},
            {"name": "Nijlen", "lat": 51.161, "lng": 4.670, "fact": "Geboorteplaats van de Kempense diamantslijperij."},
            {"name": "Berlaar", "lat": 51.117, "lng": 4.634, "fact": "Langs de Kleine Nete, midden in het diamantverleden."},
            {"name": "Grobbendonk", "lat": 51.190, "lng": 4.735, "fact": "Heuvels, heide en bos tussen Nete en Antwerpen."},
        ],
        "notes": "Diamantroute rond Lier: Kempens diamantverleden langs Kleine en Grote Nete, Nijlen en Grobbendonk.",
        "popularity": 97,
    },
    {
        "rank": 4,
        "id": "leieloop-gent",
        "city": "gent",
        "title": "Leieloop Gent",
        "highlight": "Leie & kastelen",
        "start": "Gent, Gravensteen",
        "lat": 51.057,
        "lng": 3.720,
        "mode": "lus",
        "distance_km": 50,
        "interests": ["geschiedenis", "natuur"],
        "municipalities": ["Gent", "Deinze", "Nevele", "Ooidonk"],
        "localities": [
            {"name": "Gent", "lat": 51.057, "lng": 3.720, "fact": "Historische stad aan samenvloeiing van Leie en Schelde."},
            {"name": "Deinze", "lat": 50.986, "lng": 3.527, "fact": "Leiestad met kasteeldomeinen en jaagpaden."},
            {"name": "Nevele", "lat": 51.034, "lng": 3.545, "fact": "Groene Leievallei met hoeves en oude kasteelparken."},
        ],
        "notes": "Langs de Leie, Ooidonk, Deinze en de Gentse rand.",
        "popularity": 96,
    },
    {
        "rank": 5,
        "id": "groene-gordel",
        "city": "brussel",
        "title": "Groene Gordel",
        "highlight": "Zoniënwoud",
        "start": "Brussel, Terkamerenbos",
        "lat": 50.762,
        "lng": 4.374,
        "mode": "lus",
        "distance_km": 49,
        "interests": ["natuur", "landbouw"],
        "municipalities": ["Tervuren", "Overijse", "Hoeilaart", "Watermaal-Bosvoorde"],
        "localities": [
            {"name": "Tervuren", "lat": 50.823, "lng": 4.514, "fact": "Koninklijk park en groene rand rond Brussel."},
            {"name": "Overijse", "lat": 50.774, "lng": 4.535, "fact": "Wijngaarden en heuvels in het Brabantse landschap."},
            {"name": "Hoeilaart", "lat": 50.768, "lng": 4.468, "fact": "Dennenbossen en druiventeelt op de Brusselse rand."},
        ],
        "notes": "Groene gordel rond Brussel: bos, kastelen en landelijke knooppunten.",
        "popularity": 95,
    },
    {
        "rank": 6,
        "id": "scheldeland-mechelen",
        "city": "mechelen",
        "title": "Scheldelandroute",
        "highlight": "Schelde & Dijle",
        "start": "Mechelen, Grote Markt",
        "lat": 51.026,
        "lng": 4.479,
        "mode": "lus",
        "distance_km": 51,
        "interests": ["geschiedenis", "natuur"],
        "municipalities": ["Mechelen", "Willebroek", "Puurs", "Bornem"],
        "localities": [
            {"name": "Mechelen", "lat": 51.026, "lng": 4.479, "fact": "Historische stad tussen Dijle en rivieren van het Scheldeland."},
            {"name": "Willebroek", "lat": 51.060, "lng": 4.360, "fact": "Scheldehaven en polders ten noorden van Mechelen."},
            {"name": "Puurs", "lat": 51.074, "lng": 4.290, "fact": "Landelijk Scheldeland met dijken en weilanden."},
        ],
        "notes": "Mechelen, rivieren, polders en het Scheldeland.",
        "popularity": 94,
    },
    {
        "rank": 7,
        "id": "westhoek-ieper",
        "city": "ieper",
        "title": "Westhoek & Ieperboog",
        "highlight": "Menenpoort",
        "start": "Ieper, Grote Markt",
        "lat": 50.851,
        "lng": 2.885,
        "mode": "lus",
        "distance_km": 50,
        "interests": ["oorlog", "geschiedenis"],
        "municipalities": ["Ieper", "Zonnebeke", "Poperinge", "Heuvelland"],
        "localities": [
            {"name": "Ieper", "lat": 50.851, "lng": 2.885, "fact": "Herinneringsstad met de Menenpoort en WO I-erfgoed."},
            {"name": "Zonnebeke", "lat": 50.873, "lng": 2.983, "fact": "Slagvelden en Polygon Wood in de Ieperboog."},
            {"name": "Poperinge", "lat": 50.855, "lng": 2.726, "fact": "Achterlandstad van de Westhoek, bekend om hopvelden."},
        ],
        "notes": "WO I-erfgoed, heuvels en landweer rond Ieper.",
        "popularity": 93,
    },
    {
        "rank": 8,
        "id": "haspengouwroute",
        "city": "tongeren",
        "title": "Haspengouwroute",
        "highlight": "Haspengouw",
        "start": "Tongeren, Grote Markt",
        "lat": 50.781,
        "lng": 5.464,
        "mode": "lus",
        "distance_km": 47,
        "interests": ["landbouw", "geschiedenis"],
        "municipalities": ["Tongeren", "Borgloon", "Herstappe", "Hoeselt"],
        "localities": [
            {"name": "Tongeren", "lat": 50.781, "lng": 5.464, "fact": "Oudste stad van België, Romeins erfgoed en fruitmarkt."},
            {"name": "Borgloon", "lat": 50.805, "lng": 5.343, "fact": "Heuvels, boomgaarden en het kunstproject Reading between the Lines."},
            {"name": "Hoeselt", "lat": 50.849, "lng": 5.489, "fact": "Wijngaarden en fruitboomgaarden in Haspengouw."},
        ],
        "notes": "Fruitstreek, heuvels en Romeins erfgoed in Haspengouw.",
        "popularity": 92,
    },
    {
        "rank": 9,
        "id": "kustroute-west",
        "city": "oostende",
        "title": "Kustroute West-Vlaanderen",
        "highlight": "Kusttramlijn",
        "start": "Oostende, Station",
        "lat": 51.228,
        "lng": 2.920,
        "mode": "lus",
        "distance_km": 53,
        "interests": ["natuur", "activiteiten"],
        "municipalities": ["Oostende", "Bredene", "De Haan", "Blankenberge"],
        "localities": [
            {"name": "Oostende", "lat": 51.228, "lng": 2.920, "fact": "Badstad aan de Noordzee met dijken en haven."},
            {"name": "Bredene", "lat": 51.235, "lng": 2.975, "fact": "Duinen en polders langs de kusttramlijn."},
            {"name": "Blankenberge", "lat": 51.314, "lng": 3.132, "fact": "Pier, zee en duinvegetatie aan de Westkust."},
        ],
        "notes": "Van Oostende langs de kust richting Blankenberge en terug door polders.",
        "popularity": 91,
    },
    {
        "rank": 10,
        "id": "maasvalleiroute",
        "city": "hasselt",
        "title": "Maasvalleiroute",
        "highlight": "Maasvallei",
        "start": "Hasselt, Grote Markt",
        "lat": 50.931,
        "lng": 5.337,
        "mode": "lus",
        "distance_km": 50,
        "interests": ["natuur", "landbouw"],
        "municipalities": ["Hasselt", "Genk", "Diepenbeek", "Zutendaal"],
        "localities": [
            {"name": "Hasselt", "lat": 50.931, "lng": 5.337, "fact": "Jeneverstad en poort naar de Limburgse Maasvallei."},
            {"name": "Genk", "lat": 50.965, "lng": 5.502, "fact": "Groene stad tussen kolenspoor en natuurreservaten."},
            {"name": "Diepenbeek", "lat": 50.907, "lng": 5.419, "fact": "Demervallei met bossen en landelijke wegen."},
        ],
        "notes": "Demer en Maasvallei, jeneverstad Hasselt en groene Limburgse lus.",
        "popularity": 90,
    },
]

INTEREST_LABELS: dict[str, str] = {
    "geschiedenis": "Geschiedenis",
    "natuur": "Natuur & vegetatie",
    "landbouw": "Landbouw",
    "horeca": "Horeca",
    "oorlog": "Oorlog",
    "architectuur": "Architectuur",
    "activiteiten": "Activiteiten",
    "evenementen": "Evenementen",
}


def get_route_by_id(route_id: str) -> dict[str, Any] | None:
    for route in TOP_10:
        if route["id"] == route_id:
            return dict(route)
    return None


def merge_interests(route: dict[str, Any], user_interests: list[str] | None) -> list[str]:
    merged: list[str] = []
    for item in list(route.get("interests") or []) + list(user_interests or []):
        if item and item not in merged:
            merged.append(item)
    return merged or ["geschiedenis"]


def merge_notes(route: dict[str, Any], user_notes: str) -> str:
    base = (route.get("notes") or "").strip()
    extra = (user_notes or "").strip()
    if base and extra and extra not in base:
        return f"{base} {extra}"
    return extra or base


def merge_localities(route: dict[str, Any], existing: list[Locality]) -> list[Locality]:
    seen = {item.name.lower() for item in existing}
    merged = list(existing)
    for item in route.get("localities") or []:
        name = (item.get("name") or "").strip()
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        merged.append(
            Locality(
                name=name,
                municipality=name,
                population=None,
                fact=(item.get("fact") or "").strip(),
                lat=float(item["lat"]),
                lng=float(item["lng"]),
            )
        )
    return merged


def catalog_intro(route: dict[str, Any], interests: list[str], km: float, start_label: str) -> str:
    places = ", ".join(route.get("municipalities") or [])
    themes = ", ".join(INTEREST_LABELS.get(item, item) for item in interests[:5])
    bits = [
        f"Route Top 10: {route['title']}.",
        f"Vanaf {start_label.split(',')[0]} fiets je ongeveer {km} km.",
    ]
    if places:
        bits.append(f"Gemeenten onderweg: {places}.")
    if themes:
        bits.append(f"Thema's: {themes}.")
    if route.get("notes"):
        bits.append(route["notes"])
    return " ".join(bits)


def _interest_score(route: dict[str, Any], interests: list[str] | None) -> int:
    if not interests:
        return 0
    route_set = set(route.get("interests") or [])
    return len(route_set.intersection(interests))


def _with_distance(route: dict[str, Any], lat: float, lng: float) -> dict[str, Any]:
    km = haversine_m(lat, lng, route["lat"], route["lng"]) / 1000
    return {**route, "distance_from_you_km": round(km, 1)}


def suggest_routes(
    lat: float,
    lng: float,
    interests: list[str] | None = None,
    used_ids: list[str] | None = None,
    limit: int = 10,
) -> list[dict[str, Any]]:
    used = set(used_ids or [])
    user_interests = interests or []
    routes = [_with_distance(dict(route), lat, lng) for route in TOP_10[:limit]]

    for route in routes:
        route["used_before"] = route["id"] in used
        route["swapped_from"] = None
        route["match_score"] = _interest_score(route, user_interests)
        route.pop("localities", None)

    routes.sort(key=lambda item: (item["used_before"], -item["match_score"], item["rank"]))
    return routes
