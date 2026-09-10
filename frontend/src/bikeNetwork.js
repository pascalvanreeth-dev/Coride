/** Lokaal knooppunten-grafiek (zoals fietsknooppunten.be): kleuren zonder WFS per klik. */

function edgeKey(a, b) {
  return a < b ? `${a}|${b}` : `${b}|${a}`;
}

function reverseCoords(coords) {
  return coords.map((point) => [...point]).reverse();
}

/**
 * Mutable netwerk-cache: edges by geoid-pair, adjacency, optional node meta.
 */
export function createBikeNetworkStore() {
  return {
    edges: new Map(), // key -> { a, b, m, coords }
    adj: new Map(), // geoid -> [{ to, m, key }]
    nodes: new Map(), // geoid -> node
  };
}

export function ingestBikeNetwork(store, payload) {
  if (!store || !payload) return store;
  for (const node of payload.nodes || []) {
    if (node?.geoid == null) continue;
    const geo = Number(node.geoid);
    if (!Number.isFinite(geo)) continue;
    store.nodes.set(geo, { ...store.nodes.get(geo), ...node, geoid: geo });
  }
  for (const edge of payload.edges || []) {
    const a = Number(edge.a);
    const b = Number(edge.b);
    const coords = edge.coords;
    if (!Number.isFinite(a) || !Number.isFinite(b) || !Array.isArray(coords) || coords.length < 2) {
      continue;
    }
    const key = edgeKey(a, b);
    const meters = Number(edge.m) > 0 ? Number(edge.m) : approxLengthM(coords);
    const prev = store.edges.get(key);
    if (prev && (prev.coords?.length || 0) >= coords.length) continue;
    store.edges.set(key, { a, b, m: meters, coords });
    addAdj(store, a, b, meters, key);
    addAdj(store, b, a, meters, key);
  }
  return store;
}

function addAdj(store, from, to, meters, key) {
  const list = store.adj.get(from) || [];
  if (!list.some((item) => item.to === to)) {
    list.push({ to, m: meters, key });
    store.adj.set(from, list);
  }
}

function approxLengthM(coords) {
  let total = 0;
  for (let i = 1; i < coords.length; i += 1) {
    total += haversineM(coords[i - 1], coords[i]);
  }
  return total || 1;
}

function haversineM(a, b) {
  const toRad = Math.PI / 180;
  const dLat = (b[0] - a[0]) * toRad;
  const dLng = (b[1] - a[1]) * toRad;
  const lat1 = a[0] * toRad;
  const lat2 = b[0] * toRad;
  const h =
    Math.sin(dLat / 2) ** 2 +
    Math.cos(lat1) * Math.cos(lat2) * Math.sin(dLng / 2) ** 2;
  return 2 * 6371000 * Math.asin(Math.min(1, Math.sqrt(h)));
}

function shortestPath(store, start, goal) {
  if (start === goal) return [start];
  if (!store.adj.has(start) || !store.adj.has(goal)) return null;
  const dist = new Map([[start, 0]]);
  const prev = new Map();
  const heap = [[0, start]];
  while (heap.length) {
    heap.sort((x, y) => x[0] - y[0]);
    const [cost, node] = heap.shift();
    if (cost > (dist.get(node) ?? Infinity)) continue;
    if (node === goal) break;
    for (const next of store.adj.get(node) || []) {
      const nextCost = cost + next.m;
      if (nextCost < (dist.get(next.to) ?? Infinity)) {
        dist.set(next.to, nextCost);
        prev.set(next.to, node);
        heap.push([nextCost, next.to]);
      }
    }
  }
  if (!prev.has(goal) && start !== goal) return null;
  const path = [];
  let cur = goal;
  while (true) {
    path.push(cur);
    if (cur === start) break;
    cur = prev.get(cur);
    if (cur == null) return null;
  }
  path.reverse();
  return path;
}

function coordsForStep(store, from, to) {
  const key = edgeKey(from, to);
  const edge = store.edges.get(key);
  if (!edge?.coords?.length) return null;
  if (edge.a === from && edge.b === to) return edge.coords.map((p) => [...p]);
  if (edge.b === from && edge.a === to) return reverseCoords(edge.coords);
  // Orient by nearest endpoint.
  const first = edge.coords[0];
  const last = edge.coords[edge.coords.length - 1];
  const fromNode = store.nodes.get(from);
  if (fromNode) {
    const dFirst = haversineM([fromNode.lat, fromNode.lng], first);
    const dLast = haversineM([fromNode.lat, fromNode.lng], last);
    return dFirst <= dLast ? edge.coords.map((p) => [...p]) : reverseCoords(edge.coords);
  }
  return edge.coords.map((p) => [...p]);
}

/**
 * Officiële leg A→B uit lokale cache, of null als pad ontbreekt.
 */
export function legFromLocalNetwork(store, from, to) {
  const start = Number(from?.geoid);
  const goal = Number(to?.geoid);
  if (!Number.isFinite(start) || !Number.isFinite(goal)) return null;
  const path = shortestPath(store, start, goal);
  if (!path || path.length < 2) return null;

  let geometry = [];
  let distanceM = 0;
  for (let i = 0; i < path.length - 1; i += 1) {
    const piece = coordsForStep(store, path[i], path[i + 1]);
    if (!piece || piece.length < 2) return null;
    const key = edgeKey(path[i], path[i + 1]);
    distanceM += store.edges.get(key)?.m || approxLengthM(piece);
    if (!geometry.length) {
      geometry = piece;
    } else {
      const last = geometry[geometry.length - 1];
      const first = piece[0];
      const same =
        last &&
        first &&
        haversineM(last, first) < 35;
      for (let j = same ? 1 : 0; j < piece.length; j += 1) {
        geometry.push(piece[j]);
      }
    }
  }
  if (geometry.length < 2) return null;

  // Snap eindpunten naar geklikte knoops.
  geometry[0] = [Number(from.lat), Number(from.lng)];
  geometry[geometry.length - 1] = [Number(to.lat), Number(to.lng)];

  const via = path.map((geo) => {
    const known = store.nodes.get(geo);
    if (geo === start) {
      return {
        id: from.id || known?.id || "",
        number: String(from.number ?? known?.number ?? ""),
        lat: Number(from.lat),
        lng: Number(from.lng),
        network: from.network || known?.network || null,
        geoid: geo,
        on_route: true,
      };
    }
    if (geo === goal) {
      return {
        id: to.id || known?.id || "",
        number: String(to.number ?? known?.number ?? ""),
        lat: Number(to.lat),
        lng: Number(to.lng),
        network: to.network || known?.network || null,
        geoid: geo,
        on_route: true,
      };
    }
    return {
      id: known?.id || `g${geo}`,
      number: String(known?.number ?? ""),
      lat: Number(known?.lat),
      lng: Number(known?.lng),
      network: known?.network || null,
      geoid: geo,
      on_route: true,
    };
  });

  return {
    geometry,
    distance_km: Math.round((distanceM / 1000) * 100) / 100,
    duration_min: Math.max(1, Math.round(distanceM / 3.9 / 60)),
    via_knooppunten: via,
    official: true,
    local: true,
  };
}
