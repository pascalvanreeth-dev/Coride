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
    throw new Error(data.detail || "De route kon niet worden gepland.");
  }
  return data;
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
