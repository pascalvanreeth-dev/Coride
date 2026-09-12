from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
import asyncio
import httpx

from app.models import AskRequest, AskResponse, BikeLegRequest, BikeLegResponse, BikeRouteRequest, GeocodeHit, IcoonrouteStretch, Knooppunt, PlanRequest, PoiHit, RerouteRequest, RerouteResponse, RoutePlan, RoutePreviewRequest, RoutePreviewResponse, RouteSuggestion, StopSummaryResponse, SurroundingsRequest, SurroundingsResponse, WishSuggestionsRequest, WishSuggestionsResponse
from app.services.ai import answer_about_stop
from app.services.geocoding import geocode, reverse
from app.services import knooppunten as knoop_service
from app.services.planner import plan_route, preview_route, reroute, wish_suggestions_along_route
from app.services import pois as pois_service
from app.services import routing as routing_service
from app.services import suggestions as suggestion_service
from app.services import icoonroutes as icoon_service
from app.services import surroundings as surroundings_service
from app.services import wikipedia as wikipedia_service

app = FastAPI(title="Veloverhaal", version="0.2.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/geocode", response_model=list[GeocodeHit])
async def geocode_endpoint(q: str = Query(min_length=2, max_length=200)) -> list[GeocodeHit]:
    try:
        hits = await asyncio.wait_for(geocode(q), timeout=6.0)
    except TimeoutError:
        return []
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=502,
            detail="Plaats zoeken is tijdelijk niet beschikbaar. Probeer het zo dadelijk opnieuw.",
        ) from exc
    if not hits:
        return []
    return hits


@app.get("/api/reverse", response_model=GeocodeHit)
async def reverse_endpoint(
    lat: float = Query(ge=49.0, le=52.0),
    lng: float = Query(ge=2.0, le=7.0),
) -> GeocodeHit:
    try:
        hit = await reverse(lat, lng)
        if not hit:
            raise ValueError("Geen adres gevonden voor dit GPS-punt.")
        return hit
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/api/knooppunten", response_model=list[Knooppunt])
async def knooppunten_endpoint(
    lat: float = Query(ge=49.0, le=52.0),
    lng: float = Query(ge=2.0, le=7.0),
    radius: int = Query(default=12000, ge=2000, le=18000),
) -> list[Knooppunt]:
    try:
        nodes = await knoop_service.fetch_nodes(lat, lng, radius)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return [
        Knooppunt(
            id=str(node.get("id") or ""),
            number=str(node["number"]),
            lat=float(node["lat"]),
            lng=float(node["lng"]),
            network=node.get("network"),
            geoid=node.get("geoid"),
        )
        for node in nodes
    ]


@app.post("/api/bike-warm")
async def bike_warm_endpoint(
    lat: float | None = Query(default=None, ge=49.0, le=52.0),
    lng: float | None = Query(default=None, ge=2.0, le=7.0),
    geoid: int | None = Query(default=None),
) -> dict[str, str]:
    """Prefetch netwerk rond een knoop/gebied zodat magenta-legs lokaal zijn."""
    if geoid is not None:
        asyncio.create_task(knoop_service.prefetch_geoid_star(geoid))
    if lat is not None and lng is not None:
        asyncio.create_task(knoop_service.warm_area_network(lat, lng, 12000))
    return {"status": "warming"}


@app.get("/api/bike-network")
async def bike_network_endpoint(
    lat: float = Query(ge=49.0, le=52.0),
    lng: float = Query(ge=2.0, le=7.0),
    radius: int = Query(default=12000, ge=2000, le=16000),
) -> dict:
    """Volledig knooppuntennetwerk (nodes + trajecten) voor lokale magenta-kleuring."""
    try:
        return await knoop_service.fetch_viewport_network(lat, lng, radius)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/api/poi-suggestions", response_model=list[PoiHit])
async def poi_suggestions_endpoint(
    lat: float = Query(ge=49.0, le=52.0),
    lng: float = Query(ge=2.0, le=7.0),
    interests: list[str] = Query(default=[]),
    radius: int = Query(default=7000, ge=2000, le=16000),
    sample_lat: list[float] = Query(default=[]),
    sample_lng: list[float] = Query(default=[]),
) -> list[PoiHit]:
    wanted = pois_service._unique_interests(interests)
    extra = None
    if sample_lat and sample_lng:
        extra = (float(sample_lat[-1]), float(sample_lng[-1]))
    try:
        pois = await asyncio.wait_for(
            pois_service.fetch_pois(lat, lng, radius, wanted, extra),
            timeout=8.0,
        )
    except Exception as exc:  # noqa: BLE001
        detail = str(exc)
        if "overpass" in detail.lower():
            detail = "Kaartdata (OpenStreetMap) is tijdelijk niet bereikbaar. Probeer het over een minuut opnieuw."
        raise HTTPException(status_code=502, detail=detail) from exc
    interest_set = set(wanted)
    scored: list[tuple[int, dict]] = []
    for poi in pois:
        score = 0
        if poi.get("interest") in interest_set:
            score += 10
        if poi.get("wikipedia") or poi.get("wikidata"):
            score += 2
        if poi.get("description"):
            score += 1
        scored.append((score, poi))
    scored.sort(key=lambda item: (-item[0], item[1]["name"]))
    ranked = [poi for _, poi in scored]
    pool = pois_service.build_stop_pool(ranked, wanted)
    diverse = pois_service.pick_diverse_pois(pool, wanted, wanted=min(16, max(8, len(wanted) * 2)))
    hits: list[PoiHit] = []
    for poi in diverse:
        interest = poi.get("interest") or "geschiedenis"
        if interest not in interest_set:
            interest = wanted[0]
        try:
            hits.append(
                PoiHit(
                    id=str(poi["id"]),
                    name=poi["name"],
                    lat=float(poi["lat"]),
                    lng=float(poi["lng"]),
                    kind=str(poi.get("kind") or "plek"),
                    kind_label=poi.get("kind_label"),
                    interest=interest,
                )
            )
        except Exception:
            continue
    return hits


@app.get("/api/route-suggestions", response_model=list[RouteSuggestion])
async def route_suggestions_endpoint(
    lat: float = Query(ge=49.0, le=52.0),
    lng: float = Query(ge=2.0, le=7.0),
    interests: list[str] = Query(default=[]),
    used: list[str] = Query(default=[]),
) -> list[RouteSuggestion]:
    try:
        items = await suggestion_service.suggest_routes(lat, lng, interests, used)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return [RouteSuggestion(**{k: v for k, v in item.items() if k in RouteSuggestion.model_fields}) for item in items]


@app.get("/api/icoonroute/{route_id}", response_model=IcoonrouteStretch)
async def icoonroute_stretch_endpoint(
    route_id: str,
    lat: float = Query(ge=49.0, le=52.0),
    lng: float = Query(ge=2.0, le=7.0),
    target_km: float = Query(default=50, ge=8, le=90),
) -> IcoonrouteStretch:
    try:
        data = await icoon_service.stretch_for_route(route_id, lat, lng, target_km)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    knoops = [
        Knooppunt(
            id=str(n.get("id") or ""),
            number=str(n["number"]),
            lat=float(n["lat"]),
            lng=float(n["lng"]),
            network=n.get("network"),
            geoid=n.get("geoid"),
        )
        for n in data.get("knooppunten") or []
    ]
    return IcoonrouteStretch(
        id=data["id"],
        title=data["title"],
        highlight=data.get("highlight") or "",
        start=data.get("start") or data.get("start_label") or data["title"],
        start_label=data.get("start_label") or "",
        lat=float(data["lat"]),
        lng=float(data["lng"]),
        mode=data.get("mode") or "punt",
        distance_km=float(data.get("distance_km") or target_km),
        interests=list(data.get("interests") or []),
        notes=data.get("notes") or "",
        knooppunten=knoops,
        geometry=list(data.get("geometry") or []),
        legs=list(data.get("legs") or []),
        source=data.get("source") or "Toerisme Vlaanderen icoonroutes",
    )


@app.post("/api/route-preview", response_model=RoutePreviewResponse)
async def route_preview_endpoint(request: RoutePreviewRequest) -> RoutePreviewResponse:
    try:
        preview = await preview_route(
            request.lat,
            request.lng,
            request.distance_km,
            request.mode,
            request.end_lat,
            request.end_lng,
            request.notes,
            request.poi_picks,
            list(request.interests),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return RoutePreviewResponse(**preview)


@app.post("/api/wish-suggestions", response_model=WishSuggestionsResponse)
async def wish_suggestions_endpoint(request: WishSuggestionsRequest) -> WishSuggestionsResponse:
    try:
        data = await asyncio.wait_for(
            wish_suggestions_along_route(
                request.notes,
                request.geometry,
                request.nodes,
                list(request.interests),
            ),
            timeout=10.0,
        )
    except TimeoutError:
        # Liever leeg + client-retry dan hang tot browser-abort ("duurde te lang").
        return WishSuggestionsResponse(suggestions=[], wish_summary=None)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return WishSuggestionsResponse(**data)


@app.post("/api/plan", response_model=RoutePlan)
async def plan_endpoint(request: PlanRequest) -> RoutePlan:
    if not request.interests:
        request.interests = ["geschiedenis"]
    try:
        return await asyncio.wait_for(plan_route(request), timeout=70.0)
    except TimeoutError as exc:
        raise HTTPException(
            status_code=504,
            detail="Het plannen duurde te lang. Probeer opnieuw of kies zelf knooppunten.",
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/api/stop-summary", response_model=StopSummaryResponse)
async def stop_summary_endpoint(
    name: str = Query(min_length=1, max_length=200),
    lat: float = Query(ge=49.0, le=52.0),
    lng: float = Query(ge=2.0, le=7.0),
    wikipedia_url: str | None = Query(default=None, max_length=500),
    wikipedia: str | None = Query(default=None, max_length=200),
    wikidata: str | None = Query(default=None, max_length=32),
    description: str | None = Query(default=None, max_length=1200),
    kind: str | None = Query(default=None, max_length=120),
) -> StopSummaryResponse:
    try:
        data = await wikipedia_service.lookup_stop_summary(
            name,
            lat,
            lng,
            wikipedia_url=wikipedia_url,
            wikipedia=wikipedia,
            wikidata=wikidata,
            description=description,
            kind=kind,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    summary = data.get("summary") or ""
    return StopSummaryResponse(summary=summary, url=data.get("url") or "")


@app.post("/api/surroundings", response_model=SurroundingsResponse)
async def surroundings_endpoint(request: SurroundingsRequest) -> SurroundingsResponse:
    try:
        data = await surroundings_service.live_surroundings(
            request.lat,
            request.lng,
            request.interests,
            explanation_level=request.explanation_level,
            heading=request.heading,
        )
        return SurroundingsResponse(**data)
    except Exception as exc:  # noqa: BLE001
        detail = str(exc)
        if "overpass" in detail.lower():
            detail = "Kaartdata is tijdelijk niet bereikbaar. Probeer het zo opnieuw."
        raise HTTPException(status_code=502, detail=detail) from exc


@app.post("/api/ask", response_model=AskResponse)
async def ask_endpoint(request: AskRequest) -> AskResponse:
    try:
        answer = await answer_about_stop(
            request.name,
            request.kind,
            request.summary,
            request.arrived,
            request.question,
            request.explanation_level,
            request.lat,
            request.lng,
            request.heading,
            request.place_name,
            request.interests,
            request.history,
        )
        return AskResponse(answer=answer)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/api/bike-route", response_model=BikeLegResponse)
async def bike_route_endpoint(request: BikeRouteRequest) -> BikeLegResponse:
    """Magenta-route langs gekozen knooppunten, zonder heen-en-terug over hetzelfde pad."""
    chain = [
        {
            "id": node.id or "",
            "number": node.number,
            "lat": node.lat,
            "lng": node.lng,
            "geoid": node.geoid,
            "network": node.network,
        }
        for node in request.nodes
    ]
    try:
        route_task = asyncio.create_task(
            knoop_service.network_route(chain, close_loop=request.close_loop)
        )
        stub_geometry = [[float(node["lat"]), float(node["lng"])] for node in chain]
        async def _pois() -> dict:
            if not request.interests and not (request.notes or "").strip():
                return {"suggestions": [], "wish_summary": None}
            return await wish_suggestions_along_route(
                request.notes or "",
                stub_geometry,
                chain,
                list(request.interests),
            )

        pois_task = asyncio.create_task(_pois())
        route_result, wish_result = await asyncio.gather(
            route_task, pois_task, return_exceptions=True
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=502,
            detail=str(exc) or "Geen officiële knooppuntenroute tussen deze knooppunten.",
        ) from exc
    if isinstance(route_result, BaseException):
        detail = str(route_result) or "Geen officiële knooppuntenroute tussen deze knooppunten."
        raise HTTPException(status_code=502, detail=detail) from route_result
    route = route_result
    wish = wish_result if isinstance(wish_result, dict) else {"suggestions": [], "wish_summary": None}
    geometry = list(route.get("geometry") or [])
    if len(geometry) < 2:
        raise HTTPException(
            status_code=502,
            detail="Geen officiële knooppuntenroute tussen deze knooppunten.",
        )
    via_raw = route.get("via_knooppunten") or []
    via = [
        Knooppunt(
            id=str(node.get("id") or ""),
            number=str(node.get("number") or ""),
            lat=float(node["lat"]),
            lng=float(node["lng"]),
            network=node.get("network"),
            geoid=node.get("geoid"),
            on_route=True,
        )
        for node in via_raw
        if node.get("number") is not None and node.get("lat") is not None
    ]
    return BikeLegResponse(
        geometry=geometry,
        distance_km=round(float(route["distance_m"]) / 1000, 2),
        duration_min=max(1, round(float(route["duration_s"]) / 60)),
        steps=[],
        via_knooppunten=via,
        suggestions=list(wish.get("suggestions") or []),
        wish_summary=wish.get("wish_summary"),
    )


@app.post("/api/bike-leg", response_model=BikeLegResponse)
async def bike_leg_endpoint(request: BikeLegRequest) -> BikeLegResponse:
    """Magenta-segment: alleen langs het officiële knooppuntennetwerk (WFS-trajecten)."""
    left = {
        "id": request.from_id or "",
        "number": request.from_number or "",
        "lat": request.from_lat,
        "lng": request.from_lng,
        "geoid": request.from_geoid,
        "network": request.from_network,
    }
    right = {
        "id": request.to_id or "",
        "number": request.to_number or "",
        "lat": request.to_lat,
        "lng": request.to_lng,
        "geoid": request.to_geoid,
        "network": request.to_network,
    }
    try:
        route = await knoop_service.network_leg(left, right)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=502,
            detail=str(exc) or "Geen officiële knooppuntenroute tussen deze knooppunten.",
        ) from exc
    geometry = list(route.get("geometry") or [])
    if len(geometry) < 2:
        raise HTTPException(
            status_code=502,
            detail="Geen officiële knooppuntenroute tussen deze knooppunten.",
        )
    via_raw = route.get("via_knooppunten") or []
    via = [
        Knooppunt(
            id=str(node.get("id") or ""),
            number=str(node.get("number") or ""),
            lat=float(node["lat"]),
            lng=float(node["lng"]),
            network=node.get("network"),
            geoid=node.get("geoid"),
            on_route=True,
        )
        for node in via_raw
        if node.get("number") is not None and node.get("lat") is not None
    ]
    return BikeLegResponse(
        geometry=geometry,
        distance_km=round(float(route["distance_m"]) / 1000, 2),
        duration_min=max(1, round(float(route["duration_s"]) / 60)),
        steps=[],
        via_knooppunten=via,
    )


@app.post("/api/reroute", response_model=RerouteResponse)
async def reroute_endpoint(request: RerouteRequest) -> RerouteResponse:
    try:
        return await reroute(request)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc)) from exc
