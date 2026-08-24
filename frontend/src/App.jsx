import { useMemo, useState } from "react";
import { geocode, planRoute } from "./api.js";
import Planner from "./components/Planner.jsx";
import Ride from "./components/Ride.jsx";

export default function App() {
  const [plan, setPlan] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [preview, setPreview] = useState({ lat: 50.85, lng: 4.35, zoom: 8 });

  const center = useMemo(() => [preview.lat, preview.lng], [preview]);

  async function onPlan(payload) {
    setBusy(true);
    setError("");
    try {
      const next = await planRoute(payload);
      setPlan(next);
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  return plan ? (
    <Ride plan={plan} onPlanChange={setPlan} onBack={() => setPlan(null)} />
  ) : (
    <Planner
      busy={busy}
      error={error}
      center={center}
      onPreview={setPreview}
      onPlan={onPlan}
      geocode={geocode}
    />
  );
}
