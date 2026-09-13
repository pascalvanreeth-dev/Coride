/** Browser-side wens/profiel-zoektocht via Vite-proxy — Photon eerst (Nominatim rate-limiteert snel). */

import { haversine } from "./geo.js";

/** Max. standaard-corridor (korte tochten). Langere routes: zie wishCorridorM. */
export const WISH_ROUTE_CORRIDOR_M = 2000;

/** Corridor schaalt met route-lengte: ~2,5 km kort → ~5 km op 60 km. */
export function wishCorridorM(routeKmOrGeometry, nodes) {
  let km = Number(routeKmOrGeometry);
  if (!Number.isFinite(km) || km <= 0) {
    const route = routePointsForWish(
      Array.isArray(routeKmOrGeometry) ? routeKmOrGeometry : [],
      nodes,
    );
    km = routeLengthM(route) / 1000;
  }
  if (km < 20) return 2500;
  if (km < 40) return 3800;
  return Math.min(5500, Math.round(2500 + km * 45));
}

const THEME_QUERIES = {
  geschiedenis: ["museum", "monument", "kasteel"],
  natuur: ["park", "bos", "natuurgebied"],
  landbouw: ["hoeve", "boerderij", "wijngaard"],
  horeca: ["café", "taverne", "restaurant"],
  oorlog: ["oorlogsmonument", "fort", "begraafplaats"],
  architectuur: ["kerk", "kasteel", "abdij"],
  activiteiten: ["speeltuin", "uitzicht", "zwembad"],
  evenementen: ["markt", "theater", "cultuurcentrum"],
};

const THEME_PHOTON_TAGS = {
  geschiedenis: ["tourism:museum", "historic:monument", "historic:castle"],
  natuur: ["leisure:park", "natural:wood", "leisure:nature_reserve"],
  landbouw: ["tourism:attraction"],
  horeca: ["amenity:cafe", "amenity:pub", "amenity:restaurant"],
  architectuur: ["building:church", "historic:castle", "historic:monastery"],
  oorlog: ["historic:memorial", "historic:fort", "historic:battlefield"],
  activiteiten: ["leisure:playground", "tourism:viewpoint"],
  evenementen: ["amenity:theatre", "amenity:arts_centre"],
};

const WISH_RESULT_CAP = 28;

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
  return densifyRoutePoints(out, 350, 400);
}

/** Vul lange segmenten bij zodat de corridor de magenta lijn volgt. */
function densifyRoutePoints(points, maxSpacingM = 350, maxPoints = 400) {
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

function routeLengthM(route) {
  let m = 0;
  for (let i = 1; i < (route || []).length; i += 1) {
    m += haversine(route[i - 1], route[i]);
  }
  return m;
}

/** Zoekpunten gelijkmatig langs de route — dichter bij langere tochten. */
function sampleSearchPoints(geometry, nodes) {
  const route = routePointsForWish(geometry, nodes);
  if (!route.length) return [];
  const km = routeLengthM(route) / 1000;
  // ~elke 3 km, begrensd 8–22 punten (60 km → ~20).
  const maxPoints = Math.max(8, Math.min(22, Math.round(km / 3) || 8));
  if (route.length <= maxPoints) return route;
  const out = [];
  for (let i = 0; i < maxPoints; i += 1) {
    const idx = Math.round((i * (route.length - 1)) / (maxPoints - 1));
    out.push(route[idx]);
  }
  return out;
}

async function mapPool(items, concurrency, worker) {
  const results = new Array(items.length);
  let next = 0;
  async function run() {
    while (next < items.length) {
      const i = next;
      next += 1;
      results[i] = await worker(items[i], i);
    }
  }
  const n = Math.max(1, Math.min(concurrency, items.length || 1));
  await Promise.all(Array.from({ length: n }, () => run()));
  return results;
}

export function notesWantHoreca(notes) {
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
  if (/kerk|church|begraaf|kapel|museum|station|abdij|kasteel/.test(blob)) return false;
  if (/(?:^|[^a-zà-ÿ])(?:cafe|café|taverne|pub|bar|herberg|bistro|restaurant|brasserie)(?:[^a-zà-ÿ]|$)/i.test(blob)) {
    return true;
  }
  const src = String(poi?.source || "");
  if (poi?.interest === "horeca" && /nominatim|photon|overpass|openstreetmap|^wish$|^horeca/i.test(src)) return true;
  return false;
}

function poiDedupeKey(poi) {
  if (poi?.id) return String(poi.id);
  return `${String(poi?.name || "").toLowerCase()}|${Number(poi?.lat).toFixed(4)}|${Number(poi?.lng).toFixed(4)}`;
}

function finalize(suggestions, cafeWish, corridorM = WISH_ROUTE_CORRIDOR_M) {
  const seen = new Set();
  const out = [];
  for (const poi of suggestions) {
    if (!poi) continue;
    const key = poiDedupeKey(poi);
    if (seen.has(key)) continue;
    seen.add(key);
    if (cafeWish && looksLikeHoreca(poi)) {
      poi.source = "wish";
      poi.hint = "past bij je wens";
      poi.interest = "horeca";
    }
    out.push(poi);
    if (out.length >= WISH_RESULT_CAP) break;
  }
  out.sort((a, b) => {
    const aw = a.source === "wish" ? 0 : 1;
    const bw = b.source === "wish" ? 0 : 1;
    if (aw !== bw) return aw - bw;
    return (a.route_distance_m ?? 9e9) - (b.route_distance_m ?? 9e9);
  });
  const kmLabel = corridorM >= 1000 ? `${(corridorM / 1000).toFixed(1).replace(/\.0$/, "")} km` : `${corridorM} m`;
  const hasWish = out.some((s) => s.source === "wish");
  const hasProfile = out.some((s) => s.source === "profile");
  let wish_summary = `Plekken binnen ${kmLabel} van je knooppuntenroute.`;
  if (hasWish && hasProfile) {
    wish_summary = `Plekken binnen ${kmLabel} van je route (extra wens én profiel).`;
  } else if (hasWish) {
    wish_summary = `Cafés/tavernes binnen ${kmLabel} van je knooppunten.`;
  } else if (hasProfile) {
    wish_summary = `Profielsuggesties binnen ${kmLabel} van je route.`;
  }
  return { suggestions: out, wish_summary };
}

/** Voeg meerdere bronnen samen, filter op corridor, dedupe. */
export function mergeWishSuggestionSources(lists, { notes = "", geometry = [], nodes = [] } = {}) {
  const cafeWish = notesWantHoreca(notes);
  const corridorM = wishCorridorM(geometry, nodes);
  const flat = [];
  for (const list of lists || []) {
    if (Array.isArray(list)) flat.push(...list);
  }
  const near = filterPoisNearRoute(flat, geometry, nodes, corridorM);
  return finalize(near, cafeWish, corridorM);
}

/**
 * Snelle lokale suggesties via Photon, begrensd tot de route-corridor.
 */
export async function fetchWishSuggestionsLocal(
  { notes = "", interests = [], geometry = [], nodes = [] } = {},
  { timeoutMs = 7000, maxDistanceM = null } = {},
) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  const signal = controller.signal;
  const searchPoints = sampleSearchPoints(geometry, nodes);
  const corridorM = maxDistanceM ?? wishCorridorM(geometry, nodes);
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
  // Café-wens: horeca + tot 2 profielthema's ernaast.
  const themeList = cafeWish
    ? profileThemes.slice(0, 2)
    : profileThemes.length
      ? profileThemes.slice(0, 3)
      : ["geschiedenis"];

  try {
    // Midden eerst: snellere eerste hits, daarna rest van de route.
    const mid = Math.floor(searchPoints.length / 2);
    const orderedPoints = [
      searchPoints[mid],
      ...searchPoints.filter((_, i) => i !== mid),
    ];

    const jobFns = [];
    for (const { lat: lat0, lng: lng0 } of orderedPoints) {
      if (wantCafe) {
        const cafeQueries = [
          { q: "café", tag: "amenity:cafe", limit: 8 },
          { q: "taverne", tag: "amenity:pub", limit: 6 },
          { q: "restaurant", tag: "amenity:restaurant", limit: 5 },
        ];
        for (const item of cafeQueries) {
          jobFns.push(() =>
            photonSearch(item.q, lat0, lng0, signal, { limit: item.limit, osmTag: item.tag })
              .then((features) =>
                features.map((f) => fromPhoton(f, "horeca", cafeSource, cafeHint)).filter(Boolean),
              )
              .catch(() => []),
          );
        }
      }
      for (const interest of themeList) {
        const queries = THEME_QUERIES[interest] || ["museum"];
        const tags = THEME_PHOTON_TAGS[interest] || [];
        const pairCount = Math.min(2, queries.length);
        for (let i = 0; i < pairCount; i += 1) {
          const query = queries[i];
          const tag = tags[i] || tags[0] || null;
          jobFns.push(() =>
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
    }

    // Beperkte concurrency: 20+ punten × queries mag niet in één Promise.all aborten.
    const groups = await mapPool(jobFns, 8, (fn) => fn());
    const near = filterPoisNearRoute(groups.flat(), geometry, nodes, corridorM);
    return finalize(near, cafeWish, corridorM);
  } finally {
    clearTimeout(timer);
  }
}
