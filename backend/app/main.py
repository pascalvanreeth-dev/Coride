from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
import httpx

from app.models import AskRequest, AskResponse, GeocodeHit, Knooppunt, PlanRequest, RerouteRequest, RerouteResponse, RoutePlan, RoutePreviewRequest, RoutePreviewResponse, RouteSuggestion
from app.services.ai import answer_about_stop
from app.services.geocoding import geocode, reverse
from app.services import knooppunten as knoop_service
from app.services.planner import plan_route, preview_route, reroute
from app.services import suggestions as suggestion_service

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
        return await geocode(q)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc)) from exc


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
