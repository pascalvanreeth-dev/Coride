import { useMemo, useState } from "react";
import { geocode, planRoute } from "./api.js";
import Onboarding from "./components/Onboarding.jsx";
import Planner from "./components/Planner.jsx";
import Ride from "./components/Ride.jsx";
import { loadProfile, saveProfile } from "./profile.js";
import { recordRouteUse } from "./routeHistory.js";

export default function App() {
  const [profile, setProfile] = useState(() => loadProfile());
  const [editProfile, setEditProfile] = useState(false);
  const [plan, setPlan] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [preview, setPreview] = useState({ lat: 50.85, lng: 4.35, zoom: 8 });

  const center = useMemo(() => [preview.lat, preview.lng], [preview]);

  function completeProfile(next) {
    const saved = saveProfile(next);
    setProfile(saved);
    setEditProfile(false);
  }

  async function onPlan(payload) {
    setBusy(true);
    setError("");
    try {
      const next = await planRoute(payload);
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
      profile={profile}
      onEditProfile={() => setEditProfile(true)}
      onPreview={setPreview}
      onPlan={onPlan}
      geocode={geocode}
    />
  );
}
