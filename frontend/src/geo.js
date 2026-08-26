export function haversine(a, b) {
  const radius = 6371000;
  const toRad = (value) => (value * Math.PI) / 180;
  const dLat = toRad(b.lat - a.lat);
  const dLng = toRad(b.lng - a.lng);
  const sin =
    Math.sin(dLat / 2) ** 2 +
    Math.cos(toRad(a.lat)) * Math.cos(toRad(b.lat)) * Math.sin(dLng / 2) ** 2;
  return 2 * radius * Math.asin(Math.sqrt(sin));
}

export function stopSpeaking() {
  window.speechSynthesis?.cancel();
}

export function speak(text) {
  if (!window.speechSynthesis || !text) return;
  stopSpeaking();
  const utterance = new SpeechSynthesisUtterance(text);
  const voices = window.speechSynthesis.getVoices();
  const dutch =
    voices.find((voice) => voice.lang === "nl-BE") ||
    voices.find((voice) => voice.lang?.startsWith("nl"));
  if (dutch) utterance.voice = dutch;
  utterance.lang = dutch?.lang || "nl-NL";
  utterance.rate = 0.96;
  window.speechSynthesis.speak(utterance);
}

export function interpolate(geometry, distanceM) {
  if (!geometry?.length) return null;
  let remaining = distanceM;
  for (let i = 0; i < geometry.length - 1; i += 1) {
    const a = { lat: geometry[i][0], lng: geometry[i][1] };
    const b = { lat: geometry[i + 1][0], lng: geometry[i + 1][1] };
    const segment = haversine(a, b);
    if (remaining <= segment) {
      const t = segment === 0 ? 0 : remaining / segment;
      return {
        lat: a.lat + (b.lat - a.lat) * t,
        lng: a.lng + (b.lng - a.lng) * t,
      };
    }
    remaining -= segment;
  }
  const last = geometry[geometry.length - 1];
  return { lat: last[0], lng: last[1] };
}

export function estimateRouteKm(origin, nodes, loop = true) {
  const points = [];
  if (origin?.lat != null) points.push({ lat: origin.lat, lng: origin.lng });
  for (const node of nodes || []) points.push({ lat: node.lat, lng: node.lng });
  if (loop && origin?.lat != null && nodes?.length) points.push({ lat: origin.lat, lng: origin.lng });
  if (points.length < 2) return 0;
  let meters = 0;
  for (let i = 0; i < points.length - 1; i += 1) meters += haversine(points[i], points[i + 1]);
  return (meters * 1.22) / 1000;
}

export function formatKm(km) {
  const value = Math.max(0, Number(km) || 0);
  if (value < 0.05) return "0 km";
  if (value < 10) return `${value.toFixed(1).replace(".", ",")} km`;
  return `${Math.round(value * 10) / 10} km`.replace(".", ",");
}

export function formatDuration(minutes) {
  const total = Math.max(0, Math.round(Number(minutes) || 0));
  if (!total) return "—";
  const hours = Math.floor(total / 60);
  const mins = total % 60;
  if (hours === 0) return `${mins} min`;
  if (mins === 0) return `${hours} u`;
  return `${hours} u ${mins} min`;
}

export function routeLength(geometry) {
  let total = 0;
  for (let i = 0; i < geometry.length - 1; i += 1) {
    total += haversine(
      { lat: geometry[i][0], lng: geometry[i][1] },
      { lat: geometry[i + 1][0], lng: geometry[i + 1][1] },
    );
  }
  return total;
}

export function formatDistance(meters) {
  const value = Math.max(0, Number(meters) || 0);
  if (value < 1000) return `${Math.max(10, Math.round(value / 10) * 10)} m`;
  return `${(value / 1000).toFixed(1).replace(".", ",")} km`;
}

export function bearingDeg(a, b) {
  const toRad = (value) => (value * Math.PI) / 180;
  const toDeg = (value) => (value * 180) / Math.PI;
  const dLng = toRad(b.lng - a.lng);
  const lat1 = toRad(a.lat);
  const lat2 = toRad(b.lat);
  const y = Math.sin(dLng) * Math.cos(lat2);
  const x = Math.cos(lat1) * Math.sin(lat2) - Math.sin(lat1) * Math.cos(lat2) * Math.cos(dLng);
  return (toDeg(Math.atan2(y, x)) + 360) % 360;
}

export function compassLabel(deg) {
  const dirs = ["N", "NO", "O", "ZO", "Z", "ZW", "W", "NW"];
  return dirs[Math.round(((Number(deg) || 0) % 360) / 45) % 8];
}

export function listenOnce(lang = "nl-BE") {
  return new Promise((resolve, reject) => {
    const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!SpeechRecognition) {
      reject(new Error("Spraakherkenning werkt niet in deze browser. Gebruik Chrome of Edge."));
      return;
    }
    const recognition = new SpeechRecognition();
    recognition.lang = lang;
    recognition.interimResults = false;
    recognition.maxAlternatives = 1;
    recognition.onresult = (event) => {
      const text = event.results?.[0]?.[0]?.transcript?.trim();
      if (text) resolve(text);
      else reject(new Error("Geen spraak herkend."));
    };
    recognition.onerror = () => reject(new Error("Spraakopname mislukt. Probeer opnieuw."));
    recognition.onend = () => {};
    recognition.start();
  });
}

export function uniqueChainIds(nodes) {
  const ids = [];
  const seen = new Set();
  for (const node of nodes || []) {
    const id = nodeId(node);
    if (!id || seen.has(id)) continue;
    seen.add(id);
    ids.push(id);
  }
  return ids;
}

export function dedupeNearbyPoints(points, minM = 700) {
  const kept = [];
  for (const point of points || []) {
    if (!Number.isFinite(point?.lat) || !Number.isFinite(point?.lng)) continue;
    if (kept.some((other) => haversine(other, point) < minM)) continue;
    kept.push(point);
  }
  return kept;
}

export function nodeId(node) {
  return node?.id || `${node?.number}|${Number(node?.lat).toFixed(4)}`;
}

export function poiId(poi) {
  return poi?.id || `${poi?.name}|${Number(poi?.lat).toFixed(4)}`;
}

export function knoopMatches(a, b, maxM = 150) {
  if (!a || !b || String(a.number) !== String(b.number)) return false;
  if (!Number.isFinite(a.lat) || !Number.isFinite(b.lat)) return false;
  return haversine(a, b) <= maxM;
}

export function knoopOnRoute(node, routeNodes) {
  return (routeNodes || []).some((routeNode) => knoopMatches(node, routeNode));
}

export function mergeMapKnooppunten(nearby, routeNodes, pinned = []) {
  const byId = new Map();
  for (const node of nearby || []) byId.set(nodeId(node), node);
  for (const node of routeNodes || []) {
    const id = nodeId(node);
    byId.set(id, { ...byId.get(id), ...node, on_route: true });
  }
  for (const node of pinned || []) {
    const id = nodeId(node);
    byId.set(id, { ...byId.get(id), ...node });
  }
  return [...byId.values()];
}

export function getBrowserLocation() {
  return new Promise((resolve, reject) => {
    if (!navigator.geolocation) {
      reject(new Error("GPS is niet beschikbaar in deze browser."));
      return;
    }
    navigator.geolocation.getCurrentPosition(
      (pos) =>
        resolve({
          lat: pos.coords.latitude,
          lng: pos.coords.longitude,
          accuracy: pos.coords.accuracy || 25,
        }),
      (err) => {
        if (err?.code === 1) {
          reject(new Error("GPS-toegang geweigerd. Sta locatie toe voor deze site."));
        } else if (err?.code === 2) {
          reject(new Error("Locatie niet beschikbaar. Zet GPS aan op je toestel."));
        } else {
          reject(new Error("GPS duurde te lang. Open de site via http://localhost:5173 en probeer opnieuw."));
        }
      },
      { enableHighAccuracy: true, timeout: 15000, maximumAge: 4000 },
    );
  });
}
