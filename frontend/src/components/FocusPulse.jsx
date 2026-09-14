import { useEffect } from "react";
import { Circle, Marker } from "react-leaflet";
import L from "leaflet";
import { isValidLatLng } from "../geo.js";

const pulseIcon = L.divIcon({
  className: "focus-pulse-marker",
  html: `
    <div class="focus-pulse-halo" aria-hidden="true">
      <span class="focus-pulse-wave"></span>
      <span class="focus-pulse-wave delay"></span>
    </div>
  `,
  iconSize: [140, 140],
  iconAnchor: [70, 70],
});

/** Tijdelijke pulscirkel om een aangeklikte suggestie op de kaart te laten opvallen. */
export default function FocusPulse({ position, token, durationMs = 3400, radiusM = 95, onDone }) {
  useEffect(() => {
    if (!position || !token) return undefined;
    const timer = setTimeout(() => onDone?.(), durationMs);
    return () => clearTimeout(timer);
    // onDone is intentionally omitted — parents pass inline setters.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [position?.lat, position?.lng, token, durationMs]);

  if (!position || !isValidLatLng(position.lat, position.lng) || !token) {
    return null;
  }

  const lat = Number(position.lat);
  const lng = Number(position.lng);

  return (
    <>
      <Circle
        key={`focus-circle-${token}`}
        center={[lat, lng]}
        radius={radiusM}
        pathOptions={{
          color: "#c70068",
          fillColor: "#c70068",
          fillOpacity: 0.2,
          weight: 3,
          opacity: 0.95,
          className: "focus-pulse-circle",
        }}
      />
      <Marker
        key={`focus-halo-${token}`}
        position={[lat, lng]}
        icon={pulseIcon}
        interactive={false}
        keyboard={false}
        zIndexOffset={2200}
      />
    </>
  );
}
