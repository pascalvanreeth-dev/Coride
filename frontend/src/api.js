export async function geocode(q) {
  const response = await fetch(`/api/geocode?q=${encodeURIComponent(q)}`);
  if (!response.ok) return [];
  return response.json();
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
    body: JSON.stringify(payload),
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(data.detail || "De vraag kon niet beantwoord worden.");
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
