import { useMemo, useState } from "react";
import { planRoute } from "./api.js";
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
  return null;
}

function notesWantHoreca(text) {
  return /caf[eéè]|taverne|tavern|herberg|koffie|pub|\bbar\b|brasserie|estaminet|bistro/i.test(
    String(text || ""),
  );
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
      if (payload?.local_manual && payload.route_geometry?.length > 1) {
        const notes = (payload.notes || "").trim();
        const interests = [...(payload.interests || [])];
        if (notesWantHoreca(notes) && !interests.includes("horeca")) interests.push("horeca");
        if (!interests.length) interests.push("geschiedenis");

        // Meteen naar rit (geen wachten op wish-API). Ontbrekende tegels laadt Ride op de achtergrond.
        const local = buildManualPlanFromDraft({
          geometry: payload.route_geometry,
          knooppunten: payload.knooppunten,
          distanceKm: payload.distance_km,
          durationMin: payload.route_duration_min,
          mode: payload.mode,
          notes: payload.notes,
          interests,
          poiPicks: payload.poi_picks,
          wishSuggestions: Array.isArray(payload.wish_suggestions) ? payload.wish_suggestions : [],
          profile,
        });
        if (payload.suggestion_id) recordRouteUse(payload.suggestion_id);
        setPlan(local);
        return;
      }
      let next = await planRoute(payload);
      if (payload.suggestion_id) recordRouteUse(payload.suggestion_id);
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
