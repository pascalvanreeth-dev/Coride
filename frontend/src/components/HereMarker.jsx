import { Circle, Marker, Popup } from "react-leaflet";
import L from "leaflet";

export const hereIcon = L.divIcon({
  className: "here-marker",
  html: `<div class="here-dot"><span class="here-pulse"></span></div>`,
  iconSize: [36, 36],
  iconAnchor: [18, 18],
});

export default function HereMarker({ position, accuracy = 0 }) {
  if (!position) return null;
  const radius = Math.min(Math.max(Number(accuracy) || 0, 0), 180);

  return (
    <>
      {radius > 8 && (
        <Circle
          center={[position.lat, position.lng]}
          radius={radius}
          pathOptions={{
            color: "#2563eb",
            fillColor: "#2563eb",
            fillOpacity: 0.22,
            weight: 2.5,
            opacity: 0.85,
          }}
        />
      )}
      <Marker position={[position.lat, position.lng]} icon={hereIcon} zIndexOffset={1400}>
        <Popup>Je bent hier</Popup>
      </Marker>
    </>
  );
}
