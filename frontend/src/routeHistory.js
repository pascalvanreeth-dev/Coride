export const STORAGE_KEY = "veloverhaal-used-routes";

export function getUsedRouteIds() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed.map((item) => String(item));
  } catch {
    return [];
  }
}

export function recordRouteUse(routeId) {
  if (!routeId) return;
  const id = String(routeId);
  const current = getUsedRouteIds().filter((item) => item !== id);
  current.unshift(id);
  localStorage.setItem(STORAGE_KEY, JSON.stringify(current.slice(0, 24)));
}
