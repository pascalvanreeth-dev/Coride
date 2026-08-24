from typing import Literal

from pydantic import BaseModel, Field


Interest = Literal["geschiedenis", "activiteiten", "evenementen"]
RouteMode = Literal["lus", "punt"]
ExplanationLevel = Literal["kort", "normaal", "uitgebreid"]


class PlanRequest(BaseModel):
    start: str = Field(min_length=2, max_length=200)
    end: str | None = None
    mode: RouteMode = "lus"
    interests: list[Interest] = Field(default_factory=lambda: ["geschiedenis"])
    distance_km: int = Field(default=25, ge=8, le=90)
    notes: str = Field(default="", max_length=500)
    explanation_level: ExplanationLevel = "normaal"


class Place(BaseModel):
    lat: float
    lng: float
    label: str
    country: str | None = None


class Stop(BaseModel):
    id: str
    name: str
    lat: float
    lng: float
    kind: str
    interest: Interest
    source: str
    summary: str
    approaching: str
    arrived: str
    why: str
    wikipedia_url: str | None = None
    image_url: str | None = None


class Knooppunt(BaseModel):
    id: str = ""
    number: str
    lat: float
    lng: float
    network: str | None = None
    on_route: bool = False


class Step(BaseModel):
    instruction: str
    type: str = ""
    modifier: str = ""
    distance_m: float = 0
    lat: float
    lng: float
    name: str = ""


class RoutePlan(BaseModel):
    title: str
    intro: str
    mode: RouteMode
    interests: list[Interest]
    start: Place
    end: Place
    distance_km: float
    duration_min: int
    geometry: list[list[float]]
    stops: list[Stop]
    knooppunten: list[Knooppunt] = Field(default_factory=list)
    all_knooppunten: list[Knooppunt] = Field(default_factory=list)
    knoop_chain: str = ""
    route_reason: str = ""
    steps: list[Step] = Field(default_factory=list)
    explanation_level: ExplanationLevel = "normaal"
    sources: list[str]
    ai_used: bool


class AskRequest(BaseModel):
    question: str = Field(min_length=2, max_length=400)
    name: str
    kind: str = ""
    summary: str = ""
    arrived: str = ""
    explanation_level: ExplanationLevel = "normaal"


class AskResponse(BaseModel):
    answer: str


class RerouteRequest(BaseModel):
    start_lat: float
    start_lng: float
    nodes: list[Knooppunt]
    close_loop: bool = True


class RerouteResponse(BaseModel):
    geometry: list[list[float]]
    distance_km: float
    duration_min: int
    knooppunten: list[Knooppunt]
    knoop_chain: str
    steps: list[Step]


class GeocodeHit(BaseModel):
    label: str
    lat: float
    lng: float
