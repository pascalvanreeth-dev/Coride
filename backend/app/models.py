from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


Interest = Literal[
    "geschiedenis",
    "natuur",
    "landbouw",
    "horeca",
    "oorlog",
    "architectuur",
    "activiteiten",
    "evenementen",
]
RouteMode = Literal["lus", "punt"]
ExplanationLevel = Literal["kort", "normaal", "uitgebreid"]
Fitness = Literal["recreant", "sportief", "wielrenner"]
BikeType = Literal["ebike", "stadsfiets", "racefiets", "gravel"]
HorecaPref = Literal["snack", "tafelen", "koffie", "brouwerijen"]
InteractionMode = Literal["passief", "live"]
BudgetMode = Literal["distance", "time"]
AdaptReason = Literal["regen", "wind", "veer", "korter", "anders"]


class RiderProfile(BaseModel):
    age_band: str = "31-50"
    fitness: Fitness = "recreant"
    bike: BikeType = "stadsfiets"
    horeca: list[HorecaPref] = Field(default_factory=list)
    commentary: ExplanationLevel = "normaal"
    interaction: InteractionMode = "live"


class KnoopPick(BaseModel):
    id: str = ""
    number: str
    lat: float
    lng: float
    network: str | None = None
    geoid: int | str | None = None


class PoiPick(BaseModel):
    id: str
    name: str
    lat: float
    lng: float
    kind: str = "plek"
    kind_label: str | None = None
    interest: Interest = "geschiedenis"


class PoiHit(BaseModel):
    id: str
    name: str
    lat: float
    lng: float
    kind: str
    kind_label: str | None = None
    interest: Interest
    on_route: bool = False
    hint: str | None = None
    # "wish" = Extra wens (zoekveld); "profile" = profielsuggestie
    source: str | None = None


class PlanRequest(BaseModel):
    start: str = Field(min_length=2, max_length=200)
    end: str | None = None
    mode: RouteMode = "lus"
    interests: list[Interest] = Field(default_factory=lambda: ["geschiedenis"])
    distance_km: int = Field(default=25, ge=8, le=90)
    duration_min: int | None = Field(default=None, ge=20, le=480)
    budget_mode: BudgetMode = "distance"
    notes: str = Field(default="", max_length=500)
    explanation_level: ExplanationLevel = "normaal"
    knooppunten: list[KnoopPick] = Field(default_factory=list, max_length=40)
    poi_picks: list[PoiPick] = Field(default_factory=list, max_length=20)
    profile: RiderProfile | None = None
    adapt_reason: AdaptReason | None = None
    suggestion_id: str | None = None
    # Magenta draft van de planner — hergebruik zodat Plan niet opnieuw traag routed.
    route_geometry: list[list[float]] = Field(default_factory=list, max_length=25000)
    route_duration_min: int | None = Field(default=None, ge=1, le=600)


class Place(BaseModel):
    lat: float
    lng: float
    label: str
    country: str | None = None
    place_name: str | None = None
    municipality: str | None = None


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
    wikipedia: str | None = None
    wikidata: str | None = None
    description: str | None = None
    place_name: str | None = None
    population: int | None = None
    local_fact: str | None = None
    side: str | None = None
    matches_wish: bool = False
    on_route: bool = False
    # "wish" = Extra wens (paars); "profile" = profiel (blauw)
    hint: str | None = None
    wish_source: str | None = None


class Knooppunt(BaseModel):
    id: str = ""
    number: str
    lat: float
    lng: float
    network: str | None = None
    on_route: bool = False
    geoid: int | str | None = None


class Step(BaseModel):
    instruction: str
    type: str = ""
    modifier: str = ""
    distance_m: float = 0
    lat: float
    lng: float
    name: str = ""


class WeatherInfo(BaseModel):
    available: bool = False
    summary: str = ""
    alert: str | None = None
    suggest_shorter: bool = False
    temperature_c: float | None = None
    precipitation_mm: float = 0
    wind_kmh: float = 0
    wind_direction: float | None = None
    code: int | None = None


class Locality(BaseModel):
    name: str
    municipality: str | None = None
    population: int | None = None
    fact: str = ""
    lat: float
    lng: float


class RoutePlan(BaseModel):
    title: str
    intro: str
    mode: RouteMode
    interests: list[Interest]
    notes: str = ""
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
    interaction: InteractionMode = "live"
    weather: WeatherInfo | None = None
    budget_mode: BudgetMode = "distance"
    duration_budget_min: int | None = None
    localities: list[Locality] = Field(default_factory=list)
    sources: list[str]
    ai_used: bool


class AskTurn(BaseModel):
    q: str = Field(min_length=1, max_length=400)
    a: str = Field(default="", max_length=2000)


class AskRequest(BaseModel):
    question: str = Field(min_length=2, max_length=400)
    name: str = ""
    kind: str = ""
    summary: str = ""
    arrived: str = ""
    explanation_level: ExplanationLevel = "normaal"
    lat: float | None = None
    lng: float | None = None
    heading: float | None = None
    place_name: str | None = None
    interests: list[Interest] = Field(default_factory=list)
    history: list[AskTurn] = Field(default_factory=list, max_length=8)


class AskResponse(BaseModel):
    answer: str


class SurroundingsHighlight(BaseModel):
    name: str
    kind: str
    interest: Interest
    distance_m: float | None = None


class SurroundingsRequest(BaseModel):
    lat: float = Field(ge=49.0, le=52.0)
    lng: float = Field(ge=2.0, le=7.0)
    interests: list[Interest] = Field(default_factory=list)
    explanation_level: ExplanationLevel = "normaal"
    heading: float | None = None


class SurroundingsResponse(BaseModel):
    summary: str
    place_name: str = ""
    highlights: list[SurroundingsHighlight] = Field(default_factory=list)
    ai_used: bool = False


class StopSummaryResponse(BaseModel):
    summary: str
    url: str = ""


class RerouteRequest(BaseModel):
    start_lat: float
    start_lng: float
    nodes: list[Knooppunt]
    close_loop: bool = False
    end_lat: float | None = None
    end_lng: float | None = None
    reason: AdaptReason | None = None
    remaining_nodes: list[KnoopPick] = Field(default_factory=list)
    poi_picks: list[PoiPick] = Field(default_factory=list, max_length=20)
    target_km: float | None = Field(default=None, ge=3, le=90)
    interests: list[Interest] = Field(default_factory=list)
    profile: RiderProfile | None = None


class RerouteResponse(BaseModel):
    geometry: list[list[float]]
    distance_km: float
    duration_min: int
    knooppunten: list[Knooppunt]
    knoop_chain: str
    steps: list[Step]
    reason: str = ""
    weather: WeatherInfo | None = None


class BikeRouteRequest(BaseModel):
    nodes: list[KnoopPick] = Field(min_length=2, max_length=40)
    close_loop: bool = False
    notes: str = Field(default="", max_length=500)
    interests: list[Interest] = Field(default_factory=list)


class BikeLegRequest(BaseModel):
    from_lat: float = Field(ge=49.0, le=52.0)
    from_lng: float = Field(ge=2.0, le=7.0)
    to_lat: float = Field(ge=49.0, le=52.0)
    to_lng: float = Field(ge=2.0, le=7.0)
    # Identiteit van knooppunten — voor officieel netwerk (geen vrije OSRM).
    from_id: str = ""
    from_number: str = ""
    from_geoid: int | str | None = None
    from_network: str | None = None
    to_id: str = ""
    to_number: str = ""
    to_geoid: int | str | None = None
    to_network: str | None = None


class BikeLegResponse(BaseModel):
    geometry: list[list[float]]
    distance_km: float
    duration_min: int
    steps: list[Step] = Field(default_factory=list)
    via_knooppunten: list[Knooppunt] = Field(default_factory=list)
    suggestions: list[PoiHit] = Field(default_factory=list)
    wish_summary: str | None = None


class RoutePreviewRequest(BaseModel):
    lat: float = Field(ge=49.0, le=52.0)
    lng: float = Field(ge=2.0, le=7.0)
    distance_km: int = Field(default=50, ge=8, le=90)
    mode: RouteMode = "lus"
    end_lat: float | None = Field(default=None, ge=49.0, le=52.0)
    end_lng: float | None = Field(default=None, ge=2.0, le=7.0)
    notes: str = ""
    poi_picks: list[PoiPick] = Field(default_factory=list, max_length=20)
    interests: list[Interest] = Field(default_factory=list)


class RoutePreviewResponse(BaseModel):
    geometry: list[list[float]]
    distance_km: float
    duration_min: int
    knooppunten: list[Knooppunt] = Field(default_factory=list)
    knoop_chain: str = ""
    suggestions: list[PoiHit] = Field(default_factory=list)
    wish_summary: str | None = None


class WishKnoop(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str = ""
    number: str = ""
    lat: float
    lng: float
    network: str | None = None
    on_route: bool = False
    geoid: int | str | None = None

    @field_validator("number", mode="before")
    @classmethod
    def _number_str(cls, value: Any) -> str:
        return "" if value is None else str(value)

    @field_validator("geoid", mode="before")
    @classmethod
    def _geoid_str(cls, value: Any) -> str | None:
        if value is None or value == "":
            return None
        if isinstance(value, (dict, list)):
            return None
        return str(value)


class WishSuggestionsRequest(BaseModel):
    notes: str = Field(default="", max_length=500)
    interests: list[str] = Field(default_factory=list)
    geometry: list[list[float]] = Field(default_factory=list)
    nodes: list[WishKnoop] = Field(default_factory=list)

    @field_validator("interests", mode="before")
    @classmethod
    def _known_interests(cls, value: Any) -> list[str]:
        allowed = {
            "geschiedenis",
            "natuur",
            "landbouw",
            "horeca",
            "oorlog",
            "architectuur",
            "activiteiten",
            "evenementen",
        }
        return [item for item in (value or []) if item in allowed]

    @field_validator("geometry", mode="before")
    @classmethod
    def _clean_geometry(cls, value: Any) -> list[list[float]]:
        out: list[list[float]] = []
        for point in value or []:
            if not isinstance(point, (list, tuple)) or len(point) < 2:
                continue
            try:
                lat = float(point[0])
                lng = float(point[1])
            except (TypeError, ValueError):
                continue
            if not (-90 <= lat <= 90 and -180 <= lng <= 180):
                continue
            out.append([lat, lng])
            if len(out) >= 80:
                break
        return out


class WishSuggestionsResponse(BaseModel):
    suggestions: list[PoiHit] = Field(default_factory=list)
    wish_summary: str | None = None


class GeocodeHit(BaseModel):
    label: str
    lat: float
    lng: float


class RouteSuggestion(BaseModel):
    rank: int = 0
    id: str
    city: str
    title: str
    highlight: str
    start: str
    lat: float = Field(ge=49.0, le=52.0)
    lng: float = Field(ge=2.0, le=7.0)
    end: str | None = None
    mode: RouteMode = "lus"
    distance_km: int = Field(ge=8, le=90)
    interests: list[Interest] = Field(default_factory=list)
    municipalities: list[str] = Field(default_factory=list)
    match_score: int = 0
    notes: str = ""
    distance_from_you_km: float | None = None
    used_before: bool = False
    swapped_from: str | None = None
