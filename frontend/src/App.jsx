import { useMemo, useState } from "react";
import { fetchWishSuggestions, planRoute } from "./api.js";
import Onboarding from "./components/Onboarding.jsx";
import Planner from "./components/Planner.jsx";
import Ride from "./components/Ride.jsx";
import { readCachedLocation } from "./geo.js";
import { buildManualPlanFromDraft } from "./manualPlan.js";
import { loadProfile, saveProfile } from "./profile.js";
import { recordRouteUse } from "./routeHistory.js";

function initialPreview() {
  const cached = readCachedLocation();
  if (cached) return { lat: cached.lat, lng: cached.lng, zoom: 14 };
  // Geen Gent-default: Planner wacht op GPS vóór de kaart mount.
  return null;
}

function suggestionsToStops(suggestions, notes = "", interests = []) {
  return (suggestions || []).slice(0, 16).map((poi, index) => ({
    id: String(poi.id || `wish-${index}`),
    name: poi.name || "Plek",
    lat: Number(poi.lat),
    lng: Number(poi.lng),
    kind: poi.kind_label || poi.kind || "plek",
    interest: poi.interest || interests[0] || "geschiedenis",
    source: "OpenStreetMap",
    summary: poi.hint || "Suggestie langs je route.",
    approaching: `Je nadert ${poi.name || "deze plek"}.`,
    arrived: `Je bent bij ${poi.name || "deze plek"}.`,
    why: notes?.trim()
      ? `Past bij je wens: ${notes.trim()}`
      : poi.hint === "uit je profiel"
        ? "Past bij je profiel."
        : "Suggestie langs je route.",
    wikipedia_url: null,
    image_url: null,
    wikipedia: null,
    wikidata: null,
    description: "",
    place_name: null,
    population: null,
    local_fact: null,
    side: null,
    matches_wish: true,
    on_route: Boolean(poi.on_route),
  }));
}

function sampleGeometryForWish(geometry, maxPoints = 40) {
  if (!geometry?.length) return [];
  if (geometry.length <= maxPoints) return geometry.map((pt) => [Number(pt[0]), Number(pt[1])]);
  const out = [];
  const step = (geometry.length - 1) / (maxPoints - 1);
  for (let i = 0; i < maxPoints; i += 1) {
    const pt = geometry[Math.min(geometry.length - 1, Math.round(i * step))];
    out.push([Number(pt[0]), Number(pt[1])]);
  }
  return out;
}

async function enrichPlanWithWishStops(plan, payload) {
  if (!plan || (plan.stops || []).some((stop) => stop.matches_wish)) return plan;
  if (!plan.geometry?.length) return plan;
  if (!(payload.notes?.trim() || payload.interests?.length)) return plan;
  try {
    const data = await Promise.race([
      fetchWishSuggestions({
        notes: payload.notes || "",
        interests: payload.interests || [],
        geometry: sampleGeometryForWish(plan.geometry, 48),
        nodes: plan.knooppunten || [],
      }),
      new Promise((_, reject) => setTimeout(() => reject(new Error("timeout")), 18000)),
    ]);
    const wishStops = suggestionsToStops(data?.suggestions, payload.notes, payload.interests);
    if (!wishStops.length) return plan;
    return { ...plan, stops: [...wishStops, ...(plan.stops || [])] };
  } catch {
    return plan;
  }
}

export default function App() {
  const [profile, setProfile] = useState(() => loadProfile());
  const [editProfile, setEditProfile] = useState(false);
  const [plan, setPlan] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [preview, setPreview] = useState(initialPreview);

  const center = useMemo(
    () => (preview ? [preview.lat, preview.lng] : null),
    [preview],
  );

  function completeProfile(next) {
    const saved = saveProfile(next);
    setProfile(saved);
    setEditProfile(false);
  }

  async function onPlan(payload) {
    setBusy(true);
    setError("");
    try {
      // Zelf gekozen knooppunten: start meteen vanuit de magenta draft (geen timeout).
      if (payload?.local_manual && payload.route_geometry?.length > 1) {
        let wishSuggestions = Array.isArray(payload.wish_suggestions) ? payload.wish_suggestions : [];
        // Als er nog geen suggesties zijn: snel ophalen vóór de rit start.
        if (!wishSuggestions.length && (payload.notes?.trim() || payload.interests?.length)) {
          try {
            const data = await Promise.race([
              fetchWishSuggestions({
                notes: payload.notes || "",
                interests: payload.interests || [],
                geometry: sampleGeometryForWish(payload.route_geometry, 48),
                nodes: payload.knooppunten || [],
              }),
              new Promise((_, reject) => setTimeout(() => reject(new Error("timeout")), 18000)),
            ]);
            wishSuggestions = Array.isArray(data?.suggestions) ? data.suggestions : [];
          } catch {
            wishSuggestions = [];
          }
        }
        const local = buildManualPlanFromDraft({
          geometry: payload.route_geometry,
          knooppunten: payload.knooppunten,
          distanceKm: payload.distance_km,
          durationMin: payload.route_duration_min,
          mode: payload.mode,
          notes: payload.notes,
          interests: payload.interests,
          poiPicks: payload.poi_picks,
          wishSuggestions,
          profile,
        });
        setPlan(local);
        return;
      }
      let next = await planRoute(payload);
      if (payload.suggestion_id) recordRouteUse(payload.suggestion_id);
      next = await enrichPlanWithWishStops(next, payload);
      setPlan(next);
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  if (!profile || editProfile) {
    return <Onboarding initial={profile} onComplete={completeProfile} />;
  }

  return plan ? (
    <Ride plan={plan} onPlanChange={setPlan} onBack={() => setPlan(null)} />
  ) : (
    <Planner
      busy={busy}
      error={error}
      center={center}
      zoom={preview?.zoom ?? 14}
      profile={profile}
      onEditProfile={() => setEditProfile(true)}
      onPreview={setPreview}
      onPlan={onPlan}
      onClearError={() => setError("")}
    />
  );
}
