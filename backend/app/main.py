from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
import httpx

from app.models import AskRequest, AskResponse, GeocodeHit, Knooppunt, PlanRequest, PoiHit, RerouteRequest, RerouteResponse, RoutePlan, RoutePreviewRequest, RoutePreviewResponse, RouteSuggestion, StopSummaryResponse, SurroundingsRequest, SurroundingsResponse
from app.services.ai import answer_about_stop
from app.services.geocoding import geocode, reverse
from app.services import knooppunten as knoop_service
from app.services.planner import plan_route, preview_route, reroute
from app.services import pois as pois_service
from app.services import suggestions as suggestion_service
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
        hits = await geocode(q)
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
        )
        for node in nodes
    ]


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
    points: list[tuple[float, float]] = [(lat, lng)]
    for slat, slng in zip(sample_lat, sample_lng, strict=False):
        if 49.0 <= slat <= 52.0 and 2.0 <= slng <= 7.0:
            points.append((slat, slng))
    try:
        if len(points) > 1:
            pois = await pois_service.fetch_pois_along_points(points, radius, wanted)
        else:
            pois = await pois_service.fetch_pois(lat, lng, radius, wanted)
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
        items = suggestion_service.suggest_routes(lat, lng, interests, used)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return [RouteSuggestion(**item) for item in items]


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


@app.post("/api/plan", response_model=RoutePlan)
async def plan_endpoint(request: PlanRequest) -> RoutePlan:
    if not request.interests:
        request.interests = ["geschiedenis"]
    try:
        return await plan_route(request)
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


@app.post("/api/reroute", response_model=RerouteResponse)
async def reroute_endpoint(request: RerouteRequest) -> RerouteResponse:
    try:
        return await reroute(request)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc)) from exc
