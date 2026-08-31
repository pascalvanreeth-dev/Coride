export async function geocode(q) {
  const response = await fetch(`/api/geocode?q=${encodeURIComponent(q)}`);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = typeof data.detail === "string" ? data.detail : "Zoeken mislukt.";
    throw new Error(detail);
  }
  return Array.isArray(data) ? data : [];
}

export async function reverseGeocode(lat, lng) {
  const response = await fetch(`/api/reverse?lat=${encodeURIComponent(lat)}&lng=${encodeURIComponent(lng)}`);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(data.detail || "Dit GPS-punt kon niet omgezet worden naar een adres.");
  }
  return data;
}

export async function planRoute(payload) {
  const response = await fetch("/api/plan", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(formatApiError(data.detail, "De route kon niet worden gepland."));
  }
  return data;
}

function formatApiError(detail, fallback) {
  if (!detail) return fallback;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return detail.map((item) => item.msg || item.message || String(item)).join(" ");
  }
  return fallback;
}

export async function askAbout(payload) {
  const response = await fetch("/api/ask", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    cache: "no-store",
    body: JSON.stringify(payload),
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(data.detail || "De vraag kon niet beantwoord worden.");
  }
  return data;
}

export async function fetchSurroundings(payload) {
  const response = await fetch("/api/surroundings", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(data.detail || "Omgevingsinfo kon niet geladen worden.");
  }
  return data;
}

export async function fetchRoutePreview(payload) {
  const response = await fetch("/api/route-preview", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(data.detail || "Routevoorbeeld kon niet geladen worden.");
  }
  return data;
}

export async function fetchRouteSuggestions(lat, lng, interests = [], used = []) {
  const params = new URLSearchParams({
    lat: String(lat),
    lng: String(lng),
  });
  for (const interest of interests) params.append("interests", interest);
  for (const id of used) params.append("used", id);
  const response = await fetch(`/api/route-suggestions?${params}`);
  const data = await response.json().catch(() => []);
  if (!response.ok) {
    throw new Error(data.detail || "Route Top 10 kon niet geladen worden.");
  }
  return data;
}

export async function fetchPoiSuggestions(lat, lng, interests = [], { radius = 7000, samples = [] } = {}) {
  const params = new URLSearchParams({
    lat: String(lat),
    lng: String(lng),
    radius: String(radius),
  });
  for (const interest of interests) params.append("interests", interest);
  for (const point of samples) {
    params.append("sample_lat", String(point.lat));
    params.append("sample_lng", String(point.lng));
  }
  const response = await fetch(`/api/poi-suggestions?${params}`);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    let message = typeof data.detail === "string" ? data.detail : "Suggesties konden niet geladen worden.";
    if (/overpass/i.test(message)) {
      message = "Kaartdata (OpenStreetMap) is tijdelijk niet bereikbaar. Probeer het over een minuut opnieuw.";
    }
    throw new Error(message);
  }
  return Array.isArray(data) ? data : [];
}

export async function fetchKnooppunten(lat, lng, radius = 12000) {
  const params = new URLSearchParams({
    lat: String(lat),
    lng: String(lng),
    radius: String(radius),
  });
  const response = await fetch(`/api/knooppunten?${params}`);
  const data = await response.json().catch(() => []);
  if (!response.ok) {
    throw new Error(data.detail || "Knooppunten konden niet geladen worden.");
  }
  return data;
}

export async function fetchStopSummary({ name, lat, lng, wikipedia_url = null, wikipedia = null, wikidata = null, description = null, kind = null }) {
  const params = new URLSearchParams({
    name,
    lat: String(lat),
    lng: String(lng),
  });
  if (wikipedia_url) params.set("wikipedia_url", wikipedia_url);
  if (wikipedia) params.set("wikipedia", wikipedia);
  if (wikidata) params.set("wikidata", wikidata);
  if (description) params.set("description", description);
  if (kind) params.set("kind", kind);
  const response = await fetch(`/api/stop-summary?${params}`);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(data.detail || "Beschrijving kon niet geladen worden.");
  }
  return data;
}

export async function reroute(payload) {
  const response = await fetch("/api/reroute", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(data.detail || "De route kon niet herberekend worden.");
  }
  return data;
}
