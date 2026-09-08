/** Bouw een Ride-plan uit de magenta draft — zonder te wachten op /api/plan. */

function placeFromNode(node, fallbackLabel = "Startknooppunt") {
  const number = node?.number != null ? String(node.number) : "";
  return {
    lat: Number(node.lat),
    lng: Number(node.lng),
    label: number ? `Knooppunt ${number}` : fallbackLabel,
    country: "BE",
    place_name: number ? `Knooppunt ${number}` : null,
    municipality: null,
  };
}

function sampleGeometry(geometry, maxPoints = 4000) {
  if (!geometry?.length) return [];
  if (geometry.length <= maxPoints) return geometry.map((pt) => [Number(pt[0]), Number(pt[1])]);
  const out = [];
  const step = (geometry.length - 1) / (maxPoints - 1);
  for (let i = 0; i < maxPoints; i += 1) {
    const pt = geometry[Math.min(geometry.length - 1, Math.round(i * step))];
    out.push([Number(pt[0]), Number(pt[1])]);
  }
  return out;
}

export function buildManualPlanFromDraft({
  geometry,
  knooppunten = [],
  distanceKm,
  durationMin,
  mode = "punt",
  notes = "",
  interests = ["geschiedenis"],
  poiPicks = [],
  wishSuggestions = [],
  profile = null,
}) {
  const chain = (knooppunten || []).filter(
    (node) => node && Number.isFinite(node.lat) && Number.isFinite(node.lng) && node.number != null,
  );
  const geom = sampleGeometry(geometry);
  if (chain.length < 1 || geom.length < 2) {
    throw new Error("Nog geen magenta route. Kies knooppunten en wacht tot de lijn verschijnt.");
  }

  const first = chain[0];
  const last = chain[chain.length - 1];
  const start = placeFromNode(first);
  const end = mode === "punt" ? placeFromNode(last, "Eindknooppunt") : start;
  const label = chain.map((node) => String(node.number)).join(" → ");
  const km = Math.max(0.5, Number(distanceKm) || 8);
  const minutes = Math.max(1, Number(durationMin) || Math.round((km / 16) * 60));

  const knoopModels = chain.map((node) => ({
    id: node.id || `${node.number}|${Number(node.lat).toFixed(4)}`,
    number: String(node.number),
    lat: Number(node.lat),
    lng: Number(node.lng),
    network: node.network || null,
    on_route: true,
    geoid: node.geoid ?? null,
  }));

  const pickedIds = new Set((poiPicks || []).map((poi) => String(poi.id)));
  const mergedPois = [];
  const seen = new Set();
  for (const poi of [...(poiPicks || []), ...(wishSuggestions || [])]) {
    if (!poi || !Number.isFinite(Number(poi.lat)) || !Number.isFinite(Number(poi.lng))) continue;
    const id = String(poi.id || `${poi.name}|${poi.lat}|${poi.lng}`);
    if (seen.has(id)) continue;
    seen.add(id);
    mergedPois.push(poi);
  }

  const stops = mergedPois.slice(0, 16).map((poi, index) => {
    const id = String(poi.id || `wish-${index}`);
    const picked = pickedIds.has(id);
    return {
      id,
      name: poi.name || "Plek",
      lat: Number(poi.lat),
      lng: Number(poi.lng),
      kind: poi.kind_label || poi.kind || "plek",
      interest: poi.interest || interests[0] || "geschiedenis",
      source: "OpenStreetMap",
      summary: poi.hint || (picked ? "Gekozen langs je route." : "Suggestie langs je route."),
      approaching: `Je nadert ${poi.name || "deze plek"}.`,
      arrived: `Je bent bij ${poi.name || "deze plek"}.`,
      why: notes?.trim()
        ? `Past bij je wens: ${notes.trim()}`
        : poi.hint === "uit je profiel"
          ? "Past bij je profiel."
          : "Suggestie langs je knooppuntenroute.",
      wikipedia_url: null,
      image_url: null,
      wikipedia: null,
      wikidata: null,
      description: "",
      place_name: null,
      population: null,
      local_fact: null,
      side: null,
      matches_wish: true,
      on_route: Boolean(picked || poi.on_route),
    };
  });

  return {
    title: label ? `Knooppuntenroute ${label}` : "Jouw knooppuntenroute",
    intro: label ? `Je volgt de knooppunten ${label}.` : "Je volgt de knooppunten die je zelf koos.",
    mode,
    interests: interests?.length ? interests : ["geschiedenis"],
    notes: (notes || "").trim(),
    start,
    end,
    distance_km: Math.round(km * 10) / 10,
    duration_min: minutes,
    geometry: geom,
    stops,
    knooppunten: knoopModels,
    all_knooppunten: knoopModels,
    knoop_chain: label,
    route_reason: "Eigen knooppuntenroute",
    steps: [],
    explanation_level: profile?.commentary || "normaal",
    interaction: profile?.interaction || "live",
    weather: {
      available: false,
      summary: "",
      alert: null,
      suggest_shorter: false,
      temperature_c: null,
      precipitation_mm: 0,
      wind_kmh: 0,
      wind_direction: null,
      code: null,
    },
    budget_mode: "distance",
    duration_budget_min: null,
    localities: [],
    sources: ["OpenStreetMap", "OSRM fietsrouting", "Fietsknooppuntennetwerk Vlaanderen"],
    ai_used: false,
  };
}
