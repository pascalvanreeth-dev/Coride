import { Circle, Marker, Popup } from "react-leaflet";
import L from "leaflet";

export const hereIcon = L.divIcon({
  className: "here-marker",
  html: `<div class="here-dot"><span class="here-pulse"></span></div>`,
  iconSize: [22, 22],
  iconAnchor: [11, 11],
});

export default function HereMarker({ position, accuracy = 0 }) {
  if (!position) return null;
  const radius = Math.min(Math.max(Number(accuracy) || 0, 0), 140);
  return (
    <>
      {radius > 10 && (
        <Circle
          center={[position.lat, position.lng]}
          radius={radius}
          pathOptions={{ color: "#2f80ed", fillColor: "#2f80ed", fillOpacity: 0.18, weight: 1 }}
        />
      )}
      <Marker position={[position.lat, position.lng]} icon={hereIcon} zIndexOffset={1200}>
        <Popup>Je bent hier</Popup>
      </Marker>
    </>
  );
}
