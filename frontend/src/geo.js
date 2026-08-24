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

export function speak(text) {
  if (!window.speechSynthesis || !text) return;
  window.speechSynthesis.cancel();
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

export function nodeId(node) {
  return node?.id || `${node?.number}|${Number(node?.lat).toFixed(4)}`;
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
