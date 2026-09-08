export async function geocode(q) {
  const response = await apiFetch(`/api/geocode?q=${encodeURIComponent(q)}`);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = typeof data.detail === "string" ? data.detail : "Zoeken mislukt.";
    throw new Error(detail);
  }
  return Array.isArray(data) ? data : [];
}

export async function reverseGeocode(lat, lng) {
  const response = await apiFetch(`/api/reverse?lat=${encodeURIComponent(lat)}&lng=${encodeURIComponent(lng)}`);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(data.detail || "Dit GPS-punt kon niet omgezet worden naar een adres.");
  }
  return data;
}

export async function planRoute(payload) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 90_000);
  try {
    const response = await apiFetch("/api/plan", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
      signal: controller.signal,
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw new Error(formatApiError(data.detail, "De route kon niet worden gepland."));
    }
    return data;
  } catch (err) {
    if (isAbortError(err)) {
      throw new Error("Het plannen duurde te lang. Probeer minder kilometers of kies zelf knooppunten.");
    }
    throw err;
  } finally {
    clearTimeout(timer);
  }
}

function formatApiError(detail, fallback) {
  if (!detail) return fallback;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return detail.map((item) => item.msg || item.message || String(item)).join(" ");
  }
  return fallback;
}

export function isAbortError(err) {
  if (!err) return false;
  if (err.name === "AbortError" || err.code === 20) return true;
  return /signal is aborted|aborted without reason|The operation was aborted|Verzoek afgebroken/i.test(
    String(err.message || ""),
  );
}

function networkErrorMessage(err, fallback) {
  if (isAbortError(err)) {
    return err.message || "Verzoek afgebroken.";
  }
  const msg = String(err?.message || "");
  if (/failed to fetch|networkerror|load failed|fetch failed/i.test(msg)) {
    return "Geen verbinding met de server. Controleer of de backend draait en probeer opnieuw.";
  }
  return msg || fallback;
}

async function apiFetch(url, options) {
  try {
    return await fetch(url, options);
  } catch (err) {
    if (isAbortError(err)) {
      const abortErr = new Error(err.message || "Aborted");
      abortErr.name = "AbortError";
      abortErr.cause = err;
      throw abortErr;
    }
    throw new Error(networkErrorMessage(err, "Netwerkfout."));
  }
}

export async function askAbout(payload) {
  const response = await apiFetch("/api/ask", {
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
  const response = await apiFetch("/api/surroundings", {
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
  const response = await apiFetch("/api/route-preview", {
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

export async function fetchWishSuggestions(payload) {
  const response = await apiFetch("/api/wish-suggestions", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(data.detail || "Wenssuggesties konden niet geladen worden.");
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
  const response = await apiFetch(`/api/route-suggestions?${params}`);
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
  const response = await apiFetch(`/api/poi-suggestions?${params}`);
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
  const response = await apiFetch(`/api/knooppunten?${params}`);
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
  const response = await apiFetch(`/api/stop-summary?${params}`);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(data.detail || "Beschrijving kon niet geladen worden.");
  }
  return data;
}

/** Snelle magenta-leg rechtstreeks via OSRM (Vite-proxy /osrm-bike). */
export async function fetchOsrmBikeLeg(from, to) {
  const coords = `${Number(from.lng).toFixed(6)},${Number(from.lat).toFixed(6)};${Number(to.lng).toFixed(6)},${Number(to.lat).toFixed(6)}`;
  const url = `/osrm-bike/route/v1/driving/${coords}?overview=full&geometries=geojson&steps=false&alternatives=false`;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 12_000);
  try {
    const response = await fetch(url, { signal: controller.signal });
    const data = await response.json().catch(() => ({}));
    if (!response.ok || data?.code !== "Ok" || !data?.routes?.[0]) {
      throw new Error("Geen fietsroute gevonden tussen deze knooppunten.");
    }
    const route = data.routes[0];
    const geometry = (route.geometry?.coordinates || []).map(([lng, lat]) => [lat, lng]);
    if (geometry.length < 2) {
      throw new Error("Geen fietsroute gevonden tussen deze knooppunten.");
    }
    return {
      geometry,
      distance_km: Number((Number(route.distance || 0) / 1000).toFixed(2)),
      duration_min: Math.max(1, Math.round(Number(route.duration || 0) / 60)),
      steps: [],
      via_knooppunten: [from, to],
    };
  } finally {
    clearTimeout(timer);
  }
}

/** Magenta-leg: eerst OSRM (snel), anders officieel knooppuntennetwerk. */
export async function fetchBikeLeg(from, to) {
  try {
    return await fetchOsrmBikeLeg(from, to);
  } catch {
    /* fall through to network */
  }
  const response = await apiFetch("/api/bike-leg", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      from_lat: from.lat,
      from_lng: from.lng,
      to_lat: to.lat,
      to_lng: to.lng,
      from_id: from.id || "",
      from_number: String(from.number ?? ""),
      from_geoid: from.geoid ?? null,
      from_network: from.network || null,
      to_id: to.id || "",
      to_number: String(to.number ?? ""),
      to_geoid: to.geoid ?? null,
      to_network: to.network || null,
    }),
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(
      typeof data.detail === "string"
        ? data.detail
        : "Geen officiële knooppuntenroute tussen deze knooppunten.",
    );
  }
  return data;
}

export async function reroute(payload) {
  const response = await apiFetch("/api/reroute", {
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
