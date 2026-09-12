import { bearingDeg, haversine } from "./geo.js";

function angleDiff(a, b) {
  return Math.abs(((a - b + 540) % 360) - 180);
}

/**
 * Officiële knooppuntenroutes zijn langer dan vogelvlucht knoop→knoop.
 * Doel-km delen door deze factor ≈ werkelijke fietskilometers.
 */
const NETWORK_DETOUR = 1.38;

/**
 * Bouw een knooppuntenketen rond start (en optioneel eind), zoals de backend
 * plan_node_chain — maar client-side op de knopen die al op de kaart staan.
 */
export function planAutoNodeChain(nodes, origin, targetKm, endPoint = null) {
  const pool = (nodes || []).filter(
    (n) => Number.isFinite(Number(n?.lat)) && Number.isFinite(Number(n?.lng)),
  );
  if (!origin || pool.length < 2) return [];

  // Budget in vogelvlucht tussen knopen — magenta volgt langere netwerkpaden.
  const targetM = (Math.max(4, Number(targetKm) || 25) * 1000) / NETWORK_DETOUR;
  const start = nearestNode(pool, origin);
  if (!start) return [];

  const finish =
    endPoint && Number.isFinite(Number(endPoint.lat)) && Number.isFinite(Number(endPoint.lng))
      ? nearestNode(pool, endPoint)
      : null;

  if (finish && nodeKey(finish) !== nodeKey(start)) {
    return growPointToPoint(pool, start, finish, targetM);
  }
  return growLoop(pool, start, targetM);
}

/**
 * Top 10: lus via catalogus-waypoints (Damme, Beernem, …) i.p.v. willekeurige lus rond GPS.
 */
export function planSuggestNodeChain(nodes, origin, targetKm, waypoints = []) {
  const pool = (nodes || []).filter(
    (n) => Number.isFinite(Number(n?.lat)) && Number.isFinite(Number(n?.lng)),
  );
  if (!origin || pool.length < 2) return [];

  const start = nearestNode(pool, origin);
  if (!start) return [];

  const targets = [];
  const seen = new Set([nodeKey(start)]);
  for (const wp of waypoints || []) {
    if (!Number.isFinite(Number(wp?.lat)) || !Number.isFinite(Number(wp?.lng))) continue;
    // Waypoint moet in de buurt van de route-start liggen (zelfde regio).
    if (haversine(origin, wp) > 45_000) continue;
    const node = nearestNode(pool, wp);
    if (!node || haversine(wp, node) > 8_000) continue;
    const key = nodeKey(node);
    if (seen.has(key)) continue;
    seen.add(key);
    targets.push(node);
  }

  if (targets.length < 1) {
    return planAutoNodeChain(pool, origin, targetKm, null);
  }

  const targetM = (Math.max(4, Number(targetKm) || 25) * 1000) / NETWORK_DETOUR;
  const legBudget = targetM / (targets.length + 1);
  const chain = [start];
  let cur = start;

  for (const next of targets) {
    if (nodeKey(cur) === nodeKey(next)) continue;
    const leg = growPointToPoint(pool, cur, next, Math.max(legBudget, haversine(cur, next) * 1.15));
    for (let i = 1; i < leg.length; i += 1) {
      const n = leg[i];
      if (nodeKey(chain[chain.length - 1]) === nodeKey(n)) continue;
      chain.push(n);
    }
    cur = chain[chain.length - 1];
  }

  // Terug naar start voor de lus.
  if (nodeKey(cur) !== nodeKey(start)) {
    const home = growPointToPoint(pool, cur, start, Math.max(legBudget, haversine(cur, start) * 1.15));
    for (let i = 1; i < home.length; i += 1) {
      const n = home[i];
      if (nodeKey(chain[chain.length - 1]) === nodeKey(n)) continue;
      chain.push(n);
    }
  }

  return chain.length >= 2 ? chain : planAutoNodeChain(pool, origin, targetKm, null);
}

function nodeKey(n) {
  return `${Number(n.lat).toFixed(5)},${Number(n.lng).toFixed(5)}`;
}

function nearestNode(pool, point) {
  let best = null;
  let bestD = Infinity;
  for (const n of pool) {
    const d = haversine(point, n);
    if (d < bestD) {
      bestD = d;
      best = n;
    }
  }
  return best;
}

function neighborsWithin(pool, node, maxM = 4500) {
  const out = [];
  for (const other of pool) {
    if (nodeKey(other) === nodeKey(node)) continue;
    const d = haversine(node, other);
    if (d > 80 && d <= maxM) out.push({ node: other, d });
  }
  out.sort((a, b) => a.d - b.d);
  return out.slice(0, 14);
}

function growLoop(pool, start, targetM) {
  const chain = [start];
  const used = new Set([nodeKey(start)]);
  let dist = 0;
  let heading = null;

  while (dist < targetM * 0.92 && chain.length < 48) {
    const cur = chain[chain.length - 1];
    const backHome = haversine(cur, start);
    const remaining = targetM - dist;

    if (chain.length >= 4 && backHome < remaining * 0.55 && dist > targetM * 0.55) {
      break;
    }

    const cands = neighborsWithin(pool, cur).filter((c) => !used.has(nodeKey(c.node)));
    if (!cands.length) break;

    let best = null;
    let bestScore = Infinity;
    for (const { node, d } of cands) {
      const bear = bearingDeg(cur, node);
      const turn = heading == null ? 0 : angleDiff(heading, bear);
      const homeAfter = haversine(node, start);
      const progress = dist + d;
      let score = d * 0.35 + turn * 12 + Math.abs(progress + homeAfter - targetM) * 0.45;
      if (progress > targetM * 0.7) score += homeAfter * 0.25;
      if (score < bestScore) {
        bestScore = score;
        best = { node, d, bear };
      }
    }
    if (!best) break;
    chain.push(best.node);
    used.add(nodeKey(best.node));
    dist += best.d;
    heading = best.bear;
  }

  if (chain.length >= 2) return chain;
  return [start];
}

function growPointToPoint(pool, start, finish, targetM) {
  const direct = haversine(start, finish);
  if (direct < 200) return [start, finish];

  const chain = [start];
  const used = new Set([nodeKey(start)]);
  let dist = 0;
  let heading = bearingDeg(start, finish);

  while (chain.length < 40) {
    const cur = chain[chain.length - 1];
    const toFinish = haversine(cur, finish);
    if (toFinish < 3200 || (dist > targetM * 0.75 && toFinish < 5000)) {
      if (nodeKey(cur) !== nodeKey(finish)) chain.push(finish);
      break;
    }

    const cands = neighborsWithin(pool, cur, 5000).filter((c) => !used.has(nodeKey(c.node)));
    if (!cands.length) {
      if (nodeKey(cur) !== nodeKey(finish)) chain.push(finish);
      break;
    }

    let best = null;
    let bestScore = Infinity;
    const goalBear = bearingDeg(cur, finish);
    for (const { node, d } of cands) {
      const bear = bearingDeg(cur, node);
      const turn = angleDiff(heading, bear);
      const align = angleDiff(goalBear, bear);
      const after = haversine(node, finish);
      const progress = dist + d;
      let score = after * 0.55 + d * 0.2 + turn * 8 + align * 10;
      if (targetM > direct * 1.15) {
        score += Math.abs(progress + after - targetM) * 0.2;
      }
      if (score < bestScore) {
        bestScore = score;
        best = { node, d, bear };
      }
    }
    if (!best) {
      chain.push(finish);
      break;
    }
    chain.push(best.node);
    used.add(nodeKey(best.node));
    dist += best.d;
    heading = best.bear;
  }

  if (nodeKey(chain[chain.length - 1]) !== nodeKey(finish)) chain.push(finish);
  return chain.length >= 2 ? chain : [start, finish];
}
