/** Browser-side wens/profiel-zoektocht via Vite-proxy — Photon eerst (Nominatim rate-limiteert snel). */

import { haversine } from "./geo.js";

/** Max. afstand tot de magenta-/knooppuntenroute voor zichtbare suggesties. */
export const WISH_ROUTE_CORRIDOR_M = 2000;

const THEME_QUERIES = {
  geschiedenis: ["museum", "monument"],
  natuur: ["park", "bos"],
  landbouw: ["hoeve", "boerderij"],
  horeca: ["café", "taverne"],
  oorlog: ["oorlogsmonument", "fort"],
  architectuur: ["kerk", "kasteel"],
  activiteiten: ["speeltuin", "uitzicht"],
  evenementen: ["markt", "theater"],
};

const THEME_PHOTON_TAGS = {
  geschiedenis: ["tourism:museum", "historic:monument"],
  natuur: ["leisure:park"],
  horeca: ["amenity:cafe", "amenity:pub"],
  architectuur: ["building:church", "historic:castle"],
  oorlog: ["historic:memorial"],
};

function toLatLng(point) {
  if (!point) return null;
  if (Array.isArray(point)) {
    const lat = Number(point[0]);
    const lng = Number(point[1]);
    if (!Number.isFinite(lat) || !Number.isFinite(lng)) return null;
    return { lat, lng };
  }
  const lat = Number(point.lat);
  const lng = Number(point.lng);
  if (!Number.isFinite(lat) || !Number.isFinite(lng)) return null;
  return { lat, lng };
}

/** Routepunten voor corridor: geometrie, anders knooppunten. */
export function routePointsForWish(geometry, nodes) {
  const out = [];
  const seen = new Set();
  const push = (raw) => {
    const pt = toLatLng(raw);
    if (!pt) return;
    const key = `${pt.lat.toFixed(4)},${pt.lng.toFixed(4)}`;
    if (seen.has(key)) return;
    seen.add(key);
    out.push(pt);
  };
  for (const point of geometry || []) push(point);
  if (out.length < 2) {
    for (const node of nodes || []) push(node);
  }
  return densifyRoutePoints(out, 450, 160);
}

/** Vul lange segmenten bij zodat de 2 km-corridor de magenta lijn volgt. */
function densifyRoutePoints(points, maxSpacingM = 450, maxPoints = 160) {
  if (!points?.length) return [];
  if (points.length === 1) return points;
  const out = [points[0]];
  for (let i = 1; i < points.length; i += 1) {
    const a = points[i - 1];
    const b = points[i];
    const d = haversine(a, b);
    const steps = Math.max(1, Math.ceil(d / maxSpacingM));
    for (let s = 1; s <= steps; s += 1) {
      const t = s / steps;
      out.push({
        lat: a.lat + t * (b.lat - a.lat),
        lng: a.lng + t * (b.lng - a.lng),
      });
      if (out.length >= maxPoints) return out;
    }
  }
  return out;
}

/** Kortste afstand (m) van een plek tot de route-polyline. */
export function distanceToRouteM(lat, lng, routePoints) {
  const poi = { lat: Number(lat), lng: Number(lng) };
  if (!Number.isFinite(poi.lat) || !Number.isFinite(poi.lng) || !routePoints?.length) {
    return Infinity;
  }
  let best = Infinity;
  for (let i = 0; i < routePoints.length; i += 1) {
    const a = routePoints[i];
    best = Math.min(best, haversine(poi, a));
    if (i === 0) continue;
    const b = routePoints[i - 1];
    const ax = (a.lng - b.lng) * 111320 * Math.cos((b.lat * Math.PI) / 180);
    const ay = (a.lat - b.lat) * 110540;
    const px = (poi.lng - b.lng) * 111320 * Math.cos((b.lat * Math.PI) / 180);
    const py = (poi.lat - b.lat) * 110540;
    const len2 = ax * ax + ay * ay;
    if (len2 < 1) continue;
    const t = Math.max(0, Math.min(1, (px * ax + py * ay) / len2));
    const sx = b.lng + t * (a.lng - b.lng);
    const sy = b.lat + t * (a.lat - b.lat);
    best = Math.min(best, haversine(poi, { lat: sy, lng: sx }));
  }
  return best;
}

/** Houd alleen plekken binnen de corridor van de route. */
export function filterPoisNearRoute(pois, geometry, nodes, maxM = WISH_ROUTE_CORRIDOR_M) {
  const routePoints = routePointsForWish(geometry, nodes);
  if (routePoints.length < 1) return [];
  return (pois || [])
    .map((poi) => {
      if (!poi || !Number.isFinite(Number(poi.lat)) || !Number.isFinite(Number(poi.lng))) return null;
      const dist = distanceToRouteM(poi.lat, poi.lng, routePoints);
      if (dist > maxM) return null;
      return { ...poi, on_route: dist <= 350, route_distance_m: Math.round(dist) };
    })
    .filter(Boolean);
}

/** Zoekpunten gelijkmatig langs de route. */
function sampleSearchPoints(geometry, nodes, maxPoints = 4) {
  const route = routePointsForWish(geometry, nodes);
  if (!route.length) return [];
  if (route.length <= maxPoints) return route;
  const out = [];
  for (let i = 0; i < maxPoints; i += 1) {
    const idx = Math.round((i * (route.length - 1)) / (maxPoints - 1));
    out.push(route[idx]);
  }
  return out;
}

function notesWantHoreca(notes) {
  return /caf[eéè]|taverne|tavern|herberg|koffie|pub|\bbar\b|brasserie|estaminet|bistro/i.test(
    String(notes || ""),
  );
}

/**
 * Photon-zoekte. Geen bbox: die parameter geeft bij Photon vaak 0 hits.
 * Corridorfilter gebeurt daarna hard in filterPoisNearRoute.
 */
async function photonSearch(query, lat, lng, signal, { limit = 8, osmTag = null } = {}) {
  const params = new URLSearchParams({
    q: query,
    lat: String(lat),
    lon: String(lng),
    limit: String(limit),
  });
  if (osmTag) params.set("osm_tag", osmTag);
  const response = await fetch(`/photon-api/api/?${params}`, { signal });
  if (!response.ok) return [];
  const payload = await response.json().catch(() => ({}));
  return Array.isArray(payload?.features) ? payload.features : [];
}

function fromPhoton(feature, interest, source, hint) {
  const coords = feature?.geometry?.coordinates;
  const props = feature?.properties || {};
  if (!Array.isArray(coords) || coords.length < 2) return null;
  const lng = Number(coords[0]);
  const lat = Number(coords[1]);
  const name = String(props.name || "").trim();
  if (!name || !Number.isFinite(lat) || !Number.isFinite(lng)) return null;
  const osmId = props.osm_id || `${name}|${lat}|${lng}`;
  return {
    id: `local-photon-${osmId}`,
    name,
    lat,
    lng,
    kind: props.osm_value || props.type || "plek",
    kind_label: props.osm_value || props.type || "plek",
    interest,
    on_route: false,
    hint,
    source,
  };
}

function looksLikeHoreca(poi) {
  const blob = `${poi?.kind || ""} ${poi?.kind_label || ""} ${poi?.name || ""}`.toLowerCase();
  if (/kerk|church|begraaf|kapel|museum|station/.test(blob)) return false;
  if (/cafe|café|taverne|pub|bar|herberg|bistro|restaurant/.test(blob)) return true;
  return poi?.interest === "horeca";
}

function finalize(suggestions, cafeWish) {
  const seen = new Set();
  const out = [];
  for (const poi of suggestions) {
    if (!poi || seen.has(poi.id)) continue;
    seen.add(poi.id);
    if (cafeWish && looksLikeHoreca(poi)) {
      poi.source = "wish";
      poi.hint = "past bij je wens";
      poi.interest = "horeca";
    }
    out.push(poi);
    if (out.length >= 16) break;
  }
  out.sort((a, b) => {
    const aw = a.source === "wish" ? 0 : 1;
    const bw = b.source === "wish" ? 0 : 1;
    if (aw !== bw) return aw - bw;
    return (a.route_distance_m ?? 0) - (b.route_distance_m ?? 0);
  });
  const hasWish = out.some((s) => s.source === "wish");
  const hasProfile = out.some((s) => s.source === "profile");
  let wish_summary = "Plekken binnen 2 km van je knooppuntenroute.";
  if (hasWish && hasProfile) {
    wish_summary = "Plekken binnen 2 km van je route (extra wens én profiel).";
  } else if (hasWish) {
    wish_summary = "Cafés/tavernes binnen 2 km van je knooppunten.";
  } else if (hasProfile) {
    wish_summary = "Profielsuggesties binnen 2 km van je route.";
  }
  return { suggestions: out, wish_summary };
}

/**
 * Snelle lokale suggesties via Photon, begrensd tot ~2 km van de route.
 */
export async function fetchWishSuggestionsLocal(
  { notes = "", interests = [], geometry = [], nodes = [] } = {},
  { timeoutMs = 5000, maxDistanceM = WISH_ROUTE_CORRIDOR_M } = {},
) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  const signal = controller.signal;
  const searchPoints = sampleSearchPoints(geometry, nodes, 3);
  if (!searchPoints.length) {
    clearTimeout(timer);
    return { suggestions: [], wish_summary: null };
  }

  const cafeWish = notesWantHoreca(notes);
  const themes = (interests || []).filter((item) => THEME_QUERIES[item]);
  const wantCafe = cafeWish || themes.includes("horeca");
  const profileThemes = themes.filter((item) => item !== "horeca");
  const cafeSource = cafeWish ? "wish" : "profile";
  const cafeHint = cafeWish ? "past bij je wens" : "uit je profiel";
  // Bij café-wens: eerst horeca; profiel ernaast (max 1 thema) zodat wensen niet verdwijnen.
  const themeList = cafeWish
    ? profileThemes.slice(0, 1)
    : profileThemes.length
      ? profileThemes.slice(0, 2)
      : ["geschiedenis"];

  const tasks = [];
  try {
    // Eerste zoekpunt meteen: snellere blauwe tegels naast magenta.
    const orderedPoints = searchPoints.length
      ? [searchPoints[Math.floor(searchPoints.length / 2)], ...searchPoints.filter((_, i) => i !== Math.floor(searchPoints.length / 2))]
      : [];

    for (const { lat: lat0, lng: lng0 } of orderedPoints) {
      if (wantCafe) {
        tasks.push(
          photonSearch("café", lat0, lng0, signal, { limit: 8, osmTag: "amenity:cafe" })
            .then((features) =>
              features.map((f) => fromPhoton(f, "horeca", cafeSource, cafeHint)).filter(Boolean),
            )
            .catch(() => []),
        );
        tasks.push(
          photonSearch("taverne", lat0, lng0, signal, { limit: 6 })
            .then((features) =>
              features.map((f) => fromPhoton(f, "horeca", cafeSource, cafeHint)).filter(Boolean),
            )
            .catch(() => []),
        );
      }
      for (const interest of themeList) {
        const query = THEME_QUERIES[interest]?.[0] || "museum";
        const tag = THEME_PHOTON_TAGS[interest]?.[0] || null;
        tasks.push(
          photonSearch(query, lat0, lng0, signal, { limit: 6, osmTag: tag })
            .then((features) =>
              features
                .map((f) => fromPhoton(f, interest, "profile", "uit je profiel"))
                .filter(Boolean),
            )
            .catch(() => []),
        );
      }
    }

    const groups = await Promise.all(tasks);
    const near = filterPoisNearRoute(groups.flat(), geometry, nodes, maxDistanceM);
    return finalize(near, cafeWish);
  } finally {
    clearTimeout(timer);
  }
}
